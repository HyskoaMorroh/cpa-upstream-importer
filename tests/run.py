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
SUITES = ["test_probe.py", "test_server.py", "test_pipeline.py",
          "test_edges.py", "test_reload.py", "test_speed.py", "test_web.py",
          "test_tiering.py", "test_full_redetect.py"]


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

    for suite in SUITES:
        path = os.path.join(HERE, suite)
        cmd = [sys.executable, path] + ([cfg] if cfg else [])
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
            if r.stderr:
                print(r.stderr[-1500:])
        else:
            for line in out.split("\n"):
                if "全部通过" in line:
                    try:
                        total_ok += int(line.split("·")[1].strip().split()[0])
                    except (IndexError, ValueError):
                        pass

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
