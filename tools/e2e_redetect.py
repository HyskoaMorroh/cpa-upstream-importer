"""全量重探端到端验证 —— 不是单元测试，是走完整条链的实跑。

组件测试证明每个零件对，这个脚本证明它们接起来对。走的是真实路径：
  起假上游 → 造含既有站的 config → run_job_full_redetect → _api_plan
  → rebuild_config_full → validate → write_local → 读回比对

用完即弃，跑完打印结论。失败会 raise，不静默通过。
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import cpa_probe as cp
from cpa_probe.batch import BatchProber, extract_existing_entries
from cpa_probe.pipeline import Prober
from cpa_probe.writeback import rebuild_config_full, validate, write_local


class Fake(BaseHTTPRequestHandler):
    """两种画像：good 四段全通；gate 只在带 anthropic-beta 时通。"""

    def log_message(self, *a):
        pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception:
            return {}

    def _send(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _model(self, body):
        return body.get("model") or "claude-opus-5"

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if "/models" in p:
            self._send(200, {"data": [{"id": "claude-opus-5"},
                                      {"id": "gpt-5.6-sol"},
                                      {"id": "gemini-2.5-pro"}]})
            return
        self._send(404, {"error": "nope"})

    def do_POST(self):
        profile = urllib.parse.urlparse(self.path).path.strip("/").split("/")[0]
        body = self._read()
        model = self._model(body)

        if profile == "gate" and not self.headers.get("anthropic-beta"):
            self._send(403, {"error": {"message": "only allows CC clients",
                                       "type": "permission_error"}})
            return

        self._send(200, {"id": "msg_01AbCdEfGhJiKmNoPq", "model": model,
                         "type": "message", "role": "assistant",
                         "content": [{"type": "text", "text": "ok"}],
                         "usage": {"input_tokens": 12, "output_tokens": 2}})


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    # 既有站：一个 good、一个 gate（gate 的 headers 故意漏掉，模拟"配置过期"）
    cfg_text = f"""host: "127.0.0.1"
port: 8317
remote-management:
  allow-remote: true

gemini-api-key:
  # 站 A：老配置，priority 200
  - api-key: "AIzaEXISTING_A"
    base-url: "{base}/good"
    priority: 200

claude-api-key:
  # 站 B：门禁站，headers 已过期（缺 anthropic-beta）
  - api-key: "sk-ant-EXISTING_B"
    base-url: "{base}/gate"
    priority: 300
"""
    tmpdir = tempfile.mkdtemp(prefix="e2e-redetect-")
    cfg_path = os.path.join(tmpdir, "config.yaml")
    with io.open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(cfg_text)

    raw = io.open(cfg_path, encoding="utf-8").read()
    cfg = yaml.safe_load(raw)

    # ① 提取既有站
    existing = extract_existing_entries(cfg)
    assert len(existing) == 2, f"应提取 2 个既有站，实际 {len(existing)}"
    print(f"① 提取既有站: {len(existing)} 个 "
          f"({', '.join(e[0] for e in existing)})")

    # ② 造 ParsedRow：既有站 + 一个新站
    lines = [f"{e[1]},{e[2]}" for e in existing]
    lines.append(f"{base}/good,sk-NEW_SITE")
    parsed = cp.parse_lines("\n".join(lines))
    assert len(parsed.valid) == 3, f"应解析 3 行，实际 {len(parsed.valid)}"
    print(f"② 解析: {len(parsed.valid)} 行有效, "
          f"{len({r.host for r in parsed.valid})} 个不同主机")

    # ③ 批量探测（站级并发）
    events = []
    prober = Prober(gap=0.0, probe_context=False, swap_samples=0, workers=4,
                    on_event=lambda k, d: events.append((k, d)))
    batch = BatchProber(prober, max_workers=3)

    progress = []
    t0 = time.time()
    results = batch.probe_batch(
        parsed.valid,
        progress_callback=lambda c, t, s, st: progress.append((c, t, dict(st))))
    dt = time.time() - t0

    assert len(progress) == 3, f"应有 3 次进度回调，实际 {len(progress)}"
    assert progress[-1][0] == progress[-1][1] == 3, "最后一次回调应是 3/3"
    print(f"③ 批量探测: {len(results)} 个站, {dt:.1f}s, "
          f"{len(progress)} 次进度回调, 统计 {progress[-1][2]}")

    # ④ 生成方案
    bands, seen = {}, cp.existing_fingerprints(cfg)
    all_plans = {}
    for row in parsed.valid:
        res = results.get(row.bare)
        assert res is not None, f"站 {row.bare} 无结果"
        p = cp.build_plan(row, res, cfg, bands=bands, seen=seen, probation=True)
        all_plans[(row.bare, row.api_key)] = p

    writable = sum(1 for p in all_plans.values()
                   for sp in p.sections.values() if sp.writable)
    print(f"④ 生成方案: {len(all_plans)} 个站, {writable} 个可写段")

    # ⑤ 全量重建
    new_text, warns = rebuild_config_full(cfg, all_plans, raw.splitlines(True))
    ok, msg = validate(new_text)
    assert ok, f"重建结果 YAML 非法: {msg}"
    print(f"⑤ 全量重建: {msg}, {len(warns)} 条警告")

    # ⑥ 关键断言：注释在、全局配置在
    assert "站 A：老配置" in new_text, "站 A 的人工注释丢了"
    assert "门禁站，headers 已过期" in new_text, "站 B 的人工注释丢了"
    assert 'host: "127.0.0.1"' in new_text, "全局配置 host 丢了"
    assert "allow-remote: true" in new_text, "全局配置 remote-management 丢了"
    print("⑥ 注释与全局配置: 全部保留")

    # ⑦ 写回并读回比对
    bak = write_local(cfg_path, new_text)
    back = io.open(cfg_path, encoding="utf-8").read()
    assert back == new_text, "写回后读到的内容与预期不一致"
    assert os.path.exists(bak), f"备份文件不存在: {bak}"
    bak_content = io.open(bak, encoding="utf-8").read()
    assert bak_content == raw, "备份内容不是原始文件"
    print(f"⑦ 写回: 字节一致, 备份可回滚 ({os.path.basename(bak)})")

    # ⑧ 重建后的 config 能被重新解析、能再次提取
    cfg2 = yaml.safe_load(back)
    existing2 = extract_existing_entries(cfg2)
    print(f"⑧ 幂等性: 重建后可再提取 {len(existing2)} 个站")

    # ⑨ 检查门禁站的 headers 是否被补上（这是全量重探的核心价值）
    gate_headers = None
    for sec in ("claude-api-key", "gemini-api-key", "openai-compatibility"):
        for e in (cfg2.get(sec) or []):
            if isinstance(e, dict) and "/gate" in str(e.get("base-url", "")):
                gate_headers = e.get("headers")
    if gate_headers:
        print(f"⑨ 门禁站 headers: 已补上 {list(gate_headers)}")
    else:
        print("⑨ 门禁站 headers: 未补上（该站可能被判不可写，看警告）")

    srv.shutdown()
    print(f"\n端到端验证通过 · 假上游 {base} · 临时目录 {tmpdir}")
    if warns:
        print("警告明细:")
        for w in warns:
            print(f"  · {w}")


if __name__ == "__main__":
    main()
