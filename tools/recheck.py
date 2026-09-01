#!/usr/bin/env python3
"""复核 config.yaml 里**既有**凭据的真实可用性。

    python3 tools/recheck.py [config.yaml 路径]
    python3 tools/recheck.py /opt/deploy/config.yaml --section claude-api-key
    python3 tools/recheck.py ../config.yaml --top-only     # 只测顶层（快）

与 upstream-importer 主流程的区别 —— 这一条很重要
--------------------------------------------------
主流程（cli.py / server.py）探测的是**新站**：它们还没进 config.yaml，
没有 models 字段，所以只能用 SEED_MODELS 里的种子模型去猜。

而复核既有站**绝不能用种子模型**。2026-08-30 我拿 `gpt-5.6-sol` 统一测
codex 段 11 个站，得出「0/11 可用」的结论 —— 完全错误，两层原因：

  ① **7 个站根本不声明 gpt-5.6-sol**。
     CPA 只把请求路由到声明了该模型的凭据（service_models.go 的注册表），
     拿一个站没注册的模型去打，403/404/503 是必然的 —— 那不是站坏了，
     是测错了。用户当场指出：CPAMP 面板上 cielo 显示 100% 成功率。

  ② **codex 段走 /v1/responses，不是 /v1/chat/completions**。
     config.yaml 的注释早就记了：「11 个站点中 2 个真正实现 Responses」。
     那些 `500 not implemented` 是**站方不支持这个协议**，不是故障。
     我把「协议不支持」误报成「站挂了」。

### 第四条铁律：别把好站打成限流

高并发复核会制造**假阴性**。2026-08-30 深夜实测：用 12 线程跑全段，
compat 段的 relay-i 返回 `429 Too many requests` —— 而低并发时它是可用的。
**是我打太快把它打成限流了。**

假阴性比漏测更糟：它会让人把好站当坏站处理（降档、加 weight:0），
而那是不可逆的判断错误。所以本工具按 (站, 段) 分桶节流，默认 3 秒。

    --gap 5      # 撞上限流严的站就调大
    --gap 0      # 只在你确定站方不限频时用
    --workers 4  # 降并发也能缓解，但节流比降并发更精准
                 # （不同站之间本来就该并行）

所以本工具的铁律：
  · 模型 = 从该条目的 `models` 字段读，**逐个都测**，不用种子
  · 路径 = 按段走对应协议（cpa_probe.request 已按段处理）
  · **请求头 = CPA 实际转发时那一套**（CPA_DEFAULT_UA[段] + 条目自己的
    headers）。不带的话 cielo 会全返 401 —— 我第一版就是这么
    误报「codex 段 0 可用」的，而面板上它是 100% 成功率。
  · 一个站在 A 段不可用，**不代表**它在 B 段不可用（foxtrot 在 claude
    段实测 200，在 codex 段是 500 not implemented —— 因为它不实现 Responses）

输出的「可用」只对「这个 key + 这个站 + 这个段 + 这个模型」四元组成立。
"""

from __future__ import annotations

import collections
import concurrent.futures as cf
import io
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

SECTIONS = ("gemini-api-key", "codex-api-key", "claude-api-key",
            "openai-compatibility")

C_OK, C_BAD, C_WARN, C_DIM, C_END = (
    "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m")
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C_OK = C_BAD = C_WARN = C_DIM = C_END = ""


def host_of(url: str) -> str:
    s = str(url or "")
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
    return s.split("/")[0] or "(无 base-url)"


def declared_models(entry: dict) -> list[str]:
    """这个条目**自己声明**的模型名。空表示没写 models 字段。

    取 alias 优先于 name —— alias 是对外暴露的名字，客户端按它请求
    （applyModelPrefixes / service_models.go 的注册用的是这个）。
    """
    out = []
    for m in entry.get("models") or []:
        if isinstance(m, str):
            out.append(m)
        elif isinstance(m, dict):
            n = m.get("alias") or m.get("name")
            if n:
                out.append(str(n))
    return out


def weight_zero(entry: dict, section: str) -> bool:
    """已被 weight<=0 逐出调度池 —— 与 CPA 的 Normalize 同口径。"""
    def zero(w) -> bool:
        if w is None:
            return False
        try:
            return float(w) <= 0
        except (TypeError, ValueError):
            return False

    if section == "openai-compatibility":
        ws = [ke.get("weight") if isinstance(ke, dict) and "weight" in ke else None
              for ke in (entry.get("api-key-entries") or [])]
        return bool(ws) and all(zero(w) for w in ws)
    return zero(entry.get("weight")) if "weight" in entry else False


def build_jobs(cfg: dict, want_sec: str, top_only: bool) -> list[dict]:
    """摊平成待测清单。每项 = (段, 站, key, 模型) 四元组。"""
    from cpa_probe import request as R

    jobs: list[dict] = []
    for sec in SECTIONS:
        if want_sec and sec != want_sec:
            continue
        entries = [e for e in (cfg.get(sec) or []) if isinstance(e, dict)]
        if not entries:
            continue
        live = [e for e in entries if not weight_zero(e, sec)]
        if not live:
            continue
        if top_only:
            top = max(int(e.get("priority") or 0) for e in live)
            live = [e for e in live if int(e.get("priority") or 0) == top]

        for e in live:
            models = declared_models(e)
            if not models:
                # 没写 models 的条目 CPA 会用内置目录兜底，这里跳过并单独报
                jobs.append({"sec": sec, "host": host_of(e.get("base-url")),
                             "entry": e, "model": "", "skip": "未声明 models"})
                continue
            if sec == "openai-compatibility":
                keys = [str(ke.get("api-key") or "")
                        for ke in (e.get("api-key-entries") or [])
                        if isinstance(ke, dict) and ke.get("api-key")]
            else:
                keys = [str(e.get("api-key") or "")]
            for key in keys:
                if not key:
                    continue
                for model in models:
                    jobs.append({"sec": sec, "host": host_of(e.get("base-url")),
                                 "entry": e, "key": key, "model": model,
                                 "skip": ""})
    return jobs


class _Throttle:
    """按 (站, 段) 分桶的请求间隔控制。

    为什么必须有（2026-08-30 深夜实测踩到）：用 12 线程并发复核时，
    compat 段的 relay-i 返回 **429 Too many requests** —— 上一轮低并发
    测它是可用的。**是我打太快把它打成限流了**，工具于是报出假阴性。

    这比漏测更糟：假阴性会让人把好站当坏站处理（降档、加 weight:0），
    而那是不可逆的判断错误。

    分桶键取 (站, 段) 而不是全局：站方的限频是按端点计的，四段打不同
    路径，各自计时不会放松任何一段的限制 —— 与 cpa_probe.pipeline
    的 _throttle 同口径（见那里的说明）。
    """

    def __init__(self, gap: float = 3.0):
        self.gap = gap
        self._last: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str, section: str) -> None:
        key = (host, section)
        with self._lock:
            last = self._last.get(key, 0.0)
            delay = self.gap - (time.monotonic() - last)
            # 记「即将发出」的时刻，避免同桶多线程一起放行
            self._last[key] = time.monotonic() + max(delay, 0.0)
        if delay > 0:
            time.sleep(delay)


_THROTTLE = _Throttle()


def probe_one(job: dict, timeout: int) -> dict:
    from cpa_probe import client as C
    from cpa_probe import request as R
    from cpa_probe.classify import body_excerpt, classify
    from cpa_probe.fingerprint import model_matches, resp_model

    if job.get("skip"):
        return {**job, "status": "-", "cat": "跳过", "ms": 0,
                "note": job["skip"], "ok": None}

    e, model = job["entry"], job["model"]
    base = str(e.get("base-url") or "")
    proxy = e.get("proxy-url") or None

    # 同站同段之间保持间隔 —— 否则高并发会把好站打成 429，报出假阴性
    _THROTTLE.wait(job["host"], job["sec"])

    # 请求头必须与 **CPA 实际转发时** 一致，否则测的不是真实链路。
    #
    # 2026-08-30 踩到：直连不带任何头去测 cielo 的 codex 段，21 个组合
    # 全部 401 unauthorized client，于是我报「codex 段无可用组合」——
    # 而用户截图里 CPAMP 面板显示 cielo **100% 成功率**。
    #
    # 逐头对照实测（cielo / gpt-5.6-sol）：
    #     cpa-现状（带 codex UA）  200 ✓
    #     originator-only          401
    #     ua-only-codex            200 ✓
    #     ua-only-browser          401
    #     codex-全量               200 ✓
    # 该站要的就是 codex 客户端的 User-Agent。CPA 转发时带了它，我没带。
    #
    # 所以基线头取 CPA_DEFAULT_UA[段]（cpa_probe/request.py:46-51 —— 那是
    # 从 CPA 源码抄录的实际转发值），再叠加条目自己配的 headers。
    # 条目 headers 优先：它是用户为该站特意钉死的，覆盖默认值才对。
    from cpa_probe.request import CPA_DEFAULT_UA

    hdrs: dict[str, str] = {}
    cpa_ua = CPA_DEFAULT_UA.get(job["sec"])
    if cpa_ua:
        hdrs["User-Agent"] = cpa_ua
    for k, v in (e.get("headers") or {}).items():
        hdrs[str(k)] = str(v)
    hdrs = hdrs or None
    t0 = time.monotonic()
    try:
        url, headers, body = R.build_request(
            job["sec"], base, model, job["key"], extra_headers=hdrs)
        r = C.send(url, headers=headers,
                   body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                   proxy=proxy, timeout=timeout)
        ms = int((time.monotonic() - t0) * 1000)
        cat, _ = classify(r.status, r.body)
        rm = resp_model(r.body) or ""
        # 三个独立的失败面，任一命中就不算「能出活」
        is_html = "<html" in r.body[:400].lower()
        swapped = bool(rm) and not model_matches(model, rm)
        ok = (r.status == "200") and not is_html and not swapped
        note = rm or body_excerpt(r.body, 34)
        if is_html:
            note += " [HTML拦截页]"
        if swapped:
            note += f" [换模->{rm}]"
        return {**job, "status": r.status, "cat": cat, "ms": ms,
                "note": note, "ok": ok}
    except Exception as ex:                                  # noqa: BLE001
        return {**job, "status": "ERR", "cat": "", "note": type(ex).__name__,
                "ms": int((time.monotonic() - t0) * 1000), "ok": False}


def main() -> int:
    args = sys.argv[1:]
    path = next((a for a in args if not a.startswith("-")), "")
    if not path:
        path = os.path.join(os.path.dirname(ROOT), "config.yaml")
    want_sec = ""
    if "--section" in args:
        i = args.index("--section")
        if i + 1 < len(args):
            want_sec = args[i + 1]
    top_only = "--top-only" in args
    workers = 6
    if "--workers" in args:
        i = args.index("--workers")
        if i + 1 < len(args):
            workers = max(1, int(args[i + 1]))
    if "--gap" in args:
        i = args.index("--gap")
        if i + 1 < len(args):
            _THROTTLE.gap = max(0.0, float(args[i + 1]))
    timeout = 50
    if "--timeout" in args:
        i = args.index("--timeout")
        if i + 1 < len(args):
            timeout = max(5, int(args[i + 1]))

    if not os.path.exists(path):
        print(f"找不到 {path}")
        return 2
    try:
        import yaml
    except ImportError:
        print("需要 PyYAML：pip3 install pyyaml")
        return 2

    cfg = yaml.safe_load(io.open(path, encoding="utf-8").read())
    if not isinstance(cfg, dict):
        print(f"{path} 顶层不是映射")
        return 2

    jobs = build_jobs(cfg, want_sec, top_only)
    real = [j for j in jobs if not j.get("skip")]
    print(f"{'=' * 76}")
    print(f"复核既有凭据 · {path}")
    print(f"{len(real)} 个 (段,站,key,模型) 组合"
          f"{'（只测顶层）' if top_only else ''}")
    print(f"{C_DIM}模型全部取自各条目自己的 models 字段 —— 不用种子模型。"
          f"这是 2026-08-30 踩过的坑：拿站点没声明的模型去打，"
          f"403/404 是测错不是站坏。{C_END}")
    print(f"{'=' * 76}")

    if not real:
        print("没有可测的组合")
        return 0

    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, r in enumerate(ex.map(lambda j: probe_one(j, timeout), jobs), 1):
            results.append(r)
            if i % 10 == 0:
                print(f"  {C_DIM}… {i}/{len(jobs)}{C_END}", flush=True)

    # 按段 → 站 汇总
    for sec in SECTIONS:
        rows = [r for r in results if r["sec"] == sec]
        if not rows:
            continue
        print(f"\n{'─' * 76}\n{sec}\n{'─' * 76}")
        by_host: dict[str, list[dict]] = collections.defaultdict(list)
        for r in rows:
            by_host[r["host"]].append(r)
        for host in sorted(by_host,
                           key=lambda h: -int(by_host[h][0]["entry"].get("priority") or 0)):
            rs = by_host[host]
            pri = int(rs[0]["entry"].get("priority") or 0)
            testable = [r for r in rs if r["ok"] is not None]
            good = [r for r in testable if r["ok"]]
            mark = C_OK if good else C_BAD
            share = f"{len(good)}/{len(testable)}" if testable else "未测"
            print(f"\n  {mark}{host:24}{C_END} priority {pri:5}  可用 {mark}{share}{C_END}")
            # 只列失败的与代表性成功的，避免刷屏
            shown = 0
            for r in sorted(rs, key=lambda x: (x["ok"] is True, x["model"])):
                if r["ok"] is None:
                    print(f"      {C_DIM}— {r['model'] or '(无模型)':26} {r['note']}{C_END}")
                    continue
                if r["ok"] and shown >= 2:
                    continue
                if r["ok"]:
                    shown += 1
                icon = f"{C_OK}✓{C_END}" if r["ok"] else f"{C_BAD}✗{C_END}"
                print(f"      {icon} {r['model']:26} {r['status']:4} "
                      f"{r['cat']:6} {r['ms']:6}ms {r['note'][:40]}")
            if len(good) > 2:
                print(f"      {C_DIM}…另有 {len(good) - 2} 个模型可用{C_END}")

    # 结论：每段真正能出活的 (站,模型) 组合
    print(f"\n{'=' * 76}\n结论\n{'=' * 76}")
    for sec in SECTIONS:
        rows = [r for r in results if r["sec"] == sec and r["ok"]]
        if not rows:
            others = [r for r in results if r["sec"] == sec]
            if others:
                print(f"\n  {C_BAD}{sec}{C_END}：本次复核**无可用组合**")
                cats = collections.Counter(r["cat"] for r in others if r["ok"] is False)
                print(f"      失败定性分布：{dict(cats)}")
                print(f"      {C_DIM}注意：某些段有协议限制 —— codex 段走 "
                      f"/v1/responses，多数中转站只实现 /v1/chat/completions，"
                      f"那种 500 not implemented 是站方不支持，不是故障{C_END}")
            continue
        pair = collections.defaultdict(set)
        for r in rows:
            pair[r["host"]].add(r["model"])
        print(f"\n  {C_OK}{sec}{C_END}：{len(pair)} 个站有可用组合")
        for h in sorted(pair, key=lambda x: -int(
                next(r for r in rows if r["host"] == x)["entry"].get("priority") or 0)):
            pri = int(next(r for r in rows if r["host"] == h)["entry"].get("priority") or 0)
            ms = sorted(pair[h])
            print(f"      priority {pri:5}  {h:24} {len(ms)} 个模型："
                  f"{', '.join(ms[:4])}{'…' if len(ms) > 4 else ''}")

    print(f"\n{'─' * 76}")
    print("「可用」只对 (key, 站, 段, 模型) 四元组成立 —— 同一个站在不同段")
    print("结论可以完全相反（foxtrot 在 claude 段 200、在 codex 段 500）。")
    print(f"{'─' * 76}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
