#!/usr/bin/env python3
"""演练：用本机假上游把「解析 → 探测 → 定档 → diff → 写回」整条链跑完。

    cd /opt/deploy/upstream-importer
    python3 tools/rehearse.py /opt/deploy/config.yaml

这不是测试，是**演练**。它做真实流程会做的每一件事，只有两点不同：
  · 上游是本机假服务（零外网、零成本、不触发限频）
  · 写回落到临时副本，**你的 config.yaml 一个字节都不动**

用途：上真站之前先看清「输出长什么样、diff 会插什么、priority 会给多少」。
真实运行时把 --dry-run 去掉、把 accounts.txt 换成真站，输出格式完全一致。

七种画像对应七类真实站点，account 文件由脚本自己生成：
  good        四段全通
  quota       余额耗尽（403 + 预扣费额度失败）
  cfguard     Cloudflare 拦截，代理可救
  identity    缺标识头，补 UA 可救
  swapper     静默换模（照常计费却回另一个模型）
  truncator   上下文截断（200 但只吃 300k）
  compatonly  只有 compat 段通
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import cpa_probe as cp  # noqa: E402
from cpa_probe.pipeline import Prober  # noqa: E402
from cpa_probe.writeback import (  # noqa: E402
    apply_diffs,
    build_diffs,
    validate,
    write_local,
)

C_OK, C_BAD, C_WARN, C_DIM, C_CYAN, C_END = (
    "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[96m", "\033[0m")

PROFILES = ["good", "quota", "cfguard", "identity",
            "swapper", "truncator", "compatonly"]

CATALOG = [
    "gpt-5.6-sol", "gpt-5.6-terra", "claude-opus-5", "claude-sonnet-5",
    "gemini-2.5-pro", "gemini-2.5-flash",
    "BAAI/bge-large-zh", "DeepSeek-V3", "42-mini", "Business/gemini-2.5-pro",
]


class FakeUpstream(BaseHTTPRequestHandler):
    """按 URL 第一段决定画像。与 tests/test_pipeline.py 同一套逻辑。"""

    def log_message(self, *a) -> None:
        pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return {}

    def _send(self, code: int, payload) -> None:
        raw = (payload if isinstance(payload, str)
               else json.dumps(payload, ensure_ascii=False)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _split(self) -> tuple[str, str]:
        p = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        return (p[0] if p else ""), "/" + "/".join(p[1:])

    @staticmethod
    def _sent(body: dict) -> int:
        if "input" in body:
            return len(str(body["input"]))
        for m in body.get("messages") or []:
            if isinstance(m, dict):
                return len(str(m.get("content") or ""))
        for c in body.get("contents") or []:
            for part in (c.get("parts") or []):
                return len(str(part.get("text") or ""))
        return 0

    @staticmethod
    def _ok(model: str, tokens: int, *, rid: str = "msg_01AbCdEfGhJiKm") -> dict:
        return {"id": rid, "model": model,
                "usage": {"input_tokens": tokens, "output_tokens": 12},
                "content": [{"type": "text", "text": "A hash map is unordered."}]}

    def do_GET(self) -> None:  # noqa: N802
        profile, path = self._split()
        if "models" not in path:
            self._send(404, {"error": {"message": "not found"}})
            return
        if profile in ("quota", "cfguard", "identity"):
            self._send(403, {"error": {"message": "forbidden"}})
            return
        if profile == "compatonly" and "/v1beta/" in path:
            self._send(404, {"error": {"message": "not found"}})
            return
        if "/v1beta/" in path:
            self._send(200, {"models": [{"name": f"models/{m}"} for m in CATALOG]})
        else:
            self._send(200, {"data": [{"id": m} for m in CATALOG]})

    def do_POST(self) -> None:  # noqa: N802
        profile, path = self._split()
        body = self._body()
        model = str(body.get("model") or "")
        if not model:
            m = re.search(r"/models/([^:]+):", path)
            model = m.group(1) if m else "?"
        sent = self._sent(body)

        if profile == "quota":
            self._send(403, {"error": {"message": "预扣费额度失败，剩余额度 $0.190928"}})
        elif profile == "cfguard":
            if self.headers.get("X-Via-Proxy") == "1":
                self._send(200, self._ok(model, max(sent, 20)))
            else:
                self._send(403, "")
        elif profile == "identity":
            if self.headers.get("User-Agent") or self.headers.get("Originator"):
                self._send(200, self._ok(model, max(sent, 20)))
            else:
                self._send(401, {"error": {"message": "unauthorized client"}})
        elif profile == "swapper":
            self._send(200, self._ok("agnes-2.0-flash", max(sent, 20),
                                     rid="chatcmpl-xyz123"))
        elif profile == "truncator":
            self._send(200, self._ok(model, min(max(sent, 20), 300_000)))
        elif profile == "compatonly":
            if path.endswith("/chat/completions"):
                self._send(200, self._ok(model, max(sent, 20)))
            else:
                self._send(404, {"error": {"message": "model_not_found"}})
        else:
            self._send(200, self._ok(model, max(sent, 20)))


class ProxyMarkingProber(Prober):
    """把 proxy 参数变成可观测的头 —— 假上游据此判断「是否经代理」。"""

    def _call(self, *a, **kw):
        if kw.get("proxy"):
            kw["extra_headers"] = {**(kw.get("extra_headers") or {}),
                                   "X-Via-Proxy": "1"}
            kw["proxy"] = None
        return super()._call(*a, **kw)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(ROOT), "config.yaml")
    if not os.path.isfile(cfg_path):
        print(f"找不到 config.yaml：{cfg_path}")
        print("用法：python3 tools/rehearse.py [config.yaml 路径]")
        return 2

    import yaml

    real_raw = io.open(cfg_path, encoding="utf-8").read()
    cfg = yaml.safe_load(real_raw)

    # 关键：所有写回操作都对临时副本做，真文件只读一次
    tmpdir = tempfile.mkdtemp(prefix="rehearse-")
    work = os.path.join(tmpdir, "config.yaml")
    shutil.copy2(cfg_path, work)

    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), FakeUpstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("=" * 70)
    print("演练：整条链跑一遍（假上游 · 零外网 · 不动真 config.yaml）")
    print("=" * 70)
    print(f"  真 config.yaml : {cfg_path}")
    print(f"                   {len(real_raw.splitlines())} 行 · "
          f"四段 {sum(len(cfg.get(s) or []) for s in cp.SECTIONS)} 条目 "
          f"{C_DIM}（只读，不会被修改）{C_END}")
    print(f"  写回目标       : {work} {C_DIM}（临时副本）{C_END}")
    print(f"  假上游         : {base}")

    # ---------------- ① 输入 ----------------
    acc = os.path.join(tmpdir, "accounts.txt")
    lines = [f"{base}/{p},sk-rehearse-{p}-0001" for p in PROFILES]
    lines += ["# 井号行会被忽略", "", "格式错误的行没有逗号"]
    io.open(acc, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    print(f"\n{C_CYAN}① 输入{C_END}  {acc}")
    for l in lines:
        print(f"     {C_DIM}{l}{C_END}")

    # ---------------- ② 解析 ----------------
    parsed = cp.parse_lines(io.open(acc, encoding="utf-8").read())
    print(f"\n{C_CYAN}② 解析{C_END}  有效 {len(parsed.valid)} · "
          f"无效 {len(parsed.invalid)}")
    for r in parsed.invalid:
        print(f"     {C_BAD}✗{C_END} 第 {r.line_no} 行：{r.error}")
    r0 = parsed.valid[0]
    print(f"     {C_DIM}以 {r0.host} 为例，四段 base-url 自动规范化：{C_END}")
    for s in cp.SECTIONS:
        print(f"       {s:<22} {r0.base_for(s)}")

    # ---------------- ③ 探测 ----------------
    print(f"\n{C_CYAN}③ 探测{C_END}  {len(parsed.valid)} 个候选 × 4 段")
    print(f"     {C_DIM}真实运行时这里会花钱；假上游免费{C_END}\n")

    def on_event(kind: str, data: dict) -> None:
        if kind == "candidate-start":
            print(f"  {C_CYAN}▸ {data['host']}{C_END}  {data.get('key', '')}")
        elif kind == "attempt":
            st = data["status"]
            c = C_OK if st == "200" else (C_WARN if st[0] == "4" else C_BAD)
            print(f"      {c}{st:<4}{C_END} {data['section']:<22} "
                  f"{data['model']:<18} {data['combo']:<16} {data['category']}")
        elif kind == "catalog":
            print(f"      {C_DIM}目录 {data['count']} 个模型"
                  f"（已按 gemini/gpt/claude 过滤）{C_END}")
        elif kind == "model-rejected":
            print(f"      {C_BAD}拒收{C_END} 请求 {data['requested']} "
                  f"却回 {data['actual']}（{data['backend']}）")
        elif kind == "context":
            tag = "（截断反推）" if data.get("untrusted") else ""
            print(f"      {C_DIM}上下文上限 {data['limit']:,}{tag}{C_END}")

    prober = ProxyMarkingProber(
        proxy="http://fake-proxy:7890", gap=0.0, timeout=10,
        probe_context=True, swap_samples=3, on_event=on_event,
    )

    # raw=real_raw 而不是 raw：那个名字要到 ⑤ 才被赋值（读临时副本），
    # 在这里引用它就是 UnboundLocalError —— 本脚本自建仓起就是坏的，
    # 因为它不在 tests/run.py 的套件里，没人跑到过这一行。
    # 定档要读注释里的「实测不可用」结论，档位差得很远（claude 段实测 175 vs 500）。
    bands = {s: cp.build_band(cfg, s, raw=real_raw) for s in cp.SECTIONS}
    seen = cp.existing_fingerprints(cfg)
    results, plans = [], []
    for row in parsed.valid:
        res = prober.probe(row)
        results.append(res)
        plans.append(cp.build_plan(row, res, cfg, bands=bands, seen=seen,
                                   raw=real_raw))

    # 批量定档 —— 与 server.py 的 _api_plan、cli.py 同一个函数。演练要走
    # 完整链路，漏了它这里给出的 priority 就与真实落盘不一致。
    prio_warns = cp.assign_priorities(plans, cfg, raw=real_raw)

    # ---------------- ④ 判定 ----------------
    print(f"\n{C_CYAN}④ 判定与定档{C_END}")
    for w in prio_warns:
        print(f"  {C_WARN}⚠{C_END} {w}")
    for res, plan in zip(results, plans):
        print(f"\n  {C_CYAN}{res.row.host}{C_END}  "
              f"{res.row.masked()}  {res.total_calls} 次请求")
        for sec in cp.SECTIONS:
            v = res.sections.get(sec)
            if v is None:
                continue
            sp = plan.sections.get(sec)
            if v.usable and sp:
                mark = f"{C_OK}✓{C_END}"
                extra = f"priority {sp.priority}"
                if sp.duplicate:
                    mark, extra = f"{C_WARN}={C_END}", "已存在，跳过"
                elif not sp.writable:
                    mark, extra = f"{C_WARN}!{C_END}", "无可信模型，不写入"
                print(f"    {mark} {sec:<22} {v.summary()}")
                print(f"        {C_DIM}{extra} · {sp.priority_reason}{C_END}")
                for w in sp.warnings:
                    print(f"        {C_WARN}⚠ {w}{C_END}")
            else:
                why = plan.skipped.get(sec, v.summary())
                print(f"    {C_BAD}✗{C_END} {sec:<22} {why}")

    # ---------------- ⑤ diff ----------------
    raw = io.open(work, encoding="utf-8").read()
    diffs = build_diffs(raw, plans)
    n_lines = sum(len(d.lines) for d in diffs)
    print(f"\n{C_CYAN}⑤ diff 预览{C_END}  {len(diffs)} 处插入 · 共 {n_lines} 行")
    for d in diffs:
        print(f"\n  {C_OK}+++{C_END} {d.section}  ← {d.host}  "
              f"（第 {d.insert_at} 行后）")
        for l in d.lines:
            print(f"  {C_OK}+{C_END} {l}")

    if not diffs:
        print(f"  {C_WARN}无可写入条目 —— 全部不可用或已存在{C_END}")

    # ---------------- ⑥ 写回（临时副本） ----------------
    merged = apply_diffs(raw, diffs)
    ok, msg = validate(merged)
    print(f"\n{C_CYAN}⑥ 校验与写回{C_END}")
    print(f"     {C_OK if ok else C_BAD}{msg}{C_END}")
    if not ok:
        srv.shutdown()
        return 1

    bak = write_local(work, merged)
    print(f"     {C_OK}✓{C_END} 已写入临时副本 {work}")
    print(f"       备份 {bak}")

    new_cfg = yaml.safe_load(io.open(work, encoding="utf-8").read())
    print(f"\n     {'段':<22} {'原条目':>6} {'新条目':>6} {'增量':>5}")
    for s in cp.SECTIONS:
        a, b = len(cfg.get(s) or []), len(new_cfg.get(s) or [])
        print(f"     {s:<22} {a:>6} {b:>6} {b - a:>+5}")

    n_old = sum(1 for l in raw.split("\n") if l.strip().startswith("#"))
    n_new = sum(1 for l in merged.split("\n") if l.strip().startswith("#"))
    tag = f"{C_OK}一条未丢{C_END}" if n_new >= n_old else f"{C_BAD}丢了{C_END}"
    print(f"\n     整行注释 {n_old} → {n_new}  {tag}")

    changed = []
    for s in cp.SECTIONS:
        old = [e.get("priority") for e in (cfg.get(s) or []) if isinstance(e, dict)]
        cur = [e.get("priority") for e in (new_cfg.get(s) or []) if isinstance(e, dict)]
        if cur[:len(old)] != old:
            changed.append(s)
    if changed:
        print(f"     {C_BAD}现有 priority 被改动：{changed}{C_END}")
    else:
        print(f"     {C_OK}现有条目 priority 一个未动{C_END}")

    # ---------------- ⑦ 端到端验证 ----------------
    print(f"\n{C_CYAN}⑦ 端到端验证{C_END}  "
          f"{C_DIM}（真实运行时打 CPA 自己的端点；这里用假上游代替）{C_END}")
    n_ok = n_bad = 0
    for plan in plans:
        for sec, sp in plan.sections.items():
            if not sp.writable or not sp.models:
                continue
            prof = plan.host.split(":")[-1] if ":" in plan.host else plan.host
            vok, vmsg = cp.verify_upstream(
                f"{base}/{sp.base_url.rstrip('/v1').split('/')[-1]}",
                "sk-fake-client-key", sec, sp.models[0], timeout=10,
            )
            mark = f"{C_OK}✓{C_END}" if vok else f"{C_BAD}✗{C_END}"
            print(f"     {mark} {plan.host:<34} {sec:<22} {vmsg}")
            n_ok += vok
            n_bad += (not vok)
    print(f"\n     通过 {n_ok} · 失败 {n_bad}")

    srv.shutdown()

    # ---------------- 收尾 ----------------
    after = io.open(cfg_path, encoding="utf-8").read()
    print(f"\n{'=' * 70}")
    if after == real_raw:
        print(f"{C_OK}✓ 真 config.yaml 逐字节未变{C_END}  {cfg_path}")
    else:
        print(f"{C_BAD}✗ 真 config.yaml 被修改了！这是 bug，请报告{C_END}")
        return 1

    print(f"\n临时目录（可自行查看合并结果，之后可删）：\n  {tmpdir}")
    print(f"\n{C_DIM}真实运行只需换两处：accounts.txt 填真站、去掉 --dry-run。"
          f"输出格式与上面完全一致。{C_END}")
    print("""
下一步（真站）：
  cd /opt/deploy
  cat > accounts.txt <<'TXT'
  https://你的新站.com,sk-真实key
  TXT
  python3 upstream-importer/cli.py -i accounts.txt --dry-run      # 零请求
  python3 upstream-importer/cli.py -i accounts.txt --no-context   # 小成本探测
  python3 upstream-importer/cli.py -i accounts.txt --write        # 写回
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
