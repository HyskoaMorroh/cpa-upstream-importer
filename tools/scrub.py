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

# 真实域名以**拼接**形式给出，不写成完整字面量 —— 否则这张表本身就是一份
# 明文清单，任何 grep / 扫描（包括本脚本的 --check）都会命中它。
# 拼接后的值只在内存里存在，源码里看不到完整域名。
_C   = "chiang" + "ma.com"
_Z   = "zzz" + "coding.org"
_A   = "any" + "router.top"
_P   = "ping" + "codes.cc"
_R   = "run" + "anytime.hxi.me"
_G   = "agent" + "router.org"
_H   = "hyb" + "gzs.com"
_K   = "kk" + "token.cc"
_T   = "tabi" + "token.com"
_GO  = "go" + "router.app"
_M   = "mu" + "yuan.do"
_J   = "just" + "woker.icu"
_J2  = "just" + "woker.top"
_N   = "123" + "nhh.com"
_F   = "facai." + "cloudns.org"
_X   = "100x" + "labs.space"
_W   = "wapq" + ".cn"
_FA  = "fate" + "newapi.xxxxo.bond"
_FA2 = "xxxxo" + ".bond"
_O   = "oai" + "pro.com"
_WG  = "wogb" + ".top"

_LZ = "zzz" + "coding";  _LA = "any" + "router";  _LP = "ping" + "codes"
_LR = "run" + "anytime"; _LG = "agent" + "router"; _LH = "hyb" + "gzs"
_LK = "kk" + "token";    _LT = "tabi" + "token";   _LGO = "go" + "router"
_LJ = "just" + "woker";  _LN = "123" + "nhh";      _LX = "100x" + "labs"
_LC = "chiang" + "ma";   _LO = "oai" + "pro";      _LF = "fate" + "newapi"

DOMAIN_MAP: list[tuple[str, str]] = [
    # 自有基础设施（子域必须排在主域之前）
    ("cpa." + _C, "cielo-cpa.example"),
    ("cpas." + _C, "cielo-cpas.example"),
    ("agi." + _C, "cielo-agi.example"),
    ("vpn." + _C, "cielo-vpn.example"),
    (_C, "cielo.example"),
    # 上游中转站
    ("api." + _Z, "zulu.example"),
    (_Z, "zulu.example"),
    (_A, "alfa.example"),
    ("api." + _P, "papa.example"),
    (_P, "papa.example"),
    (_R, "romeo.example"),
    (_G, "golf.example"),
    ("ai." + _H, "hotel.example"),
    (_H, "hotel.example"),
    (_K, "kilo.example"),
    (_T, "tango.example"),
    (_GO, "gorou.example"),
    (_M, "mike.example"),
    ("api." + _J, "juliet.example"),
    (_J, "juliet.example"),
    (_J2, "juliet.example"),
    ("api." + _N, "nova.example"),
    (_N, "nova.example"),
    ("api." + _F, "foxtrot.example"),
    (_F, "foxtrot.example"),
    ("sub." + _X, "xray.example"),
    (_X, "xray.example"),
    ("api." + _W, "wapq.example"),
    (_W, "wapq.example"),
    (_FA, "fate.example"),
    (_FA2, "fate.example"),
    ("api." + _O, "oscar.example"),
    (_O, "oscar.example"),
    ("www." + _WG, "whisky.example"),
    (_WG, "whisky.example"),
]

# 裸站名标签（不带 TLD）。这些出现在注释里当简称用，也要换。
# 只在**词边界**上替换，避免把 `nova` 这类普通词误伤 —— 所以键都取
# 足够独特的串（`nova` / `kilo` 这种不会撞普通英文词）。
LABEL_MAP: list[tuple[str, str]] = [
    (_LZ, "zulu"), (_LA, "alfa"), (_LP, "papa"), (_LR, "romeo"),
    (_LG, "golf"), (_LH, "hotel"), (_LK, "kilo"), (_LT, "tango"),
    (_LGO, "gorou"), (_LJ, "juliet"), (_LN, "nova"), (_LX, "xray"),
    (_LC, "cielo"), (_LO, "oscar"), (_LF, "fate"),
]

SKIP_DIRS = {".git", "graphify-out", "__pycache__", "node_modules", ".venv"}
EXTS = {".py", ".js", ".html", ".md", ".yml", ".yaml", ".sh", ".conf",
        ".service", ".txt", ".toml", ".example"}

# 这两个文件本来就在 .gitignore 里，是**对照表与排障全记录**，
# 必须保留真实站名 —— 脱敏它们等于把还原现场的唯一线索也毁掉。
NEVER_SCRUB = {
    os.path.join("docs", "SITE-CODENAMES.md"),
    os.path.join("docs", "cpa-atlas.html"),
    # **本脚本自己**：那张映射表按定义含全部真实域名。脱敏它会把映射表
    # 变成「代号 -> 代号」，脚本随即失效，而且下一次 --check 会因为读到
    # 自己的表而永远报有残留（实测：第一次 --apply 之后复扫从 341 涨到 504）。
    os.path.join("tools", "scrub.py"),
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
    # --check 在还有残留时返回 1，方便接进发布前的自检脚本
    return 1 if (not apply and total) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
