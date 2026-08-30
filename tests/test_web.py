#!/usr/bin/env python3
"""前端静态契约检查。零浏览器、零外网。

    python3 tests/test_web.py

为什么需要这个套件
------------------
实测踩到（2026-08-30）：`app.js` 的探测完成分支里写了

    $('#p2h').textContent = '② 探测完成';

而 `index.html` 里**根本没有 `id="p2h"`** —— `$('#p2h')` 返回 `null`，
`.textContent =` 立即抛 `TypeError`。执行到那一行就断，于是
`S.results` 没赋值、`renderResults` 没调用、**第 3 步永不出现**。

用户看到的现象：进度条满、候选 5/5、日志完整，但标题永远停在
「② 探测中」，转圈不停。看起来像后端卡住了，其实后端早就跑完 ——
是前端在完成的那一刻自己崩了，而且没有任何可见提示。

前 510 项测试一项都碰不到这个：它们全是 Python 侧，不解析 HTML/JS 的
对应关系。而这类失配的代价极高（整条流程卡死且无提示），检查却极便宜。

所以这里做四件事：
  ① 每个 $('#id') 都必须有对应的 id 定义
  ② 后端会发出的事件类型，前端都要认（否则日志里静默丢行）
  ③ 后端响应里前端依赖的字段，后端必须真的会给
  ④ 长任务的关键防护：轮询失败必须重试，不能永久放弃
"""

from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

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


def main() -> int:
    web = os.path.join(ROOT, "web")
    js = io.open(os.path.join(web, "app.js"), encoding="utf-8").read()
    html = io.open(os.path.join(web, "index.html"), encoding="utf-8").read()
    srv = io.open(os.path.join(ROOT, "server.py"), encoding="utf-8").read()

    # ── ① DOM id 契约 ──────────────────────────────────────────────
    section("① 每个 $('#id') 都有对应定义")
    refs = sorted(set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", js)))
    ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
    ids |= set(re.findall(r'id="([A-Za-z0-9_-]+)"', js))   # JS 动态生成的
    missing = [r for r in refs if r not in ids]
    truthy(f"引用 {len(refs)} 个 id，全部有定义",
           not missing,
           f"缺失：{missing} —— $('#x') 返回 null，取 .textContent 必崩")

    # 反向：HTML 定义了但 JS 从不引用的，不算错（可能纯样式锚点），
    # 但 id 重复定义是错 —— querySelector 只取第一个，行为不可预期
    section("① HTML 内无重复 id")
    all_html_ids = re.findall(r'id="([A-Za-z0-9_-]+)"', html)
    dup = sorted({i for i in all_html_ids if all_html_ids.count(i) > 1})
    truthy("无重复 id", not dup, f"重复：{dup}")

    # ── ② 事件类型契约 ─────────────────────────────────────────────
    section("② 后端发出的事件，前端都要认")
    # 后端：self.on_event("xxx", ...) / job.emit("xxx", ...)
    emitted = set(re.findall(r'on_event\(\s*"([a-z-]+)"',
                             io.open(os.path.join(ROOT, "cpa_probe", "pipeline.py"),
                                     encoding="utf-8").read()))
    # 前端：e.kind === 'xxx'
    handled = set(re.findall(r"e\.kind === '([a-z-]+)'", js))
    # attempt 走的是兜底分支（不在 if 链里显式判），单独放行
    handled.add("attempt")
    unhandled = sorted(emitted - handled)
    truthy(f"后端 {len(emitted)} 种事件，前端全认",
           not unhandled,
           f"未处理：{unhandled} —— 这些事件会在日志里静默丢失")

    # ── ③ 响应字段契约 ─────────────────────────────────────────────
    section("③ 前端依赖的响应字段，后端真的会给")
    # 前端读 d.xxx（轮询响应）
    for f in ("state", "events", "event_cursor", "calls", "elapsed",
              "done_rows", "total_rows", "results", "error"):
        in_js = f"d.{f}" in js
        in_srv = f'"{f}"' in srv
        if in_js:
            truthy(f"d.{f} 后端有提供", in_srv,
                   "前端读了但后端 snapshot/结果里没有这个键")

    # 写回响应的关键字段 —— 生效链的可见性全靠它们
    section("③ 写回响应：重载状态必须可见")
    for f in ("reload_ok", "reload_msg", "written", "backup", "diffs"):
        truthy(f"d.{f} 前后端都有",
               f"d.{f}" in js and f'"{f}"' in srv,
               f"前端 {'有' if f'd.{f}' in js else '无'} / "
               f"后端 {'有' if f'\"{f}\"' in srv else '无'}")

    # ── ④ 长任务防护 ───────────────────────────────────────────────
    section("④ 长任务防护")
    # 轮询失败必须重试。判据：从 poll 的 catch 起、到 catch 块结束之间要有
    # poll() 调用。用括号配平找块尾，不用正则猜缩进 —— 缩进一变正则就失效，
    # 而这条断言恰恰是要防「以后有人把重试删掉」。
    ci = js.find("catch (e) {", js.find("function poll()"))
    catch_body = ""
    if ci >= 0:
        depth, j = 0, js.index("{", ci)
        for k in range(j, len(js)):
            if js[k] == "{":
                depth += 1
            elif js[k] == "}":
                depth -= 1
                if depth == 0:
                    catch_body = js[j:k]
                    break
    truthy("轮询 catch 分支里会重试（poll()）",
           "poll();" in catch_body,
           "一次断连就永久停在「探测中」—— 实测踩过，任务其实已跑完")
    truthy("连续失败有上限，不无限刷日志",
           "pollFails >= 20" in js or "pollFails>=20" in js)
    truthy("401 不重试（token 失效重试无意义）",
           "e.status === 401" in js)
    truthy("断连后有「接回任务」出路", "showResume" in js and "btnresume" in html,
           "探测跑 293 秒，重跑代价太大 —— 必须能接回")

    # done 分支里 renderResults 必须被保护
    done_blk = re.search(r"if \(d\.state === 'done'\) \{(.*?)\n      return;",
                         js, re.S)
    truthy("done 分支里 renderResults 有 try 保护",
           bool(done_blk) and "try {" in done_blk.group(1),
           "渲染抛异常会变成 unhandled rejection，第 3 步静默不出现")

    # 转圈必须能停 —— [hidden] 兜底样式不能少
    section("④ 转圈能停")
    truthy("有 [hidden]{display:none!important} 全局兜底",
           re.search(r"\[hidden\][^{]*\{[^}]*display\s*:\s*none\s*!important",
                     html) is not None,
           ".spin{display:inline-block} 会压过 UA 的 [hidden] —— 实测踩过")

    # ── ⑤ CPA 地址不能硬编码公网域名 ───────────────────────────────
    section("⑤ CPA 地址：不硬编码公网域名")
    # 实测踩过：#o_base 的 value 写死 https://cpa.example.com，于是管理端点的
    # PUT 走公网被 Cloudflare 拦成 403 error code 1010 —— 请求根本没到 CPA，
    # 而页面报的是「注意 PUT 落盘用 O_TRUNC…核对文件完整性」，把人引错方向。
    m = re.search(r'id="o_base"[^>]*', html)
    truthy("#o_base 存在", bool(m))
    if m:
        tag = m.group(0)
        truthy("#o_base 没有硬编码的 value",
               'value=' not in tag,
               f"实际：{tag} —— 填了 value 就会覆盖服务端的 CPA_UPSTREAM_URL")
        truthy("#o_base 有 placeholder 说明留空的含义",
               "placeholder=" in tag)
    # 整个前端不该在**代码**里出现写死的公网 CPA 域名。
    # 注释里提它是好的（记录踩坑历史），所以先剥掉注释再查。
    js_code = re.sub(r"//[^\n]*", "", js)
    js_code = re.sub(r"/\*.*?\*/", "", js_code, flags=re.S)
    html_code = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    truthy("前端代码里无写死的 cpa.example.com",
           "cpa.example.com" not in html_code
           and "cpa.example.com" not in js_code,
           "公网入口在 CF 后面，管理端点必须走容器内服务名")
    # 地址只在用户填了才传 —— 传空串也会让服务端优先用它（若判据写反）
    truthy("地址只在非空时才放进请求体",
           "if (baseIn)" in js or "baseIn &&" in js,
           "无条件传 base 会让服务端配置永远用不上")

    section("⑤ 403 的两种来源要分开说")
    wb = io.open(os.path.join(ROOT, "cpa_probe", "writeback.py"),
                 encoding="utf-8").read()
    truthy("识别 Cloudflare 的 1010", "error code: 1010" in wb)
    truthy("CF 情形明说请求没到 CPA", "根本没到 CPA" in wb)
    truthy("CF 情形给出容器内直连的修法", "cli-proxy-api:8317" in wb)

    section("⑤ 后端加的诊断字段，前端必须真的用上")
    # 实测踩到（2026-08-30）：加了 4 个诊断字段（unmatched_notes / dead_hosts /
    # unhealthy_hosts / verify_over_limit），前端**一个都没用**。
    # 这些字段的全部价值就是「让静默问题可见」—— 不显示等于没加。
    for f, why in (
            ("dead_hosts", "档位谱要标出哪些站是 weight:0（挡它零代价）"),
            ("unhealthy_hosts", "要标出哪些站注释里记着实测不可用"),
            ("unmatched_notes", "注释短名匹配不上时必须告警，否则静默漏判"),
            ("verify_over_limit", "超出验证上限的条目已写入但没验，必须说明")):
        truthy(f"前端用到 {f}（{why}）", f in js,
               "后端提供了但前端没消费 —— 诊断信息不可见等于不存在")

    section("⑤ 段级风险告警")
    # 层级隔离下「顶层单点」与「顶层全死」是两类结构性风险，
    # 只看档位数字看不出来。
    truthy("有单点告警", "单点" in js)
    truthy("有顶层全死告警", "全部实测不可用" in js)
    truthy("按健康度区分站的样式", "hdead" in js and "hdead" in html)
    truthy("告警样式能换行显示（覆盖 .tier .h 的 nowrap）",
           re.search(r"\.tier\.warn[^{]*\{[^}]*white-space\s*:\s*normal", html)
           is not None,
           "不覆盖 nowrap 的话长文案会被截断成一行看不全")

    # ── ⑥ 并行选项要真的传出去 ─────────────────────────────────────
    section("⑤ 端到端验证不能默认被跳过")
    # 这层验证是**唯一**能发现「直连 200、经 CPA 换模」的手段，可它原来挂在
    # 一个要用户去 config.yaml 里翻 api-keys 的输入框上 —— 于是默认永远跳过，
    # 用户只看到「不代表新上游真能出活」而不知道该怎么办。
    truthy("后端会自动取客户端 Key", "_cpa_client_key" in srv,
           "不自动取，这层验证就永远是摆设")
    truthy("自动取的是 api-keys", '"api-keys"' in srv or "'api-keys'" in srv)
    truthy("用户填的优先于自动取",
           'push.get("client_key")' in srv,
           "手工填了就该用手工的（比如想用另一个入口 Key 验）")
    truthy("上报 Key 来源，便于判断验的是什么", "verify_key_src" in srv)
    # 安全：客户端入口 Key 绝不能进响应体，那等于把 CPA 入口凭据递给浏览器
    truthy("响应里不含 client_key 的值",
           '"client_key": client_key' not in srv
           and 'result["client_key"]' not in srv,
           "这是 CPA 的入口凭据，只能在服务端用")
    truthy("前端 placeholder 说明留空会自动取",
           "自动取" in html,
           "不说明的话用户不知道留空是安全的默认")

    section("⑥ 并行选项贯通")
    truthy("前端有并行开关 #o_fast", 'id="o_fast"' in html)
    truthy("前端把 workers 传给后端", "workers:" in js)
    truthy("前端把 candidate_workers 传给后端", "candidate_workers:" in js)
    truthy("后端读 workers", '"workers"' in srv)
    truthy("后端读 candidate_workers", '"candidate_workers"' in srv)

    print("\n" + "=" * 62)
    if _fail:
        print(f"失败 {len(_fail)} 项 / 通过 {_pass} 项\n")
        for f in _fail:
            print(f"  ✗ {f}\n")
        return 1
    print(f"全部通过 · {_pass} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
