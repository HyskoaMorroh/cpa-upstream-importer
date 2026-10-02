#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前脱敏：把仓库里的真实上游站名与自有域名换成代号。

为什么用代号而不是删掉（与 docs/SITE-CODENAMES.md 同一条理由）
------------------------------------------------------------
注释里的域名不是装饰，是**判定规则的依据**：

    # 换出口 IP 没用。实测 hotel 三段都配了 mihomo 代理，走代理仍被拦

删掉站名后这句变成「实测某站」，下次想复核「到底哪个站？现在还这样吗？」
就没有线索了，规则会退化成不可追溯的教条。代号保留可追溯性 ——
拿本地那份对照表就能还原现场，而对照表本身在 `.gitignore` 里。

两类替换，处理方式不同
--------------------
  · **注释/文档里的实测记录** —— 直接换成代号词（`alfa` → `alfa`）。
  · **测试里作为数据的域名** —— 换成 `<代号>.example`（保持是个合法域名
    形态），否则像「真站名不能被误当字段名排除」这类断言会失去它要验的形态。

幂等：已经是代号的不会被再次替换（代号词不在映射表的键里）。

用法
----
    python3 tools/scrub.py --check    # 只报告，不改（发布前自检）
    python3 tools/scrub.py --apply    # 实际替换
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys

# 真实域名 -> 代号。顺序要紧：**长的在前**，否则 `cielo.example` 会先被
# `cielo.example` 匹配掉，导致 `cielo-cpa.example` 里的子域前缀留在原地。

# 真实域名与代号的对照表**不在本文件里**（2026-10-01 外置）。
#
# 为什么必须外置：这张表按定义是一份**真实上游域名的明文清单**。原来它
# 直接写在这里，于是本脚本本身成了仓库里最敏感的一个文件 —— 而它又被
# 跟踪进了公开仓库。拼接写法（`"exam" + "ple.com"`）只躲得过 grep，
# 躲不过任何人打开文件看一眼，也躲不过 `python -c "import scrub; print(scrub.DOMAIN_MAP)"`。
#
# 现在表在 `tools/scrub-domains.local.py`，由 `.gitignore` 与
# `.dockerignore` 双重排除；本文件只留**加载逻辑**，可以公开。
# 表不存在时 `--apply` 直接拒绝执行（没有表就不知道该换什么），
# 但 `--check` 的**结构性闸**仍然可用 —— 那一闸靠形态判据，不依赖对照表。
# `__file__` 在 exec/compile 加载时不存在（test_api_compliance 就这么加载它
# 来做源码级契约检查），所以按多个候选位置找表，而不是只认一个推断出来的
# 目录 —— 推断错了的表现是「表静默为空」，那等于脱敏没做却不报错。
_TABLE_NAME = "scrub-domains.local.py"


def _table_candidates() -> list[str]:
    here = os.path.dirname(os.path.abspath(
        globals().get("__file__") or sys.argv[0] or "."))
    return [
        os.path.join(here, _TABLE_NAME),
        os.path.join(here, "tools", _TABLE_NAME),
        os.path.join(os.getcwd(), "tools", _TABLE_NAME),
        os.path.join(os.getcwd(), _TABLE_NAME),
    ]


def _load_domain_table() -> tuple[list, list]:
    """从本地对照表读 (DOMAIN_MAP, LABEL_MAP)。缺表时回两个空表。"""
    path = next((c for c in _table_candidates() if os.path.isfile(c)), "")
    if not path:
        return [], []
    ns: dict = {"os": os}
    with io.open(path, encoding="utf-8") as fh:
        exec(compile(fh.read(), path, "exec"), ns)           # noqa: S102
    return list(ns.get("DOMAIN_MAP") or []), list(ns.get("LABEL_MAP") or [])


DOMAIN_MAP, LABEL_MAP = _load_domain_table()

SKIP_DIRS = {".git", "graphify-out", "__pycache__", "node_modules", ".venv"}
EXTS = {".py", ".js", ".html", ".md", ".yml", ".yaml", ".sh", ".conf",
        ".service", ".txt", ".toml", ".example"}

# 这两个文件本来就在 .gitignore 里，是**对照表与排障全记录**，
# 必须保留真实站名 —— 脱敏它们等于把还原现场的唯一线索也毁掉。
NEVER_SCRUB = {
    os.path.join("docs", "SITE-CODENAMES.md"),
    os.path.join("docs", "cpa-atlas.html"),
    # 本地对照表：按定义含全部真实域名。脱敏它会把表变成「代号 -> 代号」，
    # 脚本随即失效，而且下一次 --check 会因为读到自己的表而永远报有残留
    # （实测：第一次 --apply 之后复扫从 341 涨到 504）。
    # 它在 .gitignore / .dockerignore 里，不会被提交或打进镜像。
    os.path.join("tools", "scrub-domains.local.py"),
}


def targets() -> list[str]:
    """要处理的文件：仓库内、扩展名在白名单、且**不被 git 忽略**。

    被忽略的文件不进公开仓库，脱敏它们没有收益，反而会毁掉本地参考。
    """
    out: list[str] = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, ".")
            if rel in NEVER_SCRUB:
                continue
            if os.path.splitext(f)[1] not in EXTS and f != "Dockerfile":
                continue
            if subprocess.run(["git", "check-ignore", "-q", p],
                              capture_output=True).returncode == 0:
                continue
            out.append(p)
    return out


# ───────────────────── 结构性兜底闸（2026-09-30 加）─────────────────────
#
# 为什么清单法不够
# ---------------
# 上面那张 `DOMAIN_MAP` 靠人工维护，**漏一个就放一个过**，而且 `--check`
# 会给出「0 处待替换」的假阴性 —— 比不检查更糟，它制造「已经查过了」的错觉。
#
# 2026-09-30 实测：表里漏了 7 个本轮在用的站（代号见本地 SITE-CODENAMES.md），
# 其中一个明文躺在**被 git 跟踪**的 `cpa_probe/writeback.py` 注释里，
# 而 `python tools/scrub.py --check` 报 0。
#
# 不在这里列出那 7 个真名 —— 列了这段注释自己就是一份明文清单，
# 而本文件恰好在 `NEVER_SCRUB` 里、替换轮不会碰它（那正是上一版的错误：
# 为了说明问题把站名写进注释，结果结构性检测第一轮就命中自己）。
#
# 这一层不看清单，只看**形态**：任何「像真实主机名」又不在公开白名单里的
# 串一律报出来。判据是结构性的，新站出现时不需要有人来加表。
#
# 白名单收的是「公开就公开、无所谓」的域名：上游依赖、标准组织、
# 文档示例保留域（RFC 2606 的 .example / .invalid / .test / example.com）。
# 本项目自有域名与上游中转站都不在里面 —— 它们必须走代号。

_HOSTLIKE = re.compile(
    r"(?<![A-Za-z0-9._/-])"
    r"((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,3}"
    # TLD 表只用来判「这串像不像主机名」，不是安全边界 —— 漏一个 TLD
    # 只会少报，而少报的那个仍要靠人眼在 diff 里看到。宁可短一点、准一点：
    # 全都是本项目实际见过的上游中转站在用的后缀 + 常见通用顶级域。
    r"(?:com|net|org|top|cc|vip|xyz|art|me|bond|cn|space|icu|do|app|io|dev"
    r"|ai|co|info|site|online|pro|tech|cloud|link|live|fun|shop|store|club"
    r"|work|sh|gg|tv|so|st|ly|is|one|world|cfd|sbs|lol|mom|cyou|quest"
    r"|monster|homes|cloudns))"
    r"(?![A-Za-z0-9._-])",
    re.I)

# 公开域名白名单。命中的不报 —— 它们本来就该以真名出现在源码里。
_PUBLIC_OK = re.compile(r"""
    # RFC 2606 / 6761 保留给文档与测试的域名，以及本项目的代号后缀
      \.(?:example|invalid|local|test|localhost)$
    | ^(?:localhost|host\.docker\.internal)$
    | ^(?:[a-z0-9-]+\.)*(?:example\.(?:com|org|net))$
    # 上游依赖与基础设施
    | ^(?:[a-z0-9-]+\.)*(?:
          github\.com | githubusercontent\.com | github\.io | ghcr\.io
        | gitlab\.com | sourceforge\.net
        | router-for\.me                 # CPA 权威名录（模型清单数据源）
        | golang\.org | python\.org | pypi\.org | npmjs\.com | crates\.io
        | docker\.com | docker\.io | nginx\.org | alpinelinux\.org
        | debian\.org | ubuntu\.com | letsencrypt\.org
        | jsdelivr\.net | unpkg\.com | cloudflare\.com
        # 模型厂商与官方 API —— 这些是协议对端，不是本部署的上游中转站
        | openai\.com | anthropic\.com | googleapis\.com | google\.com
        | gstatic\.com | moonshot\.cn | deepseek\.com | bigmodel\.cn
        | siliconflow\.cn | aliyuncs\.com | aliyun\.com | x\.ai
        # 标准与文档
        | apache\.org | mozilla\.org | w3\.org | ietf\.org | rfc-editor\.org
        | yaml\.org | json\.org | stackoverflow\.com | readthedocs\.io
        | iterm2\.com | iterm\.app
        | x\.com                          # parse.py 里当反例用的短域名
      )$
""", re.I | re.X)

# 形如主机名但其实不是域名的串。报出来只是噪声，会让真信号被淹掉 ——
# 而「信号被淹」与「假阴性」的后果一样：没人会逐行看 155 行输出。
_NOT_A_HOST = re.compile(r"""
    # 文件名（`install.sh`、`app.js`、`healthcheck.sh`）
      \.(?:sh|py|js|md|txt|ya?ml|json|conf|html|css|toml|service|example)$
    # 代码里的属性访问（`row.host`、`plan.section`、`logger.info`）
    | ^(?:row|plan|self|opts|cfg|args|req|res|ctx|node|elem|obj|val|key|url
        |api|app|win|doc|msg|err|out|buf|tmp|cur|prev|next|head|tail|body
        |meta|conf|stat|info|warn|debug|trace|fmt|str|int|list|dict|set
        |bool|float|bytes|logger|logging|server|subprocess|json|os|sys|re
        |time|path|host|entry|item|data|section|resp|sect|spec|verdict
        |base-url|[a-z])\.
    # 测试夹具与文档里的**虚构**站名。判据是「一眼看得出是编的」：
    #   · 单字母 / 编号站名（`a.com`、`s1.example.com`、`site1.com`）
    #   · 带角色后缀的（`site-low.com`、`relay-m.example.ai`）
    #   · 明显的占位词（`foobar`、`bare-domain`、`same`、`examplex`）
    # 真实上游中转站的名字不长这样 —— 它们是可注册的商业域名。
    # 收进来是为了让输出短到有人真的会读；漏判的代价由 DOMAIN_MAP
    # 那一层与人眼看 diff 兜着。
    | ^(?:[a-z]\d?|s\d+|site\d*|site-(?:low|mid|high)|relay(?:-[a-z])?
        |foobar|ai\.foobar|bare-domain|same|examplex|\d+)\.
    | ^(?:[a-z0-9-]+\.)*(?:foobar|examplex|42labs)\.
""", re.I | re.X)


def suspects() -> dict[str, list[str]]:
    """{可疑主机名: [出现位置]}。形态判据，不依赖 DOMAIN_MAP。

    `NEVER_SCRUB` 里的文件照样扫 —— 本脚本自己那张表按定义含真实域名，
    而它**在 git 索引里**（2026-09-30 实测：`.gitignore` 列了它但
    `git ls-files` 仍然有它，ignore 规则对已跟踪文件无效，且已推上公开
    origin/main）。扫出来才能提醒「这个文件不该被跟踪」。
    """
    hits: dict[str, list[str]] = {}
    for p in targets() + sorted(NEVER_SCRUB):
        if not os.path.exists(p):
            continue
        try:
            text = io.open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.split("\n"), 1):
            for m in _HOSTLIKE.finditer(line):
                host = m.group(1).lower()
                if _PUBLIC_OK.search(host) or _NOT_A_HOST.match(host):
                    continue
                hits.setdefault(host, []).append(f"{p}:{i}")
    return hits


def tracked_never_scrub() -> list[str]:
    """`NEVER_SCRUB` 里**仍被 git 跟踪**的文件 —— 必须报出来。

    `.gitignore` 对已跟踪文件无效。这道检查存在的理由就是 2026-09-30 实测到的
    那个洞：`tools/scrub.py` 在 `.gitignore` 里、却在索引里、还已经推上了
    公开 origin/main。只靠「加进 .gitignore」这道闸从设立起就没生效过。
    """
    out: list[str] = []
    for rel in sorted(NEVER_SCRUB):
        r = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                           capture_output=True)
        if r.returncode == 0:
            out.append(rel)
    return out


def scrub(text: str) -> tuple[str, int]:
    """返回 (脱敏后文本, 替换次数)。"""
    n = 0
    for real, code in DOMAIN_MAP:
        c = text.count(real)
        if c:
            text = text.replace(real, code)
            n += c
    for real, code in LABEL_MAP:
        # 词边界：前后不能是字母数字、点、连字符 —— 否则会切进已替换好的
        # `zulu.example` 或别的域名中间。
        rx = re.compile(rf'(?<![A-Za-z0-9.\-]){re.escape(real)}(?![A-Za-z0-9.\-])')
        text, c = rx.subn(code, text)
        n += c
    return text, n


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    if apply and not DOMAIN_MAP and not LABEL_MAP:
        print("未加载本地域名对照表，拒绝 --apply；未修改任何文件。")
        return 2
    total = 0
    touched: list[tuple[str, int]] = []
    for p in targets():
        try:
            t = io.open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        new, n = scrub(t)
        if n:
            total += n
            touched.append((p, n))
            if apply:
                io.open(p, "w", encoding="utf-8", newline="").write(new)
    for p, n in sorted(touched, key=lambda x: -x[1]):
        print(f"  {p:<52} {n}")
    verb = "已替换" if apply else "待替换"
    print(f"\n{verb} {total} 处，涉及 {len(touched)} 个文件")
    if not apply and total:
        print("（这是 --check，未改动任何文件；用 --apply 实际执行）")

    # ── 结构性兜底闸（不依赖 DOMAIN_MAP 完整性）──
    # 清单法漏一个就放一个过，而 `--check` 会报「0 处」的假阴性。
    # 这一层按形态判，新站出现时不需要有人来加表。见 `suspects` 的说明。
    #
    # 注意它**在 --apply 之后也跑**：--apply 只能替换表里有的，
    # 漏掉的那些正是这一层要抓的。
    hits = suspects()
    if hits:
        print(f"\n⚠ 结构性检测：{len(hits)} 个疑似真实主机名"
              f"（不在公开白名单里，DOMAIN_MAP 可能漏了它们）")
        for host, locs in sorted(hits.items(), key=lambda kv: -len(kv[1])):
            shown = "、".join(locs[:3]) + ("…" if len(locs) > 3 else "")
            print(f"  {host:<36} {len(locs):>3} 处  {shown}")
        print("  处置：真站名 → 加进 DOMAIN_MAP 并 --apply；"
              "公开依赖 → 加进 _PUBLIC_OK；代码属性/文件名 → 加进 _NOT_A_HOST")

    tracked = tracked_never_scrub()
    if tracked:
        print(f"\n⚠ 这些文件含真实域名却**仍被 git 跟踪**"
              f"（.gitignore 对已跟踪文件无效）：")
        for rel in tracked:
            print(f"  {rel}")
        print("  处置：`git rm --cached <文件>` 之后才算真的不进仓库；"
              "若已推上公开远端，还要处理历史")

    # --check 在还有残留时返回 1，方便接进发布前的自检脚本。
    # 结构性发现与「仍被跟踪」同样计入 —— 它们比表内残留更危险
    # （表内残留至少是**已知**的）。--apply 模式下也要非 0：
    # 替换完了但仍有漏网的，那次发布同样不该继续。
    leaks = bool(hits) or bool(tracked)
    return 1 if (leaks or (not apply and total)) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
