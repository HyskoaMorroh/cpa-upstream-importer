#!/usr/bin/env python3
"""服务契约回归测试。零外网请求 —— 只打本机自己启的服务。

    python3 tests/test_server.py [config.yaml 路径]

覆盖：鉴权闸门、静态资源与路径穿越、脱敏、写回三道闸门、原文件不被触碰。
探测类路由（/api/probe）不在此覆盖 —— 它会真打上游、花钱、触发限频。
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)   # 让 fixture_cfg 可导入
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fixture_cfg                                        # noqa: E402

_fail: list[str] = []
_pass = 0


def eq(name: str, got, want) -> None:
    global _pass
    if got != want:
        _fail.append(f"{name}\n      got  = {got!r}\n      want = {want!r}")
    else:
        _pass += 1
        print(f"  ok  {name}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client:
    def __init__(self, port: int, token: str):
        self.port = port
        self.token = token

    def __call__(self, path: str, body=None, *, token: str | None = "__default__",
                 method: str | None = None) -> tuple[int, str]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        rq = urllib.request.Request(url, data=data,
                                    method=method or ("POST" if data else "GET"))
        tok = self.token if token == "__default__" else token
        if tok:
            rq.add_header("Authorization", "Bearer " + tok)
        if data:
            rq.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(rq, timeout=20) as rs:
                return rs.status, rs.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # 连接层失败
            return 0, repr(e)


def main() -> int:
    # 不传路径就用自带的最小样本 —— 绝不回落到 ../config.yaml（生产配置）。
    # 原来那样做有两个后果，都实测踩到了（2026-08-31）：
    #   · 刚 clone 的仓库与 CI runner 上直接 return 2，而 CI 明确不带路径调用
    #   · 断言挂在一个会变的生产文件上：那份 config.yaml 一改，
    #     「gemini 五档」这类硬编码基线就失效，报红却指不出真缺陷
    # 另外这个套件会起真服务、可能在 config 目录旁留下 .bak —— 更不该指向生产文件。
    cfg_path, _synthetic, _tmp = fixture_cfg.resolve(sys.argv, label="HTTP 契约")

    before = io.open(cfg_path, encoding="utf-8").read()
    # 目录里可能早就有历史备份（VPS 上就有 08-28/08-29 那几个）。
    # 断言要看的是「本次测试没新增」，不是「目录里一个都没有」。
    cfg_dir = os.path.dirname(cfg_path)
    baks_before = {f for f in os.listdir(cfg_dir) if ".bak-" in f}
    port, token = free_port(), "regress-token-" + os.urandom(4).hex()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server.py"),
         "--config", cfg_path, "--port", str(port), "--token", token],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env)

    req = Client(port, token)
    try:
        up = False
        for _ in range(80):
            time.sleep(0.25)
            if proc.poll() is not None:
                print("服务启动即退出：\n" + (proc.stdout.read() if proc.stdout else ""))
                return 1
            st, _b = req("/api/context")
            if st:
                up = True
                break
        eq("服务启动", up, True)
        if not up:
            return 1

        section("鉴权闸门")
        eq("无 token 401", req("/api/context", token=None)[0], 401)
        eq("错 token 401", req("/api/context", token="wrong")[0], 401)
        st, body = req("/api/context")
        eq("正确 token 200", st, 200)
        ctx = json.loads(body)
        eq("四段齐全", sorted(ctx["sections"]), sorted(ctx["section_order"]))
        # 档位谱要与**当前这份** config.yaml 对得上，而不是与某个写死的数字。
        # 原来写的是「claude 顶档 1000」「gemini 五档」—— 那是自带样本的形状，
        # 传真实 config.yaml 进来就必然失败（2026-08-31 实测：gemini 实为六档）。
        # 断言挂在会变的外部文件上，是这一整轮修的同一类缺陷：报红却指不出
        # 任何真问题。改成从被测的那份文件现算期望值，两种输入都成立。
        import yaml as _yaml
        _cfg = _yaml.safe_load(io.open(cfg_path, encoding="utf-8").read())
        for _sec in ("gemini-api-key", "codex-api-key", "claude-api-key",
                     "openai-compatibility"):
            _pris = {e.get("priority") for e in (_cfg.get(_sec) or [])
                     if isinstance(e, dict) and isinstance(e.get("priority"), int)}
            eq(f"{_sec} 档位数与文件一致",
               len(ctx["sections"][_sec]["tiers"]), len(_pris))
            if _pris:
                eq(f"{_sec} 顶档与文件一致",
                   ctx["sections"][_sec]["top"], max(_pris))
        eq("context 不含明文 key", "sk-" in body, False)

        section("失败封锁 · 直接调类方法（不走 HTTP，免污染后续用例）")
        # 这段锁住一个曾经形同虚设的 bug：_locked_out 在「未封锁」分支里
        # 顺手把 count 也清零，而 _authed 每次都调它 —— 于是失败计数永远
        # 回到 0，5 次封锁永不触发。当时全部测试仍是绿的。
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            "srv_under_test", os.path.join(ROOT, "server.py"))
        _m = importlib.util.module_from_spec(_spec)
        sys.modules["srv_under_test"] = _m
        _spec.loader.exec_module(_m)
        H = _m.Handler

        eq("阈值与 CPA 一致（5 次）", H.MAX_FAILURES, 5)
        eq("封锁时长与 CPA 一致（30 分钟）", H.BAN_SECONDS, 30 * 60)

        ip = "203.0.113.77"
        counts = []
        for _ in range(4):
            H._note_failure(ip)
            H._locked_out(ip)                    # 关键：模拟 _authed 每次都查
            with H._fail_lock:
                counts.append((H._failures.get(ip) or {}).get("count"))
        eq("失败计数真的在累加", counts, [1, 2, 3, 4])
        eq("第 4 次后尚未封锁", H._locked_out(ip), 0.0)

        H._note_failure(ip)                       # 第 5 次
        eq("第 5 次触发封锁", H._locked_out(ip) > 0, True)
        eq("封锁时长接近 30 分钟", 1700 < H._locked_out(ip) <= 1800, True)

        H._note_success(ip)
        with H._fail_lock:
            eq("成功登录清空该 IP 记录", ip in H._failures, False)

        other = "198.51.100.9"
        for _ in range(5):
            H._note_failure(other)
        eq("按 IP 隔离：该 IP 被封", H._locked_out(other) > 0, True)
        eq("按 IP 隔离：别的 IP 不受影响", H._locked_out("192.0.2.1"), 0.0)

        H.BAN_SECONDS = 1                         # 缩短以验证自动解封
        expire_ip = "192.0.2.55"
        for _ in range(5):
            H._note_failure(expire_ip)
        eq("缩短后仍会封锁", H._locked_out(expire_ip) > 0, True)
        time.sleep(1.2)
        eq("封锁到期自动解封", H._locked_out(expire_ip), 0.0)
        H.BAN_SECONDS = 30 * 60

        section("CPA 管理密码登录")
        # config.yaml 里的 secret-key 是 bcrypt 哈希（CPA 首次加载时自动转换，
        # config_load.go:104-113）。用户输原始密码，服务端做 bcrypt 比对。
        #
        # 这里的 H 是 importlib 另行加载的一份 Handler，它的 cfg_path 是空的
        # （真正带 --config 的那份在子进程里）。不显式设就会一路走
        # 「读不到 → 返回空 → 跳过」，把测试写成了永远不检查。
        H.cfg_path = cfg_path
        h = H._cpa_mgmt_hash()
        if h:
            eq("读到的是 bcrypt 形态", h.startswith(("$2a$", "$2b$", "$2y$")), True)
            eq("哈希本身不能当密码", H._check_cpa_password(h), False)
        else:
            print("     -- config.yaml 的 secret-key 非 bcrypt 形态，跳过")
        eq("空密码一律拒绝", H._check_cpa_password(""), False)
        try:
            import bcrypt  # noqa: F401
            has_bcrypt = True
        except ImportError:
            has_bcrypt = False

        # 两条分支都必须有断言，否则「装了 bcrypt 的机器」上这段等于没测。
        # 之前只写了 not has_bcrypt 那半边：本机（无 bcrypt）跑 333 项，
        # VPS（有 bcrypt）跑 332 项，而少掉的恰好是唯一验证「错密码被拒」的那条。
        if has_bcrypt:
            # 用已知密码现算一个哈希，验证 bcrypt 比对真的在工作
            salt = bcrypt.gensalt(rounds=4)          # 4 轮：测试要快
            real = bcrypt.hashpw(b"correct-horse", salt).decode()
            saved = H.cfg_path
            try:
                import tempfile
                fd, tmp = tempfile.mkstemp(suffix=".yaml")
                os.close(fd)
                io.open(tmp, "w", encoding="utf-8").write(
                    "remote-management:\n  secret-key: \"%s\"\n" % real)
                H.cfg_path = tmp
                eq("正确密码通过 bcrypt 比对",
                   H._check_cpa_password("correct-horse"), True)
                eq("错密码被拒", H._check_cpa_password("wrong-horse"), False)
                eq("哈希本身当密码用被拒", H._check_cpa_password(real), False)
                eq("空密码被拒", H._check_cpa_password(""), False)
            finally:
                H.cfg_path = saved
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        else:
            eq("未装 bcrypt 时该路径安全关闭",
               H._check_cpa_password("anything"), False)
            print("     ↑ 不退化成明文比较")
            print("     注：装了 bcrypt 的机器会多跑 4 项真实比对用例")

        section("静态资源")
        st, html = req("/", token=None)          # 首页允许免鉴权，token 由前端带
        eq("首页 200", st, 200)
        eq("首页是 HTML", html.lstrip().startswith("<!"), True)
        eq("app.js 200", req("/static/app.js", token=None)[0], 200)
        eq("路径穿越被挡", req("/static/../server.py", token=None)[0], 403)
        eq("越界路径不返回源码", "BaseHTTPRequestHandler"
           in req("/static/../server.py", token=None)[1], False)

        section("解析路由")
        st, body = req("/api/parse", {
            "text": "https://x.example.com,sk-test1234567890\n"
                    "https://y.example.org/v1,sk-abcd9876543210\n"
                    "badline-no-comma\n"})
        eq("parse 200", st, 200)
        pj = json.loads(body)
        eq("有效 2", len(pj["valid"]), 2)
        eq("无效 1", len(pj["invalid"]), 1)
        eq("codex 补 /v1", pj["valid"][0]["bases"]["codex-api-key"],
           "https://x.example.com/v1")
        eq("claude 不带 /v1", pj["valid"][0]["bases"]["claude-api-key"],
           "https://x.example.com")
        eq("compat 补 /v1", pj["valid"][1]["bases"]["openai-compatibility"],
           "https://y.example.org/v1")
        eq("key 已脱敏", pj["valid"][0]["key_masked"], "sk-tes...7890")
        eq("无 api_key 字段", "api_key" in pj["valid"][0], False)
        eq("响应无明文 key", "sk-test1234567890" in body, False)
        eq("空文本不报错", req("/api/parse", {"text": ""})[0], 200)

        section("写回三道闸门")
        eq("未知 job 的 plan → 404", req("/api/plan", {"job_id": "nope"})[0], 404)
        eq("未知方案 apply → 404",
           req("/api/apply", {"plan_id": "nope", "confirm": True})[0], 404)
        st, body = req("/api/apply", {"plan_id": "nope", "confirm": False})
        eq("未确认被拒", st in (400, 404), True)

        section("未知路由")
        eq("GET 未知 404", req("/api/nope")[0], 404)
        eq("POST 未知 404", req("/api/nope", {"a": 1})[0], 404)

        section("原文件")
        eq("config.yaml 逐字节未变",
           io.open(cfg_path, encoding="utf-8").read(), before)
        baks_after = {f for f in os.listdir(cfg_dir) if ".bak-" in f}
        eq("本次未新增 .bak", sorted(baks_after - baks_before), [])

        section("密文比较 · 非 ASCII 安全")
        # hmac.compare_digest(str, str) 在任一边含非 ASCII 字符时抛 TypeError：
        #   comparing strings with non-ASCII characters is not supported
        # CPA 的管理密码完全可能含中文。抛异常会变成 500，看起来像服务坏了，
        # 而不是「密码不对」。_same_secret 先 encode 成 bytes 再比，避开限制。
        same = _m._same_secret
        eq("相同 ASCII 判真", same("abc123", "abc123"), True)
        eq("不同 ASCII 判假", same("abc123", "abc124"), False)
        eq("不等长判假（不抛）", same("abc", "abcdef"), False)
        eq("相同中文密码判真", same("密码很长很安全", "密码很长很安全"), True)
        eq("不同中文密码判假", same("密码很长很安全", "密码很长很危险"), False)
        eq("中文对 ASCII 判假", same("密码", "mima"), False)
        eq("emoji 也不抛", same("pw🔑", "pw🔑"), True)
        eq("两边都空判假", same("", ""), False)
        eq("单边空判假", same("abc", ""), False)
        # 混合：一边纯 ASCII 一边非 ASCII —— 最容易抛的组合
        eq("ASCII 对中文不抛且判假", same("abcdef", "中文密码"), False)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 样本目录留到最后再删 —— 上面的断言要检查目录里有没有多出 .bak
        if _tmp:
            import shutil
            shutil.rmtree(_tmp, ignore_errors=True)

    print("\n" + "=" * 66)
    if _fail:
        print(f"失败 {len(_fail)} 项 / 通过 {_pass} 项\n")
        for f in _fail:
            print("  ✗ " + f)
        return 1
    print(f"全部通过 · {_pass} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
