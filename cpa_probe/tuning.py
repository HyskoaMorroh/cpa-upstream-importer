"""全局调优段：把「重试预算」与「顶层池大小」的耦合算出来，不靠人工核对。

为什么需要这个模块（2026-09-12，来自 `CPA配置修改需求.md` 与 docx 第 7 条）
------------------------------------------------------------------
CPA 的 `max-retry-credentials` 与本项目写的 `priority` 是**耦合**的，而这份
耦合原来只写在 config.yaml 的注释里，靠人工每次改完档位回头核对：

    「这两个数字是耦合的：**改顶层池大小就必须回头核对这里**，
      用 tools/diag403.py 一跑就知道够不够。」

而本项目正是**决定顶层池大小的那一方**（`plan.assign_priorities` 定档、
`writeback` 落盘）。人工核对必然漏：档位每次重探都可能变，注释不会自己更新。
两个方向的漏都有确定后果，且互相矛盾 —— 这就是必须算而不是猜的原因：

  · 预算 **<** 顶层池 —— 有可用凭据却没机会试。预算耗尽后 CPA
    `return lastErr` 原样透传（conductor_execution.go:325-330），
    客户端看到最后那个上游的 403。用户明确要求「挂机时绝不该看到 403」。
  · 预算 **>** 能在窗口内跑完的次数 —— 反向代理（Cloudflare 免费/Pro 的
    源站读超时 120 秒，不可调）先掐断，客户端拿 524
    `origin_response_timeout`，而 CPA 其实还在正常重试。

`CPA配置修改需求.md` 建议把 `max-retry-credentials` 从 12 压到 4。那份建议的
算术有一处**关键错误**，照做会直接制造上面第一种后果：

    它写「12 × (连接+等待) + 11 × 最多 8 秒退避」，把 `max-retry-interval`
    当成**每个凭据之间**都要等一次。但那一项是**各重试轮次之间**等待凭证
    冷却的上限（config.yaml:249 原注释），轮数 = `request-retry` + 1。
    当前 `request-retry: 1` → 只有一次轮间等待，退避总量是 8 秒而不是 88 秒。

所以本模块不采纳那两个建议值，改为**从配置实际形态算**：读顶层池的真实
大小、算最坏静默时长、与窗口比。算得出来的结论才跟得上档位变化。

本模块只做计算与建议，不写盘 —— 写盘走 `writeback.global_tuning_diffs`，
并且与其余写回一样要操作员在界面上确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parse import host_of

# 反向代理的源站读超时。Cloudflare 免费/Pro 固定 120 秒且**不可从源站侧延长**
# —— nginx 的 `proxy_read_timeout 600` 对它无效，那是 CF 边缘自己的计时器。
#
# 做成参数而不是写死：自建入口 / Enterprise 套餐 / 直连（无 CF）的窗口不同，
# 而窗口是这套计算的唯一外部约束，写死它等于把结论钉在一种部署上。
DEFAULT_EDGE_WINDOW_SEC = 120.0

# 留给「一个慢站」的余量。窗口不是给重试独占的：任何一次尝试都可能是那个
# 首字节特别慢的上游，而它的耗时不在「平均每次」里。
#
# 依据：config.yaml 原注释记的实测「foxtrot 首字节 18-33 秒、nova 约 19.3 秒」，
# 取 30 秒覆盖到那批里最慢的。
DEFAULT_SLOW_SITE_MARGIN_SEC = 30.0

# 没有实测数据时，单次失败尝试按多少秒估。
#
# 依据是本项目自己的实测记录（config.yaml 注释）：「顶层平均 2.9 秒/次」，
# 另一处记「单次失败约 2 秒」。取大的那个 —— 低估会算出过大的预算，
# 而过大的预算是 524 那一侧的后果。
#
# 有探测结果时不用这个值，用实测的（见 `attempt_seconds_from_results`）。
FALLBACK_ATTEMPT_SEC = 2.9

# 同一个站在顶层数组里连续出现多少个就算有速率限制风险。
#
# 为什么会有这个约束：平滑加权轮询在**权重相同时严格按数组顺序**轮转
# （selector.go:539-560），而条目按站分组连续排列。于是预算越大，越可能
# 连着打同一个站 —— config.yaml 注释记的实测：「8 次请求落在同一个 75 毫秒
# 窗口内，触发了该站前面 Cloudflare 的速率限制」，日志里还表现为 HTML
# 挑战页而不是真正的 403，一度被误判成出口 IP 被封。
#
# 取 3：注释里定的改法就是「把每站顶层压到 3 个」。
MAX_SAME_HOST_RUN = 3

_SECTIONS = ("gemini-api-key", "codex-api-key", "claude-api-key",
             "openai-compatibility")


@dataclass
class TierFacts:
    """一个段的顶层池实况。全部从 config.yaml 读，不含任何猜测。"""

    section: str
    top_priority: int | None = None
    credentials: int = 0
    hosts: list[str] = field(default_factory=list)
    # 顶层数组里同一个站最长连续多少个。见 MAX_SAME_HOST_RUN。
    longest_same_host_run: int = 0
    run_host: str = ""


@dataclass
class Advice:
    """一条调优建议。`current` 与 `want` 相等时不产生 diff。"""

    path: tuple[str, ...]
    current: object
    want: object
    why: str
    # severity: "blocker" 会导致确定的错误行为；"warn" 是风险；"info" 是卫生问题
    severity: str = "warn"

    @property
    def changed(self) -> bool:
        return self.current != self.want

    @property
    def label(self) -> str:
        return ".".join(self.path)


def _entry_credentials(section: str, entry: dict) -> list[str]:
    """一个条目承载几把 Key。compat 段的 Key 挂在 api-key-entries 下。"""
    if section == "openai-compatibility":
        out = []
        for ke in entry.get("api-key-entries") or []:
            if isinstance(ke, dict) and ke.get("api-key"):
                out.append(str(ke["api-key"]))
        return out
    return [str(entry["api-key"])] if entry.get("api-key") else []


def _in_pool(section: str, entry: dict) -> bool:
    """这个条目在不在 CPA 的调度池里。

    不在池里的条目不占预算也不该被算进顶层池 —— 把它算进去会让预算虚高，
    而虚高的预算是 524 那一侧的后果。判据复用 `plan.entry_out_of_pool`
    与 `plan.entry_all_zero_weight`（两处已经逐条核对过 CPA 源码），
    不在这里另写一套。
    """
    from .plan import entry_all_zero_weight, entry_out_of_pool

    if entry_out_of_pool(section, entry):
        return False
    # weight: 0 只在 weighted-round-robin 下才真的逐出调度池；其余策略不读
    # weight。这里保守地**仍然算进池**（宁可预算略大也不要漏试），只有确定
    # 被排除的（disabled / excluded-models 含 `*`）才剔除。
    return True


def tier_facts(cfg: dict) -> dict[str, TierFacts]:
    """逐段算出顶层池实况。"""
    out: dict[str, TierFacts] = {}
    for section in _SECTIONS:
        rows = [e for e in (cfg.get(section) or [])
                if isinstance(e, dict) and _in_pool(section, e)]
        facts = TierFacts(section=section)
        prios = [e.get("priority") for e in rows
                 if isinstance(e.get("priority"), int)]
        if not prios:
            out[section] = facts
            continue
        top = max(prios)
        facts.top_priority = top
        # 按**原数组顺序**取顶层条目 —— 连续同站的判断依赖这个顺序
        # （selector.go 权重相同时严格按数组序轮转）。
        seq: list[str] = []
        for e in rows:
            if e.get("priority") != top:
                continue
            host = host_of(str(e.get("base-url") or ""))
            for _key in _entry_credentials(section, e):
                seq.append(host)
        facts.credentials = len(seq)
        facts.hosts = sorted({h for h in seq if h})
        run = best = 0
        prev = None
        for host in seq:
            run = run + 1 if host == prev else 1
            prev = host
            if run > best:
                best, facts.run_host = run, host
        facts.longest_same_host_run = best
        out[section] = facts
    return out


def attempt_seconds_from_results(results) -> tuple[float, str]:
    """从探测结果里取「单次尝试要多久」。返回 (秒, 依据说明)。

    用**失败尝试**的耗时而不是全部：预算花在失败上，成功那次直接返回、
    不消耗后续预算。取 90 分位而不是均值 —— 最坏情况才是与窗口相比的那个数。

    拿不到样本时返回 FALLBACK_ATTEMPT_SEC 并在说明里讲清那是估值。
    """
    samples: list[float] = []
    for res in (results or {}).values():
        for verdict in (getattr(res, "sections", None) or {}).values():
            for att in getattr(verdict, "attempts", None) or []:
                if getattr(att, "ok", False):
                    continue
                ms = getattr(att, "elapsed_ms", 0) or 0
                if ms > 0:
                    samples.append(ms / 1000.0)
    if not samples:
        return FALLBACK_ATTEMPT_SEC, (
            f"无实测样本，按 {FALLBACK_ATTEMPT_SEC} 秒/次估")
    samples.sort()
    idx = min(len(samples) - 1, int(round(0.9 * (len(samples) - 1))))
    return samples[idx], f"{len(samples)} 次失败尝试的 90 分位"


def worst_case_seconds(*, budget: int, rounds: int, attempt_sec: float,
                       interval_sec: float) -> float:
    """最坏静默时长。

    形状（与 config.yaml 原注释算的那一版一致）：

        rounds × budget × attempt_sec + (rounds - 1) × interval_sec

    `interval_sec` 是**轮间**等待冷却的上限，不是每个凭据之间都等一次 ——
    这正是 `CPA配置修改需求.md` 算错的地方，见模块 docstring。
    """
    rounds = max(1, rounds)
    return rounds * budget * attempt_sec + (rounds - 1) * max(0.0, interval_sec)


def advise(cfg: dict, *, attempt_sec: float | None = None,
           attempt_why: str = "",
           edge_window_sec: float = DEFAULT_EDGE_WINDOW_SEC,
           slow_site_margin_sec: float = DEFAULT_SLOW_SITE_MARGIN_SEC
           ) -> tuple[list[Advice], list[str], dict[str, TierFacts]]:
    """算出全局调优建议。返回 (建议列表, 说明/警告, 顶层池实况)。

    只读 cfg，不改它，也不写盘。
    """
    facts = tier_facts(cfg)
    notes: list[str] = []
    out: list[Advice] = []

    if attempt_sec is None:
        attempt_sec, attempt_why = FALLBACK_ATTEMPT_SEC, (
            f"无实测样本，按 {FALLBACK_ATTEMPT_SEC} 秒/次估")
    attempt_sec = max(0.1, float(attempt_sec))

    retry = cfg.get("request-retry")
    rounds = (retry + 1) if isinstance(retry, int) and retry >= 0 else 2

    # ── ① max-retry-credentials：至少覆盖最大的那个顶层池 ──
    #
    # 为什么取**最大**而不是逐段各配一个：CPA 的 `max-retry-credentials`
    # 是**全局**一项（config.yaml 顶层），四段共用。取最大才能保证每段的
    # 顶层凭据都有机会被试到；取小的那个会让凭据最多的那一段又回到
    # 「有可用凭据却没机会试」。
    need = max((f.credentials for f in facts.values()), default=0)
    driver = max(facts.values(), key=lambda f: f.credentials, default=None)
    cur_budget = cfg.get("max-retry-credentials")
    cur_interval = cfg.get("max-retry-interval")
    interval = cur_interval if isinstance(cur_interval, int) else 0

    # 窗口能容纳多少次尝试（扣掉慢站余量与轮间等待）
    usable = edge_window_sec - slow_site_margin_sec - (rounds - 1) * max(0, interval)
    fits = int(usable // (rounds * attempt_sec)) if usable > 0 else 0

    if need == 0:
        notes.append("四段都没有可计费凭据，重试预算无从计算 —— 跳过这一项")
    elif need <= fits:
        # 覆盖顶层池且跑得完 —— 这才是两个方向都成立的取值
        if isinstance(cur_budget, int):
            out.append(Advice(
                ("max-retry-credentials",), cur_budget, need,
                f"顶层池最大的是 {driver.section}（{need} 个凭据，"
                f"档位 {driver.top_priority}）。预算必须 >= 它，否则有可用凭据"
                f"却没机会试，预算耗尽后 CPA 原样透传最后那个错误"
                f"（conductor_execution.go:325-330）—— 挂机时客户端就会看到 403。"
                f"按{attempt_why}（{attempt_sec:.1f} 秒/次）算，{need} 个凭据"
                f"最坏静默 {worst_case_seconds(budget=need, rounds=rounds, attempt_sec=attempt_sec, interval_sec=interval):.0f} 秒，"
                f"在 {edge_window_sec:.0f} 秒窗口内仍留 "
                f"{edge_window_sec - worst_case_seconds(budget=need, rounds=rounds, attempt_sec=attempt_sec, interval_sec=interval):.0f} 秒余量",
                "blocker" if cur_budget < need else "warn"))
    else:
        # 冲突：覆盖顶层池就跑不完窗口。**不能靠压预算解决** —— 那是
        # 「透传 403」那一侧的后果。真正的解法是压顶层池（本项目做得到）。
        notes.append(
            f"⚠ 冲突：{driver.section} 顶层有 {need} 个凭据，而 "
            f"{edge_window_sec:.0f} 秒窗口（扣 {slow_site_margin_sec:.0f} 秒慢站余量"
            f"与 {(rounds - 1) * max(0, interval)} 秒轮间等待）只跑得完 {fits} 个。"
            f"压预算会让多出来的凭据永远轮不到并透传 403；正确的解法是把"
            f"该段每站的顶层凭据数压下来（其余降一档），让顶层池 <= {fits}。"
            f"本项目的批量定档能做这件事 —— 这一项不自动改预算，交给你决定")
        if isinstance(cur_budget, int) and cur_budget > fits:
            out.append(Advice(
                ("max-retry-credentials",), cur_budget, fits,
                f"仅在你不打算压顶层池时才用这个值：把预算压到窗口跑得完的 "
                f"{fits} 个，代价是另外 {need - fits} 个顶层凭据永远轮不到",
                "warn"))

    # ── ② 轮间等待：留着，但不能吃掉窗口 ──
    #
    # 为什么不像 .md 说的压到 2：这一项为 0 或过小时，「所有候选都在冷却」
    # 那一支会直接停止重试并原样透传（conductor_selection.go:977-982），
    # 而普通 403 会让凭据冷却 30 分钟（conductor_cooldown.go:813-821，
    # 硬编码不可配）。挂机场景下宁可等几秒也不要透传。
    #
    # 上限来自窗口：轮间等待与两轮尝试同时挤在同一个 120 秒里。
    if isinstance(cur_interval, int) and rounds > 1 and need:
        budget_for_calc = need if need <= fits else (
            cur_budget if isinstance(cur_budget, int) else need)
        room = (edge_window_sec - slow_site_margin_sec
                - rounds * budget_for_calc * attempt_sec)
        cap = int(max(0, room) // (rounds - 1))
        if cur_interval > cap:
            out.append(Advice(
                ("max-retry-interval",), cur_interval, cap,
                f"两轮尝试要 {rounds * budget_for_calc * attempt_sec:.0f} 秒，"
                f"加上 {slow_site_margin_sec:.0f} 秒慢站余量，轮间等待最多还能占 "
                f"{cap} 秒。当前 {cur_interval} 秒会把最坏静默推过 "
                f"{edge_window_sec:.0f} 秒窗口，客户端拿 524 而不是真实错误码",
                "blocker"))
        elif cur_interval <= 0:
            out.append(Advice(
                ("max-retry-interval",), cur_interval, min(8, cap),
                f"为 0 时「所有候选都在冷却」会直接停止重试并原样透传"
                f"（conductor_selection.go:977-982），而普通 403 冷却 30 分钟。"
                f"挂机时宁可等几秒也不要把 403 交给客户端；窗口还容得下 {cap} 秒",
                "warn"))

    # ── ③ 流式引导缓冲：这一项 .md 说得对 ──
    #
    # `stream-bootstrap-buffering: false` 时 CPA 在**尚未确认上游会吐 token**
    # 之前就提交了 200 + text/event-stream 响应头。此后上游断开就已无退路：
    # 改不成 5xx（头发出去了）、换不了凭据（客户端在读这个流了），
    # 结果是一个语法合法但 0 个事件的 200 —— 与用户报的
    # `StreamNoEventsError; 0 stream events received` 吻合。
    #
    # 代价（照实说）：首字节延迟增加约等于上游的首事件延迟。本部署 nginx
    # 侧 proxy_read_timeout 600、CF 侧 120 秒，都容得下实测的 18-33 秒首字节。
    codex = cfg.get("codex")
    if isinstance(codex, dict) and "stream-bootstrap-buffering" in codex:
        cur = codex.get("stream-bootstrap-buffering")
        if cur is not True:
            out.append(Advice(
                ("codex", "stream-bootstrap-buffering"), cur, True,
                "false 时 CPA 在未确认上游产出第一个事件之前就提交了 200 + "
                "text/event-stream 响应头，之后上游断开已无退路（改不成 5xx、"
                "换不了凭据），只能给出 0 个事件的 200 —— 即客户端报的 "
                "StreamNoEventsError。改 true 后失败仍可重试或返回真实错误码，"
                "代价是首字节延迟增加约等于上游首事件延迟",
                "blocker"))

    streaming = cfg.get("streaming")
    if isinstance(streaming, dict) and "bootstrap-retries" in streaming:
        cur = streaming.get("bootstrap-retries")
        if isinstance(cur, int) and cur < 3:
            out.append(Advice(
                ("streaming", "bootstrap-retries"), cur, 3,
                "引导阶段失败时可以换凭据重来，而不是把空流交给客户端。"
                "引导阶段尚未向下游写任何事件，所以重试不会产生重复内容",
                "warn"))

    # ── ④ debug 日志 ──
    #
    # 这一项是卫生问题而不是故障：CPA 的 debug 日志可能含请求内容与凭据片段，
    # 而日志目录是 bind-mount 出去的。排障期开着是对的，长期开着不是。
    if cfg.get("debug") is True:
        out.append(Advice(
            ("debug",), True, False,
            "debug 日志可能含请求内容与凭据片段，而日志目录是 bind-mount "
            "出去的。正在排障就留着，否则关掉",
            "info"))

    # ── ⑤ 连续同站：预算再大也别踩速率限制 ──
    for f in facts.values():
        if f.longest_same_host_run > MAX_SAME_HOST_RUN:
            notes.append(
                f"⚠ {f.section} 顶层有 {f.longest_same_host_run} 个连续的同站凭据"
                f"（{f.run_host}）。权重相同时轮询严格按数组顺序"
                f"（selector.go:539-560），于是一次请求会连打这个站 "
                f"{f.longest_same_host_run} 次 —— 实测这会触发站方前面 "
                f"Cloudflare 的速率限制，返回 HTML 挑战页而不是真正的错误。"
                f"建议把该站顶层压到 {MAX_SAME_HOST_RUN} 个以内（其余降一档）")

    return out, notes, facts
