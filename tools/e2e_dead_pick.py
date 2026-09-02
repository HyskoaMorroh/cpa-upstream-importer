"""判死的段被勾选后，写进 config.yaml 的参数必须全部有定义。

为什么单独一个脚本：这条要求横跨探测、方案、写回三层，单元测试各测一段，
证明不了「落到文件里的那一行是完整的」。这里走真实链路，最后读回 YAML
逐字段检查 —— priority 是不是整数、headers 是不是空、模型名有没有编。

现场形态：站方 /v1/models 报得出模型，但拿这把 Key 发请求被 403 门禁挡掉。
CPAMP 面板能列出模型，本工具判死 —— 两者都没错，说的是不同的事。

    python3 tools/e2e_dead_pick.py
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import cpa_probe as cp
from cpa_probe.pipeline import Prober, model_fits_section
from cpa_probe.writeback import apply_diffs, build_diffs, validate


class Gate(BaseHTTPRequestHandler):
    """目录给得出模型，POST 一律 403 —— 门禁站的典型形态。"""

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if "/models" in urllib.parse.urlparse(self.path).path:
            self._send(200, {"data": [{"id": "claude-opus-5"},
                                      {"id": "claude-sonnet-5"},
                                      {"id": "gemini-2.5-pro"}]})
            return
        self._send(404, {"error": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        self._send(403, {"error": {"message": "only allows CC clients",
                                   "type": "permission_error"}})


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Gate)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    cfg_text = """host: "127.0.0.1"
port: 8317

# 人工注释：这一行必须活着走到最后
claude-api-key: []
gemini-api-key: []
"""
    tmpdir = tempfile.mkdtemp(prefix="e2e-deadpick-")
    cfg_path = os.path.join(tmpdir, "config.yaml")
    with io.open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(cfg_text)
    raw = io.open(cfg_path, encoding="utf-8").read()
    cfg = yaml.safe_load(raw)

    row = cp.parse_lines(f"{base},sk-ant-gatekeeper").valid[0]
    prober = Prober(gap=0.0, probe_context=False, swap_samples=0, workers=4)
    res = prober.probe(row)

    dead = [s for s, v in res.sections.items() if not v.usable]
    print(f"① 探测: {len(dead)}/{len(res.sections)} 段判死 "
          f"({', '.join(sorted(dead))})")
    assert dead, "门禁站应至少有一段判死，否则这个脚本测不到东西"

    cat_hits = {s: len(v.catalog or []) for s, v in res.sections.items()
                if not v.usable and (v.catalog or [])}
    print(f"② 目录: {cat_hits} —— 判死但站方报得出模型")
    assert cat_hits, "目录没读到模型，判死段就没有候选可选，覆盖不到本用例"

    plan = cp.build_plan(row, res, cfg, bands={},
                         seen=cp.existing_fingerprints(cfg), probation=True)
    picked = {s: sp for s, sp in plan.sections.items()
              if s in dead and sp.writable}
    print(f"③ 方案: {len(picked)} 个判死段可勾选")
    assert picked, "判死段全都进不了方案 —— 这正是要修的缺陷"

    # 逐字段检查：写进 config.yaml 的参数不许留「待定」
    for sec, sp in sorted(picked.items()):
        assert isinstance(sp.priority, int), \
            f"{sec} priority 不是整数: {sp.priority!r}"
        assert sp.priority_reason, f"{sec} priority 没有理由"
        assert sp.headers, f"{sec} headers 为空 —— 门禁站少了门票必然废掉"
        assert sp.models, f"{sec} 没有模型名"
        # 四种来源都是**确定值**，本用例查的是「不许待定」，不是「必须实测过」。
        #
        # 2026-09-02：这条原来排除 seed，而段族过滤（09b77cf）之后 codex 段
        # 必然走到 seed —— 假门禁站的目录只报 claude/gemini 模型，段族闸把它们
        # 从 codex 段全部滤掉是**正确的**（拿 claude 模型名打 /responses 是 CPA
        # 永不会发的形态）。断言比行为旧了一轮。
        assert sp.model_source in ("probed", "catalog", "manual", "seed"), \
            f"{sec} 模型来源不明: {sp.model_source!r}"
        # 但 seed 必须**被标出来**：可信度最低的那一档，界面上不能与实测同形。
        # 2026-09-02 起 seed 不再是「写死的猜测」而是「当前市面最新清单」
        # （三层：CPA 权威名录 / 本地 config.yaml / 内置兜底），措辞跟着改了，
        # 但「没有实测依据」这句必须还在 —— 那是这条警告存在的唯一理由。
        if sp.model_source == "seed":
            assert any("没有实测依据" in w or "没有任何实测依据" in w
                       for w in sp.warnings), \
                f"{sec} 用了市面最新清单却没说「没有实测依据」—— " \
                "猜的和实测的在界面上一个样"
        # 段族一致性：落进方案的模型必须与本段协议匹配，不论来源是哪一种
        for m in sp.models:
            assert model_fits_section(sec, m), \
                f"{sec} 收下了跨族模型 {m} —— CPA 永远不会这样发"
        assert sp.prefix is not None, f"{sec} prefix 未定"
        print(f"   {sec}: priority={sp.priority} "
              f"headers={len(sp.headers)}项 models={len(sp.models)}个 "
              f"来源={sp.model_source}")

    diffs = build_diffs(raw, [plan])
    out = apply_diffs(raw, diffs)
    ok, msg = validate(out)
    assert ok, f"写回结果 YAML 非法: {msg}"
    assert "人工注释：这一行必须活着走到最后" in out, "人工注释丢了"
    print(f"④ 写回: {msg}, {len(diffs)} 条 diff")

    # 读回文件逐条查 —— 前面查的是方案对象，这里查真正落进 YAML 的值
    back = yaml.safe_load(out)
    checked = 0
    per_sec: dict[str, int] = {}
    # 四段全查 —— 只查两段就成了「循环范围决定结论」，落地数看着少还以为丢了条目
    for sec in ("claude-api-key", "codex-api-key",
                "gemini-api-key", "openai-compatibility"):
        for e in (back.get(sec) or []):
            if not isinstance(e, dict):
                continue
            assert "priority" in e, f"{sec} 落地条目缺 priority"
            assert isinstance(e["priority"], int), \
                f"{sec} 落地 priority 不是整数: {e['priority']!r}"
            assert e.get("models"), f"{sec} 落地条目缺 models"
            assert e.get("headers"), f"{sec} 落地条目缺 headers"
            # compat 的 Key 挂在 api-key-entries 下，其余三段是条目级 api-key
            if sec == "openai-compatibility":
                assert e.get("api-key-entries"), f"{sec} 落地条目缺 api-key-entries"
            else:
                assert e.get("api-key"), f"{sec} 落地条目缺 api-key"
            per_sec[sec] = per_sec.get(sec, 0) + 1
            checked += 1
    assert len(per_sec) == 4, \
        f"四段都勾选了却只有 {len(per_sec)} 段落地: {sorted(per_sec)}"
    print(f"⑤ 读回: {checked} 个落地条目，priority/models/headers/api-key 全部有值")
    print("   " + " · ".join(f"{k}={v}" for k, v in sorted(per_sec.items())))
    assert checked, "写回后一个条目都没有 —— 链路没走通"

    srv.shutdown()
    print(f"\n判死段勾选验证通过 · 假门禁站 {base} · 临时目录 {tmpdir}")


if __name__ == "__main__":
    main()
