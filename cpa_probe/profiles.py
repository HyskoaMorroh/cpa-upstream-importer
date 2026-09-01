r"""客户端画像梯：站方要什么形态的客户端，就给它什么形态。

为什么独立成模块（2026-09-01）
----------------------------
原来三处各有一份画像：`request.identity_combos`（四段共用一份 codex 形态）、
`tools/diag-identity.py`（自带一张表）、`tools/recheck.py`（一个头都不发）。
三份不一致，于是同一个站在三个入口给出三种结论。这里是唯一一份。

梯子为什么必须**嵌套**
--------------------
实测教训（2026-09-01 agentrouter）：门票是 user-agent + anthropic-beta + x-app
三项**缺一不可**。如果梯子是「试 A、试 B、试 C」的平行结构，三项各试一遍全都
失败，而它们的并集本来是通的 —— 探测会把一个可用站判死。

所以主梯每一档都是前一档的**超集**：第 k 档失败即前 k 档的并集都不够，不必
回头补试。第一个通过的档是「最省的可用档位」，不是「最小必需头集合」——
后者要逐项剔除，代价 2^n 次请求，不值。

最后一档 = CPA 开 `fingerprint-profile: claude-code-cli` 时实际发出的全套
（源码依据见下面各常量）。这一档还不通，说明站方查的东西超出配置层能表达的
范围，改 config.yaml 无解，只能人工接管。

alt 档为什么单列
--------------
浏览器 UA 这类是**替换** User-Agent 而非追加，放进嵌套梯会破坏超集关系。
它们排在主梯之后：主梯全败才试，问的是「站方是不是压根不认 CLI，只认浏览器」。

值从哪来
-------
优先从 CPA 自己的 `claude-header-defaults` / `codex.header-defaults` 配置块派生
（config_types.go:115-132），读不到才用内置常量。这样 CPA 升级或用户改了那个块，
探测自动跟上，不用改这里的代码。

版本号是**填充**，形态才是门票 —— 实测 `claude-cli/2.1.220` 被拒、
`claude-cli/2.1.220 (external, cli)` 通过。CPA 的检测正则同口径
（claude_client_detection.go:32 要求 `^claude-cli/\d+\.\d+\.\d+\s+\(external,...\)$`）。
所以 UA 用模板拼，版本号可换，括号那截不能少。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from .parse import SECTIONS


# ── 内置默认值（CPA 源码抄录，配置读不到时的回落）─────────────────
#
# claude：helps/claude_device_profile.go:23-27 defaultClaudeFingerprint*
_CC_VERSION_DEFAULT = "2.1.220"
_CC_PKG_DEFAULT = "0.94.0"
_CC_RUNTIME_DEFAULT = "v26.3.0"
_CC_OS_DEFAULT = "MacOS"
_CC_ARCH_DEFAULT = "arm64"
_CC_TIMEOUT_DEFAULT = "600"

# UA 形态模板。括号那截是门票，版本号只是填充 —— 见模块 docstring。
_CC_UA_TEMPLATE = "claude-cli/{version} (external, cli)"

# anthropic-beta：claude_executor_request.go:106-150 claudeCodeCLIBetas() 组装出的
# 常量部分。前两项 + claudeCodeCLIConstantBetas（同文件 60-66）。
# 与请求体相关的条件项（advisor / advanced-tool-use / effort / fast-mode 等）不带 ——
# 探测请求里没有 tools、没有 speed，真实客户端那时也不会发它们。
_CC_BETAS_MIN = "claude-code-20250219"
_CC_BETAS_STD = ",".join([
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "redact-thinking-2026-02-12",
    "thinking-token-count-2026-05-13",
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
])
# 全量再补 mid-conversation-system 与 effort —— claudeCodeCLIBetas 在
# 「非 legacy 模型」与「无条件」两处分别追加它们（同文件 123-132）。
_CC_BETAS_FULL = _CC_BETAS_STD + ",mid-conversation-system-2026-04-07,effort-2025-11-24"

# codex：codex_executor_request.go:26-27
_CODEX_UA_DEFAULT = "codex-tui/0.146.0 (Mac OS 26.5.0; arm64) iTerm.app/3.6.10 (codex-tui; 0.146.0)"
_CODEX_ORIGINATOR_DEFAULT = "codex-tui"
# 旧值：2026-08-29 从本机 codex 插件抄录。有的站认这个不认上面那个（客户端
# 形态不同：vscode 插件 vs 终端 TUI），所以两个都要在梯子里。
_CODEX_UA_VSCODE = "codex_cli_rs/0.5.11 (Windows 11; x86_64) WindowsTerminal"
_CODEX_ORIGINATOR_VSCODE = "codex_vscode"

# compat：openai_compat_executor.go:155 死值，CPA 不给这一段做任何伪装
_COMPAT_UA_DEFAULT = "cli-proxy-openai-compat"

# gemini：CPA 在 gemini 段不设 UA（gemini_executor.go 只 set x-goog-api-key）
_GEMINI_CLI_UA = "GeminiCLI/0.4.0 (linux; x64)"

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_OPENAI_SDK_UA = "OpenAI/Python 1.59.0"

# metadata.user_id 的两种形态。站方可能只认其中一种，所以两种都要试。
#   json  —— CPA 自己生成的（helps/cloak_utils.go:14-18 claudeMetadataUserID）
#   plain —— 2026-08-31 实测某站通过的下划线拼接形态
# device_id 必须是 64 位十六进制（cloak_utils.go:12 的正则），且**随 Key 变** ——
# 写死等于所有 Key 共用一个假身份，站方一眼看穿。
# 注意花括号**不转义**：render() 走 str.replace 而不是 str.format，写成 `{{`
# 会让双花括号原样留在输出里，站方拿到的是非法 JSON。自检脚本抓到过一次。
_UID_JSON = ('{"device_id":"{key_hash}","account_uuid":"",'
             '"session_id":"{uuid1}"}')
_UID_PLAIN = "user_{key_hash}_account_{uuid2}_session_{uuid1}"

_CC_SYSTEM_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."


@dataclass(frozen=True)
class Profile:
    """一档画像。headers 与 body_patch 同时给 —— 光有 headers 表达不了
    metadata.user_id，那是请求**体**字段，而它是某些站的门票之一。

    family: 客户端族。嵌套超集关系**只在族内成立** —— OpenAI SDK 与 Claude Code
          CLI 是两套不同的客户端指纹，前者不是后者的子集，把它们串成一条链会让
          自检误报，也会掩盖「这个站认哪一族」这个真正的结论。
    tier: 族内完整度，0=基线 1=最省 2=标准 3=全量 4+=带 body。同 (族, tier) 的
          多个档是「或」关系（codex 的 TUI 与 vscode 两种真实形态）。
    alt:  True = 替换型画像（浏览器 UA），只在主梯全败后试。
    """

    name: str
    tier: int
    family: str = "cc"
    headers: dict[str, str] = field(default_factory=dict)
    body_patch: dict = field(default_factory=dict)
    alt: bool = False
    why: str = ""

    @property
    def is_baseline(self) -> bool:
        return not self.headers and not self.body_patch


def _cc_headers(defaults: dict, *, betas: str, std: bool = False,
                full: bool = False) -> dict[str, str]:
    """Claude Code 形态的头。full=True 时补 X-Stainless 族与会话头。

    头名一律小写：CPA 上线前会把它们改成真实客户端的大小写
    （claude_executor_request.go:1055-1065 claudeWireHeaderCasing），而 urllib
    会把 `anthropic-beta` 规范化成 `Anthropic-beta`。HTTP 头名大小写不敏感，
    中转站按 Go 的 http.Header 或 nginx 处理都会归一化，所以这里不做特殊处理 ——
    但**值**的形态必须精确（见 docstring 里 UA 的实测）。
    """
    ua = _CC_UA_TEMPLATE.format(
        version=defaults.get("version") or _CC_VERSION_DEFAULT)
    h = {
        "user-agent": ua,
        "anthropic-beta": betas,
    }
    if std or full:
        # x-app 是实测的门票之一（agentrouter：缺它 401，有它 200）。
        # 从 std 档起就带上 —— 它比 X-Stainless 族更常被检查。
        # 用显式参数而不是拿 betas 与常量比身份：那种写法在 betas 被
        # 拼接或从配置读入后就静默失效，而失效方向是「少发门票头」。
        h["x-app"] = "cli"
        h["anthropic-version"] = "2023-06-01"
        h["anthropic-dangerous-direct-browser-access"] = "true"
    if full:
        h.update({
            "x-stainless-lang": "js",
            "x-stainless-runtime": "node",
            "x-stainless-retry-count": "0",
            "x-stainless-timeout": defaults.get("timeout") or _CC_TIMEOUT_DEFAULT,
            "x-stainless-runtime-version": defaults.get("runtime_version") or _CC_RUNTIME_DEFAULT,
            "x-stainless-package-version": defaults.get("package_version") or _CC_PKG_DEFAULT,
            "x-stainless-os": defaults.get("os") or _CC_OS_DEFAULT,
            "x-stainless-arch": defaults.get("arch") or _CC_ARCH_DEFAULT,
            "x-claude-code-session-id": "{uuid1}",
            "accept": "application/json",
            "accept-encoding": "gzip, deflate, br, zstd",
        })
    return h


def _claude_ladder(d: dict) -> list[Profile]:
    """claude 段的画像梯。每档是前一档的超集（见模块 docstring）。"""
    return [
        Profile("baseline", 0, why="站方不查客户端身份"),
        Profile("cc-min", 1,
                headers=_cc_headers(d, betas=_CC_BETAS_MIN),
                why="只查 UA 形态与 claude-code beta"),
        Profile("cc-std", 2,
                headers=_cc_headers(d, betas=_CC_BETAS_STD, std=True),
                why="另查 x-app / anthropic-version（实测 agentrouter 属此档）"),
        Profile("cc-full", 3,
                headers=_cc_headers(d, betas=_CC_BETAS_FULL, std=True, full=True),
                why="另查 X-Stainless SDK 指纹族与会话头"),
        # ── body 档：headers 全套之上再加 metadata.user_id ──
        # 两种形态各一档：站方只认其中一种是实测过的（json 形态是 CPA 生成的，
        # plain 形态是 2026-08-31 手工试通的）。先 json —— 与 CPA 开
        # fingerprint-profile 后的真实行为一致，通了就能直接给出配置建议。
        Profile("cc-body-json", 4,
                headers=_cc_headers(d, betas=_CC_BETAS_FULL, std=True, full=True),
                body_patch={"metadata": {"user_id": _UID_JSON}},
                why="另查 metadata.user_id（CPA 的 JSON 形态）"),
        Profile("cc-body-plain", 4,
                headers=_cc_headers(d, betas=_CC_BETAS_FULL, std=True, full=True),
                body_patch={"metadata": {"user_id": _UID_PLAIN}},
                why="另查 metadata.user_id（下划线拼接形态）"),
        Profile("cc-body-system", 4,
                headers=_cc_headers(d, betas=_CC_BETAS_FULL, std=True, full=True),
                body_patch={"metadata": {"user_id": _UID_JSON},
                            "system": [{"type": "text", "text": _CC_SYSTEM_TEXT,
                                        "cache_control": {"type": "ephemeral"}}]},
                why="另查 Claude Code 的 system 标记块"),
        # ── alt 档：替换型，主梯全败才试 ──
        Profile("browser-ua", 9, family="browser",
                headers={"user-agent": _BROWSER_UA}, alt=True,
                why="站方只认浏览器，不认任何 CLI"),
    ]


def _codex_ladder(d: dict) -> list[Profile]:
    """codex 段。两种客户端形态并列在同一 tier —— TUI 与 vscode 插件的
    UA/Originator 都是真实存在的形态，站方可能只白名单其中一种。
    """
    ua_tui = d.get("codex_user_agent") or _CODEX_UA_DEFAULT
    return [
        Profile("baseline", 0, why="站方不查客户端身份"),
        Profile("originator-only", 1, family="codex",
                headers={"originator": _CODEX_ORIGINATOR_DEFAULT},
                why="只查 Originator（不含版本号，最抗客户端升级）"),
        Profile("codex-tui", 2, family="codex",
                headers={"user-agent": ua_tui,
                         "originator": _CODEX_ORIGINATOR_DEFAULT},
                why="终端 TUI 形态（CPA 转发时的默认值）"),
        Profile("codex-vscode", 2, family="codex",
                headers={"user-agent": _CODEX_UA_VSCODE,
                         "originator": _CODEX_ORIGINATOR_VSCODE},
                why="VSCode 插件形态"),
        Profile("codex-full", 3, family="codex",
                headers={"user-agent": ua_tui,
                         "originator": _CODEX_ORIGINATOR_DEFAULT,
                         "version": "0.146.0",
                         "accept": "application/json",
                         "connection": "Keep-Alive"},
                why="另查 Version 与传输协商头"),
        Profile("browser-ua", 9, family="browser",
                headers={"user-agent": _BROWSER_UA}, alt=True,
                why="站方只认浏览器"),
    ]


def _gemini_ladder(d: dict) -> list[Profile]:
    """gemini 段。CPA 在这一段**不设 UA**（gemini_executor.go 只 set
    x-goog-api-key），所以基线就是「不发 UA」。原来这里挂的是 Originator ——
    那是 codex 独有的头，对 gemini 毫无意义，是四段共用一份表的遗留缺陷。
    """
    return [
        Profile("baseline", 0, why="站方不查客户端身份（CPA 在此段本就不发 UA）"),
        Profile("gemini-cli", 1, family="gemini-cli",
                headers={"user-agent": _GEMINI_CLI_UA},
                why="查 gemini-cli 的 UA"),
        Profile("gemini-cli-full", 2, family="gemini-cli",
                headers={"user-agent": _GEMINI_CLI_UA,
                         "x-goog-api-client": "gl-node/22.14.0 gccl/0.4.0",
                         "accept": "application/json"},
                why="另查 Google SDK 客户端标识头"),
        Profile("browser-ua", 9, family="browser",
                headers={"user-agent": _BROWSER_UA}, alt=True,
                why="站方只认浏览器"),
    ]


def _compat_ladder(d: dict) -> list[Profile]:
    """compat 段。CPA 发死值 `cli-proxy-openai-compat`，不做任何伪装
    （openai_compat_executor.go:155）。

    为什么要带 claude 形态的档：同一个站的 compat 段与 claude 段可能共用一个
    分组，被同一套门禁拦。实测 agentrouter 的 compat 段正是 cc-std 档救活的
    （7 个 Key 从全部 401 变成 5 通过 + 2 欠费）。

    body 档也带上：compat 段发的是 /chat/completions，metadata.user_id 不是
    它的合法字段，但**多一个未知字段不会让通的站变不通**（OpenAI 形态的实现
    普遍忽略未识别字段），而少了它可能让一个能救的站判死。方向上宁可多试。
    """
    return [
        Profile("baseline", 0, why="站方不查客户端身份"),
        Profile("openai-sdk", 1, family="openai-sdk",
                headers={"user-agent": _OPENAI_SDK_UA,
                         "x-stainless-lang": "python"},
                why="查 OpenAI 官方 SDK 指纹"),
        Profile("cc-min", 2, headers=_cc_headers(d, betas=_CC_BETAS_MIN),
                why="与 claude 段共用门禁的站，最省档"),
        Profile("cc-std", 3,
                headers=_cc_headers(d, betas=_CC_BETAS_STD, std=True),
                why="与 claude 段共用门禁（实测 agentrouter compat 属此档）"),
        Profile("cc-full", 4,
                headers=_cc_headers(d, betas=_CC_BETAS_FULL, std=True, full=True),
                why="共用门禁且查 SDK 指纹族"),
        Profile("cc-body-json", 5,
                headers=_cc_headers(d, betas=_CC_BETAS_FULL, std=True, full=True),
                body_patch={"metadata": {"user_id": _UID_JSON}},
                why="共用门禁且查 metadata.user_id"),
        Profile("browser-ua", 9, family="browser",
                headers={"user-agent": _BROWSER_UA}, alt=True,
                why="站方只认浏览器"),
    ]


_LADDERS = {
    "claude-api-key": _claude_ladder,
    "codex-api-key": _codex_ladder,
    "gemini-api-key": _gemini_ladder,
    "openai-compatibility": _compat_ladder,
}


def defaults_from_config(cfg: dict | None) -> dict:
    """从 CPA 自己的配置块派生画像值。读不到就返回空 dict（各处回落内置默认）。

    为什么要读它：`claude-header-defaults`（config_types.go:115-124）是 CPA
    发出 X-Stainless 族与 UA 版本号的真实来源。写死在这里的话，用户改了那个块
    或 CPA 升级后换了默认值，探测发的形态就与 CPA 实际转发的不一致 ——
    那会让「探测通了但 CPA 不通」或反之，两种误判都发生过。
    """
    if not isinstance(cfg, dict):
        return {}
    out: dict[str, str] = {}
    hd = cfg.get("claude-header-defaults")
    if isinstance(hd, dict):
        ua = str(hd.get("user-agent") or "").strip()
        if ua:
            # 只取版本号 —— 形态由模板固定，见模块 docstring。
            import re
            m = re.match(r"^claude-cli/(\d+\.\d+\.\d+)", ua)
            if m:
                out["version"] = m.group(1)
        for src, dst in (("package-version", "package_version"),
                         ("runtime-version", "runtime_version"),
                         ("os", "os"), ("arch", "arch"), ("timeout", "timeout")):
            v = str(hd.get(src) or "").strip()
            if v:
                out[dst] = v
    codex = cfg.get("codex")
    if isinstance(codex, dict):
        chd = codex.get("header-defaults")
        if isinstance(chd, dict):
            v = str(chd.get("user-agent") or "").strip()
            if v:
                out["codex_user_agent"] = v
    return out


def ladder(section: str, cfg: dict | None = None, *,
           include_alt: bool = True, max_tier: int | None = None) -> list[Profile]:
    """取一个段的画像梯，按 tier 升序。第一个通过的档即「最省可用档」。

    max_tier 用来在「只想快速判断有没有门禁」时截断（比如 max_tier=2 只试到
    标准档）。默认不截断 —— 宁可多打几次，也不要把一个能救的站判死。
    """
    if section not in SECTIONS:
        raise ValueError(f"未知段：{section!r}，应为 {SECTIONS}")
    items = _LADDERS[section](defaults_from_config(cfg))
    if not include_alt:
        items = [p for p in items if not p.alt]
    if max_tier is not None:
        items = [p for p in items if p.tier <= max_tier or p.alt]
    # 按 tier 排序而非按族分组：跨族也要由省到全。openai-sdk（t1）排在
    # cc-min（t2）之前，是因为 compat 段的站更可能是 OpenAI 形态 ——
    # 先试便宜且更可能命中的那个。
    return sorted(items, key=lambda p: (p.tier, p.name))


def families(section: str, cfg: dict | None = None) -> dict[str, list[Profile]]:
    """按客户端族分组，族内按 tier 升序。超集不变式只在族内检查。"""
    out: dict[str, list[Profile]] = {}
    for p in ladder(section, cfg):
        out.setdefault(p.family, []).append(p)
    for v in out.values():
        v.sort(key=lambda p: (p.tier, p.name))
    return out


def render(obj, api_key: str, ctx: dict | None = None):
    """把模板变量按当前 Key 求值。深拷贝，不改原对象。

    {key_hash} 取该 Key 的 sha256 前 64 位十六进制 —— device_id 必须随 Key 变
    （cloak_utils.go:12 要求 ^[a-f0-9]{64}$）。写死等于所有 Key 共用一个假身份。
    {uuid1} = 会话 UUID，{uuid2} = 账号 UUID。每次调用新生成。
    """
    if ctx is None:
        ctx = {
            "key_hash": hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:64],
            "uuid1": str(uuid.uuid4()),
            "uuid2": str(uuid.uuid4()),
        }
    if isinstance(obj, str):
        out = obj
        for k, v in ctx.items():
            out = out.replace("{" + k + "}", v)
        return out
    if isinstance(obj, dict):
        return {k: render(v, api_key, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, api_key, ctx) for v in obj]
    return obj


def materialize(prof: Profile, api_key: str) -> tuple[dict[str, str], dict]:
    """把一档画像对当前 Key 求值，返回 (headers, body_patch)。

    两者共用同一份 ctx —— x-claude-code-session-id 与 metadata.user_id 里的
    session_id 必须是**同一个** UUID，那是真实客户端的行为
    （claude_executor_request.go:947-949 明确说 header 与 body 传同一个
    agent-conversation UUID）。分开求值会得到两个不同的 UUID，站方一比就露。
    """
    ctx = {
        "key_hash": hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:64],
        # 模板变量语义固定：uuid1 = 会话、uuid2 = 账号。headers 与 body_patch
        # 共用同一份 ctx，所以 x-claude-code-session-id 与 metadata 里的
        # session_id 天然是同一个值。别再引入第三个「session 别名」变量 ——
        # 那种写法在某个模板忘了跟着改时会静默产生两个不同的 UUID。
        "uuid1": str(uuid.uuid4()),
        "uuid2": str(uuid.uuid4()),
    }
    return (render(prof.headers, api_key, ctx),
            render(prof.body_patch, api_key, ctx))


def config_advice(section: str, prof: Profile) -> tuple[dict[str, str], list[str]]:
    """这一档要怎么落到 config.yaml。返回 (要写的 headers, 额外提示)。

    body_patch 不能写进 config.yaml —— 那是请求**体**字段，条目只支持 headers
    （config_types.go 四段的 Headers 都是 map[string]string）。claude 段的解法是
    `fingerprint-profile: claude-code-cli`，让 CPA 自己在转发时注入
    （claude_fingerprint_policy.go:96 -> applyClaudeCLIIdentity）。其余三段
    没有这个字段，body 档通过也无法落地 —— 必须如实报告，不能假装能写。
    """
    if prof.is_baseline:
        return {}, []
    hdrs = dict(prof.headers)
    notes: list[str] = []
    if prof.body_patch:
        if section == "claude-api-key":
            notes.append(
                "该站还要 metadata.user_id（请求体字段，headers 表达不了）。"
                "在这个条目上设 `fingerprint-profile: \"claude-code-cli\"`，"
                "由 CPA 转发时注入；仍不通再把 `cloak.mode` 设成 `always`。")
        else:
            notes.append(
                f"该站要 metadata.user_id，但 {section} 段没有 fingerprint-profile "
                "字段（CPA 只在 claude-api-key 上支持它），配置层无法表达 —— "
                "只能人工接管或放弃该段。")
    if prof.alt:
        notes.append("这是替换型画像（非 CLI 形态），与 CPA 转发时的默认 UA 冲突："
                     "写进 headers 会覆盖 CPA 的值，需确认该站确实只认这个形态。")
    return hdrs, notes
