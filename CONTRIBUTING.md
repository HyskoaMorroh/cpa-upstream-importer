# 贡献指南

## 跑测试

```bash
python3 tests/run.py                      # 697 项，零外网请求
python3 tests/run.py /path/to/config.yaml  # 加 55 项真实文件用例（共 752）
```

不带路径也能全过 —— 需要配置的套件自带最小样本。**任何 PR 都必须让
`python3 tests/run.py` 全绿**，CI 会在 Python 3.9 / 3.12 / 3.13 上各跑一遍。

## 这个项目的测试观

测试不是为了覆盖率，是为了**锁住已经踩过的坑**。所以：

- 每条断言尽量写清「为什么加这条」，最好带上实测证据（状态码、耗时、行号）
- 只验结构性质的断言价值有限 —— 这个项目吃过教训：560 项测试全绿的
  同时，定档算法把满分候选压到 12 分档，因为没有一条断言问过
  「这个档位合理吗」
- 修一个缺陷时顺手问「这个修法自己会错在哪」。两轮自查里有 3 处新缺陷
  是前一轮修复自己引入的

## 改代码前先读注释

`cpa_probe/` 与 `config.yaml` 里的长注释不是啰嗦 —— 它们记录了推翻过的
结论与其依据。例如：

- `writeback.write_local` 为什么**必须**就地覆写而不能 `os.replace`
- `plan.host_matches_note` 为什么**不做**前缀匹配
- `pipeline._throttle` 为什么按 `(host, section)` 而不是全局分桶

这些都有实测反例。动之前先读，否则很可能把修好的东西改回去。

## 提交前自查

```bash
# 语法与编译
python3 -m compileall -q cpa_probe server.py cli.py tools tests
node --check web/app.js        # 有 node 的话

# 别把秘密提交上去 —— .gitignore 已列出常见形态，但仍要自己看一眼
git status --short
git diff --cached | grep -iE 'sk-[a-z0-9]{20,}|secret-key|api-key:' || echo "无明文凭据"
```

`config.yaml`、`.env`、`accounts.txt` 都在 `.gitignore` 里。它们含**明文
上游 API Key** —— 一旦推上公开仓库，即使随后 force-push 删掉，fork
与第三方缓存仍可能留有副本，那些 Key 必须视为已泄露并全部轮换。

## 提 PR

- 一个 PR 做一件事。混着改定档算法和前端样式会很难 review
- commit 信息说清**为什么**，不只是改了什么
- 如果推翻了某条既有注释里的结论，在注释里写明「推翻了什么、依据是什么」，
  而不是直接删掉旧结论 —— 那些记录本身有价值

## 报 issue

带上这些能省一轮来回：

```bash
python3 tools/diag403.py /path/to/config.yaml   # 结构性诊断（只读，不发请求）
python3 tests/run.py                            # 确认测试是否也失败
docker compose logs upstream-importer | tail -40
```

**别贴 `config.yaml` 原文** —— 它含明文 Key。`diag403.py` 的输出只有
域名与档位，没有凭据。
