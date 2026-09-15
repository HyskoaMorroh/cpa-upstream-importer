"""从 CLIProxyAPI 源码提取身份头常量，检测画像梯是否已漂移。

为什么需要这个（也为什么它只能做到这一步）
----------------------------------------
画像梯有两层内容，可自动化程度完全不同：

  **值** —— UA 版本号、X-Stainless 族、anthropic-beta 各项。这些在 CPA 源码里
  是 const 块与 []string 切片，形态稳定、可解析。CPA 升级换了值，这里能发现。

  **档次划分** —— 「cc-min 含哪三个头」这种。CPA 源码里**没有**这个信息：
  它只知道自己转发时发什么，从不问「少发几个行不行」。而画像梯的全部意义
  正是问这个 —— config.yaml 里的 headers 越少越稳（站方改门禁时越不容易
  整体失效）。分档依据是实测出来的站方行为，不是 CPA 的数据。

所以这个模块只做三件事，不做第四件：
  1. 提取 CPA 的常量值
  2. 与 profiles.py 用的值比对，报漂移
  3. 给出「CPA 完整头集合」供最全档参考

它**不**生成档次划分。那需要实测。

用法
----
    python3 -m cpa_probe.cpa_source_probe /path/to/CLIProxyAPI

读不到源码时返回空结果 —— 这个模块是可选增强，不是运行前提。
"""

from __future__ import annotations

import io
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field


# CPA 源码里这几个文件是身份头的权威来源。路径相对仓库根。
_CLAUDE_REQ = "internal/runtime/executor/claude_executor_request.go"
_CODEX_REQ = "internal/runtime/executor/codex_executor_request.go"
# codex 请求体的强制改写在 execute 里，不在 request 里 —— 两个文件都要读。
_CODEX_EXEC = "internal/runtime/executor/codex_executor_execute.go"
# claude 段身份类配置的合法取值来源（fingerprint-profile / cloak.mode）
_CLAUDE_FP = "internal/config/claude_fingerprint_profile.go"
_CFG_TYPES = "internal/config/config_types.go"

# CPAMP（CPA-Manager-Plus）侧：停用语义的权威来源。
# key 类段的通配符与 compat 段的布尔字段名分别在这两个文件里。
_CPAMP_UTILS = "apps/web/src/components/providers/utils.ts"
_CPAMP_TYPES = "apps/web/src/types/provider.ts"
_CPAMP_RAW_BASE = "https://raw.githubusercontent.com/seakee/CPA-Manager-Plus"

# sub2api 侧：claude_code_only 分组的**校验判据**权威来源（2026-09-14 加）。
#
# 为什么需要它（用户第 1 条问的那个 503）
# ------------------------------------
# 同一个站填进 cc switch 用 Claude Code 直连正常、经 CPA 就 503
# `No available accounts: this group only allows Claude Code clients`。
# 拒绝发生在 sub2api 侧的 ClaudeCodeValidator，它校验四项：
#   · User-Agent 匹配 `claude-cli/x.y.z`
#   · system prompt 与官方模板的 Dice 相似度 >= 阈值
#   · anthropic-beta / anthropic-version / X-App 头非空
#   · metadata.user_id 存在
# 本项目对应的处置是给条目写 `cloak.mode` + `fingerprint-profile`
# （见 plan.py 的 claude 段身份分支）—— 那两个值的**合法取值**已从 CPA
# 源码解析，但「补成什么样才算过」的判据此前只写在注释里，是死知识。
#
# 拉它是为了让判据跟着上游走：sub2api 改了 UA 正则或相似度阈值，
# 本项目的探测形态与警告文案自动跟上，不用手工同步。
_SUB2API_VALIDATOR = "backend/internal/service/claude_code_validator.go"
_SUB2API_RAW_BASE = "https://raw.githubusercontent.com/Wei-Shaw/sub2api"

# 远程模式：直接从 GitHub 拉这两个文件。
#
# 为什么值得单独有这条路：本地源码模式要么 git clone（宿主机加一条 cron、
# 容器加一个挂载），要么手工下 zip（没有 .git，版本比对失效）。而实际部署
# 常常只有 docker-compose.yml + config.yaml + .env + nginx.conf 这几个文件 ——
# 为了一个只读检查去铺一套源码同步，成本不成比例。
#
# 现使用 SOURCE_MANIFEST 的有界清单；不 clone、不执行远程代码。
_RAW_BASE = "https://raw.githubusercontent.com/router-for-me/CLIProxyAPI"
_GH_API = "https://api.github.com/repos/router-for-me/CLIProxyAPI"

# 远程结果缓存。GitHub 对未认证请求限 60 次/小时，而这个检查在每次打开
# 网页时都会跑 —— 不缓存会很快撞限额，撞了之后检查静默失效。
# 6 小时：CPA 不会一天改几次身份头。
_REMOTE_TTL = 6 * 3600
# **失败也要缓存**（2026-09-02 实测）。原来只在成功时写缓存，于是拉不通的
# 环境每次打开网页都重付一遍超时 —— 国内 VPS 直连 raw.githubusercontent
# 不通，实测每次干等 15 秒且第二次没有变快。
#
# TTL 比成功短得多：网络故障通常是暂时的（代理刚起、DNS 抖动），
# 不该像成功结论那样压 6 小时。10 分钟足够挡住「反复刷新页面」这个场景。
_REMOTE_FAIL_TTL = 600
_remote_cache: dict = {"at": 0.0, "ident": None, "ref": "", "ok": False}

# commit 号也要缓存。原来每次调用都走网络（实测连打三次都是 0.47s），
# 而它与 extract_remote 打的是同一个 GitHub，同样会在拉不通时干等。
_COMMIT_TTL = 6 * 3600
_COMMIT_FAIL_TTL = 600
_commit_cache: dict = {"at": 0.0, "commit": None, "ref": ""}


@dataclass
class CpaIdentity:
    """从 CPA 源码提取到的身份常量。"""

    # claude 段：无条件发送的 beta 项（按 wire 顺序）
    claude_betas_unconditional: list[str] = field(default_factory=list)
    # claude 段：有条件发送的 beta（键 = 常量名，值 = beta 字符串）
    claude_betas_conditional: dict[str, str] = field(default_factory=dict)
    # codex 段的 UA 与 originator
    codex_user_agent: str = ""
    codex_originator: str = ""
    # codex 段 CPA 会**强制写死**的请求体字段（键 -> 字面值），
    # 以及它会**删掉**的字段。两者都从 codex_executor_execute.go 解析。
    # 用途：让探测形态跟着 CPA 升级自动对齐，而不是在 request.py 里写死。
    # 见 `codex_body_shape()`。
    codex_body_force: dict[str, str] = field(default_factory=dict)
    codex_body_drop: list[str] = field(default_factory=list)
    # 四段各自的无条件请求体改写：{段名: (强制字段 dict, 删除字段 list)}。
    # 上面两个 codex_body_* 是它在 codex 段的旧字段名，保留不动（向后兼容，
    # 已有调用方与测试在用）。第 1 条要求四段都对齐，不只 codex。
    body_shape: dict = field(default_factory=dict)
    # claude 段身份类配置的合法取值（从 CPA 源码解析，不写死）。
    # 决定我们能不能给条目写 `cloak` / `fingerprint-profile` —— CPA 的写入
    # 路径会用 ValidateClaudeFingerprintProfile 拒绝不认识的值。
    claude_fingerprint_profiles: list[str] = field(default_factory=list)
    claude_cloak_modes: list[str] = field(default_factory=list)
    # CPAMP 表达「停用」的两套写法（key 类段的通配符 / compat 段的布尔字段）。
    # 从 CPA-Manager-Plus 的前端源码解析，供 bulk.py 使用。
    cpamp_disable_all_rule: str = ""
    cpamp_disabled_field: str = ""
    # sub2api 的 claude_code_only 校验判据（2026-09-14）。空 = 没拉到，
    # 那时 plan.py 的警告文案退回泛化措辞，不猜具体阈值。
    #
    # 用途：判死原因是「客户端」类（503 only allows Claude Code clients）时，
    # 把「上游到底在校验什么」写进 warning，让操作员知道 cloak 要补齐哪几项。
    s2a_ua_pattern: str = ""            # 如 `^claude-cli/\d+\.\d+\.\d+`
    s2a_prompt_threshold: float = 0.0   # Dice 相似度阈值，如 0.5
    s2a_system_prompts: list[str] = field(default_factory=list)
    s2a_required_headers: list[str] = field(default_factory=list)
    source_root: str = ""
    errors: list[str] = field(default_factory=list)
    body_rules: dict = field(default_factory=dict)
    coverage: dict[str, str] = field(default_factory=dict)
    snapshot_id: str = ""
    revisions: dict[str, str] = field(default_factory=dict)
    checked_at: float = 0.0
    expires_at: float = 0.0
    immutable: bool = False
    uncertainty: list[str] = field(default_factory=list)
    claude_header_defaults: dict[str, str] = field(default_factory=dict)
    cpamp_disable_semantics: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (bool(self.coverage) and not self.errors
                and all(v == "covered" for v in self.coverage.values()))


def _read(root: str, rel: str) -> str:
    p = os.path.join(root, rel)
    try:
        with io.open(p, encoding="utf-8", errors="replace") as handle:
            text = handle.read(_SOURCE_MAX_BYTES + 1)
        return text if len(text.encode("utf-8")) <= _SOURCE_MAX_BYTES else ""
    except OSError:
        return ""


def source_commit(root: str) -> str:
    """源码目录当前的 git commit（短）。读不到返回空串。

    为什么要它：挂进来的是**源码**，而跑着的是**编译产物**。`git pull` 之后
    源码变了但 CPA 容器没重启时，漂移检测会按新源码判断，而实际转发的仍是旧
    二进制 —— 那种不一致比不检测更容易误导。

    拿它与 CPA 管理响应头里的 X-CPA-COMMIT 比对就能发现这种情形。
    不解析 packed-refs / worktree 等复杂情形：读不到就返回空，调用方降级。
    """
    head = _read(root, ".git/HEAD").strip()
    if not head:
        return ""
    if head.startswith("ref:"):
        ref = head[4:].strip()
        sha = _read(root, os.path.join(".git", *ref.split("/"))).strip()
        if not sha:
            # packed-refs 的情形
            packed = _read(root, ".git/packed-refs")
            for line in packed.splitlines():
                if line.endswith(" " + ref):
                    sha = line.split()[0]
                    break
        head = sha
    return head[:12] if re.fullmatch(r"[0-9a-f]{40}", head) else ""


def _const_map(src: str) -> dict[str, str]:
    """解析 Go 的 const 块：`name = "value"`（含对齐空格）。"""
    out: dict[str, str] = {}
    for m in re.finditer(r'^\s*(\w+)\s*=\s*"([^"]+)"', src, re.M):
        out[m.group(1)] = m.group(2)
    return out


def _slice_items(src: str, var: str, consts: dict[str, str]) -> list[str]:
    """解析 `var X = []string{ ... }`，元素可以是字面量或常量名。"""
    m = re.search(rf'var\s+{re.escape(var)}\s*=\s*\[\]string\{{(.*?)\}}',
                  src, re.S)
    if not m:
        return []
    items: list[str] = []
    for line in m.group(1).splitlines():
        line = line.split("//")[0].strip().rstrip(",").strip()
        if not line:
            continue
        if line.startswith('"') and line.endswith('"'):
            items.append(line[1:-1])
        elif line in consts:
            items.append(consts[line])
    return items


_IDENT_TTL = 6 * 3600
_ident_cache: dict = {"at": 0.0, "ident": None}


def cached_identity(*, source_root: str = "", ttl: int = _IDENT_TTL,
                    cpamp_root: str | None = None, ref: str | None = None,
                    cpamp_ref: str | None = None, proxy: str | None = None
                    ) -> "CpaIdentity":
    """当前的上游常量快照，带进程内缓存。

    这是给**运行期消费者**（bulk.py 的停用语义、request.py 的 codex 形态）
    用的统一入口 —— 它们每次操作都要问一次，不能各自去读盘/发网络。

    取值顺序：
      1. 显式传入的 source_root
      2. 环境变量 `CPA_SOURCE_ROOT`（部署里挂载 CPA 源码时设）
      3. 远程 GitHub（`extract_remote`，自带 6 小时缓存与失败缓存）

    任何一步失败都返回一个**空的** CpaIdentity，而不是抛异常 ——
    调用方一律按「拿不到就用内置默认」处理。这个模块是可选增强，
    不是运行前提（与模块 docstring 的原则一致）。
    """
    root = source_root or os.environ.get("CPA_SOURCE_ROOT", "")
    panel = cpamp_root if cpamp_root is not None else os.environ.get("CPAMP_SOURCE_ROOT", "")
    if not root:
        return extract_remote(ref=ref or os.environ.get("CPA_SOURCE_REF", "main"),
                              cpamp_ref=cpamp_ref or os.environ.get("CPAMP_SOURCE_REF", "main"),
                              sub2api_ref=os.environ.get("SUB2API_SOURCE_REF", "main"),
                              proxy=proxy)
    # Read the bounded manifest to detect dirty trees and ZIP roots too. Never
    # complete a local revision with unrelated remote traits.
    sources = _local_sources(root, panel)
    key = (os.path.realpath(root), os.path.realpath(panel) if panel else "",
           source_commit(root), source_commit(panel) if panel else "",
           _snapshot(sources), ref, cpamp_ref)
    now = time.time()
    entry = _ident_cache.get(key)
    if entry and now < entry.expires_at:
        return entry
    ident = _parse_sources(sources, source_root=root)
    ident.revisions = {"cpa": key[2], "cpamp": key[3]}
    ident.snapshot_id = hashlib.sha256(repr(key).encode()).hexdigest()
    if ref or cpamp_ref:
        ident.uncertainty.append("Local roots are inspected as-is; requested refs are not checked out.")
    ident.expires_at = now + (ttl if ident.ok else min(ttl, _REMOTE_FAIL_TTL))
    _cache_put(_ident_cache, key, ident)
    return ident


def parse_codex_body_shape(src: str) -> tuple[dict[str, str], list[str]]:
    """从 `codex_executor_execute.go` 解析 CPA 对 codex 请求体做的强制改写。

    为什么必须从源码解析而不是写死（用户 2026-09-11 的要求）
    ----------------------------------------------------
    探测要问的是「CPA 这样发，这个站收不收」。CPA 每次升级都可能增删强制字段
    —— 2026-09-10 实测就发现它无条件 `stream=true`、注入 image_generation 工具、
    补空 `instructions`、删掉五个字段。这些若写死在 `request.py` 里，CPA 一升级
    探测形态就与真实转发脱节，而脱节的表现是**静默误判**（把可用站判死，或把
    不可用站判活），不会报错。

    解析两类调用：
        helps.SetBoolIfDifferent(body, "stream", true)   -> 强制 stream=true
        helps.SetStringIfDifferent(body, "model", x)     -> 强制字段（值取变量名）
        sjson.DeleteBytes(body, "previous_response_id")  -> 删除该字段

    只认作用在 `body` 这个变量上的调用 —— 同一文件里还有作用在
    `translatedReq` / `payload` 等别的变量上的同名调用，那些是别的路径。

    解析不出来时返回空 —— 调用方要按「拿不到就沿用内置默认」处理，
    这个模块是可选增强，不是运行前提（与模块 docstring 的原则一致）。
    """
    return parse_body_shape(src)


# 四段各自的 executor 文件与「请求体变量名」。
# 变量名不同是因为 compat 段的翻译产物叫 translated，其余三段叫 body ——
# 解析必须认准变量，否则会把作用在别的变量上的同名调用也算进来。
_BODY_SHAPE_SOURCES: dict[str, tuple[str, str]] = {
    "codex-api-key": ("internal/runtime/executor/codex_executor_execute.go", "body"),
    "claude-api-key": ("internal/runtime/executor/claude_executor_execute.go", "body"),
    "gemini-api-key": ("internal/runtime/executor/gemini_executor.go", "body"),
    "openai-compatibility": (
        "internal/runtime/executor/openai_compat_executor.go", "translated"),
}


def parse_body_shape(src: str, var: str = "body"
                     ) -> tuple[dict[str, str], list[str]]:
    """通用版：解析某个 executor 对请求体做的**无条件**改写。

    `parse_codex_body_shape` 是它在 codex 段的特例（保留那个名字是因为
    已有调用方与测试在用；两者共用这一份实现，不许再分叉）。

    为什么四段都要（第 1 条：codex / gemini / claude / openai **都有**大量
    站点在 cc-switch 能用、经 CPA 就不通）：探测要问的是「CPA 这样发通不通」，
    而 CPA 对每一段都有自己的无条件改写。逐段实测提取到的差异：

        codex   强制 stream=true；删 previous_response_id / generate /
                prompt_cache_retention / safety_identifier / stream_options
        gemini  强制 stream=true；删 session_id
        compat  include_usage 仅流式入口；缓存字段依配置与会话条件
        claude  无强制；删 diagnostics

    compat 非流式请求不能混入流式入口的 include_usage 字段。

    只返回非流式入口适用的已确认调用。带未知 if 条件的改写（如 claude 的
    `ensureModelMaxTokens` 只在 max_tokens 缺席时补）不在这里 ——
    那类要看运行时状态，静态解析给不出确定结论，硬套反而会引入新的形态错位。
    """
    return resolve_body_rules(parse_body_rules(src, var))


def parse_body_rules(src: str, var: str = "body") -> list[dict]:
    """Lex Go scopes; accept only known entrypoints and simple boolean guards.

    This is deliberately not a Go interpreter or a call-graph proof. Unhandled
    scopes, helper functions and nonliteral values remain visible as unknown.
    """
    tokens = list(re.finditer(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|`[^`]*`|'
        r"'(?:\\.|[^'\\])*'|[A-Za-z_]\w*|[^\s]", src or "", re.S))
    tokens = [t for t in tokens if not t.group().startswith(("//", "/*"))]
    scopes = []
    statement = []
    rules = []
    for i, token in enumerate(tokens):
        value = token.group()
        if value == "{":
            text = " ".join(statement)
            fn = re.search(r'\bfunc\s+(?:\([^)]*\)\s*)?(\w+)\s*\(', text)
            if fn:
                scopes.append(("function", fn.group(1)))
            else:
                scopes.append(("guard", text))
            statement = []
            continue
        if value == "}":
            if scopes:
                scopes.pop()
            statement = []
            continue
        if value == ";":
            statement = []
            continue
        if value in ("SetBoolIfDifferent", "SetStringIfDifferent", "DeleteBytes",
                     "SetBytes", "SetRawBytes", "SetIntIfDifferent"):
            tail = tokens[i + 1:i + 10]
            args = [t.group() for t in tail]
            if len(args) >= 5 and args[:3] == ["(", var, ","]:
                try:
                    path = json.loads(args[3])
                except (ValueError, TypeError):
                    path = None
                if isinstance(path, str):
                    raw = "" if value == "DeleteBytes" else (args[5] if len(args) > 5 else "")
                    literal = (value == "DeleteBytes" or
                               (len(args) > 6 and args[6] == ")" and
                                (raw in ("true", "false") or raw.startswith('"'))))
                    conditions = {}
                    unknown = []
                    if not re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", path):
                        unknown.append("unsupported-sjson-path")
                    if value not in ("SetBoolIfDifferent", "SetStringIfDifferent", "DeleteBytes"):
                        unknown.append("unsupported-operation:" + value)
                    for kind, text in scopes:
                        if kind == "function":
                            if text == "ExecuteStream":
                                conditions["entrypoint"] = "ExecuteStream"
                            elif text == "Execute":
                                conditions["entrypoint"] = "Execute"
                            else:
                                unknown.append("helper:" + text)
                                if text == "applyPromptCacheKey" and re.search(
                                        r"compat\s*==\s*nil\s*\|\|\s*!compat\.SupportPromptCacheKey",
                                        src):
                                    conditions["support-prompt-cache-key"] = True
                        else:
                            guard = re.sub(r"\s+", "", text)
                            if guard in ("ifstream", "if!stream"):
                                conditions["stream"] = guard == "ifstream"
                            elif guard in ("ife.cfg.SupportPromptCacheKey",
                                           "if!e.cfg.SupportPromptCacheKey"):
                                conditions["support-prompt-cache-key"] = "!" not in guard
                            else:
                                unknown.append("guard:" + text[-160:])
                    if not literal:
                        unknown.append("nonliteral:" + raw)
                    rules.append({"path": path, "op": "drop" if value == "DeleteBytes" else "set",
                                  "value": raw, "conditions": conditions,
                                  "unknown": unknown})
        # Keep only the current line for control prefixes, but retain multiline
        # function signatures. Strings/comments cannot introduce braces.
        if i and "\n" in src[tokens[i - 1].end():token.start()] and "func" not in statement:
            statement = []
        statement.append(value)
    return rules


def resolve_body_rules(rules: list[dict], *, stream: bool = False,
                       config: dict | None = None) -> tuple[dict[str, str], list[str]]:
    force, drop = {}, []
    context = {"support-prompt-cache-key": False, **(config or {}), "stream": stream,
               "entrypoint": "ExecuteStream" if stream else "Execute"}
    for rule in rules:
        if rule["unknown"] or any(context.get(k) != v
                                  for k, v in rule["conditions"].items()):
            continue
        path = rule["path"]
        if rule["op"] == "drop":
            force.pop(path, None)
            if path not in drop:
                drop.append(path)
        else:
            if path in drop:
                drop.remove(path)
            force[path] = rule["value"]
    return force, drop


def forces_stream(identity, section: str) -> bool | None:
    """CPA 转发这一段时会不会**无条件**把请求改成流式。

    返回 True / False / None（源码里读不出结论）。

    为什么必须从源码读而不是写死段名（docx 第 6 条：严禁硬编码）
    ------------------------------------------------------
    探测要问的是「CPA 这样发通不通」，所以探测的请求形态必须与 CPA 实际
    转发的形态一致。而「哪几段强制流式」是 CPA 的实现细节，会随它的版本变：
    本模块的文档里记着 codex 与 gemini 都强制 `stream=true`，而 pipeline
    那边一直写的是 `section == "codex-api-key"` —— 一处写死的段名，
    CPA 改了它不会跟着变。

    判据取 `body_rules` 里那条 `stream: set true` 的无条件改写 ——
    它本来就是从 executor 源码解析出来的（见 `parse_body_shape`），
    与 `resolve_body_rules` 用同一份数据，不引入第二套判据。

    读不出来时返回 None，让调用方保留自己的保守默认 —— 源码拉不到
    （远程名录不通是常态）不该让探测形态突然变一种。
    """
    rules = (getattr(identity, "body_rules", None) or {}).get(section)
    if rules is None:
        return None
    for rule in rules:
        if rule.get("path") != "stream" or rule.get("unknown"):
            continue
        if rule.get("op") == "set" and str(rule.get("value")).lower() == "true":
            return True
    return False


def parse_claude_identity_opts(fp_src: str, types_src: str) -> dict[str, list[str]]:
    """从 CPA 源码解析 claude 段「身份类」配置项的**合法取值**。

    为什么必须解析而不是写死（第 6 条要求：严禁硬编码）
    ------------------------------------------------
    这两项决定 CPA 会不会替我们补上 Claude Code 的身份（system 块 + 计费块）：

      · `fingerprint-profile` —— 合法值由
        `NormalizeClaudeFingerprintProfile`（claude_fingerprint_profile.go:25）
        的 switch 分支决定。CPA 用 `ValidateClaudeFingerprintProfile` 在写入
        路径上**直接拒绝**不认识的值，所以我们写错一个字这条配置就落不进去。
      · `cloak.mode` —— 合法值写在 `CloakConfig.Mode` 的注释里
        （config_types.go:349-353）：auto / always / never。

    解析不出来时返回空列表，调用方按「拿不到就不写这个字段」处理 ——
    宁可少写一个可选字段，也不要写一个会被 CPA 拒绝的值。
    """
    out: dict[str, list[str]] = {"fingerprint_profile": [], "cloak_mode": []}

    # const ClaudeFingerprintProfileClaudeCodeCLI = "claude-code-cli"
    for m in re.finditer(
            r'ClaudeFingerprintProfile\w*\s*=\s*"([^"]+)"', fp_src or ""):
        v = m.group(1).strip()
        if v and v not in out["fingerprint_profile"]:
            out["fingerprint_profile"].append(v)

    # CloakConfig.Mode 的合法值只在注释里列着：
    #   // Mode controls cloaking behavior: "auto" (default), "always", or "never".
    m = re.search(r'Mode controls cloaking behavior:([^\n]*)', types_src or "")
    if m:
        for v in re.findall(r'"([a-z]+)"', m.group(1)):
            if v not in out["cloak_mode"]:
                out["cloak_mode"].append(v)
    return out


def parse_cpamp_disable_rule(src: str) -> tuple[str, str]:
    """从 CPAMP 前端源码解析「停用」用的两套写法。

    CPAMP 里 key 类段与 openai-compatibility 段表达停用的方式**完全不同**
    （前者往 `excluded-models` 塞通配符，后者用布尔 `disabled`）。写错字段的
    后果是「界面显示已停用、CPA 照常轮询」，比不做还糟，所以这两个值必须跟着
    CPAMP 升级自动同步，而不是抄在我们代码里。

    解析目标（`apps/web/src/components/providers/utils.ts`）：
        export const DISABLE_ALL_MODELS_RULE = '*';

    布尔字段名目前是 `disabled`（`OpenAIProviderConfig` 的字段）；从
    `apps/web/src/types/provider.ts` 里确认它仍然存在，存在就返回该名字，
    不存在就返回空让调用方沿用默认并报漂移。
    """
    rule = ""
    m = re.search(r'DISABLE_ALL_MODELS_RULE\s*(?::\s*\w+)?\s*=\s*[\'"]([^\'"]+)[\'"]',
                  src)
    if m:
        rule = m.group(1)
    field_name = "disabled" if re.search(r'^\s*disabled\??\s*:\s*boolean',
                                         src, re.M) else ""
    return rule, field_name


def parse_sub2api_validator(src: str) -> dict:
    """从 sub2api 的 claude_code_validator.go 解析 claude_code_only 的校验判据。

    为什么要解析而不是抄下来（2026-09-14）
    ----------------------------------
    用户第 1 条问的 503 —— `only allows Claude Code clients` —— 拒绝就发生在
    这个文件里。本项目的处置是给条目写 `cloak.mode` + `fingerprint-profile`，
    让 CPA 自己补齐身份；但「补成什么样才算过」此前只写在 plan.py 的注释里。
    注释不会随上游更新，阈值一改就成了错的知识。

    解析目标（全部是文件里的**具名常量**，不是行为推断）：
        claudeCodeUAPattern    = regexp.MustCompile(`(?i)^claude-cli/\\d+\\.\\d+\\.\\d+`)
        systemPromptThreshold  = 0.5
        claudeCodeSystemPrompts = []string{ "You are Claude Code, ...", ... }
        必需头：r.Header.Get("X-App") / ("anthropic-beta") / ("anthropic-version")

    返回 dict 而不是多个返回值：字段会随上游增删，dict 让调用方按需取、
    缺项就是空，不会因为解包个数对不上而整条链崩掉。

    拿不到任何一项时返回空 dict —— 调用方据此退回泛化文案，绝不猜。
    """
    out: dict = {}

    m = re.search(r'claudeCodeUAPattern\s*=\s*regexp\.MustCompile\(`([^`]+)`\)', src)
    if m:
        out["ua_pattern"] = m.group(1)

    m = re.search(r'systemPromptThreshold\s*=\s*([0-9.]+)', src)
    if m:
        try:
            out["prompt_threshold"] = float(m.group(1))
        except ValueError:
            pass

    # system prompt 模板：`var claudeCodeSystemPrompts = []string{ ... }` 里的
    # 双引号字面量。只取块内的，避免把文件别处的字符串也收进来。
    blk = re.search(r'claudeCodeSystemPrompts\s*=\s*\[\]string\{(.*?)\n\}',
                    src, re.S)
    if blk:
        prompts = re.findall(r'"((?:[^"\\]|\\.)*)"', blk.group(1))
        out["system_prompts"] = [p for p in prompts if len(p) > 12]

    # 必需头：文件里 `r.Header.Get("X")` 后紧跟「空则 return false」的那几个。
    # 只收大小写敏感的原写法，交给调用方去做不区分大小写的比对。
    heads = re.findall(r'r\.Header\.Get\("([\w-]+)"\)', src)
    seen: list[str] = []
    for h in heads:
        if h.lower() != "user-agent" and h not in seen:
            seen.append(h)
    if seen:
        out["required_headers"] = seen

    return out


def parse_cpamp_disable_semantics(utils: str, rows: str) -> dict:
    """Recognize panel enable/disable predicates, not provider weight heuristics."""
    rule, _ = parse_cpamp_disable_rule(utils)
    if not rule:
        return {}
    out = {}
    if re.search(r"enabled:\s*!hasDisableAllModelsRule\(config\.excludedModels\)", rows):
        out.update(native_field="excluded-models", native_disable_value=rule)
    if re.search(r"enabled:\s*provider\.disabled\s*!==\s*true", rows):
        out.update(compat_field="disabled", compat_disable_value=True)
    normalized = re.sub(r"\s+", "", utils)
    if all(part in normalized for part in (
            "!==DISABLE_ALL_MODELS_RULE", "stripDisableAllModelsRule(models)",
            "return[...base,DISABLE_ALL_MODELS_RULE]",
            "withoutDisableAllModelsRule", "returnbase;")):
        out["enable_preserves_exclusions"] = True
    return out


def _http_get(url: str, *, timeout: int = 15,
              proxy: str | None = None) -> tuple[int, str]:
    """GET 一个文本资源。返回 (状态码, 正文)。失败返回 (0, 错误说明)。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={
        # GitHub 对无 UA 的请求会 403
        "User-Agent": "cpa-upstream-importer/drift-check",
        "Accept": "application/vnd.github.raw, text/plain, */*",
    })
    try:
        if proxy:
            op = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            op = urllib.request.build_opener()
        with op.open(req, timeout=timeout) as resp:
            data = resp.read(_SOURCE_MAX_BYTES + 1)
            if len(data) > _SOURCE_MAX_BYTES:
                return 0, "source exceeds size limit"
            return resp.status, data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.reason or f"HTTP {e.code}"
    except Exception as e:                              # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def extract_remote(*, ref: str = "main", timeout: int = 60,
                   proxy: str | None = None,
                   use_cache: bool = True, cpamp_ref: str = "main",
                   sub2api_ref: str = "main") -> CpaIdentity:
    """Fetch the bounded manifest from public GitHub, pinning each repository.

    A successful complete snapshot lives six hours; partial/failure results ten
    minutes. No source is executed. Missing files and unresolved revisions stay
    explicit. The shared fetch budget stops scheduling requests after timeout.

    预算为什么是 60 秒（2026-09-15 实测定）
    ------------------------------------
    清单横跨三个仓库、14 个文件，外加每仓一次 commits 查询解析 revision，
    共 17 次请求。实测单次 1.4–1.9 秒（直连 GitHub）→ 24–32 秒。

    两次实测教训：
      · 原默认 8 秒是两仓库时代的值。加 sub2api 后连 cpa 那一仓都跑不完 ——
        `revisions` 只剩 cpa、cpamp 四个文件全 missing、`immutable=False`，
        而失败缓存 10 分钟，界面上看不出原因。
      · 改 45 秒 + 按仓平均分（15 秒/仓）仍不够：cpa 的 9 个文件要约 17 秒，
        实测 21 个文件 missing、两仓报 deadline exceeded。

    现在按**文件数加权**分配（见下面 `_weights`），60 秒下 cpa 得约 33 秒、
    cpamp 约 17 秒、sub2api 约 7 秒，各自留 1.5 倍以上余量。

    这个预算只在**缓存未命中**时付；命中时是 6 小时一次，代价可忽略。
    拉不通的环境由 `_REMOTE_FAIL_TTL` 兜住，不会每次打开网页都重付。
    """
    from urllib.parse import quote, urlsplit
    now = time.time()
    key = (_RAW_BASE, _CPAMP_RAW_BASE, _SUB2API_RAW_BASE,
           ref, cpamp_ref, sub2api_ref, proxy)
    entry = _remote_cache.get(key)
    if use_cache and entry and now < entry.expires_at:
        _remote_cache.update(at=entry.checked_at, ident=entry, ref=ref, ok=entry.ok)
        return entry
    sources, revisions, issues = {}, {}, []
    # 每仓一份**独立**预算，且按**文件数**加权，不是平均分（2026-09-15）。
    #
    # 共享预算的失效方式：仓库按固定顺序遍历，排第一的 cpa 有 9 个文件，
    # 它慢一点就把后两仓的额度吃光 —— 实测 timeout=25 时 cpamp 三个文件
    # 全 missing、sub2api 整仓没进 revisions，而失败缓存 10 分钟，
    # 界面上只看到「拉不到」，看不出是被前一仓饿死的。
    #
    # 平均分同样不够：45/3=15 秒给 cpa 的 9 个文件（每次 1.4–1.9 秒，
    # 另加一次 commits 查询）必然超，实测 21 个文件 missing。
    # 按文件数 +1（那次 commits 查询）加权后，各仓拿到与工作量匹配的额度。
    _repos = (("cpa", _RAW_BASE, ref),
              ("cpamp", _CPAMP_RAW_BASE, cpamp_ref),
              ("sub2api", _SUB2API_RAW_BASE, sub2api_ref))
    # +1 = 解析 revision 的那次 commits 查询
    _weights = {r: len(SOURCE_MANIFEST[r]) + 1 for r, _b, _q in _repos}
    _total_w = sum(_weights.values()) or 1
    _budget = max(1, timeout)
    for repo, base, requested in _repos:
        deadline = time.monotonic() + _budget * _weights[repo] / _total_w
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com"
                or not re.fullmatch(r"/[\w.-]+/[\w.-]+", parsed.path)):
            issues.append("unsupported public source repository")
            continue
        revision = requested
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 只放弃**这一仓**，不终止整轮（2026-09-15）。
                # 每仓有独立预算，本仓耗尽与其余仓无关；原来的 break
                # 会让排在后面的仓库一个都拿不到，退回共享预算的老毛病。
                issues.append(repo + ": revision deadline exceeded")
                continue
            status, body = _http_get(
                "https://api.github.com/repos" + parsed.path + "/commits/"
                + quote(requested, safe=""), timeout=remaining, proxy=proxy)
            try:
                revision = json.loads(body).get("sha", "") if status == 200 else ""
            except (ValueError, AttributeError):
                revision = ""
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                issues.append(repo + ": revision unresolved")
                revision = requested
        revisions[repo] = revision
        for rel in SOURCE_MANIFEST[repo]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 跳出本仓剩余文件，外层继续下一仓 —— 本仓预算耗尽
                # 不该连累别的仓库。
                issues.append(repo + ": source fetch deadline exceeded")
                break
            status, text = _http_get(f"{base}/{quote(revision, safe='')}/{rel}",
                                     timeout=remaining, proxy=proxy)
            if status == 200 and len(text.encode("utf-8")) <= _SOURCE_MAX_BYTES:
                sources[(repo, rel)] = text
    out = _parse_sources(sources, source_root=f"github:{_RAW_BASE}@{ref}")
    out.revisions = revisions
    out.snapshot_id = hashlib.sha256(
        (out.snapshot_id + repr((key[:4], revisions))).encode()).hexdigest()
    out.immutable = (len(revisions) == len(_REPO_BASES) and all(
        re.fullmatch(r"[0-9a-f]{40}", r) for r in revisions.values()))
    out.uncertainty.extend(issues)
    if not out.immutable:
        out.errors.append("remote snapshot revision is not pinned")
    out.expires_at = now + (_REMOTE_TTL if out.ok else _REMOTE_FAIL_TTL)
    if use_cache:
        _cache_put(_remote_cache, key, out)
        # Retain the old diagnostics keys used by existing callers/tests.
        _remote_cache.update(at=now, ident=out, ref=ref, ok=out.ok)
    return out


def remote_commit(*, ref: str = "main", timeout: int = 8,
                  proxy: str | None = None,
                  use_cache: bool = True) -> str:
    """拉 GitHub 上该 ref 的最新 commit（短）。拿不到返回空串。

    与本地 .git/HEAD 的作用相同：拿它跟运行中 CPA 的 X-CPA-COMMIT 比，
    发现「上游已更新但你的 CPA 还是旧版」。

    缓存与 extract_remote 同一套策略（成功 6 小时 / 失败 10 分钟）。
    原来完全不缓存 —— 实测连打三次都走网络，而它与 extract_remote 打的是
    同一个 GitHub，拉不通时同样干等一个超时。
    """
    now = time.time()
    cache_key = (_GH_API, ref, proxy)
    if use_cache and _commit_cache["commit"] is not None \
            and _commit_cache.get("key") == cache_key:
        ttl = _COMMIT_TTL if _commit_cache["commit"] else _COMMIT_FAIL_TTL
        if now - _commit_cache["at"] < ttl:
            return _commit_cache["commit"]
    got = _remote_commit_uncached(ref=ref, timeout=timeout, proxy=proxy)
    if use_cache:
        _commit_cache.update(at=now, commit=got, ref=ref, key=cache_key)
    return got


def _remote_commit_uncached(*, ref: str = "main", timeout: int = 8,
                            proxy: str | None = None) -> str:
    """真正发请求的那一半。缓存判定在 remote_commit 里。"""
    st, body = _http_get(f"{_GH_API}/commits/{ref}", timeout=timeout,
                         proxy=proxy)
    if st != 200:
        return ""
    m = re.search(r'"sha"\s*:\s*"([0-9a-f]{40})"', body)
    return m.group(1)[:12] if m else ""


_SOURCE_MAX_BYTES = 1024 * 1024
SOURCE_MANIFEST = {
    "cpa": tuple(dict.fromkeys((
        _CLAUDE_REQ, _CODEX_REQ, _CODEX_EXEC, _CLAUDE_FP, _CFG_TYPES,
        *[value[0] for value in _BODY_SHAPE_SOURCES.values()],
        "internal/runtime/executor/codex_executor_stream.go",
        "internal/runtime/executor/claude_executor_stream.go",
        "internal/runtime/executor/helps/claude_device_profile.go",
        "internal/api/handlers/management/config_apikey_disable.go",
    ))),
    "cpamp": (_CPAMP_UTILS, _CPAMP_TYPES,
              "apps/web/src/components/providers/ProviderTable/rowData.ts",
              "apps/web/src/services/api/providers.ts"),
    "sub2api": (_SUB2API_VALIDATOR,),
}

# 仓库 → raw 基址。遍历与 immutable 判据都按这张表走，加仓库只改这里。
_REPO_BASES = {
    "cpa": _RAW_BASE,
    "cpamp": _CPAMP_RAW_BASE,
    "sub2api": _SUB2API_RAW_BASE,
}


def _cache_put(cache: dict, key, ident: CpaIdentity) -> None:
    if len(cache) >= 40:
        cache.pop(next(iter(cache)))
    cache[key] = ident


def _local_sources(root: str, cpamp_root: str) -> dict:
    return {(repo, rel): _read(base, rel)
            for repo, base in (("cpa", root), ("cpamp", cpamp_root)) if base
            for rel in SOURCE_MANIFEST[repo]}


def _snapshot(sources: dict) -> str:
    return hashlib.sha256(json.dumps(
        sorted((repo, path, value) for (repo, path), value in sources.items() if value),
        ensure_ascii=True).encode()).hexdigest()


def extract(root: str, *, cpamp_root: str | None = None) -> CpaIdentity:
    """Inspect only the bounded public source manifest; never run source code."""
    panel = cpamp_root if cpamp_root is not None else os.environ.get("CPAMP_SOURCE_ROOT", "")
    ident = _parse_sources(_local_sources(root, panel), source_root=root)
    ident.revisions = {"cpa": source_commit(root),
                       "cpamp": source_commit(panel) if panel else ""}
    ident.snapshot_id = hashlib.sha256(
        (ident.snapshot_id + repr((os.path.realpath(root),
                                  os.path.realpath(panel) if panel else "",
                                  ident.revisions))).encode()).hexdigest()
    ident.expires_at = ident.checked_at + (_IDENT_TTL if ident.ok else _REMOTE_FAIL_TTL)
    return ident


def _parse_sources(sources: dict, *, source_root: str) -> CpaIdentity:
    """The only local/remote trait parser. Missing capability is not success."""
    out = CpaIdentity(source_root=source_root, checked_at=time.time(),
                      snapshot_id=_snapshot(sources))
    for repo, paths in SOURCE_MANIFEST.items():
        for path in paths:
            out.coverage[f"file:{repo}:{path}"] = (
                "covered" if sources.get((repo, path)) else "missing")
    src = sources.get(("cpa", _CLAUDE_REQ), "")

    consts = _const_map(src)

    # 无条件发送的部分 —— 对照 claudeCodeCLIBetas 的函数体：
    #   betas = append(betas, claudeCodeBeta)        ← 无条件
    #   for ... claudeCodeCLIConstantBetas           ← 无条件（除 redact-thinking
    #                                                   在 thinking 显式设置时跳过）
    #   betas = append(betas, claudeEffortBeta)      ← 无条件
    #   mid-conversation-system                      ← 非 legacy system reminder 时
    #
    # 其余（oauth / context-1m / advisor / advanced-tool-use / fallback-credit /
    # fast-mode / extended-cache-ttl / cache-diagnosis）都带条件，探测时不该
    # 无条件发 —— 我们用 api-key 而非 oauth，请求体也不带 tools。
    seq: list[str] = []
    if "claudeCodeBeta" in consts:
        seq.append(consts["claudeCodeBeta"])
    seq.extend(_slice_items(src, "claudeCodeCLIConstantBetas", consts))
    if "claudeMidConvSystemBeta" in consts:
        seq.append(consts["claudeMidConvSystemBeta"])
    if "claudeEffortBeta" in consts:
        seq.append(consts["claudeEffortBeta"])
    out.claude_betas_unconditional = seq

    for name in ("claudeOAuthBeta", "claudeContext1MBeta",
                 "claudeAdvisorToolBeta", "claudeAdvancedToolUseBeta",
                 "claudeFallbackCreditBeta", "claudeExtendedCacheTTLBeta",
                 "claudeCacheDiagnosisBeta", "claudeStructuredOutputsBeta",
                 "claudeServerSideFallbackBeta"):
        if name in consts:
            out.claude_betas_conditional[name] = consts[name]

    csrc = sources.get(("cpa", _CODEX_REQ), "")
    if csrc:
        cc = _const_map(csrc)
        for k, v in cc.items():
            lk = k.lower()
            if "useragent" in lk and not out.codex_user_agent:
                out.codex_user_agent = v
            elif "originator" in lk and not out.codex_originator:
                out.codex_originator = v

    # codex 请求体形态：让探测跟着 CPA 升级自动对齐，不写死在 request.py
    exec_src = sources.get(("cpa", _CODEX_EXEC), "")
    if exec_src:
        force, drop = parse_codex_body_shape(exec_src)
        out.codex_body_force, out.codex_body_drop = force, drop
    # 读不到就留空，**不记 errors**：`ok` 的判据是 errors 为空，而这个文件
    # 只影响「探测体形态能否自动跟随」这一项增强，缺了它身份头提取照常可用。
    # 与模块 docstring 的原则一致：可选增强，不是运行前提。
    # （只挂了两个身份头文件的部署、以及测试夹具，都属于这种情形。）

    # 四段的请求体形态 —— 让探测跟着 CPA 升级自动对齐（第 1 条 + 第 6 条）。
    # 读不到某个文件就跳过那一段，调用方按「没有就不改基线」处理。
    for _sec, (_rel, _var) in _BODY_SHAPE_SOURCES.items():
        split = _rel.replace("_execute.go", "_stream.go")
        _src = sources.get(("cpa", _rel), "")
        if split != _rel:
            _src += "\n" + sources.get(("cpa", split), "")
        if _src:
            out.body_rules[_sec] = parse_body_rules(_src, _var)
            out.body_shape[_sec] = resolve_body_rules(out.body_rules[_sec])
        rules = out.body_rules.get(_sec, [])
        out.coverage["body:" + _sec] = (
            "missing" if not _src.strip() else
            "partial" if not rules or not re.search(r"\bfunc\b.*\bExecute\b", _src)
            or any(r["unknown"] for r in rules) else "covered")

    # claude 段身份类配置的合法取值 —— 决定我们能不能写 cloak /
    # fingerprint-profile。写错值 CPA 的写入路径会直接拒绝。
    ids = parse_claude_identity_opts(sources.get(("cpa", _CLAUDE_FP), ""),
                                     sources.get(("cpa", _CFG_TYPES), ""))
    out.claude_fingerprint_profiles = ids["fingerprint_profile"]
    out.claude_cloak_modes = ids["cloak_mode"]

    # CPAMP 的停用语义。它与 CPA 是两个仓库，本地模式下允许缺席 ——
    # 多数部署只 clone 了 CPA。缺席时 bulk.py 沿用内置默认并在漂移里报出来。
    u = sources.get(("cpamp", _CPAMP_UTILS), "")
    t = sources.get(("cpamp", _CPAMP_TYPES), "")
    out.cpamp_disable_all_rule = parse_cpamp_disable_rule(u)[0]
    out.cpamp_disabled_field = parse_cpamp_disable_rule(t)[1]
    out.cpamp_disable_semantics = parse_cpamp_disable_semantics(
        u, sources.get(("cpamp",
                       "apps/web/src/components/providers/ProviderTable/rowData.ts"), ""))

    # sub2api 的 claude_code_only 校验判据（2026-09-14）。
    # 拉不到就全空 —— plan.py 那边据此退回泛化文案，不猜阈值。
    s2a = parse_sub2api_validator(
        sources.get(("sub2api", _SUB2API_VALIDATOR), ""))
    out.s2a_ua_pattern = s2a.get("ua_pattern", "")
    out.s2a_prompt_threshold = s2a.get("prompt_threshold", 0.0)
    out.s2a_system_prompts = s2a.get("system_prompts", [])
    out.s2a_required_headers = s2a.get("required_headers", [])
    defaults = _const_map(sources.get(
        ("cpa", "internal/runtime/executor/helps/claude_device_profile.go"), ""))
    for name, value in defaults.items():
        for suffix, key in (("UserAgent", "user-agent"), ("PackageVersion", "package-version"),
                            ("RuntimeVersion", "runtime-version"), ("OS", "os"),
                            ("Arch", "arch")):
            if name.endswith(suffix):
                out.claude_header_defaults[key] = value
    for capability, present in (
        ("claude-betas", out.claude_betas_unconditional),
        ("codex-identity", out.codex_user_agent and out.codex_originator),
        ("fingerprint", out.claude_fingerprint_profiles),
        ("cloak", out.claude_cloak_modes),
        ("cpamp-enable-rules", {"native_field", "native_disable_value", "compat_field",
                               "compat_disable_value", "enable_preserves_exclusions"}
         <= out.cpamp_disable_semantics.keys()),
        ("claude-header-defaults", out.claude_header_defaults),
        ("cpamp-disable", out.cpamp_disable_all_rule and out.cpamp_disabled_field)):
        out.coverage[capability] = "covered" if present else "missing"
    out.uncertainty = [k + ":" + v for k, v in out.coverage.items() if v != "covered"]
    out.uncertainty.append("Bounded static extraction only; runtime and future code are unverified.")

    return out


def source_status(ident: CpaIdentity) -> dict:
    """Serializable evidence metadata; checked does not mean runtime verified."""
    return {"partial": not ident.ok, "coverage": dict(ident.coverage),
            "snapshot_id": ident.snapshot_id, "revisions": dict(ident.revisions),
            "checked_at": ident.checked_at, "expires_at": ident.expires_at,
            "immutable": ident.immutable, "uncertainty": list(ident.uncertainty),
            "errors": list(ident.errors)}


# ---------------------------------------------------------------------------
# 漂移检测
# ---------------------------------------------------------------------------


@dataclass
class Drift:
    """一处漂移。severity: warn = 可能误判，info = 仅供参考。"""

    what: str
    ours: str
    theirs: str
    severity: str = "warn"
    note: str = ""


def compare(ident: CpaIdentity) -> list[Drift]:
    """把提取到的 CPA 常量与 profiles.py 用的值比对。

    只报**方向明确**的差异：
      · 我们发了 CPA 不会无条件发的 beta  → warn（可能被站方按项拦）
      · CPA 无条件发而我们没发的 beta      → warn（可能过不了门禁）
    顺序差异只报 info —— HTTP 头值的项顺序站方一般不敏感，而 CPA 自己也按
    请求内容重排。
    """
    from . import profiles

    drifts: list[Drift] = []
    if not ident.claude_betas_unconditional:
        return drifts

    theirs = ident.claude_betas_unconditional
    ours_full = profiles._CC_BETAS_FULL.split(",")

    tset, oset = set(theirs), set(ours_full)
    cond = set(ident.claude_betas_conditional.values())

    for extra in sorted(oset - tset):
        note = ""
        sev = "warn"
        if extra in cond:
            names = [k for k, v in ident.claude_betas_conditional.items()
                     if v == extra]
            note = (f"CPA 只在特定条件下发它（{', '.join(names)}）—— "
                    f"我们用 api-key 探测，不满足那个条件")
        drifts.append(Drift(
            what=f"anthropic-beta 多发 {extra}",
            ours=extra, theirs="（不发）", severity=sev, note=note))

    for missing in sorted(tset - oset):
        drifts.append(Drift(
            what=f"anthropic-beta 少发 {missing}",
            ours="（不发）", theirs=missing, severity="warn",
            note="CPA 无条件发这一项，画像梯的最全档应当包含它"))

    if theirs and ours_full and tset == oset and theirs != ours_full:
        drifts.append(Drift(
            what="anthropic-beta 项顺序不同",
            ours=",".join(ours_full), theirs=",".join(theirs),
            severity="info",
            note="项集合一致，仅顺序不同。站方一般不敏感"))

    return drifts


def compare_config(cfg: dict | None) -> list[Drift]:
    """不读源码时的退路：拿 config.yaml 的 header-defaults 比对。

    为什么需要这条路：容器里只挂了 config.yaml（挂整个目录等于把 .env、
    secrets/ 一起递进去），读不到 CPA 源码。而 `claude-header-defaults` 是
    CPA 自己写在配置里的默认值，同样能反映升级后的变化 —— 只是覆盖面小，
    只有 UA 版本与 X-Stainless 族，管不到 beta 清单。

    覆盖不到的部分不假装检查：宁可报「这几项无法核对」，也不要让人以为
    全都比过了。
    """
    from . import profiles

    drifts: list[Drift] = []
    if not isinstance(cfg, dict):
        return drifts
    hd = cfg.get("claude-header-defaults")
    if not isinstance(hd, dict):
        return drifts

    # 比的是「配置里的值」与「我们的内置回落常量」，**不是**与运行时派生值 ——
    # 后者本来就从配置读（defaults_from_config），拿它比永远相等，等于没检查。
    #
    # 真正要答的问题是：CPA 侧已经换了值，而我们抄录的内置常量还是旧的吗？
    # 内置常量只在配置读不到时才生效，所以差异本身不是故障，是「该更新抄录了」
    # 的信号 —— 严重度 info。
    pairs = (
        ("user-agent", "UA 版本号", profiles._CC_VERSION_DEFAULT, True),
        ("package-version", "x-stainless-package-version",
         profiles._CC_PKG_DEFAULT, False),
        ("runtime-version", "x-stainless-runtime-version",
         profiles._CC_RUNTIME_DEFAULT, False),
        ("os", "x-stainless-os", profiles._CC_OS_DEFAULT, False),
        ("arch", "x-stainless-arch", profiles._CC_ARCH_DEFAULT, False),
        ("timeout", "x-stainless-timeout", profiles._CC_TIMEOUT_DEFAULT, False),
    )
    for src, label, builtin, is_ua in pairs:
        want = str(hd.get(src) or "").strip()
        if not want:
            continue
        if is_ua:
            m = re.match(r"^claude-cli/(\d+\.\d+\.\d+)", want)
            want = m.group(1) if m else ""
            if not want:
                continue
        if want != str(builtin):
            drifts.append(Drift(
                what=label, ours=str(builtin), theirs=want, severity="info",
                note=("config.yaml 的 claude-header-defaults 更新了这个值，"
                      "内置回落常量还是旧的。当前探测会用配置里的新值，"
                      "所以不影响本次 —— 但配置被清空时会回落到旧值")))

    return drifts


def report(root: str) -> tuple[CpaIdentity, list[Drift]]:
    ident = extract(root)
    return ident, compare(ident)


def _drift_json(drifts: list[Drift]) -> list[dict]:
    return [{"what": d.what, "ours": d.ours, "theirs": d.theirs,
             "severity": d.severity, "note": d.note} for d in drifts]


def _stale_drift(src_commit: str, runtime_commit: str) -> Drift | None:
    """源码与运行中二进制不是同一版本时的警告。两侧任一缺失就不判。"""
    if not (src_commit and runtime_commit):
        return None
    if (runtime_commit.startswith(src_commit[:7])
            or src_commit.startswith(runtime_commit[:7])):
        return None
    return Drift(
        what="源码与运行中的 CPA 不是同一版本",
        ours=f"源码 {src_commit}", theirs=f"运行中 {runtime_commit}",
        severity="warn",
        note=("下面的比对是按**源码**做的，而 CPA 实际转发用的是旧二进制。"
              "请重新构建并重启 CPA，或忽略下面的结论"))


def check(*, source_root: str = "", cfg: dict | None = None,
          runtime_commit: str = "", allow_remote: bool = False,
          remote_ref: str = "main", proxy: str | None = None) -> dict:
    """给服务端调用的统一入口。返回可直接进 JSON 的 dict。

    三条路径，按精度降序尝试：

      1. **本地源码**（source_root）—— 最精确：能区分有条件/无条件 beta，
         能读 .git 拿 commit。需要宿主机 clone + 容器挂载。
      2. **远程拉取**（allow_remote）—— 只拉两个 Go 文件（约 110KB），
         不需要源码目录、不需要 git、不需要额外挂载。适合「VPS 上只有
         compose + config + env + nginx 四个文件」这种部署。缓存 6 小时。
      3. **config.yaml 的 header-defaults** —— 覆盖面小得多（管不到 beta
         清单），但容器里一定有。

    三条都不成立时返回 checked=False 并说明原因 —— 不假装检查过。

    runtime_commit 是 CPA 管理接口回的 X-CPA-COMMIT。给了就与源码 commit 比，
    不一致说明「源码已更新但 CPA 没重启」，此时按源码判断的结论对不上实际
    转发行为，必须提示出来。
    """
    # ── 路径 1：本地源码 ──
    if source_root:
        ident, drifts = report(source_root)
        if ident.claude_betas_unconditional:
            src_commit = ident.revisions.get("cpa", "")
            stale = _stale_drift(src_commit, runtime_commit)
            if stale:
                drifts.insert(0, stale)
            return {
                "checked": True,
                "source": "CPA 源码（本地）",
                "source_root": source_root,
                "source_commit": src_commit,
                "runtime_commit": runtime_commit,
                "stale_binary": bool(stale),
                "betas_unconditional": ident.claude_betas_unconditional,
                "betas_conditional": ident.claude_betas_conditional,
                "codex_user_agent": ident.codex_user_agent,
                "codex_originator": ident.codex_originator,
                "drifts": _drift_json(drifts),
                **source_status(ident),
            }

    # ── 路径 2：远程拉取 ──
    if allow_remote:
        ident = extract_remote(ref=remote_ref, proxy=proxy)
        if ident.claude_betas_unconditional:
            drifts = compare(ident)
            revision = ident.revisions.get("cpa", "")
            src_commit = revision[:12] if re.fullmatch(r"[0-9a-f]{40}", revision) else ""
            stale = _stale_drift(src_commit, runtime_commit)
            if stale:
                stale.note = ("下面的比对是按 GitHub 上的**最新源码**做的，而你"
                              "运行的 CPA 是旧版本。要么升级 CPA，要么把 "
                              "CPA_SOURCE_REF 指到你实际用的那个 tag")
                drifts.insert(0, stale)
            out = {
                "checked": True,
                "source": f"GitHub {remote_ref}（有界源码清单）",
                "source_commit": src_commit,
                "runtime_commit": runtime_commit,
                "stale_binary": bool(stale),
                "betas_unconditional": ident.claude_betas_unconditional,
                "betas_conditional": ident.claude_betas_conditional,
                "codex_user_agent": ident.codex_user_agent,
                "codex_originator": ident.codex_originator,
                "drifts": _drift_json(drifts),
                **source_status(ident),
            }
            # codex 文件拉不到属非致命，但要让人看见
            soft = [e for e in ident.errors if e.startswith("（非致命）")]
            if soft:
                out["soft_errors"] = soft
            return out
        # 远程失败不静默 —— 拿不到就落到路径 3，但把原因带出去
        remote_why = "；".join(ident.errors) or "未知原因"
    else:
        remote_why = ""

    # ── 路径 3：config.yaml 的 header-defaults ──
    drifts = compare_config(cfg)
    if cfg and isinstance(cfg.get("claude-header-defaults"), dict):
        out = {
            "checked": True,
            "source": "config.yaml 的 claude-header-defaults",
            "partial": True,
            "uncovered": ["anthropic-beta 清单（只有源码里有）"],
            "drifts": _drift_json(drifts),
        }
        if remote_why:
            out["remote_failed"] = remote_why
        return out

    why = ("读不到 CPA 源码，config.yaml 里也没有 claude-header-defaults。"
           "画像梯用的是内置常量（从 CPA 源码抄录），无法核对是否已过期")
    if remote_why:
        why = f"远程拉取失败（{remote_why}）；且 " + why
    return {"checked": False, "why": why, "drifts": []}


def _main(argv: list[str]) -> int:
    # Windows 控制台默认 GBK，打不出 ⚠ / ✓。与 tests/run.py 同一套处理。
    import sys as _sys
    for stream in (_sys.stdout, _sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if not argv:
        print("用法：python3 -m cpa_probe.cpa_source_probe <CLIProxyAPI 仓库根>")
        return 2
    ident, drifts = report(argv[0])

    if ident.errors:
        for e in ident.errors:
            print(f"✗ {e}")
        return 1

    print(f"CPA 源码：{ident.source_root}")
    print(f"claude 无条件 beta（{len(ident.claude_betas_unconditional)} 项）：")
    for b in ident.claude_betas_unconditional:
        print(f"    {b}")
    print(f"claude 有条件 beta（{len(ident.claude_betas_conditional)} 项，"
          f"探测时不发）：")
    for name, val in sorted(ident.claude_betas_conditional.items()):
        print(f"    {val:38} {name}")
    if ident.codex_user_agent:
        print(f"codex user-agent：{ident.codex_user_agent}")
    if ident.codex_originator:
        print(f"codex originator：{ident.codex_originator}")

    print()
    if not drifts:
        print("✓ 画像梯与 CPA 源码一致，无漂移")
        return 0

    warn = [d for d in drifts if d.severity == "warn"]
    print(f"发现 {len(drifts)} 处漂移（{len(warn)} 处需处理）：")
    for d in drifts:
        mark = "⚠" if d.severity == "warn" else "·"
        print(f"  {mark} {d.what}")
        if d.note:
            print(f"      {d.note}")
    return 1 if warn else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
