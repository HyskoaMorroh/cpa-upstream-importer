#!/usr/bin/env python3
"""cpa_probe 回归测试。零网络请求，可随时复跑。

    cd /opt/deploy/upstream-importer
    python3 tests/test_probe.py                    # 纯逻辑，不需要 config.yaml
    python3 tests/test_probe.py /opt/deploy/config.yaml   # 加上真实文件的用例

不用 pytest —— VPS 上不想装依赖。失败即非零退出，可直接进 CI。

每条断言背后都是一次实测教训，注释里写清是哪条。改判定规则前先跑这个。
"""

from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # fixture_cfg

import fixture_cfg
import cpa_probe as cp
from cpa_probe.pipeline import CandidateResult, SectionVerdict
from cpa_probe.writeback import (
    _detect_indent,
    _section_span,
    apply_diffs,
    build_diffs,
    validate,
)

FAILED: list[str] = []
PASSED = 0


def eq(name: str, got, want) -> None:
    global PASSED
    if got != want:
        FAILED.append(f"{name}\n      got  = {got!r}\n      want = {want!r}")
    else:
        PASSED += 1
        print(f"  ok  {name}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


# ==========================================================================
# 1. 解析与 URL 规范化
# ==========================================================================


def test_parse() -> None:
    section("解析与 URL 规范化")

    txt = """
https://example.com,sk-abc123456789
https://api.example.org/v1,sk-def987654321
# 注释行忽略
bare-domain.io,sk-xyz111222333

https://bad.com
,sk-nourl
https://nokey.com,
"""
    res = cp.parse_lines(txt)
    eq("有效行 3", len(res.valid), 3)
    eq("无效行 3", len(res.invalid), 3)
    eq("裸域名自动补 https", res.valid[2].bare, "https://bare-domain.io")
    eq("尾部 /v1 被剥离", res.valid[1].bare, "https://api.example.org")

    r = res.valid[0]
    # 12 站 206 条目零例外：段决定 base-url 形态，用户不必记
    eq("gemini 不带 /v1", r.base_for("gemini-api-key"), "https://example.com")
    eq("claude 不带 /v1", r.base_for("claude-api-key"), "https://example.com")
    eq("codex 必带 /v1", r.base_for("codex-api-key"), "https://example.com/v1")
    eq("compat 必带 /v1", r.base_for("openai-compatibility"), "https://example.com/v1")

    # /v1beta 是 gemini 的真实路径段，不能当成 /v1 剥掉
    eq("不误剥 /v1beta", cp.strip_v1("https://x.com/v1beta"), "https://x.com/v1beta")

    eq("脱敏保前6后4", r.masked(), "sk-abc...6789")
    eq("短 key 也脱敏", cp.mask_key("sk-123"), "sk-***")
    eq("空 key 返回空", cp.mask_key(""), "")

    eq("缺逗号被拒", "逗号" in res.invalid[0].error, True)


# ==========================================================================
# 2. 定性判定 —— 正文关键词优先于状态码
# ==========================================================================


def test_classify() -> None:
    section("定性判定")

    cases = [
        # 2026-08-29 真实误判修正：relay-l 的 403 正文是余额，不是门禁。
        # 处置方向完全相反 —— 一个该充值，一个该换 IP。
        ("403", "预扣费额度失败, 剩余 $0.190928", "余额"),
        ("403", '{"error":{"message":"user quota is not enough"}}', "余额"),
        ("402", "Budget pool quota has been exhausted", "余额"),
        ("403", "该模型额度已经达到上限", "余额"),
        # CF 特征词
        ("403", "<html>Attention Required! | Cloudflare</html>", "IP封"),
        ("403", "challenge-platform script", "IP封"),
        ("403", "访问已被拦截，请完成安全验证", "IP封"),
        # 403 空正文 = 概率性边缘拦截，重试即可，不代表站点坏
        ("403", "", "边缘"),
        ("403", "   \n  ", "边缘"),
        # 403 有正文但无余额/CF 特征 = 站方策略
        ("403", "站方策略拒绝该请求", "门禁"),
        # 探测方法自身触发的，不是站点故障
        ("400", "反测活已拦截本次请求：短消息命中测活探针关键词（如 hi、你好等）", "反测活"),
        ("429", "bulk model probing detected", "限频"),
        # 站方硬拒
        ("400", '{"error":"sensitive_words detected"}', "死路"),
        ("404", '{"error":{"code":"model_not_found"}}', "死路"),
        ("500", "当前分组无可用渠道", "死路"),
        ("401", "unauthorized client", "鉴权"),
        # CPA 自注入工具被拒
        ("403", "Image generation is not enabled for this group", "注入"),
        # 状态码兜底
        ("200", '{"model":"claude-opus-5"}', "可用"),
        ("429", "too many requests", "限流"),
        ("503", "upstream busy", "临时"),
        ("500", "internal error", "临时"),
        ("000", "", "未知"),
    ]
    for st, body, want in cases:
        got, _why = cp.classify(st, body)
        eq(f"classify({st}, {body[:30]!r})", got, want)

    # 余额判定必须排在 CF 之前：同一条正文两种特征都有时以余额为准
    got, _ = cp.classify("403", "cdn-cgi ... 预扣费额度失败")
    eq("余额优先于 CF", got, "余额")

    section("余额的英文说法（2026-08-31 补）")
    # 原来只认 quota 家族与中文「余额不足」，于是这两种常见英文表述落到
    # 「门禁」—— 门禁是 usable=False，等于**一个充值就能用的站被判死**。
    # 判错方向是「把活站当死站」，用户白丢一个可用站且看不出原因。
    for body in ("insufficient balance", "credit exhausted", "insufficient credits",
                 "out of credit", "balance is too low", "账户余额不足，请充值",
                 "您已欠费", "余额已用完"):
        got, _ = cp.classify("403", body)
        eq(f"判余额：{body[:18]}", got, "余额")
    # 反向：这些不能被误判成余额
    for st, body, want in (("403", "<html>Attention Required!</html>", "IP封"),
                           ("401", "unauthorized client", "鉴权"),
                           ("404", "model_not_found", "死路")):
        got, _ = cp.classify(st, body)
        eq(f"不误判成余额：{body[:24]}", got, want)

    section("客户端门禁：不看状态码，看正文（2026-08-31 实测）")
    # 站方只认特定客户端。**必须与「门禁」分开** —— 处置完全不同：
    # 门禁要站方侧开通，这个补客户端标识就可能过。
    #
    # 关键是它可能挂在**任意**状态码上。实测那个站回的是 503，而 503 在
    # 状态码兜底里是「临时」（usable=True、该重试）—— 于是探测白重试两次，
    # 而这类拒绝与站方负载无关，重试一万次也一样。
    for st, body in (("503", '{"error":{"message":"No available accounts: this '
                             'group only allows Claude Code clients"}}'),
                     ("403", "This group is restricted to Claude Code clients "
                             "(/v1/messages only)"),
                     ("403", "client not allowed"),
                     ("400", "仅支持 Claude Code 客户端")):
        got, _ = cp.classify(st, body)
        eq(f"{st} 判客户端：{body[:34]}", got, "客户端")
    eq("客户端门禁不可接入", cp.is_usable("客户端"), False)
    eq("客户端门禁不降权（补标识可能就过）", cp.should_downrank("客户端"), False)
    # 探测复制不了那个客户端形态时，用户需要知道还有别的出路
    eq("处置里点明人工接管这条路", "人工接管" in cp.advice("客户端"), True)
    # 反向：普通 503 仍是「临时」，不能被新规则抢走
    for st, body, want in (("503", "upstream busy", "临时"),
                           ("503", "Upstream service temporarily unavailable", "临时"),
                           ("502", "Bad Gateway", "临时"),
                           ("503", "No available channel", "死路")):
        got, _ = cp.classify(st, body)
        eq(f"不被客户端规则误收：{body[:30]}", got, want)

    section("200 但正文是错误体（假阳性防线）")
    # 有的站对**所有**请求都回 200，把真实错误放正文里。而 Attempt.ok 只看
    # 状态码、model_matches 拿不到 model 字段时按设计放行 —— 两者叠加会让
    # 这种站四段全判可用、注册 11 个模型，实际完全不能用。
    # 死站进 config.yaml 会耗尽重试预算，最终让客户端收到 500。
    from cpa_probe.classify import has_error_envelope as _hee
    for body in ('{"error":{"message":"no available channel"}}',
                 '{"error":"quota exceeded"}',
                 '{"type":"error","error":{"type":"overloaded_error"}}',
                 '{"error":[{"code":1}]}'):
        eq(f"认出错误体：{body[:30]}", _hee(body), True)
    # 判据必须窄 —— 这些合法响应一个都不能误伤
    for body in ('{"id":"msg_01AB","model":"claude-opus-5","content":[{"text":"ok"}]}',
                 '{"choices":[{"message":{"content":"talking about error handling"}}]}',
                 '{"modelVersion":"gemini-2.5-pro","candidates":[]}',
                 '{"error":null}', '{"error":""}', '{"error":{}}', '{"error":[]}',
                 'data: {"delta":"hi"}', '', 'plain text', '[1,2,3]',
                 '{"type":"message","content":[]}'):
        eq(f"不误伤：{(body[:30] or '<空>')}", _hee(body), False)

    section("模型白名单：o 系列不能被漏掉")
    # 白名单规则是「只留 gemini / gpt / claude 三类」。o1 / o3-mini 属于
    # 「gpt 那一类」，只是 OpenAI 换了命名 —— 2026-08-31 实测被前缀匹配漏掉。
    from cpa_probe.pipeline import model_allowed as _ma
    for m in ("o1", "o1-mini", "o3", "o3-mini", "o4-mini", "o1-2024-12-17",
              "gpt-4o", "claude-opus-5", "gemini-2.5-pro",
              "Business/gemini-2.5-pro", "anthropic/claude-fable-5"):
        eq(f"放行 {m}", _ma(m), True)
    # 不能因为放宽 o 系列就误收这些
    for m in ("openai-whisper", "omni-moderation", "o", "ollama-llama3",
              "order-model", "deepseek-chat", "grok-4", "qwen-max",
              "glm-4", "kimi-k2", "llama-3"):
        eq(f"排除 {m}", _ma(m), False)

    section("处置语义")
    eq("余额不降权（充值自愈）", cp.should_downrank("余额"), False)
    eq("限流不降权（CPA 自带轮换）", cp.should_downrank("限流"), False)
    eq("边缘不降权（概率性）", cp.should_downrank("边缘"), False)
    eq("死路要降权", cp.should_downrank("死路"), True)
    eq("IP封要降权", cp.should_downrank("IP封"), True)
    eq("可用视为可接入", cp.is_usable("可用"), True)
    eq("门禁不可接入", cp.is_usable("门禁"), False)
    eq("余额仍可接入", cp.is_usable("余额"), True)

    section("正文摘要")
    html = "<html><script>var x=1</script><body>  拒绝   访问 </body></html>"
    eq("剥 script 与标签", cp.body_excerpt(html), "拒绝 访问")
    eq("超长截断带省略号", cp.body_excerpt("x" * 500).endswith("…"), True)


# ==========================================================================
# 3. 指纹 —— id 形态比 model 字段可靠
# ==========================================================================


def test_fingerprint() -> None:
    section("后端 id 指纹")

    eq("Bedrock", cp.backend_of("msg_bdrk_01ABC"), "AWS Bedrock")
    # base58 字母表排除 0OIl（易混字符），所以样本里不能出现小写 l
    eq("Anthropic 官方", cp.backend_of("msg_01AbCdEfGhJiKm"), "Anthropic 官方")
    eq("含 base58 排除字符则不算官方",
       cp.backend_of("msg_01AbCdEfGhIjKl"), "其他形态")
    eq("中转自造 msg", cp.backend_of("msg_" + "a" * 32), "中转自造")
    eq("OpenAI chat 兼容", cp.backend_of("chatcmpl-x1"),
       "OpenAI Chat 兼容（多为中转）")
    eq("Responses 官方", cp.backend_of("resp_" + "f" * 40),
       "OpenAI Responses 官方形态")
    eq("中转自造 resp", cp.backend_of("resp_" + "a" * 20), "中转自造")
    eq("无 id 返回 ?", cp.backend_of(None), "?")
    eq("无法识别的前缀", cp.backend_of("weird-id-123"), "未知形态")

    section("换模判定容错")
    # 早期版本这里返回 False，配合 read(20000) 截断造成 100% 假换模率。
    # /v1/responses 把整个 Codex 系统提示放在 instructions（40KB+），
    # model 字段排其后被切掉 → resp_model 返回 None → 误判换模。
    eq("actual 为 None 不判换模", cp.model_matches("claude-opus-5", None), True)
    eq("日期版本后缀算同一模型",
       cp.model_matches("claude-opus-5", "claude-opus-5-20260101"), True)
    eq("thinking 后缀算同一模型",
       cp.model_matches("claude-opus-5", "claude-opus-5-thinking"), True)
    eq("latest 后缀算同一模型",
       cp.model_matches("gpt-5.6-sol", "gpt-5.6-sol-latest"), True)
    eq("真换模判 False",
       cp.model_matches("gpt-5.6-sol", "agnes-2.0-flash"), False)

    section("响应字段提取")
    body = ('{"id":"msg_01ABCDEFGHIJ","model":"claude-opus-5",'
            '"usage":{"input_tokens":4390,"output_tokens":12}}')
    eq("resp_model", cp.resp_model(body), "claude-opus-5")
    eq("resp_id", cp.resp_id(body), "msg_01ABCDEFGHIJ")
    eq("input_tokens", cp.input_tokens(body), 4390)
    eq("gemini 的 modelVersion 也认",
       cp.resp_model('{"modelVersion":"gemini-2.5-pro"}'), "gemini-2.5-pro")
    eq("嵌套 response.model 也认",
       cp.resp_model('{"response":{"model":"gpt-5.6-sol"}}'), "gpt-5.6-sol")
    eq("坏 JSON 退化为正则",
       cp.resp_model('garbage "model": "claude-opus-5" more'), "claude-opus-5")
    eq("prompt_tokens 同义",
       cp.input_tokens('{"usage":{"prompt_tokens":777}}'), 777)

    section("截断校验")
    # relay-m.example：发 105 万字符只回 132,696 tokens，且模型被换成
    # codex-auto-review —— 那个 200 完全不可信
    eq("远小于发送量判截断",
       cp.truncated(1_050_000, '{"usage":{"input_tokens":132696}}'), True)
    eq("接近发送量不判截断",
       cp.truncated(4390, '{"usage":{"input_tokens":4300}}'), False)
    eq("拿不到 token 数不判截断", cp.truncated(4390, "{}"), False)

    section("换模率统计")
    samples = [
        {"status": "200", "requested": "gpt-5.6-sol", "actual": "gpt-5.6-sol",
         "backend": "中转自造", "input_tokens": 4390},
        {"status": "200", "requested": "gpt-5.6-sol", "actual": "agnes-2.0-flash",
         "backend": "其他形态", "input_tokens": 285},
        {"status": "200", "requested": "gpt-5.6-sol", "actual": None,
         "backend": "?", "input_tokens": 4390},
        {"status": "429", "requested": "gpt-5.6-sol", "actual": None},
    ]
    sw = cp.swap_rate(samples)
    eq("分母不含 unknown", sw["rate_pct"], 50.0)
    eq("same 计数", sw["same"], 1)
    eq("swap 计数", sw["swap"], 1)
    eq("unknown 单列", sw["unknown"], 1)
    eq("多后端形态是换模信号", sw["multi_backend"], True)
    eq("token 跨度异常是强信号", sw["token_span_anomaly"], True)

    clean = [{"status": "200", "requested": "m", "actual": "m",
              "backend": "b", "input_tokens": 100} for _ in range(3)]
    eq("全一致换模率 0", cp.swap_rate(clean)["rate_pct"], 0.0)
    eq("全一致单后端", cp.swap_rate(clean)["multi_backend"], False)


# ==========================================================================
# 4. 去重指纹 —— 段间行为相反
# ==========================================================================


def test_dedup() -> None:
    section("去重五元组")

    a = cp.dedup_key("claude-api-key", api_key="k1", base_url="https://x.com")
    b = cp.dedup_key("claude-api-key", api_key="k1", base_url="https://x.com")
    eq("同五元组同指纹", a, b)

    diff_cases = [
        ("proxy 不同", cp.dedup_key("claude-api-key", api_key="k1",
                                    base_url="https://x.com",
                                    proxy_url="http://mihomo:7890")),
        ("prefix 不同", cp.dedup_key("claude-api-key", api_key="k1",
                                     base_url="https://x.com", prefix="CDX/")),
        ("headers 不同", cp.dedup_key("claude-api-key", api_key="k1",
                                      base_url="https://x.com",
                                      headers={"User-Agent": "x"})),
        ("段不同", cp.dedup_key("codex-api-key", api_key="k1",
                                base_url="https://x.com")),
        ("key 不同", cp.dedup_key("claude-api-key", api_key="k2",
                                  base_url="https://x.com")),
    ]
    for name, other in diff_cases:
        eq(f"{name}则指纹不同", a == other, False)

    # headers 顺序不能影响指纹，否则同一条目会被判成两个
    h1 = cp.dedup_key("claude-api-key", api_key="k", base_url="https://x.com",
                      headers={"A": "1", "B": "2"})
    h2 = cp.dedup_key("claude-api-key", api_key="k", base_url="https://x.com",
                      headers={"B": "2", "A": "1"})
    eq("headers 顺序无关", h1, h2)


# ==========================================================================
# 5. 定档 —— 层级隔离下的避让
# ==========================================================================


def _band(
    tiers: dict[int, list[str]],
    model_top: dict[str, int],
    model_tiers: dict[str, dict[int, list[str]]] | None = None,
) -> cp.Band:
    b = cp.Band(section="claude-api-key")
    b.tiers = sorted(tiers, reverse=True)
    b.hosts_at = tiers
    b.model_top = model_top
    b.model_tiers = model_tiers or {}
    return b


def test_priority() -> None:
    section("priority 定档")

    band = _band(
        {1000: ["a"], 950: ["b"], 900: ["c"], 120: ["d"], 110: ["e"], 20: ["f"]},
        {"opus-5": 1000, "sonnet-5": 120, "haiku": 20},
    )
    eq("顶档取最大值", band.top, 1000)
    # 相邻档间隔 > 1 即算空档，含最高档之下那一段
    eq("空档识别", band.gaps(),
       [(950, 1000), (900, 950), (120, 900), (110, 120), (20, 110)])

    # 关键回归：ceiling 必须取 min 而不是 max。
    # 取 max 时，同时声明 opus-5(顶层1000) 与 sonnet-5(顶层120) 的候选会拿到
    # 975 —— 不动 opus-5，却把 sonnet-5 的顶层整个换掉。层级隔离下那是
    # 完全取代，不是「略微靠前」。
    p, why = cp.suggest_priority(band, 100, models=["opus-5", "sonnet-5"])
    imp = cp.compute_impact(band, ["opus-5", "sonnet-5"], p)
    eq("满分候选不劫持任何顶层", [i.model for i in imp if i.hijacks], [])
    eq("避让说明写明跳过", "避让" in why, True)

    p1, _ = cp.suggest_priority(band, 100, models=["opus-5"])
    eq("单模型也不劫持",
       [i.model for i in cp.compute_impact(band, ["opus-5"], p1) if i.hijacks], [])

    plo, _ = cp.suggest_priority(band, 10, models=["opus-5"])
    eq("低分档位不高于高分", plo <= p1, True)

    for name, val in (("高分", p1), ("低分", plo), ("多模型", p)):
        eq(f"{name}建议值不撞现有档", val in band.tiers, False)

    section("试用期默认 · 新站不挤掉已验证的站")
    # 上面那个 band 没填 model_tiers，试用期算出的挡站数恒为 0，测不到东西。
    # 用带 model_tiers 的真实形状：claude 段那种「顶层 1000、下面还有 5 档」。
    trial = _band(
        {1000: ["relay-h"], 950: ["relay-a"], 900: ["relay-f"],
         800: ["relay-l"], 120: ["relay-m"], 30: ["relay-j"]},
        {"opus-5": 1000},
        {"opus-5": {1000: ["relay-h"], 950: ["relay-a"], 900: ["relay-f"],
                    800: ["relay-l"], 120: ["relay-m"], 30: ["relay-j"]}},
    )
    p_trial, why_trial = cp.suggest_priority(trial, 100, models=["opus-5"])
    p_score, why_score = cp.suggest_priority(trial, 100, models=["opus-5"],
                                             probation=False)
    n_trial = len(cp.compute_impact(trial, ["opus-5"], p_trial)[0].shadowed_hosts)
    n_score = len(cp.compute_impact(trial, ["opus-5"], p_score)[0].shadowed_hosts)

    # 核心：满分候选默认也进低档，而不是按分数抢到 975
    eq("试用期档位低于按得分档位", p_trial < p_score, True)
    eq("试用期挡的站更少", n_trial < n_score, True)
    eq("试用期理由写明档位性质", "试用期档位" in why_trial, True)
    eq("试用期理由给出提权目标", str(p_score) in why_trial, True)
    eq("试用期理由说明提权代价", "跑几天" in why_trial, True)
    eq("按得分模式理由不提试用期", "试用期" in why_score, False)
    # 两种模式都不许劫持顶层
    for tag, val in (("试用期", p_trial), ("按得分", p_score)):
        eq(f"{tag}不劫持顶层",
           cp.compute_impact(trial, ["opus-5"], val)[0].hijacks, False)

    # 试用期档位应当是「可插档里挡站最少」的那个
    from cpa_probe.plan import _shadow_count
    cands = [(lo + hi) // 2 for lo, hi in trial.gaps()]
    cands = [c for c in cands if c <= trial.model_top["opus-5"]]
    eq("试用期取挡站最少的档",
       _shadow_count(trial, ["opus-5"], p_trial),
       min(_shadow_count(trial, ["opus-5"], c) for c in cands))

    # 全新模型此段无承载站，新增不构成劫持，可用最高空档
    pn, _ = cp.suggest_priority(band, 100, models=["brand-new-xyz"])
    eq("无承载模型不受避让限制", pn >= p, True)

    # 空段
    empty = _band({}, {})
    pe, why_e = cp.suggest_priority(empty, 100)
    eq("空段取 100 基准", pe, 100)
    eq("空段说明", "为空" in why_e, True)

    # 无空档：贴最低档之下
    packed = _band({3: ["a"], 2: ["b"], 1: ["c"]}, {})
    pp, why_p = cp.suggest_priority(packed, 100)
    eq("无空档置于最低档之下", pp < 1 or pp == 1, True)
    eq("无空档有说明", "无可插空档" in why_p, True)

    section("build_plan 透传试用期开关")
    # 开关必须一路透到 suggest_priority。曾经断在这里：前端 ID 写成
    # o_byscore 而 HTML 里是 o_probation，读到 undefined 恒为 false，
    # 勾选框点了没反应（默认行为恰好正确，所以不容易发现）。
    bp_cfg = {
        "claude-api-key": [
            {"api-key": "old1", "base-url": "https://top.example.com",
             "priority": 1000, "models": [{"name": "opus-5", "alias": "opus-5"}]},
            {"api-key": "old2", "base-url": "https://mid.example.com",
             "priority": 500, "models": [{"name": "opus-5", "alias": "opus-5"}]},
            {"api-key": "old3", "base-url": "https://low.example.com",
             "priority": 40, "models": [{"name": "opus-5", "alias": "opus-5"}]},
        ]
    }
    bp_row = cp.parse_lines("https://brand-new.example.com,sk-probation-test-1")[0]         if isinstance(cp.parse_lines("https://brand-new.example.com,sk-x"), list)         else cp.parse_lines("https://brand-new.example.com,sk-probation-test-1").valid[0]

    bp_res = CandidateResult(row=bp_row)
    bp_v = SectionVerdict(section="claude-api-key", usable=True,
                          base_url="https://brand-new.example.com",
                          models=["opus-5"])
    bp_res.sections = {"claude-api-key": bp_v}

    plan_prob = cp.build_plan(bp_row, bp_res, bp_cfg)          # 默认试用期
    plan_score = cp.build_plan(bp_row, bp_res, bp_cfg, probation=False)
    pp = plan_prob.sections["claude-api-key"]
    ps = plan_score.sections["claude-api-key"]
    eq("默认走试用期", "试用期" in pp.priority_reason, True)
    eq("probation=False 走得分", "按得分" in ps.priority_reason, True)
    eq("试用期档位更低", pp.priority < ps.priority, True)
    eq("试用期挡站不多于按得分",
       len({h for i in pp.impacts for h in i.shadowed_hosts})
       <= len({h for i in ps.impacts for h in i.shadowed_hosts}), True)
    eq("两种模式都不劫持顶层",
       [i.model for i in pp.impacts + ps.impacts if i.hijacks], [])

    section("影响面 · 抢顶层")
    eq("越过顶层即劫持",
       cp.compute_impact(band, ["opus-5"], 1100)[0].hijacks, True)
    eq("低于顶层不劫持",
       cp.compute_impact(band, ["opus-5"], 130)[0].hijacks, False)
    eq("等于顶层是同层共享",
       cp.compute_impact(band, ["opus-5"], 1000)[0].shares, True)
    eq("无承载模型不产生 impact",
       cp.compute_impact(band, ["unknown-model"], 9999), [])

    section("影响面 · 挡下层")
    # 层级隔离下「插在中间」不是排序靠前，是把下面整层跳过。
    # 真实案例：gemini 段插 465 不动 relay-g 的 900，却把 30/20/15/10
    # 四档共 9 个站全挡在后面 —— 这是最容易漏看的影响面。
    shadow_band = _band(
        {900: ["relay-g"], 30: ["relay-c", "relay-h"], 20: ["relay-d"],
         10: ["relay-e"]},
        {"gemini-2.5-pro": 900, "gemini-3.6-flash": 30},
        {
            "gemini-2.5-pro": {900: ["relay-g"], 30: ["relay-c", "relay-h"],
                               20: ["relay-d"], 10: ["relay-e"]},
            "gemini-3.6-flash": {30: ["relay-c"]},
        },
    )
    imp = cp.compute_impact(shadow_band, ["gemini-2.5-pro"], 465)[0]
    eq("465 不劫持 900 顶层", imp.hijacks, False)
    eq("465 挡住下面全部 4 个站", sorted(imp.shadowed_hosts),
       ["relay-c", "relay-d", "relay-e", "relay-h"])
    eq("挡住的档位正确", sorted(imp.shadowed), [10, 20, 30])

    imp_low = cp.compute_impact(shadow_band, ["gemini-2.5-pro"], 25)[0]
    eq("25 只挡 20/10 两档", sorted(imp_low.shadowed_hosts),
       ["relay-d", "relay-e"])

    imp_top = cp.compute_impact(shadow_band, ["gemini-2.5-pro"], 950)[0]
    eq("950 既劫持也挡住全部", imp_top.hijacks, True)
    eq("950 连顶层站也挡住", "relay-g" in imp_top.shadowed_hosts, True)

    imp_bottom = cp.compute_impact(shadow_band, ["gemini-2.5-pro"], 5)[0]
    eq("垫底不挡任何站", imp_bottom.shadowed_hosts, [])

    # 按模型分别算：同一个 priority 对不同模型挡住的站不同
    imp_flash = cp.compute_impact(shadow_band, ["gemini-3.6-flash"], 465)[0]
    eq("flash 只有 30 档一个站被挡", imp_flash.shadowed_hosts, ["relay-c"])

    section("影响面 → 警告文案")
    sp_warn = cp.SectionPlan(section="gemini-api-key", base_url="https://x.com",
                             api_key="k", models=["gemini-2.5-pro"],
                             priority=465)
    sp_warn.impacts = cp.compute_impact(shadow_band, ["gemini-2.5-pro"], 465)
    shadow_hosts = {h for i in sp_warn.impacts if not i.hijacks
                    for h in i.shadowed_hosts}
    eq("警告能算出被挡站数", len(shadow_hosts), 4)

    section("空档内取值等价 · 更保守选项")
    # 关键性质：同一空档内取任何值，被挡站点完全相同。所以「手工调低」
    # 这种建议没有操作性 —— 必须给出下一个空档的确切数值。
    from cpa_probe.plan import _shadow_count, gentler_option
    same = {_shadow_count(shadow_band, ["gemini-2.5-pro"], v)
            for v in (35, 200, 465, 700, 890)}
    eq("(30,900) 空档内取值挡住数恒定", same, {4})

    alt = gentler_option(shadow_band, ["gemini-2.5-pro"], 465)
    eq("给出更保守选项", alt is not None, True)
    if alt:
        alt_pri, now_n, alt_n = alt
        eq("建议值更低", alt_pri < 465, True)
        eq("当前挡住数正确", now_n, 4)
        eq("建议值确实少挡", alt_n < now_n, True)
        eq("建议值实测与声明一致",
           _shadow_count(shadow_band, ["gemini-2.5-pro"], alt_pri), alt_n)

    # 已经垫底：无更保守选项可给
    eq("不挡任何站时不给建议",
       gentler_option(shadow_band, ["gemini-2.5-pro"], 5), None)
    eq("无模型时不给建议", gentler_option(shadow_band, [], 465), None)

    section("质量打分")
    def mkv(**kw):
        v = SectionVerdict(section="claude-api-key", usable=True,
                           models=kw.pop("models", ["m1", "m2"]))
        for k, val in kw.items():
            setattr(v, k, val)
        return v

    eq("不可用得 0", cp.score_verdict(SectionVerdict(section="x", usable=False)), 0)
    base = cp.score_verdict(mkv())
    eq("干净候选满分", base, 100)
    # 静默换模扣最重 —— 照常计费却返回另一个模型，比不可用更危险
    eq("换模扣分最重",
       cp.score_verdict(mkv(swap={"swap": 2, "same": 2, "rate_pct": 50.0})),
       base - 50)
    eq("需代理次之", cp.score_verdict(mkv(need_proxy=True)), base - 20)
    eq("需 UA 最轻",
       cp.score_verdict(mkv(min_headers={"User-Agent": "x"})), base - 5)
    eq("单模型扣分", cp.score_verdict(mkv(models=["only"])), base - 10)


# ==========================================================================
# 6. 请求构造
# ==========================================================================


def test_request() -> None:
    section("请求构造")
    from cpa_probe import request as rq

    url, hdr, body = rq.build_request("gemini-api-key", "https://g.com",
                                      "gemini-2.5-pro", "K1")
    eq("gemini 路径", url,
       "https://g.com/v1beta/models/gemini-2.5-pro:generateContent?key=K1")
    eq("gemini key 走 query 不走头", "Authorization" in hdr, False)
    eq("gemini body 形状", list(body), ["contents"])

    url, hdr, body = rq.build_request("codex-api-key", "https://c.com/v1",
                                      "gpt-5.6-sol", "K2")
    eq("codex 路径", url, "https://c.com/v1/responses")
    eq("codex Bearer", hdr["Authorization"], "Bearer K2")
    eq("codex body 用 input", "input" in body, True)

    # 传裸域名也要补出 /v1 —— 用户不必记哪个段要哪种形态
    url, _, _ = rq.build_request("codex-api-key", "https://c.com",
                                 "gpt-5.6-sol", "K2")
    eq("codex 自动补 /v1", url, "https://c.com/v1/responses")

    url, hdr, body = rq.build_request("claude-api-key", "https://a.com",
                                      "claude-opus-5", "K3")
    eq("claude 路径", url, "https://a.com/v1/messages")
    # 中转站实现不一：只发一种可能让通的站误判 401，所以两种都发
    eq("claude 同时发 Bearer", hdr["Authorization"], "Bearer K3")
    eq("claude 同时发 x-api-key", hdr["x-api-key"], "K3")
    eq("claude 带 anthropic-version", hdr["anthropic-version"], "2023-06-01")

    url, _, _ = rq.build_request("openai-compatibility", "https://o.com/v1",
                                 "gpt-5.6-sol", "K4")
    eq("compat 路径", url, "https://o.com/v1/chat/completions")

    _, _, b = rq.build_request("claude-api-key", "https://a.com", "m", "k",
                              text="hello")
    eq("自定义探测文本生效", b["messages"][0]["content"], "hello")
    # "hi" 会命中站方测活探针关键词，返回 400 反测活，与真实不可用混淆
    eq("默认探测文本非 hi", rq.PROBE_TEXT.startswith("Reply with one short"), True)

    _, hdr, _ = rq.build_request("codex-api-key", "https://c.com/v1", "m", "k",
                                 extra_headers={"Originator": "codex_vscode"})
    eq("额外头被合入", hdr["Originator"], "codex_vscode")

    section("标识头回退序列")
    combos = rq.identity_combos("codex-api-key")
    names = [n for n, _ in combos]
    # Originator 不含版本号，不受客户端升级影响 —— 这是用户提的
    # 「表头随版本升级变化」的解法：优先选最耐用的那个
    eq("originator-only 排在 ua-only 之前",
       names.index("originator-only") < names.index("ua-only-codex"), True)
    eq("首个是 cpa 现状基线", names[0], "cpa-现状")

    section("列模型端点")
    u, _ = rq.models_endpoint("gemini-api-key", "https://g.com", "K")
    eq("gemini 列模型", u, "https://g.com/v1beta/models?key=K")
    u, _ = rq.models_endpoint("codex-api-key", "https://c.com/v1", "K")
    eq("codex 列模型", u, "https://c.com/v1/models")
    u, _ = rq.models_endpoint("claude-api-key", "https://a.com", "K")
    eq("claude 列模型", u, "https://a.com/v1/models")

    section("模型清单解析")
    eq("OpenAI 形态",
       rq.parse_models_response("codex-api-key",
                                '{"data":[{"id":"gpt-5.6-sol"},{"id":"gpt-4"}]}'),
       ["gpt-4", "gpt-5.6-sol"])
    eq("gemini 的 models/ 前缀被剥",
       rq.parse_models_response("gemini-api-key",
                                '{"models":[{"name":"models/gemini-2.5-pro"}]}'),
       ["gemini-2.5-pro"])
    eq("坏 JSON 退化正则",
       "claude-opus-5" in rq.parse_models_response(
           "claude-api-key", 'junk "id": "claude-opus-5" junk'), True)

    section("模型白名单")
    from cpa_probe.pipeline import model_allowed
    # 用户定的规则：只保留 gemini / gpt / claude 三类
    for m in ("gemini-2.5-pro", "gpt-5.6-sol", "claude-opus-5",
              "Business/gemini-flash"):
        eq(f"保留 {m}", model_allowed(m), True)
    for m in ("BAAI/bge-large", "DeepSeek-V3", "GLM-4", "42-mini", "grok-4"):
        eq(f"剔除 {m}", model_allowed(m), False)


# ==========================================================================
# 7. 真实 config.yaml（可选）
# ==========================================================================


def test_real_config(path: str) -> None:
    section(f"真实 config.yaml · {os.path.basename(path)}")
    import yaml

    raw = io.open(path, encoding="utf-8").read()
    cfg = yaml.safe_load(raw)
    lines = raw.split("\n")
    print(f"      {len(lines)} 行 · {len(raw.encode()) // 1024} KB")

    # 段定位与缩进探测：不猜，从现有条目读
    for sec in cp.SECTIONS:
        span = _section_span(lines, sec)
        eq(f"{sec} 能定位", span is not None, True)
        if not span:
            continue
        st, en = span
        dash, _field = _detect_indent(lines, st, en)
        n = sum(1 for i in range(st + 1, en) if lines[i].startswith(dash + "- "))
        eq(f"{sec} 条目数与 YAML 一致", n, len(cfg.get(sec) or []))

    fps = cp.existing_fingerprints(cfg)
    for sec in cp.SECTIONS:
        eq(f"{sec} 指纹已提取", len(fps[sec]) > 0, True)

    bands = {s: cp.build_band(cfg, s) for s in cp.SECTIONS}
    for sec in cp.SECTIONS:
        b = bands[sec]
        print(f"      {sec:<22} {len(b.tiers):>2} 档 · 顶 {b.top:>4} · "
              f"{len(b.gaps())} 空档")
        eq(f"{sec} 有档位", len(b.tiers) > 0, True)

    # 端到端：造方案 → diff → 应用 → 校验
    row = cp.parse_lines(
        "https://regress-test.example.com,sk-regress1234567890").valid[0]
    res = CandidateResult(row=row)

    def mk(sec, models, **kw):
        v = SectionVerdict(section=sec, usable=True,
                           base_url=row.base_for(sec), models=models)
        for k, val in kw.items():
            setattr(v, k, val)
        return v

    res.sections = {
        "gemini-api-key": SectionVerdict(section="gemini-api-key", usable=False,
                                         category="死路",
                                         action="分组无该模型渠道"),
        "codex-api-key": mk("codex-api-key", ["gpt-5.6-sol"], need_proxy=True,
                            min_headers={"Originator": "codex_vscode"}),
        "claude-api-key": mk("claude-api-key", ["claude-opus-5"],
                             max_context_length=928106,
                             context_model="claude-opus-5"),
        "openai-compatibility": mk("openai-compatibility", ["gpt-5.6-sol"]),
    }

    plan = cp.build_plan(row, res, cfg, bands=bands)
    eq("不可用段被跳过", "gemini-api-key" in plan.skipped, True)
    eq("可写段 3 个", len([1 for p in plan.sections.values() if p.writable]), 3)
    eq("无劫持顶层警告",
       [w for p in plan.sections.values() for w in p.warnings if "抢走" in w], [])

    diffs = build_diffs(raw, plan and [plan])
    eq("生成 3 处插入", len(diffs), 3)
    out = apply_diffs(raw, diffs)
    ok, msg = validate(out)
    eq("合并后 YAML 校验通过", ok, True)
    print(f"      {msg}")

    new = yaml.safe_load(out)
    for sec, delta in (("claude-api-key", 1), ("codex-api-key", 1),
                       ("openai-compatibility", 1), ("gemini-api-key", 0)):
        eq(f"{sec} 条目 +{delta}",
           len(new[sec]) - len(cfg[sec]), delta)

    # 只追加，绝不改动现有行
    for sec in cp.SECTIONS:
        old = [e.get("priority") for e in cfg[sec]]
        eq(f"{sec} 现有 priority 未被改动",
           [e.get("priority") for e in new[sec]][:len(old)], old)

    n_old = sum(1 for l in raw.split("\n") if l.strip().startswith("#"))
    n_new = sum(1 for l in out.split("\n") if l.strip().startswith("#"))
    eq("整行注释一条未丢", n_new, n_old)

    # 新条目字段正确性
    ce = [e for e in new["claude-api-key"]
          if e.get("base-url") == "https://regress-test.example.com"][0]
    eq("claude base 不带 /v1", ce["base-url"],
       "https://regress-test.example.com")
    eq("max-context-length 落到实测的那个模型上",
       ce["models"][0].get("max-context-length"), 928106)
    # 只有实测过的模型带这个字段。同站不同模型窗口能差一个数量级，
    # 外推等于伪造数据 —— 客户端会按错的窗口定压缩点。
    eq("未实测的模型不带 max-context-length",
       [m.get("name") for m in ce["models"][1:]
        if "max-context-length" in m], [])
    xe = [e for e in new["codex-api-key"]
          if e.get("base-url") == "https://regress-test.example.com/v1"][0]
    eq("codex base 带 /v1", xe["base-url"],
       "https://regress-test.example.com/v1")
    eq("proxy-url 写入", xe["proxy-url"], "http://mihomo:7890")
    eq("headers 写入", xe["headers"], {"Originator": "codex_vscode"})
    oe = [e for e in new["openai-compatibility"]
          if e.get("base-url") == "https://regress-test.example.com/v1"][0]
    # compat 段结构与其他三段不同：key 在 api-key-entries 里
    eq("compat 用 api-key-entries",
       isinstance(oe.get("api-key-entries"), list), True)
    eq("compat key 在 entries 内",
       oe["api-key-entries"][0]["api-key"], "sk-regress1234567890")

    section("批内去重")
    seen = cp.existing_fingerprints(cfg)
    p1 = cp.build_plan(row, res, cfg, bands=bands, seen=seen)
    p2 = cp.build_plan(row, res, cfg, bands=bands, seen=seen)
    eq("首次可写", p1.sections["claude-api-key"].writable, True)
    eq("同批重复被判重", p2.sections["claude-api-key"].duplicate, True)
    eq("重复项不可写", p2.sections["claude-api-key"].writable, False)
    # gemini 段是静默丢弃，其余三段是注册成两个 —— 说明必须不同
    eq("claude 段说明提到不去重",
       "不去重" in p2.sections["claude-api-key"].duplicate_note, True)

    section("compat 段按主机归并 · 同站多 Key 只出一个 provider")
    # 实测缺陷（2026-08-30 首次真实探测发现）：5 个 relay-i.example 的 Key 生成了
    # 5 个重名 compat provider，每个只带 1 个 Key。而现有 12 个 provider 全部
    # 是「一站一条、多 Key 挂 api-key-entries」（relay-f 15 个、relay-l 15 个）。
    # CPA 的 compat 段不去重，重名会让同一站注册成 N 个 provider、模型清单重复 N 遍。
    import collections as _c

    multi_keys = [f"sk-multikey-{i:04d}-aaaabbbbcccc" for i in range(5)]
    multi_rows = cp.parse_lines(
        "\n".join(f"https://multikey-test.example.com,{k}" for k in multi_keys)).valid
    eq("造出 5 个同主机 Key", len(multi_rows), 5)
    eq("确实同一主机", len({r.host for r in multi_rows}), 1)

    m_bands, m_seen, m_plans = {}, cp.existing_fingerprints(cfg), []
    for mrow in multi_rows:
        mres = CandidateResult(row=mrow)
        mres.sections = {
            "gemini-api-key": SectionVerdict(section="gemini-api-key", usable=False,
                                             category="死路", action="分组无该模型渠道"),
            "codex-api-key": SectionVerdict(section="codex-api-key", usable=False,
                                            category="边缘", action="403 且正文为空"),
            "claude-api-key": SectionVerdict(
                section="claude-api-key", usable=True,
                base_url=mrow.base_for("claude-api-key"),
                models=["claude-opus-5", "claude-opus-4-8"]),
            "openai-compatibility": SectionVerdict(
                section="openai-compatibility", usable=True,
                base_url=mrow.base_for("openai-compatibility"),
                models=["claude-opus-5", "claude-opus-4-8"],
                min_headers={"User-Agent": "cli-proxy-openai-compat"}),
        }
        m_plans.append(cp.build_plan(mrow, mres, cfg, bands=m_bands, seen=m_seen))

    m_diffs = build_diffs(raw, m_plans)
    per_sec = _c.Counter(d.section for d in m_diffs)
    eq("claude 段 5 处（每 Key 一条）", per_sec["claude-api-key"], 5)
    eq("compat 段只 1 处（归并）", per_sec["openai-compatibility"], 1)

    m_out = apply_diffs(raw, m_diffs)
    m_ok, _m_msg = validate(m_out)
    eq("归并后 YAML 校验通过", m_ok, True)
    m_new = yaml.safe_load(m_out)

    kk = [e for e in m_new["openai-compatibility"]
          if isinstance(e, dict) and "multikey-test" in str(e.get("base-url", ""))]
    eq("compat 只有一个 provider 条目", len(kk), 1)
    eq("5 个 Key 全在 api-key-entries 里",
       len(kk[0].get("api-key-entries") or []), 5)
    eq("entries 里的 Key 与输入一致",
       [x["api-key"] for x in kk[0]["api-key-entries"]], multi_keys)
    eq("模型清单没重复", len(kk[0].get("models") or []), 2)
    eq("headers 只写一次", kk[0].get("headers"),
       {"User-Agent": "cli-proxy-openai-compat"})

    names = [e.get("name") for e in m_new["openai-compatibility"] if isinstance(e, dict)]
    dups = [k for k, v in _c.Counter(names).items() if v > 1]
    eq("compat 段无重名 provider", dups, [])

    ck = [e for e in m_new["claude-api-key"]
          if isinstance(e, dict) and "multikey-test" in str(e.get("base-url", ""))]
    eq("claude 段仍是每 Key 一条", len(ck), 5)

    eq("原文件未被写", io.open(path, encoding="utf-8").read(), raw)


# ==========================================================================



# ==========================================================================
# 8. 写回的降级路径 —— 容器单文件挂载时 os.replace 会失败
# ==========================================================================


def test_writeback_fallback() -> None:
    import shutil
    import tempfile
    from unittest import mock
    from cpa_probe import writeback

    section("写回 · 单文件挂载降级")

    d = tempfile.mkdtemp(prefix="wbfb-")
    try:
        cfg = os.path.join(d, "config.yaml")
        io.open(cfg, "w", encoding="utf-8").write("api-keys:\n  - k1\n")
        bdir = os.path.join(d, "backups")

        # 正常路径：os.replace 可用
        bak1 = writeback.write_local(cfg, "api-keys:\n  - k2\n")
        eq("正常写入生效", "k2" in io.open(cfg, encoding="utf-8").read(), True)
        eq("备份落在同目录", os.path.isfile(bak1), True)

        # 降级路径：模拟单文件 bind mount —— replace 抛 OSError
        # 容器里 config.yaml 本身是挂载点，rename 到它会 EBUSY/EXDEV
        with mock.patch("os.replace", side_effect=OSError(16, "Device or resource busy")):
            bak2 = writeback.write_local(cfg, "api-keys:\n  - k3\n",
                                         backup_dir=bdir)
        eq("降级仍写入成功", "k3" in io.open(cfg, encoding="utf-8").read(), True)
        eq("备份落到独立目录", os.path.dirname(bak2), bdir)
        eq("降级前已备份旧内容",
           "k2" in io.open(bak2, encoding="utf-8").read(), True)
        eq("无 .tmp 残留",
           [f for f in os.listdir(d) if f.endswith(".tmp")], [])
    finally:
        shutil.rmtree(d, ignore_errors=True)

def main() -> int:
    print("=" * 66)
    print("cpa_probe 回归测试（零网络请求）")
    print("=" * 66)

    test_parse()
    test_classify()
    test_fingerprint()
    test_dedup()
    test_writeback_fallback()
    test_priority()
    test_request()

    # 自带样本兜底：test_real_config 验的是「段定位 / 缩进探测 / 指纹提取在
    # 真实形状上成立」，那些性质与是不是真实凭据无关。原来不传路径就整段跳过，
    # 于是本机与 CI 的项数不同，而「跳过」和「通过」在汇总行里长得一样 ——
    # 2026-08-30 的 bcrypt 分支就是这么漏了两个月的。
    cfg_path, _synth, _fx_tmp = fixture_cfg.resolve(sys.argv, label="真实形状")
    try:
        import yaml  # noqa: F401
        test_real_config(cfg_path)
    except ImportError:
        print("\n未安装 PyYAML，跳过真实文件用例")
    finally:
        if _fx_tmp:
            import shutil
            shutil.rmtree(_fx_tmp, ignore_errors=True)

    print("\n" + "=" * 66)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for f in FAILED:
            print(f"\n  ✗ {f}")
        return 1
    print(f"全部通过 · {PASSED} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
