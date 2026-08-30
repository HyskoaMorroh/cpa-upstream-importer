#!/usr/bin/env python3
"""403 诊断：为什么客户端经 CPA 拿到 403，以及该改哪个配置。

    python3 tools/diag403.py [config.yaml 路径]
    python3 tools/diag403.py /opt/deploy/config.yaml --section claude-api-key

只读。不发任何请求、不改任何文件。

它回答什么
----------
客户端报 403 时，第一反应往往是「某个 key 坏了」。但在 CPA 的**层级隔离**下，
真正的问题通常是结构性的：

    sdk/cliproxy/auth/selector.go:325-333
    if priority > bestPriority { ... }      ← 只保留最高那一层

**只有最高 priority 那一档参与轮询，其余整层被跳过。** 所以 206 个凭据里
可能只有 5 个在真正服务 —— 那 5 个所属的站一挂，整段就 403，另外 201 个
凭据一个都不会被用到（它们不是备份，是死重量）。

叠加 `request-retry` 的语义：那是**总尝试次数**的上限。设成 1 表示
「打一次，失败就把错误透传给客户端」，连换一个凭据的机会都没有。

这个脚本把这两件事算出来，并指出哪些改动能立刻扩大可用池。
"""

from __future__ import annotations

import collections
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

SECTIONS = ("gemini-api-key", "codex-api-key", "claude-api-key",
            "openai-compatibility")

C_OK, C_BAD, C_WARN, C_DIM, C_END = (
    "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m")
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C_OK = C_BAD = C_WARN = C_DIM = C_END = ""


def host_of(url: str) -> str:
    s = str(url or "")
    for p in ("https://", "http://"):
        if s.startswith(p):
            s = s[len(p):]
    return s.split("/")[0] or "(无 base-url)"


def _weight_zero(entry: dict, section: str) -> bool:
    """这个条目的凭据是否已被 weight<=0 逐出（CPA 归零后 selector 剔除）。

    compat 段的 weight 在 api-key-entries 里，条目级读不到 —— 与
    cpa_probe.plan.entry_all_zero_weight 同口径。
    """
    def zero(w) -> bool:
        if w is None:
            return False
        try:
            return float(w) <= 0
        except (TypeError, ValueError):
            return False

    if section == "openai-compatibility":
        ws = [ke.get("weight") if isinstance(ke, dict) and "weight" in ke else None
              for ke in (entry.get("api-key-entries") or [])]
        return bool(ws) and all(zero(w) for w in ws)
    return zero(entry.get("weight")) if "weight" in entry else False


def _longest_run(seq: list) -> int:
    """最长连续相同元素的长度。CPA 按数组序轮转，所以这个数就是
    「最多会连打同一个站几次」。"""
    if not seq:
        return 0
    best = cur = 1
    for i in range(1, len(seq)):
        cur = cur + 1 if seq[i] == seq[i - 1] else 1
        best = max(best, cur)
    return best


def entry_hosts(section: str, entries: list) -> list[tuple[str, int, dict]]:
    """返回 [(host, priority, 原条目)]。compat 段一个条目=一个站。"""
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        out.append((host_of(e.get("base-url")), int(e.get("priority") or 0), e))
    return out


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
        else os.path.join(os.path.dirname(ROOT), "config.yaml")
    want_sec = ""
    if "--section" in sys.argv:
        i = sys.argv.index("--section")
        if i + 1 < len(sys.argv):
            want_sec = sys.argv[i + 1]

    if not os.path.exists(path):
        print(f"找不到 {path}")
        return 2

    try:
        import yaml
    except ImportError:
        print("需要 PyYAML：pip3 install pyyaml")
        return 2

    raw = io.open(path, encoding="utf-8").read()
    cfg = yaml.safe_load(raw)
    if not isinstance(cfg, dict):
        print(f"{path} 顶层不是映射")
        return 2

    print(f"{'=' * 72}")
    print(f"403 诊断 · {path}")
    print(f"{len(raw.splitlines())} 行 · "
          f"四段 {sum(len(cfg.get(s) or []) for s in SECTIONS)} 条目")
    print(f"{'=' * 72}")

    # ── ① 重试配置 ────────────────────────────────────────────────────
    print(f"\n{'─' * 72}\n① 重试与容错配置\n{'─' * 72}")
    rr = cfg.get("request-retry")
    mrc = cfg.get("max-retry-credentials")
    mri = cfg.get("max-retry-interval")
    dc = cfg.get("disable-cooling")
    tec = cfg.get("transient-error-cooldown-seconds")

    print(f"  request-retry                     {rr}")
    print(f"  max-retry-credentials             {mrc}")
    print(f"  max-retry-interval                {mri}")
    print(f"  disable-cooling                   {dc}")
    print(f"  transient-error-cooldown-seconds  {tec}")

    findings: list[str] = []

    # 以下判定全部基于读源码确认的行为，不是猜测：
    #   isCredentialRetryRoundStatus  conductor_selection.go:1038-1051
    #       403/408/429/500/502/503/504 都算「该换凭据」—— 403 在内
    #   MarkResult                    conductor_cooldown.go:813-821
    #       普通 403 → suspendReason="payment_required"，冷却 **30 分钟硬编码**
    #   isCloudflareChallengeErrorMessage  conductor_cooldown.go:1561-1567
    #       只有 CF 挑战是特例（退避从 10 秒起，不长封）
    #   shouldRetryAfterErrorWithHomeRetryLimit  conductor_selection.go:976-981
    #       所有候选都在冷却时，maxWait<=0 就 return 0,false —— **直接放弃**

    if isinstance(rr, int) and rr <= 1:
        findings.append(
            f"{C_BAD}request-retry = {rr}{C_END} —— 额外重试轮数（默认 3）。\n"
            f"      403 本身是「该换凭据」的状态码"
            f"（conductor_selection.go:1038-1051 里 403 在列），\n"
            f"      但 {rr} 轮意味着换凭据的机会极少。你有 200+ 凭据，"
            f"却几乎在第一个 403 上就放弃。\n"
            f"      {C_OK}建议 3~5{C_END}")

    # max-retry-interval 是最隐蔽的一个 —— 它不是「等待上限」那么无害
    if isinstance(mri, int) and mri <= 0:
        findings.append(
            f"{C_BAD}max-retry-interval = {mri}{C_END} —— 这一项最容易被误解。\n"
            f"      它不只是「不等待」：conductor_selection.go:976-981 的逻辑是 ——\n"
            f"        · 有立刻可用的凭据  → 立刻重试（不受这一项影响）\n"
            f"        · {C_BAD}所有候选都在冷却 → maxWait<=0 就直接放弃，"
            f"把 403 透传给客户端{C_END}\n"
            f"      而普通 403 会让凭据**冷却 30 分钟**"
            f"（conductor_cooldown.go:813-821，硬编码，不可配）。\n"
            f"      所以顶层那几个凭据一旦接连 403，整层在 30 分钟内全在冷却，\n"
            f"      而这一项让 CPA 连「等几秒再试」都不做 —— 直接 403。\n"
            f"      {C_OK}建议 30（默认值）{C_END}：愿意等最多 30 秒，"
            f"就能跨过短冷却窗口。")

    if dc is True:
        findings.append(
            f"{C_BAD}disable-cooling = true{C_END} —— 冷却被关掉了。"
            f"返 403 的凭据不会被暂时排除，\n"
            f"      于是同一个坏凭据会被反复选中，重试再多也是撞同一面墙。")

    if isinstance(mrc, int) and 0 < mrc <= 1:
        findings.append(
            f"{C_WARN}max-retry-credentials = {mrc}{C_END} —— 每轮最多尝试 {mrc} 个凭据"
            f"（0 = 不限，conductor_execution.go:325）。\n"
            f"      顶层有多个凭据时这会限死故障转移范围。")

    # ── ①b 预算 vs 顶层池：403 会不会被透传 ─────────────────────────
    print(f"\n{'─' * 72}")
    print("①b 预算 vs 顶层池 —— 决定 403 会不会透传给客户端")
    print(f"{C_DIM}   读源码确认：continue-and-cooldown **仍消耗预算**"
          f"（attempted 在 Execute 之前就标记，conductor_execution.go:362），")
    print(f"   预算耗尽后**返回 lastErr 原样透传**（:325-330）。"
          f"所以预算 < 顶层池 = 挂机时必然看到 403。{C_END}")
    print(f"{'─' * 72}")

    budget = ((rr if isinstance(rr, int) and rr > 0 else 1)
              * (mrc if isinstance(mrc, int) and mrc > 0 else 10 ** 6))
    print(f"  尝试预算 = request-retry({rr}) x max-retry-credentials({mrc}) "
          f"= {budget if budget < 10 ** 6 else '不限'} 个凭据/请求")

    for sec in SECTIONS:
        if want_sec and sec != want_sec:
            continue
        entries = [e for e in (cfg.get(sec) or []) if isinstance(e, dict)]
        # weight<=0 的凭据 CPA 已剔除，不占预算
        live = [e for e in entries if not _weight_zero(e, sec)]
        if not live:
            continue
        pairs = entry_hosts(sec, live)
        if not pairs:
            continue
        top = max(p for _h, p, _e in pairs)
        tops = [(h, e) for h, p, e in pairs if p == top]
        n = len(tops)
        seq = [h for h, _e in tops]
        run = _longest_run(seq)
        enough = budget >= n
        mark_b = C_OK if enough else C_BAD
        mark_r = C_OK if run < 8 else C_BAD
        print(f"\n  {sec}")
        print(f"    顶层 {top}：{n} 凭据 / {len(set(seq))} 站")
        print(f"    预算 {budget if budget < 10**6 else '不限'} vs 池 {n}  "
              f"{mark_b}{'能试遍' if enough else f'只能试 {budget}/{n} —— 余下的永远轮不到'}{C_END}")
        print(f"    最长连续同站 {mark_r}{run}{C_END}"
              f"{'' if run < 8 else '  <-- CF 速率限制风险（8 次落在 75ms 窗口就踩过）'}")
        print(f"    {C_DIM}最坏耗时 约 {n * 2} 秒（单次失败约 2 秒）{C_END}")

        if not enough:
            findings.append(
                f"{C_BAD}{sec} 的预算 {budget} 小于顶层池 {n}{C_END} —— "
                f"挂机时必然透传 403。\n"
                f"      余下 {n - budget} 个凭据**永远轮不到**："
                f"CPA 达到 max-retry-credentials 就返回最后那个错误"
                f"（conductor_execution.go:325-330）。\n"
                f"      {C_OK}修法{C_END}：把 max-retry-credentials 提到 {n}，"
                f"或把单站在顶层的凭据数压下来（见下一条）。")
        if run >= 8:
            findings.append(
                f"{C_BAD}{sec} 顶层最长连续同站 {run} 个{C_END} —— "
                f"CPA 的平滑加权轮询在权重相同时**严格按数组顺序轮转**"
                f"（selector.go:539-560），\n"
                f"      而条目按站分组排列，所以会连打同一个站 {run} 次。\n"
                f"      2026-08-26 实测：8 次请求落在 75 毫秒窗口内就触发了"
                f"Cloudflare 速率限制。\n"
                f"      {C_OK}修法{C_END}：把该站在顶层的凭据压到 3 个左右"
                f"（其余降一档），让轮询跨站交错。\n"
                f"      {C_DIM}注意 weight 解决不了这个 —— 它改变被选中的频率，"
                f"不改变同权重内的顺序。{C_END}")

    # ── ② 层级隔离：真正在服务的池有多大 ──────────────────────────────
    print(f"\n{'─' * 72}\n② 层级隔离 —— 实际参与轮询的池")
    print(f"{C_DIM}   只有最高 priority 那一层参与轮询（selector.go:325-333），"
          f"其余整层被跳过{C_END}\n{'─' * 72}")

    single_host_sections: list[tuple[str, str, int, int]] = []

    for sec in SECTIONS:
        if want_sec and sec != want_sec:
            continue
        entries = cfg.get(sec) or []
        if not entries:
            print(f"\n  {sec:24} {C_DIM}空{C_END}")
            continue
        pairs = entry_hosts(sec, entries)
        if not pairs:
            continue
        top = max(p for _h, p, _e in pairs)
        tops = [(h, e) for h, p, e in pairs if p == top]
        hosts = collections.Counter(h for h, _e in tops)

        share = f"{len(tops)}/{len(pairs)}"
        mark = C_BAD if len(hosts) == 1 else (
            C_WARN if len(hosts) == 2 else C_OK)
        print(f"\n  {sec}")
        print(f"    顶层 priority {top} —— {mark}{share}{C_END} 个凭据参与轮询，"
              f"分布在 {mark}{len(hosts)}{C_END} 个站")
        for h, n in hosts.most_common():
            print(f"        {h:36} {n}")
        if len(pairs) - len(tops) > 0:
            print(f"    {C_DIM}其余 {len(pairs) - len(tops)} 个凭据在下层，"
                  f"**一个都不会被用到**（不是备份，是死重量）{C_END}")

        if len(hosts) == 1:
            single_host_sections.append(
                (sec, next(iter(hosts)), len(tops), len(pairs)))

        # 下一档是什么 —— 提示「降到多少能把它并进池子」
        lower = sorted({p for _h, p, _e in pairs if p < top}, reverse=True)
        if lower:
            nxt = lower[0]
            nxt_hosts = collections.Counter(
                h for h, p, _e in pairs if p == nxt)
            nxt_n = sum(nxt_hosts.values())
            gap = top - nxt
            print(f"    {C_DIM}下一档 priority {nxt}：{nxt_n} 个凭据 / "
                  f"{len(nxt_hosts)} 个站（{', '.join(list(nxt_hosts)[:3])}"
                  f"{' …' if len(nxt_hosts) > 3 else ''}）{C_END}")
            # 落差本身是信息：几十的落差是同级微调，几百的落差通常意味着
            # 「顶层是实测验证过的，下面是试用档或被处置过的」——
            # 后者不能盲目提上来，那等于把未验证的站放进生产轮询。
            if gap >= 300:
                print(f"    {C_WARN}落差 {gap} 很大 —— 下一档更可能是"
                      f"「试用期最低档」或「被降权处置过」，\n"
                      f"      不是「稍差一点的备选」。直接提到 {top} 等于"
                      f"把未验证的站放进生产轮询。{C_END}")
                print(f"    {C_DIM}建议：先用投喂台单独探测那几个站，"
                      f"确认可用再逐个提档；或提到一个中间值"
                      f"（如 {top - 50}）先观察{C_END}")
            else:
                print(f"    {C_DIM}落差仅 {gap}，属同级微调{C_END}")
            # 无论落差大小，都必须先查实测记录 —— 这是踩过的坑：
            # codex 段的 relay-c(800) 落差只有 100，看着像「稍差一点的备选」，
            # 实测却是 200 但正文为 CF_APP_WAF 拦截页（假阳性，状态码骗过判定）。
            # 光看 priority 落差会给出把必然失败的站提到顶层这种危险建议。
            print(f"    {C_WARN}提档前必须核实本段实测记录{C_END}"
                  f"{C_DIM} —— priority 只反映「当初定的档」，"
                  f"不反映「现在还能不能用」。\n"
                  f"      本文件同段注释里搜该站名，看有没有"
                  f"「实测 200」以外的结论（WAF/换模/永久排除/超时）。\n"
                  f"      更可靠的做法：用投喂台或 cpa_probe 现测一次 —— "
                  f"注意状态码 200 不等于可用，\n"
                  f"      要同时核对返回的 model 字段与正文是否为 HTML 拦截页。{C_END}")

    for sec, host, n, total in single_host_sections:
        # 顺带算出「提到顶层后池子会变多大」—— 光说「提上来」没法决策，
        # 得知道代价（多引入几个站）和收益（池子从几个变几个）。
        entries = cfg.get(sec) or []
        pairs = entry_hosts(sec, entries)
        top = max(p for _h, p, _e in pairs)
        lower = sorted({p for _h, p, _e in pairs if p < top}, reverse=True)
        merge_hint = ""
        if lower:
            nxt = lower[0]
            nxt_items = [(h, e) for h, p, e in pairs if p == nxt]
            nxt_hosts = sorted({h for h, _e in nxt_items})
            gap = top - nxt
            if gap >= 300:
                # 大落差：下一档多半是试用档或被处置过的。给出**安全**的做法，
                # 不能直接教人提档 —— 那会把未验证的站放进生产轮询。
                merge_hint = (
                    f"\n      {C_WARN}下一档 priority {nxt} 落差 {gap}，"
                    f"很可能是试用期最低档或被降权处置过 ——\n"
                    f"      不要直接提上来。{C_END}安全路径：\n"
                    f"        1. 用投喂台重新探测这些站"
                    f"（{', '.join(nxt_hosts[:3])}"
                    f"{' 等' if len(nxt_hosts) > 3 else ''}），确认现在真能用\n"
                    f"        2. 选 1~2 个通过的，提到 {top}，让它们与 {host} 同层\n"
                    f"        3. 观察一段时间，CPAMP「近期请求」看成功率再决定要不要加更多")
            else:
                merge_hint = (
                    f"\n      候选：priority {nxt} 的 {len(nxt_items)} 个凭据"
                    f"（{', '.join(nxt_hosts)}），落差仅 {gap}。\n"
                    f"      {C_WARN}但提档前必须现测一次{C_END} —— priority 只是"
                    f"「当初定的档」，不代表现在还能用。\n"
                    f"      {C_DIM}实测踩过：codex 段 relay-c 落差只有 100，"
                    f"实测却返回 200 + CF_APP_WAF 拦截页（假阳性）。\n"
                    f"      核对三件事：状态码、返回的 model 字段是否被换、"
                    f"正文是不是 HTML 拦截页。{C_END}\n"
                    f"      确认可用后改成 {top}，池子就从 {n} 个变成"
                    f" {n + len(nxt_items)} 个、从 1 站变成 {1 + len(nxt_hosts)} 站。")
        findings.append(
            f"{C_BAD}{sec} 的顶层只有 {host} 一个站{C_END}（{n}/{total} 凭据）——"
            f"**单点**。\n"
            f"      层级隔离下，这个站一挂（403 / 封号 / 欠费），整段立刻不可用，\n"
            f"      另外 {total - n} 个凭据一个都顶不上 —— 它们不是备份，是死重量。"
            f"{merge_hint}")

    # ── ③ 结论 ────────────────────────────────────────────────────────
    print(f"\n{'=' * 72}\n③ 结论\n{'=' * 72}")
    if not findings:
        print(f"  {C_OK}未发现结构性问题。{C_END}")
        print(f"  403 更可能来自单个上游本身（封号 / 风控 / 站方限制），"
              f"或客户端入口鉴权。")
    else:
        for i, f in enumerate(findings, 1):
            print(f"\n  {i}. {f}")

    print(f"\n{'─' * 72}")
    print("这个脚本只读配置，不发请求 —— 它诊断的是**结构性**成因。")
    print("要确认具体是哪个上游在返 403，需要看 CPA 的请求日志或打一次业务端点。")
    print(f"{'─' * 72}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
