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
    # 两个来源都要扫（2026-09-01 补）：
    #   pipeline.py  self.on_event("xxx", ...)   —— 探测流程
    #   server.py    job.emit("xxx", ...)        —— 任务编排（全量重探那条路）
    # 原来只扫前者，于是 run_job_full_redetect 发的 info/progress/error 三种
    # 从未被这项检查覆盖，前端漏了分支也不报错 —— 它们落到兜底分支，
    # 在日志里显示成 `[info] {"msg":"…"}` 的原始 JSON。而全量重探恰恰是最
    # 需要可读进度的场景。
    pipe_src = io.open(os.path.join(ROOT, "cpa_probe", "pipeline.py"),
                       encoding="utf-8").read()
    emitted = set(re.findall(r'on_event\(\s*"([a-z-]+)"', pipe_src))
    emitted |= set(re.findall(r'\.emit\(\s*"([a-z-]+)"', srv))
    # 前端：e.kind === 'xxx'
    handled = set(re.findall(r"e\.kind === '([a-z-]+)'", js))
    # attempt 走的是兜底分支（不在 if 链里显式判），单独放行
    handled.add("attempt")
    unhandled = sorted(emitted - handled)
    truthy(f"后端 {len(emitted)} 种事件，前端全认",
           not unhandled,
           f"未处理：{unhandled} —— 这些事件会在日志里静默丢失")

    # ── ②b 模型规则两侧必须一致 ────────────────────────────────────
    #
    # 2026-09-02 现场两张截图：codex 段勾上了 gpt-image-2 / gpt-oss-120b /
    # gpt-oss-20b，gemini 段目录里列出 flash / batch-inference / pro-agent。
    # 根因是规则散在三处（Python 的 model_allowed、model_fits_section，
    # 与 web/app.js 的两个正则），三处判据不一致。
    #
    # 现在 Python 侧单一实现在 model_catalog.section_allows，前端有一份**必须
    # 逐条等价**的拷贝（浏览器里跑不了 Python，只能复制规则）。这一项拿同一批
    # 模型名喂两边，结果不一致就失败 —— 单边改规则会被立刻抓到。
    section("②b 模型段规则：前端与 Python 逐条等价")
    import json as _json
    import shutil
    import subprocess

    sys.path.insert(0, ROOT)
    from cpa_probe import model_catalog as _mc     # noqa: E402

    _SAMPLES = [
        # gpt 族：正牌、老款、推理系列、图像、开源小模型
        "gpt-5.6-sol", "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-4o",
        "o3-mini", "gpt-image-2", "gpt-oss-120b", "gpt-oss-20b",
        # claude 族
        "claude-opus-5", "claude-fable-5", "claude-sonnet-5", "claude-opus-4-8",
        "anthropic/claude-opus-5",
        # gemini：pro 变体 / 低版本 / flash / 图像 / 批处理 / 无版本
        "gemini-3.1-pro", "gemini-3.1-pro-high", "gemini-3.1-pro-low",
        "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-search",
        "gemini-3.1-pro-preview-customtools", "gemini-2.5-pro",
        "gemini-2.0-pro", "gemini-3.5-flash", "gemini-3-pro-image-preview",
        "gemini-batch-inference", "gemini-pro-agent",
        "Business/gemini-2.5-pro",
        # kimi
        "kimi-k2", "kimi-k3", "kimi-k2.7-code",
        # 有意排除的族
        "deepseek-v4f", "glm-5.2", "grok-4.6", "x-ai/grok-4.6", "opus-5",
        "llama-3", "qwen-max",
    ]
    _SECS = ("gemini-api-key", "codex-api-key", "claude-api-key",
             "openai-compatibility")

    node = shutil.which("node")
    if not node:
        print("  --  跳过：没有 node，无法执行前端规则（CI 装了 node）")
    else:
        # 只取 app.js 里 SECTION_LABEL 之前那段 —— 规则全在那里，
        # 后面的代码碰 DOM，在 node 里跑会崩。
        cut = js.index("const SECTION_LABEL")
        # 每条产品线取最高世代的判据（2026-09-02 从「同系列取最新」改过来）。
        # key 是标签，值是要喂给两侧的清单 —— 两边跑完逐条比。
        _GEN_CASES = {
            # 现场截图那组：gpt-4o 与 gpt-5.5 必须被 5.6 挤掉
            "shot": ["gpt-4o", "gpt-5.1", "gpt-5.5", "gpt-5.6-luna",
                     "gpt-5.6-terra"],
            "gpt": ["gpt-5.6-sol", "gpt-5.7-sol", "gpt-5.6"],
            "claude": ["claude-opus-4-8", "claude-opus-5"],
            "kimi": ["kimi-k2", "kimi-k3"],
            "prefix": ["anthropic/claude-opus-5", "claude-opus-5"],
            # 日期戳不该让同世代的一款「更新」而挤掉另一款
            "stamp": ["claude-haiku-4-5", "claude-haiku-4-5-20251001"],
            # 整组都认不出版本 —— 全留
            "nover": ["o1", "o3-mini"],
            # 规格后缀不该自成产品线（32k / nano / codex）
            "specs": ["gpt-4-32k", "gpt-5.4-nano", "gpt-5.6",
                      "gpt-5.3-codex", "gpt-5-codex"],
        }
        payload = _json.dumps({"secs": list(_SECS), "models": _SAMPLES,
                               "gen": _GEN_CASES})
        script = js[:cut] + f"""
const IN = {payload};
const out = {{allow: {{}}, proto: {{}}, gen: {{}}, line: {{}}}};
IN.secs.forEach((s) => {{
  out.allow[s] = IN.models.filter((m) => famOk(s, m));
  out.proto[s] = IN.models.filter((m) => protoOk(s, m));
}});
Object.keys(IN.gen).forEach((k) => {{
  out.gen[k] = newestGenerationPerLine(IN.gen[k]);
}});
IN.models.forEach((m) => {{ out.line[m] = productLine(m); }});
console.log(JSON.stringify(out));
"""
        r = subprocess.run([node, "-e", script], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            _fail.append("前端规则脚本执行失败\n      " + (r.stderr or "")[-400:])
            print("  FAIL 前端规则脚本执行失败")
        else:
            got = _json.loads(r.stdout.strip().splitlines()[-1])
            for s in _SECS:
                want = [m for m in _SAMPLES if _mc.section_allows(s, m)]
                eq(f"{s} 放行集合两侧一致", got["allow"][s], want)
                # 手填那条路走的是更宽的 protocol_ok —— 两侧也要一致，
                # 否则「界面手填能写、后端拒收」或反过来（2026-09-03）。
                want_p = [m for m in _SAMPLES
                          if _mc.section_protocol_ok(s, m)]
                eq(f"{s} 手填放行集合两侧一致", got["proto"][s], want_p)
            # 两者的差别必须**只在四族之外**，且只在 compat 段放开。
            # 写死这条不变式：将来任一侧改了族判定，这里立刻炸。
            for s in _SECS:
                extra = [m for m in got["proto"][s] if m not in got["allow"][s]]
                if s == "openai-compatibility":
                    truthy(f"{s} 手填多放行的全是四族之外",
                           all(_mc.family(_mc.bare_name(m)) not in _mc.FAMILIES
                               for m in extra),
                           f"实得 {extra}")
                    truthy(f"{s} 手填确实放开了四族之外（grok/glm 这类）",
                           len(extra) > 0,
                           "放不开就等于操作员没法写回已知可用的 grok-4.6")
                else:
                    eq(f"{s} 手填与工具选型同集合（前三段按族拒）", extra, [])
            for k, ms in _GEN_CASES.items():
                eq(f"取最高世代 · {k}", got["gen"][k],
                   _mc.newest_generation_per_line(ms))
            # 产品线拆分也要逐条一致 —— 它决定分组，错一个就全错
            for m in _SAMPLES:
                eq(f"产品线 · {m}", got["line"][m], _mc._product_line(m))

    # ── ③ 响应字段契约 ─────────────────────────────────────────────
    section("③ 前端依赖的响应字段，后端真的会给")
    # 前端读 d.xxx（轮询响应）
    # 进度字段也在其中：unit_done/unit_total 是进度条的分母分子，
    # eta_* 与 rate_per_min 决定「剩余多久」那一块，slowest_* 决定
    # 「卡在谁身上」。前端读了后端不给 = 界面永久空白，而不会报错。
    for f in ("state", "events", "event_cursor", "calls", "elapsed",
              "done_rows", "total_rows", "results", "error",
              "unit_done", "unit_total", "in_flight",
              "eta_sec", "eta_lo", "eta_hi", "eta_suppressed",
              "rate_per_min", "samples", "slowest_host", "slowest_age"):
        in_js = f"d.{f}" in js
        in_srv = f'"{f}"' in srv
        if in_js:
            truthy(f"d.{f} 后端有提供", in_srv,
                   "前端读了但后端 snapshot/结果里没有这个键")

    # 方案级警告必须被读。后端一直返回 /api/plan 的 warnings，前端曾从来
    # 不读 —— 定档退化（整批下移、越过现有档位、用户改成同值）会改变哪个站
    # 先被尝试，不显示等于那一轮修复不可见。
    section("③ 定档提示必须显示在界面上")
    truthy("/api/plan 返回 warnings", '"warnings"' in srv)
    truthy("前端读 d.warnings", "d.warnings" in js,
           "后端返回但前端不读 = 定档退化悄悄发生")
    truthy("有渲染函数把它放进 planmeta",
           "planWarnings(d)" in js and "function planWarnings" in js)

    # ── ③c 首屏不能是白屏 ──────────────────────────────────────────
    #
    # 2026-09-02 现场：漂移检测在 /api/context 的请求路径里拉 GitHub，国内
    # VPS 拉不通时干等 15 秒，而 #gate 与 #app 都 hidden —— 那段时间只有页头，
    # 正文纯空白且没有任何提示，看起来像页面坏了。
    # ── ③e 目录读不到时后端填的模型必须显示出来 ────────────────────────
    #
    # 2026-09-02 现场（截图1）：后端已按「当前市面最新」填了 6 个模型、警告
    # 文本里也列着那 6 个名字，而那一格只渲染了一个空的手填框 —— 它从
    # S.forced 取值，而 S.forced 此刻是空的。用户看到空白，且提交时读的正是
    # S.forced，所以那个段勾上也写不进任何模型。
    # ── ③f 落后目录不预勾 + 限频学习要可见 ────────────────────────────
    section("③f 落后目录不预勾、限频学习可见")
    truthy("后端回 market_top_gen", '"market_top_gen"' in srv,
           "前端在勾选前就渲染结果表，那时还没有 /api/plan 的响应")
    truthy("前端 pickDefaults 用它判落后", "market_top_gen" in js)
    truthy("落后时返回空（一个都不勾）",
           "if (top && genGreater(mkt, top)) return [];" in js)
    truthy("界面说清为什么不勾", "整份目录都落后于市面最新" in js)
    truthy("后端回 catalog_stale", '"catalog_stale"' in srv)
    truthy("限频学习有事件", "rate-limit-learned" in js,
           "自动放慢了却不说，用户会以为探测卡住了")

    section("③e 无目录时用后端方案填勾选框")
    truthy("留了 fallback 容器", "cats fallback" in js,
           "没有容器就没地方填，用户只能看到空手填框")
    truthy("refreshPlan 用 sp.models 填它",
           ".cats.fallback" in js and "sp.models.map" in js)
    truthy("填完立刻回写 S.forced", "S.forced[p.line_no] = S.forced[p.line_no]" in js,
           "提交时读的是 S.forced 不是 DOM —— 不回写就是「勾着但没接管」")
    truthy("seed 猜测**不**回写 S.forced",
           "sp.model_source !== 'seed'" in js,
           "回写会让后端把猜测当手填：徽标不再提示无依据，且跨段新增那道闸"
           "按 model_source 判、manual 放行 —— 猜测清单能凭空新增条目")
    truthy("已有用户记录时不覆盖", "rec !== undefined ? rec : sp.models" in js)
    truthy("容器已填过就不重填", "!fb.querySelector('.cm')" in js,
           "每次 refreshPlan 都重填会把用户的取消勾选覆盖掉")
    truthy("全选/反选/清空按钮跟着出现", "fallback-tools" in js)
    truthy("placeholder 不再说「手填模型名」",
           "站方目录也没报模型：手填模型名" not in js,
           "模型已经填好了，再让用户手填是误导")

    # ── ③f 可用行也要能改模型清单（2026-09-03 现场） ────────────────────
    #
    # 那一格原来只渲染 `v.models.join(', ')` 纯文本，三处后果：
    #   ① v.models 为空（静默换模 / 200 包错误体，`_accept` 全拒）时显示
    #      「无可信模型」，而后端方案里 sp.models 已经有 6 个（seed 兜底）——
    #      判死行有 .cats.fallback 容器接住它，可用行连容器都没有。
    #   ② 可用行完全没有手填入口，操作员想改清单只能去改 config.yaml。
    #   ③ 探测只验 max_models（默认 4）个就停，站方目录里其余名字看不见。
    section("③f 可用行的模型格：勾选框 + 手填 + 目录补充")
    truthy("可用行不再是纯文本 join",
           "esc(v.models.join(', ')) || '<span class=\"hint\">无可信模型" not in js,
           "纯文本没有勾选框也没有手填框，实测清单为空时只能显示「无可信模型」")
    truthy("实测清单做成勾选框", "const uProbed" in js and "uPick.has(m)" in js)
    truthy("目录里探测没验到的也列出来（默认不勾）",
           "const uExtra" in js and "!uProbed.includes(m)" in js,
           "探测只验前几个就停，其余名字看不见等于选不了")
    truthy("预勾结果**不**回写 S.forced",
           "if (uRec === undefined && uPick.size)" not in js,
           "forced 非空会让 build_plan 走 manual 分支：徽标从「实测」变「手填」、"
           "recommended 翻假，而 seed 猜测被洗成 manual 正好绕过新增段那道闸")
    truthy("可用行也有手填框", "const uFm" in js)
    truthy("实测为空时留 fallback 容器给 refreshPlan 填",
           js.count("cats fallback") >= 2,
           "可用行没有容器 = 后端填的 6 个模型无处显示（现场截图那一条）")
    truthy("后端 verdict 带 catalog", '"catalog"' in srv,
           "前端要用它列出探测没验到的名字")
    # 手填框与勾选框是同一个段的两个入口，任一侧变化都要**合并**另一侧
    # ── ③i 目录里一个四族的都没有时收站方自己报的 ──────────────────────
    #
    # 2026-09-03：runanytime 与 facai 的 compat 段目录里只有 grok-4.6 /
    # glm-5.2，而 grok-4.6 是那个站唯一端到端验证过的模型。按族过滤后目录变空
    # → 前端只显示手填框、后端写工具猜的名字（这个站从没报过它们）。
    # 前后端要用同一条规则退这一步。
    section("③i 目录全是四族之外时退一步收下")
    truthy("前端按 famOk 优先、protoOk 兜底",
           "const catFam = (v.catalog || []).filter((m) => m && famOk(sec, m))" in js
           and "const cat = catFam.length ? catFam : catProto;" in js,
           "只按 famOk 过滤会让这类站的目录整份变空")
    truthy("「已滤掉」只算协议层发不出去的",
           "const cut = (v.catalog || []).filter((m) => m && !protoOk(sec, m)).length" in js,
           "四族之外那批被收下了，不该再报成滤掉")
    truthy("界面说清为什么退这一步", "catOff ?" in js and "从没报过它们" in js)
    truthy("后端同一条规则",
           "catalog_offfamily" in io.open(
               os.path.join(ROOT, "cpa_probe", "plan.py"),
               encoding="utf-8").read(),
           "前端列出来、后端却写猜测清单 = 两边不一致")

    truthy("手填框旁有即时校验提示位", 'class="fmhint"' in js,
           "协议层不成立的手填要当场标出来，不用等一次 /api/plan 往返")
    truthy("校验用 protoOk 而不是 famOk",
           "const bad = typed.filter((m) => !protoOk(sc, m))" in js,
           "famOk 会把 grok-4.6 标成红的，而 compat 段那恰恰是唯一验证过的模型")
    truthy("四族之外单独给一句说明（不是报错）",
           "const off = typed.filter((m) => protoOk(sc, m) && !famOk(sc, m))" in js)
    truthy("手填框写入时合并勾选框的选中项",
           "const chosen = tr" in js and "chosen.concat(typed)" in js,
           "只取手填值整份覆盖 = 勾了 3 个再手填 1 个，前 3 个静默丢失")

    # ── ③g 落盘那层的闸必须反映到界面 ──────────────────────────────────
    #
    # 2026-09-03 现场：跨段新增的闸只存在于 rebuild_config_full 内部，界面按
    # 「没有闸」渲染 —— writable / recommended 都是 True，显示「建议写入」并
    # 默认勾上，勾了写不进，只在 warnings 里留一句话。
    section("③g 界面三态与落盘一致")
    truthy("后端回 write_blocked", '"write_blocked"' in srv)
    truthy("后端回 new_section", '"new_section"' in srv)
    truthy("writable 把 write_blocked 算进去",
           "not self.write_blocked" in
           io.open(os.path.join(ROOT, "cpa_probe", "plan.py"),
                   encoding="utf-8").read(),
           "界面显示可写而落盘拒收 = 勾了没反馈")
    truthy("前端按它显示「不写入」", "sp.write_blocked ? '不写入'" in js)
    truthy("被拦下的段不进「全勾」",
           "if (sp.write_blocked) { blocked += 1; return; }" in js,
           "勾了也写不进，界面上勾着就是在骗人")
    truthy("新增段在行内标出来", "new_section && !sp.write_blocked" in js,
           "它改变的是条目数而不只是某个字段，diff 里不显眼")
    truthy("来源徽标每轮重建而不是追加",
           "ml.innerHTML = bits.join(' ')" in js,
           "追加式写法在 model_source 变化后会留着上一轮的徽标")

    # ── ③h 全量重探也要读 selected ──────────────────────────────────
    #
    # 那条路原来完全不读 `selected`，于是操作员取消勾选的段照样被重写。
    section("③h 全量重探尊重勾选")
    truthy("full_redetect 分支里有 write_plans",
           "write_plans" in srv and "shallow.sections = keep" in srv,
           "不剪选择集 = 取消勾选的段仍被改写")

    # ── ③d weight:0 的措辞必须跟着调度策略变 ─────────────────────────
    #
    # 2026-09-02 核实 CPA 源码：只有 WeightedRoundRobinSelector.Pick 调
    # positiveWeightAuths 把零权重凭据整个剔除（selector.go:650 → 637-644）；
    # RoundRobinSelector（:589，**默认**策略）与 FillFirstSelector（:787）
    # 根本不读 weight。说成「一定不参与调度」在后两种策略下是错的。
    section("③d weight:0 的含义随 routing.strategy 变")
    truthy("后端把策略回给前端",
           '"routing_strategy"' in srv and '"weight_zero_excludes"' in srv)
    truthy("前端按它分岔措辞", "weight_zero_excludes" in js,
           "两种策略下同一句话不可能都对")
    truthy("非 wrr 时说明「仍参与轮询」", "仍参与轮询" in js or "仍会正常参与轮询" in js)

    section("③c 首屏骨架与 pending 状态")
    truthy("HTML 里有启动骨架 #bootbox", 'id="bootbox"' in html,
           "/api/context 返回前什么都不显示 = 白屏")
    truthy("骨架默认可见（不带 hidden）",
           re.search(r'id="bootbox"[^>]*\shidden', html) is None,
           "带 hidden 就等于没有骨架")
    truthy("拿到 ctx 后收起骨架", "hideSkel()" in js)
    truthy("三条出路都收骨架（无 token / 出错 / 成功）",
           js.count("hideSkel()") >= 3,
           "漏一条就会让骨架与正文同时显示")
    truthy("慢的时候换文案说明原因", "#bootmsg" in js and "bootmsg" in html)
    truthy("后端给得出 pending 状态", '"pending": True' in srv,
           "首次核对还没算完时要有明确状态，不能让前端猜")
    truthy("前端认 pending 并轮询", "d.pending" in js
           and "scheduleDriftPoll" in js)
    truthy("轮询有次数上限", "_driftPolls >= 10" in js,
           "无上限会一直刷 /api/context")
    truthy("过期结论标出来", "d.refreshing" in js and '"refreshing"' in srv,
           "旧结论与刚核对的在界面上不能长一个样")
    truthy("漂移检测走缓存快照而不是直接调",
           "_drift_snapshot(" in srv
           and re.search(r"drift\s*=\s*cp\.check_profile_drift", srv) is None,
           "直接调就回到「拉 GitHub 挡住页面」那个状态")

    # 写回响应的关键字段 —— 生效链的可见性全靠它们
    section("③ 写回响应：重载状态必须可见")
    for f in ("reload_ok", "reload_msg", "written", "backup", "diffs"):
        # 后端那侧找的是 JSON 键的字面量（带引号），所以先把带引号的形态
        # 算成普通变量，别塞进 f-string 的表达式里 —— Python 3.9 的 f-string
        # 表达式部分**不允许出现反斜杠**（3.12 才放开），而 CI 的下限是 3.9。
        # 本机 3.14 上编译得过、CI 3.9 上 SyntaxError，是最容易漏的一类。
        quoted = '"' + f + '"'
        in_js = "有" if ("d." + f) in js else "无"
        in_srv = "有" if quoted in srv else "无"
        truthy(f"d.{f} 前后端都有",
               ("d." + f) in js and quoted in srv,
               f"前端 {in_js} / 后端 {in_srv}")

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

    section("⑦ 人工接管（探测判不可用但操作员确知可用）")
    # 2026-08-31 实测缺陷：很多中转站**不给测活**（探针短消息被拦、
    # 分组只允许特定客户端），而真实对话完全正常。原来不可用的段
    # 渲染的是 `<td class="pick">—</td>` —— 没有勾选框，这类站完全无法导入。
    truthy("不可用的段也渲染勾选框", "sel force" in js,
           "判定会错，必须留人工出口")
    truthy("有手填模型的输入框", 'class="fm"' in js)
    truthy("前端把 forced 传给后端", "forced: S.forced" in js)
    truthy("后端读 forced", '"forced"' in srv)
    truthy("后端把 force 转给 build_plan", "force=" in srv)
    # 2026-09-01 反转：原来这里断言「没填模型就拦住勾选」，前提是
    # 「空清单到后端会被跳过」。那个前提已经不成立 —— build_plan 现在给
    # 判死段用种子模型兜底（严禁 priority 等参数未定），所以每段都有确定
    # 清单，勾选不该再被拦。现在要保的是**给候选可选**，而不是逼人手打。
    truthy("目录候选给成可勾清单", 'class="cm"' in js,
           "站方 /models 报得出模型时不该逼操作员手打")
    truthy("标注模型来源", "model_source" in js,
           "实测通过 / 目录声明 / 手填 / 种子兜底，可信度差一截")
    truthy("种子兜底的段有区分标记", "'seed'" in js)
    truthy("重来时清空 forced", "S.forced = {}" in js)

    section("⑧ 三条途径的字段必须齐平")
    # 2026-09-02 用户指出：单站诊断只出 header，其余参数（代理、指纹、
    # priority、前缀、模型、上限、影响面）全缺。根因是四条途径各自组装返回值。
    # 现在诊断也走 prober.probe + build_plan，复用同一套序列化。
    truthy("诊断走完整探测", "fp.probe(row)" in srv,
           "只跑画像梯只能得出 header，其余参数全缺")
    truthy("诊断走 build_plan", "plan_out = plan_json(pl)" in srv,
           "priority / 前缀 / 影响面 都由 build_plan 算")
    truthy("诊断回 verdicts", '"verdicts": verdicts' in srv)
    truthy("前端渲染完整参数表", "完整参数（与全量检测同一套判定）" in js)
    for f in ("profile_name", "min_body_kind"):
        truthy(f"verdict_json 带 {f}", f'"{f}": v.' in srv,
               "界面上「请求指纹」那一列靠它")
    # time_window 要转成 list（tuple 不能直接进 JSON）
    truthy("verdict_json 带 time_window",
           '"time_window": list(v.time_window)' in srv,
           "限时段的站要标出窗口，否则看着像不可用")
    for f in ("prefix", "weight"):
        truthy(f"plan_json 带 {f}", f'"{f}": sp.{f}' in srv,
               "它会落进 config.yaml")
    truthy("weight:0 在结果表里有警示", "逐出调度池" in js,
           "写回后 CPAMP 显示未启用，界面必须提前说清")

    section("⑧ 收尾时的缺口必须说清")
    # 现场报障：`71/79 (90%)` 就切到第三步，看着像「没跑完就往下走」。
    # 实际是 8 个候选探测抛异常、进不了结果集（server.py 的 lost 分支
    # 已逐条报了原因）。缺陷在前端只显示比值，不说差额是**失败**还是**未跑**。
    truthy("算出缺口", "d.total_rows - d.done_rows" in js)
    truthy("缺口有独立配色", "'partial'" in js)
    truthy("说清不是没跑完", "不是「还没跑完」" in js)

    section("⑧ 过程可见性：新增事件前端都要认")
    # 后端新发的事件如果前端不认，就在日志里静默丢失 ——
    # 用户只看到同一个模型出现两次，不知道为什么。
    truthy("前端认 transient-retry", "transient-retry" in js)
    truthy("前端认 model-rejected", "model-rejected" in js)

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
