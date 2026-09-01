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
import os
import re
from dataclasses import dataclass, field


# CPA 源码里这几个文件是身份头的权威来源。路径相对仓库根。
_CLAUDE_REQ = "internal/runtime/executor/claude_executor_request.go"
_CODEX_REQ = "internal/runtime/executor/codex_executor_request.go"


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
    source_root: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.claude_betas_unconditional) and not self.errors


def _read(root: str, rel: str) -> str:
    p = os.path.join(root, rel)
    try:
        return io.open(p, encoding="utf-8", errors="replace").read()
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


def extract(root: str) -> CpaIdentity:
    """从 CPA 仓库根提取身份常量。"""
    out = CpaIdentity(source_root=root)
    if not os.path.isdir(root):
        out.errors.append(f"不是目录：{root}")
        return out

    src = _read(root, _CLAUDE_REQ)
    if not src:
        out.errors.append(f"读不到 {_CLAUDE_REQ}")
        return out

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

    csrc = _read(root, _CODEX_REQ)
    if csrc:
        cc = _const_map(csrc)
        for k, v in cc.items():
            lk = k.lower()
            if "useragent" in lk and not out.codex_user_agent:
                out.codex_user_agent = v
            elif "originator" in lk and not out.codex_originator:
                out.codex_originator = v

    return out


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
    if not ident.ok:
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


def check(*, source_root: str = "", cfg: dict | None = None,
          runtime_commit: str = "") -> dict:
    """给服务端调用的统一入口。返回可直接进 JSON 的 dict。

    优先读源码（精确，能区分有条件/无条件 beta）；读不到就退回 config.yaml
    的 header-defaults（覆盖面小但容器里一定有）。两条都不成立时返回
    checked=False —— 不假装检查过。

    runtime_commit 是 CPA 管理接口回的 X-CPA-COMMIT。给了就与源码的 git HEAD
    比对 —— 不一致说明「源码已更新但 CPA 没重启」，此时按源码判断的漂移结论
    对不上实际转发行为，必须提示出来。
    """
    if source_root:
        ident, drifts = report(source_root)
        if ident.ok:
            src_commit = source_commit(source_root)
            stale = bool(src_commit and runtime_commit
                         and not runtime_commit.startswith(src_commit[:7])
                         and not src_commit.startswith(runtime_commit[:7]))
            if stale:
                drifts.insert(0, Drift(
                    what="源码与运行中的 CPA 不是同一版本",
                    ours=f"源码 {src_commit}",
                    theirs=f"运行中 {runtime_commit}",
                    severity="warn",
                    note=("下面的比对是按**源码**做的，而 CPA 实际转发用的是旧"
                          "二进制。请重新构建并重启 CPA，或忽略下面的结论")))
            return {
                "checked": True,
                "source": "CPA 源码",
                "source_root": source_root,
                "source_commit": src_commit,
                "runtime_commit": runtime_commit,
                "stale_binary": stale,
                "betas_unconditional": ident.claude_betas_unconditional,
                "betas_conditional": ident.claude_betas_conditional,
                "codex_user_agent": ident.codex_user_agent,
                "codex_originator": ident.codex_originator,
                "drifts": [
                    {"what": d.what, "ours": d.ours, "theirs": d.theirs,
                     "severity": d.severity, "note": d.note}
                    for d in drifts
                ],
            }

    drifts = compare_config(cfg)
    if cfg and isinstance(cfg.get("claude-header-defaults"), dict):
        return {
            "checked": True,
            "source": "config.yaml 的 claude-header-defaults",
            "partial": True,
            "uncovered": ["anthropic-beta 清单（只有源码里有）"],
            "drifts": [
                {"what": d.what, "ours": d.ours, "theirs": d.theirs,
                 "severity": d.severity, "note": d.note}
                for d in drifts
            ],
        }

    return {
        "checked": False,
        "why": ("读不到 CPA 源码，config.yaml 里也没有 claude-header-defaults。"
                "画像梯用的是内置常量（从 CPA 源码抄录），无法核对是否已过期"),
        "drifts": [],
    }


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
