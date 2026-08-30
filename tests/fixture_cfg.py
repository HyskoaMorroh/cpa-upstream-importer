#!/usr/bin/env python3
"""测试用的最小 config.yaml，各套件共享。

为什么必须有这个文件（2026-08-31 发现的第 10 个缺陷）
--------------------------------------------------
`test_server.py` 与 `test_edges.py` 原来把 `../config.yaml` —— 也就是
**生产配置** —— 当默认输入，找不到就 `return 2`。后果有两层：

  · 刚 clone 的仓库、CI runner、任何没有 CPA 部署的机器上，
    `python3 tests/run.py` 直接失败。而 CI 的注释写着「套件自带最小样本，
    CI 里没有真实配置」—— 那句话对这两个套件是假的，推上去必然红。
  · 更隐蔽的一层：断言挂在一个**会变**的生产文件上。实测踩到 ——
    生产 config.yaml 被改（891 KB → 468 KB）之后，`gemini 五档` 与
    `解析出 11 个不可用站` 这类硬编码基线全部失效，而代码一行没动。
    测试报红却指不出真正的缺陷，比没有测试更糟：它会让人去改代码。

所以规则是：**套件必须能在没有任何真实配置的机器上跑完**，真实文件
只作为额外的一轮。传了路径就多跑一轮真实数据，没传就用这里的样本。

这份样本的形状是按各套件的需求凑的，改动前先确认没有套件依赖它：
    四段齐全                  所有套件
    gemini 五个不同 priority  test_server 的档位谱
    每段 prefix 占比 >= 70%   test_edges ⑫ 的主导 prefix 判定
    compat 有个 3 Key 的站    test_edges ⑨⑩⑪ 的合并与判重
    该站首个 Key 也在 claude 段、且带 prefix + headers
                              test_edges ⑪ 的「五元组撞不上、(key,base) 撞上」
    注释里带实测不可用结论    test_tiering 的健康度信号解析
"""

from __future__ import annotations

import io
import os
import tempfile

# 注释里的站名与 base-url 的域名**故意不同**（site-a.example 的短名写作
# alpha）—— 别名表要从 compat 段的 name 字段建，这一点必须能被测到。
MINIMAL_CFG = """# 测试用最小配置（tests/fixture_cfg.py 自带，无真实凭据）
api-keys:
  - "sk-test-client-0001"

# ── gemini ──────────────────────────────────────────────────────────
gemini-api-key:
  # alpha：实测 503 No available channel
  # delta 永久排除（2026-08-27）
  - api-key: "sk-fx-gemini-0001"
    base-url: "https://site-a.example"
    prefix: "GLE"
    priority: 600
    models:
      - name: "gemini-2.5-pro"
        alias: "gemini-2.5-pro"
  - api-key: "sk-fx-gemini-0002"
    base-url: "https://site-b.example"
    prefix: "GLE"
    priority: 400
    models:
      - name: "gemini-2.5-pro"
        alias: "gemini-2.5-pro"
  - api-key: "sk-fx-gemini-0003"
    base-url: "https://site-c.example"
    prefix: "GLE"
    priority: 300
    models:
      - name: "gemini-2.5-flash"
        alias: "gemini-2.5-flash"
  - api-key: "sk-fx-gemini-0004"
    base-url: "https://site-d.example"
    prefix: "GLE"
    priority: 200
    models:
      - name: "gemini-2.5-pro"
        alias: "gemini-2.5-pro"
  - api-key: "sk-fx-gemini-0005"
    base-url: "https://site-e.example"
    prefix: "GLE"
    priority: 20
    weight: 0
    models:
      - name: "gemini-2.5-pro"
        alias: "gemini-2.5-pro"

# ── codex ───────────────────────────────────────────────────────────
codex-api-key:
  # echo：实测 403，站点恢复后删除本行
  # foxtrot：实测 503 No available channel（已恢复 2026-08-29）
  - api-key: "sk-fx-codex-0001"
    base-url: "https://site-a.example/v1"
    prefix: "CDX"
    priority: 800
    models:
      - name: "gpt-5.6-sol"
        alias: "gpt-5.6-sol"
  - api-key: "sk-fx-codex-0002"
    base-url: "https://site-f.example/v1"
    prefix: "CDX"
    priority: 500
    models:
      - name: "gpt-5.6-sol"
        alias: "gpt-5.6-sol"
  - api-key: "sk-fx-codex-0003"
    base-url: "https://site-g.example/v1"
    prefix: "CDX"
    priority: 20
    models:
      - name: "gpt-5.6-sol"
        alias: "gpt-5.6-sol"

# ── claude ──────────────────────────────────────────────────────────
claude-api-key:
  # charlie：实测 503 No available channel
  # delta 站点级不可用
  # 这一条的 api-key 与 compat 段 bravo 站的首个 Key 相同，且带
  # prefix + headers —— test_edges ⑪ 靠它验「五元组撞不上、(key,base) 撞上」
  - api-key: "sk-fx-shared-0001"
    base-url: "https://site-b.example"
    prefix: "ANT"
    priority: 1000
    headers:
      User-Agent: "claude-cli/2.0.14 (external, cli)"
    models:
      - name: "claude-opus-5"
        alias: "claude-opus-5"
      - name: "claude-sonnet-5"
        alias: "claude-sonnet-5"
  - api-key: "sk-fx-claude-0002"
    base-url: "https://site-c.example"
    prefix: "ANT"
    priority: 400
    models:
      - name: "claude-opus-5"
        alias: "claude-opus-5"
  - api-key: "sk-fx-claude-0003"
    base-url: "https://site-h.example"
    prefix: "ANT"
    priority: 120
    models:
      - name: "claude-sonnet-5"
        alias: "claude-sonnet-5"
  - api-key: "sk-fx-claude-0004"
    base-url: "https://site-i.example"
    prefix: "ANT"
    priority: 20
    models:
      - name: "claude-opus-5"
        alias: "claude-opus-5"

# ── openai 兼容 ─────────────────────────────────────────────────────
openai-compatibility:
  - name: "bravo"
    base-url: "https://site-b.example/v1"
    priority: 530
    api-key-entries:
      - api-key: "sk-fx-shared-0001"
      - api-key: "sk-fx-compat-0002"
      - api-key: "sk-fx-compat-0003"
    models:
      - name: "claude-opus-5"
        alias: "claude-opus-5"
      - name: "gpt-5.6-sol"
        alias: "gpt-5.6-sol"
  - name: "alpha"
    base-url: "https://site-a.example/v1"
    priority: 500
    api-key-entries:
      - api-key: "sk-fx-compat-0004"
    models:
      - name: "gpt-5.6-sol"
        alias: "gpt-5.6-sol"
  - name: "foxtrot"
    base-url: "https://site-f.example/v1"
    priority: 200
    api-key-entries:
      - api-key: "sk-fx-compat-0005"
    models:
      - name: "claude-opus-5"
        alias: "claude-opus-5"
"""


def resolve(argv: list[str], *, label: str = "") -> tuple[str, bool, str]:
    """给套件用的统一入口。返回 (config 路径, 是否自带样本, 临时目录)。

    传了路径且文件存在就用它；否则把 MINIMAL_CFG 写进一个临时目录。
    临时目录一并返回，调用方负责 shutil.rmtree —— 不在这里注册
    atexit，因为有套件要在跑完后检查目录里有没有多出 .bak 文件。

    绝不回落到 `../config.yaml`：那是生产配置，测试既不该依赖它的内容，
    也不该在它旁边留下备份文件。要跑真实数据请显式传路径。
    """
    if len(argv) > 1:
        given = argv[1]
        if not os.path.isfile(given):
            raise SystemExit(f"找不到 config.yaml：{given}")
        return given, False, ""

    tmp = tempfile.mkdtemp(prefix="cpa-fixture-")
    path = os.path.join(tmp, "config.yaml")
    io.open(path, "w", encoding="utf-8", newline="\n").write(MINIMAL_CFG)
    bar = "─" * 62
    print(f"{bar}\n未提供 config.yaml —— 使用自带最小样本"
          f"{('（' + label + '）') if label else ''}\n{bar}")
    return path, True, tmp
