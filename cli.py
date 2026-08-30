#!/usr/bin/env python3
"""批量导入 CPA 上游账号 —— 命令行入口。

在 VPS /opt/deploy 下跑。前端服务复用同一套 cpa_probe，这个 CLI 是
「不开网页也能用」的那条路，也是前端的参照实现。

用法
----
    # 1) 只解析，看格式对不对（零请求，零成本）
    python3 -m upstream_importer.cli --input accounts.txt --dry-run

    # 2) 探测但不写回（默认行为，必须显式 --write 才落盘）
    python3 -m upstream_importer.cli --input accounts.txt

    # 3) 探测 + 预览 diff + 写回本地 config.yaml
    python3 -m upstream_importer.cli --input accounts.txt --write

    # 4) 写回。**给了管理密码就自动触发 CPA 重载**，不用 restart
    #    没给密码不会静默跳过 —— 会告警并让你 docker restart cli-proxy-api
    python3 -m upstream_importer.cli --input accounts.txt --write \
        --mgmt-key "$MGMT"

    # 5) 再加端到端验证（确认新上游经 CPA 真能出活）
    python3 -m upstream_importer.cli --input accounts.txt --write \
        --mgmt-key "$MGMT" --client-key "$CPA_CLIENT_KEY"

    # 6) 关掉上下文探测（省钱：每个段少 4-6 次大 body 请求）
    python3 -m upstream_importer.cli --input accounts.txt --no-context

    # 7) 退回串行（撞上按账号全局限频的站时）
    python3 -m upstream_importer.cli --input accounts.txt \
        --workers 1 --candidate-workers 1

$MGMT 必须是 CPA 后台的**原始密码**，不是 config.yaml 里那串 $2a$ 哈希 ——
PUT 端点用 bcrypt.CompareHashAndPassword 校验（handler.go:387），哈希必然 401。

输入格式：每行 `url,key`。url 带不带 /v1 都行，服务按段自动规范化。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cpa_probe as cp  # noqa: E402
from cpa_probe.pipeline import Prober  # noqa: E402
from cpa_probe.writeback import (  # noqa: E402
    apply_diffs,
    build_diffs,
    reload_cpa,
    validate,
    verify_upstream,
    write_local,
)

C_OK, C_BAD, C_WARN, C_DIM, C_END = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m"


def _no_color() -> None:
    global C_OK, C_BAD, C_WARN, C_DIM, C_END
    C_OK = C_BAD = C_WARN = C_DIM = C_END = ""


def _load_cfg(path: str) -> tuple[str, dict]:
    import yaml

    raw = io.open(path, encoding="utf-8").read()
    cfg = yaml.safe_load(raw)
    if not isinstance(cfg, dict):
        sys.exit(f"{path} 顶层不是映射，结构异常")
    return raw, cfg


def _print_parse(res: cp.ParseResult) -> None:
    print(f"\n{'='*72}\n解析\n{'='*72}")
    print(f"  有效 {len(res.valid)} 行 · 无效 {len(res.invalid)} 行")
    for r in res.invalid:
        print(f"  {C_BAD}✗{C_END} 第 {r.line_no} 行：{r.error}")
        print(f"      {C_DIM}{r.raw[:70]}{C_END}")
    if res.valid:
        print(f"\n  {'主机':<32} {'Key（脱敏）':<20} 四段 base-url")
        for r in res.valid:
            print(f"  {r.host:<32} {r.masked():<20}")
            for s in cp.SECTIONS:
                print(f"      {C_DIM}{s:<22} {r.base_for(s)}{C_END}")


def _print_result(res, plan) -> None:
    print(f"\n  {'—'*68}")
    print(f"  {res.row.host}  ({res.row.masked()})  共 {res.total_calls} 次请求")
    for section in cp.SECTIONS:
        v = res.sections.get(section)
        if v is None:
            continue
        if v.usable:
            sp = plan.sections.get(section)
            mark = f"{C_OK}✓{C_END}"
            extra = ""
            if sp:
                extra = f" → priority {sp.priority}"
                if sp.duplicate:
                    mark = f"{C_WARN}={C_END}"
                    extra = f" {C_WARN}已存在，跳过{C_END}"
            print(f"    {mark} {section:<22} {v.summary()}{extra}")
            if sp:
                print(f"        {C_DIM}{sp.priority_reason}{C_END}")
                if sp.duplicate:
                    print(f"        {C_WARN}{sp.duplicate_note}{C_END}")
                for w in sp.warnings:
                    print(f"        {C_WARN}⚠ {w}{C_END}")
                if sp.models:
                    print(f"        {C_DIM}模型：{', '.join(sp.models)}{C_END}")
                # 逐模型影响面。抢顶层与挡下层是两件事，都要能看见 ——
                # 层级隔离下「挡住」意味着那些站整层被跳过。
                for imp in sp.impacts:
                    if imp.hijacks:
                        rel = f"{C_BAD}抢走顶层（原 {imp.current_top}）{C_END}"
                    elif imp.shares:
                        rel = f"{C_WARN}与顶层同层（{imp.current_top}）{C_END}"
                    else:
                        rel = f"{C_DIM}低于顶层 {imp.current_top}{C_END}"
                    hosts = imp.shadowed_hosts
                    tail = (f"挡住 {len(hosts)} 站："
                            + " ".join(hosts[:6])
                            + (" …" if len(hosts) > 6 else "")) if hosts else "不挡任何站"
                    print(f"          {imp.model:<26} {rel}  {C_DIM}{tail}{C_END}")
        else:
            reason = plan.skipped.get(section, v.summary())
            print(f"    {C_BAD}✗{C_END} {section:<22} {reason}")
            bad = [a for a in v.attempts if not a.ok]
            if bad and bad[-1].excerpt:
                print(f"        {C_DIM}{bad[-1].status} · {bad[-1].excerpt[:110]}{C_END}")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="upstream-importer",
        description="批量导入 CPA 上游账号：解析 → 探测 → 定档 → diff → 写回",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--input", "-i", required=True, help="txt 文件路径，或 - 表示从标准输入读")
    ap.add_argument("--config", default="config.yaml", help="config.yaml 路径（默认当前目录）")
    ap.add_argument("--proxy", default="",
                    help="探测用代理。留空则自动探测（容器内 http://mihomo:7890，"
                         "宿主机 http://127.0.0.1:7890），两个都不通就跳过代理尝试")
    ap.add_argument("--no-proxy", action="store_true", help="完全不试代理")
    ap.add_argument("--gap", type=float, default=3.0,
                    help="请求间隔秒。relay-b 有 bulk probe guard，别低于 3")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--no-context", action="store_true",
                    help="关掉 max-context-length 二分探测（省钱）")
    ap.add_argument("--swap-samples", type=int, default=3,
                    help="静默换模采样次数。单次测不出来，<2 则跳过")
    ap.add_argument("--by-score", action="store_true",
                    help="按探测得分定档，而非默认的试用期最低档。"
                         "满分候选会拿到高档位并把已验证的站挡在其后 —— "
                         "只在你确认新站确实该优先时用")
    ap.add_argument("--workers", type=int, default=4, metavar="N",
                    help="单个候选内部的四段并行度（1-4，默认 4）。"
                         "四段打不同端点、节流按 (host, section) 分桶，"
                         "所以并行不会放松任何站的限频。设 1 = 老的串行行为")
    ap.add_argument("--candidate-workers", type=int, default=4, metavar="N",
                    help="同时探几个候选（默认 4）。实际上限是不同主机数 —— "
                         "同主机的多个 Key 会被归并成一次形态学习")
    ap.add_argument("--dry-run", action="store_true", help="只解析，不发任何请求")
    ap.add_argument("--write", action="store_true",
                    help="确认写回本地 config.yaml（不给这个开关只预览）")
    ap.add_argument("--push", metavar="CPA_BASE",
                    nargs="?", const="", default="",
                    help="CPA 管理端点地址。默认自动取（容器内 CPA_UPSTREAM_URL，"
                         "否则 http://127.0.0.1:8317）——**写回后默认就会推**，"
                         "因为只写盘不推有丢事件的风险。给了值就用你给的")
    ap.add_argument("--no-reload", action="store_true",
                    help="写回后不触发 CPA 重载。只在你打算自己 "
                         "docker restart cli-proxy-api 时用 —— "
                         "不加这个开关也不给密码时会明确告警，不会静默跳过")
    ap.add_argument("--mgmt-key", default=os.environ.get("MGMT", ""),
                    help="CPA management key（默认取环境变量 MGMT）")
    ap.add_argument("--client-key", default=os.environ.get("CPA_CLIENT_KEY", ""),
                    help="CPA 客户端入口 Key（config.yaml 的 api-keys 之一）。"
                         "给了才做端到端验证 —— push 成功只说明 CPA 收下配置，"
                         "不代表新上游能出活")
    ap.add_argument("--json", metavar="PATH", help="把完整结果导出为 JSON")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        _no_color()

    text = sys.stdin.read() if args.input == "-" else io.open(args.input, encoding="utf-8").read()
    parsed = cp.parse_lines(text)
    _print_parse(parsed)

    if not parsed.valid:
        sys.exit("\n没有有效行，退出")
    if args.dry_run:
        print(f"\n{C_DIM}--dry-run：未发任何请求{C_END}")
        return

    raw, cfg = _load_cfg(args.config)
    print(f"\n{C_DIM}config.yaml {len(raw.splitlines())} 行 · "
          f"四段 {sum(len(cfg.get(s) or []) for s in cp.SECTIONS)} 条目{C_END}")

    # 传 raw：定档要读注释里的「实测不可用」结论，否则会把死站当活站保护，
    # 把可用新站压到最低档（2026-08-30 实测到的缺陷）。
    bands = {s: cp.build_band(cfg, s, raw=raw) for s in cp.SECTIONS}
    seen = cp.existing_fingerprints(cfg)

    print(f"\n{'='*72}\n探测（{len(parsed.valid)} 个候选 × 4 段）\n{'='*72}")
    if not args.no_context:
        print(f"{C_WARN}上下文探测已开启：每个可用段额外 4-6 次大 body 请求，会计费。"
              f"用 --no-context 关闭{C_END}")

    def on_event(kind: str, data: dict) -> None:
        if kind == "attempt":
            print(f"    {C_DIM}· {data['section']:<22} {data['model']:<20} "
                  f"{data['combo']:<18} {data['status']:<4} {data['category']}{C_END}")
        elif kind == "catalog":
            print(f"    {C_DIM}· {data['section']:<22} /models 目录 {data['count']} 个"
                  f"（已按 gemini/gpt/claude 过滤）{C_END}")

    # 代理地址：容器内是服务名，宿主机是映射端口 —— 两者不通用。
    # 之前默认写死 mihomo:7890，在宿主机上永远不通，每次都得手工加
    # --no-proxy。这里自动探一次，省掉那个开关。
    proxy = None
    if not args.no_proxy:
        if args.proxy:
            proxy = args.proxy
        else:
            from cpa_probe.client import probe_proxy
            for cand in ("http://mihomo:7890", "http://127.0.0.1:7890"):
                ok_p, detail = probe_proxy(cand, timeout=3)
                if ok_p:
                    proxy = cand
                    print(f"  {C_DIM}代理自动选定 {cand}（{detail}）{C_END}")
                    break
            if proxy is None:
                print(f"  {C_WARN}两个代理地址都不通，本轮跳过全部 via-proxy 尝试。"
                      f"注意 config.yaml 里有凭据配了 proxy-url，那些站此刻走不通{C_END}")

    prober = Prober(
        proxy=proxy,
        gap=args.gap,
        timeout=args.timeout,
        probe_context=not args.no_context,
        swap_samples=args.swap_samples,
        workers=args.workers,
        on_event=on_event,
    )

    t0 = time.monotonic()

    # 候选并行度上限取「不同主机数」—— 同主机的多个 Key 会被 single-flight
    # 归并成一次形态学习，多开线程只是空转。
    hosts = {r.host for r in parsed.valid}
    cand_workers = max(1, min(len(hosts), args.candidate_workers))

    slots: list = [None] * len(parsed.valid)
    if cand_workers > 1 and len(parsed.valid) > 1:
        print(f"  {C_DIM}并行：{cand_workers} 个候选 × 每候选最多 "
              f"{prober.workers} 段{C_END}")
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=cand_workers, thread_name_prefix="probe-cand") as ex:
            futs = {ex.submit(prober.probe, r): i
                    for i, r in enumerate(parsed.valid)}
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                slots[i] = fut.result()
                row = parsed.valid[i]
                print(f"  {C_DIM}· 完成 {row.host} ({row.masked()}){C_END}")
    else:
        for i, row in enumerate(parsed.valid, 1):
            print(f"\n  [{i}/{len(parsed.valid)}] {row.host}")
            slots[i - 1] = prober.probe(row)

    # 定档必须串行，且严格按输入行序 —— build_plan 会把本候选算出的条目
    # 计入 seen/bands，后续候选据此判重与定档。并行或乱序会让「同一批里
    # 重复的行」互相看不见对方，从而生成两条一样的条目。
    results, plans = [], []
    for row, res in zip(parsed.valid, slots):
        plan = cp.build_plan(row, res, cfg, bands=bands, seen=seen,
                             probation=not args.by_score)
        results.append(res)
        plans.append(plan)

    print(f"\n{'='*72}\n结论（耗时 {int(time.monotonic()-t0)}s）\n{'='*72}")
    for res, plan in zip(results, plans):
        _print_result(res, plan)

    diffs = build_diffs(raw, plans)
    n_lines = sum(len(d.lines) for d in diffs)
    print(f"\n{'='*72}\ndiff 预览（{len(diffs)} 处插入，共 {n_lines} 行）\n{'='*72}")
    if not diffs:
        print(f"  {C_WARN}无可写入条目 —— 全部不可用或已存在{C_END}")
        return
    for d in diffs:
        print(f"\n  {C_OK}+++{C_END} {d.section}  ← {d.host}  （第 {d.insert_at} 行后）")
        for line in d.lines:
            print(f"  {C_OK}+{C_END} {line}")

    merged = apply_diffs(raw, diffs)
    ok, msg = validate(merged)
    print(f"\n  校验：{C_OK if ok else C_BAD}{msg}{C_END}")
    if not ok:
        sys.exit("  校验未通过，不写回")

    if args.json:
        payload = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "candidates": [
                {
                    "host": p.host,
                    "key": p.masked_key,
                    "sections": {
                        s: {
                            "priority": sp.priority,
                            "priority_reason": sp.priority_reason,
                            "models": sp.models,
                            "proxy_url": sp.proxy_url,
                            "headers": sp.headers,
                            "max_context_length": sp.max_context_length,
                            "context_model": sp.context_model,
                            "score": sp.score,
                            "duplicate": sp.duplicate,
                            "warnings": sp.warnings,
                        }
                        for s, sp in p.sections.items()
                    },
                    "skipped": p.skipped,
                }
                for p in plans
            ],
        }
        io.open(args.json, "w", encoding="utf-8").write(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
        print(f"  已导出 {args.json}")

    if not args.write:
        print(f"\n{C_WARN}未写回 —— 加 --write 才落盘。这是硬闸门，不提供跳过。{C_END}")
        return

    bak = write_local(args.config, merged)
    print(f"\n  {C_OK}✓{C_END} 已写回 {args.config}")
    print(f"      备份 {bak}")

    # ── 让 CPA 立即生效 ─────────────────────────────────────────────
    # 默认就做。只写盘不推有真实风险：inotify 事件可能丢，而 CPA 没有
    # 轮询兜底（internal/watcher/ 只有 debounce 定时器，没有 Ticker），
    # 事件一丢就永远不重载，也不会自愈。
    #
    # write_local 已保证 inode 不变（单文件 bind mount 的硬要求），
    # 所以容器能看到新字节；PUT 的作用是把「事件可能丢」换成
    # 「事件必然有」，并且给出一个可判断的 HTTP 回执 + 读回校验。
    if args.no_reload:
        print()
        print(f"  {C_WARN}⚠ --no-reload：未触发 CPA 重载。{C_END}")
        print(f"  {C_DIM}磁盘已改。CPA 大概率会自己收到 inotify 事件并重载，"
              f"但**没有保证** —— 事件丢了它不会自愈。{C_END}")
        print(f"  {C_DIM}确认生效：docker restart cli-proxy-api{C_END}")
        return

    cpa_base = args.push or os.environ.get("CPA_UPSTREAM_URL") or "http://127.0.0.1:8317"

    if not args.mgmt_key:
        print()
        print(f"  {C_WARN}⚠ 没有管理密码，未触发 CPA 重载。{C_END}")
        print(f"  {C_DIM}原因：PUT /v0/management/config.yaml 要用 bcrypt 比对"
              f"原始密码（handler.go:387）。{C_END}")
        print()
        print("  让它确定生效，选一条：")
        print(f"    {C_OK}A{C_END} 重启容器（最直接，约 8 秒）")
        print("        docker restart cli-proxy-api")
        print(f"    {C_OK}B{C_END} 本命令补上密码重跑（不重启）")
        print("        --mgmt-key '<你在 CPA 后台输的原始密码>'")
        print(f"       {C_DIM}或 export MGMT='<原始密码>' 后重跑{C_END}")
        print(f"  {C_DIM}CPAMP 面板另有 30 秒前端缓存（constants.ts:13），"
              f"生效后等 30 秒再硬刷新。{C_END}")
        return

    # config.yaml 里的 secret-key 是 bcrypt 哈希（CPA 首次加载时自动转换，
    # config_load.go:104-113），PUT 端点要的是**原始密码**。
    # 把哈希当密码传必然 401，且连续 5 次会封 IP 30 分钟 —— 先挡住。
    if args.mgmt_key.startswith(("$2a$", "$2b$", "$2y$")):
        sys.exit("  --mgmt-key 收到的是 config.yaml 里那串 bcrypt 哈希，"
                 "不是原始密码。\n"
                 "  PUT 端点用 bcrypt.CompareHashAndPassword 校验"
                 "（handler.go:387），哈希当密码传必然 401。\n"
                 "  请传你在 CPA 后台输的那个原始密码。")

    print(f"\n  触发 CPA 重载 {cpa_base}")
    rok, rmsg = reload_cpa(cpa_base, args.mgmt_key, merged)
    if rok:
        print(f"  {C_OK}✓{C_END} {rmsg}")
        print(f"  {C_DIM}CPAMP 面板有 30 秒前端缓存，等 30 秒再硬刷新{C_END}")
    else:
        print(f"  {C_BAD}✗{C_END} {rmsg}")
        print()
        print(f"  {C_WARN}配置已写入磁盘，但 CPA 未确认用上。"
              f"执行 docker restart cli-proxy-api{C_END}")
        sys.exit(1)

    # 第二级验证。少了它就只知道「CPA 接受了这份 YAML」，
    # 不知道「客户端真能用新上游」—— 直连 200 而经 CPA 换模是实测存在的。
    if not args.client_key:
        print(f"\n  {C_WARN}未给 --client-key，跳过端到端验证{C_END}")
        print(f"  {C_DIM}重载成功只说明 CPA 收下了配置。要确认能出活，"
              f"补上 config.yaml 里 api-keys 之一{C_END}")
    else:
        print(f"\n  端到端验证（打 CPA 自己的业务端点）")
        bad = 0
        for plan in plans:
            for sec, sp in plan.sections.items():
                if not sp.writable or not sp.models:
                    continue
                vok, vmsg = verify_upstream(
                    cpa_base, args.client_key, sec, sp.models[0],
                )
                mark = f"{C_OK}✓{C_END}" if vok else f"{C_BAD}✗{C_END}"
                print(f"    {mark} {plan.host:<26} {sec:<22} "
                      f"{sp.models[0]:<20} {vmsg}")
                if not vok:
                    bad += 1
        if bad:
            print(f"\n  {C_BAD}{bad} 项端到端验证失败，但配置已写入{C_END}")
            print(f"  {C_DIM}这些条目直连可能是好的，经 CPA 却不行。"
                  f"按上面的说明处置，或用备份回滚{C_END}")
            sys.exit(1)
        print(f"\n  {C_OK}全部通过{C_END}")


if __name__ == "__main__":
    main()
