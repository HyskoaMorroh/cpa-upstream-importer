#!/usr/bin/env python3
"""跑全部测试。改任何代码后先跑这个。

用法
----
    python3 tests/run.py                    # 纯逻辑用例
    python3 tests/run.py ../config.yaml     # 加上真实 config.yaml 用例

退出码 0 = 全通过，非 0 = 有失败。可直接接进 CI 或 git hook。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 四个套件覆盖面互不重叠，缺一不可：
#   test_probe     纯函数（解析/判定/指纹/去重/定档/影响面/写回）
#   test_server    HTTP 契约（鉴权/封锁/静态/路由/写回闸门/脱敏）
#   test_pipeline  假上游端到端 —— 唯一会走进 Prober._call 的套件
#                  （2026-08-30 实测：__init__ 的同名遮蔽让 _call 必崩，
#                    而当时另外两个套件全绿）
#   test_edges     写回的边界形状（同站 100 Key、撞已有 provider、
#                  已存在 Key 重导…… 全是「真跑一次才发现」型的坑）
# test_pipeline 排后面：它起假上游、真发 HTTP，比纯逻辑慢。
#
# `*_compliance.py` 是另一类：按**契约面**切（源码对齐 / 传输 / 写回 /
# HTTP API / 规划 / 探测），与上面按功能切的那批交叉覆盖。它们原来没进这张
# 表 —— 于是 `python tests/run.py` 全绿并不代表它们也绿，实测过一次「主套件
# 全通过而 compliance 里 6 项红着」。要么进表，要么删掉，留着不跑最坏：
# 它给的是「已经测过了」的错觉。
SUITES = ["test_probe.py", "test_server.py", "test_pipeline.py",
          "test_edges.py", "test_reload.py", "test_speed.py", "test_web.py",
          "test_tiering.py", "test_full_redetect.py", "test_bulk.py",
          "test_priority_consistency.py", "test_sub2api_source.py",
          "test_source_compliance.py", "test_transport_compliance.py",
          "test_writeback_compliance.py", "test_planning_compliance.py",
          "test_probe_compliance.py", "test_api_compliance.py"]


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，打不出 ✗ 与 U+FFFD，会 UnicodeEncodeError。

    子进程输出用 errors="replace" 解码，遇到 GBK 转不出的字节会变成 U+FFFD，
    再原样打到 GBK stdout 就崩。这里把本进程 stdout 强制成 UTF-8 且遇不到
    的字符替换掉 —— 测试汇报不该因为终端编码而失败。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _count_cases(out: str) -> int:
    """从一个套件的输出里数出它跑了多少项。

    两种小结格式并存，都要认（2026-09-16 修）
    ----------------------------------------
    原来只认「全部通过 · N 项」，而另外两类套件的写法它挑不出来：
      · unittest 派生：`Ran 42 tests in 0.161s` + `OK`
        （test_source / transport / writeback / api）
      · 自写断言器：`29/29 passed; 0 failed`
        （test_planning / probe_compliance）
    它们**从不打印**「全部通过」，于是 `total_ok` 少算这 6 个套件的全部
    用例 —— 实测汇总报「合计 1813 项」时，planning 的 29 项根本没进去。

    后果不是数字难看：这张合计是「跑全了没有」的唯一信号，少算会让人
    以为遗漏的是别的套件，从而去错地方找。所以三种格式都解析。
    """
    for line in out.split("\n"):
        if "全部通过" in line:
            try:
                return int(line.split("·")[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
        # unittest: `Ran 42 tests in 0.161s`
        m = re.match(r"^Ran (\d+) tests? in ", line.strip())
        if m:
            return int(m.group(1))
        # 自写断言器: `29/29 passed; 0 failed`
        m = re.match(r"^(\d+)/(\d+) passed", line.strip())
        if m:
            return int(m.group(2))
        # 自写断言器（另一种）: `26 passed, 0 failed`
        m = re.match(r"^(\d+) passed,\s*\d+ failed", line.strip())
        if m:
            return int(m.group(1))
    return 0


def main() -> None:
    _force_utf8_stdout()

    cfg = sys.argv[1] if len(sys.argv) > 1 else ""
    if cfg and not os.path.isfile(cfg):
        sys.exit(f"找不到 config.yaml：{cfg}")

    total_ok = 0
    failed: list[str] = []

    # 子进程也要按 UTF-8 输出，否则它们自己先在 GBK 上崩
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 真实 config.yaml 走**环境变量**而不是命令行位置参数。
    #
    # 2026-09-16 实测：原来写成 `cmd = [py, path] + ([cfg] if cfg else [])`，
    # 而 `unittest.main()` 会把 `argv[1]` 当成待加载的**测试模块名**：
    #
    #     AttributeError: module '__main__' has no attribute 'C:/.../config'
    #
    # 那次七个套件一起红（四个报错、三个静默返回 0 却什么都没跑），
    # 而合计项数照常统计 —— 校准模式其实从来没跑过真实数据。
    if cfg:
        env["IMPORTER_TEST_CONFIG"] = cfg

    for suite in SUITES:
        path = os.path.join(HERE, suite)
        cmd = [sys.executable, path]
        print(f"\n{'#' * 66}\n# {suite}\n{'#' * 66}")
        r = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace",
                           capture_output=True, env=env)
        out = r.stdout or ""
        # 只回显小结与失败行，通过项太多会刷屏
        for line in out.split("\n"):
            if ("✗" in line or "失败" in line or "全部通过" in line
                    or "跳过" in line or line.startswith("      ")):
                print(line)
        if r.returncode:
            failed.append(suite)
            # 失败时把子套件的原始输出尾部整段打出来（2026-09-05 加）。
            #
            # 为什么需要：上面那个回显过滤器按关键词挑行，而它挑不全 ——
            # 实测遇到一次 `失败套件：test_server.py` 而单跑那个套件全绿，
            # 复现时手上没有任何失败项信息（`✗` 行没落进过滤器），
            # 五轮追查全部无法定位。偶发失败最需要的恰恰是第一次的现场。
            #
            # 只在失败时打，且只打尾部 —— 通过时那些输出会刷屏。
            tail = "\n".join((out or "").splitlines()[-40:])
            if tail.strip():
                print(f"\n--- {suite} 输出尾部（失败现场）---")
                print(tail)
            if r.stderr:
                print(f"--- {suite} stderr ---")
                print(r.stderr[-1500:])
        else:
            # 小结行落在 stdout 还是 stderr 不统一（2026-09-16 实测）：
            # 自写断言器打 stdout，unittest 默认打 stderr。只读一个流会
            # 让半数套件的项数静默消失。
            total_ok += _count_cases(out) or _count_cases(r.stderr or "")

    print(f"\n{'=' * 66}")
    if failed:
        print(f"失败套件：{', '.join(failed)}")
        sys.exit(1)
    print(f"全部套件通过 · 合计 {total_ok} 项")
    if not cfg:
        # 不写死数字 —— 上一版写的「43 项（共 305）」在套件增长后就过期了，
        # 而过期的提示比没有提示更糟：它让人以为跑全了。
        print("提示：传 config.yaml 路径可额外跑真实文件用例")
        print("      python3 tests/run.py /path/to/config.yaml")


if __name__ == "__main__":
    main()
