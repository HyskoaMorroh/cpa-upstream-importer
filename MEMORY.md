# 交接：2026-09-06 第十一轮排查

本轮任务：对照三份外部材料排查本项目（`cpa-upstream-importer`）——
运行快照 `投喂台 · CPA 上游灌输.mhtml`、一份真实 `config.yaml`、
以及 `CLIProxyAPI-main`（Go，被配置的那一方）与 `CPA-Manager-Plus-main`（TS）。

## 一、两份材料不是同一个快照（先看清这个，否则一切对账都错）

| | MHTML 运行快照 | 桌面 `config.yaml` |
|---|---|---|
| 行数 | 表头写 `6,082 行 · 121 条目` | 8,788 行（非空 8,775 / 非注释 4,112） |
| 各段顶层 priority | 280 / 350 / 372 / 45 | 600 / 800 / 1000 / 550 |
| compat 段站序 | 与桌面文件几乎相反 | —— |
| 最新日期戳 | 晚于桌面文件 | 2026-09-01 / 09-02 |

**结论：不能拿这两份直接 diff 判「上游数据丢失」。** 条目数 121 一致是巧合
（两份都是 121 个条目），行数与档位谱三项全不对。

## 二、上游数据没有丢

用产品自己的 `extract_existing_entries` + `host_of` 跑桌面 `config.yaml`：

- 177 个条目 → 79 个凭据（去重键 `(host, api_key)`）→ 14 个站
- 快照里 78 张卡片 / 14 个站，**逐站数量完全对得上**：
  gorou 15、tango 14、golf 7、romeo 7、muyuan 6、
  juliet 5、kilo 5、其余各 3
- 唯一缺口：`cielo.example sk-Wxy…ic59`（只配在 gemini 段）——
  与「两份不同快照」一致，不是去重缺陷
- 日志写「177 → 78 个凭据，省掉 99 次」而代码算出 79/98，同一个来源差异

`mask_key`（前 6 后 4）在这 79 个凭据上零碰撞，脱敏没有把两把 Key 混成一个。

## 三、修掉的三个真缺陷（每个都用产品代码实跑证明）

### 1. `max-context-length` 写错了单位（最严重，是真实数据错误）

- `pipeline._bisect` 的 `lo`/`hi`/`mid` 是**发送字符数**（探测发 `"x" * n`），
  返回值直接进 `models[].max-context-length`，而 CPA 把那个字段当 **token** 用：
  `model_registry.go:1440` → `/v1/models` 的 `max_context_length`；
  `codex/models/models.go:206-211` → `context_window` / `max_context_window`
- **铁证**：桌面 `config.yaml` 里 6 处 `max-context-length: 987500` 正是旧二分
  第三个中点 `(875000+1100000)//2` 的字符数，不是任何站声明的窗口
- 后果：虚报约 4 倍 → 客户端按虚高窗口定压缩点 → 塞到真实上限外被上游截断，
  正是 README 第 08 章那条 400
- 修法：新增 `_CHARS_PER_TOKEN = 4` 与 `_chars_to_tokens()`，
  **两个** return 都要折算（`ok_hi` 那条 + 二分收敛那条）；
  `declared`（上游正文自报）与 `input_tokens`（截断反推）本来就是 token，
  **不得**再折算
- 旧值迁移：`batch._fix_legacy_char_context()` —— 旧二分取值集合封闭可枚举
  （17 个值，公差 56250），命中格子的折回 token，格子外的原样留。
  **`200000` 有意排除**：它既是旧下界，也是 Claude/GPT 两系最常见的真实窗口，
  折它会把一个正确的声明值改小（误差方向变成「报小」，那是真实损失）
- 连带：`score_verdict` 里 `< 200_000` 那条判据此前实际比的是 20 万**字符**
  （≈5 万 token），几乎永不触发；单位修正后才真的生效

### 2. 兜底清单覆盖既有条目的模型清单（数据丢失）

- 实测：tango 的 claude 条目原有 4 个模型
  （`claude-opus-5` / `-thinking` / `claude-opus-4-8` / `-4-8-thinking`），
  重探判死后 `models` 被换成 `claude-fable-5-1` 等 6 个**这个站从没验过**的名字
  → `claude-opus-4-8` 与 `-4-8-thinking` 直接消失
- 根因：`plan.py` 的兜底分支无条件用 `model_catalog.latest_models()`
  （「当前市面最新」，与「这个站卖什么」无关），而中转站关 `/models` 是常态
- 修法：新增 `existing_models_for(cfg, section, base_url, api_key)`，
  兜底时**原清单优先**，`model_source` 记新值 `prior`
  - 判据用「原清单存不存在」而不是 `rebuild` 标志 —— 手工重贴既有站是同一处境
  - 「站」这一维走 `entry_scope`（前三段 host、compat 段含路径的 provider 身份），
    与 `CarryTables` 同口径。**这个键分叉在本项目发生过两次**
  - 键含 `api_key`：同站另一把 Key 的清单不许串过来
- `prior` 要在**四处**注册（漏一处就是「后端认、界面不认」那类缺陷）：
  `plan._SRC_LABEL_CN`、`writeback._SRC_LABEL`、`plan._EVID`（与 catalog 同档 1）、
  手填丢弃时的 `whence` 表
- `prior` 与 `seed` 同办的两道闸：`new_section_admitted()` 不放行（不许凭它新增段）；
  `web/app.js` 不回写 `S.forced`（回写会让后端当成手填 → 徽标变「手填」+ 跨段闸放行，
  正是 121 条目变 246 那次事故的路径）

### 3. 同一份措辞表分叉成两处（方向反了的误判）

- `classify.py` 的「分组无该模型渠道」规则与 `pipeline._MODEL_SPECIFIC_DEAD_END`
  各写一份平行措辞表，已分叉：`分组无该模型渠道`、`当前分组下无此模型的渠道`
  两种正文 classify 认、pipeline 不认（7 种正文里错 2 种）
- 后果：`_stage1_baseline` 靠 `_model_specific_dead_end` 决定
  「换个模型再试」还是「整段判死」→ 不认 = 立即收敛整段，
  而那本来是换个模型就可能通的站。快照里 23 个全灭凭据报的正是这一句
- 修法：`classify.MODEL_CHANNEL_BODY` 单一来源，pipeline 导入它；
  测试断言 `classify` 的规则对象 **is** 那个常量（不许再手抄）

## 四、测试

全套 1705 项全过（`tests/run.py`）；带真实 config 跑 1707 项全过
（`python3 tests/run.py <config.yaml 路径>`）。
`test_full_redetect.py` 57 项（原 54 + 新 3）。三个新用例：
`上下文上限单位是 token`、`死路措辞表与 classify 共用`、`兜底不覆盖既有清单`。

**撤销验证抓到一次假绿**（值得记住的形态）：
最初按 `chars//4` 造 `input_tokens` 测「hi 一发通过」，
而 `275000 < 1100000*0.5` 命中的是**截断分支**，返回值恰好等于折算结果 ——
于是把 `return _chars_to_tokens(hi)` 改回 `return hi` 测试照样全绿。
改成 `input_tokens = 发送字符数` 才真的走 `ok_hi` 那条路。
另外「hi 通过」与「二分收敛」是**两个** return，只测前者时改后者也全绿，
所以补了 `①b` 用例（正文不提数字、超过 60 万字符就 400，逼二分走完）。

四次撤销验证的结果：R1a 红、R1b 红、R2 红、R3 红，恢复后全绿。

## 五、本轮继续推进的工作（2026-09-06 晚间）

### 4. 405 Method Not Allowed 判定缺失（第四个真缺陷）

- **现象**：zulu 维护期间对所有 POST 一律回 `405 Method Not Allowed` + nginx HTML，
  GET 回 200 HTML 维护页。用户有 29 个健康 codex 凭据，却因优先级最高的 zulu
  返回 405 而全失败 —— CPA 直接把 405 返给客户端而不轮换下一凭据。
- **根因**：405 在 CPA 里既不是客户端错误（`clienterror/client_error.go:95-112` 仅覆盖
  400/401/402/403/404/406/408/409/410/413/414/415/422/429，**不含 405**），
  也不在用户 `config.yaml` 的 `request-scoped-errors` 里（实测 177 条目零配置），
  导致 CPA 收到 405 后不降级、不重试、不轮换，直接透传给客户端。
- **修法**：
  1. `classify.py` 新增 405 关键词规则（正文含 `405 not allowed` / `method not allowed` 等），
     判定为「临时 · 405 Method Not Allowed」
  2. 状态码兜底分支新增 `if s == "405": return "临时", "405 Method Not Allowed"`，
     保证裸 405（空正文或无关键词）也判为「临时」
  3. 「临时」类别对应 CPA 的 `continue-and-cooldown` 规则（与 500/502/503 同处理），
     让 405 触发降级与轮换
- **测试**：新增 10 项（test_edges.py ⑬），覆盖关键词匹配与兜底两条路径，
  撤销验证删除两层防护后回退到「未知 · 未覆盖的状态码 405」（符合预期）
- **影响面**：本项目所有判定路径（pipeline / server / replay）均经过 `classify.classify()`，
  修改一处全生效。CPA 的 `request-scoped-errors` 配置由本工具生成，下次推送自动包含新规则。

测试结果：test_edges.py 从 122 项增至 132 项，全过；全套仍 1705 项全过。

**撤销验证**（删除两层防护后回退到「未知 · 未覆盖的状态码 405」）：
```python
# 删掉关键词规则里的 405 相关行
if any(k in bl for k in ["405", "not allowed", "method not"]):
    return "临时", "405 Method Not Allowed"
# 删掉兜底分支里的 405 判定
if s == "405":
    return "临时", "405 Method Not Allowed"
```
撤销后 test_edges.py ⑬ 全红（5 项），恢复后全绿。实测 7 个状态码组合全判为「临时」。

## 六、未完成

- `claude-fake-5` 的来路**已查清，缺陷未修**（下一轮第一件事）。
  它不在本仓库、不在桌面 `config.yaml`、不在 Go 参考实现、也不在远程 CPA
  权威名录（实拉 77 个模型、17 个 `claude-*`，`fake` 零命中）。
  它来自 `latest_models` 的**第 2 层**：`_cfg_models(cfg, section)` ——
  「本地 config.yaml 里已经写着的模型名」也算候选。
  实测复现：只要 config 里**任何一个站**写了 `claude-fake-5`，
  另一个判死站的兜底清单就会拿到它（layer2 -> latest_models -> 该站 models）。
  这一层设计上是对的（站方特供型号只在这层，实测 4 个确实能用的名字不在
  CPA 名录里），问题是它**不区分「这个名字属于哪个站」**——
  A 站的名字（可能是手误、也可能只有 A 站卖）会被推荐给 B 站。
  `name_is_safe()` 只挡非法字符（中文、`[`、空格），挡不住「合法但不存在」。
  修的方向：第 2 层要么加「多少个站在用」的门槛（只有一个站用的名字不外推），
  要么把来路带到界面上说清「这个名字来自本地配置的另一个站，不是权威名录」
- 三个子代理（pipeline+plan、server+web、probe transport 形态对齐）
  全部因 API 错误中途失败，未拿到汇报。**probe transport 与 CPA 的形态对齐
  一项都没查** —— 那是历史上误判的主要来源，须补
- `writeback` 侧「未勾选的既有条目会不会被删改」这个问题**没有得到验证**。
  从 `_orphan_entry_lines` 的注释看是「原样搬回」，但没实跑证明
- 工作区里有一处**未提交且未接线**的改动：`plan.py` 的
  `session_affinity_on()` / `affinity_crosstier_note()` 两个函数
  （`git diff` 里 76 行新增，`build_plan` 内已加调用），
  生产配置 `routing.session-affinity` 当前是 `false`，所以此刻不触发
- 全部改动**尚未提交**

## 六、复现用的临时脚本（都在 `%TEMP%`，可重跑）

- `full.py` —— 用真实 config 跑 tango 整链，看模型清单丢没丢
- `mig.py` —— 看 `existing_model_context` 对真实 config 的折算结果
  （12 处：kilo 10 处 987500→246875、zulu 2 处 15515 原样）
- `d6.py` —— 两处措辞表的分叉对比
- `newsite.py` —— 新站 / 既有站新 Key 不许继承别人清单
