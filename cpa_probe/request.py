"""按段构造上游请求。路径与鉴权形态与 CPA 自身 executor 完全对齐。

源码依据（CLIProxyAPI-main）：
  gemini  gemini_executor.go:186-190   {base}/v1beta/models/{model}:generateContent
                                       Key 走 **x-goog-api-key 头**，不是 query string
  codex   codex_executor_execute.go:76 {base}/responses                保留声明的 base
  claude  claude_executor_execute.go:30 {base}/v1/messages?beta=true
  compat  openai_compat_executor.go:146 {base}/chat/completions        保留声明的 base

段名一律用 config.yaml 里的原始键名字符串（"gemini-api-key" 等），不另设枚举 ——
少一层映射，写回时直接就是 YAML 的键。

claude 段鉴权按 CPA 最终出站规则：官方 API key 用 x-api-key，
第三方基址用 Bearer。额外鉴权只能通过显式 headers 配置重现。

「与 CPA 对齐」优先于「减少变量」（2026-09-01 修正两处）
--------------------------------------------------
原来这两处为了「少引入失败面」而故意与 CPA 不同，结果都造成了误判：

  · gemini 段把 Key 放 query string。实测 cielo 三种画像全部连接层
    失败（000），而前端用 x-goog-api-key 头能拉到几百个模型 —— 探测测的
    不是 CPA 会走的那条路，于是把一个可用站判死。
  · claude 段不带 `?beta=true`。站方按 query 参数分流时，探测与真实转发
    结论不一致，两个方向的误判都可能发生。

探测要回答的是「CPA 这样发通不通」，不是「这个端点本身通不通」。形态不一致时，
探测结论对 config.yaml 就没有指导意义。
"""

from __future__ import annotations

from .parse import SECTIONS, base_for_section

# 探测文本：绝不能用 "hi" / "你好" —— 会触发站方反测活拦截。
# 用技术问句，长度足够但不浪费 token。（2026-08-29 实测修正）
PROBE_TEXT = (
    "Reply with one short sentence: what is the difference "
    "between a hash map and a tree map?"
)

# 探测文本池：**同一把 Key 恒定、不同 Key 不同**（2026-09-11）
# ---------------------------------------------------------
# 为什么要池而不是一句话固定值
#   站方的反测活多是「同一段文本高频重复出现」这种统计特征 —— 一句写死的
#   探针在站方日志里会是一条极显眼的尖峰（本项目一轮全量重探对同一个站要打
#   几十次）。换成按 Key 派生的选择，站方看到的是不同用户问不同的技术问题，
#   与真实流量的形态一致。
#
# 为什么**不能**每次请求随机
#   `fingerprint.py` 的换模检测靠「同一输入的响应是否一致」，`input_tokens`
#   的上下文推算也依赖输入长度稳定。每次换文本会让这两条判定失去基线，
#   等于用一个新的假阳性换掉旧的。所以是「按 Key 稳定」而不是「随机」。
#
# 选文本的三条硬约束（前两条是 2026-08-29 实测出来的）
#   · 绝不用 "hi" / "你好" / "你是什么模型" —— 这类是反测活的头号特征串；
#   · 必须是真实技术问句：有明确答案、答案短、不触发内容审查；
#   · 长度相近（本池 70-95 字符），使 `sent_chars` 在不同 Key 之间可比。
_PROBE_TEXTS: tuple[str, ...] = (
    PROBE_TEXT,
    "Reply with one short sentence: what is the difference "
    "between a stack and a queue?",
    "Reply with one short sentence: what does an index do in a database?",
    "Reply with one short sentence: what is the difference "
    "between TCP and UDP?",
    "Reply with one short sentence: what does a compiler do?",
    "Reply with one short sentence: what is the difference "
    "between a process and a thread?",
    "Reply with one short sentence: what is a hash collision?",
    "Reply with one short sentence: what does garbage collection do?",
)


def probe_text_for(api_key: str) -> str:
    """这把 Key 该用哪句探测文本。同 Key 恒定，不同 Key 分散。

    用 Key 的 sha256 取模选池 —— 与 `codex_cache_key` / profiles 的
    `device_id` 同一套派生思路（随 Key 变、不随请求变），三处口径一致。

    空 Key 回落到 `PROBE_TEXT`：保持既有行为，不给调用方制造新的分支。
    """
    if not api_key:
        return PROBE_TEXT
    import hashlib
    h = hashlib.sha256(api_key.encode("utf-8", "replace")).digest()
    return _PROBE_TEXTS[h[0] % len(_PROBE_TEXTS)]

# Codex 客户端真实标识（2026-08-29 从本机 codex 插件实测抄录）
UA_CODEX = "codex_cli_rs/0.5.11 (Windows 11; x86_64) WindowsTerminal"
ORIGINATOR = "codex_vscode"

# codex 段请求体里的 `instructions`。真实 Codex CLI 发的是整份系统提示词
# （codex-rs/core/gpt_5_codex_prompt.md 等，按模型选），这里只取开头几句 ——
# 实测站方校验的是**字段在不在**，不是内容长短（去掉这个字段仍被接受，
# 但真实客户端一定带，照带以保持形态一致）。
CODEX_INSTRUCTIONS = (
    "You are Codex, based on GPT-5. You are running as a coding agent in the "
    "Codex CLI on a user's computer."
)


def codex_cache_key(api_key: str) -> str:
    """codex 段请求体的 `prompt_cache_key`。

    真实 Codex CLI 每个会话带一个稳定 id（codex-api/src/common.rs:300）。
    站方按字段集校验，缺它一律 `400 invalid codex request`（2026-09-10 实测）。

    取值随 Key 变而不随请求变：
      · 随 Key 变 —— 不同凭据不共用同一个缓存键，避免站方按 cache key 做
        会话归并时把两把 Key 的探测算成同一会话；
      · 不随请求变 —— 同一把 Key 的多次探测复用同一个键，与真实客户端
        「一个会话一个键」的形态一致，也便于站方侧对账。

    与 `profiles.py` 的 `device_id` 同一套理由（见那里的 `key_hash` 说明）。
    """
    import hashlib
    return "codex-" + hashlib.sha256((api_key or "").encode()).hexdigest()[:32]

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# CPA 各段默认发出的 User-Agent（下界基准，非「完全不发 UA」）
# 依据 cpa-atlas 第 06 章「各协议段默认发出的 User-Agent」
CPA_DEFAULT_UA: dict[str, str | None] = {
    "gemini-api-key": None,                     # gemini 段 CPA 不设 UA
    "codex-api-key": UA_CODEX,                  # 视 cloaking 开关，当前用客户端真实值
    "claude-api-key": "CLIProxyAPI/6.0",        # 透传，缺失时回落此值
    "openai-compatibility": "cli-proxy-openai-compat",
}


def _check(section: str) -> None:
    if section not in SECTIONS:
        raise ValueError(f"未知段：{section!r}，应为 {SECTIONS}")


def build_request(
    section: str,
    base_url: str,
    model: str,
    api_key: str,
    *,
    extra_headers: dict[str, str] | None = None,
    max_tokens: int = 64,
    # None = 按 Key 从探测文本池里派生（同 Key 恒定，见 probe_text_for）。
    # 显式传字符串仍然优先 —— 换文本救援那条路要指定文本，不能被覆盖。
    text: str | None = None,
    stream: bool = False,
    declared_base: bool = False,
    entry_config: dict | None = None,
    cfg: dict | None = None,
    source_identity=None,
    client_headers: dict[str, str] | None = None,
    session_id: str = "",
) -> tuple[str, dict[str, str], dict]:
    """返回 (url, headers, json_body)。

    新裸源采用保守默认路径；已有 CPA 配置须传 declared_base=True。
    source_identity 可固定整轮任务的快照，避免热路径重复读源码。

    model 必须先过 `model_catalog.name_is_safe`（2026-09-05 加的最后一道）。
    gemini 段把它拼进 URL 路径，含 `../` 或 `?` 的名字会改变请求去向 ——
    上游两层闸（`section_allows` / `section_protocol_ok`）已经拦过，这里再拦
    一次是因为**这里才是真正拼 URL 的地方**：将来加新调用路径时，忘了过上游
    闸门也不会漏出去。抛异常而不是静默改名 —— 静默改会让日志里的模型名与
    实际发出去的不一致。
    """
    _check(section)
    if text is None:
        text = probe_text_for(api_key)
    from .model_catalog import name_is_safe
    why = name_is_safe(model)
    if why:
        raise ValueError(f"模型名不安全，拒绝构造请求：{why}（{model[:60]!r}）")
    base = base_for_section(base_url, section, declared_base=declared_base)
    if source_identity is None:
        from .cpa_source_probe import cached_identity
        source_identity = cached_identity()
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if section == "gemini-api-key":
        # Key 走 **头** 而不是 query string —— CPA 只用 x-goog-api-key
        # （gemini_executor.go:190/304/424/504/665，全库无 `?key=`）。
        # 2026-09-01 实测：用 query string 时 cielo 三种画像全部
        # 连接层失败（000），而前端用头的方式能拉到几百个模型 —— 探测测的
        # 不是 CPA 真实会走的那条路，于是把一个可用站判死。
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        url = f"{base}/v1beta/models/{model}:{action}"
        headers["x-goog-api-key"] = api_key
        body: dict = {"contents": [{"role": "user", "parts": [{"text": text}]}]}

    elif section == "codex-api-key":
        # 请求体必须带真实 Codex CLI 的字段集，否则 new-api 系中转直接回
        # `400 invalid codex request`（2026-09-10 alfa.example 实测逐字段确认）
        # ----------------------------------------------------------------
        # 原来这里是 `{"model":…, "stream":False, "input":"<字符串>"}`，三个字段。
        # 站方按**字段集**校验，缺一个就判形，与凭据、身份头都无关 ——
        # 实测把 Originator / User-Agent 全部删掉，结果与带全套 codex-tui 头一致。
        #
        # 逐字段拆出来的必填项（其余字段去掉都仍被接受）：
        #   · `input` 必须是**数组**（`[{type:message, role, content:[{type:input_text,…}]}]`），
        #     裸字符串一律 400。CPA 的翻译层也做同样的转换
        #     （codex_openai-responses_request.go:16-21）
        #   · `include: ["reasoning.encrypted_content"]` —— 去掉立刻退回 400
        #   · `prompt_cache_key` —— 去掉立刻退回 400。这是原来唯一缺的那个，
        #     真实 Codex CLI 一定带（codex-rs/codex-api/src/common.rs:300），
        #     而 CPA 在 codex 路径上**不删它**（codex_executor_execute.go:61 只删
        #     prompt_cache_retention；翻译层 :34 只删 prompt_cache_options /
        #     prompt_cache_retention；唯一删它的 server_routes.go:245
        #     sanitizeCodexAlphaSearchBody 仅作用于 alpha-search 开关），
        #     所以真实客户端经 CPA 转发时它原样透传 —— 探测缺它就是探测的错。
        #
        # 其余字段（instructions / reasoning / tool_choice / store /
        # parallel_tool_calls）实测去掉也不会被拒，但仍然照真实客户端带上：
        # 「与 CPA 对齐」优先于「减少变量」（见模块 docstring）。
        #
        # CPA Execute 对上游无条件 stream=true；消费者必须验证 SSE 终态，
        # 不能为了沿用旧 JSON 判定链而改变请求契约。
        url = f"{base}/responses"
        headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "instructions": CODEX_INSTRUCTIONS,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": text}]}],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"effort": "medium", "summary": "auto"},
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": codex_cache_key(api_key),
        }

    elif section == "claude-api-key":
        # 带 `?beta=true` —— CPA 三条 claude 路径全都带
        # （claude_executor_execute.go:31、claude_executor_stream.go:33、
        #   claude_executor_tokens.go:128 —— 三处都是
        #   `fmt.Sprintf("%s/v1/messages?beta=true", baseURL)`）。
        # 用全名不用缩写：CPA 的 executor 目录里有 40 多个
        # `claude_executor_*.go`，写 `_stream.go` 那种缩写在别处搜不到。
        # 原来不带的理由是「少一个变量」，但那让探测与真实转发形态不一致：
        # 站方按 query 参数分流时，探测通了而 CPA 不通（或反之）。
        # 对齐优先于减少变量 —— 探测要问的是「CPA 这样发通不通」。
        url = f"{base}/v1/messages?beta=true"
        headers.update(_claude_auth(base, api_key))
        headers["anthropic-version"] = "2023-06-01"
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": text}],
        }

    else:  # openai-compatibility
        url = f"{base}/chat/completions"
        headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": text}],
        }

    # 按 CPA 源码把该段的**无条件请求体改写**套到探测体上（2026-09-11）
    # ----------------------------------------------------------------
    # 第 1 条说的不只是 codex：「codex、gemini、claude、openai 等类型都有
    # 大量网站」在 cc-switch 能用、经 CPA 就不通。根因同一个 —— 探测发的
    # 形态与 CPA 真实转发的形态不一致，而每一段的差异各不相同。
    #
    # 按执行入口及条件提取（`cpa_source_probe.parse_body_rules`）：
    #   codex   强制 stream=true；删 previous_response_id / generate /
    #           prompt_cache_retention / safety_identifier / stream_options
    #   gemini  按流式入口处理；删 session_id 也须核对所属入口
    #   compat  include_usage 仅用于流式；prompt_cache_key 的辅助函数
    #           受配置与会话条件控制，不能把变量名或猜测值当成强制字段
    #   claude  删 diagnostics
    # 其中 compat 的两个强制字段是本项目探测**从没发过**的 —— 严格校验的
    # 中转站会因未知字段拒收，于是「探测通、网关不通」。
    #
    # 取值**从 CPA 源码解析**（第 6 条：严禁硬编码），拿不到就保持基线不动 ——
    # 宁可少改一个字段，也不要按过期的假设去改写请求体。
    #
    # 不套用无法确定的变量与条件，避免猜测请求体：
    #   · `model` 不套：它的值在 CPA 里是变量（baseModel / upstreamModel），
    #     解析出来是变量名不是字面量，而探测早就自己填了正确的模型名。
    #   · 已确认的删除项照常处理；删不存在的键是空操作。
    if stream and section in ("claude-api-key", "openai-compatibility"):
        body["stream"] = True
    shape = _body_shape_for(section, stream=stream, config=entry_config,
                            source_identity=source_identity)
    if shape:
        force, _drop = shape
        for k, val in (force or {}).items():
            if k == "model":
                continue
            if val == "true":
                _set_dotted(body, k, True)
            elif val == "false":
                _set_dotted(body, k, False)
            elif val.startswith('"'):
                import json
                _set_dotted(body, k, json.loads(val))
        for path in _drop:
            _drop_dotted(body, path)

    # Explicit custom authentication is reproducible via the entry's headers.
    # Dynamic references need caller context; absent references are omitted.
    for custom in ((entry_config or {}).get("headers"), extra_headers):
        headers = apply_custom_headers(headers, custom, client_headers=client_headers,
                                       session_id=session_id)
    if section == "codex-api-key":
        from . import profiles
        ident = source_identity
        codex_cfg = (cfg or {}).get("codex") or {}
        if codex_cfg.get("disable-codex-cloaking") is not True:
            # 这里的覆盖是**刻意**的，别改成「只补没给过的」（2026-09-18 试过一次）
            # ------------------------------------------------------------
            # cloaking 开着时 CPA 自己就会把 UA 与 Originator 改写成它那一套
            # （`codex_executor_execute.go` 一带）。探测要复现的是「经 CPA 之后
            # 到达站方的形状」，所以这里必须同样覆盖 —— 否则测出来「某个画像档
            # 能过」，而 CPA 实跑时那个 UA 根本发不出去，结论是假的。
            # 契约锁在 tests/test_source_compliance.py:207。
            # 代价：cloaking 开着时画像梯里只改 UA/Originator 的那几档与基线
            # 等价，白烧请求。本部署 `codex.disable-codex-cloaking: true`
            # （fsdownload/config.yaml:475），走的是下面的分支，梯子照常生效。
            headers = apply_custom_headers(headers, {
                "User-Agent": ident.codex_user_agent or profiles._CODEX_UA_DEFAULT,
                "Originator": ident.codex_originator or profiles._CODEX_ORIGINATOR_DEFAULT})
        headers = apply_custom_headers(headers, {
            "Accept": "text/event-stream" if body.get("stream") else "application/json"})

    return url, headers, body


_BODY_SHAPE_CACHE: dict | None = None  # Legacy test hook; no process-lifetime cache.


def _body_shape_for(section: str, *, stream: bool = False, config: dict | None = None,
                    source_identity=None):
    """Resolve the task snapshot; source cache owns freshness and failures.

    Pass source_identity to pin a probing task. Without it, changed roots,
    revisions or expired failures are observable rather than cached forever.
    """
    from .cpa_source_probe import cached_identity, resolve_body_rules
    ident = source_identity if source_identity is not None else cached_identity()
    if section in ident.body_rules:
        return resolve_body_rules(ident.body_rules[section], stream=stream, config=config)
    return ident.body_shape.get(section)


def _set_dotted(obj: dict, path: str, value) -> None:
    """按 `a.b.c` 的点号路径写值，中间层不存在就建。

    CPA 的 `SetBoolIfDifferent(translated, "stream_options.include_usage", true)`
    用的是 sjson 的点号路径语法，探测这边要写出**同样的嵌套结构**，
    直接 `body["stream_options.include_usage"]` 会造出一个字面量含点的键，
    那不是同一个 JSON 形态。
    """
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _drop_dotted(obj: dict, path: str) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        obj = obj.get(part)
        if not isinstance(obj, dict):
            return
    obj.pop(parts[-1], None)


def _claude_auth(base: str, api_key: str) -> dict[str, str]:
    from urllib.parse import urlsplit
    if not api_key:
        return {}
    if urlsplit(base).hostname == "api.anthropic.com":
        return {"x-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def apply_custom_headers(headers: dict, custom: dict | None, *,
                         client_headers: dict | None = None,
                         session_id: str = "") -> dict:
    """Case-insensitive override, resolving CPA $ references without leaking them."""
    out = dict(headers)
    incoming = {k.lower(): v for k, v in (client_headers or {}).items()}
    for key, value in (custom or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        key, value = key.strip(), value.strip()
        if not key or not value:
            continue
        if "$CPA-SESSION-ID" in value.upper():
            if not session_id:
                continue
            import re
            value = re.sub(r"\$CPA-SESSION-ID", lambda _: session_id, value, flags=re.I)
        elif value.startswith("$"):
            value = incoming.get(value[1:].strip().lower(), "")
        if not value:
            continue
        if "\r" in key + value or "\n" in key + value:
            raise ValueError("invalid custom header")
        for old in list(out):
            if old.lower() == key.lower():
                del out[old]
        out[key] = value
    return out


def models_endpoint(section: str, base_url: str, api_key: str, *,
                    declared_base: bool = False, extra_headers: dict | None = None,
                    entry_config: dict | None = None,
                    client_headers: dict | None = None,
                    session_id: str = "") -> tuple[str, dict[str, str]]:
    """列模型端点。用于「这个站到底有哪些模型」的发现阶段。

    这是探测的第一步，代价最低（多数站不计费），先拿到真实模型清单再逐个验。
    """
    _check(section)
    base = base_for_section(base_url, section, declared_base=declared_base)
    headers = {"Content-Type": "application/json"}

    if section == "gemini-api-key":
        # 同 build_request：Key 走头，与 CPA 一致
        headers["x-goog-api-key"] = api_key
        url = f"{base}/v1beta/models"
    elif section == "claude-api-key":
        headers.update(_claude_auth(base, api_key))
        headers["anthropic-version"] = "2023-06-01"
        url = f"{base}/v1/models"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
        url = f"{base}/models"
    for custom in ((entry_config or {}).get("headers"), extra_headers):
        headers = apply_custom_headers(headers, custom, client_headers=client_headers,
                                       session_id=session_id)
    return url, headers


def identity_combos(section: str, cfg: dict | None = None):
    """**已弃用**，转发给 `profiles.ladder()`。保留只为不破坏外部调用。

    为什么弃用（2026-09-01）
    ----------------------
    原实现四段共用一份 codex 形态（User-Agent + Originator）。`Originator`
    是 codex 独有的头，对 claude / gemini 段毫无意义 —— 于是 claude 段一个
    对的组合都没有，五种全试也过不去。实测 golf 的门票是
    user-agent + anthropic-beta + x-app 三项缺一不可，而那三项从不在这个表里。

    更根本的是返回值只有 headers。`metadata.user_id` 是请求**体**字段，
    这个签名压根表达不了它 —— 而它是 zulu 的门票之一。

    新表见 cpa_probe/profiles.py：按段分开、按客户端族分组、族内嵌套超集、
    headers 与 body_patch 同时给。
    """
    from . import profiles

    return [(p.name, p.headers) for p in profiles.ladder(section, cfg)
            if not p.body_patch]


def parse_models_response(section: str, text: str) -> list[str]:
    """从列模型响应里抽出模型 id 清单。三种 JSON 形态都认。

    **在这里就丢掉字符不安全的名字**（2026-09-05 加）。这是站方数据进入本工具的
    第一个入口，越早丢越好 —— 后面有目录落盘、探测队列、方案生成、界面预勾、
    写回 config.yaml 五条下游路径，逐个补闸容易漏（实测 `newest_generation_per_line`
    就会原样保留 `../../../gemini-3.1-pro`）。

    丢掉而不是报错：一个中转站的目录里混进几个奇怪名字不该让整轮探测失败。
    但要**在事件流里说出来** —— 调用方拿 `parse_models_response_verbose` 取原因。
    """
    import json
    import re

    try:
        data = json.loads(text)
    except Exception:
        # 退化：正则捞 id/name 字段
        return sorted(set(re.findall(r'"(?:id|name)"\s*:\s*"([^"]{2,80})"', text)))

    out: list[str] = []
    if isinstance(data, dict):
        # OpenAI 形态 {"data":[{"id":...}]}
        items = data.get("data") or data.get("models") or []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    v = it.get("id") or it.get("name") or ""
                    if isinstance(v, str) and v:
                        # gemini 的 name 形如 "models/gemini-2.5-pro"
                        out.append(v.split("/")[-1] if v.startswith("models/") else v)
                elif isinstance(it, str):
                    out.append(it)
    elif isinstance(data, list):
        for it in data:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                v = it.get("id") or it.get("name") or ""
                if isinstance(v, str) and v:
                    out.append(v)
    return sorted(set(_keep_safe_names(out)))


def _keep_safe_names(names: list[str]) -> list[str]:
    """丢掉字符不安全的模型名。见 model_catalog.name_is_safe。"""
    from .model_catalog import name_is_safe

    return [n for n in names if not name_is_safe(n)]


def unsafe_names(text: str, section: str = "") -> list[tuple[str, str]]:
    """目录里被丢掉的名字与原因。给事件流用 —— 静默丢站方数据不好。

    单独一个函数而不是让 `parse_models_response` 返回两个值：那个函数有
    七个调用点，改签名都要动；而「被丢了什么」只有事件流关心。
    """
    import json
    import re

    from .model_catalog import name_is_safe

    raw: list[str] = []
    try:
        data = json.loads(text)
    except Exception:
        raw = re.findall(r'"(?:id|name)"\s*:\s*"([^"]{2,80})"', text)
    else:
        def walk(items):
            for it in items or []:
                if isinstance(it, str):
                    raw.append(it)
                elif isinstance(it, dict):
                    v = it.get("id") or it.get("name") or ""
                    if isinstance(v, str) and v:
                        raw.append(v.split("/")[-1]
                                   if v.startswith("models/") else v)
        if isinstance(data, dict):
            walk(data.get("data") or data.get("models") or [])
        elif isinstance(data, list):
            walk(data)
    out = []
    for n in dict.fromkeys(raw):
        why = name_is_safe(n)
        if why:
            out.append((n[:80], why))
    return out


def next_page_token(text: str) -> str:
    """Gemini 列模型的翻页游标。没有就返回空串。

    为什么需要（2026-09-01，对齐 CPAMP）：`/v1beta/models` 分页返回，
    默认页长有限。CPAMP 会一直翻到 `nextPageToken` 为空、上限 20 页
    （`apps/web/src/services/api/models.ts:314` 的
    `for (let page = 0; page < 20; page += 1)`，被
    `ProviderHealthCheckDrawer/healthCheck.ts:401` 的
    `fetchGeminiModelsViaApiCall` 调用）；本工具原来只读第一页，于是
    gemini 段的目录被截断，后面的模型根本没机会被验。

    2026-09-04 核实：文件位置以前写成 `healthCheck.ts:279-364` —— 那个行号区间
    是 `models.ts` 里的分页函数，不是 healthCheck。上限 20 页这个数是对的。

    其余三段（OpenAI / Codex / Claude 形态）不分页，调用方只对 gemini 用。
    """
    import json

    try:
        data = json.loads(text)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    tok = data.get("nextPageToken") or data.get("next_page_token") or ""
    return tok if isinstance(tok, str) else ""
