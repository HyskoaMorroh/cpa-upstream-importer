"""四阶段探测流水线。

对每个 (url, key) 候选：
  ① 段归属  —— 四段各打一次，看哪几段通。段决定 URL 形态与协议路径。
  ② 模型发现 —— 先问 /models 目录，再逐个验。compat 段必须探到至少一个，
                留空则该 provider 注册 0 个模型（service_models.go:714-717）。
  ③ 处置    —— 不通的段：先试代理，再试补标识头。
                优先级 proxy-url > headers > 降 priority，绝不用 weight: 0。
  ④ 质量    —— 通的段：验静默换模；可选二分探 max-context-length。

节流：每次请求之间等 gap 秒。relay-b.example 有 bulk probe guard
（60 秒内 4 个不同模型即触发），gap 默认 3 秒。

成本：不开上下文探测时单候选约 10-25 次请求，body 都很小。开上下文探测后
每个 (段, 模型) 多 4-6 次大 body 请求 —— 那部分明确要算钱，默认只对
新增候选的首个模型跑。
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

# 直接导入函数，不要写 `from . import classify as cls`。
# __init__.py 里 `from .classify import classify` 会把包属性 cpa_probe.classify
# 从「模块」改写成「函数」，且它排在 `from .pipeline import ...` 之前 ——
# 那样 cls 拿到的是函数，cls.classify(...) 必然 AttributeError。
# 这条路径只有真发请求时才会走到，纯逻辑用例覆盖不到，所以必须写死成函数导入。
from . import betas
from .classify import body_excerpt as _body_excerpt
from .classify import classify as _classify
from .classify import has_error_envelope as _has_error_envelope
from .classify import time_window as _time_window
from . import client, fingerprint, profiles, request
from .parse import SECTIONS, ParsedRow, base_for_section, host_of

# 每段的种子模型。/models 目录拿不到时兜底；拿到目录时用来定验证顺序。
# 按段分开 —— claude 段问 gpt-5.6-sol 必然 404，那是 CPA 的段语义决定的。
SEED_MODELS: dict[str, list[str]] = {
    "gemini-api-key": ["gemini-2.5-pro", "gemini-2.5-flash"],
    "codex-api-key": ["gpt-5.6-sol", "gpt-5.6-terra"],
    "claude-api-key": ["claude-opus-5", "claude-sonnet-5"],
    "openai-compatibility": ["gpt-5.6-sol", "claude-opus-5", "gemini-2.5-pro"],
}

# 只保留这三类 —— 用户 2026-08-29 定的规则：
# 「模型类型只能是在 gemini、gpt、claude 这三种类型中的才保留」
MODEL_PREFIX_WHITELIST = ("gemini", "gpt", "claude")

# OpenAI 的推理系列不叫 gpt-*，但它属于上面规则里的「gpt 那一类」。
# 2026-08-31 实测：o1 / o3-mini 被上面的前缀白名单丢掉 —— 那是规则**想留
# 却漏掉**的，不是有意排除（deepseek / grok / qwen / glm / kimi 才是有意排除）。
#
# 用正则而不是前缀元组：`o1`、`o3`、`o4-mini` 这类是「字母 o + 数字」开头，
# 而 `openai-xxx`、`omni-xxx` 不该命中，单纯 startswith("o") 会误收。
_OPENAI_REASONING_RE = re.compile(r"^o\d+(?:[.\-]|$)")

# 每段最多验几个模型。聚合站声明几百个（relay-m 曾 838 个），
# 全验会触发反测活且极贵。
MAX_MODELS_PER_SECTION = 4

# 每段最多**尝试**几次模型验证。
#
# 为什么不能只有 MAX_MODELS_PER_SECTION（2026-09-01 量化发现）：
# 那个是「已接受几个」的上限，而失败的尝试不增加计数。一个声明 838 个模型
# 的聚合站，如果它的模型全都验不过（分组不含、限时段、要门票），循环会把
# 白名单过滤后剩下的**全部**打一遍才结束 —— 上面那句注释说的「全验会触发
# 反测活且极贵」正是这个情形，但原来的 break 条件兑现不了它。
#
# 取 10：够拿到 4 个可信模型（种子 2-3 个 + 目录里前几个通常就够），
# 又把最坏情形从「目录长度」压到常数。
MAX_MODEL_ATTEMPTS_PER_SECTION = 10


def _encode(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _body_kind(prof) -> str:
    """这一档的 body 补丁是什么形态。空串 = 不需要 body。

    只记形态名不记值：求值后的值含随机 UUID，落进报告会让每次输出都不同，
    没法用来做「上次是什么、这次变了没有」的漂移比较。
    """
    if not prof.body_patch:
        return ""
    uid = ""
    md = prof.body_patch.get("metadata")
    if isinstance(md, dict):
        uid = str(md.get("user_id") or "")
    kind = "metadata.user_id"
    if uid.startswith("{"):
        kind += "(json)"
    elif uid.startswith("user_"):
        kind += "(plain)"
    if "system" in prof.body_patch:
        kind += "+system"
    return kind


# 「这个模型没有」的说法，按语义收拢而不是按状态码。
#
# 2026-09-01 复盘 79 凭据实跑：上游中转站（new-api / one-api 系）用
# **503** 回 `No available channel for model X under group default`，
# 全场出现 175 次、直接判死 92 个段。这句话的语义与 404 `model_not_found`
# 完全相同 —— 「你要的这个模型，这个分组里没有」，是**模型专属**的。
# 原来只豁免 404，于是 503 这条路整段收敛，45/79 个凭据判 0 段可用，
# 而其中 27 个日志里其实出现过 200、7 个 /models 目录明明拿到过模型。
#
# 这句话确实来自上游而非 CPA：CLIProxyAPI 全仓库搜 "No available channel"
# 零命中，它自己的措辞是 conductor_selection.go:492 的 auth_unavailable。
_MODEL_SPECIFIC_DEAD_END = re.compile(
    r"model_not_found"
    r"|no available channel"          # 上游中转站：该分组无此模型的活跃通道
    r"|无可用渠道|可用渠道不存在"
    r"|model .{0,80}(?:not (?:supported|found)|does not exist)"
    r"|不支持所选模型|模型不存在",
    re.I,
)

# 只在这些码上认「模型专属」。200 不该走到这里；5xx 里只认 503
# （中转站的调度失败），500/502/504 是站方故障，已归「临时」并会重试。
_MODEL_SPECIFIC_CODES = frozenset({"400", "403", "404", "503"})


def _model_specific_dead_end(att) -> bool:
    """这个「死路」是不是只针对**当前这个模型**，换个模型可能就通了。

    为什么要区分（2026-08-31 实测）：404 `model_not_found` 说的是「这个分组
    里没有这个模型」，而 SEED_MODELS 里的模型是本工具写死的猜测 —— 它不存在
    完全不能说明这个站不可用。实测某站 claude-sonnet-5 返回 404，整段被判死，
    而该站的 claude-opus-5 是可用的。

    与之相对，敏感词拦截、Key 分组不匹配、路径不存在这几种「死路」与模型
    无关，换模型也救不了，那种要立即收敛以省请求数。

    判据按**正文语义**而不是状态码（2026-09-01 修正，见上方常量的说明）：
    同一句「没有这个模型的渠道」，不同中转站分别用 400 / 403 / 404 / 503
    发出来，只认 404 会漏掉最常见的那一种。
    """
    if str(att.status) not in _MODEL_SPECIFIC_CODES:
        return False
    return bool(_MODEL_SPECIFIC_DEAD_END.search(att.excerpt or ""))


def model_allowed(name: str) -> bool:
    """按用户规则过滤模型名：只留 gemini / gpt / claude 三类。

    o1 / o3-mini 这类 OpenAI 推理系列也放行 —— 见 _OPENAI_REASONING_RE 的说明：
    它们属于规则里的「gpt 那一类」，只是 OpenAI 换了命名，2026-08-31 实测
    被前缀白名单漏掉了。
    """
    n = (name or "").strip().lower()
    # 去掉可能的 provider 前缀（relay-m 会写 Business/gemini-xxx）
    if "/" in n:
        n = n.split("/")[-1]
    if n.startswith(MODEL_PREFIX_WHITELIST):
        return True
    return bool(_OPENAI_REASONING_RE.match(n))


@dataclass
class Attempt:
    """一次探测的完整记录。保留正文摘要 —— 判定依赖正文而非状态码。"""

    section: str
    model: str
    combo: str
    status: str
    category: str
    action: str
    elapsed_ms: int
    proxy: str | None = None
    resp_model: str | None = None
    resp_id: str | None = None
    backend: str = ""
    input_tokens: int | None = None
    excerpt: str = ""
    sent_chars: int = 0
    # 200 但正文是错误体。见 _accept 的说明 —— 这是实测过的假阳性来源，
    # 而 status == "200" 单独看不出来，所以在 _call 里当场判好存下来。
    error_envelope: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "200"

    def as_sample(self) -> dict:
        """转成 fingerprint.swap_rate 需要的形状。"""
        return {
            "status": self.status,
            "requested": self.model,
            "actual": self.resp_model,
            "backend": self.backend,
            "input_tokens": self.input_tokens,
        }


@dataclass
class SectionVerdict:
    """一个候选在一个段上的结论。直接决定要不要写进这一段、带什么字段。"""

    section: str
    base_url: str = ""
    usable: bool = False
    models: list[str] = field(default_factory=list)
    need_proxy: bool = False
    min_headers: dict[str, str] = field(default_factory=dict)
    # 通过时用的画像档名（profiles.Profile.name）。写回与报告都要它 ——
    # 「需要 cc-std」比「需要 3 个头」对人有用得多。
    profile_name: str = ""
    # 该档的 body 补丁形态（只记有没有、是哪种，不记求值后的值 —— 那含
    # 随机 UUID，落进报告会让每次输出都不同）。非空表示 headers 表达不了，
    # claude 段要 fingerprint-profile，其余三段配置层无解。
    min_body_kind: str = ""
    # 分组的可调用时段（("09:00","18:00")）。「时段」类才有。
    time_window: tuple[str, str] | None = None
    swap: dict = field(default_factory=dict)
    max_context_length: int | None = None
    # 上限是在**哪个模型**上实测的。同站不同模型窗口不同（claude-opus-5
    # 与 haiku 差一个数量级），把 A 的实测值写到 B 上等于伪造数据 ——
    # 客户端会按错误的窗口定压缩点，重演那条 400。
    context_model: str = ""
    context_untrusted: bool = False
    category: str = ""
    action: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    # 站方 /models 目录声明的模型（白名单过滤后）。`_stage0_catalog` 填。
    #
    # 与 `models` 的区别是**声明**与**实测**：这里是站方说它有什么，`models`
    # 是本工具实际验证通过的。两者的差集很有价值 —— 声明有却验不过的模型，
    # 正是 CPAMP 面板「模型」列会显示、而真实转发会失败的那批（那一列读的
    # 是 config.yaml 的 models 字段长度，rowData.ts:78-79，不做可用性校验）。
    #
    # 判死的段也要留着它：操作员人工接管时，这是唯一可选的候选清单。
    catalog: list[str] = field(default_factory=list)

    @property
    def need_ua(self) -> bool:
        return bool(self.min_headers)

    @property
    def swap_detected(self) -> bool:
        return bool(self.swap.get("swap"))

    def summary(self) -> str:
        if not self.usable:
            base = f"{self.category or '不可用'} — {self.action}"
            if self.time_window:
                base += f"（可调用时段 {self.time_window[0]}~{self.time_window[1]}）"
            return base
        bits = [f"{len(self.models)} 模型"]
        if self.need_proxy:
            bits.append("需代理")
        if self.profile_name:
            bits.append(f"需画像 {self.profile_name}")
        elif self.min_headers:
            bits.append("需 " + "+".join(self.min_headers))
        if self.min_body_kind:
            bits.append(f"需 {self.min_body_kind}")
        if self.swap_detected:
            bits.append(f"⚠ 换模 {self.swap.get('rate_pct', 0)}%")
        if self.max_context_length:
            bits.append(f"上限 {self.max_context_length:,}")
        return " · ".join(bits)


@dataclass
class CandidateResult:
    row: ParsedRow
    sections: dict[str, SectionVerdict] = field(default_factory=dict)

    @property
    def usable_sections(self) -> list[str]:
        return [s for s, v in self.sections.items() if v.usable]

    @property
    def total_calls(self) -> int:
        return sum(len(v.attempts) for v in self.sections.values())


class Prober:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        gap: float = 3.0,
        timeout: int = 120,
        probe_context: bool = True,
        swap_samples: int = 3,
        workers: int = 4,
        max_models: int = MAX_MODELS_PER_SECTION,
        max_model_attempts: int = MAX_MODEL_ATTEMPTS_PER_SECTION,
        reuse_profile_verdict: bool = True,
        on_event: Callable[[str, dict], None] | None = None,
        cfg_snapshot: dict | None = None,
    ):
        # 现有 config.yaml 的快照。画像梯从它的 `claude-header-defaults` /
        # `codex.header-defaults` 派生真实的 UA 版本号与 X-Stainless 值
        # （profiles.defaults_from_config）。给 None 时回落内置常量 ——
        # 那些常量是从 CPA 源码抄录的，不是猜的，所以缺配置也能工作。
        self.cfg_snapshot = cfg_snapshot
        self.proxy = proxy
        self.gap = gap
        self.timeout = timeout
        self.probe_context = probe_context
        self.swap_samples = swap_samples
        # 每段收几个模型、最多试几次。做成参数是因为这两个数的取舍与
        # 具体站群有关：聚合站多时该压低尝试数，站少而模型杂时该放宽。
        self.max_models = max(1, int(max_models))
        self.max_model_attempts = max(self.max_models, int(max_model_attempts))
        # 同段整梯全败后，后续种子是否跳过画像梯。默认开 —— 门票是站+段的
        # 属性，与模型无关（见 _profiles_failed）。留开关是为了万一遇到
        # 「同一段不同模型走不同门禁」的站，能一键回到旧行为自证。
        self.reuse_profile_verdict = reuse_profile_verdict
        # 四段并行度。1 = 完全串行（老行为，出问题时的退路）。
        # 上限就是 4 —— 段数固定，再高没有意义。
        self.workers = max(1, min(int(workers), len(SECTIONS)))
        self.on_event = on_event or (lambda kind, data: None)
        # 节流按 **host** 分开记。原来是全局一个时间戳，探 A 站要等 B 站的
        # gap —— 不同站之间没有任何理由互相等。反测活是站方行为，只对同站生效。
        self._last_call: dict[tuple[str, str], float] = {}
        # (host, section) -> 已学到的段形态。同一主机的第 2..N 个 Key 直接复用，
        # 只补一次凭证确认。见 _reuse_shape 的说明。
        self._shape: dict[tuple[str, str], SectionVerdict] = {}
        # (host, section) -> 门闩。表示「已有线程在学这个段的形态」。
        # 同主机多 Key 并发时防止重复做那 12 次昂贵探测。见 _probe_one_section。
        self._inflight: dict[tuple[str, str], threading.Event] = {}
        # (host, section) -> 整梯跑完全败。**门票是站+段的属性，与用哪个种子
        # 模型问它无关** —— 站方查的是 headers 与 body 形态，不看模型名。
        #
        # 为什么值得单独记（2026-09-01 量化）：_try_profiles 的调用点在
        # _stage1 的种子循环内部，返回 False 后循环走到下一个种子，整梯重跑。
        # 而每段有 2-3 个种子，claude 段 7 档 × 2 = 14 次、compat 6 档 × 3 = 18 次。
        # 四段全不通的站现状 48 次画像请求，其中 27 次是重复问同一个问题。
        #
        # 只缓存**失败**：成功的形态已经由 _shape 记着，而且成功时 _stage1
        # 直接 return，不会走到下一个种子。
        self._profiles_failed: set[tuple[str, str]] = set()
        self._lock = threading.RLock()
        self._proxy_state: bool | None = None   # None=未检 True/False=预检结果
        # 代理预检专用锁。不复用 _lock —— 预检要占最多 4 秒，
        # 而 _lock 同时保护节流计时与形态缓存，不能被长时间持有。
        self._proxy_lock = threading.Lock()

    # ---------- 底层 ----------

    @property
    def live_proxy(self) -> str | None:
        """代理地址，仅在预检通过时返回；不通则返回 None。

        为什么不直接用 self.proxy：`via-proxy` 是 IP封/边缘 的首选处置，
        每段每模型都会试一次。代理不通时每次都干等满 timeout（默认 120 秒）
        才失败 —— 实测日志里 mihomo:7890 早已不通（preflight 也报了警），
        5 个 key 多段累计十几分钟纯粹白等，且结果全是无用的 `000 未知`。

        预检只做一次 TCP 握手，4 秒封顶，结果缓存在 _proxy_state。
        """
        if not self.proxy:
            return None
        if self._proxy_state is None:
            # 必须在专用锁下做，且锁内再判一次 —— 四段并行时会同时看到
            # _proxy_state is None，无锁的话每段各做一次 TCP 预检（实测被
            # 测试抓到：proxy-precheck 事件发了 4 次）。
            #
            # 用独立的 _proxy_lock 而不是 self._lock：probe_proxy 要占用最多
            # 4 秒，而 _lock 同时保护 _throttle 与 _shape —— 拿着它睡 4 秒
            # 会把另外三段的节流计算一起堵住。
            with self._proxy_lock:
                if self._proxy_state is None:
                    ok, detail = client.probe_proxy(self.proxy, timeout=4)
                    self.on_event("proxy-precheck", {"proxy": self.proxy, "ok": ok,
                                                     "detail": detail})
                    self._proxy_state = ok
        return self.proxy if self._proxy_state else None

    def _throttle(self, host: str = "", section: str = "") -> None:
        """按 (host, section) 保持 gap 秒。不同主机、不同段互不等待。

        为什么按 host 而不是全局：gap 存在的理由是站方的 bulk probe guard
        （relay-b 实测低于 3 秒会触发），那是**站方**的限频。探 A 站时为 B 站
        的上一次请求等待纯属浪费 —— 多主机批量导入时这笔账按主机数翻倍。

        为什么再按 section 细分（2026-08-30 加）：guard 是**按端点**计的。
        四段打的是四个不同路径：
            gemini  /v1beta/models/{model}:generateContent
            codex   /v1/responses
            claude  /v1/messages
            compat  /v1/chat/completions
        它们共享一个 gap 桶时，单站四段的 56 次请求要串成 55 x 3s = 165 秒
        纯睡 —— 这是「探测要十几分钟」最大的一笔。拆开后四段各自计时，
        同段内仍严格保持 gap（guard 该防的东西一点没松）。

        风险与取舍：如果某站的 guard 是按账号（而非端点）全局计的，拆开后
        瞬时并发会是原来的 4 倍。这就是 --gap 仍然存在、且默认保持 3 秒的
        原因 —— 撞上那种站把 gap 调大即可，不需要改回全局串行。
        """
        # 用元组当键，不用字符串拼接 —— 裸拼接会有歧义碰撞：
        # ("a", "b|c") 与 ("a|b", "c") 拼出同一个 "a|b|c"，两者会共享
        # 同一个 gap 桶（测试抓到过）。host 来自用户输入的 URL，
        # section 虽然是内部常量，也没有理由留这个坑。
        bucket = (host, section)
        with self._lock:
            last = self._last_call.get(bucket, 0.0)
            wait = self.gap - (time.monotonic() - last)
            if wait > 0:
                # 记成「即将发出」，避免同桶并发时多个线程一起放行
                self._last_call[bucket] = time.monotonic() + wait
            else:
                self._last_call[bucket] = time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _call(
        self,
        section: str,
        base: str,
        key: str,
        model: str,
        *,
        combo: str,
        extra_headers: dict[str, str] | None = None,
        body_patch: dict | None = None,
        proxy: str | None = None,
        text: str | None = None,
    ) -> Attempt:
        # 按 (host, section) 节流：同站同段保持 gap，其余互不等待
        self._throttle(host_of(base), section)
        kwargs = {"extra_headers": extra_headers}
        if text is not None:
            kwargs["text"] = text
        url, headers, body = request.build_request(section, base, model, key, **kwargs)
        if body_patch:
            # 画像的 body 补丁（metadata.user_id 等）。浅层 merge 就够 ——
            # 补丁只碰顶层键，而顶层同名键就该整体替换（metadata 整个替换，
            # 不是与探测自己的 metadata 合并 —— 探测本来不发 metadata）。
            body.update(body_patch)
        resp = client.send(
            url,
            headers=headers,
            body=_encode(body),
            proxy=proxy,
            timeout=self.timeout,
        )
        category, action = _classify(resp.status, resp.body)
        rid = fingerprint.resp_id(resp.body)
        sent = len(text) if text is not None else len(request.PROBE_TEXT)
        att = Attempt(
            section=section,
            model=model,
            combo=combo,
            status=resp.status,
            category=category,
            action=action,
            elapsed_ms=resp.elapsed_ms,
            proxy=proxy,
            resp_model=fingerprint.resp_model(resp.body),
            resp_id=rid,
            backend=fingerprint.backend_of(rid),
            input_tokens=fingerprint.input_tokens(resp.body),
            excerpt="" if resp.status == "200" else _body_excerpt(resp.body),
            sent_chars=sent,
            error_envelope=_has_error_envelope(resp.body),
        )
        self.on_event(
            "attempt",
            {
                "section": section,
                # host 必须带 —— 79 个站并发，日志是交织的流。不带归属时
                # 「某站的行」和「别站的行」混在一起，看着像这个站没跑完。
                "host": host_of(base),
                "model": model,
                "combo": combo,
                "status": resp.status,
                "category": category,
                "elapsed_ms": resp.elapsed_ms,
            },
        )
        return att

    # ---------- ① 段归属 + ③ 处置 ----------

    def _accept(self, v: SectionVerdict, model: str, att: Attempt) -> list[str]:
        """200 之后再判「回的是不是我要的模型」，判过才收进清单。

        为什么必须有这一步：`_stage2` 对目录里的模型做了 `model_matches`
        校验，而 `_stage1` 原先对种子模型直接 `v.models = [model]` ——
        等于换模站的第一个模型拿到免检通行证。relay-e 那种「请求
        gpt-5.6-sol 回 agnes-2.0-flash」的站会被判成可用并写进 config.yaml，
        照常计费却拿不到要的模型，比不可用更危险。

        段仍算 usable（端点确实响应 200，凭证本身有效），但模型不进清单。
        models 为空 → `SectionPlan.writable` 为 False，不会写入。

        为什么还要判「200 包错误体」（2026-08-31 实测的假阳性）
        ------------------------------------------------------
        `Attempt.ok` 只看 `status == "200"`，而 `model_matches` 在
        `resp_model` 为 None 时按设计返回 True（无证据不判换模）。两者叠加，
        「HTTP 200 + 正文是 {"error":...} + 没有 model 字段」这种站一路绿灯。
        实测复现：四段全部 usable=True、注册 11 个模型，而那个站**完全不能用**。

        这是**假阳性**，比判死更危险：死站带着模型进生产 config.yaml，每次轮到
        它就吃一次失败，耗尽 request-retry x max-retry-credentials 预算，
        最终客户端收到 500 —— 正是 tools/diag403.py 要诊断的那个症状。

        判据必须精准，不能简单地「200 且无 model 字段就拒」：
          · gemini 段用的是 `modelVersion` 而非 `model`，resp_model 已覆盖；
          · 有些站的合法响应确实不带 model 字段（流式首包、极简实现）。
        所以判的是「正文顶层有错误结构」而不是「缺 model 字段」：
        顶层 error / 顶层 "type":"error"，两者都是明确的错误信号。
        """
        if att.error_envelope:
            self.on_event("model-rejected", {
                "section": v.section,
                "host": host_of(v.base_url),
                "requested": model,
                "actual": None,
                "reason": "200 但正文是错误体",
            })
            return []
        if fingerprint.model_matches(model, att.resp_model):
            return [model]
        self.on_event("model-rejected", {
            "section": v.section,
            "host": host_of(v.base_url),
            "requested": model,
            "actual": att.resp_model,
            "backend": att.backend,
        })
        return []

    # 「临时」类的重试：站方负载上限（503/502/504）不代表站点不可用。
    # 实测踩到：某站 claude-opus-5 首次 503，而第二个种子 claude-sonnet-5
    # 返回 404（该站根本没这个模型），整段被判死 —— 而那站其实可用。
    _TRANSIENT_RETRIES = 1
    _TRANSIENT_WAIT = 2.0

    # 基线阶段打几个模型。原来是 `SEED_MODELS[section]` 的全长（2-3 个），
    # 现在候选来自目录（可能几百个），必须显式限长 —— 基线的目的是定段归属
    # 与找最小门票，不是把目录验穷（那是 _stage2 的活）。
    #
    # 取 2：一个够定归属，两个能区分「站级不可用」与「这个模型不在」。
    _BASELINE_MODELS = 2

    # 目录最多翻几页。只有 gemini 的 /v1beta/models 分页，与 CPAMP 的
    # healthCheck.ts:279-364 取同一个上限，防异常站的无限 nextPageToken。
    _CATALOG_PAGES = 20

    # 判定的严重度排序。全部种子都失败时取**最严重**的那个，而不是最后一个。
    # 越靠前越严重。不在表里的类别按「未知」处理。
    # 越靠前越严重。全部种子失败时取**最严重**的那个。
    #
    # 2026-08-31 修正：原来「死路」排第一，于是一个该站根本不存在的模型
    # （SEED_MODELS 里写死的猜测）返回 404 model_not_found 时，会盖掉
    # 另一个种子的真实结论。实测踩到：opus-5 返回 503（客户端门禁）、
    # sonnet-5 返回 404，整段判「死路」—— 而真正该报的是「客户端」。
    #
    # 修法是两层：模型专属死路不进 seen（见 _stage1），这里再把「客户端」
    # 排在「死路」之前 —— 它更接近根因，且处置明确（补标识或人工接管）。
    # 「WAF」紧跟「客户端」：两者都是形态问题，且 WAF 的处置（试画像）与
    # 客户端同源，比「死路」更接近根因。
    # 「时段」排在最后段 —— 它 usable=True，且窗口内自然可用，是最不严重的
    # 一类失败。若排在前面，一个时段受限的站会盖掉另一个种子的真实故障。
    _SEVERITY = ("客户端", "WAF", "死路", "鉴权", "注入", "门禁", "IP封",
                 "未知", "临时", "限频", "限流", "边缘", "反测活", "余额", "时段")

    @classmethod
    def _severity_rank(cls, category: str) -> int:
        """越小越严重。不在表里的排在「未知」的位置。"""
        try:
            return cls._SEVERITY.index(category)
        except ValueError:
            return cls._SEVERITY.index("未知")

    def _stage1(self, row: ParsedRow, section: str) -> SectionVerdict:
        """基线不通 → 试代理 → 试补标识头。三步都按处置优先级排。

        为什么判定不能「后一个种子覆盖前一个」（2026-08-31 实测）
        ----------------------------------------------------
        原来每轮循环无条件 `v.category = att.category`，于是种子列表里
        **后面**那个模型的结论会盖掉前面的。实测后果：
            claude-opus-5   -> 503 临时（站方忙，本该重试）
            claude-sonnet-5 -> 404 死路（该站根本没这个模型）
        整段判「死路」并 usable=False —— 而 claude-sonnet-5 只是这里写死的
        第二个种子，它不存在完全不能说明这个站不可用。

        改成：全部种子失败后，取**最严重**的类别（见 _SEVERITY）。
        「死路」只有在真的是死路时才成立，不会由一个不相干的模型带来。

        探哪些模型（2026-09-01 改）
        ------------------------
        先 `_stage0_catalog` 问站方目录，用目录里真实存在的模型开打，种子只
        作兜底。原来直接拿写死的种子撞，撞不上就判死 —— 那是把「我猜的模型
        不在」当成了「这个站不能用」。
        """
        base = base_for_section(row.bare, section)
        v = SectionVerdict(section=section, base_url=base)
        seen: list[tuple[str, str]] = []      # 每个候选的 (类别, 处置)

        # 目录优先。拿不到就是空列表，`_probe_order` 自动回落到种子。
        v.catalog = self._stage0_catalog(row, section, base)

        # 基线阶段只打前几个 —— 这里的目的是「定段归属 + 找最小门票」，
        # 不是把目录验穷。验穷是 _stage2 的活，且有 max_model_attempts 兜着。
        probe_models = self._probe_order(section, v.catalog)[:self._BASELINE_MODELS]
        for model in probe_models:
            att = self._call(section, base, row.api_key, model, combo="baseline")
            v.attempts.append(att)

            # 临时错误（站方负载）重试 —— 不重试就会把「忙」当成「坏」。
            tries = 0
            while (att.category == "临时" and tries < self._TRANSIENT_RETRIES):
                tries += 1
                self.on_event("transient-retry", {
                    "section": section, "host": host_of(base), "model": model,
                    "status": att.status, "wait": self._TRANSIENT_WAIT,
                })
                time.sleep(self._TRANSIENT_WAIT)
                att = self._call(section, base, row.api_key, model,
                                 combo=f"retry{tries}")
                v.attempts.append(att)

            seen.append((att.category, att.action))

            if att.ok:
                v.category, v.action = att.category, att.action
                v.usable = True
                v.models = self._accept(v, model, att)
                return v

            # 余额 / 死路：换模型、加头、走代理都救不了。立即收敛。
            #
            # 但「死路」只对**这个模型**成立时不该终止整段：404 model_not_found
            # 说的是「这个分组没有这个模型」，换个模型完全可能通。只有在
            # 死路原因与模型无关时（敏感词、分组无渠道）才真的没救。
            if att.category == "余额":
                v.category, v.action = att.category, att.action
                return v
            if att.category == "死路" and not _model_specific_dead_end(att):
                v.category, v.action = att.category, att.action
                return v

            # 反测活：探测文本本身触发了拦截，换一句重试
            if att.category == "反测活":
                att = self._call(
                    section, base, row.api_key, model, combo="alt-text",
                    text="Explain the TCP three-way handshake in one sentence.",
                )
                v.attempts.append(att)
                v.category, v.action = att.category, att.action
                if att.ok:
                    v.usable = True
                    v.models = self._accept(v, model, att)
                    return v

            # IP封 / 边缘：代理是最高优先级处置
            if att.category in ("IP封", "边缘") and self.live_proxy:
                att = self._call(
                    section, base, row.api_key, model,
                    combo="via-proxy", proxy=self.live_proxy,
                )
                v.attempts.append(att)
                if att.ok:
                    v.usable = True
                    v.need_proxy = True
                    v.category, v.action = att.category, att.action
                    v.models = self._accept(v, model, att)
                    return v

            # 时段：分组按时间窗口开放。窗口外重试一万次也一样，但凭据是好的 ——
            # 记下窗口后立即收敛，不浪费请求，也不把它判成不可用。
            if att.category == "时段":
                v.time_window = _time_window(att.excerpt)
                v.category, v.action = att.category, att.action
                self.on_event("time-window", {
                    "section": section, "host": host_of(base),
                    "window": v.time_window,
                })
                return v

            # 客户端形态类：按画像梯由省到全回退，第一个 200 即最省可用档。
            #
            # 为什么触发条件这么宽（每一项都是实测踩出来的）：
            #   客户端 —— 站方明说「只允许某某客户端」，可能回 503（不在 401/403 里）
            #   WAF    —— 自建拦截页，认的是客户端形态，换 IP 无效
            #   门禁/IP封/边缘/401/403 —— 都可能实际是形态问题被误分类
            #   鉴权   —— 401 unauthorized client 会落到这里（实测 agentrouter）
            # 判错方向的代价不对称：多试几档只是多几次请求，漏试会把可用站判死。
            if (att.category in ("客户端", "WAF", "门禁", "IP封", "边缘", "鉴权")
                    or att.status in ("401", "403", "503")):
                if self._try_profiles(row, section, base, model, v):
                    return v

        # 全部种子都没通。取**最严重**的类别，而不是最后一个种子的结论。
        # 见本方法 docstring：后者会让一个该站不存在的模型判死整段。
        if seen:
            best = min(seen, key=lambda ca: self._severity_rank(ca[0]))
            v.category, v.action = best
        return v

    def _profile_kwargs(self, v: SectionVerdict, api_key: str) -> dict:
        """后续请求（模型扫描 / 换模采样 / 上下文二分 / 换 Key 复验）要带的
        画像参数。**必须与 stage1 通过时那一档完全一致**。

        为什么不能只传 min_headers（这是个实测过的坑的同构形态）
        ------------------------------------------------------
        stage1 用画像通过后，后面四处若只带 headers 不带 body 补丁，那些请求
        对需要 `metadata.user_id` 的站（实测 zzzcoding）会全部失败 ——
        于是「段可用但注册 0 个模型」，或者换 Key 复验时把好 Key 判成坏 Key。
        这与 wave-1 修过的「_stage2 绕过 _accept」是同一类缺陷：主路径加了
        检查，第二条路径没加。所以这里做成唯一入口，四处都走它。

        每次重新 materialize 而不是缓存求值结果：body 里的 session_id 应当
        每个请求都是新的（真实客户端行为），缓存住会让所有请求共用一个会话 ID。
        """
        if v.profile_name:
            for prof in profiles.ladder(v.section, self.cfg_snapshot):
                if prof.name == v.profile_name:
                    hdrs, patch = profiles.materialize(prof, api_key)
                    return {"extra_headers": hdrs or None,
                            "body_patch": patch or None}
        return {"extra_headers": dict(v.min_headers) or None}

    def _try_profiles(self, row: ParsedRow, section: str, base: str,
                      model: str, v: SectionVerdict) -> bool:
        """按画像梯升级。第一个通过的档写进 verdict 并返回 True。

        梯子是**嵌套超集**（见 profiles 模块 docstring）：第 k 档失败即前 k 档
        的并集都不够，不必回头补试。实测依据 —— agentrouter 的门票是
        user-agent + anthropic-beta + x-app 三项缺一不可，平行尝试会全败而
        它们的并集本来是通的。

        保守取向：整梯跑完（含 alt 档），不提前放弃。多试几档只多几次请求，
        而漏试会把一个可用站判死 —— 后者不可逆（用户按报告弃用了那个站）。

        但「整梯跑完」只需要做**一次**：门票是站+段的属性（站方查 headers 与
        body 形态，不看模型名），所以第一个种子试完整梯全败之后，同段的后续
        种子直接跳过，不重问同一个问题。省的量见 _profiles_failed 的说明。
        """
        pkey = (host_of(base), section)
        if self.reuse_profile_verdict:
            with self._lock:
                already_failed = pkey in self._profiles_failed
            if already_failed:
                self.on_event("profile-skipped", {
                    "section": section, "host": host_of(base), "model": model,
                    "why": "同段整梯已试过且全败，门票与模型无关",
                })
                return False

        tried = 0
        for prof in profiles.ladder(section, self.cfg_snapshot):
            if prof.is_baseline:
                continue                    # 基线已在调用方试过
            hdrs, patch = profiles.materialize(prof, row.api_key)
            att = self._call(
                section, base, row.api_key, model, combo=f"id:{prof.name}",
                extra_headers=hdrs or None, body_patch=patch or None,
                proxy=self.live_proxy if v.need_proxy else None,
            )
            v.attempts.append(att)
            tried += 1
            if not att.ok:
                continue
            # 200 也要过 _accept —— 「200 但正文是错误体」是实测过的假阳性
            # 来源（某站对所有请求都回 200，把真实错误放正文里）。
            models = self._accept(v, model, att)
            if not models:
                continue
            v.usable = True
            v.min_headers = dict(hdrs)
            v.profile_name = prof.name
            v.min_body_kind = _body_kind(prof)
            v.category, v.action = att.category, att.action
            v.models = models
            self.on_event("profile-hit", {
                "section": section, "host": host_of(base),
                "profile": prof.name, "tier": prof.tier,
                "family": prof.family, "tried": tried,
                "needs_body": bool(patch),
            })
            return True
        # 整梯全败，但站方可能在正文里明说了缺什么能力 —— 补上再打一次。
        # 见 betas 模块 docstring（anyrouter.top：八档正文逐字相同，全是
        # 「请启用 1m 上下文」，说明站方没查客户端身份，只是缺一个 beta）。
        if self._retry_with_betas(row, section, base, model, v, tried):
            return True

        with self._lock:
            self._profiles_failed.add(pkey)
        self.on_event("profile-exhausted", {
            "section": section, "host": host_of(base), "tried": tried,
        })
        return False

    def _retry_with_betas(self, row: ParsedRow, section: str, base: str,
                          model: str, v: SectionVerdict, tried: int) -> bool:
        """正文点名要 beta 就补上重试。只 claude 段有 anthropic-beta。

        用顶档（整梯最后一档的 headers）作基底：正文既然没在查客户端身份，
        多带门票无害；而少带会引入第二个变量，分不清是 beta 补对了还是门票
        本来就够。
        """
        if section != "claude-api-key":
            return False

        # 从已跑的尝试里找站方索要的项。取最后一次失败的正文 —— 八档一致时
        # 取哪次都一样，不一致时最后一次对应门票最全，最可信。
        extra: list[str] = []
        for att in reversed(v.attempts):
            extra = betas.wanted(att.excerpt or "")
            if extra:
                break
        if not extra:
            return False

        # 基底取**非 alt 的族内最高档**。alt（browser-ua）是替换型画像，不是
        # CC 门票的超集 —— 拿它作基底会把 CC 门票整个丢掉，实测落地只剩 2 个
        # header。而 CPAMP 里能用的形态是「CC 门票 + 1m」，不是「浏览器 + 1m」。
        top = None
        for prof in profiles.ladder(section, self.cfg_snapshot):
            if not prof.is_baseline and not prof.alt:
                top = prof
        if top is None:
            return False

        hdrs, patch = profiles.materialize(top, row.api_key)
        hdrs = dict(hdrs)
        # 头名大小写不敏感，但现有值可能挂在任意拼法上，逐个找
        slot = next((k for k in hdrs if k.lower() == "anthropic-beta"),
                    "anthropic-beta")
        hdrs[slot] = betas.merge(hdrs.get(slot, ""), extra)

        self.on_event("beta-retry", {
            "section": section, "host": host_of(base), "model": model,
            "added": extra, "profile": top.name, "after_tried": tried,
        })
        att = self._call(
            section, base, row.api_key, model, combo=f"beta:{top.name}",
            extra_headers=hdrs, body_patch=patch or None,
            proxy=self.live_proxy if v.need_proxy else None,
        )
        v.attempts.append(att)
        if not att.ok:
            return False
        models = self._accept(v, model, att)
        if not models:
            return False

        v.usable = True
        v.min_headers = hdrs
        v.profile_name = f"{top.name}+beta"
        v.min_body_kind = _body_kind(top)
        v.category, v.action = att.category, att.action
        v.models = models
        self.on_event("beta-hit", {
            "section": section, "host": host_of(base),
            "profile": v.profile_name, "added": extra,
        })
        return True

    # ---------- ⓿ 目录发现（先问站方，再动手打） ----------

    def _stage0_catalog(self, row: ParsedRow, section: str,
                        base: str, *, need_proxy: bool = False) -> list[str]:
        """GET /models 拿站方声明的模型清单。**在任何推理请求之前**跑。

        为什么必须前置（2026-09-01 复盘 79 凭据实跑）
        --------------------------------------------
        原来这一步在 `_stage2`，而 `_stage2` 只在 `_stage1` 已经成功时才跑
        （见 `_full_probe`）—— 顺序完全颠倒：拿写死的种子模型去撞，撞不上
        就判死，从头到尾没问过站方「你到底有什么」。实测后果：45/79 个凭据
        判 0 段可用，其中 7 个的 /models 目录明明拿得到模型（最多 199 个），
        而 model-scan 阶段（用目录里的模型）成功 200 共 78 次，模型名如
        claude-opus-4-6 / claude-opus-4-8 / gpt-oss-120b —— 全都不在种子表里。

        为什么这一步比推理请求安全
        ------------------------
        · GET 目录端点，多数站不计费、不计入调用统计、不触发风控；
        · 参考实现全都只这么做：CPAMP 的健康检查只发 GET 目录
          （healthCheck.ts:397-451，全库无 POST messages/chat 测活），
          CLIProxyAPI 自己完全不探上游存活（唯一定时任务 model_updater.go
          拉的是 GitHub 上的模型名录 JSON，不发推理请求）。
        · 与用户「严禁 Hi/你好 这类简单测活」的要求同向：能不发推理就不发。

        拿不到目录不是失败 —— 返回空列表，调用方回落到种子模型。
        """
        url, headers = request.models_endpoint(section, base, row.api_key)
        proxy = self.live_proxy if need_proxy else None
        seen: list[str] = []
        token = ""

        # gemini 的 /v1beta/models 分页；其余三段一页出完。上限 _CATALOG_PAGES
        # 是为了不被恶意/异常的无限 nextPageToken 拖住。
        for page in range(self._CATALOG_PAGES):
            self._throttle(host_of(base), section)
            page_url = url
            if token:
                sep = "&" if "?" in url else "?"
                page_url = f"{url}{sep}pageToken={urllib.parse.quote(token)}"
            resp = client.send(
                page_url, headers=headers, body=b"", method="GET",
                proxy=proxy, timeout=self.timeout,
            )
            if resp.status != "200":
                if page == 0:
                    # 与 catalog 事件同一套载荷约定：不重复 host。
                    self.on_event("catalog-miss", {
                        "section": section, "host": host_of(base),
                        "status": resp.status,
                    })
                    return []
                break            # 翻页中断：已拿到的仍然算
            for m in request.parse_models_response(section, resp.body):
                if m not in seen:
                    seen.append(m)
            token = (request.next_page_token(resp.body)
                     if section == "gemini-api-key" else "")
            if not token:
                break

        catalog = [m for m in seen if model_allowed(m)]
        # 载荷保持 {section, count} 两字段 —— 前端与 test_pipeline 的
        # 「catalog 载荷字段」断言按这个约定写。host 由 candidate-start
        # 给出，这里重复只会让事件流变胖；白名单滤掉多少不进载荷，避免
        # 把上游的模型名规模也一起吐出去。
        self.on_event("catalog", {"section": section, "host": host_of(base),
                                  "count": len(catalog)})
        return catalog

    def _probe_order(self, section: str, catalog: list[str],
                     exclude: list[str] | None = None) -> list[str]:
        """验证顺序：目录里的模型优先，种子只作兜底。

        目录是站方**声明有**的，种子是本工具**猜**的。先验声明的那批，命中率
        高得多，也不会为不存在的模型白烧一次请求。种子仍保留在队尾：有些站
        的目录端点不开放（401/404），但推理端点照常工作。
        """
        skip = set(exclude or ())
        order: list[str] = []
        # 目录里与种子同名的排到最前 —— 既在目录里、又是已知好用的模型
        preferred = [m for m in SEED_MODELS[section] if m in catalog]
        for m in preferred + catalog + SEED_MODELS[section]:
            if m not in order and m not in skip and model_allowed(m):
                order.append(m)
        return order

    # ---------- ② 模型发现 ----------

    def _stage2(self, row: ParsedRow, v: SectionVerdict) -> None:
        """把段内还没验过的模型补齐到 max_models。

        目录已经在 `_stage0_catalog` 拿过并缓存在 `v.catalog`，这里不再重复
        GET —— 重复问同一个目录端点纯属浪费，且会撞节流。

        compat 段留空 = 注册 0 个模型，所以这一步对 compat 是硬要求。
        """
        found = list(v.models)
        order = self._probe_order(v.section, v.catalog, exclude=found)

        attempted = 0
        for model in order:
            if len(found) >= self.max_models:
                break
            # 尝试次数也要有上限 —— found 只数成功的，而目录可能有几百项
            # 且全部验不过。见 MAX_MODEL_ATTEMPTS_PER_SECTION 的说明。
            if attempted >= self.max_model_attempts:
                self.on_event("model-scan-capped", {
                    "section": v.section, "host": host_of(v.base_url),
                    "attempted": attempted, "accepted": len(found),
                    "remaining": len(order) - attempted,
                })
                break
            attempted += 1
            att = self._call(
                v.section, v.base_url, row.api_key, model, combo="model-scan",
                proxy=self.live_proxy if v.need_proxy else None,
                **self._profile_kwargs(v, row.api_key),
            )
            v.attempts.append(att)
            # 必须走 _accept，不要在这里自己判 model_matches。
            #
            # 2026-08-31 实测：原来这里写的是
            #     if att.ok and fingerprint.model_matches(model, att.resp_model)
            # 于是「200 但正文是错误体」的检查被绕过 —— 那道检查只加在
            # _accept 里，而这条是**第二条接受路径**。结果假阳性照旧：
            # 站方全回 200 包错误体，四段仍注册 11 个模型。
            #
            # 这是同一个坑的第二次：wave 1 修过 _stage1 绕过 model_matches，
            # 现在换成 _stage2 绕过错误体检查。所以接受与否只留一个入口。
            if att.ok and self._accept(v, model, att):
                found.append(model)

        v.models = found

    # ---------- ④a 静默换模 ----------

    def _stage4_swap(self, row: ParsedRow, v: SectionVerdict) -> None:
        """单次测不出来 —— weighted-round-robin 下换模是间歇性的。"""
        if not v.models or self.swap_samples < 2:
            return
        model = v.models[0]
        samples: list[dict] = []
        for _ in range(self.swap_samples):
            att = self._call(
                v.section, v.base_url, row.api_key, model, combo="swap-sample",
                proxy=self.live_proxy if v.need_proxy else None,
                **self._profile_kwargs(v, row.api_key),
            )
            v.attempts.append(att)
            samples.append(att.as_sample())
        v.swap = fingerprint.swap_rate(samples)
        if v.swap.get("swap"):
            self.on_event("swap", {"section": v.section,
                                   "host": host_of(v.base_url), "model": model,
                                   "rate_pct": v.swap.get("rate_pct")})

    # ---------- ④b 上下文上限 ----------

    def _stage4_context(self, row: ParsedRow, v: SectionVerdict) -> None:
        """探真实上限，写进 models[].max-context-length。

        这是唯一有 config.yaml 落点的探测：
          max-context-length → service_models.go:702-706 → model_registry.go:1242
          → codex/models/models.go:207-211 → context_window / max_context_window
        客户端据此定压缩点，所以它直接修掉「967k 逼近 995k 只剩 3% 余量」那个 400。
        """
        if not self.probe_context or not v.models:
            return
        model = v.models[0]
        limit, untrusted = self._bisect(row, v, model)
        v.max_context_length = limit
        v.context_model = model          # 只对这个模型有效，别外推
        v.context_untrusted = untrusted
        if limit:
            self.on_event("context", {"section": v.section, "model": model,
                                      "limit": limit, "untrusted": untrusted})

    # 上限直接写在错误正文里的常见形态。命中任一即可免掉整轮二分。
    #
    # 为什么值得单独做：二分最多 6 次请求，body 20 万-110 万字符，
    # 上传本身就要数秒到数十秒 —— 这是「探测要十几分钟」的第二大笔。
    # 而绝大多数上游在超限时会**明说**上限是多少：
    #   OpenAI 系  maximum context length is 200000 tokens
    #   Claude 系  prompt is too long: 215000 tokens > 200000 maximum
    #   国内中转    最大上下文长度为 128000
    # 有明说就用它，不必自己试出来。
    _LIMIT_PATTERNS = (
        # "maximum context length is 200000 tokens"
        r"maximum\s+context\s+length\s+is\s+(\d{4,8})",
        # "prompt is too long: 215000 tokens > 200000 maximum"
        r">\s*(\d{4,8})\s*maximum",
        # "context_length_exceeded ... limit 128000"
        r"context[_\s-]?length[^\d]{0,40}?(\d{4,8})",
        # "max_tokens ... 200000" / "max input tokens: 200000"
        r"max(?:imum)?[_\s-]?(?:input[_\s-]?)?tokens?[^\d]{0,20}(\d{4,8})",
        # 中文形态
        r"最大(?:上下文)?(?:长度|token数?)[^\d]{0,10}(\d{4,8})",
        r"上下文[^\d]{0,10}(?:上限|限制)[^\d]{0,10}(\d{4,8})",
    )

    @classmethod
    def _limit_from_body(cls, excerpt: str) -> int | None:
        """从错误正文里抠出上游自报的上下文上限（tokens）。抠不到返回 None。

        返回的是 **token 数**，与 max-context-length 的单位一致
        （service_models.go:702-706 读的就是 token 数）。

        合理性下限 8000：低于这个值的数字几乎不可能是上下文上限，更可能是
        撞上了 max_tokens 输出上限、错误码或时间戳。宁可放弃走二分，
        也不能把一个错的小值写进 config.yaml —— 那会让客户端过早压缩。
        上限 2_000_000：再大的数字不是上下文窗口。
        """
        if not excerpt:
            return None
        low = excerpt.lower()
        for pat in cls._LIMIT_PATTERNS:
            m = re.search(pat, low)
            if not m:
                continue
            try:
                val = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 8_000 <= val <= 2_000_000:
                return val
        return None

    def _bisect(self, row: ParsedRow, v: SectionVerdict, model: str) -> tuple[int | None, bool]:
        """二分实际可接受上下文。返回 (上限, 是否因截断而不可信)。

        截断校验：200 但 input_tokens < 发送量*0.5 说明上游截了，那个 200
        不算通过。relay-m 发 105 万字符只回 132,696 tokens，模型还被换成
        codex-auto-review —— 200 完全不可信，此时实测 token 数才是真容量。
        """
        lo, hi, rounds = 200_000, 1_100_000, 4

        # 上游自报的上限（tokens）。任何一次失败的正文里读到就记下来 ——
        # 读到就不必再试了，省掉最多 5 次百万字符请求。
        declared: int | None = None

        def check(chars: int) -> tuple[bool, int | None]:
            nonlocal declared
            att = self._call(
                v.section, v.base_url, row.api_key, model,
                combo=f"ctx-{chars // 1000}k",
                proxy=self.live_proxy if v.need_proxy else None,
                text="x" * chars,
                **self._profile_kwargs(v, row.api_key),
            )
            v.attempts.append(att)
            if not att.ok:
                if declared is None:
                    declared = self._limit_from_body(att.excerpt)
                    if declared is not None:
                        self.on_event("context-declared",
                                      {"section": v.section, "model": model,
                                       "limit": declared})
                return False, None
            tok = att.input_tokens
            if tok is not None and tok < chars * 0.5:
                return False, tok  # 截断：tok 就是真实容量
            return True, tok

        # 先打 hi。**顺序从「先 lo 后 hi」改成「先 hi」**，这是省时间的关键：
        #   · hi 通过  → 上限 >= hi，一次请求就结束（老逻辑要两次）
        #   · hi 超限  → 正文往往直接写着上限，解析到就结束（老逻辑要 6 次）
        # 只有「hi 失败且正文没说」才需要往下二分。
        #
        # 代价：hi 是 110 万字符，比 lo 贵。但它同时也是最可能一次定音的那一发，
        # 期望请求数从 2-6 降到 1-2。
        ok_hi, trunc_hi = check(hi)
        if ok_hi:
            return hi, False
        if trunc_hi:
            return trunc_hi, True
        if declared is not None:
            # 上游明说了上限。注意单位：declared 是 **token 数**，
            # 而 lo/hi 是 **字符数** —— 二者不能混算。
            # max-context-length 要的正是 token 数（service_models.go:702-706），
            # 所以直接返回 declared，不做任何字符换算。
            return declared, False

        ok, trunc = check(lo)
        if not ok:
            if declared is not None:
                return declared, False
            return (trunc, True) if trunc else (None, False)

        left, right = lo, hi
        for _ in range(rounds):
            if right - left <= 20_000:
                break
            mid = (left + right) // 2
            ok_mid, trunc_mid = check(mid)
            if ok_mid:
                left = mid
            elif trunc_mid:
                return trunc_mid, True
            elif declared is not None:
                return declared, False
            else:
                right = mid
        return left, False

    # ---------- 形态复用 ----------

    def _reuse_shape(
        self, row: ParsedRow, section: str, shape: SectionVerdict
    ) -> SectionVerdict:
        """同主机的第 2..N 个 Key：套用已学到的段形态，只验凭证本身。

        为什么可以复用：段的形态是**主机**的属性 ——
          · 这个站在这一段上有哪些模型      （站方的渠道配置）
          · 要不要走代理                    （站方的边缘防护）
          · 最小必需标识头                  （站方的 UA/Originator 校验）
          · 上下文窗口上限                  （站方给这个模型的容量）
          · 有没有静默换模                  （站方的路由行为）
        换一个 Key 不会改变其中任何一条。实测日志：5 行全是 relay-i.example，
        却把 4 段从头到尾各探 5 遍 —— 36 次 × 5 = 180 次请求，其中
        144 次在重复求证同一件事。

        为什么仍要发一次请求：**凭证有效性是 Key 的属性，不是主机的**。
        同一个站的 5 个 Key 完全可能一个欠费、一个被封、三个正常。所以
        每段仍打一次基线（带上已学到的 headers 与代理），只是不再重跑
        模型目录扫描、换模采样、上下文二分 —— 那三步问的都是主机的事。

        代价从 36 次降到 4 次（每段 1 次），且判定精度不降：凭证坏了照样
        当场发现，只是不再为同一个主机重复学习同一套形态。
        """
        base = base_for_section(row.bare, section)
        v = SectionVerdict(section=section, base_url=base)
        # 目录是**主机**的属性（站方声明有哪些模型），与 Key 无关 —— 直接
        # 沿用，不重新 GET。放在所有分支之前：底下有三个提前 return，
        # 判死的段也要带着候选清单回去给操作员人工接管用。
        v.catalog = list(shape.catalog)

        if not shape.models:
            # 主机在这一段本就没有可信模型 —— 换 Key 也不会变出模型来。
            # 直接沿用结论，一次请求都不必发。
            v.usable = shape.usable
            v.category, v.action = shape.category, shape.action
            self.on_event("shape-reused", {"section": section, "host": row.host,
                                           "verified": False,
                                           "reason": "该段无可信模型，无需逐 Key 复验"})
            return v

        probe_model = shape.models[0]
        att = self._call(
            section, base, row.api_key, probe_model,
            combo="reuse-verify",
            proxy=self.live_proxy if shape.need_proxy else None,
            **self._profile_kwargs(shape, row.api_key),
        )
        v.attempts.append(att)
        v.category, v.action = att.category, att.action

        if not att.ok:
            # 这个 Key 在这一段不通（欠费 / 被封 / 权限不同）。
            # 不回退到全量探测：形态已知，失败原因就是凭证本身。
            self.on_event("shape-reused", {"section": section, "host": row.host,
                                           "verified": True, "ok": False,
                                           "reason": f"{att.category} — {att.action}"})
            return v

        if not fingerprint.model_matches(probe_model, att.resp_model):
            # 同主机换 Key 后开始换模 —— 站方可能按 Key 分渠道。
            # 这种情况形态不能复用，退回全量探测。
            self.on_event("shape-reuse-abort", {
                "section": section, "host": row.host,
                "reason": f"复验时请求 {probe_model} 却回 {att.resp_model}，"
                          f"该 Key 渠道与首个 Key 不同，改走全量探测",
            })
            return self._full_probe(row, section)

        # 凭证有效且模型对得上 —— 套用主机形态
        v.usable = True
        v.models = list(shape.models)
        v.need_proxy = shape.need_proxy
        v.min_headers = dict(shape.min_headers)
        v.profile_name = shape.profile_name
        v.min_body_kind = shape.min_body_kind
        v.time_window = shape.time_window
        v.swap = dict(shape.swap)
        v.max_context_length = shape.max_context_length
        v.context_model = shape.context_model
        v.context_untrusted = shape.context_untrusted
        self.on_event("shape-reused", {"section": section, "host": row.host,
                                       "verified": True, "ok": True,
                                       "models": len(v.models)})
        return v

    # ---------- 编排 ----------

    def _full_probe(self, row: ParsedRow, section: str) -> SectionVerdict:
        """一个段的完整探测。首个 Key 走这条，之后复用它的形态。

        目录发现在 `_stage1` 内部最前面（`_stage0_catalog`），所以判死的段
        也带着 `v.catalog` 回来 —— 操作员人工接管时需要那份候选清单。
        只有「验穷模型 + 换模抽样 + 上限实测」这三步跳过：段不通时它们全都
        问不出有效结果，白烧请求。
        """
        v = self._stage1(row, section)
        if v.usable:
            self._stage2(row, v)
            self._stage4_swap(row, v)
            self._stage4_context(row, v)
        return v

    def _probe_one_section(self, row: ParsedRow, section: str) -> SectionVerdict:
        """探一个段。probe() 的工作单元，串行与并行共用同一份逻辑。

        (host, section) 上做 single-flight：同一主机同一段的完整探测**最多
        只有一个在跑**，后到的等它出结果再走复用路径。

        为什么需要这道门闩：形态学习是这里最贵的动作（模型目录扫描 + 换模
        采样 + 上下文二分，最多 12 次请求，含 4 次百万字符的大 body）。
        没有门闩时，同一主机的多个 Key 一旦并发进来，都会看到
        _shape 里还没有条目，于是各自跑一遍完整探测 —— 5 个 Key 就是
        5 倍开销，而它们学到的形态必然相同（形态是主机的属性）。

        用 per-key 的 Event 而不是全局锁：不同 (host, section) 之间不该互等。
        """
        key = (row.host, section)
        while True:
            with self._lock:
                shape = self._shape.get(key)
                if shape is not None:
                    break                       # 已有形态，走复用
                gate = self._inflight.get(key)
                if gate is None:
                    # 本线程认领这次形态学习
                    gate = threading.Event()
                    self._inflight[key] = gate
                    owner = True
                else:
                    owner = False               # 别人在学，等它
            if owner:
                try:
                    v = self._full_probe(row, section)
                    with self._lock:
                        # 只有真正学到东西才存 —— 凭证类失败（欠费）是 Key 的
                        # 属性，存下来会让后面的 Key 错误地继承别人的欠费结论。
                        if v.usable:
                            self._shape[key] = v
                finally:
                    # 无论成败都必须放闸，否则等待方永久卡死
                    with self._lock:
                        self._inflight.pop(key, None)
                    gate.set()
                return v
            # 等认领者出结果。超时兜底：认领者若卡在长 timeout 上，
            # 等待方不该被无限期拖住 —— 醒来重判，那时要么有形态可复用，
            # 要么门闩已清空，由本线程接手认领。
            gate.wait(timeout=self.timeout + 30)

        return self._reuse_shape(row, section, shape)

    def probe(self, row: ParsedRow) -> CandidateResult:
        """探一个候选的四段。

        四段并行（workers>1 时）。为什么可以并行：
          · 四段打的是四个不同端点，业务上互不依赖
          · _throttle 按 (host, section) 计时，同段内仍严格保持 gap
          · _shape / _last_call / _proxy_state 三处共享状态都在 self._lock 下
          · SectionVerdict 每段一个对象，不跨段写

        为什么这一步收益最大：原来四段共享一个 gap 桶且串行，单站 56 次请求
        要 55 x 3s = 165 秒纯睡。拆桶 + 并行后，四段各自睡自己的，
        墙钟时间取四段里最慢的那一段，而不是四段之和。
        """
        res = CandidateResult(row=row)
        self.on_event("candidate-start", {"host": row.host, "key": row.masked()})

        if self.workers > 1:
            # 结果按 SECTIONS 原序回填 —— 前端表格与 CLI 输出都依赖这个顺序，
            # 不能让完成先后决定展示顺序。
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(self.workers, len(SECTIONS)),
                    thread_name_prefix="probe-sec") as ex:
                futs = {ex.submit(self._probe_one_section, row, sec): sec
                        for sec in SECTIONS}
                done: dict[str, SectionVerdict] = {}
                for fut in concurrent.futures.as_completed(futs):
                    sec = futs[fut]
                    try:
                        done[sec] = fut.result()
                    except Exception as e:      # noqa: BLE001
                        # 一段炸了不该拖垮另外三段。记成不可用并带上原因，
                        # 让它照常走「不可用」那条展示路径。
                        v = SectionVerdict(
                            section=sec,
                            base_url=base_for_section(row.bare, sec))
                        v.category, v.action = "死路", f"探测异常：{e}"
                        done[sec] = v
                        self.on_event("section-error",
                                      {"section": sec, "host": row.host,
                                       "error": str(e)})
            for sec in SECTIONS:
                v = done[sec]
                res.sections[sec] = v
                self.on_event("section-done", {"section": sec, "host": row.host,
                                               "usable": v.usable,
                                               "summary": v.summary()})
        else:
            for section in SECTIONS:
                v = self._probe_one_section(row, section)
                res.sections[section] = v
                self.on_event("section-done", {"section": section, "host": row.host,
                                               "usable": v.usable,
                                               "summary": v.summary()})

        self.on_event("candidate-done", {"host": row.host,
                                         "usable": res.usable_sections,
                                         "calls": res.total_calls})
        return res
