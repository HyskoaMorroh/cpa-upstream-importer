#!/usr/bin/env python3
"""写回「生效链」的回归测试。零外网请求（用本地 http.server 假冒 CPA）。

    python3 tests/test_reload.py [config.yaml 路径]

为什么单独一个套件
------------------
「磁盘改了但 CPA 不生效」这个故障，前 434 项测试**一项都抓不到** ——
它们只验证生成的 YAML 对不对，不验证「CPA 容器能不能看到」。
故障出在两个测试盲区：

  1. write_local 曾用 tmp + os.replace，**换 inode**。config.yaml 是
     单文件 bind mount，容器启动时 inode 就定死了，换了之后容器永远
     读旧文件。实测：宿主 212 条目，CPA 面板 206，重启才对上。
  2. PUT 返回 200 不代表 CPA 用上了新配置。没有读回校验的话，
     inode 分叉这类问题在服务端看来「一切正常」。

所以这里测三件事：
  ① write_local 前后 inode 必须相同（且内容真的换了）
  ② 读回校验能识别「CPA 读到的是旧内容」
  ③ PUT 的各类失败码分类正确（422/400 是安全失败，401 是密码传错）
"""

from __future__ import annotations

import http.server
import io
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)   # 让 fixture_cfg 可导入

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

import fixture_cfg                                       # noqa: E402
from cpa_probe.writeback import (                              # noqa: E402
    _readback_check,
    push_to_cpa,
    reload_cpa,
    write_local,
)

_pass = 0
_fail: list[str] = []


def section(title: str) -> None:
    print(f"── {title} " + "─" * max(0, 58 - len(title)))


def eq(label: str, got, want) -> None:
    global _pass
    if got == want:
        _pass += 1
        print(f"  ok  {label}")
    else:
        _fail.append(f"{label}\n      期望 {want!r}\n      实得 {got!r}")
        print(f"  FAIL {label}")


def truthy(label: str, got, hint: str = "") -> None:
    global _pass
    if got:
        _pass += 1
        print(f"  ok  {label}")
    else:
        _fail.append(f"{label}\n      实得 {got!r} {hint}")
        print(f"  FAIL {label}")


def contains(label: str, hay: str, needle: str) -> None:
    global _pass
    if needle in hay:
        _pass += 1
        print(f"  ok  {label}")
    else:
        _fail.append(f"{label}\n      «{needle}» 不在：{hay!r}")
        print(f"  FAIL {label}")


# --------------------------------------------------------------------------
# 假 CPA。只实现 GET/PUT /v0/management/config.yaml。
# --------------------------------------------------------------------------


class FakeCPA(http.server.BaseHTTPRequestHandler):
    # 由测试逐项设置
    password = "correct-horse"
    stored = ""            # CPA「容器里看到的」内容
    put_status = 200       # 想让 PUT 返回什么
    freeze_stored = False  # True 表示 PUT 落盘但 GET 仍返回旧内容（模拟 inode 分叉）
    put_bodies: list[str] = []
    err_body = ""            # 非 200 时返回的正文；空则用默认

    def _auth_ok(self) -> bool:
        want = f"Bearer {type(self).password}"
        return self.headers.get("Authorization") == want

    def log_message(self, *a):    # 静音
        pass

    def do_GET(self):
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            return
        data = type(self).stored.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/yaml")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8")
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return
        type(self).put_bodies.append(body)
        st = type(self).put_status
        if st != 200:
            self.send_response(st)
            self.end_headers()
            body_err = (type(self).err_body
                        or '{"error":"invalid_config","message":"boom"}')
            self.wfile.write(body_err.encode("utf-8"))
            return
        if not type(self).freeze_stored:
            type(self).stored = body
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


def start_fake() -> tuple[str, http.server.ThreadingHTTPServer]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeCPA)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv
# 最小样本已移到 tests/fixture_cfg.py —— 各套件共用一份，形状只需维护一处。



def main() -> int:
    # 样本取自 tests/fixture_cfg.py。绝不回落到 ROOT/config.yaml ——
    # 那既可能是生产配置，也让断言随一个会变的外部文件漂移。
    cfg_path, _synth, _fx_tmp = fixture_cfg.resolve(sys.argv, label="写回与重载")
    tmpdir = tempfile.mkdtemp(prefix="reload-test-")
    original = io.open(cfg_path, encoding="utf-8").read()

    try:
        # ── ① inode 恒定 ────────────────────────────────────────────────
        section("write_local 必须保持 inode（单文件 bind mount 的硬要求）")
        target = os.path.join(tmpdir, "config.yaml")
        shutil.copy2(cfg_path, target)
        ino_before = os.stat(target).st_ino
        dev_before = os.stat(target).st_dev

        changed = original + "\n# reload-test marker\n"
        bak = write_local(target, changed, backup_dir=os.path.join(tmpdir, "b"))

        st_after = os.stat(target)
        eq("inode 未变", st_after.st_ino, ino_before)
        eq("st_dev 未变", st_after.st_dev, dev_before)
        eq("内容确实换成了新的",
           io.open(target, encoding="utf-8").read(), changed)
        truthy("备份文件存在", os.path.exists(bak))
        eq("备份内容是写入前的原文",
           io.open(bak, encoding="utf-8").read(), original)
        truthy("没有留下 .tmp 残留",
               not os.path.exists(target + ".tmp"),
               "tmp+rename 会换 inode，绝不能走那条路")

        # 连写两次，inode 仍不能变 —— 循环使用时更容易踩到
        write_local(target, changed + "\n# second\n",
                    backup_dir=os.path.join(tmpdir, "b"))
        eq("连续两次写入后 inode 依旧未变",
           os.stat(target).st_ino, ino_before)

        # ── ② 读回校验 ──────────────────────────────────────────────────
        section("读回校验：能识别「CPA 看到的是旧内容」")
        base, srv = start_fake()
        try:
            FakeCPA.password = "correct-horse"
            FakeCPA.put_status = 200
            FakeCPA.freeze_stored = False
            FakeCPA.stored = original
            FakeCPA.put_bodies = []

            # 一致：内容相同（读回只比行数与四段条目数，这里逐字节相同必过）
            ok, msg = _readback_check(base, "correct-horse", original)
            truthy("内容一致时读回通过", ok, msg)
            contains("说明里带四段条目总数", msg, "条目")

            # 不一致：CPA 卡在旧内容，而我们期望的是加了条目的新内容
            #（模拟 inode 分叉：宿主机已改，容器仍读旧文件）
            more = _append_claude_entry(original)
            ok, msg = _readback_check(base, "correct-horse", more)
            eq("条目数不符时读回失败", ok, False)
            contains("失败说明点明 inode", msg, "inode")
            contains("失败说明给出可执行处置", msg, "docker restart")

            ok, msg = _readback_check(base, "wrong-pass", original)
            eq("密码错时读回失败", ok, False)
            contains("说明点出 GET 失败", msg, "GET 失败")

            # ── ③ push_to_cpa 状态码分类 ───────────────────────────────
            section("PUT 状态码分类")
            FakeCPA.stored = original
            ok, msg = push_to_cpa(base, "correct-horse", more)
            truthy("正常路径成功（PUT + 读回一致）", ok, msg)
            contains("成功说明提到读回", msg, "读回")
            eq("CPA 侧确实收到了新内容", FakeCPA.stored, more)

            FakeCPA.put_status = 422
            ok, msg = push_to_cpa(base, "correct-horse", more)
            eq("422 判为失败", ok, False)
            contains("422 说明为安全失败", msg, "未落盘")

            FakeCPA.put_status = 400
            ok, msg = push_to_cpa(base, "correct-horse", more)
            eq("400 判为失败", ok, False)
            contains("400 也说明未落盘", msg, "未落盘")

            FakeCPA.put_status = 500
            ok, msg = push_to_cpa(base, "correct-horse", more)
            eq("500 判为失败", ok, False)
            contains("500 要求人工核对完整性", msg, "完整性")

            # 403 + Cloudflare 正文 —— 实测踩过：PUT 走了公网域名，被 CF
            # 拦成 error code 1010。这时 config.yaml **一个字节都没动**，
            # 而旧代码把它归进「核对文件完整性」那条兜底，完全引错方向。
            FakeCPA.put_status = 403
            FakeCPA.err_body = ('<!DOCTYPE html><title>Attention Required! '
                                '| Cloudflare</title>error code: 1010')
            ok, msg = push_to_cpa(base, "correct-horse", more)
            eq("403 + CF 正文判为失败", ok, False)
            contains("点明是 Cloudflare 拦下", msg, "Cloudflare")
            contains("明确说请求没到 CPA", msg, "根本没到 CPA")
            contains("给出容器内直连的修法", msg, "cli-proxy-api:8317")
            truthy("不误导去核对文件完整性", "完整性" not in msg,
                   "文件没被碰过，让人去核对完整性是错的方向")

            # 403 但不是 CF —— 另一种处置，不能混为一谈
            FakeCPA.err_body = '{"error":"remote management disabled"}'
            ok, msg = push_to_cpa(base, "correct-horse", more)
            eq("403 非 CF 也判失败", ok, False)
            contains("提到 allow-remote", msg, "allow-remote")
            truthy("非 CF 的 403 不谎称是 CF", "Cloudflare" not in msg)
            FakeCPA.err_body = ""

            FakeCPA.put_status = 200
            ok, msg = push_to_cpa(base, "wrong-pass", more)
            eq("密码错判为失败", ok, False)
            contains("401 说明指向 bcrypt 原始密码", msg, "bcrypt")

            # PUT 成功但 CPA 读回仍是旧的 —— 最隐蔽的那种，必须抓住
            FakeCPA.stored = original
            FakeCPA.freeze_stored = True
            ok, msg = push_to_cpa(base, "correct-horse", more)
            eq("PUT 200 但读回仍旧内容 → 判为失败", ok, False)
            contains("说明点明读回校验失败", msg, "读回校验失败")
            FakeCPA.freeze_stored = False

            # ── ④ reload_cpa 的前置拦截 ────────────────────────────────
            section("reload_cpa 前置拦截")
            # 比前后差值，不写死总数 —— 假 CPA 对 401 是在鉴权前 return，
            # 不记入 put_bodies，写死数字会算错。这里要验的是
            # 「被拦下的请求一次都没发出去」，差值为 0 才是那个意思。
            sent_before = len(FakeCPA.put_bodies)

            ok, msg = reload_cpa(base, "", more)
            eq("空密码直接失败", ok, False)
            contains("说明缺少管理密码", msg, "管理密码")

            for h in ("$2a$10$abc", "$2b$10$abc", "$2y$10$abc"):
                ok, msg = reload_cpa(base, h, more)
                eq(f"bcrypt 哈希（{h[:4]}）被拦下", ok, False)
                contains(f"{h[:4]} 说明指出要原始密码", msg, "原始密码")

            eq("被拦下的 4 次请求一个字节都没发出去",
               len(FakeCPA.put_bodies) - sent_before, 0)

            FakeCPA.stored = original
            ok, msg = reload_cpa(base, "correct-horse", more)
            truthy("正确密码可通过", ok, msg)
        finally:
            srv.shutdown()
            srv.server_close()

        section("原文件")
        eq("config.yaml 逐字节未变",
           io.open(cfg_path, encoding="utf-8").read(), original)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        if _fx_tmp:
            shutil.rmtree(_fx_tmp, ignore_errors=True)

    print("\n" + "=" * 62)
    if _fail:
        print(f"失败 {len(_fail)} 项 / 通过 {_pass} 项\n")
        for f in _fail:
            print(f"  ✗ {f}\n")
        return 1
    print(f"全部通过 · {_pass} 项")
    return 0


def _append_claude_entry(text: str) -> str:
    """在 claude-api-key 段末尾插一条最小条目，用于制造「条目数不同」。

    直接文本插入，不经 build_diffs —— 这个套件要测的是读回校验本身，
    不该依赖计划生成那条链。
    """
    lines = text.splitlines()
    from cpa_probe.writeback import _detect_indent, _section_span
    span = _section_span(lines, "claude-api-key")
    assert span is not None, "测试用的 config.yaml 里没有 claude-api-key 段"
    st, en = span
    dash, field = _detect_indent(lines, st, en)
    new = [f'{dash}- api-key: "reload-test-key"',
           f'{field}base-url: "https://reload-test.example.com"']
    return "\n".join(lines[:en] + new + lines[en:]) + "\n"


if __name__ == "__main__":
    sys.exit(main())
