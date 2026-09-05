#!/usr/bin/env python3
"""cpa_probe 回归测试。零网络请求，可随时复跑。

    cd /opt/deploy/upstream-importer
    python3 tests/test_probe.py                    # 纯逻辑，不需要 config.yaml
    python3 tests/test_probe.py /opt/deploy/config.yaml   # 加上真实文件的用例

不用 pytest —— VPS 上不想装依赖。失败即非零退出，可直接进 CI。

每条断言背后都是一次实测教训，注释里写清是哪条。改判定规则前先跑这个。
"""

from __future__ import annotations

import copy
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


def truthy(name: str, got, hint: str = "") -> None:
    global PASSED
    if got:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED.append(f"{name}\n      实得 {got!r} {hint}")


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
        # 「访问已被拦截/安全验证」是站方自建拦截页（WAF 按形态拦），
        # 不是 CF 边缘拦截。2026-09-01 实测 hotel 带代理仍被拦 ——
        # 若判成「IP封」，处置会写「加代理」，那条路已证伪。
        ("403", "访问已被拦截，请完成安全验证", "WAF"),
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
    # 白名单规则是「只留 gemini / gpt / claude / kimi 四类」。o1 / o3-mini 属于
    # 「gpt 那一类」，只是 OpenAI 换了命名 —— 2026-08-31 实测被前缀匹配漏掉。
    # kimi 是 2026-09-02 加的：用户把它列进 compat 段的允许清单，CPA 的权威
    # 名录里也确实有 kimi provider。它只在 compat 段有意义，前三段由段族闸拦。
    from cpa_probe.pipeline import model_allowed as _ma
    for m in ("o1", "o1-mini", "o3", "o3-mini", "o4-mini", "o1-2024-12-17",
              "gpt-4o", "claude-opus-5", "gemini-2.5-pro",
              "Business/gemini-2.5-pro", "anthropic/claude-fable-5",
              "kimi-k2", "kimi-k3", "kimi-k2.7-code"):
        eq(f"放行 {m}", _ma(m), True)
    # 不能因为放宽 o 系列就误收这些
    for m in ("openai-whisper", "omni-moderation", "o", "ollama-llama3",
              "order-model", "deepseek-chat", "grok-4", "qwen-max",
              "glm-4", "llama-3"):
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
        {1000: ["hotel"], 950: ["alfa"], 900: ["foxtrot"],
         800: ["relay-l"], 120: ["relay-m"], 30: ["relay-j"]},
        {"opus-5": 1000},
        {"opus-5": {1000: ["hotel"], 950: ["alfa"], 900: ["foxtrot"],
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
    # 真实案例：gemini 段插 465 不动 golf 的 900，却把 30/20/15/10
    # 四档共 9 个站全挡在后面 —— 这是最容易漏看的影响面。
    shadow_band = _band(
        {900: ["golf"], 30: ["cielo", "hotel"], 20: ["relay-d"],
         10: ["relay-e"]},
        {"gemini-2.5-pro": 900, "gemini-3.6-flash": 30},
        {
            "gemini-2.5-pro": {900: ["golf"], 30: ["cielo", "hotel"],
                               20: ["relay-d"], 10: ["relay-e"]},
            "gemini-3.6-flash": {30: ["cielo"]},
        },
    )
    imp = cp.compute_impact(shadow_band, ["gemini-2.5-pro"], 465)[0]
    eq("465 不劫持 900 顶层", imp.hijacks, False)
    eq("465 挡住下面全部 4 个站", sorted(imp.shadowed_hosts),
       sorted(["cielo", "relay-d", "relay-e", "hotel"]))
    eq("挡住的档位正确", sorted(imp.shadowed), [10, 20, 30])

    imp_low = cp.compute_impact(shadow_band, ["gemini-2.5-pro"], 25)[0]
    eq("25 只挡 20/10 两档", sorted(imp_low.shadowed_hosts),
       ["relay-d", "relay-e"])

    imp_top = cp.compute_impact(shadow_band, ["gemini-2.5-pro"], 950)[0]
    eq("950 既劫持也挡住全部", imp_top.hijacks, True)
    eq("950 连顶层站也挡住", "golf" in imp_top.shadowed_hosts, True)

    imp_bottom = cp.compute_impact(shadow_band, ["gemini-2.5-pro"], 5)[0]
    eq("垫底不挡任何站", imp_bottom.shadowed_hosts, [])

    # 按模型分别算：同一个 priority 对不同模型挡住的站不同
    imp_flash = cp.compute_impact(shadow_band, ["gemini-3.6-flash"], 465)[0]
    eq("flash 只有 30 档一个站被挡", imp_flash.shadowed_hosts, ["cielo"])

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
       "https://g.com/v1beta/models/gemini-2.5-pro:generateContent")
    eq("gemini key 走 x-goog-api-key 头", hdr.get("x-goog-api-key"), "K1")
    eq("gemini 不走 Authorization", "Authorization" in hdr, False)
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
    eq("claude 路径", url, "https://a.com/v1/messages?beta=true")
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
    # 新梯子已不叫 ua-only-codex（那是旧 identity_combos 的名字），且
    # originator-only 本身就已经在 browser-ua 之前了（嵌套超集），这条
    # 改为验梯子首档是 baseline。
    eq("首档是 baseline", names[0], "baseline")
    eq("originator-only 排在 browser-ua 之前",
       names.index("originator-only") < names.index("browser-ua"), True)

    section("列模型端点")
    u, h = rq.models_endpoint("gemini-api-key", "https://g.com", "K")
    eq("gemini 列模型", u, "https://g.com/v1beta/models")
    eq("gemini 列模型头里带 Key", h.get("x-goog-api-key"), "K")
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
    # 用户定的规则：保留 gemini / gpt / claude / kimi 四类
    for m in ("gemini-2.5-pro", "gpt-5.6-sol", "claude-opus-5",
              "Business/gemini-flash", "kimi-k3"):
        eq(f"保留 {m}", model_allowed(m), True)
    for m in ("BAAI/bge-large", "DeepSeek-V3", "GLM-4", "42-mini", "grok-4"):
        eq(f"剔除 {m}", model_allowed(m), False)

    # ── 段级模型规则（用户 2026-09-02 定，实现在 model_catalog）──
    #
    # 现场两张截图：codex 段勾上了 gpt-4o / gpt-image-2 / gpt-oss-120b /
    # gpt-oss-20b（都是 gpt 族，旧的段族闸放行），gemini 段目录里列出
    # flash / batch-inference / pro-agent。规则散在三处（model_allowed、
    # model_fits_section、web/app.js）且互不一致是根因。
    section("段级模型规则：四条硬规则")
    from cpa_probe import model_catalog as _mc

    # codex 只收 gpt 系
    for m in ("gpt-5.6-sol", "gpt-5.6", "gpt-4o", "o3-mini"):
        eq(f"codex 收 {m}", _mc.section_allows("codex-api-key", m), True)
    for m in ("claude-opus-5", "gemini-3.1-pro", "kimi-k3", "deepseek-v4f"):
        eq(f"codex 拒 {m}", _mc.section_allows("codex-api-key", m), False)

    # claude 只收 claude 系
    for m in ("claude-opus-5", "claude-fable-5", "claude-sonnet-5"):
        eq(f"claude 收 {m}", _mc.section_allows("claude-api-key", m), True)
    eq("claude 拒 gpt-5.6", _mc.section_allows("claude-api-key", "gpt-5.6"), False)

    # gemini 只收 *-pro 且版本 >= 2.5
    for m in ("gemini-3.1-pro", "gemini-3.1-pro-high", "gemini-3.1-pro-low",
              "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-search",
              "gemini-3.1-pro-preview-customtools", "gemini-2.5-pro"):
        eq(f"gemini 收 {m}", _mc.section_allows("gemini-api-key", m), True)
    for m in ("gemini-2.0-pro",            # 版本低于 2.5
              "gemini-3.5-flash",          # flash 不是 pro
              "gemini-3-pro-image-preview",  # 图像模型
              "gemini-batch-inference",    # 批处理端点
              "gemini-pro-agent"):         # 没有版本号，不符合 gemini-<版本>-pro
        eq(f"gemini 拒 {m}", _mc.section_allows("gemini-api-key", m), False)

    # compat 收四族，拒其余
    for m in ("gpt-5.6-sol", "claude-opus-5", "gemini-3.1-pro", "kimi-k3",
              "Business/gemini-2.5-pro"):
        eq(f"compat 收 {m}", _mc.section_allows("openai-compatibility", m), True)
    for m in ("deepseek-v4f", "glm-5.2", "grok-4.6", "x-ai/grok-4.6",
              "opus-5"):                   # 没有 claude 前缀，认不出族
        eq(f"compat 拒 {m}", _mc.section_allows("openai-compatibility", m), False)

    # 非对话模型一律不收（图像 / 语音 / 嵌入 / 开源小模型 / 批处理）
    for sec in ("codex-api-key", "openai-compatibility"):
        for m in ("gpt-image-2", "gpt-oss-120b", "gpt-oss-20b"):
            eq(f"{sec} 拒非对话 {m}", _mc.section_allows(sec, m), False)

    section("每条产品线取最高世代（旧世代不放入）")
    # 2026-09-02 从「同系列取最新」改成「产品线取最高世代」。
    #
    # 为什么改（现场截图）：按系列分组时 `gpt-5.5` 的系列是 `gpt-*`，而
    # luna / terra 各自是 `gpt-*-luna` / `gpt-*-terra` —— 三个独立系列，
    # 5.5 没有对手所以留下；`gpt-4o` 更直接：旧正则不认 `4o` 是版本，
    # 它自成一系永远保留。两件事叠加就是 codex 段勾着 gpt-4o 与 gpt-5.5。
    ngl = _mc.newest_generation_per_line
    eq("现场截图那组：4o / 5.1 / 5.5 全被 5.6 挤掉",
       ngl(["gpt-4o", "gpt-5.1", "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra"]),
       ["gpt-5.6-luna", "gpt-5.6-terra"])
    eq("gpt-5.7 挤掉 gpt-5.6", ngl(["gpt-5.6-sol", "gpt-5.7-sol"]),
       ["gpt-5.7-sol"])
    eq("opus-5 挤掉 opus-4-8",
       ngl(["claude-opus-4-8", "claude-opus-5"]), ["claude-opus-5"])
    eq("kimi-k3 挤掉 kimi-k2", ngl(["kimi-k2", "kimi-k3"]), ["kimi-k3"])
    eq("同世代的所有变体都保留",
       ngl(["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6"]),
       ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6"])
    # 产品线不同就各自留 —— opus / sonnet / fable 是三条线
    eq("不同产品线互不淘汰",
       ngl(["claude-opus-5", "claude-sonnet-5", "claude-fable-5"]),
       ["claude-opus-5", "claude-sonnet-5", "claude-fable-5"])
    # 日期戳只是同一世代的另一种写法，不该让它挤掉不带戳的那个
    eq("日期戳不构成更高世代",
       ngl(["claude-haiku-4-5", "claude-haiku-4-5-20251001"]),
       ["claude-haiku-4-5", "claude-haiku-4-5-20251001"])
    # 规格后缀（32k / nano / codex）不该自成产品线从而躲过世代过滤
    eq("规格后缀不自成产品线",
       ngl(["gpt-4-32k", "gpt-5.4-nano", "gpt-5.6"]), ["gpt-5.6"])
    eq("codex 是同一条线上的变体",
       ngl(["gpt-5-codex", "gpt-5.3-codex"]), ["gpt-5.3-codex"])
    # 整组都认不出版本 —— 全留，无从比较不淘汰。
    # 用真正没有版本记号的名字：o1 / o3-mini 现在解析得出世代（见下面那组）。
    eq("无版本的整组保留",
       ngl(["gpt-oss:120b", "gpt-oss:20b"]), ["gpt-oss:120b", "gpt-oss:20b"])
    # o 系列（2026-09-04 现场截图：codex 段同时勾着 o1 与 o3）。
    # 那一族的世代数字紧贴开头的 o，_VERSION_RE 读不出来，于是七个名字
    # 「整组认不出版本」被兜底全留、默认全勾 —— 与 gpt-4o 那次同一个形态。
    eq("o3 挤掉 o1", ngl(["o1", "o3-mini"]), ["o3-mini"])
    eq("o 系列同线取最高世代",
       ngl(["o1", "o1-pro", "o3", "o3-mini", "o3-pro", "o4-mini",
            "o4-mini-high"]),
       ["o3-pro", "o4-mini", "o4-mini-high"])
    eq("o 系列的世代来自紧跟 o 的数字",
       (_mc.series_and_version("o3-mini"), _mc._product_line("o3-mini")),
       (("o*-mini", (3,)), "o"))
    # 以 o 开头但 o 不是版本记号的名字不受影响 —— 锚是「o 后紧跟数字」
    eq("omni-3 不当成 o 系列",
       (_mc.series_and_version("omni-3")[0], _mc._product_line("omni-3")),
       ("omni-*", "omni"))
    eq("oss-20b 不当成 o 系列", _mc.series_and_version("oss-20b"),
       ("oss-20b", None))
    # o 系列与 gpt 系列是**互不相干的编号体系**，两条线各自取最高世代
    eq("o 与 gpt 各成一线", ngl(["o1", "o3", "gpt-5.6"]), ["o3", "gpt-5.6"])
    # 4o 现在被识别为世代 (4, 0)，与 5.6 同线可比
    eq("gpt-4o 被 gpt-5.6 挤掉", ngl(["gpt-4o", "gpt-5.6"]), ["gpt-5.6"])
    eq("产品线拆分：4o 与 5.6 同线",
       (_mc._product_line("gpt-4o"), _mc._product_line("gpt-5.6")),
       ("gpt", "gpt"))
    # 幂等：同一批输入两次结果一致（diff 要可复核）
    _batch = ["gpt-5.6", "gpt-4o", "claude-opus-5", "kimi-k3", "gpt-5.5"]
    eq("幂等", ngl(_batch), ngl(_batch))

    section("同系列取最新版（保留给手填等窄场景）")
    eq("同版本时裸名优先于带前缀的",
       _mc.newest_per_series(["anthropic/claude-opus-5", "claude-opus-5"]),
       ["claude-opus-5"])


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
    # 判死段现在**进方案**（有种子模型兜底、六项参数算全），只是不建议写。
    # 断言从「被跳过」改成「不建议写」—— 那才是这条用例真正要保的：
    # 判死的段不会在用户没勾的情况下落进 config.yaml。
    eq("不可用段进了方案", "gemini-api-key" in plan.sections, True)
    eq("不可用段可勾选", plan.sections["gemini-api-key"].writable, True)
    eq("不可用段不建议写", plan.sections["gemini-api-key"].recommended, False)
    eq("建议写的段 3 个",
       len([1 for p in plan.sections.values() if p.recommended]), 3)
    eq("无劫持顶层警告",
       [w for p in plan.sections.values() for w in p.warnings if "抢走" in w], [])

    # build_diffs 按 writable 筛，而判死段现在也 writable —— 生产路径靠
    # /api/plan 先剪一遍（selected=None 退到 recommended）。这里模拟那一步。
    only_rec = copy.copy(plan)
    only_rec.sections = {k: v for k, v in plan.sections.items() if v.recommended}
    diffs = build_diffs(raw, [only_rec])
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
    # 是「一站一条、多 Key 挂 api-key-entries」（foxtrot 15 个、relay-l 15 个）。
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


def test_client_send_never_raises():
    """`client.send` 必须兑现「任何异常都转成 Response」。

    2026-09-05 修的 P0。Python 语义：except 块**内部**抛出的异常不受同一 try
    的其余 handler 保护。原来 `raw = e.read(READ_LIMIT)` 就写在 HTTPError 的
    handler 里，于是下面 `socket.timeout` 与兜底 `Exception` 都接不到它。

    实测触发形态（Cloudflare 拦截页、nginx 慢响应都是这样）：
    `403 + Content-Length: 5000` 但只写 2 字节后挂住 → TimeoutError 穿出 send()。

    后果分两条路：
      · 并行（workers>1）—— pipeline 兜住，把这个**只是回应慢的活站**写成
        「死路 · 探测异常」（usable=False + should_downrank=True）并建议降权
      · 串行与 run_job 的 `f.result()` —— 整个 job 报错，一批凭据全丢
    """
    import socket as _socket
    import threading as _threading
    import time as _time

    from cpa_probe import client as _client

    srv = _socket.socket()
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    stop = _threading.Event()

    def serve():
        try:
            c, _a = srv.accept()
            c.recv(4096)
            # 声明 5000 字节，只写 2 字节后挂住
            c.sendall(b"HTTP/1.1 403 Forbidden\r\n"
                      b"Content-Length: 5000\r\n\r\nAB")
            stop.wait(10)
            c.close()
        except Exception:                       # noqa: BLE001
            pass

    _threading.Thread(target=serve, daemon=True).start()
    t0 = _time.monotonic()
    try:
        r = _client.send(f"http://127.0.0.1:{port}/x", headers={}, body=b"",
                         method="GET", timeout=2)
    except Exception as e:                      # noqa: BLE001
        stop.set()
        srv.close()
        raise AssertionError(
            f"client.send 抛异常了（{type(e).__name__}）—— 它的 docstring "
            f"承诺不抛，上游两条路径都按这个不变式写") from e
    stop.set()
    srv.close()

    eq("正文读不全也要保住状态码", r.status, "403")
    truthy("读取失败在 error 里说清", "正文读取失败" in (r.error or ""),
           f"实得 {r.error!r}")
    truthy("耗时接近 timeout 而不是挂死",
           _time.monotonic() - t0 < 6, "超时没生效")


def test_compressed_body_is_decoded():
    """压缩过的正文必须解开 —— 否则整条正文判定链失效。

    2026-09-05 修的 P1。画像梯的 cc-full / cc-body-* / compat cc-full 几档发
    `accept-encoding: gzip, deflate, br, zstd`（抄 CPA 的形态）。站方照办后
    body 是二进制，`decode(errors="replace")` 变成一串 U+FFFD，而**整条判定链
    都读文本**：

        classify 判「可用」、has_error_envelope=False、resp_model=None
        → model_matches 放行 → _accept 收下这个模型
        betas.wanted / _limit_from_body / input_tokens / 余额 / 限频 / 时段
        → 关键词一个都匹配不上

    也就是「死站带模型进 config.yaml」那个假阳性，只是改由压缩触发。

    判据：CPA 发同一套值**并且**解码（`decodeResponseBody`，注释说它同时处理
    「头声明」与「magic byte 探测」两种）。探测原来只抄了前一半。

    按 magic byte 而不是响应头判：实测有中转站压缩了却不声明。
    """
    import gzip as _gzip
    import json as _json
    import zlib as _zlib

    from cpa_probe.client import _decode_body as D

    body = _json.dumps({"type": "error", "error": {"message": "余额不足"}},
                       ensure_ascii=False)
    co = _zlib.compressobj(wbits=-_zlib.MAX_WBITS)
    raw_deflate = co.compress(body.encode()) + co.flush()

    for why, raw in (("未压缩", body.encode()),
                     ("gzip", _gzip.compress(body.encode())),
                     ("zlib(deflate)", _zlib.compress(body.encode())),
                     ("raw deflate", raw_deflate)):
        got = D(raw)
        truthy(f"{why} 能解出正文关键词", "余额不足" in got,
               f"实得 {got[:60]!r} —— 整条正文判定链会失效")

    # 解不了的压缩要**明说**，不能给一串替换字符：那会让判定链无声失效
    zstd = D(b"\x28\xb5\x2f\xfd" + b"\x00" * 40)
    truthy("zstd 明说本工具不解码",
           "不解码" in zstd and "zstd" in zstd, f"实得 {zstd[:60]!r}")
    br_like = D(bytes(range(0x80, 0x100)) * 2)
    truthy("无法解码时明说而不是给 U+FFFD",
           "无法解码" in br_like, f"实得 {br_like[:60]!r}")
    truthy("说明里不含替换字符", "\ufffd" not in br_like)

    # 回归：普通文本与空正文
    eq("空正文", D(b""), "")
    truthy("普通中文 JSON 原样", "你好" in D('{"a":"你好"}'.encode()))

    # `client.send` 必须**真的调**它 —— 只有 _decode_body 写对、调用点还是
    # `raw.decode(...)` 的话，上面全部断言仍然过（2026-09-05 撤销实验发现）。
    # 起个假上游回 gzip 正文，看 Response.body 是不是解开的。
    import gzip as _gz
    import socket as _sk
    import threading as _th

    from cpa_probe import client as _cl

    payload = _gz.compress(body.encode())
    srv2 = _sk.socket()
    srv2.setsockopt(_sk.SOL_SOCKET, _sk.SO_REUSEADDR, 1)
    srv2.bind(("127.0.0.1", 0))
    port2 = srv2.getsockname()[1]
    srv2.listen(1)

    def serve2():
        try:
            c, _a = srv2.accept()
            c.recv(4096)
            head = ("HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
                    f"Content-Length: {len(payload)}\r\n\r\n").encode()
            c.sendall(head + payload)
            c.close()
        except Exception:                       # noqa: BLE001
            pass

    _th.Thread(target=serve2, daemon=True).start()
    r2 = _cl.send(f"http://127.0.0.1:{port2}/x", headers={}, body=b"",
                  method="GET", timeout=5)
    srv2.close()
    eq("gzip 响应的状态码", r2.status, "200")
    truthy("client.send 真的解了压",
           "余额不足" in r2.body,
           f"实得 {r2.body[:60]!r} —— 调用点还是 raw.decode，"
           f"整条正文判定链失效")


def test_gemini_input_tokens_recognised():
    """gemini 段的 token 字段名必须认 —— 否则截断校验完全失效。

    2026-09-05 修的 P1。原来只认 `input_tokens`（Anthropic / Codex）与
    `prompt_tokens`（OpenAI / compat）。gemini 用
    `usageMetadata.promptTokenCount` —— 于是 gemini 段 `input_tokens()` 恒为
    None，而截断校验是

        tok is None → check() 直接返回 (True, None) → _bisect 第一发 hi 就通过

    结果 `max-context-length: 1100000` 被**凭空**写进 gemini 段：
    `_MIN_TRUSTED_CONTEXT` 与「relay-m 发 105 万字符只回 13 万 tokens」那条
    实测教训，在 gemini 段一次都不会触发。静默截断的站拿到假的百万窗口，
    客户端据此定压缩点。同一处也让 swap 的 token_span_anomaly 恒为假。

    判据：CPA 按段取字段 —— `helps/usage_helpers.go:901` 读 `promptTokenCount`。
    """
    import json as _json

    from cpa_probe import fingerprint as _fp

    for why, obj in (
        ("OpenAI / compat", {"usage": {"prompt_tokens": 1234}}),
        ("Anthropic", {"usage": {"input_tokens": 1234}}),
        ("Codex responses", {"usage": {"input_tokens": 1234}}),
        ("gemini 原生", {"usageMetadata": {"promptTokenCount": 1234,
                                           "candidatesTokenCount": 5}}),
        ("gemini 蛇形变体", {"usage_metadata": {"prompt_token_count": 1234}}),
    ):
        eq(f"input_tokens · {why}", _fp.input_tokens(_json.dumps(obj)), 1234)

    # **输出**量与总量不能被当成输入量 —— 那会让截断校验反向失效
    eq("只有输出与总量时返回 None",
       _fp.input_tokens(_json.dumps(
           {"usageMetadata": {"candidatesTokenCount": 77,
                              "totalTokenCount": 999}})), None)
    eq("无 usage 时返回 None",
       _fp.input_tokens(_json.dumps({"candidates": [{"content": {}}]})), None)


def test_context_limit_not_confused_with_usage_or_output():
    """自报上限不能把「请求用量」或「输出上限」当成上下文窗口。

    2026-09-05 修的 P1，与已修的「input_tokens:10 写成窗口」是同一缺陷的
    **反方向**。实测三处误取：

        'context_length_exceeded: your request has 275000 tokens' → 275000
        'max_tokens: 64000 > 32000, ... output tokens'            → 64000
        'max_tokens must be <= 8192'                              → 8192

    前两个抓到的是**请求值**，比真实窗口大 → 客户端永不压缩，每个长请求都撞
    400。第三个把**输出**上限写成上下文窗口。区间闸
    `8000 <= val <= 2000000` 一个都挡不住。

    两处收紧：关键词与数字之间必须有限额语气词（不是任意 40 个非数字字符）；
    正文里出现「输出上限」字样时整段放弃。原来那条 `max…tokens?` 模式整条
    删掉 —— `max_tokens` 在 OpenAI 系里指的就是输出上限，误取比命中多。
    """
    from cpa_probe.pipeline import Prober as _P

    L = _P._limit_from_body
    for text, want in (
        # 真实上限形态 —— 必须抠对（这些是走二分的省钱路径）
        ("maximum context length is 200000 tokens", 200000),
        ("prompt is too long: 215000 tokens > 200000 maximum", 200000),
        ("context_length_exceeded, limit 128000", 128000),
        ("context length is limited to 131072", 131072),
        ("max input tokens: 200000", 200000),
        ("最大上下文长度为 128000", 128000),
        ("上下文上限 262144", 262144),
        # 混合正文：同时提上下文与输出上限。上下文那半要留、输出那半要丢 ——
        # 整段否决会把对的值一起丢掉，白走一轮二分（6 次大 body 请求）。
        ("maximum context length is 200000 tokens; "
         "output tokens limited to 8192", 200000),
        ("context length is 200000 but max_tokens must be <= 8192", 200000),
        ("最大上下文长度为 128000，输出上限 8192", 128000),
        # 数字**前面**有输出上限字样 —— 那个数字说的就是输出上限。
        # 这几条只有前置窗口那道闸能挡：模式本身会命中（2026-09-05
        # 撤销实验发现，不加这几条时把整道闸去掉测试仍然绿）。
        ("output tokens: maximum context length is 8192", None),
        ("max_tokens exceeded; context length is 16384", None),
        ("completion tokens limit: context_length_exceeded, limit 8192", None),
        ("输出上限：最大上下文长度为 8192", None),
        # 误取形态 —— 必须一个都不要
        ("context_length_exceeded: your request has 275000 tokens", None),
        ("max_tokens: 64000 > 32000, which is the maximum allowed "
         "output tokens", None),
        ("max_tokens must be <= 8192", None),
        ("This model supports at most 8192 completion tokens", None),
        ("max output tokens is 16384", None),
        ("输出上限 8192", None),
        ("", None),
    ):
        eq(f"自报上限 · {text[:44] or '(空)'}", L(text), want)


def test_model_names_are_character_checked():
    """站方目录里的模型名必须过字符校验 —— 它会拼进 URL 并写进 config.yaml。

    2026-09-05 修的 P1。原来 `section_allows` 只判族与版本，不限字符。实测
    通过全部闸门并原样上线：

        '../../../gemini-3.1-pro'       → 逃出 base 路径，打到同主机别的端点
        'gemini-3.1-pro-x?a=b'          → `:generateContent` 落进 query，
                                          实际请求的是另一个端点；它回 200
                                          就成了「该模型可用」的伪证
        'gemini-3.1-pro.%2e%2e%2fadmin' → 编码过的路径穿越

    而且**不需要拿到 200**：plan 的 catalog 分支把目录里的名字直接当候选，
    这串字面量进 config.yaml，CPA 用同样的方式拼 URL 再发一次。

    `/` **必须允许**：生产配置里 85 个模型名有 `Business/gemini-2.5-pro`、
    `anthropic/claude-opus-5` 这种带前缀的，非字母数字字符只用到 `-` `.` `/`。
    所以判据是「只许这三个 + 禁 `..` 段 + 禁 query/fragment 起始字符」，
    不是「不许有 `/`」。
    """
    import json as _json

    from cpa_probe import model_catalog as _mc
    from cpa_probe import request as _rq

    # ① 判据本身
    for name in ("gemini-3.1-pro", "claude-opus-5", "gpt-5.6-sol",
                 "Business/gemini-2.5-pro", "anthropic/claude-opus-4.8",
                 "gpt-oss:120b", "o4-mini-high", "kimi-k2.7-code"):
        eq(f"真实名字放行 · {name}", _mc.name_is_safe(name), "")
    for name in ("../../../gemini-3.1-pro", "a/../../gemini-3.1-pro",
                 "gemini-3.1-pro-x?a=b", "gemini-3.1-pro.%2e%2e%2fadmin",
                 "gemini-3.1-pro#frag", "gemini-3.1-pro\nX-Injected: 1",
                 "/gemini-3.1-pro", "gemini-3.1-pro/", "gemini//pro",
                 ".hidden", "", "gemini 3.1 pro", "好gemini",
                 "gemini-3.1-pro;a=b", "x" * 129):
        truthy(f"危险名字拒绝 · {name[:28]!r}",
               bool(_mc.name_is_safe(name)))

    # ② 两个选型闸都要过它（手填不豁免 —— 可能从站方页面复制粘贴）
    for name in ("../../../gemini-3.1-pro", "gemini-3.1-pro-x?a=b"):
        eq(f"section_allows 拒 · {name[:24]!r}",
           _mc.section_allows("gemini-api-key", name), False)
        eq(f"section_protocol_ok 拒 · {name[:24]!r}",
           _mc.section_protocol_ok("openai-compatibility", name), False)
    # 回归：合法带前缀的名字仍然通过
    eq("Business/ 前缀仍进 gemini 段",
       _mc.section_allows("gemini-api-key", "Business/gemini-2.5-pro"), True)
    eq("anthropic/ 前缀仍进 compat 段",
       _mc.section_protocol_ok("openai-compatibility",
                               "anthropic/claude-opus-5"), True)

    # ③ 目录入口就丢掉，且说出来
    doc = _json.dumps({"data": [
        {"id": "gemini-3.1-pro"}, {"id": "../../../gemini-3.1-pro"},
        {"id": "Business/gemini-2.5-pro"}, {"id": "gemini-3.1-pro-x?a=b"}]})
    got = _rq.parse_models_response("gemini-api-key", doc)
    eq("目录解析只留安全名字", sorted(got),
       ["Business/gemini-2.5-pro", "gemini-3.1-pro"])
    rejected = _rq.unsafe_names(doc)
    eq("被丢的名字有 2 个", len(rejected), 2)
    truthy("每条都带原因", all(why for _n, why in rejected))

    # ④ 拼 URL 处是最后一道 —— 抛异常而不是静默改名
    ok_url, _h, _b = _rq.build_request(
        "gemini-api-key", "https://x.example", "gemini-3.1-pro", "sk-k")
    truthy("合法名字照常拼 URL", "gemini-3.1-pro:generateContent" in ok_url)
    for bad in ("../../../gemini-3.1-pro", "x?a=b"):
        try:
            _rq.build_request("gemini-api-key", "https://x.example", bad,
                              "sk-k")
        except ValueError as e:
            truthy(f"build_request 拒 {bad[:20]!r}", "不安全" in str(e))
        else:
            FAILED.append(f"build_request 收下了危险模型名 {bad!r}")


def test_no_available_channel_on_any_5xx():
    """「无可用渠道」在任何 5xx 上都算模型专属 —— 只豁免 503 会判死可用站。

    2026-09-05 修。`classify` 的正文优先规则让
    `No available channel for model X under group default` 在**任何**状态码上
    都判「死路」，而模型专属豁免原来只认 400/403/404/503。实测：

        400 死路 专属=True   403 死路 专属=True   404 死路 专属=True
        500 死路 专属=False  ← 立刻判死整段
        502 死路 专属=False  ← 同上
        503 死路 专属=True
        504 死路 专属=False  ← 同上

    同一句话、同一个语义（「这个分组里没有你要的这个模型」），只因为中转站用
    500 而不是 503 发出来，`_stage1` 就在第一个种子上直接 return —— 连第二个
    种子、画像梯、代理都不试。而中转站过载时用 500/502 回这句话是常见形态：
    项目自己的复盘说 666 次 500 是日志最大头。

    状态码本身不带语义（这句措辞来自上游中转站而非 CPA，全仓搜零命中），
    所以不该用它当豁免闸。

    反向也要守：真正的站方故障（正文里**没有**这句话）不能被误豁免 ——
    那种 `classify` 判「临时」而不是「死路」，走不到这个判据。
    """
    from cpa_probe.classify import classify as _classify
    from cpa_probe.pipeline import Attempt as _Attempt
    from cpa_probe.pipeline import _model_specific_dead_end as _ms

    def mk(code, body):
        cat, _why = _classify(code, body)
        return cat, _ms(_Attempt(section="claude-api-key", model="m",
                                 combo="baseline", status=code, category=cat,
                                 action="", elapsed_ms=1, excerpt=body))

    MSG = "No available channel for model claude-opus-5 under group default"
    for code in ("400", "403", "404", "500", "502", "503", "504"):
        cat, ms = mk(code, MSG)
        eq(f"{code} + 无可用渠道 → 死路", cat, "死路")
        truthy(f"{code} + 无可用渠道 → 模型专属", ms,
               "换个模型可能就通，不该判死整段")

    # 中文措辞同样覆盖
    for code in ("500", "503"):
        cat, ms = mk(code, "当前分组无可用渠道")
        truthy(f"{code} + 中文无可用渠道 → 模型专属", ms)

    # 反向：真站方故障不能被误豁免
    for code, body in (("500", "internal server error"),
                       ("502", "Bad Gateway"),
                       ("504", "gateway timeout"),
                       ("500", "<html>502 Bad Gateway</html>")):
        cat, ms = mk(code, body)
        eq(f"{code} {body[:22]!r} 判临时", cat, "临时")
        eq(f"{code} {body[:22]!r} 不算模型专属", ms, False)


def test_model_specific_dead_end_excluded_from_severity_vote():
    """模型专属死路不参与「整段是什么状况」的评选。

    2026-09-05 修。`_stage1` 的 docstring 早就写明「修法是两层：模型专属死路
    不进 seen，这里再把客户端排在死路之前」—— **但第一层从来没实现**，
    那一行是无条件 `seen.append(...)`。

    后果（实测）：「opus-5 客户端门禁 + sonnet-5 该站没有这个模型」这种组合里，
    「死路」严重度 rank 2 优于「门禁」rank 5，于是 `min(seen, ...)` 最终报
    「死路 — 分组无渠道，充值无效」，而真正该报的是客户端门禁（补标识或人工
    接管就能用）。**报错方向反了：一个能救的站被说成没救。**

    但不能整个丢掉：所有种子都是模型专属死路时（该站确实没有我们试的这几个
    模型），那就是唯一的结论 —— 丢掉会让段判不可用却没有类别，界面显示成空白。
    所以分两级：`seen` 优先，空了才用 `seen_weak`。
    """
    import io as _io

    import os as _os

    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = _io.open(_os.path.join(root, "cpa_probe", "pipeline.py"),
                   encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))

    truthy("模型专属死路进 seen_weak 而不是 seen",
           "seen_weak.append((att.category, att.action))" in code,
           "无条件 append 到 seen 会让它参与评选")
    truthy("评选时优先用 seen、空了才用 seen_weak",
           "pool = seen or seen_weak" in code,
           "全是模型专属死路时段会没有类别，界面空白")
    truthy("评选基于 pool 而不是 seen",
           "min(pool, key=lambda ca: self._severity_rank(ca[0]))" in code)

    # 严重度顺序本身：客户端 / WAF 必须排在死路之前（那是这条修复的另一半）
    from cpa_probe.pipeline import Prober as _P

    rank = _P._severity_rank
    truthy("客户端比死路更接近根因", rank("客户端") < rank("死路"))
    truthy("WAF 比死路更接近根因", rank("WAF") < rank("死路"))
    truthy("门禁排在死路之后（它更笼统）", rank("门禁") > rank("死路"))
    truthy("时段最不严重", rank("时段") == max(
        rank(c) for c in _P._SEVERITY))


def test_out_of_pool_entries_are_not_live_sites():
    """CPA 已排除在调度池外的条目不能被当成在用站参与定档避让。

    2026-09-05 修（契约对齐审计发现）。两种形态：

      ① compat 段的 `disabled: true`
         `internal/watcher/synthesizer/config.go:288-290` 与
         `sdk/cliproxy/service_models.go:198` 都是遇 Disabled 直接 continue
         —— 那个 provider **连 Auth 都不合成**。

      ② 任意段的 `excluded-models: ["*"]`
         那正是 CPA 管理面板「停用一个 config 型凭据」的实现
         （`config_apikey_disable.go:12` 的
         `configAPIKeyDisablePattern = "*"`）。`applyExcludedModels` 用通配
         把该凭据的模型全过滤掉，拿到空清单就 `UnregisterClient`。

    实测后果（撤销对比）：一个 `disabled: true` 的 provider 在 300 档且声明
    claude-opus-5 时 ——

        修前：档位谱 [300, 200]、dead 为空 → 新站定档 250，
              理由「挡 1 个在用站」，而那一个不在调度池里
        修后：档位谱 [200]、dead={dead.example} → 新站定档 195

    与 `weight: 0` 的区别：那个只在 weighted-round-robin 下生效，
    这两个**任何策略下都生效**，所以不看 routing.strategy。

    生产配置里这两种当前都是 0 个，所以这是补闸而不是修事故 —— 但本工具
    自己的界面代码（`web/app.js`）早就写着「`excluded-models` 含 `*`」是
    CPAMP 停用徽标的来源，定档这一路却没据此排除。
    """
    import yaml as _yaml

    from cpa_probe.plan import entry_out_of_pool as _OP

    # ① 判据本身。
    #
    # 每条用例只变**一个**维度，其余维度给齐正常值 —— 否则测的是别的判据。
    # （2026-09-05 加了 base-url 空与 compat models 空两条判据之后，
    #  原来那些只写 `{"disabled": False}` 的用例会命中新判据而不是本意。）
    def _ok(sec, **kw):
        """一个各维度都正常的最小条目。"""
        e = {"base-url": "https://ok.example/v1", "api-key": "sk-ok"}
        if sec == "openai-compatibility":
            e["name"] = "ok"
            e["models"] = [{"name": "claude-opus-5"}]
        e.update(kw)
        return e

    C, K, G, X = ("openai-compatibility", "claude-api-key",
                  "gemini-api-key", "codex-api-key")
    for sec, entry, want, why in (
        # ── disabled（只有 compat 有这个字段）──
        (C, _ok(C, disabled=True), True, "compat disabled"),
        (C, _ok(C, disabled=False), False, "compat disabled=False"),
        (C, _ok(C), False, "compat 正常"),
        # 前三段没有 disabled 字段 —— 误认会把正常站判死
        (K, _ok(K, **{"disabled": True}), False, "claude 段没这个字段"),
        # ── excluded-models 含 `*`（管理面板的停用机制）──
        (K, _ok(K, **{"excluded-models": ["*"]}), True, "claude 停用"),
        (X, _ok(X, **{"excluded-models": ["*", "gpt-4o"]}), True, "codex 停用"),
        (C, _ok(C, **{"excluded-models": [" * "]}), True, "带空格的 *"),
        # 正常的 excluded-models 不算停用
        (K, _ok(K, **{"excluded-models": ["gpt-4o"]}), False, "只屏蔽一个模型"),
        (K, _ok(K, **{"excluded-models": []}), False, "空列表"),
        (K, _ok(K), False, "claude 正常"),

        # ── base-url 为空：门槛**按段不同**（2026-09-05 加）──
        #
        # codex 与 compat 只要 base-url 空就被 CPA 在加载期删掉
        # （config_normalization.go:208-210 / :167-170，后者注释写着
        #  "treated as removed"）—— 哪怕 api-key 有值。
        (X, _ok(X, **{"base-url": ""}), True, "codex 空 base-url"),
        (C, _ok(C, **{"base-url": ""}), True, "compat 空 base-url"),
        # gemini 与 claude 要**两个都空**才删
        # （config_normalization.go:244-246 / synthesizer/config.go:145-147）
        (G, _ok(G, **{"base-url": ""}), False, "gemini 只空 base-url"),
        (K, _ok(K, **{"base-url": ""}), False, "claude 只空 base-url"),
        (G, {"api-key": "", "base-url": ""}, True, "gemini 两个都空"),
        (K, {}, True, "claude 两个都缺"),

        # ── compat 的 models 为空（2026-09-05 加）──
        #
        # registerCompat 走 UnregisterClient（service_models.go:206-216），
        # 而 supportsModel 在 supportedModelSet 为空时对任何具名模型返回 false
        # （scheduler.go:838-841）→ 对每个模型都不在池里。
        (C, {"name": "x", "base-url": "https://a/v1"}, True, "compat 缺 models"),
        (C, {"name": "x", "base-url": "https://a/v1", "models": []}, True,
         "compat 空 models"),
        (C, {"name": "x", "base-url": "https://a/v1",
             "models": [{"name": ""}]}, True, "compat models 名字是空串"),
        # **codex 段相反**：空 models 会回落 GetCodexProModels()
        # （service_models.go:825-826），仍在池。这个段间差异是必须分开写的理由。
        (X, {"api-key": "k", "base-url": "https://a/v1"}, False,
         "codex 缺 models 仍在池"),
        (X, {"api-key": "k", "base-url": "https://a/v1", "models": []}, False,
         "codex 空 models 仍在池"),
    ):
        got = bool(_OP(sec, entry))
        eq(f"出池判据 · {why}", got, want)

    # ② 定档不再避让它
    RAW = (
        'openai-compatibility:\n'
        '  - name: "dead-one"\n'
        '    base-url: "https://dead.example/v1"\n'
        '    priority: 300\n'
        '    disabled: true\n'
        '    api-key-entries:\n'
        '      - api-key: "kd"\n'
        '    models:\n'
        '      - name: "claude-opus-5"\n'
        '        alias: ""\n'
        '  - name: "alive"\n'
        '    base-url: "https://alive.example/v1"\n'
        '    priority: 200\n'
        '    api-key-entries:\n'
        '      - api-key: "ka"\n'
        '    models:\n'
        '      - name: "claude-opus-5"\n'
        '        alias: ""\n'
    )
    cfg = _yaml.safe_load(RAW)
    band = cp.build_band(cfg, "openai-compatibility")
    eq("停用的档位不进档位谱", band.tiers, [200])
    truthy("停用的站进 dead_hosts", "dead.example" in band.dead_hosts,
           f"实得 {sorted(band.dead_hosts)}")
    eq("停用的站不占「这个模型的最高档」",
       band.model_top.get("claude-opus-5"), 200)

    pri, reason = cp.suggest_priority(band, 0, models=["claude-opus-5"],
                                      probation=True)
    truthy("新站不为停用的站让位",
           pri < 200,
           f"定档 {pri} —— 300 档那个 provider 不在调度池里，"
           f"不该让新站避让它")
    truthy("理由里不说「挡 N 个在用站」", "在用站" not in reason,
           f"实得 {reason[:80]!r}")

    # ③ excluded-models 那一路同样
    RAW2 = (
        'claude-api-key:\n'
        '  - api-key: "k-off"\n'
        '    base-url: "https://off.example"\n'
        '    priority: 900\n'
        '    excluded-models: ["*"]\n'
        '    models:\n'
        '      - name: "claude-opus-5"\n'
        '        alias: ""\n'
        '  - api-key: "k-on"\n'
        '    base-url: "https://on.example"\n'
        '    priority: 500\n'
        '    models:\n'
        '      - name: "claude-opus-5"\n'
        '        alias: ""\n'
    )
    band2 = cp.build_band(_yaml.safe_load(RAW2), "claude-api-key")
    eq("被停用凭据的档位不进档位谱", band2.tiers, [500])
    truthy("被停用的站进 dead_hosts", "off.example" in band2.dead_hosts)

    # ④ 同一个站有活条目时不能整站判死
    RAW3 = (
        'claude-api-key:\n'
        '  - api-key: "k1"\n'
        '    base-url: "https://mix.example"\n'
        '    priority: 900\n'
        '    excluded-models: ["*"]\n'
        '  - api-key: "k2"\n'
        '    base-url: "https://mix.example"\n'
        '    priority: 900\n'
    )
    band3 = cp.build_band(_yaml.safe_load(RAW3), "claude-api-key")
    eq("同站还有活条目时不算死站", "mix.example" in band3.dead_hosts, False)
    eq("那个档位仍然占着", band3.tiers, [900])


def test_ws_crosstier_note_for_codex():
    """codex 段的 WS 请求跨档，影响面文案必须说清这一层。

    2026-09-05（契约对齐审计发现）。本工具整套影响面计算建立在
    **priority 硬隔离**上（`availableAuthsFromPriorityBuckets`
    只收集 bestPriority 那一桶）。而 codex 段有一条例外：

        scheduler.go:987-997 `highestReadyPriorityLocked`
        preferWebsocket=true 时从高到低扫 priorityOrder，返回**第一个含 ws
        凭据的档** —— 源码注释自己写着 "even if they are in a lower priority
        tier than HTTP-only credentials"

    触发条件在本部署是活的：`weighted-round-robin` + `session-affinity: false`
    → 内建选择器 → scheduler 快路。

    生产实测（fsdownload 版 codex 段）：425 档带 ws 且是最高档，所以此刻不越档；
    但 350/349/348 三档全无 ws，425 一冷却，WS 请求会直接跳到 154 档的
    anyrouter.top，越过三个健康档。

    **处置是「只改文案与计数、不改定档算法」**，理由写在 `ws_crosstier_note`
    的 docstring 里：跨档只发生在下游用 WS 连接时（少数路径），HTTP 请求的
    档位谱仍然完全成立；而 `websockets` 是探测写的、会随重探变化，让它参与
    定档会让档位谱不稳定。

    所以这一项守的是**文案**：codex 段一定有这句话、其余三段一定没有、
    带 ws 与不带 ws 的措辞不同。
    """
    import yaml as _yaml

    from cpa_probe.plan import ws_crosstier_note as _N

    RAW = (
        'codex-api-key:\n'
        '  - api-key: "k-hi"\n'
        '    base-url: "https://hi.example/v1"\n'
        '    priority: 425\n'
        '    websockets: true\n'
        '    models:\n'
        '      - name: "gpt-5.6-sol"\n'
        '        alias: ""\n'
        '  - api-key: "k-mid"\n'
        '    base-url: "https://mid.example/v1"\n'
        '    priority: 350\n'
        '    models:\n'
        '      - name: "gpt-5.6-sol"\n'
        '        alias: ""\n'
        '  - api-key: "k-low"\n'
        '    base-url: "https://low.example/v1"\n'
        '    priority: 154\n'
        '    websockets: true\n'
        '    models:\n'
        '      - name: "gpt-5.6-sol"\n'
        '        alias: ""\n'
    )
    cfg = _yaml.safe_load(RAW)
    band = cp.build_band(cfg, "codex-api-key")

    # ① Band 记住了哪些档带 ws
    eq("带 ws 的档（降序）", band.ws_tiers, [425, 154])
    eq("425 档的 ws 站", band.ws_hosts_at.get(425), ["hi.example"])
    eq("350 档不在 ws 表里", 350 in band.ws_tiers, False)
    # 全部档位不受影响
    eq("档位谱不变", band.tiers, [425, 350, 154])

    # ② 带 ws 的新条目：要说清它与「所有档里带 ws 的站」竞争
    note = _N(band, 300, True)
    truthy("带 ws 时有说明", bool(note))
    truthy("说清是跨档取", "跨档" in note, f"实得 {note[:80]!r}")
    truthy("点名更高的 ws 档", "hi.example" in note, note[:120])
    truthy("点名被挡在后面的更低 ws 档", "low.example" in note, note[:160])
    truthy("给出 CPA 判据", "scheduler.go" in note)
    truthy("不含未渲染的 markdown 标记（界面用 esc 原样显示）",
           "**" not in note, f"实得 {note[:80]!r}")

    # 定在最高处时不该说「更高的 ws 档还有…」
    top_note = _N(band, 500, True)
    truthy("定在最高处说自己是首选", "首选" in top_note, top_note[:100])

    # ③ 不带 ws 的新条目：要说清 WS 请求根本不会落到它身上
    off = _N(band, 500, False)
    truthy("不带 ws 时也有说明", bool(off))
    truthy("说清它不参与那条路径", "不参与" in off, off[:120])
    truthy("提醒档位结论只对 HTTP 成立", "HTTP" in off, off[:160])

    # ④ 其余三段一定没有这句话。
    #
    # 用**手工构造带 ws_tiers 的 Band** 来验，而不是靠「那三段的配置里没有
    # websockets 字段」—— 后者绕不过第二道闸（`not band.ws_tiers`），
    # 于是撤销掉 section 判断时测试仍然绿（2026-09-05 撤销实验发现）。
    #
    # 为什么两道闸都要：section 判断守「这个机制只存在于 codex」，
    # ws_tiers 判断守「这个段里确实有 ws 条目」。少任一道都会在某种输入下
    # 说出不成立的话。
    from cpa_probe.plan import Band as _Band

    for sec in ("claude-api-key", "gemini-api-key", "openai-compatibility"):
        b2 = cp.build_band(cfg, sec)
        eq(f"{sec} 无 ws 跨档说明（真实配置）", _N(b2, 500, True), "")
        # 硬塞一个 ws_tiers 进去 —— section 判断必须挡住它
        faked = _Band(section=sec)
        faked.tiers = [500, 300]
        faked.ws_tiers = [500]
        faked.ws_hosts_at = {500: ["ws.example"]}
        eq(f"{sec} 即使 ws_tiers 非空也不加说明", _N(faked, 300, True), "")

    # ⑤ codex 段但全段没有 ws 条目 —— 硬隔离仍成立，不该多话
    RAW_NOWS = (
        'codex-api-key:\n'
        '  - api-key: "k1"\n'
        '    base-url: "https://a.example/v1"\n'
        '    priority: 500\n'
        '    models:\n'
        '      - name: "gpt-5.6-sol"\n'
        '        alias: ""\n'
    )
    b3 = cp.build_band(_yaml.safe_load(RAW_NOWS), "codex-api-key")
    eq("全段无 ws 时不加说明", _N(b3, 300, True), "")
    eq("全段无 ws 时 ws_tiers 为空", b3.ws_tiers, [])

    # ⑥ 说明真的接到了方案的 warnings 上
    import io as _io

    import os as _os2

    _root = _os2.path.dirname(_os2.path.dirname(
        _os2.path.abspath(__file__)))
    src = _io.open(_os2.path.join(_root, "cpa_probe", "plan.py"),
                   encoding="utf-8").read()
    truthy("build_plan 里调了 ws_crosstier_note",
           "ws_crosstier_note(band, sp.priority" in src,
           "只有函数写对没人调用等于没修")
    truthy("按实测的 websockets 值传参",
           "v.websockets is True" in src,
           "传错的话带 ws 与不带 ws 的措辞会反")


def test_field_names_are_not_mistaken_for_hosts():
    """注释里的 CPA 字段名不能被当成死站名。

    2026-09-05 修（契约对齐审计发现）。`_NOT_A_HOST` 停在旧字段集，CPA 后来
    加的字段一个都没进。实测触发：

        # websockets: true   probed 503, WS not supported
          → _DEAD_NOTE 命中 → _HOST_IN_NOTE 抓出 `websockets`
          → _looks_like_host 返回 True → 进 unhealthy_hosts

    于是一个**字段名**被当成死站，定档因此偏保守（新站被压到一堆「死站」
    后面），而且不报错、不警告。

    生产 config.yaml 当前只抓出真站名没踩到 —— 但本工具自己写的
    `websockets: true   # 原值搬运…` 行尾注释一旦改成英文说明就会触发。

    **两个方向都要守**：字段名不当站名，真站名不被误排除。后者尤其要紧 ——
    排除表加多了会让「读注释拿健康度」这件事失效（某个站真叫
    `mode.example.com` 时，它的点分标签里就有 `mode`），而那同样不报错。
    """
    import yaml as _yaml

    from cpa_probe.plan import _looks_like_host as _L

    # ① CPA 的新字段名一个都不能当站名
    for field in ("websockets", "alpha-search", "support-prompt-cache-key",
                  "cloak", "strict-mode", "sensitive-words", "cache-user-id",
                  "is-compat", "thinking", "display-name", "force-mapping",
                  "image", "input-modalities", "output-modalities",
                  "match-regexr", "rebuild-mid-system-message",
                  "experimental-cch-signing", "disable-cooling",
                  "request-retry"):
        eq(f"字段名不当站名 · {field}", _L(field), False)

    # ② 真站名与它们的点分标签不能被误排除
    for host in ("chiangma.com", "anyrouter.top", "api.123nhh.com",
                 "runanytime.hxi.me", "muyuan.do", "ai.hybgzs.com",
                 "api.facai.cloudns.org", "agentrouter.org", "gorouter.app",
                 "kktoken.cc", "tabitoken.com", "justwoker.top"):
        truthy(f"真站名仍认 · {host}", _L(host))
    for label in ("chiangma", "anyrouter", "muyuan", "facai", "gorouter",
                  "kktoken", "hybgzs", "123nhh"):
        truthy(f"站名标签仍认 · {label}", _L(label))

    # ③ 端到端：字段名注释不该产出 unhealthy_hosts
    RAW = (
        'codex-api-key:\n'
        '  - api-key: "k1"\n'
        '    base-url: "https://real.example/v1"\n'
        '    priority: 500\n'
        '    # websockets: true   probed 503, WS not supported\n'
        '    # alpha-search: true   403 forbidden\n'
        '    # is-compat: true   upstream rejected agent_message\n'
        '    models:\n'
        '      - name: "gpt-5.6-sol"\n'
        '        alias: ""\n'
    )
    band = cp.build_band(_yaml.safe_load(RAW), "codex-api-key", raw=RAW)
    eq("字段名注释不产出死站", sorted(band.unhealthy_hosts), [])

    # ④ 反向：真站名的死站注释仍要抓到（不能因为补表把这条路弄哑）
    RAW2 = (
        'codex-api-key:\n'
        '  - api-key: "k1"\n'
        '    base-url: "https://good.example/v1"\n'
        '    priority: 500\n'
        '    models:\n'
        '      - name: "gpt-5.6-sol"\n'
        '        alias: ""\n'
        '  # bad.example: 实测 503 不可用\n'
        '  - api-key: "k2"\n'
        '    base-url: "https://bad.example/v1"\n'
        '    priority: 400\n'
        '    models:\n'
        '      - name: "gpt-5.6-sol"\n'
        '        alias: ""\n'
    )
    band2 = cp.build_band(_yaml.safe_load(RAW2), "codex-api-key", raw=RAW2)
    truthy("真站名的死站注释仍抓到",
           any("bad" in h for h in band2.unhealthy_hosts),
           f"实得 {sorted(band2.unhealthy_hosts)} —— 补排除表时把这条路弄哑了")


def test_ws_handshake_has_a_real_deadline():
    """WS 握手的 timeout 必须是**整次握手**的截止时间，不是每次 recv 的超时。

    2026-09-05 修的 P2。原来只 `sock.settimeout(timeout)` 然后循环 recv ——
    那是每次读的超时。对端每 0.3 秒送 1 字节且永不发空行时，每次 recv 都在
    timeout 内返回，于是循环最多要收满 64KB 才退出：上界是
    **65536 × 每字节间隔**，而不是调用方给的 timeout。

    实测：`timeout=1` 的调用被挂住 180 秒以上仍未返回（复现脚本本身超时）。
    而调用方传的是 `min(self.timeout, 30)`，本意是 30 秒上限 —— 段级线程
    被钉住，站级并发的槽位也一起占着。

    判据：对面 CPA 用的是真正的截止时间 ——
    `codex_websockets_connection.go:32` 的 `dialer.HandshakeTimeout`
    （同文件 :27 = 30 * time.Second），gorilla 那个字段覆盖整次握手。

    这一项同时守**正常路径不受影响**：加 deadline 时最容易写坏的是
    「剩余时间算成 0 或负数导致立刻超时」。
    """
    import base64 as _b64
    import hashlib as _hashlib
    import socket as _socket
    import threading as _threading
    import time as _time

    from cpa_probe import client as _client

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def _start(mode):
        """起一个假 WS 端点。返回 (port, stop_event)。"""
        srv = _socket.socket()
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)
        stop = _threading.Event()

        def run():
            try:
                c, _a = srv.accept()
                data = c.recv(4096).decode("utf-8", "replace")
                key = ""
                for ln in data.split("\r\n"):
                    if ln.lower().startswith("sec-websocket-key:"):
                        key = ln.split(":", 1)[1].strip()
                if mode == "ok":
                    acc = _b64.b64encode(_hashlib.sha1(
                        (key + GUID).encode()).digest()).decode()
                    c.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        b"Sec-WebSocket-Accept: " + acc.encode()
                        + b"\r\n\r\n")
                elif mode == "bad_accept":
                    c.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\n"
                        b"Upgrade: websocket\r\n"
                        b"Sec-WebSocket-Accept: WRONGVALUE=\r\n\r\n")
                elif mode == "403":
                    c.sendall(b"HTTP/1.1 403 Forbidden\r\n"
                              b"Content-Length: 2\r\n\r\nno")
                elif mode == "drip":
                    # 每 0.3 秒 1 字节，永不发空行 —— 就是原缺陷的触发形态
                    while not stop.is_set():
                        try:
                            c.sendall(b"X")
                        except OSError:
                            break
                        stop.wait(0.3)
                c.close()
            except Exception:                   # noqa: BLE001
                pass
            finally:
                try:
                    srv.close()
                except OSError:
                    pass

        _threading.Thread(target=run, daemon=True).start()
        return port, stop

    # ① 慢速滴数据：必须在 timeout 附近返回，不能被挂住。
    #
    # 在**线程里**调并 join 一个上界（2026-09-05）：缺陷回归时这个调用会跑
    # 65536 × 0.3 秒 ≈ 5.5 小时 —— 直接调的话这条断言不是「失败」而是
    # 「把整个套件挂死」。撤销验证实测过：整批跑到 900 秒被杀，而超时的
    # 原因看不出是哪一项。
    #
    # 一条在回归时挂死的断言，几乎和不存在一样糟：CI 会超时而不是报失败。
    port, stop = _start("drip")
    _time.sleep(0.15)
    box: dict = {}

    def _shake():
        box["t0"] = _time.monotonic()
        try:
            box["r"] = _client.ws_handshake(
                f"ws://127.0.0.1:{port}/responses", timeout=1)
        except Exception as e:                  # noqa: BLE001
            box["err"] = e
        box["dt"] = _time.monotonic() - box["t0"]

    th = _threading.Thread(target=_shake, daemon=True)
    th.start()
    th.join(timeout=6)                          # timeout=1 的调用给 6 倍余量
    hung = th.is_alive()
    stop.set()                                  # 关掉假上游，让线程能退出
    if hung:
        th.join(timeout=5)
    truthy("慢速滴数据时不被挂住",
           not hung,
           "6 秒内没返回（timeout 传的是 1）—— timeout 是每次 recv 的超时"
           "而不是整体截止，上界变成 65536 × 每字节间隔（约 5.5 小时）")
    if not hung and "r" in box:
        dt = box["dt"]
        truthy(f"按 timeout 返回（实测 {dt:.1f}s）", dt < 4,
               f"耗时 {dt:.1f}s")
        eq("超时返回连接层失败", box["r"].status, "000")
        # 错误消息要是**可读的超时说明**，不能是异常的 repr。
        #
        # 为什么单独断言这一点（2026-09-05 撤销实验发现）：去掉
        # `if left <= 0: return` 那道闸之后，`left` 变负 →
        # `sock.settimeout(-0.3)` 抛 ValueError → 被最外层
        # `except Exception` 兜住 → 返回 `000` + `repr(e)`。
        # 于是「不被挂住」与「status == 000」两条都照样过 ——
        # 只有消息从「握手超时（…）」变成
        # `ValueError('Timeout value out of range')`。
        #
        # 那个消息会直接进事件流与导出日志：运维看到一个 Python 异常名，
        # 而真实原因是站方在慢慢吐响应头。
        err = box["r"].error or ""
        truthy("错误里说清是握手超时", "握手超时" in err,
               f"实得 {err!r}")
        truthy("错误不是裸异常 repr",
               "Error(" not in err and "ValueError" not in err,
               f"实得 {err!r} —— 那会让运维以为是本工具的 bug，"
               f"而真实原因是站方在慢慢吐响应头")

    # ② 正常路径不受影响 —— 加 deadline 最容易写坏这一侧
    for mode, want_status, why in (("ok", "101", "正常 101 + 正确 accept"),
                                   ("403", "403", "站方拒绝")):
        port, stop = _start(mode)
        _time.sleep(0.15)
        t0 = _time.monotonic()
        r2 = _client.ws_handshake(f"ws://127.0.0.1:{port}/responses",
                                  timeout=5)
        dt2 = _time.monotonic() - t0
        stop.set()
        eq(f"{why} → status", r2.status, want_status)
        truthy(f"{why} 不该等满 timeout（实测 {dt2:.2f}s）", dt2 < 2,
               "deadline 算错会让正常请求也等满")

    # ③ accept 值不对要认出来（反代吞了 Upgrade、自己回个 101）
    port, stop = _start("bad_accept")
    _time.sleep(0.15)
    r3 = _client.ws_handshake(f"ws://127.0.0.1:{port}/responses", timeout=5)
    stop.set()
    eq("accept 错时状态仍是 101", r3.status, "101")
    truthy("accept 错时在 error 里点明",
           "Accept" in (r3.error or ""),
           f"实得 {r3.error!r} —— 光看 101 会把反代当成支持 WS")

    # ④ 时钟跳变的边角：left 算成负数时不能抛 ValueError。
    #
    # 正常情形下走不到那一支（recv 自己的超时先触发），所以只能直接测函数：
    # 把 deadline 设成过去，看它返回可读的超时说明而不是异常 repr。
    # 撤销实验查明：去掉那道闸的后果不是挂死，而是
    # `settimeout(负数)` → ValueError → 消息变成 Python 异常名。
    port, stop = _start("drip")
    _time.sleep(0.15)
    _real_mono = _time.monotonic
    try:
        # 让 deadline 一算出来就已经过期：第一次 monotonic 返回真实值，
        # 之后跳到未来
        calls = {"n": 0}

        def _jumpy():
            calls["n"] += 1
            return _real_mono() + (100.0 if calls["n"] > 2 else 0.0)

        _client.time.monotonic = _jumpy       # type: ignore[attr-defined]
        r4 = _client.ws_handshake(f"ws://127.0.0.1:{port}/responses",
                                  timeout=1)
    finally:
        _client.time.monotonic = _real_mono   # type: ignore[attr-defined]
        stop.set()
    eq("时钟跳变时仍返回 000", r4.status, "000")
    truthy("时钟跳变时消息可读（不是 ValueError）",
           "握手超时" in (r4.error or "")
           and "ValueError" not in (r4.error or ""),
           f"实得 {r4.error!r} —— settimeout(负数) 抛的 ValueError 漏出来了")

    # ⑤ 源码判据：必须有 deadline 而不是只 settimeout 一次
    import io as _io
    import os as _os3

    _root = _os3.path.dirname(_os3.path.dirname(_os3.path.abspath(__file__)))
    src = _io.open(_os3.path.join(_root, "cpa_probe", "client.py"),
                   encoding="utf-8").read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    truthy("握手循环里有 deadline", "deadline = time.monotonic() + timeout" in code)
    truthy("每次 recv 前按剩余时间设超时",
           "sock.settimeout(left)" in code,
           "只在循环外 settimeout 一次就是原缺陷")


def test_client_send_never_raises():
    """`client.send` 的 docstring 承诺「任何异常都转成 Response，不抛出」。

    2026-09-05 修的 P0。原来 HTTPError 分支里写着 `raw = e.read(READ_LIMIT)`
    —— 它在 except 块**内部**，而 Python 的语义是：except 块内抛出的异常
    **不受同一 try 的其余 handler 保护**。于是下面的 socket.timeout 与兜底
    Exception 都接不到它，异常一路穿出 `send()`。

    实测触发形态：`403 + Content-Length: 5000` 但只写 2 字节后挂住
    （Cloudflare 拦截页、nginx 慢响应都是这形态）→ TimeoutError。

    上游两条调用路径都按这个不变式写：
      · 并行路径（workers>1）把它写成 `category="死路"` +
        `action="探测异常：…"` + `should_downrank=True` ——
        一个只是回应慢的**活站**被判死并建议降权
      · 串行路径与 `run_job` 的 `f.result()` 让整个 job 报错，一批凭据全丢

    状态码要保住：403 就是 403，正文读不全不改变这个事实。
    """
    import socket as _socket
    import threading as _threading
    import time as _time

    from cpa_probe import client as _client

    def _slow_body_server():
        srv = _socket.socket()
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)
        stop = _threading.Event()

        def run():
            try:
                c, _a = srv.accept()
                c.recv(8192)
                # 声明 5000 字节但只写 2 个，然后挂住
                c.sendall(b"HTTP/1.1 403 Forbidden\r\n"
                          b"Content-Length: 5000\r\n\r\nno")
                stop.wait(30)
                c.close()
            except Exception:                   # noqa: BLE001
                pass
            finally:
                try:
                    srv.close()
                except OSError:
                    pass

        _threading.Thread(target=run, daemon=True).start()
        return port, stop

    port, stop = _slow_body_server()
    _time.sleep(0.15)
    raised = None
    r = None
    t0 = _time.monotonic()
    try:
        r = _client.send(f"http://127.0.0.1:{port}/v1/messages",
                         headers={"Content-Type": "application/json"},
                         body=b"{}", timeout=2)
    except BaseException as e:                  # noqa: BLE001
        raised = e
    dt = _time.monotonic() - t0
    stop.set()

    truthy("正文读取超时不抛异常", raised is None,
           f"抛出了 {type(raised).__name__ if raised else ''} —— "
           f"破掉「任何异常都转成 Response」，并行路径会把活站判死并降权")
    if r is not None:
        eq("状态码保住（403 就是 403）", r.status, "403")
        truthy("错误里说清是正文读取失败",
               "正文读取失败" in (r.error or ""),
               f"实得 {r.error!r}")
        truthy(f"按 timeout 返回（实测 {dt:.1f}s）", dt < 6, f"{dt:.1f}s")

    # 源码判据：正文读取必须在**独立的** try 里，不能裸在 except 块内
    import io as _io
    import os as _os

    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = _io.open(_os.path.join(_root, "cpa_probe", "client.py"),
                   encoding="utf-8").read()
    truthy("HTTPError 分支里的正文读取有自己的 try",
           "except Exception as read_err:" in src,
           "裸写 `raw = e.read(...)` 时那一行抛的异常不受同一 try 的"
           "其余 handler 保护")

    print("[OK] client.send: 正文读取超时不抛异常、状态码保住、"
          "错误可读、读取有独立 try")


def test_compressed_bodies_are_decoded():
    """压缩过的响应正文必须解开 —— 否则整条正文判定链静默失效。

    2026-09-05 修的 P1。画像梯的 cc-full / cc-body-* / compat cc-full 几档发
    `accept-encoding: gzip, deflate, br, zstd`（抄 CPA 的形态）。站方照办后
    body 是二进制，而 `decode(errors="replace")` 把它变成一串 U+FFFD ——
    **整条判定链都读文本**：

        classify           → 无异常关键词 → 判「可用」
        has_error_envelope → False
        resp_model         → None → model_matches 放行 → _accept 收下这个模型
        betas.wanted / _limit_from_body / input_tokens / 余额 / 限频 / 时段
                           → 关键词一个都匹配不上

    也就是「死站带模型进 config.yaml」那个假阳性，只是改由压缩触发。

    判据：CPA 发同一套 Accept-Encoding（`claude_executor_request.go:1093`）
    **并且**解码（`claude_executor_execute.go:345`/`:373` 的
    `decodeResponseBody`，注释明确说同时处理「头声明」与「magic byte 探测」
    两种）。探测原来只做了前一半。

    br 与 zstd 标准库没有解码器（本项目零第三方依赖），探到就返回一句可读的
    说明 —— 静默给 U+FFFD 会让上面那条链**无声**失效。
    """
    import gzip as _gzip
    import json as _json
    import zlib as _zlib

    from cpa_probe.classify import classify as _classify
    from cpa_probe.classify import has_error_envelope as _has_env
    from cpa_probe.client import _decode_body as _dec

    ERR = _json.dumps({"type": "error",
                       "error": {"message": "insufficient balance, "
                                            "请充值后重试"}}).encode()

    _co = _zlib.compressobj(wbits=-_zlib.MAX_WBITS)
    RAW_DEFLATE = _co.compress(ERR) + _co.flush()

    for why, raw in (("未压缩", ERR),
                     ("gzip", _gzip.compress(ERR)),
                     ("zlib（deflate 常见形态）", _zlib.compress(ERR)),
                     ("raw deflate（无 zlib 头）", RAW_DEFLATE)):
        out = _dec(raw)
        truthy(f"{why} 能解出正文", "insufficient balance" in out,
               f"实得 {out[:70]!r}")
        # 解开之后判定链要能认出这是错误
        truthy(f"{why} 解开后 classify 认出余额问题",
               _classify("403", out)[0] == "余额",
               f"实得 {_classify('403', out)[0]}")
        truthy(f"{why} 解开后 has_error_envelope 为真", _has_env(out))

    # 解不了的压缩要给**可读说明**，不能是一串替换字符
    zstd = b"\x28\xb5\x2f\xfd" + b"\x00" * 32
    out = _dec(zstd)
    truthy("zstd 给可读说明", "zstd" in out and "不解码" in out,
           f"实得 {out[:70]!r}")
    truthy("zstd 的说明不含替换字符", "\ufffd" not in out)
    # br 没有 magic byte —— 按「解出来几乎全是替换字符」反推
    br_like = bytes(range(0x80, 0xC0)) * 4
    out2 = _dec(br_like)
    truthy("疑似 br 给可读说明", "无法解码" in out2 or "不解码" in out2,
           f"实得 {out2[:70]!r}")

    eq("空正文仍是空串", _dec(b""), "")

    print("[OK] Compressed body: gzip / zlib / raw deflate 都解开且判定链认得出、"
          "zstd 与 br 给可读说明而不是替换字符")

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

    section("探测层安全与协议对齐（2026-09-05）")
    test_client_send_never_raises()
    test_compressed_body_is_decoded()
    test_gemini_input_tokens_recognised()
    test_context_limit_not_confused_with_usage_or_output()
    test_model_names_are_character_checked()
    test_no_available_channel_on_any_5xx()
    test_model_specific_dead_end_excluded_from_severity_vote()
    test_out_of_pool_entries_are_not_live_sites()
    test_ws_crosstier_note_for_codex()
    test_field_names_are_not_mistaken_for_hosts()
    test_ws_handshake_has_a_real_deadline()
    test_client_send_never_raises()
    test_compressed_bodies_are_decoded()

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
