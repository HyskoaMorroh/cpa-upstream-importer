# cpa-upstream-importer

**CPA（CLIProxyAPI）上游账号批量导入**：粘贴 `url,key` → 自动探测判定 →
建议 priority → diff 确认 → 写回 `config.yaml` → 触发 CPA 重载 → 端到端验证。

[![build](https://img.shields.io/badge/tests-passing-brightgreen)](tests/)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![platform](https://img.shields.io/badge/platform-amd64%20%7C%20arm64-lightgrey)](deploy/Dockerfile)

给谁用：手里有一堆中转站账号，要接进 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
做多上游轮询，而**不想逐个手写 200 行 YAML**、也不想在写错 priority 之后
花两个通宵排查「为什么面板显示 100% 成功率但客户端一直报 403」。

它解决什么：

| 问题 | 这个工具怎么办 |
|---|---|
| 一个新账号该进哪个段？带不带 `/v1`？ | 四段各打一次真实请求定归属，`base-url` 形态按段自动规范化 |
| `priority` 填多少？ | 读现有档位谱 + `weight:0` + 注释里的实测结论，给出**不挡在用站**的最高档 |
| 站方静默换模了怎么办？ | 比对返回的 `model` 字段，换模的默认不勾选 |
| 写完 `config.yaml` 后 CPA 不生效 | 自动 `PUT` 触发重载 + **读回校验**（抓 inode 分叉） |
| 挂机时客户端收到 403 / 524 | `tools/diag403.py` 算出「重试预算 vs 顶层池」够不够 |
| 手里的账号现在还能用吗？ | `tools/recheck.py` 按各站**自己声明的模型**复核，不用种子模型猜 |

> **图解教程：[`docs/tutorial.html`](docs/tutorial.html)** —— 十章，带流程图与
> 档位谱示意图，讲清每一步<b>为什么这么设计</b>。本 README 讲怎么用，
> 那份讲为什么；每条判定背后都是一次实测误判的修正。
> 直接在浏览器里打开即可（单文件、零外链、零脚本，跟随系统深色模式）。

---

## 快速上手

### Docker（推荐，支持 amd64 / arm64）

```bash
git clone <你的仓库地址> && cd cpa-upstream-importer

cp .env.example .env
$EDITOR .env          # 至少要设 CONFIG_PATH，指向你的 CPA config.yaml

docker compose up -d
docker compose logs cpa-upstream-importer | head -20    # 拿访问地址与 token
```

浏览器开 `http://127.0.0.1:8765/`，用 **CPA 后台的管理密码**登录
（或 `.env` 里的 `IMPORTER_TOKEN`）。

用现成镜像而不是本地构建：

```bash
# .env 里改两行
IMAGE=ghcr.io/<owner>/cpa-upstream-importer:latest
PULL_POLICY=missing
```

### 命令行（不开网页）

```bash
pip3 install "PyYAML==6.0.2" "bcrypt==4.2.1"

python3 cli.py -i accounts.txt --config /path/to/config.yaml --dry-run   # 零请求，先看解析
python3 cli.py -i accounts.txt --config /path/to/config.yaml             # 探测但不写
python3 cli.py -i accounts.txt --config /path/to/config.yaml --write     --mgmt-key "$MGMT"                                                  # 写回 + 自动重载
```

`accounts.txt` 每行 `url,key`，带不带 `/v1` 都行。

### 只做诊断，不导入

#### 命令行工具（零请求，只读分析）

```bash
python3 tools/diag403.py /path/to/config.yaml    # 结构性诊断：预算 vs 顶层池
python3 tools/recheck.py /path/to/config.yaml --top-only   # 复核既有凭据
```

`diag403.py` 算的是**结构性问题**：

- **预算 < 顶层池** → 有可用凭据轮不到，耗尽后 CPA 透传 403
- **预算太大** → 越过反向代理回源窗口（Cloudflare 100 秒），客户端收 524
- **连续同站** → 预算越大越可能连打同一站，触发站方 Cloudflare 速率限制

`recheck.py` 按条目自己声明的模型打真实请求，告诉你「哪些站挂了」。

#### 图形化调优面板（网页）

**新功能**：Web 界面「全局调优体检」面板，算出建议值并可一键写回。

访问 `http://127.0.0.1:8765/`（Docker 默认端口），展开「全局调优体检」：

1. **点「开始体检」** —— 读取 config.yaml、计算顶层池实况、给出改动建议
2. **预览 diff** —— 只改值，键序 / 注释 / 空行 / 缩进逐字保留
3. **确认写回** —— 自动备份 + 写回 + 触发 CPA 重载

**为什么要这个面板**：

`max-retry-credentials` 与本工具写的 `priority` 是**耦合**的——预算要覆盖
顶层池，而顶层池由档位决定，每次重探后档位都可能变。这份耦合原来只写在
config.yaml 注释里靠人工核对，现在工具算给你看：

```
单次失败尝试按 2.9 秒算（3 秒连接超时 - 10% 提前量）· 回源窗口 100 秒

claude-api-key  顶层档位 900 · 5 个凭据 · 3 个站
                最长连续同站 3 个（one.example）—— 会连打同一个站
```

如果顶层池 5 个而预算只有 4，面板会标红：

```
max-retry-credentials: 4 → 5    【必改】
当前预算 4 < 顶层池 5，有可用凭据永远轮不到 —— 预算耗尽后 CPA 透传 403
```

**对比**：

| | `diag403.py` | 图形面板 |
|---|---|---|
| 形态 | CLI，只读报告 | 网页，能写回 |
| 输入 | 手工指定文件路径 | 自动读 `.env` 的 `CONFIG_PATH` |
| 输出 | 文本报告 | 改动预览 + diff + 一键写回 |
| 适合 | CI / 自动化脚本 | 人工调优、快速修正 |

---

## 发布到 GitHub 与 Docker Hub

### 提交前先隐去域名

要隐去的有两类，**第二类最容易漏**：

- **自有域名** —— 客户端入口、管理面板、本服务自身、以及自己那台机器
  作为上游时的域名。同一个域名多种角色，替换目标各不相同。
- **第三方中转站域名** —— 这些是排障案例的主体。哪个站 403、哪个静默
  换模、哪个按出口 IP 拉黑，公开出去等于公布「我在用这些站、它们各自
  什么毛病」。它们会散布在注释、测试数据与文档里，`grep` 一遍远比想象的多。

替换脚本本身**不适合放进仓库** —— 它的替换规则表里必须逐条写着原始域名，
否则认不出要替换什么。也就是说那个脚本就是一份域名清单，进了公开仓库等于
把要隐去的东西直接发出去（本项目实际踩过这一步）。建议放在仓库之外，
或让脚本从一个被 `.gitignore` 排除的规则文件里读。

写这类脚本时有三个性质必须在替换后仍然成立：

1. **占位符字母按原名字母序分配**。`test_probe.py` 有断言把运行时
   `sorted()` 的结果和源码里的字面量比对，字母乱分配会让两边排序不一致 ——
   测试失败，而原因跟被测逻辑毫无关系。
2. **短名 == 域名首标签**（`foxtrot` / `relay-f.example`）。
   `host_matches_note` 的无别名表兜底靠这个性质。
3. **但有一对故意不共享子串**（`jdw` / `relay-h.example`）。
   `test_tiering.py` 那条「必须靠别名表才认得」的断言靠它。
   只顾 2 不顾 3，那条断言就恒真而不再验证任何东西。

测试里当作**前缀误匹配反例**的域名不要替换 —— 换成占位符后那条断言会恒真。

替换完必须跑一遍 `python3 tests/run.py`，全过才算替换没破坏断言数据。

### 检查有没有秘密要泄露

```bash
git status --short
git diff --cached | grep -iE 'sk-[a-z0-9]{20,}|secret-key' || echo "干净"
```

`.gitignore` 已列出 `config.yaml`、`.env`、`accounts.txt`、`*.bak-*` 等。
**这些文件含明文上游 API Key** —— 一旦推上公开仓库，即使随后
force-push 删掉，fork 与第三方缓存仍可能留有副本，那些 Key 必须视为
已泄露并全部轮换。

### CI 会自动做什么

`.github/workflows/build.yml`：

| 触发 | 动作 |
|---|---|
| PR | 3 个 Python 版本跑测试 + 构建镜像（**不推送**） |
| 推 `main` | 测试 → 构建 → 推 `latest` 与 `sha-xxxxxxx` |
| 推 tag `v*` | 同上，另加 `1.2.3` 与 `1.2` 语义化标签 |
| 手动触发 | 可自定义 `platforms`（比如加上 `linux/arm/v7`） |

**GHCR 零配置** —— `GITHUB_TOKEN` 自带 `packages:write`，fork 也直接可用。
镜像名从 `github.repository` 自动取。

**Docker Hub 需要配三项**（两必一选，不配就跳过那一步，不会失败）：

```
Settings → Secrets and variables → Actions

  Variables 标签 → New repository variable
    DOCKERHUB_USERNAME   你的 Docker Hub 用户名     必填
    DOCKERHUB_IMAGE      镜像名                     可选，留空 = 用仓库名

  Secrets 标签 → New repository secret
    DOCKERHUB_TOKEN      Access Token               必填
```

| 变量 | 放哪 | 为什么 |
|---|---|---|
| `DOCKERHUB_USERNAME` | Variables（放 Secrets 也能用） | 用户名本来就公开，明文便于排查 |
| `DOCKERHUB_IMAGE` | Variables | 同上；Docker Hub 命名空间只有一层，镜像名常与仓库名不同 |
| `DOCKERHUB_TOKEN` | **Secrets** | Variables 在构建日志里是**明文**，放这里等于把 token 印在每次日志上 |

用户名放 Variables 或 Secrets 都能工作（工作流两处都读，`vars` 优先），
但**推荐 Variables**：放进 Secrets 后它在日志里会被打成 `***`，镜像名跟着
显示成 `***/xxx` —— 推失败时看不出到底推去了哪个命名空间。

Access Token 在 `hub.docker.com/settings/security` 生成，权限 `Read & Write`
就够。**不要填登录密码** —— token 可单独吊销、可限权限；密码泄露等于整个
Docker Hub 账号失守。

两项必填**都要有**才会发布。只设用户名、稍后再加 token 是很自然的操作
顺序，而那样会让每次构建都红在 401 上，且看不出是「token 还没配」——
所以 CI 里判的是两者同时存在，缺任一安静跳过。

`DOCKERHUB_IMAGE` 留空时用 GitHub 仓库名的**后一段**。不能直接用
`github.repository`：那是 `owner/repo`，推到 Docker Hub 会变成
`<user>/owner/repo`，而 Docker Hub 的命名空间只有一层，斜杠会被拒。

### 手动构建多架构镜像

```bash
docker buildx create --use --name multi 2>/dev/null || docker buildx use multi

# 先登录（凭据存在 Docker Desktop 的 credsStore 里，不会落到文件）
docker login

# VERSION 会写进镜像的 org.opencontainers.image.version 标签。
# 同时打 latest 与版本 tag —— latest 方便 compose 拉，版本 tag 用于回滚。
VER=$(git describe --tags --always 2>/dev/null || git rev-parse --short HEAD)

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f deploy/Dockerfile \
  --build-arg VERSION="$VER" \
  -t <你的仓库>/cpa-upstream-importer:latest \
  -t <你的仓库>/cpa-upstream-importer:"$VER" \
  --push .

# 确认 manifest list 里真的有多个平台
docker buildx imagetools inspect <你的仓库>/cpa-upstream-importer:latest
```

加 `linux/arm/v7` 会慢很多 —— `bcrypt` 在那个平台没有预编译 wheel，
要现场用 Rust 编译。Dockerfile 按 `TARGETPLATFORM` 自动判断，
amd64/arm64 跳过编译工具链的安装。

推完在 VPS 上升级：

```bash
docker compose pull cpa-upstream-importer && docker compose up -d
```

---

## 兼容性

| 项 | 支持 |
|---|---|
| CPU 架构 | `linux/amd64`、`linux/arm64`（预构建镜像）；`linux/arm/v7` 可自行 buildx |
| Python | 3.9 - 3.13（CI 在 3.9 / 3.12 / 3.13 上跑全套测试） |
| 依赖 | `PyYAML` + `bcrypt`。`cpa_probe/` 本身只用标准库 |
| 宿主系统 | 任何能跑 Docker 的；裸机跑需要 Python 3.9+ |

`bcrypt` 在 arm/v7 等平台没有预编译 wheel，Dockerfile 会按
`TARGETPLATFORM` 自动决定要不要装 Rust 工具链 —— amd64/arm64 走纯下载，
其他架构现场编译。同一个 Dockerfile 覆盖所有情况。

---

## 核心机制

### 路由批量管理（2026-09-11）

**问题**：CPAMP 的「AI 提供商」页只能逐条操作 —— 表格没有多选列，
后端 provider 段只有 `GET/PUT/DELETE`、没有任何 bulk 路径，它自己的
「按结果应用」是 `for` 循环逐条 read-modify-write（每条都 `GET /config`
+ `PUT` 整个 158 条数组）。更要紧的是它按条目扁平分页（158 条 / 10 条一页
/ 16 页），**看不出「同一个网址下几把 Key 的档位一不一致」**。

**为什么这件事重要**：CPA 按层级调度，只取 priority 最高那一桶
（`selector.go` 的 `availableAuthsFromPriorityBuckets` 只收 `bestPriority`）。
同址多 Key 一旦分裂档位，高档那几条会被优先抽中并先耗尽，低档的实质是冷备 ——
本该并行轮询的多把 Key 变成了主备切换。

**实测对账**：拿部署前后两份 `config.yaml` 逐组比对，**注入前 40 组 0 组分裂，
注入后 3 组分裂**：

| 段 | 网址 | 条目 | priority |
|---|---|---|---|
| codex | `romeo.example/v1` | 4 | **{350, 147}** |
| codex | `golf.example/v1` | 7 | **{348, 149}** |
| gemini | `romeo.example` | 3 | **{218, 215}** |

`romeo` 那 4 条的 `models` / `headers` / `proxy-url` **逐字相同**，
唯独 idx9 是 350 —— 纯漂移，不是有意配置。

**根因**：`assign_priorities` 本身守住了「同 host 同档」，但它**之后**有两处破坏：
用户手工改档是按 `rid = row.line_no`（每把 Key 一行）应用的，改一把就分层；
而 `priority_collisions` 只检测「不同 host 同值」，**不检测「同 host 不同值」**，
于是破坏了也零告警。已补 `priority_split_within_host()`，列为**阻断级**。

**做法**：新增 `cpa_probe/bulk.py` + `GET /api/routes` +
`POST /api/bulk-preview` / `/api/bulk-apply`，前端加「路由批量管理」面板
（按 段 × 网址 分组，分裂的组红框顶出来）。与 CPAMP 的对比：

| 维度 | CPAMP | 本项目 |
|---|---|---|
| 写入 | 分段 PUT，重新序列化 | 行级改写整份文本 |
| 注释 | 一律丢失 | 逐行保留（实测 1744 行守恒） |
| 改 10 处 | 10 轮全量 PUT | **1 轮** |
| 条目定位 | `api-key`+`base-url` 查询，重复条目命中哪条不确定 | 该段数组**下标** |
| 并发写 | 两个标签页静默互相覆盖 | 基线比对，冲突 409 |

**启停是两套字段**：key 类段往 `excluded-models` 塞通配符 `"*"`，
`openai-compatibility` 用布尔 `disabled`。写错的后果是「界面显示已停用、
CPA 照常轮询」。**这两个值不写死** —— `disable_semantics()` 每次从 CPAMP 源码
实时解析，带 6 小时缓存。实测把上游通配符改成 `ALL`、字段改名 `isPaused`，
本项目不改一行代码就跟着写对，且往返逐字节可逆。

**不做批量删除**：删除不可逆，且下标会随删除移位（前一个删除让后一个下标失效）。
当前只提供批量启用 / 停用 / 统一优先级，三项都可逆。

#### 单站就手改档 + 站间档位自动错开（2026-09-13）

用户现场反馈原话：「根本没法对每个相同域名的上游优先值进行手工调整」。
核实后的状况是**功能都在、但藏起来了**：卡片上的档位是死文本
（`<span class="p">`），而操作栏是 `$('#bmact').hidden = sel.length === 0` ——
54 个卡片全未勾选时整条操作栏**不渲染**，于是那个面板看起来根本没有批量功能。

改了三处：

| 原来 | 现在 |
|---|---|
| 改一个站的档位：勾选 → 滚到面板底部 → 填数字 → 点应用（四步，视线离开卡片） | 卡片头部就是输入框，改完即进待提交队列 |
| 操作栏未选中时 `hidden` | **常驻**，未选中时置灰 + 说明「为什么点不动」 |
| 「批量设为」是裸 `input number`，没有参照 | 加**档位预设按钮**：列出该段现有档位谱可点选，另有「最高+1 / 最低-1 / 中值」 |
| 单站启停/删除要绕道批量操作 | 卡片内联 `[启用\|停用] [删除…]`，悬停出现 |
| 档位无法一眼分辨 | 档位按黄金角轮转配色，**同色 = 同档** |

**为什么「批量设为 350」必须做撞值消解**：那是跨组动作，一次能命中几十个组。
全落 350 会被 CPA 的 selector 当**同一个桶**按 weight 轮询 —— 站与站的先后
次序被推平，而 priority 的**唯一**作用就是区分先后。用户第 3⑶/第 7 条的要求是
「同一个类型不同域名的优先级一定要不同，哪怕算出来相同，也要适当做点微调给出点偏差」。

`resolve_priority_collisions()` 按 `(-目标值, host)` 排序，同目标值的组依次
下移 `step`（默认 1），并避开该段**既有的**在用档位。用 host 字典序而不是
勾选顺序定序，是因为选中集是个 Set，遍历顺序不稳定 —— 同样的输入两次预览
必须给出同样的结果，否则 diff 无法复核。此前的 `priority_collisions`
只在 `build_plan` 那条路上被调用，批量管理直接改既有条目、不经过它，所以
那边报了也没人看。

放在服务端（`_resolve_op_collisions`）而不是前端：单站直改与批量设档是两条
路，两条都必须过同一道消解，否则「单站改的撞了、批量改的不撞」这类不一致
迟早会出现。消解说明走 `collision_notes` 单独一份，前端顶在 diff 上方 ——
它解释了「我填 350、落盘却是 349」。

写回走与投喂流程**完全相同**的链路：预览 diff → 人工确认 → 基线比对 →
YAML 校验 → 备份落盘 → 推送重载 → 读回校验，并共用同一把写盘锁。

### 全量重探的定档被 CF 切成 524（2026-09-13）

现场（投喂台 mhtml 快照）：③ 判定与定档 顶上一条红框，内容是**一整页 CF 的
`524: A timeout occurred` HTML**（含 `normalize.css` 的 `<style>` 与
`[endif]-->` 注释），priority 与建议栏全停在占位符。两个独立缺陷叠在一起：

1. **前端把反代错误页原样倒进界面**。`api()` 的兜底 `JSON.parse(txt)` 失败后
   把 `txt.slice(0, 400)` 当 error 抛出来 —— 而 CF 的 524 页正文就是 HTML，
   真实信息埋在 300 字符之后。已加 `_proxyErrText()`：认出 HTML 错误页后按
   状态码翻译成人话，并把 `<title>` 里的真实信息提出来。
2. **`/api/plan` 跑过了 CF 的 100 秒上限**。nginx 侧是 `proxy_read_timeout 600`，
   所以断在 CF 那一层。而这次请求**并没有白跑**：前端每次勾选变化都防抖
   180ms 后重发一次 `/api/plan`，除 `selected` 外的入参（job / overrides /
   forced / 配置原文）完全一样，173 站的重建在一轮操作里被重复做了好几遍。

已加**定档响应缓存**（键 = job + overrides + selected + forced + `by_score` +
`config_revision(raw)`，TTL 180 秒，最多 4 条）与**耗时日志**
（`定档完成：X.X 秒 · 全量重探=True · N 个候选`）。缓存键里必须带配置指纹：
配置在别处被改过时要立刻失效，否则会拿旧基线的方案去写回 —— 那正是
`add_plan` 存 `base_raw` 要防的事。

**根因未消除**：173 站全量重建单次若真超过 100 秒，第一次冷启动仍会 524。
要看日志里那个秒数再决定是否把 `/api/plan` 改成异步任务（复用 `job` 轮询
那条链路）—— 那是改协议，不在本轮范围。

详见[图文教程 07.95 章](docs/tutorial.html#s7-95)。

### 截图里的一堆问题：模型乱、待定、低档乱选（2026-09-16 全部修复）

**现场现象**（2026-09-15 投喂台 mhtml 快照，173 站，1144 次请求）：

- 692 个 priority 格全停在「待定」占位符，根本没写进去
- 1772 个 mini / flash / fast 低档模型被勾上（应当全部滤掉）
- gorouter.app 一个站吃掉 **420 次**请求；zzzcoding 84 次；两个站占全轮 44%
- 前端 LOW_TIER 正则比后端少 `flash|fast`，导致低档模型在界面里看起来已勾选
  但写回时被后端悄悄删掉 —— 界面与 config.yaml 不一致
- 订阅 URL 含百分号编码（`%2F` / `%3D`）时 bootstrap 脚本的 init 容器
  直接 TypeError 崩掉，mihomo 代理永不启动，34 条 `proxy-url` 全部失效

**根因与修复**

| 现象 | 根因 | 修复 |
|---|---|---|
| 692 个「待定」 | 全轮 1650s 触发 CF 524，响应在 plan 阶段就被 Cloudflare 截断 | 已在上一节说明，本轮加定档结果缓存（TTL 180s）减少重跑 |
| low-tier 乱选 | `web/app.js` LOW_TIER 正则少 `flash\|fast` | 已补全，前后端正则一致，test_web 275 项通过 |
| 420 次重复 502 | 「临时/未知」类不缓存，但同站后续 Key 重复问同一个挂掉的站 | 熔断机制：同 (站,段) 连续 3 次完整探测同码 → 后续 Key 零请求直接复用 |
| `000` 打断熔断计数 | 网络抖动产生 `000`（连接失败）会清零已积累的连续 502 计数 | `000` 既不计数也不重置，不把「没拿到回答」当成「站方行为变了」 |
| 代理永不启动 | `hc_block(url) % name` 二段式 `%` 格式化遇 `%2F` 崩溃 | 一次填完，`json.dumps` 处理 URL 保证 YAML 安全 |

**拉新镜像后还会出现这些问题吗？**

不会出现模型乱、待定、低档乱选这三类。具体保证：

1. **LOW_TIER 前后端一致** —— `mini / nano / lite / flash / fast`
   五类在前端界面和后端写回两处用同一条正则，不会出现「界面已勾 → 写回被删」。
2. **待定不会发生** —— `topup_to_market_top()` 强制填充：检测到任何已通模型就
   自动补全同 generation 全系列；若最新代全不通，也会填充最高档并保留次新代，
   不会让任何条目停在空值。
3. **熔断防超时** —— gorouter / zzzcoding 类稳定故障站在第 3 次完整探测后短路，
   后续 Key 零请求复用，总请求量大幅下降，触发 524 的概率降低。

**还需要你手动操作的一件事**：compose 里加 `CPA_MANAGEMENT_TOKEN`，
否则健康分定档会静默退回到「静态检测分」，见上一节。



用户第 3⑶ 条要求「按 CPA 实际运行状态定优先级」——很多站是**故意被关掉但
调用成功率很高**，那些参数的停用/调度开关应当重新打开、档位应当靠前。

这条链路的实现是：`plan.py` 调 `runtime_health.fetch_cpa_runtime_health()`
拿 `/v0/management/api-key-usage`，算 `健康分 = 可调度比例×60% + 活跃比例×40%`，
再按分降序排档。

**它需要一个管理令牌，而这个令牌直到 2026-09-16 才进 compose。**

`fetch_cpa_runtime_health(base_url)` 的签名里 `management_token` 是可选参数，
而唯一的调用点**不传它** —— 只读环境变量 `CPA_MANAGEMENT_TOKEN`
（`runtime_health.py:69`）。仓库的三份部署文件原来都没有这个变量，于是：

```
401/403 → 返回 None → 健康分退回「静态检测分」
```

日志里只有一行 `段 X：CPA 运行时数据不可用，将基于检测结果预测健康分数`。
**特性静默失效，而部署文件看起来都是齐全的。**

已补进本仓库的 `docker-compose.yml` / `docker-compose.local.yml` / `.env.example`。

#### 改仓库模板还不够：真正生效的是你自己那份 compose

本仓库的三份是**模板**。实际部署往往用一份独立维护的 `docker-compose.yml`
（整栈的那份，含 CPA、CPAMP、sub2api、mihomo…），它不在本仓库里。
**改了模板而没改那一份，镜像里带着修复的代码，变量却没注入，特性照样不生效。**

2026-09-16 实测就踩了这一脚：仓库三份都补好、镜像也推上去了，而线上仍是
「CPA 运行时数据不可用」——因为线上那份 compose 的 `environment:` 里根本没有
这个键。

在你自己那份 compose 的 `upstream-importer` 服务下加：

```yaml
    environment:
      # …既有的 IMPORTER_* / CPA_UPSTREAM_URL …
      CPA_MANAGEMENT_TOKEN: "${CPA_MANAGEMENT_PASSWORD:-}"
```

**值建议写成引用而不是再抄一遍明文**：CPA 容器那边用的是
`MANAGEMENT_PASSWORD`，投喂台这边叫 `CPA_MANAGEMENT_TOKEN` ——
**同一个管理密码、两个变量名**。写成引用后改密码只需动 `.env` 一处，
两个服务同时跟上；抄明文迟早会分叉成两个值。

取值就是 CPA 后台的管理密码（或 `config.yaml` 里 `remote-management.secret-key`
对应的**原始密码** —— CPA 自己会把 bcrypt 哈希贴回那个字段，哈希本身不能当密码用）。

**怎么确认变量真的进了容器：**

```bash
docker exec upstream-importer sh -c 'echo "长度: ${#CPA_MANAGEMENT_TOKEN}"'
# 输出 0 就是没注入
```

**怎么确认它在工作：** 跑一次导入，看日志里有没有

```
段 claude-api-key：已获取 CPA 运行时健康数据，将基于实际运行状态分配优先级
```

出现「不可用」那行就是令牌没生效，此时档位全部来自静态检测分。

### 上游常量自动同步（2026-09-11）
本项目对 CPA / CPAMP 的所有形态假设都**从上游源码实时解析**，不抄成常量：

| 解析什么 | 来源文件 | 用途 |
|---|---|---|
| codex 强制/删除的请求体字段 | `codex_executor_execute.go` | 让探测形态跟着 CPA 升级自动对齐 |
| 身份头常量（UA / originator / beta 族） | `claude_executor_request.go`、`codex_executor_request.go` | 画像梯的值 |
| 停用语义（通配符 / 布尔字段名） | CPAMP `providers/utils.ts`、`types/provider.ts` | 批量启停写哪个字段 |
| `disable-image-generation` 实际取值 | 运行时 `config.yaml` | 决定探测要不要发 `image_generation` 工具 |

统一入口 `cpa_source_probe.cached_identity()`，三层回落：
显式路径 → 环境变量 `CPA_SOURCE_ROOT` / `CPAMP_SOURCE_ROOT` → GitHub 远程，
6 小时缓存且**失败也缓存**（国内直连 raw.githubusercontent 不通时不会每次干等）。
任何一层拿不到都回落内置默认并在漂移报告里说明，不静默。

实测提取结果：强制 `{stream: true, model: baseModel}`，
删除 `[previous_response_id, generate, prompt_cache_retention, safety_identifier,
stream_options]`；CPAMP 侧 `'*'` 与 `'disabled'`。

### 探测形态必须与 CPA 实跑一致（2026-09-11）

**问题**：一批上游在 cc-switch 里能用，填进 CPA/CPAMP 就报
`400 invalid codex request`，本项目的探测也把它们判死。

**逐字段实测**（alfa.example，模型 `gpt-6-astra`）：

| 请求体 | 结果 |
|---|---|
| 缺 `prompt_cache_key` | `400 invalid codex request` |
| 缺 `include` | `400 invalid codex request` |
| 两者都有 | 500「负载已达上限」（**形态被接受**） |

**身份头完全无关** —— 把 `Originator` / `User-Agent` 全部删掉，结果不变。

**根因**：这类中转（new-api 系）按**真实 Codex CLI 的字段集**校验请求。
而本项目原来发的是 `{"model", "stream": false, "input": "<字符串>"}` 三个字段，
CPAMP 的「连通性测试」发的也是同一个瘦身 body —— 所以**那个测试失败并不代表
网关坏了**，真实 Codex CLI 经 CPA 转发时是通的（CPA 在 codex 路径上不删
`prompt_cache_key`）。

**修法**：`request.py` 的 codex 基线补齐到真实客户端形态
（数组 `input` + `include` + `prompt_cache_key` + `instructions` + `reasoning`
+ `tool_choice` + `store`），并新增判定类别「**形态**」（`usable=False` 但
**不降权** —— 站没问题，问题在探测）。

复验：改后同一站从 `400`（判死）变为 `520 → 判定「临时」`，假阴性链断开。

### codex 段 headers 转发修复（2026-09-06）

**问题**：用户在 CPAMP 管理界面配置 `zulu.example` 的 codex 段，
手动填写了正确的 `originator: codex-tui` 和 User-Agent，测试连通性时仍返回 403
"This account only allows Codex official clients"。同一个 URL 和 Key 在
cc-switch 直连时正常。

**根因**：CPA 的 `codex_executor_request.go` 第 369 行调用
`applyCodexCloakingHeaders`，该函数在 `ApplyCustomHeadersFromAttrs`
（应用配置文件自定义 headers）**之后**执行，**无条件覆盖**了 User-Agent 和
Originator，导致配置文件的自定义值被丢弃。

**修复**：
1. **CPA 侧**：修改 `applyCodexCloakingHeaders` 为条件设置（只在 header
   不存在时设置默认值），尊重配置文件的自定义 headers
2. **upstream-importer 侧**：在 `plan.py` 第 2248 行添加 codex 段 originator
   门禁，提前发现配置错误

**通用性**：此修复适用于所有要求特定客户端标识的上游站点。CPA 的条件设置逻辑
确保：配置了自定义 headers → 使用配置值；未配置 → 使用默认值（向后兼容）。

详见[图文教程 FIX 章节](docs/tutorial.html#s-fix-403)。

### 批量定档（2026-09-02）

**问题**：79 个凭据写回后 `claude` 段 74 个条目**全是 175**、`gemini` 段 76 个全是
225。`suggest_priority` 每次只看「当前 config.yaml 有哪些空档」，串行调用 79 次
问的是同一个问题、拿到同一个答案 —— 而 `priority` 的唯一作用就是区分先后，
全同值等于这个字段没写。

**语义依据**（照现有 config.yaml 的规律，三段无一例外）：

```
kilo.example      5 个 Key   priority 1000
tango.example  14 个 Key   priority  990
gorou.example   15 个 Key   priority  985
```

**同一个站的所有 Key 共用一档，站与站不同**。这与 CPA 的调度一致：`priority`
决定哪一层先被尝试，同层内部按 `weight` 轮询（`selector.go:539-549` 只取最高
那一桶）。同站多 Key 指向同一个上游、能力相同，给它们不同值会把「多 Key 轮询」
变成「主备切换」—— 第 2 把 Key 只在第 1 把不可用时才被碰，白费配额。

**做法**：`assign_priorities` 收齐全批方案后按段处理。安全边界仍由
`suggest_priority` 划（不动现有值、不劫持顶层、试用期进最低可插档），本函数
只负责把各站分开：每站先算出自己的上限 `cap`，再按排序取
`min(cap, 上一站 - 1)`，跳过与现有档位相撞的值。

**排序键是「证据强弱 → 分数 → 主机名」**（2026-09-03 真实探测暴露后加第一个键）。
`score_verdict` 对 `usable=False` 一律返回 0，于是「四段全灭」与「探测通过但扣满分」
排在同一档，之后只按主机名排 —— **字母序决定谁上去**。

实测那次重探：`hotel.example` 四段全灭（WAF ×12）却在 claude 段拿到该段第 2 名、
gemini 段第 2 名，纯粹因为 `ai.` 开头。`claude-sonnet-5` 与 `gemini-3.1-pro` 的顶层
承载因此换到一个**刚被判死的站**上 —— 层级隔离下顶层站不可用时那一层整个白撞一轮
（`scheduler.go:402` 只取最高那一桶）。

判据用 `model_source`（这一段这次有没有依据）而不是 `score`（探测质量）：

| 档 | `model_source` | 含义 |
|---|---|---|
| 0 | `probed` | 本次实测跑通推理 |
| 1 | `manual` / `catalog` | 手填 / 站方目录声称有 |
| 2 | `seed` | 工具猜测，零依据 |

同档内仍按分数、再按主机名 —— 稳定性不能丢，否则同一批输入两次运行给出不同档位，
diff 无法复核。修复后 claude 段 8 个 `probed` 站占 493-500，5 个 `seed` 站掉到
170-175；两个热门模型的顶层回到实测通过的站上。

**组内证据不一致时点出来**：同站多 Key 共用一档是对的（同一个上游、能力相同），
但「这一档由谁的实测撑起来」得说清。实测 `golf.example` 的 claude 段 7 把 Key 里
6 把实测通过、1 把余额耗尽走了种子猜测 —— 那一把因此拿到与实测同档的 494，并把 3 个
它自己都没验过的模型顶上了顶层。档位不改，但 `priority_reason` 里会写「本 Key 的清单
来自工具猜测，同档是同站其他 Key 的实测撑起来的」，那句话原样落进 config.yaml 的
行尾注释。

第一版直接从空档由高到低铺值，绕开了那三条约束。拿生产 config.yaml 实测
（14 站 × 3 Key）：

| 段 | 第一版 | 后果 |
|---|---|---|
| codex | 787..618（上限 550） | 14/14 站抢走 `gpt-5.5` 等模型的顶层 |
| compat | 549..536（上限 520） | 14/14 站抢顶层 |
| claude | 999..949 | 最高档挡住 5 个在用站（试用期本该 0 个） |

抢顶层会让 `recommended` 整段翻假（劫持是不建议勾选的四个条件之一），
于是默认写入集合从 24 段塌到 12 段 —— 界面上表现为「codex 与 compat
两段默认一个都不勾」。现在四段全部 0 劫持、最高档挡 0-1 个在用站。

**空档太窄时整批下移**：`claude` 段高位档位谱极密（1000/995/990/985 相邻只差 5），
14 个站挤进去成了 999/998/997… 正确但手工微调余地几乎没有。所以当站数超过
该空档容量的两倍时，往下找**更宽且代价不增**的空档整批下移（只接受「挡住的
在用站数不多于原档」的）。代价是新站整体排得更低，这是明确的取舍。

**四类提示**会回到界面（`/api/plan` 的 `warnings`，显示在方案页顶部）：整批
下移、越过现有档位、压到最低值 1、以及**用户手工改出的同层**——
`priority_collisions` 在用户覆盖之后再查一遍，同层按 weight 轮询是合法配置，
但它取消的正是「不同网站不同优先级」，必须说出来而不是默默照写。

**`raw` 必须一路传到 `build_band`**。它只在拿到 config.yaml 原文时才解析注释里的
「实测不可用」结论，而那直接决定「挡住下层算不算代价」。实测差距（满分候选）：
`claude` 段 175 → 500、`gemini` 段 225 → 280。漏传不报错，只是把可用新站压到
一堆死站后面 —— 单站诊断、CLI、网页端因此会给出不同的 priority，现在三条途径
全部传。

#### 既有站沿用原档，重探不改站间次序（2026-09-04）

**现场**：同一个上游地址、只是 token 不同的几条，`priority` 不一致 ——
`kilo.example` 的 claude 段 5 条里 3 条 372、2 条 164；`tango.example` 14 条里
9 条 371、5 条 167。

两个独立成因，各自都足以造成拆档：

| 成因 | 位置 | 表现 |
|---|---|---|
| 重探的既有站被当新站，从空档重新分配 | `assign_priorities` | 全勾时整份配置的站间次序被推平：claude 段 12 个站从 1000/995/990/985/700/650/630/600/400/350/300/50 变成 500..489 一片连号 |
| 留守条目原样搬回旧值 | `_orphan_entry_lines` | 没勾 / 判不可写 / 探测异常的那几把留在旧档，与被重探那几把的新值并存 |

第一个成因里有个自我强化的细节：`taken`（不许相撞的档位集合）是从 `cfg` 读出来
的，里面塞着**这些站自己**的旧档 —— 于是每个站都躲开自己原来的值往下掉。

**语义依据**：`priority` 决定「哪一层先被尝试」，层级隔离下只取最高可用桶
（`availableAuthsFromPriorityBuckets`，`selector.go:527-553`；`priorityOrder`
降序，`scheduler.go:1229-1231`）。既有站的档位是先前一轮定下的站间次序，
**重探一次不构成改它的依据** —— 重探验证的是「这把 Key 还能不能用」，不是
「这个站该排第几」。

**做法**：`existing_host_tiers(band)` 先算出每个站在本段已占的档（取最高那一档
作锚，低档的实际不参与首选），按 host 命中的直接沿用、`priority_reason` 写
「沿用该站在本段的原档 N」；只有真正的新站才进原来的空档分配流程。唯一的例外由
`_hijacks_at` 兜：沿用原档会抢走**别人**的顶层时（成因只有一个——本次给这个站
注册了它原来没有的模型，而那个模型的现有顶层比这个站的档位低）才按新站流程
重新定档，并给一条警告。

落盘那一半在 `_orphan_entry_lines` 里修：留守条目的 `priority:` 行对齐到同站的
新值，其余字段与注释逐字保留，行尾注释换成「对齐同站档位 · 本次未重探此 Key」。
值本来就相同时不改也不报 —— 不制造无意义的 diff。

两处各有自己的测试（`test_existing_hosts_keep_their_tier` /
`test_orphan_entries_realign_priority`），而 `rehearse_real_rebuild` 的第⑤组拿
真实配置守最终不变式：**落盘后每个 `(站, 段)` 只能有一个 `priority`**，全勾与
「隔一把勾一把」两种情形都比。原文件里本来就拆开的站按最高档对齐并报出来 ——
那是本次之前留下的状态，但会让「同站同档」无从判断。

### 模型库：段级规则 + 同系列取最新 + 在线名录（2026-09-02）

现场两张截图暴露的两个问题：

- **模型没填上** —— `gemini` 段目录读不到时只填两个写死的种子，界面上是
  「站方目录也没报模型：手填模型名」的空输入框
- **勾上了低级模型** —— `codex` 段 8 个全勾，含 `gpt-4o`、`gpt-image-2`、
  `gpt-oss-120b`、`gpt-oss-20b`

根因是规则散在三处（`model_allowed`、`model_fits_section`、`web/app.js` 的
两个正则），三处判据不一致。现在单一实现在 `cpa_probe/model_catalog.py`，
前端有一份逐条等价的拷贝（浏览器跑不了 Python），`tests/test_web.py` 拿同一批
模型名喂两边比对 —— 单边改规则会被立刻抓到。

**四条段级规则**：

| 段 | 允许 |
|---|---|
| `codex-api-key` | 只 gpt 系（含 `o1` / `o3-mini` 这类推理系列） |
| `claude-api-key` | 只 claude 系 |
| `gemini-api-key` | 只 `gemini-<版本>-pro*`，且版本 **>= 2.5** |
| `openai-compatibility` | gpt / claude / gemini / kimi 四族 |

外加两条贯穿全局的：

- **每条产品线只留最高世代** —— `gpt-5.6` 出现时 `gpt-5.5` / `gpt-5.1` /
  `gpt-4o` 全部不放入；`kimi-k3` 挤掉 `kimi-k2`。同世代的所有变体都保留
  （`gpt-5.6` / `-sol` / `-luna` / `-terra` 四个都要）。

  **2026-09-02 二次修正**：原来按「系列」分组不够 —— `gpt-5.5` 的系列是
  `gpt-*`，而 luna / terra 各自是 `gpt-*-luna` / `gpt-*-terra`，三个独立系列，
  5.5 没有对手所以留下；`gpt-4o` 更直接：旧正则不认 `4o` 是版本，它自成一系
  永远保留。两件事叠加就是现场截图里 codex 段勾着 `gpt-4o` 与 `gpt-5.5` 的原因。

  现在按**产品线**分组（剥掉版本与变体后缀：`-sol` / `-luna` / `-terra` /
  `-high` / `-low` / `-preview` / `-nano` / `-32k` / `-codex` …），线内取最高
  世代。世代只比版本号前两位 —— `claude-haiku-4-5-20251001` 的日期戳不该让它比
  `claude-haiku-4-5` 更新（同一款）。整条线都认不出版本时全留
  （`gpt-oss:120b` / `gpt-oss:20b`），无从比较不淘汰。

  **2026-09-04 三次修正：`o` 系列**。现场截图里 codex 段同时勾着 `o1` 与 `o3`
  （连 `o1-pro` / `o3-mini` / `o3-pro` / `o4-mini` / `o4-mini-high` 一共七个全勾）。
  与 `gpt-4o` 那次是**同一个形态**，换了一族：版本正则要求数字前不紧贴字母
  （`(?<![A-Za-z0-9.])`），而这一族的数字紧贴开头的 `o`，于是七个名字全部解析成
  「无版本」，被「整组认不出就全留」的兜底一起收下。`o3` 是 `o1` 的后继，两代
  一起注册等于让 CPA 的轮询把请求分给旧款。

  加了一条 `_O_SERIES_RE`（`^o\d+`），产品线统一叫 `o`，于是七个名字变成
  `o` / `o-pro` 两条线各取最高世代 → `o3-pro` / `o4-mini` / `o4-mini-high`。
  锚在开头且紧跟数字，`omni-3` / `oss-20b` / `openai/...` 不受影响。

  这一改带出一处连带缺陷：`catalog_is_stale`（判「站方目录整体落后」）原来比
  两侧的**全局**最高世代，而 `o` 系列与 `gpt` 系列是**互不相干的编号体系** ——
  `o3` 的 3 不代表它比 `gpt-5.6` 老一代（两者同期）。给 `o` 系列补上版本解析后，
  「目录里只有 o 系列」的站会被误判成落后从而一个都不预勾。改成**逐产品线**比：
  只对两侧都出现的线比较，全部落后才算落后，没有可比的线就不判。后端与前端
  （`market_top_gen_lines`）用同一套数据，否则界面预勾与落盘清单再次分叉。
- **无版本号一律不收（2026-09-11 用户口径，所有类型一视同仁）** ——
  「没有版本号就等于低等级模型」。原来「整条产品线都认不出版本就全留」的兜底
  让每一个无版本号的名字（它们各自自成一条线、永远没有对手）永久保留，现场就是
  `gpt-reserve` 与 `gpt-6` / `gpt-6-astra` 一起被勾上，而该型号并不存在。
  与遗留的 `claude-fake-5` 是同一形态：名字合法（`name_is_safe` 只挡非法字符，
  挡不住「合法但不存在」），却没有任何版本信息可比。现在无条件丢弃。

  实测行为：

  | 输入 | 输出 |
  |---|---|
  | `gpt-5.4-mini, gpt-5.5, gpt-5.6, -luna, -sol, -terra, gpt-6, gpt-6-astra, gpt-reserve` | `gpt-6, gpt-6-astra` |
  | `claude-opus-5, claude-opus-4-8, claude-sonnet-5, claude-fable-5-1` | `claude-opus-5, claude-sonnet-5, claude-fable-5-1` |
  | `gemini-3.1-pro, gemini-2.5-pro` | `gemini-3.1-pro` |
  | `o1, o1-pro, o3, o3-mini, o3-pro, o4-mini, o4-mini-high` | `o3-pro, o4-mini, o4-mini-high` |

  某站全部模型都无版本号时清单会被清空，此时退到目录 / 兜底清单，界面提示手填；
  手填走 `manual` 来源，**不受本规则约束**（显式意图优先）。

- **实测清单也过「就高」闸，但既有条目在用的低世代不删** —— 四条模型来源里
  probed 原本是唯一不过闸的，于是 `_stage2` 按目录顺序补进来的旧款会与新款
  一起落盘。现在接上同一个 `newest_generation_per_line`；为防数据丢失
  （2026-09-06 第 2 号缺陷的现场：tango 的 claude 条目同时在用
  `claude-opus-5` 与 `claude-opus-4-8`），**原条目已经在用的低世代保留**，
  被丢弃的写进 `sp.warnings`，手填可恢复。

- **候选顺序按世代降序，不用目录字母序** —— 目录来自
  `parse_models_response` 的 `sorted(set(...))`（字母序），而 `_stage2` 收满
  `max_models` 就停，于是目录大的站上同世代的新变体根本轮不到被验证 ——
  这正是「勾了 `gpt-5.6` 却没勾 `gpt-5.6-sol`」的直接成因。现在用
  `rank_models` 排（六级键第一顺位就是版本降序），与「就高」同一份判据。

- **检测不到高级模型时按该系列最高级填充**（`topup_to_market_top`）——
  用户 2026-09-11 明确要求，**推翻**了 2026-09-02 定的「站方目录整体落后时
  列出但不预勾」。理由是探测本身会有 BUG，目录没报的模型实际上往往能用。
  按类型分别填充：gemini 只填带 `pro` 的最高编号；codex 填最高世代**整个系列
  的所有名字**；claude 填 `*-5` 那一级全部；`openai-compatibility`
  **允许多族并存**，每族各填各自的最高级。判据是逐产品线比世代，
  已经是最高世代的线原样不动。

- **非对话模型一律不收** —— 图像（`gpt-image-2`、`gemini-3-pro-image`）、
  语音（`-tts`）、嵌入、开源小模型（`-oss-`）、批处理（`gemini-batch-inference`）。
  它们走的不是对话协议路径，写进去 CPA 路由必失配。

### 手填走更宽的一套判据

上面那张表管的是「**工具自己**要不要挑这个模型」。操作员在结果表里手填的清单
走另一条：`section_protocol_ok`，只挡**协议层不可能成立的**。

两者唯一的差别是四族之外（`grok` / `glm` / `deepseek` / `qwen` / `llama`）：

| | 工具选型（`section_allows`） | 手填（`section_protocol_ok`） |
|---|---|---|
| 段协议不匹配（往 claude 段填 gpt） | 拒 | 拒 |
| 非对话模型（图像 / 语音 / 嵌入） | 拒 | 拒 |
| gemini 段的非 pro 档 | 拒 | 拒 |
| 四族之外，compat 段 | 拒 | **放行**，带一条警告 |
| 四族之外，前三段 | 拒 | 拒 |

**为什么必须放行**（2026-09-03 拿生产 config.yaml 核实）：compat 段走
`/chat/completions`（`openai_compat_executor.go:107`），CPA 侧对模型名零校验
（`buildOpenAICompatibilityConfigModels` 照单注册，`service_models.go:713-739`）——
能不能用只取决于上游认不认。而那份配置里 `romeo` 的 compat 段**唯一通过
端到端验证的模型就是 `grok-4.6`**（注释原文：「整个 vip 分组当前只有 grok-4.6
有渠道，已通过端到端验证的只有它」），`foxtrot` 段还有 `grok-4.6` + `glm-5.2`。

按族拒掉手填，操作员就再也没有办法把这个**已知可用**的模型写回去 —— 那一段会从
「有一个确认可用的模型」变成「只剩两个确认 503 的」。「本工具不主动推荐四族之外」
是选型偏好，把它升级成「操作员显式指定也不许」就越权了。

放行时界面会给一条警告说清「不在四族清单里、已按你的指定写入、工具没验证过」，
不让它看起来像工具推荐的。手填框旁还有一条即时提示（不用等一次 `/api/plan` 往返）——
判据同样是 `protoOk` 而不是 `famOk`，用后者会把 `grok-4.6` 标成红的，而它恰恰是该
写进去的那一个。前端 `protoOk` 与后端 `section_protocol_ok` 由 `tests/test_web.py`
拿同一批模型名喂两边比对，并写死「两者的差别只在四族之外、且只在 compat 段放开」
这条不变式。

### 目录里一个四族的都没有时，收站方自己报的

同一个形态在**目录**路径上也会出现，而且是自动路径 —— 按四族过滤后清单变空，
就落进种子兜底，写工具猜的名字。那时的选择是：

| 选项 | 后果 |
|---|---|
| 写工具猜的名字 | 这个站**从没报过**它们，CPA 路由过去大概率 404 |
| 写站方自己报的名字 | 未验证，但至少是这个站说它有的 |

后者严格更好，所以现在退这一步：目录里一个四族的都没有时，收下协议层成立的全部
名字，`model_source` 记成 `catalog`，界面与警告都说清「站方目录里没有本工具四族
清单内的任何模型，上面列的是它自己报的 —— 退这一步是因为另一个选项更糟」。

目录里**有**四族的名字时，四族之外的照旧不进清单 —— 选型偏好在有得选时仍然生效。
前三段一律按族拒（协议层就发不出去），只有 compat 段的 `/chat/completions` 真的
不限族。

**三层数据源**（探测拿不到模型时填「当前市面最新」，可信度递减）：

1. **CPA 权威名录** —— CPA 自己的 `model_updater.go` 每 3 小时拉的那份 JSON，
   `models.router-for.me/models.json` 与 GitHub 互为备份。它是 CPA **实际认识**
   的模型集合，会自动跟进新版本。缓存成功 6 小时 / 失败 10 分钟。
2. **本地 config.yaml 已有的模型名** —— 最强的本地证据。站方特供型号只在这层：
   实测 `gemini-3.1-pro-high`、`gemini-3.1-pro-preview-search`、
   `gemini-3.1-pro-preview-customtools`、`gpt-5.6` 四个都不在 CPA 名录里，
   但它们就在生产配置里跑着。
3. **内置兜底** —— 前两层都拿不到时用（国内 VPS 直连 GitHub 不通是常态）。

为什么不能只有第 3 层：写死的清单会过期，而过期的表现是「填进去的模型 CPA
每次轮到都失败」—— 与缺模型一样坏，却更难发现（界面上看着有值）。

**排序与轮转**：`newest_per_series` 只做同系列去旧，不同系列之间无从取舍，
所以取前 N 个之前还要 `rank_models`（主力款 → 变体 → 降级档）再按**产品线**
轮转。实测不轮转时 `claude` 段前四名是 `opus-5` / `opus-5-thinking` /
`opus-4-8-m-aws` / `fable-5-1` —— 四个里三个是 opus 的写法，把 sonnet 挤出去了。
`compat` 段按**族**轮转，否则前 N 全是 claude，浪费掉「一个条目转多族」这个价值。

三层齐备时实测输出：

```
codex    gpt-5-codex, gpt-5.6, gpt-5.3-codex-spark, gpt-5.6-luna, gpt-5.6-sol, gpt-5.6-terra
claude   claude-opus-5, claude-fable-5-1, claude-sonnet-5, claude-mythos-preview, …
gemini   gemini-3.1-pro, -pro-high, -pro-preview, -pro-preview-customtools, -pro-preview-search, -pro-low
compat   claude-opus-5, gpt-5-codex, kimi-k3, gemini-3.1-pro, …
```

**手填也过同一套规则**，但丢弃项会明确告知 —— 手填是用户的显式意图，悄悄改掉
比拒绝更糟。实测手填 `gpt-5.6-sol, gpt-image-2, gpt-oss-20b, gpt-5.5,
claude-opus-5` 时收下前两个合法的，并警告「手填的 3 个模型已丢弃」。

### 能力开关靠实测决定开或不开（2026-09-04）

CPA 的布尔字段不都是一类。逐个读四段的结构体（含**模型级**字段），分三类：

| 字段 | 位置 | 性质 | 本项目怎么处置 |
|---|---|---|---|
| `websockets` | codex 条目 | **上游能力** | 实测握手 |
| `support-prompt-cache-key` | compat 条目 | **上游能力** | 实测请求 |
| `models[].is-compat` | codex 模型 | **上游能力** | 只搬原值，**不探**（见下） |
| `alpha-search` | codex 条目 | 授权类（`CredentialPolicyCodexAlphaSearchV1` 按它筛凭据） | 只搬原值，**不探**（见下） |
| `rebuild-mid-system-message` | claude 条目 | 本地行为（把 role=system 消息挪到顶层 system） | carry 原文搬运 |
| `experimental-cch-signing` | claude 条目 | 本地行为（CCH 签名，CPA 自己按 upstream 决定） | 同上 |
| `cloak.strict-mode` / `cache-user-id` | claude 条目 | 本地改写行为 | 同上 |
| `disable-cooling` / `request-retry` | 四段条目 | 本地策略 | 同上 |
| `disabled` | compat 条目 | 用户显式停用 | 同上，不该被探测覆盖 |
| `models[].thinking` / `display-name` / `force-mapping` / `image` / `*-modalities` | 各段模型 | 声明与本地映射 | `existing_model_extras` 搬原值 |

**「是上游能力」与「该由探测决定」是两件事**（2026-09-05 校正）。前一张表把
它们混成一栏，于是 `is-compat` 被归进「手工字段只搬不探」——那个归类的理由
写错了：它确实是上游能力。

真正的判据是**探的代价与收益之比**：

| 字段 | 是上游能力 | 探它要付什么 | 开错的后果 | 结论 |
|---|---|---|---|---|
| `websockets` | 是 | 一次 WS 握手（不计费） | CPA 走 WS 且**不回落 HTTP**，那个凭据的 WS 请求全废 | **探** |
| `support-prompt-cache-key` | 是 | 一次带 `prompt_cache_key` 的普通请求 | 上游严格校验时**每一个**请求都失败 | **探** |
| `is-compat`（codex 段） | 是 | 要构造 MultiAgentV2 的 `agent_message` 请求体 | **当前配置下无影响**（见下） | **不探** |
| `is-compat`（claude 段） | 是 | 要先拿到一个带签名的 thinking 块再回放 —— **两轮有状态对话** | 上游拒收空签名 thinking 时，带 thinking 的请求失败 | **不探**（探不了） |
| `alpha-search` | 否（授权类） | 一次真实搜索请求到 `{base}/alpha/search`，那是**计费的业务调用** | 只是这个凭据不被 Alpha Search 端点选中 | **不探** |

`is-compat` 这个字段在**两个结构体里都有，而语义不同** —— 之前只看了 codex 那个，
结论虽然对但理由不完整：

| | codex 段（`CodexModel`） | claude 段（`ClaudeModel`） |
|---|---|---|
| 源码 | `config_types.go:539-545` | `config_types.go:443-447` |
| 作用 | 把 MultiAgentV2 的 `agent_message` 转成可移植的 Responses message | 保留**空签名**的 thinking 块，并启用 provider-aware 的签名回放 |
| 前置条件 | **要 `codex.optimize-multi-agent-v2` 也为 true** | **无条件** |
| 消费点 | `api_key_model_capabilities.go:228` | 一路进 `sanitizeClaudeMessagesForClaudeUpstreamWithDebug`（`claude_executor_execute.go:271`、`claude_executor_stream.go:263`） |
| 生产现状 | `optimize-multi-agent-v2: false` → **不生效** | 生效，但 0 个模型配了它 |

两侧「不探」的理由不同，必须分开记：

- **codex 侧是「当前不生效」** —— 探一个前置条件关着的字段，成本真实、收益为零。
  **那个开关一旦改成 true，这一行要重新评估。**
- **claude 侧是「探不了」** —— 要判断一个站接不接受空签名的 thinking 块，
  得先让上游返回一次带 thinking 的响应，再把它回放进下一次请求。那是**两轮
  有状态的对话**，而探测的每一次请求都是独立单轮。硬探只能得出不可靠结论，
  那比不探更糟 —— 本项目一直在避免「未验证当已验证」。

`alpha-search` 与这两个的不探理由又不同：那个的代价是**花钱**（真实搜索调用），
而且开错的后果轻（选不中就是没有可用凭据，不像 `websockets` 那样全废）。

**三种「不探」的理由各不相同，这件事本身值得记**：不探不等于漏了。
`websockets` 与 `support-prompt-cache-key` 之所以探，是因为「一次不计费的请求
就能判定」且「配错的后果是全废」两条同时成立。

#### `websockets` 怎么探

与 CPA 的 `dialCodexWebsocket`（`codex_websockets_connection.go:30`）同构：

- **URL**：`{base}/responses` 的 http→ws 换 scheme。与
  `buildCodexResponsesWebsocketURL`（同文件 `:223-240`）逐行对齐 —— 只换
  scheme，路径与 query 原样
- **头**：`Authorization: Bearer`，加上 CPA 无条件保证的
  `OpenAI-Beta: responses_websockets=2026-02-06`（`codex_websockets_request.go:102-105`）。
  少这个头站方可能回 400，那测出来的是「没带门票的握手不通」而不是「不支持 WS」
- 再叠加该段已学到的最小必需头 —— 门票是站的属性，WS 握手同样要过

用标准库手搓握手，不引第三方（`cpa_probe/` 全模块无第三方依赖）：只需要状态行
那一次，帧收发不需要。

**校验 `Sec-WebSocket-Accept`**：光看 101 会把「反代吞了 Upgrade、自己回个 101」
当成支持。accept 值是 `key + GUID` 的 SHA-1 base64（RFC 6455 §4.2.2），算不对
就不是真正的 WS 端点。

`need_proxy` 的段**不探**：CPA 的 WS 拨号走 `newProxyAwareWebsocketDialer` 会用
条目的 `proxy-url`，而这里直连 —— 直连拿到的 403 说明不了走代理时的行为，那是
「拿一个不成立的实验下结论」。记 `None`（未探测）而不是 `False`。

#### `support-prompt-cache-key` 怎么探

CPA 开这个开关后会往请求体注入 `prompt_cache_key`
（`openai_compat_executor.go:875` 起）。上游的反应有两种：

- 忽略未知字段 → 200，开着无害且能命中上游的前缀缓存
- 严格校验 → 400 `unrecognized request argument` 之类，开着会让**每一个**请求都失败

后者正是必须实测的理由 —— 那是一个「开了就全废」的开关。判据只看这一次请求成不
成立，不比对缓存命中：命中率要多轮同 prompt 才看得出，而那与「开关能不能开」
是两个问题。

#### 一处顺带修掉的重复键隐患

这两个字段原来在 carry 白名单**外**（走原文搬运）。改成 `render_entry` 自己写
之后，两条路同时生效会产出两行同名键。这不是「值取谁」的小问题：

```
PyYAML          取后一个（静默）
Go 的 yaml.v3   报 mapping key already defined —— CPA 起不来
```

用 Go 实跑确认过，不是推测。所以把它们移进 `_RENDERED_KEYS`，原值改由
`existing_toggles` 查表搬。

#### 三条途径都要看得见

`--no-capabilities`（CLI）/「探测能力开关」勾选框（Web）/ 单站诊断不探（它不
生成写回方案）。显示：诊断页新增「能力开关」列（三态各自措辞）、结果表处置格的
徽标（只显示确认支持的 —— 「不支持」是常态，316 行里每行都挂一个会淹掉真信息）、
事件流、导出日志、CLI 输出。

### `weight: 0` 的语义取决于 routing.strategy（2026-09-02）

本项目多处把 `weight: 0` 当「站已被逐出调度池、挡住它零代价」的**强信号**读
（定档算法据此把新站排到它们之前）。核对 CPA 源码后发现那**只在一种策略下成立**：

| 策略 | 读 weight 吗 | `weight: 0` 的效果 |
|---|---|---|
| `weighted-round-robin` | 是 | `positiveWeightAuths` 整个剔除（`selector.go:650` → `637-644`） |
| `round-robin`（**默认**） | 否 | 照常参与轮询（`selector.go:589`） |
| `fill-first` | 否 | 照常参与轮询（`selector.go:787`） |

没配 `routing.strategy` 的部署走默认 round-robin —— 此时把零权重站当死站会让
定档以为「挡住它没代价」，从而把新站插到一批**其实在服务**的站之前。方向正是
本项目反复强调的更坏那个：把活站当死站。

`weight_zero_excludes(cfg)` 按配置判断策略（认 `weighted-round-robin` /
`weightedroundrobin` / `wrr` 三种拼法，大小写与空格不敏感，与
`service_config.go:42-47` 一致），`build_band` 据它决定要不要把零权重站计入
`dead_hosts`。界面上那句提示也跟着分岔 —— 非 wrr 策略下说「当前策略不读 weight，
仍参与轮询」，并说明 CPAMP 面板的「未启用」是按 weight 判的、与实际调度不一致。

当前部署实测配的是 `weighted-round-robin`，所以旧行为一直是对的；但那是配置的
巧合，不是代码的保证。

### 规则收紧不许把段变成「勾不上」（2026-09-02）

新规则上线后自查抓到两处「合理代码合起来出错」：

- `elif v.catalog:` 只判目录**非空**。一个只报 `flash` / `oss` / `grok` 的站，
  过滤后是空列表 → `models=[]` → `writable=False` → **那个段连勾选框都点不动**,
  正是 2026-09-01 修过的「判死段勾不上」被新规则重新引入。改成「目录为空**或**
  过滤后为空」都落到市面最新清单。
- 手填的**全部**不合规时 `forced_models` 变空 → `if forced_models and not
  v.usable` 为假 → 落到 seed 分支 → `model_source` 是 `seed` 而非 `manual`，
  于是那条「已丢弃」警告永远不触发。用户手填了两个、一个都没进去、界面上一句
  提示都没有。警告挪出 manual 分支，无条件报，并额外说明「已改用市面最新清单」。

### 无目录时后端填的模型必须显示出来（2026-09-02）

现场截图：一个站 gemini 段「可信模型」那一格**完全空白**，只有一个提示手填的
输入框。而同一行下方的橙色警告里写着「模型清单取自『当前市面最新』（CPA 权威
名录 6 个 + 本地 config.yaml 8 个）：gemini-3.1-pro, gemini-3.1-pro-high, …」——
**后端有 6 个模型，前端一个都没显示**。

根因：目录为空时前端走 `cat.length ? … : …` 的 else 分支，只渲染一个空的手填框，
它从 `S.forced` 取值而那里此刻是空的。方案里的 `sp.models` 从来没被显示，也没写进
`S.forced` —— **而提交时读的正是 `S.forced`**。所以那个段勾上也写不进任何模型，
用户的判断是对的。

修法：else 分支留一个空容器 `.cats.fallback`，`refreshPlan` 拿后端方案的
`sp.models` 填成勾选框、默认全勾、并**立刻回写 `S.forced`**（提交读的是它，
不读 DOM）。全选 / 反选 / 清空按钮跟着出现。容器已填过就不再重填 —— 否则每次
`refreshPlan` 都会把用户的取消勾选覆盖掉。placeholder 也改成「还可手填目录外的
模型名（上面那批已按市面最新填好）」，不再误导成「什么都没有，请自己填」。

### 兜底必须放在所有分支之外（2026-09-02，第三次修同一处）

现场截图里某站 compat 段四个标记连起来是「**可用 + 实测 + 无可信模型 +
不可写入**」。`_accept()` 在静默换模或「200 包错误体」时拒收模型，段仍算可用
（端点确实响应、凭证有效），但清单一个都没进。

而前两版把兜底写在 `else:` 里：

```python
elif v.usable:
    models = list(v.models)   # ← 空列表也照抄，不进兜底
else:
    if v.catalog: ...
    if not models: ...        # 兜底只在这里
```

**判据必须是「清单空不空」，不是「走了哪条分支」** —— 用户的要求是「实测不可用
就填充成对应类型的最高级别模型」。现在兜底移到所有分支之外，三条入口（单站诊断 /
增量导入 / 全量重探）实测都填 6 个。

两种处境的措辞也分开写：`usable=True` 那种说「端点响应正常、凭证有效，但每次
返回的模型都与请求不一致（静默换模或 200 包错误体）」，不再和「探测未通过」混同。

### 站方目录整体落后时列出但不预勾（2026-09-02）

某站 codex 段目录只有 `gpt-4` / `gpt-4-32k` / `gpt-4o` / `gpt-4o-mini` ——
**四个都是世代 (4,0)**，于是「同产品线取最高世代」把四个全留下还默认全勾。
规则本身没错（那条线里 (4,0) 就是最高），但违反「最新是 gpt-5.6 时 gpt-4o
不该默认勾选」的意图。

**没有改用市面最新清单**：那个站的目录里确实没有 5.6 系的名字，写进去 CPA 路由
不到，把「有老模型可用」变成死条目 —— 比默认勾错更糟。所以只降级默认勾选：
清单照旧列出（确知可用可手工勾），`recommended` 为假，界面说明「整份目录都落后于
市面最新（本段最新已到 5.6）」。

判据是**世代**而非名字 —— 站方特供型号不在市面名录里，按名字比会把它误判成落后。
`/api/context` 回 `market_top_gen`，因为结果表在勾选**之前**就渲染了，那时还没有
`/api/plan` 的响应，两边都要能判。

**2026-09-04 改成逐产品线比**：给 `o` 系列补上版本解析后，全局比较立刻出错 ——
`o3-mini` 的世代是 (3,0)，而 codex 段的市面最新是 `gpt-5.6-sol` 的 (5,6)，两个数字
来自互不相干的编号体系。按全局比会把「目录里只有 o 系列」的站判成落后、一个都不
预勾。现在只对**两侧都出现**的产品线比较最高世代，全部落后才算落后；目录里的线
在市面清单里没有对应时无从比较，不判落后 —— 与「整组认不出版本就全留」同一条原则。
`/api/context` 因此多回一个 `market_top_gen_lines`（逐线版本），前端用它，
不再用全局那个数。

### 限频阈值自动学习（2026-09-02）

一个站在 79 凭据那轮里 **46 次**撞上 `bulk probe guard`，判定「限频」→ 处置写着
「加大探测间隔重试」→ 然后**什么都没做**，接着用同样的节奏打下一个模型。

那句正文里带着确切阈值：

```
bulk probe guard: ip 1.2.3.4 requested 4 distinct models in 60s
```

工具读得出来却没用上，等于让用户去看日志、猜一个 `--gap`、再重跑十几分钟。

现在 `_note_rate_limit` 从正文解析 `N 个模型 / M 秒`，算出平均间隔 × 1.1（滑动
窗口，贴着阈值走仍会偶发命中），对该站生效并**整站合用一个 gap 桶** —— 这类 guard
按账号/IP 全局计数，按 (host, section) 分桶会让瞬时并发变成 4 倍，那正是 46 次
全撞的原因。只收紧不放松（更松的响应不覆盖已学到的严格值），荒谬窗口钳在 60 秒。

**为什么换代理不是解法**：站方数的是「这个 IP 在 60 秒里问了几个不同模型」。
换到代理出口后同样的节奏立刻又触发一次，只是白烧一次请求并把代理 IP 也搭进去。
`_PROXY_POINTLESS` 里「限频」那条早就写着「探测节奏问题，加大 gap 而非换 IP」。

### 上下文上限的下限校验（2026-09-02）

实测日志里一个 compat 站三个条目都拿到 `max-context-length: 10`。CPA 把它直接当
`context_window` / `max_context_window` 报给客户端
（`internal/client/codex/models/models.go:206-211`）—— **10 个 token 的窗口**，
那个站每一次请求都立刻超限。

根因：`_bisect` 的截断判据是 `tok < chars * 0.5` 就把 `tok` 当真实容量，而上游
回 `input_tokens: 10`（根本没统计）时，10 就成了「实测容量」。

加了下限 `_MIN_TRUSTED_CONTEXT = 8000`：比任何真实模型的窗口都小一个数量级
（现役最小的也有 200k），不会误伤真实小窗口站；低于它就当「测不出」并发
`context-untrusted` 事件说明 —— 缺这个字段 CPA 会回落内置目录值，比写一个
荒谬的数安全得多。

### proxy-url 搬运必须按段（2026-09-02）

`existing_proxies` 原来按 `(host, api_key)` 索引，跨段共用一个值。实测
`kilo.example` 的 5 把 Key 在 compat 段有 `proxy-url: http://mihomo:7890`，
在 claude 段**故意没有** —— 那个站的 claude 路径直连可用，走代理反而多一跳。

按两元组搬运会把 compat 的代理灌进 claude 段，实测 claude 段 `proxy-url`
从 3 条涨到 8 条。多一跳不会让请求失败，所以 `validate` 与写后验证都发现不了。
键改成 `(段, host, api_key)` 后前后各段完全一致。

### 重探时每个字段以哪一侧为准（2026-09-04）

「全量重探会更新什么」以前只有一句「headers/代理/优先级/前缀全部更新」，
而实现里各字段的处置**本来就不一致**（`proxy-url` 是「探测有值优先、否则搬
原值」，不是整字段替换）。文档与代码不一致时人会按文档做决定，所以把判据
逐字段写清。

判据只有一条：**这个字段是谁的属性**。

| 字段 | 属性归属 | 处置 | 理由 |
|---|---|---|---|
| `models` | 站 × Key × 本次探测 | **实测替换** | 清单就是这次要更新的东西 |
| `priority` | 站间次序 | **沿用原档** | 重探验证的是「这把 Key 还能不能用」，不是「这个站该排第几」。只有新站才定档 |
| `proxy-url` | 站 × 段 | 探测有值优先，否则搬原值 | 重探时这个站可能直连就通，方案里为空 —— 抹掉会让必须走代理的站下次直连拿 403 |
| `headers` | 站 × 段 | **合并**：原值为底，探测值覆盖同名键 | 见下 |
| `weight` | 用户显式意图 | 只搬原值，探测不产生 | `weight: 0` 是「逐出调度池」的唯一表达 |
| `prefix` | 用户显式意图 | 只搬原值 | `dominant_prefix` 只是给**新条目**猜的默认值 |
| compat 的 `name` | CPA 的 provider 身份 | 只搬原值 | 改名作废冷却状态与能力缓存 |
| `websockets` / `support-prompt-cache-key` | **上游能力** | 实测三态，见下 |
| `max-context-length`（模型级） | 站 × 模型 | 本次实测 > 原值搬运 > 不写 | 不把 A 的窗口外推给 B |
| `models[].alias` 等模型级字段 | 用户显式意图 | 只搬原值 | 段级兼容名是人配的 |
| 白名单外字段（`request-scoped-errors` / `fingerprint-profile` / `cloak` …） | 本地策略 | carry 原文逐字搬 | 探测问不出来，也不该由探测决定 |

#### headers 为什么是「合并」而不是「整字段替换」

探测能产出的 `anthropic-beta` 是画像梯里写死的常量清单，里面**永远没有**两项：

- `oauth-2025-04-20` —— `profiles.py` 有意去掉：api-key 探测不该声称走 oauth
- `context-1m-2025-08-07` —— 只由 `betas.py` 在站方正文点名时才补

整字段替换会把原条目里手工配的能力 beta 静默抹掉。实测 Desktop 版那份配置：
含这两个 beta 的条目 **33** 个，其中 `alfa.example` 的 claude 条目 headers
**只有** `anthropic-beta: context-1m-2025-08-07`、没有 UA —— baseline 一通过
`need_ua=False`，`sp.headers` 是空 dict，那个站的 1m 上下文直接被关掉。

所以：原值在下、探测值覆盖同名键。`anthropic-beta` 特殊处理 —— 它是逗号分隔的
**集合**，同名覆盖会丢项，走 `betas.merge` 保序去重合并。

#### 能力开关为什么是「实测优先」，方向与 headers 相反

`websockets` 与 `support-prompt-cache-key` 的处置是**实测覆盖原值**，包括
「实测不支持时把原来开着的关掉」。这与 headers 的合并方向相反，理由是后果不对称：

| 配错方向 | 后果 |
|---|---|
| `websockets: true` 而站方不支持 | CPA 走 WS 通道且**不回落 HTTP**（`CodexAutoExecutor` 只按下游形态与该开关分流，`codex_websockets_executor.go:71-77`）—— 那个凭据的 WS 请求全废 |
| `websockets: false` 而站方支持 | 只是用不上 WS，无害 |
| headers 多带几个能力 beta | 站方多数忽略未知 beta |
| headers 少了能力 beta | 那项能力关掉（1m 上下文变 200k） |

两边都是「往安全的那一侧偏」，只是安全的方向不同：能力开关的安全侧是**关**，
headers 的安全侧是**多带**。

三态而不是布尔：

| 实测结论 | 写回 | 界面措辞 |
|---|---|---|
| `True` | 写 `<字段>: true` | 「支持」+ 实测依据 |
| `False` | **不写**（CPA 零值即关闭），原值即使是 true 也不搬 | 「实测不支持」+ 返回码 |
| `None`（未探测） | 不写，但原值为 true 时照原值搬 | 「未探测」+ 为什么没探 |

`False` 与 `None` 写回时行为相同，但界面措辞必须分开 —— 把「探过、站方明确
拒绝」和「没探过」显示成一个样子，就是本项目反复修的那类「未验证当已验证」
的镜像。

`None` 的三个来源：关掉了 `--no-capabilities` / 该段本次判不可用 / 该段需走
代理（本探测是直连，直连的握手结果说明不了走代理时的行为 —— 那是「拿一个
不成立的实验下结论」）。

### 段族过滤（2026-09-02）

**问题**：聚合站的 `/models` 目录把三族模型混在一起报。探测队列原样接收，
于是 gemini 段拿 `claude-opus-4-6` 去打 `/v1beta/models/claude-opus-4-6:generateContent`
—— CPA 永远不会这样发，段决定协议路径，模型必须是该协议下的模型。

**量化**（79 凭据实跑日志）：

| | 请求数 | 成功率 |
|---|---|---|
| 跨族 | 435 / 773（**56%**） | 5.5% |
| 同族 | 338 | 24.6% |

跨族请求里 **240 次返回 500**（占跨族 55%），而同族一次 500 都没有。500 被判
「临时」触发重试，日志里 666 次 500 的最大头就是这么来的。除了白烧配额，
反复拿协议不匹配的模型名轰炸正是站方风控盯的形态。

**解法**：`SECTION_FAMILY` 闸，三个协议段只探本族（gemini→gemini、codex→gpt、
claude→claude）。`compat` 段不设限 —— 它走 `/chat/completions`，本身就是万能
转发口，三族都合法。过滤在三处（探测队列、目录落盘、方案生成）各做一次，
纵深防御。

### 目录优先（2026-09-02）

**问题**：原来的顺序是「拿写死的种子模型撞 → 撞不上判死」，而 `/models` 目录
在 `_stage2`，只在 `_stage1` 已经成功时才跑 —— 从头到尾没问过站方「你到底有
什么」。实测 45/79 个凭据判 0 段可用，其中 7 个的目录明明拿得到模型（最多 199 个）。

**解法**：`_stage0_catalog` 提到最前，用目录里真实存在的模型开打，种子只作兜底。
GET 目录端点多数站不计费、不计入调用统计、不触发风控 —— 与「严禁简单测活」
同向。CPAMP 的健康检查也只发 GET 目录（全库无 POST 测活）。

gemini 段的 `/v1beta/models` 分页，最多翻 20 页（与 CPAMP 同一上限）。

### 判死的段也给完整参数（2026-09-02）

**问题**：判死的段不生成方案，界面上勾选框灰着、priority 显示「待定」。而很多
中转站禁止测活却确实可用 —— 那样等于把可用站扔掉。

**解法**：每段都算出确定参数，并标注模型清单的来源：

| 标记 | 含义 | 默认勾选 |
|---|---|---|
| 实测 | 发过请求跑通 | ✓ |
| 目录 | 站方 `/models` 声明 | ✗ |
| 手填 | 操作员填的 | ✗ |
| 猜测 | 种子兜底，最不可信 | ✗ |

`priority` / `headers` / `proxy-url` / 指纹 / 上下文上限全部走与可用段同一套算法。
**写进 config.yaml 的参数不会有未定项** —— 缺席比填错更难排查。

### 未知字段搬运（2026-09-02）

`render_entry` 是白名单式渲染，只写它认识的 12 个字段。而全量重探用它**整段重写**
—— 生产配置 121 个条目里 **117 条**带白名单外的字段，重写后全部静默消失
（`validate()` 报成功、YAML 合法，只是行为变了）：

```
request-scoped-errors  116 条   冷却规则，丢了欠费的 Key 留在轮询池
fingerprint-profile      1 条   让 CPA 自己补设备指纹
```

2026-09-04 重新点过一遍这张表，改掉三处不实：

| 原来写的 | 实际 |
|---|---|
| `excluded-models` 39 条 | **0 条**。两份生产配置里它只出现在**注释**（`# excluded-models 可选，屏蔽指定模型`），没有一个真实条目用它 |
| `disabled` 1 条 | **0 条**。compat 段没有任何 `disabled: true` |
| `proxy-url` 24 条 | 26 条（Desktop）/ 16 条（fsdownload），但它**在白名单里**（`render_entry` 自己写、`existing_proxies` 搬原值），不属于 carry |

前两处的成因是「按 `grep -c excluded-models` 数」—— 那把注释也数进去了。
这类计数从此都按解析后的 YAML 数，不按行数。

`websockets` 与 `support-prompt-cache-key` **2026-09-04 从 carry 移出去了** ——
它们现在由实测决定（见「能力开关靠实测决定开或不开」），原值走
`existing_toggles` 查表。两条路同时生效会写出重复键，而 Go 的 `yaml.v3` 对
重复键直接报 `mapping key already defined` —— CPA 起不来。

`extract_carry_lines()` 按**原文行**搬运 —— 这些字段结构任意深，重新序列化要
处理缩进、引号风格、键序，而原文行拿来就用、逐字保真。索引键是
`host\x00api-key`：同一个站的多个 Key 里只有一个带 `fingerprint-profile` 时，
按 host 索引会让另两个也被染上。

### 画像系统（2026-09-01）

**问题**：中转站按客户端身份门禁。探测发 `x-api-key`+`content-type` 被拒 503，
真实 Claude Code 发完整 CC 头集能通 —— 同一站、同一 Key，成败取决于下游客户端。

**解法**：25 档画像梯 × 4 段，按客户端族分组（cc / codex / gemini-cli / openai-sdk / browser），
族内嵌套超集（后档 = 前档 + 新项）。baseline 失败后逐档升级，第一个通的即最小必需画像。

```
claude 段梯子（8 档）：
  baseline → cc-min → cc-std → cc-full → 
  [cc-body-json / cc-body-plain / cc-body-system] → browser-ua

codex 段梯子（6 档）：
  baseline → originator-only → [codex-tui / codex-vscode] → codex-full → browser-ua
```

**headers + body_patch 双层**：`metadata.user_id` 是请求体字段，旧 `identity_combos`
只返回 headers 表达不了。新 `Profile` 同时给 headers 与 body 补丁，按 Key 求值
模板变量（`{key_hash}` / `{uuid1..3}`）—— 静态 user_id 会让所有 Key 共用一个假身份。

**实测依据**：
- golf claude 段：baseline 401 → cc-std（user-agent + anthropic-beta + x-app）200
- zulu claude 段：cc-min 503 → cc-std 502（门禁通过，站方自己上游挂了）
- golf codex 段：baseline 401 → originator-only 200

画像写进 **config.yaml 条目 headers**（四段通用、与下游客户端无关、优先级最高）。
`fingerprint-profile` 只在站方要请求体字段时用（目前 13 站里只有 zulu 一个）。

### 新增两类：时段与 WAF

**时段类（usable=True）**：分组按窗口开放。实测 hotel codex 段 `09:00~18:00`
—— 窗口外打凭据有效但站方拒绝，报 403 + 提示「限时段」；窗口内可用。

**WAF 类（usable=False）**：站方 WAF 按形态拦。实测 hotel 三段带代理仍被拦
`访问已被拦截` —— 换 IP 无效，不是封 IP，是请求形态问题。

时段类立即收敛（不重试），导入时标注窗口并提示手工复测。

### 前缀规整（站级前缀 + 段级别名）

**问题**：65 个 claude 条目都是 `ANT`、13 个 compat provider 都是 `CHMA`。
`rewriteModelForAuth` 只判「模型名是否以这个 prefix 开头」—— `ANT/claude-opus-5`
同时命中全部 12 个 claude 站，落到哪个由 priority + 加权轮询决定。「指名某站」
这个能力事实上不存在。

**解法**：14 站各自独占前缀（从域名生成，`api.alpharelay.com` → `ALP`、
`betagate.cc` → `BET`），保留原名轮询，用 `models[].alias` 补段级兼容名。

```
你想要的                        用哪个名字                  效果
让 CPA 自动挑最好的站          claude-opus-5（原名）       按 priority 轮询全部 12 站
指名 alpharelay                ALP/claude-opus-5           只落 alpharelay 那 7 条
指名 betagate                  BET/claude-opus-5           只落 betagate 那 5 条
兼容旧客户端（用 ANT/...）     ANT/claude-opus-5（别名）   同原名轮询
```

三个名字同一条链路、同一套 headers、同一个成功率。段级别名可选（`--keep-section-prefix`），
默认补上，config 变长但不破坏现有客户端。站级前缀自动分配、幂等、沿用你手工定的值。

### 界面：进度与勾选

**进度**（步骤 ②）除了「已完成 / 总数」还给三样：

- **剩余时间区间** —— 点值用全量累计均值，区间用经验分位（p25 / p99）。跨 12 组
  随机种子回放：平均误差 30-67%、区间命中 94-97%。
- **吞吐率** —— 每分钟完成几个，实测量不是外推。
- **在飞跟踪** —— 还有几个在跑、最早开始的那个已跑多久、是哪个站。

ETA 只在**并发 ≤ 4** 时给。并发 30 时区间命中率只有 9% —— 剩余墙钟被「在飞
最长的那个还需多久」主导（进度 40/79 时占比 100%），而那个值在它结束前无法从
已完成的样本推出。这不是算法不够好，是信息不在样本里。超过阈值就只给吞吐率
与在飞跟踪，并在界面上说明原因。

**勾选**（步骤 ③）：

- `全勾选` 不看判定状态 —— 很多站禁止测活却可用，按判定筛等于把它们扔掉。
  唯一跳过的是 `duplicate`（撞已有 Key，写进去是重复条目）。
- 三个预设按钮有选中态，`aria-pressed` 同步。
- 目录模型给可勾选清单，按段默认预勾同族：codex / compat 预勾 `gpt-*`、
  claude 预勾 `claude-*`、gemini 优先 `gemini-*-pro`。另有 全选 / 反选 / 清空。
- **一键导出** txt 到浏览器下载目录。含请求指纹、最小门票头、body 补丁形态、
  需代理、可调用时段、上下文上限及其实测模型。api-key 只出末四位。

**方案页**（步骤 ④）顶部显示**定档提示**：整批下移、越过现有档位、压到最低值 1、
手工改出的同层。这些说的是站与站的相对关系，不属于任何单个段，所以只在这里给
一处 —— 段级警告仍在结果表每段自己的展开行里。

---

## 目录

```
cpa-upstream-importer/
├ cpa_probe/          共享判定库（16 模块，无第三方依赖，PyYAML 可选）
│  ├ parse.py         解析 url,key；按段规范化 base-url
│  ├ classify.py      响应定性：余额/封号/限流/门禁/IP封/反测活/死路/临时/注入/时段/WAF
│  ├ request.py       按段构造请求，路径与 CPA executor 对齐（gemini 用 x-goog-api-key 头 / claude 带 ?beta=true）
│  ├ client.py        统一 HTTP 传输（原三脚本用了三种底层）
│  ├ fingerprint.py   后端 id 指纹、静默换模判定、截断校验
│  ├ profiles.py      25 档画像梯 × 4 段，按客户端族分组，族内嵌套超集，headers + body_patch 双层
│  ├ prefixes.py      站级前缀自动分配（从域名生成、幂等、尊重手工值）、段级别名补充
│  ├ model_catalog.py 段级模型规则、同系列取最新、CPA 权威名录（三层兜底）
│  ├ pipeline.py      四阶段探测编排 · 段/候选并行 · single-flight 形态复用 · 画像升级
│  ├ plan.py          去重、priority 定档（单站上限 + 批量站级分配，读 weight:0 与实测注释）、影响面
│  ├ writeback.py     行级 YAML 编辑（保注释）、备份、diff、重载 CPA + 读回校验
│  ├ batch.py        站级并行探测、CarryTables（八张「必须原样搬」的查表）
│  ├ betas.py        anthropic-beta 集合合并（逗号分隔，保序去重）
│  ├ resources.py    并发数按 cgroup 实测推荐
│  └ cpa_source_probe.py  从 CPA 源码读权威模型名录
├ server.py           HTTP 服务（标准库 + PyYAML + bcrypt，后两个是硬依赖）
├ cli.py              命令行入口
├ web/                前端：index.html + app.js
├ docker-compose.yml  独立部署模板（全部走 .env 变量，零硬编码）
├ .env.example        环境变量样例（CONFIG_PATH 必填）
├ .github/workflows/  CI：3 个 Python 版本跑测试 + 多架构镜像发布
├ LICENSE             MIT
├ CONTRIBUTING.md     贡献指南
├ tests/              回归测试（九个套件 1196 项，零外网请求，自带最小样本）
│  ├ run.py           跑全部，退出码 0/1，可接 CI。传 config.yaml 路径可加跑真实用例
│  ├ fixture_cfg.py   自带的最小 config.yaml（各套件共用；不传路径时就用它）
│  ├ test_probe.py    解析/判定/指纹/去重/定档/影响面/写回
│  ├ test_server.py   HTTP 契约：鉴权/封锁/静态/路径穿越/写回闸门/非 ASCII 密码
│  ├ test_pipeline.py 假上游端到端：四阶段编排 + 段族过滤 + 二级代理 + 事件流
│  ├ test_edges.py    写回边界 12 形状：同站 100 Key / 撞已有站 / prefix
│  ├ test_reload.py   生效链：inode 恒定 / 读回校验 / 403-CF 与状态码分类
│  ├ test_speed.py    并发正确性：节流分桶 / single-flight / 缓存失效
│  ├ test_web.py      前后端静态契约：DOM id / 事件 / 重试 / 地址 / 验证不跳过
│  ├ test_tiering.py  定档算法：分数生效 / 死站零代价 / 别名表 / 逐轮自查缺陷
│  ├ test_full_redetect.py  全量重探：去重 / ETA / 未知字段与 proxy 搬运 / 注释保全
│  └ rehearse_real_rebuild.py  拿**真实** config.yaml 演练重建并逐项对账
│                              （不进 run.py：要真实文件才有意义）
├ tools/
│  ├ recheck.py       复核**既有**凭据：按各站声明的模型 + CPA 的真实转发头
│  ├ diag403.py       403 结构性诊断：预算 vs 顶层池、单点、档位落差
│  ├ rehearse.py      整链演练（假 CPA，零外网）
│  └ export-logs.sh  导出 CPA 错误日志，打包前强制脱敏
├ deploy/
│  ├ preflight.sh     部署前自检（只读，不改任何东西）
│  ├ install.sh       systemd 一键安装（幂等，可重复跑）
│  ├ upstream-importer.service   systemd 单元
│  ├ Dockerfile       python:3.12-slim + PyYAML + bcrypt
│  └ nginx-snippet.conf   外网访问必须经它加 TLS
├ docs/
│  ├ tutorial.html    图解教程 10 章（单文件，零外链，可直接打开）
│  └ upstream-import-spec.md   设计文档 18 节
│    （另有 cpa-atlas.html —— 本站排障全记录，含真实站点结论，
│     已在 .gitignore 里，不进公开仓库；零代码依赖它）
└ legacy/             原探测脚本，原样保留可继续单独使用（**判据是旧的**，见下）
   ├ audit-upstreams.py    逐组合可用性审计 · HTML 报告
   ├ probe-fix.py          四问诊断：基线→换模→最小必需头→代理
   ├ context-probe.py      二分探测上下文上限
   ├ swap-watch.py         换模率采样
   ├ probe-upstreams.py    mihomo 节点探测 · AUTO 组 filter 回写
   └ logs-digest.sh        错误日志一行摘要
```

`config.yaml` / `docker-compose.yml` / `.env` / `nginx.conf` / `mihomo/` **仍在 `/opt/deploy` 根**——
`docker-compose.yml` 里有 `./config.yaml`、`./mihomo`、`.env` 三处相对挂载，移动会破坏部署。

### `legacy/` 里那 2523 行用的是旧判据

它们是本工具的前身（三个独立脚本），原样留着是因为「单独跑一个站看它要什么头」
这件事它们做得直接。但**判据没有跟着主流程更新**，两处已经分叉：

| | `legacy/` | 主流程 |
|---|---|---|
| 客户端画像 | `audit-upstreams.py` 自己的 `identity_headers()`，一套固定头集 | `profiles.py` 的 25 档梯子 × 4 段，族内嵌套超集 |
| HTTP 底层 | 三个脚本三种（`urllib.request` / `subprocess curl` / …） | `client.py` 统一 |
| gemini 段鉴权 | `?key=` 拼在 URL 上 | `x-goog-api-key` 头 —— CPA 全库无 `?key=`（`gemini_executor.go:90/190/…`） |
| 探测文本 | `"hi"`（`audit-upstreams.py` 3 处、`swap-watch.py` 2 处） | 88 字符的技术问句 |

后两行是**会改变结论**的分叉，不只是实现差异：

- `?key=` 那种形态实测被一批站直接拒（前端用头能拉到几百个模型、用 query 拿 000），
  所以 legacy 会把好站报成坏站
- `"hi"` 正是站方反测活规则最先拦的形态。主流程有一道测试
  （`test_probe_text_not_trivial`）钉住这件事，但它**只扫 `request.py` 与
  `pipeline.py`**，扫不到 `legacy/` —— 那 5 处 `"hi"` 一直在

所以：**拿 legacy 的结论去改配置会与主流程不一致，且它自己有封号风险**。它们适合
「看一眼这个站返回什么原始正文」，不适合当判据。要判据就跑 `python3 cli.py` 或网页端的
单站诊断 —— 那两条路与写回同一套规则。

---

## 快速开始

### 第 0 步 · 自检（必做，只读）

```bash
cd /opt/deploy/cpa-upstream-importer
bash deploy/preflight.sh
```

八项检查：运行环境（含 bcrypt）、文件完整性、语法编译、`config.yaml` 结构与档位谱、
主栈 compose 集成、端口占用、全量回归测试、网络出口。**不改任何文件、不发外网 API 请求。**
失败即退出码 1，并打印实际值 —— 那个值就是线索。

通过后它会把下一步命令直接打出来。

### 第 1 步 · 起容器（网页操作，日常推荐）

服务已并入主栈 `/opt/deploy/docker-compose.yml`，与 `cli-proxy-api`、`cpa-manager-plus` 并列。

**先把代码传到 VPS**（在**你自己的电脑**上跑）：

```bash
# 整个目录传过去。-r 必须有 —— 里面有 cpa_probe/、web/、deploy/、tests/ 四个子目录
scp -r cpa-upstream-importer root@<你的VPS-IP>:/opt/deploy/

# docker-compose.yml 也要传（cpa-upstream-importer 那段服务定义在里面）
scp docker-compose.yml root@<你的VPS-IP>:/opt/deploy/
```

> `<你的VPS-IP>` 换成真实地址，**尖括号要去掉**。
> 传完可以核对一下：`ssh root@<VPS> 'ls /opt/deploy/cpa-upstream-importer'`
> 应看到 `cpa_probe deploy docs tests tools web README.md cli.py server.py docker-compose.yml`。

然后在 **VPS** 上：

```bash
cd /opt/deploy

# ① 确认 uid 与 config.yaml 属主一致（不一致则容器写回时 PermissionError）
stat -c '%u:%g' config.yaml            # 通常 0:0
# 不是 0:0 就写进 .env：
#   stat -c 'IMPORTER_UID=%u' config.yaml >> .env
#   stat -c 'IMPORTER_GID=%g' config.yaml >> .env

# ② 可选：固定 token（不固定也能用 —— 直接拿 CPA 后台密码登录）
echo "IMPORTER_TOKEN=$(openssl rand -hex 20)" >> .env

# ③ 构建并启动
docker compose build cpa-upstream-importer
# up -d 立刻返回，但服务写 stdout 要一两秒 —— 直接 logs 会看到空白。
# 两条连写，中间等 3 秒：
docker compose up -d cpa-upstream-importer && sleep 3 && docker compose logs cpa-upstream-importer
```

日志里会有一行 `打开 : http://0.0.0.0:8765/?token=...`。
**容器内绑 `0.0.0.0` 是对的** —— 端口只发布到宿主 `127.0.0.1`（见 compose 的 `ports`），
外网扫不到。

### 第 2 步 · 从本机打开页面

服务只监听 VPS 的 `127.0.0.1:8765`，**从外面根本连不上** —— 这是刻意的，
它持有明文上游 Key 且能改写 `config.yaml`。两条路进去：

#### 方案 A · SSH 隧道（推荐，零配置）

⚠️ **这条在你自己的电脑上跑，不是在 VPS 上。**

```bash
# 本机 PowerShell 或终端。203.0.113.7 是 RFC 5737 文档专用地址，
# 换成你自己 VPS 的 IP。别写 <你的VPS-IP> 这种占位符 ——
# 尖括号是 bash 的重定向符，照抄会报 syntax error near unexpected token
ssh -N -L 8765:127.0.0.1:8765 root@203.0.113.7
```

**这条命令在做什么**：把 VPS 上的 `127.0.0.1:8765` 映射到你电脑的
`127.0.0.1:8765`，数据走 SSH 加密通道。公网上不存在这个端口 —— 扫不到、
爆破不了。相当于给自己开了一条临时专线。

两个容易踩的点：

- `-N` 表示只做端口转发、不开 shell，所以**这条会占住终端不返回**。
  那是正常的 —— 另开一个窗口用浏览器，用完回来 Ctrl-C 断开。
- 别把 `<VPS>` 这种占位符照抄进去，尖括号是 bash 的重定向符，
  会报 `syntax error near unexpected token`。

然后浏览器打开 `http://127.0.0.1:8765/`。

#### 方案 B · nginx 反代到独立域名

想直接输网址、或要在手机上用，就配反代 —— 见 `deploy/nginx-snippet.conf`。

**但有一条实测结论必须先知道：不能加 basic auth。**

本部署的 `nginx.conf` 在 `cpas.example.com` 那段已记过这个坑（2026-08-26 实测）：
basic auth 与 Bearer **抢同一个 `Authorization` 头**。投喂台前端
（`web/app.js:66`）发的是 `Authorization: Bearer <token>`，会覆盖浏览器为
basic auth 发的 `Authorization: Basic ...`。结果是**页面能打开、但所有 API 请求 401**，
界面永远卡在加载状态。

所以这一层只能用不占用该头的方式：**nginx IP 白名单**（`allow`/`deny`）、
**限速**（`limit_req`），或 Cloudflare Access（走 cookie）。

白名单最严，但出口 IP 一变就得改配置 —— 在外面临时要用时很麻烦。如果你要
「随时能开网页输密钥」，那就只剩限速这条路：

```nginx
# http 段
limit_req_zone $binary_remote_addr zone=importer_auth:10m rate=12r/m;
limit_req_zone $binary_remote_addr zone=importer_all:10m rate=240r/m;
limit_conn_zone $binary_remote_addr zone=importer_conn:10m;
# 回 429 而不是默认的 503 —— 前端能区分「你太快了」与「服务挂了」，
# 而 503 会让人以为是上游站点的问题（那是这个工具最常见的报错类型）
limit_req_status 429;
limit_conn_status 429;

# server 段
limit_conn importer_conn 8;          # 探测只用一条轮询连接，正常用不到 8

# 登录与凭据校验：慢速档。/api/context 是登录后第一个被调的端点，
# 撞密码必然经过它。burst 允许「输错一次再输一次」，nodelay 让这几次
# 立刻放过而不是排队 —— 排队会让正常用户觉得卡。
location = /api/context { limit_req zone=importer_auth burst=6 nodelay; proxy_pass http://127.0.0.1:8765; }
location = /api/apply   { limit_req zone=importer_auth burst=3 nodelay; proxy_pass http://127.0.0.1:8765; }
# 轮询是 1.5 秒一次，跑满 5 分钟约 200 次 —— 240r/m 够用；
# burst 给到 60 是为了首屏那批静态资源不被拦
location /             { limit_req zone=importer_all  burst=60 nodelay; proxy_pass http://127.0.0.1:8765; }
```

服务端本身已有按 IP 的失败封锁（5 次 / 30 分钟）。nginx 这一层是为了两件
它做不到的事：让「换 IP 继续试」的成本更高（服务端封的是单 IP，这里限的是
速率），以及把撞库流量挡在 Python 之外，不让它占满 `ThreadingHTTPServer`
的线程。

顺带把这几个响应头也加上 —— 响应里带上游站名与判定结论，那些不该进缓存或
被别的站嵌进 iframe：

```nginx
add_header X-Robots-Tag "noindex, nofollow, noarchive" always;
add_header Cache-Control "no-store" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "no-referrer" always;
```

三个方案怎么选：

| | SSH 隧道 | 反代 + IP 白名单 | 反代 + 限速 |
|---|---|---|---|
| 公网暴露 | 无 | 有，但只对白名单 | 有 |
| 要配证书 | 不要 | 要 | 要 |
| 要改 nginx.conf | 不要 | 要 | 要 |
| 手机能用 | 不方便 | 看出口 IP | 能 |
| 每次操作 | 先开隧道 | 直接输网址 | 直接输网址 |
| 出口 IP 变动 | 无影响 | **白名单失效，要改配置** | 无影响 |
| 撞库防护 | 不可达 | 不可达 | 限速 + 服务端封锁 |

偶尔加几个账号 —— 用隧道。固定在家/公司操作 —— 白名单最严。
要随时随地开网页输密钥 —— 限速那套，但**长随机 token 是前提**：
限速把爆破从「每秒几百次」压到「每分钟 12 次」，可它不能替代一把好密钥。

### 第 3 步 · 用完关掉

它持有明文上游 Key 且能改写 `config.yaml`，**不要长开**：

```bash
# VPS
docker compose stop cpa-upstream-importer
```

登录用 **CPA 后台管理密码**（输原始密码，不是 `config.yaml` 里那串 `$2a$` 哈希），
或 `.env` 里的 `IMPORTER_TOKEN`。详见下面「登录：两把钥匙都认」。

**网页四步向导**：

| 步 | 做什么 | 此步成本 |
|---|---|---|
| ① 投喂 | 粘贴或拖 txt，每行 `url,key`。带不带 `/v1` 都行 | 零请求 |
| ② 探测 | 四段各打一次定归属；不通的先试代理、再回退标识头 | **开始花钱** |
| ③ 定档 | 结果表带表头 · 系统预勾选 · priority 可手工改 | 零请求 |
| ④ 写回 | diff 预览 → 确认 → 备份 → 落盘 → 可选推 CPA | **真正落盘** |

系统预勾选会**跳过**三类（仍可手工勾上）：静默换模、抢走顶层、上限由截断反推。

### 部署踩坑：`docker-compose pull` 会失败并中断整条命令

**症状**（2026-08-31 实测）：

```
$ docker-compose down && docker-compose pull && docker-compose up -d
 ! Network deploy_mynet Resource is still in use
 ! Image cpa-upstr... pull access denied for cpa-upstream-importer,
   repository does not exist or may require 'docker login'
Error response from daemon: pull access denied for cpa-upstream-importer ...
```

**后果比看起来严重**：`pull` 失败让 `&&` 链断掉，后面的 `up -d` **根本没执行** ——
主栈另外 27 个服务也跟着没起来。`docker ps` 里只剩几个之前就在跑的容器。

**成因**：`compose pull` 默认对**所有**服务尝试从 registry 拉镜像，
哪怕该服务有 `build:`。而 `cpa-upstream-importer:local` 是本地构建的，
Docker Hub 上不存在这个仓库。

**修法**：给该服务加 `pull_policy: build`（两份 compose 都已加）：

```yaml
  cpa-upstream-importer:
    build:
      context: ./cpa-upstream-importer
      dockerfile: deploy/Dockerfile
    image: cpa-upstream-importer:local
    pull_policy: build        # ← 告诉 compose：只从 build 来，别去 pull
```

需要 Compose v2.x。若你的 `docker-compose` 是 v1 二进制（不认这个字段）：

```bash
# 办法一：pull 时显式排除它
docker-compose pull $(docker-compose config --services | grep -v cpa-upstream-importer)

# 办法二：别用 && 把 pull 和 up 串起来 —— pull 失败不该阻止 up
docker-compose pull || true
docker-compose up -d
```

> **另一条警告可以忽略**：`! Network deploy_mynet Resource is still in use`
> 前缀是 `!` 而不是 `Error` —— 它是警告。成因是 `mysql-veloera`、
> `kirara-agent`、`llonebot` 这些**不属于本 compose** 的容器还连在
> `deploy_mynet` 上，所以 `down` 删不掉那个网络。不影响 `up -d`。
> 真要清掉就先停那些容器，但没有必要。

**验证部署是否真的成功**，别只看命令有没有报错：

```bash
docker compose ps                      # 应列出本栈全部服务
docker compose ps | grep -c Up         # 数一下真正在跑的
docker ps --format '{{.Names}}' | sort # 与预期对照
```

---

### 更新已部署的服务（改了代码之后）

**`web/`、`cpa_probe/`、`server.py`、`cli.py` 都是 `COPY` 进镜像的，不是挂载。
改了它们必须重建镜像 —— 只 `restart` 容器毫无作用，跑的还是旧代码。**

在**你自己的电脑**上：

```bash
scp -r cpa-upstream-importer root@<你的VPS-IP>:/opt/deploy/
scp docker-compose.yml   root@<你的VPS-IP>:/opt/deploy/     # 改了 compose 才需要
```

在 **VPS** 上：

```bash
cd /opt/deploy
docker compose build cpa-upstream-importer          # 必须，不能省
docker compose up -d cpa-upstream-importer
```

改完哪些要做什么：

| 改了什么 | 要做什么 |
|---|---|
| `web/`（index.html / app.js） | `build` + `up -d` + **浏览器硬刷新**（`Ctrl+Shift+R`） |
| `cpa_probe/`、`server.py`、`cli.py` | `build` + `up -d` |
| `deploy/Dockerfile` | `build` + `up -d` |
| `docker-compose.yml` | `up -d`（只改注释不会触发重建，正常） |
| `.env`（token / uid） | `up -d`（compose 会重建容器读新环境变量） |

> **浏览器硬刷新为什么必须**：`app.js` 与 `index.html` 是静态资源，浏览器会缓存。
> 镜像换了但浏览器还在跑旧 `app.js` —— 表现成「明明修了却还是老样子」。
> `Ctrl+Shift+R`（Mac 上 `Cmd+Shift+R`）跳过缓存。还不行就开无痕窗口验证一次。

彻底重来（镜像层也不要）：

```bash
docker compose down cpa-upstream-importer
docker compose build --no-cache cpa-upstream-importer
docker compose up -d cpa-upstream-importer && sleep 3 && docker compose logs cpa-upstream-importer
```

`importer-backups` 是命名卷，`down` 不会删它 —— 写回前的备份都还在。要看：

```bash
# 卷的真实名字 = <compose 项目名>_importer-backups。
# 没设 COMPOSE_PROJECT_NAME 时项目名取目录名，在 /opt/deploy 下就是 deploy。
# 不确定就先查：
docker volume ls | grep importer-backups

docker run --rm -v deploy_importer-backups:/b alpine ls -lh /b
```

改完代码本地先跑一遍测试（在**你自己的电脑**上，零外网请求）：

```bash
cd cpa-upstream-importer
python3 tests/run.py ../config.yaml       # 全过；比不传路径多一轮真实数据
```

### 第 1 步（备选）· 命令行

不想开网页、或要接脚本时用这条。**判定逻辑与网页完全相同**（共用 `cpa_probe/`）。

```bash
cd /opt/deploy

# ① 零请求零成本 —— 先确认格式对不对
python3 cpa-upstream-importer/cli.py -i accounts.txt --dry-run

# ② 探测但不写回（默认行为；必须显式 --write 才落盘）
python3 cpa-upstream-importer/cli.py -i accounts.txt --no-context

# ③ 探测 + diff 预览 + 写回
python3 cpa-upstream-importer/cli.py -i accounts.txt --no-context --write
```

`--write` **默认就会触发 CPA 重载** —— 只要给得出管理密码。没给密码不会静默跳过，
会明确告警并让你 `docker restart cli-proxy-api`。

```bash
# 管理密码：必须是你在 CPA 后台输的**原始密码**
#
# 不要从 config.yaml 提取 secret-key —— 那是 bcrypt 哈希（CPA 首次加载时自动
# 转换，config_load.go:104-113），而 PUT 端点用 bcrypt.CompareHashAndPassword
# 校验（handler.go:387），哈希当密码传必然 401。cli.py 会直接拒绝以
# $2a$ / $2b$ / $2y$ 开头的值，不让你白撞 401（连续 5 次失败封该 IP 30 分钟）。
#
# 用 read -s 避免进 shell history：
read -rs -p "CPA 管理密码: " MGMT && export MGMT && echo

python3 cpa-upstream-importer/cli.py -i accounts.txt --no-context --write \
    --mgmt-key "$MGMT"
```

要顺带做端到端验证（确认新上游经 CPA 真能出活，而不只是「CPA 收下了配置」），
再加客户端入口 Key：

```bash
# 客户端入口 key：取 api-keys 第一个。这个在 config.yaml 里是明文，可以直接读
export CPA_CLIENT_KEY=$(python3 -c "import yaml,io;print((yaml.safe_load(io.open('/opt/deploy/config.yaml',encoding='utf-8').read()).get('api-keys') or [''])[0])")
echo "CLIENT=${CPA_CLIENT_KEY:0:8}..."     # 确认取到了

python3 cpa-upstream-importer/cli.py -i accounts.txt --no-context --write \
    --mgmt-key "$MGMT" --client-key "$CPA_CLIENT_KEY"
```

`--push` 只在需要指定别的 CPA 地址时才用（默认取 `CPA_UPSTREAM_URL`，否则
`http://127.0.0.1:8317`）。要跳过重载自己重启：`--no-reload`。

`--client-key` 是第二道验证：push 成功只说明 CPA 收下了 YAML，
这一步才证明客户端真能用新上游 —— 换模站会在这里被判失败。

**网页开关与 CLI 参数一一对应**：

| 网页勾选 | CLI | 不勾/不给时 |
|---|---|---|
| 探测上下文上限 | （默认开） | `--no-context` |
| 失败时试代理 | （默认开，自动探地址） | `--no-proxy` |
| 同站间隔 N 秒 | `--gap N` | 默认 3 |
| 换模采样 N 次 | `--swap-samples N` | 默认 3 |
| 试用期定档 | （默认开） | `--by-score` |

### 代理地址：容器内外不通用

mihomo 有两个地址，取决于**从哪里**访问：

| 从哪里 | 地址 | 为什么 |
|---|---|---|
| 容器内（compose 起的服务） | `http://mihomo:7890` | 同 `default` 网络，Docker 内部 DNS 可解析 |
| 宿主机（直接跑 python3） | `http://127.0.0.1:7890` | compose 里映射的那个端口 |

CLI 与服务端都会**自动依次探测这两个**，不用手工选。

这件事有实际后果：`config.yaml` 里 **24 个凭据**配了 `proxy-url`
（gemini/codex/claude 各 7、compat 2）。**在容器内跑探测比在宿主机跑更准** ——
那 23 个站的代理路径能真正被验证。
---

## 部署

三种起法，按需选一种。**都只监听回环地址** —— 这个服务持有明文上游 Key 且能改写 `config.yaml`，不该直接对公网开。

### 登录：两把钥匙都认

| 凭据 | 从哪来 | 适合 |
|---|---|---|
| `IMPORTER_TOKEN` | 服务启动时打印，或 `.env` 里固定 | 脚本、自动化 |
| **CPA 后台管理密码** | 你进 `cpa` / `cpas` 后台用的那个密码 | 日常人工使用 |

第二条是**默认开启**的：能进 CPA 后台的人就能进投喂台，不必另记凭据 ——
两把钥匙的权限本来就等价（都能改写 `config.yaml`）。

输的是**原始密码**，不是 `config.yaml` 里那串 `$2a$...`（那是 bcrypt 哈希，
CPA 首次加载时自动转换的，见 `config_load.go:104-113`）。服务端做 bcrypt 比对，
所以需要 `python3-bcrypt`；没装则这条路径**安全关闭**，不会退化成明文比较。

失败 **5 次封该 IP 30 分钟**，与 CPA 自己的口径一致（`handler.go:301-302`）。
不想开这条路径：`--no-cpa-key`。

### 一次性（排障用，最简单）

```bash
cd /opt/deploy/cpa-upstream-importer
IMPORTER_TOKEN=$(openssl rand -hex 16) python3 server.py \
    --config /opt/deploy/config.yaml --port 8765
```

用完 Ctrl-C。不留后台进程，事故面最小。

### systemd（常驻）

```bash
cd /opt/deploy/cpa-upstream-importer/deploy
sudo ./install.sh                 # 装 unit + 生成 token + 起服务
sudo systemctl status upstream-importer
journalctl -u upstream-importer -f
```

`install.sh` 会做的事：校验 `config.yaml` 存在与可写、生成 32 字符随机 token 写进 `/etc/upstream-importer.env`（权限 600）、装 unit、`daemon-reload`、起服务、打印访问地址。已存在 token 时**不覆盖**。

### Docker · 已并入主栈（推荐常用）

服务定义就在 `/opt/deploy/docker-compose.yml` 的 `services.cpa-upstream-importer`，
与 `cli-proxy-api`、`cpa-manager-plus` 并列。

```bash
cd /opt/deploy

# 首次：确认 uid 与 config.yaml 属主一致，否则写回会 PermissionError
stat -c '%u:%g' config.yaml                    # 通常 0:0
# 不是 0:0 就写进 .env：
#   stat -c 'IMPORTER_UID=%u' config.yaml >> .env
#   stat -c 'IMPORTER_GID=%g' config.yaml >> .env

# 可选：固定 token（不固定也能用 —— 直接拿 CPA 后台密码登录）
echo "IMPORTER_TOKEN=$(openssl rand -hex 20)" >> .env

docker compose build cpa-upstream-importer
docker compose up -d cpa-upstream-importer
docker compose logs cpa-upstream-importer | head -20     # 看访问地址

docker compose stop cpa-upstream-importer                # 用完关掉，别长开
```

`restart: "no"` 是有意的 —— 它是按需启的运维工具，不该随主栈自启常驻。

三处刻意的设计，动之前先读注释：

| 项 | 值 | 为什么 |
|---|---|---|
| `ports` | `127.0.0.1:8765:8765` | 写成 `8765:8765` 会暴露到公网 |
| `volumes` | 只挂 `./config.yaml` 单文件 | 挂整个 `/opt/deploy` 等于把 `.env`、`secrets/`、`auths/` 一起递过去 |
| `user` | `${IMPORTER_UID:-0}:${IMPORTER_GID:-0}` | 镜像里的 `USER importer(10001)` 写不了 root 的 `config.yaml` |

单文件挂载在容器启动时就把宿主 inode 解析定死了，所以 `writeback.write_local`
**一律就地 `O_TRUNC` 覆写，绝不 `tmp + os.replace`** —— 换 inode 会让
`cli-proxy-api` 永远读旧文件（详见下面「为什么落盘必须就地覆写」）。
就地覆写不原子，所以**备份先于写入完成**，是前置条件不是可选项。备份落到
`importer-backups` 独立卷 —— 单文件挂载时同目录不可写。

根目录另有一份 `docker-compose.yml`，是**只含本项目的参考模板**，
用于在别的机器上单独跑（不带 CPA 主栈），或主栈那段被误改时对照恢复。
日常不用它。

### 外网访问

优先用 SSH 端口转发，**不要直接开公网端口**：

```bash
# 在你自己的电脑上跑，IP 换成你的 VPS
ssh -N -L 8765:127.0.0.1:8765 root@203.0.113.7
# 然后本机浏览器开 http://127.0.0.1:8765/
```

真要走域名，用 `deploy/nginx-snippet.conf`（完整 server 块，独立域名）。
两层叠加：**TLS + IP 白名单**。

**不要加 basic auth** —— 它与前端发的 `Authorization: Bearer` 抢同一个头，
会让页面能开但 API 全 401。同样的坑 `nginx.conf` 的 `cpas` 那段已记录过。
详见上面「方案 B」与片段里的说明。

---

## 测试

改任何代码后先跑这个：

```bash
cd /opt/deploy/cpa-upstream-importer
python3 tests/run.py                        # 不读 config.yaml 的那些
python3 tests/run.py /opt/deploy/config.yaml   # 加真实文件那一轮
```

退出码 0 = 全通过。**不传路径也必须全过** —— 套件自带最小样本
（`tests/fixture_cfg.py`），刚 clone 的仓库、CI runner、任何没有 CPA 部署的
机器都能跑完。传路径只是多跑一轮真实数据。

> 这一条是踩出来的：`test_server.py` 与 `test_edges.py` 原来把
> `../config.yaml`（生产配置）当默认输入，找不到就 `exit 2`。于是 CI 上必然
> 红，而 CI 的注释还写着「套件自带最小样本」。更隐蔽的一层是断言挂在一个
> **会变**的文件上：那份 config.yaml 一改，`gemini 五档`、`解析出 11 个不可用站`
> 这类硬编码基线全部失效 —— 报红却指不出真缺陷，比没有测试更糟，它会让人
> 去改代码。现在这类断言全部改成从被测的那份文件现算，或改断与内容无关的
> 不变式（解析可重复、站名都合法）。

八个套件覆盖面互不重叠，**缺一不可**（项数会随新增用例变化，这里不写死）：

| 套件 | 覆盖 | 发请求 |
|---|---|---|
| `test_probe.py` | 解析 · 判定 · 指纹 · 去重 · 定档 · 影响面 · 写回 | 否 |
| `test_server.py` | 鉴权 · 失败封锁 · 静态资源 · 路径穿越 · 写回闸门 · 脱敏 | 只打本机服务 |
| `test_pipeline.py` | 四阶段编排 · 代理预检 · 形态复用 · 事件流 · 端到端验证 | 全部打本机假上游 |
| `test_edges.py` | 写回边界 12 形状：同站 100 Key · 撞已有 provider · 已存在 Key 重导 · prefix 沿用 | 否 |
| `test_reload.py` | 生效链：inode 恒定 · 读回校验 · 403-CF 与状态码分类 | 打本机假 CPA |
| `test_speed.py` | 并发正确性：节流分桶 · single-flight · 配置缓存失效 | 否 |
| `test_web.py` | 前后端静态契约：DOM id · 事件 · 重试 · 地址 · 验证不跳过 | 否 |
| `test_tiering.py` | 定档算法：分数生效 · 死站零代价 · 别名表 · 逐轮自查缺陷 | 否 |

`test_pipeline.py` 起一个本机 HTTP 服务扮演上游，按 7 种画像返回不同响应
（余额耗尽、CF 拦截、缺标识头、静默换模、上下文截断、只通 compat 段……），
所以**零外网、零成本**，却是唯一能覆盖 `Prober._call` 的套件。

这一层不是可选的 —— 它第一次运行就抓到三个纯逻辑测试永远碰不到的缺陷
（同名遮蔽导致必崩、urllib 自动注入 UA、种子模型免检）。

`test_edges.py` 同理：它抓到四个「跑完真实探测才会暴露」的缺陷 ——
compat 段生成重名 provider、撞已有站时不合并、判重只查五元组、从不生成 `prefix`。
**层数不是冗余，是不同的问题。**

真实文件用例会读 `config.yaml` 并在内存里模拟写入，**不落盘**——
测试结尾会逐字节比对确认原文件未变。

---

## 输入格式

每行一组，逗号分隔：

```
https://api.example.com,sk-xxxxxxxxxxxx
https://other.example.org/v1,sk-yyyyyyyyyyyy
# 井号开头与空行忽略；同一站多个 key 写多行
```

**url 带不带 `/v1` 都行。** 段决定形态，系统自动规范化：

| 段 | base-url 形态 | 请求路径 |
|---|---|---|
| `gemini-api-key` | 裸域名 | `{base}/v1beta/models/{model}:generateContent?key=` |
| `claude-api-key` | 裸域名 | `{base}/v1/messages` |
| `codex-api-key` | 带 `/v1` | `{base}/responses` |
| `openai-compatibility` | 带 `/v1` | `{base}/chat/completions` |

14 个现存主机、121 个条目零例外。

---

## 探测会不会因为参数没配对而误判

常见的疑问。答案分三层，**第一层不给选择**：

### 第一层：baseline 无条件带齐

这些是 CPA 真实转发时一定会发的——缺一个就是在测另一条路，而不是测这个站：

| 段 | baseline 必带 |
|---|---|
| gemini | `x-goog-api-key`（**走头，不是 `?key=`**） |
| codex | `Authorization: Bearer` |
| claude | `Authorization` + `x-api-key` + `anthropic-version`，URL 带 `?beta=true` |
| compat | `Authorization: Bearer` |

每一条都是踩坑改出来的：

- **gemini 用头不用 `?key=`** —— 2026-09-01 实测：用 query string 时某站三种画像
  全部连接层失败（`000`），而用头能拉到几百个模型。探测测的不是 CPA 真实走的那条
  路，于是**把一个可用站判死**。
- **claude 两个鉴权头都发** —— 中转站实现不一，只发一个可能误判 401。
- **claude URL 带 `?beta=true`** —— CPA 三条 claude 路径全都带。原来不带的理由是
  「少一个变量」，但那让探测与真实转发形态不一致：站方按 query 参数分流时，探测
  通了而 CPA 不通（或反之）。**对齐优先于减少变量** —— 探测要问的是「CPA 这样发
  通不通」。

### 第二层：画像梯逐档升级（25 档 / 四段）

baseline 被拒后才逐档试，由省到全，第一个通过的即最小必需集。

**刻意不一次上全量**——那样虽然更容易通，却不知道哪几个头是必需的。而
`config.yaml` 里 `headers` 越少越稳：站方改门禁时，写 12 个头的条目比写 3 个的更
容易整体失效。

代价是多几次请求。这个取舍写在代码注释里：「多试几档只多几次请求，而漏试会把一个
可用站判死——**后者不可逆**」。

### 第三层：代理（条件触发 + 先预检）

只在判定为 `IP封` / `边缘` 时才试，且**先探代理自己通不通**：

```python
@property
def live_proxy(self):
    """代理地址，仅在预检通过时返回；不通则返回 None。"""
```

这条防的是另一种误判：代理本身挂了，却把「经代理失败」记成「这个站不行」。

### 仍会误判的四种情形

说清边界比声称「不会误判」有用：

| 情形 | 现象 | 出路 |
|---|---|---|
| 站方要真实客户端的**请求体**字段 | 25 档全过不了 | claude 段设 `fingerprint-profile: claude-code-cli` 让 CPA 补；其余三段配置层无解 |
| 分组**限时段** | 窗口外一律 403 | 已单独判为「时段」类而非「不可用」，并标出窗口 |
| 探测时**刚好余额不足** | 判「余额」 | 充值后重探。这一类不是误判，是时点事实 |
| 要的头**不在 25 档里** | 整梯全败 | 单站诊断看每档正文摘要；确知能用就人工接管 |

**最后一种是根本限制**：25 档来自实测积累，不可能穷尽所有站方的门禁规则。所以留了
两个人工出口——单站诊断与人工接管。

换个说法：**baseline 那一层不会因为我们少发头而误判**（该发的都发了），但**画像梯
之外的门禁我们测不出来**。前者是「我们没犯错」，后者是「我们的知识有边界」——
两件事不该混为一谈。

---

### 2026-09-05 补齐的五处「与 CPA 不一致」

一轮探测流水线审计（子代理，只读，判据是 CPA 现行源码）报了 12 处，其中五处属于
**同一形态**：探测发出去的东西与 CPA 真实发的不一样，于是测出来的结论对不上真实
转发。这类缺陷不报错、不失败，只是把好站说成坏站或反之。

每一处都跟了**撤销验证**——把修复改回旧行为，确认对应断言真的变红。

#### ① codex 段的 body 差两处

CPA 的 codex Execute 无条件 `stream=true`，并且在默认配置下把
`[{"type":"image_generation","output_format":"png"}]` 塞进 `tools`
（`codex_executor_execute.go:57` 与 `:64-66`；`config_load.go:75` 确认
`DisableImageGenerationOff` 是**默认值**），stream 时 `Accept: text/event-stream`。

探测原来发 `{"model":…,"stream":false,"input":…}`、无 tools、无 Accept。两个方向：

- 站方拒收注入的工具 → 探测判「可用」、写进 `config.yaml`，而 CPA **每一次**真实
  请求都失败
- 站方只实现 SSE 或校验 stream → 探测拿 400/未知，判死一个可用站

顺带发现 `classify` 里那条「注入」规则（认 `image_generation is not enabled`）
**一直是死代码**——探测从不发 tools，所以永远命中不了。

**为什么不直接把 baseline 改成 `stream=true`**：那样响应就是 SSE，而整条判定链读的
是 JSON 正文（`resp_model` / `input_tokens` / `has_error_envelope` /
`betas.wanted`）。改了等于把一种假阳性换成另一种。所以加了一档
`codex-cpa-shape`（tier=4，排在 `codex-full` 之后），它只回答一个问题：
「CPA 那样发，这个站收不收」。

#### ② 站方在 400 上索要 beta 时，整梯与 beta 重试都不跑

`classify` 对 400 没有兜底，实测这几种正文全落「未知」：

```
400 + '请启用 128k 输出后重试'                    → 未知
400 + 'missing required header: anthropic-beta'  → 未知
400 + 'anthropic-beta must include output-128k…' → 未知
（同样的正文在 403 上都判「门禁」）
```

而画像梯的触发条件是「类别 ∈ {客户端, WAF, 门禁, IP封, 边缘, 鉴权} 或状态码 ∈
{401, 403, 503}」——400 一个都不命中。更要紧的是 `_retry_with_betas` 的**唯一
调用点在画像梯内部**，整梯不跑它就永不跑：`betas.py` 的
`output-128k-2025-02-19` 与 `fine-grained-tool-streaming` 两条规则是**死代码**。
1m 那条能走到纯属巧合——`classify` 恰好把「1m context」限定在 `{400, 403}` 上
判「门禁」。

更糟的是「未知」在 `_PROXY_SECOND` 里，于是处置变成**换出口 IP**——对「缺一个
请求头」这个根因完全无关的补救。

修法是触发条件加一句「正文点名要求了我们认识的 beta」。**不泛化 400**——那会让
每个 400 都多跑一整梯，而 400 是最常见的错误码（参数错、模型名错、body 形状错
都是 400）。端到端对照：

| | 修前 | 修后 |
|---|---|---|
| 请求数 | 2 | 10 |
| 带 beta 的请求 | **0** | 2 |
| 段可用 | **False** | True |
| 类别 | **未知** | 可用 |

#### ③ 压缩过的错误正文一律判「可用」

画像梯的 `cc-full` / `cc-body-*` / compat `cc-full` 几档发
`accept-encoding: gzip, deflate, br, zstd`（抄 CPA 的形态），而 `client` 原来
不解压——`decode(errors="replace")` 把二进制变成一串 U+FFFD，于是**整条判定链
都失效**：

```
classify           → 无异常关键词 → 判「可用」
has_error_envelope → False
resp_model         → None → model_matches 放行 → _accept 收下这个模型
betas.wanted / _limit_from_body / input_tokens / 余额 / 限频 / 时段
                   → 关键词一个都匹配不上
```

那就是「死站带模型进 `config.yaml`」那个假阳性，只是改由压缩触发。

判据：CPA 发同一套 Accept-Encoding（`claude_executor_request.go:1093`）
**并且**解码（`claude_executor_execute.go:345`/`:373` 的 `decodeResponseBody`，
注释明确说同时处理「头声明」与「magic byte 探测」两种）。探测只抄了前一半。

现在按 magic byte 解 gzip 与 zstd，deflate 两种 wbits 各试一次
（`zlib` 与 raw deflate **都没有可靠的 magic byte**，raw deflate 首字节实测是
`0xab`）。br 与 zstd 标准库没有解码器（本项目零第三方依赖），探到就返回一句
**可读的说明**——静默给 U+FFFD 会让上面那条链无声失效。

#### ④ `client.send` 会抛异常，破掉自己承诺的不变式

`HTTPError` 分支里原来写着 `raw = e.read(READ_LIMIT)`——它在 except 块**内部**，
而 Python 的语义是 except 块内抛出的异常**不受同一 try 的其余 handler 保护**。
于是下面的 `socket.timeout` 与兜底 `Exception` 都接不到它，异常一路穿出 `send()`。

实测触发形态：`403 + Content-Length: 5000` 但只写 2 字节后挂住（Cloudflare
拦截页、nginx 慢响应都是这形态）。后果不对称得很难看：

- 并行路径把这个**只是回应慢的活站**写成「死路 · 探测异常」并建议降权
- 串行路径与 `run_job` 的 `f.result()` 让整个 job 报错，一批凭据全丢

修法是正文读取放进自己的 try，读失败也**不丢状态码**（403 就是 403，正文读不全
不改变这个事实）。

#### ⑤ WS 握手的 timeout 不是整体截止时间

原来只 `sock.settimeout(timeout)` 然后循环 recv——那是**每次读**的超时。对端每
0.3 秒送 1 字节且永不发空行时，每次 recv 都在 timeout 内返回，循环最多要收满
64KB 才退出：上界是 **65536 × 每字节间隔**（约 5.5 小时），而不是调用方给的
timeout。实测 `timeout=1` 的调用被挂住 180 秒以上仍未返回。

调用方传的是 `min(self.timeout, 30)`，本意是 30 秒上限——段级线程被钉住，
站级并发的槽位也一起占着。

判据：对面 CPA 用的是真正的截止时间——`codex_websockets_connection.go:32` 的
`dialer.HandshakeTimeout`（同文件 `:27` = 30 秒），gorilla 那个字段覆盖整次握手
而不是单次读。

现在加了 deadline，每次 recv 前把剩余时间设成 socket 超时。代码里那道
`if left <= 0: return` **是防御性的不是承重的**（正常情形下 recv 自己的超时先
触发），注释里写清了——否则下一个人会以为它没用而删掉，而删掉的后果是
`settimeout(负数)` 抛 `ValueError` 让错误消息变成 Python 异常名。

#### 另外三处：模型名、上下文上限、已停用凭据

- **模型名会被拼进出网 URL** 且未做字符校验——见「安全」那一节
- **`_limit_from_body` 把请求用量当上限**：`'context_length_exceeded: your
  request has 275000 tokens'` 原来抠出 275000，那是**请求值**而不是上限，
  比真实窗口大 → 客户端永不压缩，每个长请求都撞 400。`'max_tokens must be
  <= 8192'` 则把**输出**上限写成上下文窗口。前者收紧了关键词与数字之间的
  语气词要求，后者整条模式删掉——`max_tokens` 在 OpenAI 系里指的就是输出上限，
  那条模式带来的误取比命中多
- **已停用的凭据被当成在用站参与定档避让**：见「priority 定档」那一节的
  `entry_out_of_pool`

---

## 先诊断一个站：它到底要什么 header

批量导入之前常有个更具体的问题：**这个站为什么 401？它的门票是什么？**

页面顶部「单站诊断」折叠区回答这个。填 url + key、选段、点开始，它按画像梯
由省到全试，第一个通过的档就是最小必需集：

```
      baseline         t0 403  客户端  37ms   站方不查客户端身份
      cc-min           t1 403  客户端  13ms   只查 UA 形态与 claude-code beta
  ✓   cc-std           t2 200         31ms   另查 x-app / anthropic-version

最小必需画像：cc-std（试了 3 档）
下面这段可直接粘进 config.yaml 的该段条目里：

  - api-key: "<你的 key>"
    base-url: "https://api.example.com"
    headers:
      user-agent: "claude-cli/2.1.220 (external, cli)"
      anthropic-beta: "claude-code-20250219,interleaved-thinking-2025-05-14,…"
      x-app: "cli"
      anthropic-version: "2023-06-01"
      anthropic-dangerous-direct-browser-access: "true"

[复制 YAML]  [填进上面的输入框]
```

**它与批量导入是两个不同的意图**，所以不共用流水线：诊断只回答一个问题，
不建任务、不生成方案、不写回。想导入就点「填进上面的输入框」走正常流程 ——
诊断与写回之间必须有人工确认这一跳。

默认只查 claude 段（3–8 次请求）。选「全部四段」约 25 次。

它用的是 `profiles.ladder()`，与实际探测**同一套画像梯** —— 诊断结论必须与
真正导入时的行为一致，所以没有另写一份表。

三种结论的处置完全不同，界面分开说：

| 结论 | 意味着 |
|---|---|
| 命中某档且需要 headers | 给你 YAML，直接粘 |
| baseline 就通 | `headers` 留空即可，不需要任何头 |
| 整梯全败 | **不一定是拒绝你** —— 可能余额、限时段、或只认浏览器。看每档的正文摘要判断 |

命中的档如果还需要请求体字段（`needs_body`），界面会单独提示：headers 表达不了
它，claude 段可在条目里设 `fingerprint-profile: claude-code-cli` 让 CPA 自己补，
其余三段配置层无解。

命令行等价物（VPS 上没开网页时用）：

```bash
docker exec -i upstream-importer python3 /app/tools/diag-identity.py \
  --url https://api.example.com --key sk-xxx --section claude-api-key
```

---

## 改探测给出的 headers

探测判出的 headers 不是不可改的。结果表每段有个「请求头」折叠区，可以逐项编辑、
加行、删行。两种情形会用到：

- 探测判「门禁」，但你从别处知道正确的头
- 探测给出的头多了一项（漂移检测抓到过无条件发 `oauth-2025-04-20` 那一处）

**改动后那一段会标「已手工改过」，并明说「已验证」不再成立** —— 探测是用改动前
那套跑通的，改了就没测过了。这一点必须显眼：界面仍显示「✓ 可用」而实际配置已
不同，是最容易误导的情形。

「恢复探测值」一键回退。留空的行提交时丢弃（与 CPAMP 的 `buildHeaderObject`
同口径 —— 两边行为不同会让人在一处试通、另一处失败时找不到原因）。

头名会做**警告级**校验：含下划线（`anthropic_beta` 这类手滑）或没见过的头名会
提示，但**不阻止提交** —— 那张已知表不可能穷尽所有站方要的头，挡住合法冷门头
比放过一个手滑更糟。

---

## 每次尝试都能看

结果表下方每段有「N 次尝试」折叠区，列出这一段打的每一次请求：

```
claude 的 3 次尝试          1 次 200 · 最慢 37ms

  模型            画像/阶段    状态  类别    耗时   入 token  后端        正文摘要
  claude-opus-5   baseline    403   客户端  37ms                        {"error":{"message":"only…
  claude-opus-5   cc-min      403   客户端  13ms                        {"error":{"message":"only…
  claude-opus-5   cc-std      200          31ms   37        anthropic
```

排障时要看的「哪一档通的、别的档报什么、哪个慢」都在这里。`resp_model` 只在与
请求的模型不同时显示（那是换模）；「发送字符」列只在上下文二分那几次有值
（几十万字符），整段没有时该列不出现 —— 一个恒空的列看起来像数据丢了。

---

## 探测做什么

```
① 段归属   四段各打一次，看哪几段通
② 模型发现 问 /models 目录，按 gemini/gpt/claude 白名单过滤后逐个验
③ 处置     不通的：先试代理，再按「由省到全」回退标识头找最小必需头
           优先级 proxy-url > headers > 降 priority，绝不用 weight: 0
④ 质量     多次采样验静默换模；二分探 max-context-length
```

### 判定类别

正文关键词**优先于**状态码 —— 403 可以是余额、门禁、IP 封、CF 边缘拦截中的任意一种。

| 类别 | 判据 | 处置 |
|---|---|---|
| 余额 | `budget pool` / `预扣费额度失败` / `quota` 等，与状态码无关 | 充值，**永不降权** |
| 封号 | 403 + `has been banned` | 只降**该 key**，不动同站其他 key |
| 限流 | 429 | CPA 自带冷却与轮换 |
| 门禁 | 403 无 CF 特征，或 400 + `1m 上下文` | 站方后台开通，配置层无解 |
| IP封 | 403 + `challenge-platform` / `cdn-cgi` / `访问已被拦截` | 加 `proxy-url` |
| 边缘 | 403 + 空正文 | CF 概率拦截，重试即可 |
| 反测活 | `反测活` / `测活探针` | 换探测文本重测 |
| 死路 | `sensitive_words` / `无可用渠道` / `model_not_found` | 无解 |

探测文本固定用技术问句，**不能用 `hi`** —— 短消息会命中站方测活探针关键词。

### 临时错误会重试，一个不存在的模型不判死整段

两条实测教训（2026-08-31）：

- **503 / 502 / 504 重试一次**（间隔 2 秒）。站方负载上限不代表站点不可用。
  持续 503 判「临时」而非「死路」，重试有上限。
- **全部种子失败时取最严重的类别**，而不是最后一个种子的结论。
  种子模型是本工具写死的猜测：某站 `claude-opus-5` 返回 503、
  `claude-sonnet-5` 返回 404，原来整段判「死路」—— 而 sonnet-5 该站
  压根没有，它不存在完全不能说明这个站不可用。

404 `model_not_found` 属于**模型专属死路**，换个模型继续试；
敏感词、分组无渠道这类与模型无关的死路才立即收敛以省请求数。

### HTTP 200 不等于可用

有的中转站对**所有**请求都回 200，把真实错误放在正文里。

工具会检查正文顶层是否有 `error` 结构（或顶层 `"type":"error"`）——
命中就不收该模型。判据刻意窄：空值 `null`/`""`/`{}`/`[]` 不算，非 JSON 不算，
嵌套在 `choices`/`candidates`/`content` 里的 error 字样不算（模型正常输出
完全可能谈论 error）。

> 为什么这条最要紧：`Attempt.ok` 只看状态码，而换模判定在拿不到 `model`
> 字段时按设计放行（无证据不判换模）。两者叠加曾让这种站**四段全判可用、
> 注册 11 个模型**，而它完全不能用。死站进 `config.yaml` 会耗尽
> `request-retry × max-retry-credentials` 预算，最终客户端收到 500 ——
> 比判死危险得多。

---

## 站方不给测活怎么办：人工接管

有些站探针式短消息会被拦，或分组只允许特定客户端（`This group is
restricted to Claude Code clients`），而**真实对话完全正常**。这类站探测必然
判死，但它确实可用。

结果表里判死的那一行现在也有勾选框，右侧多一个模型输入框：

```
写入  段      判定    处置                          模型（手填）
 ☐   claude  门禁   403 且无余额/CF 特征     [claude-opus-5, claude-opus-4-8]
```

填了模型名（逗号分隔）才能勾上 —— 探测没验成功过任何模型，工具无从推断
该注册什么。清空模型会同步取消勾选。

勾上后的方案里会看到：

```
定档理由: 人工接管（探测判「门禁」）· 试用期档位 70（挡 1 个在用站…）
警告:     探测未通过（门禁 — 403 且无余额/CF 特征），模型清单由你手工指定：
          claude-opus-5, claude-opus-4-8。工具没有验证过这些模型能用
```

**人工接管只绕过「能不能用」这一个判定。** 去重、定档、影响面计算、diff
确认一道都不少 —— 那几道防的是写坏 `config.yaml`，与「这个站能不能用」是
两件事。试用期策略照常生效，影响面按你给的模型清单实算。

命令行侧目前没有等价参数，要人工接管请用网页。

> ⚠ 工具不会验证你填的模型名。写错会让 CPA 每次轮到它都失败，
> 消耗重试预算。填之前先在站方后台或用 `/v1/models` 确认。

---

## priority 定档

**数值大者优先，且分层隔离**（`sdk/cliproxy/auth/selector.go` 的
`availableAuthsFromPriorityBuckets` 与 `highestPriorityAuths`；快路在
`sdk/cliproxy/auth/scheduler.go` 的 `highestReadyPriorityLocked` +
`pickReadyAtPriorityLocked`）。低档凭据只在更高档**全部**不可用时才参与 ——
插错档不是「略微靠后」，而是永远轮不到。

### 硬隔离有两条例外（2026-09-05 核实）

「只取最高那一桶」这条判据是本工具整个定档算法与影响面计算的基础，
所以它的例外必须写清：

| 例外 | 触发条件 | 本部署当前 |
|---|---|---|
| **codex/xai + 下游 WS** | `preferWebsocket=true` 时从高到低扫，返回**第一个含 ws 凭据的档**（`scheduler.go` 的 `highestReadyPriorityLocked`，源码注释原话 "even if they are in a lower priority tier than HTTP-only credentials"） | **是活的** —— `routing.strategy: weighted-round-robin` + `session-affinity: false` 走内建选择器快路 |
| **session-affinity** | 选择器是 `SessionAffinitySelector` 时，交给它的候选是**全部档位**（`conductor_selection.go` 的 `availableAuthsForSelector`，注释说 "so an established binding can be validated instead of being preempted by a recovered higher-priority credential"）。不限段、不限 WS | **不触发** —— 生产配置 `session-affinity: false` |

第一条本工具已经会报警告（`ws_crosstier_note`）。第二条目前只有这段文档 ——
它一旦打开，本工具对「已绑定会话」的挡站计数就不成立（对「新会话」仍成立，
因为冷启动绑定还是从最高档开始）。

两条 WS 那一路的边界，核实过一并记下：`pickMixed` 在多 provider 时一律传
`preferWebsocket=false`，只有单一 provider 时才委派给 `pickSingle` 从而生效；
旧路的 `preferCodexWebsocketAuths` 是在**已经收窄到最高档之后**过滤，**不跨档**
—— 跨档只发生在 scheduler 快路。

### 已被 CPA 排除在池外的条目不参与避让

`build_band` 原来只看 `priority` / `models` / `weight`，于是下面这些条目被当成
**在用站**参与定档避让 —— 而 CPA 根本不会把请求路由到它们。判据是
`entry_out_of_pool`（`cpa_probe/plan.py`），四种形态：

| 形态 | CPA 侧的判据 |
|---|---|
| compat 的 `disabled: true` | `internal/watcher/synthesizer/config.go` 与 `sdk/cliproxy/service_models.go` 都是遇 `Disabled` 直接 continue —— 那个 provider **连 Auth 都不合成** |
| 任意段 `excluded-models` 含 `*` | 那正是管理面板「停用一个 config 型凭据」的实现（`config_apikey_disable.go` 的 `configAPIKeyDisablePattern = "*"`）。`applyExcludedModels` 用通配把该凭据的模型全过滤掉，清单空则 `UnregisterClient` |
| `base-url` 为空 | **门槛按段不同**：codex 与 compat 只要 base-url 空就在加载期被删（`config_normalization.go`，compat 那句的注释原文是 "treated as removed"）；gemini 与 claude 要 **api-key 与 base-url 都空**才删 |
| compat 的 `models` 为空 | `registerCompat` 走 `UnregisterClient`，而 `scheduledAuthMeta.supportsModel` 在 `supportedModelSet` 为空时对任何**具名**模型返回 false。**codex 段相反** —— 空 models 会回落 `GetCodexProModels()`，仍在池 |

实测后果（构造三站）：一个 `disabled: true` 的 provider 在 300 档且声明 kimi-k3
→ `model_top['kimi-k3']=300` → `suggest_priority` 把它当顶层避让，新站被压到 225，
而理由文案说「会挡 N 个在用站」—— 其中那一个不在调度池里。

生产配置里这四种形态都是 0 例，所以是补闸不是修事故。**但最后两种的段间差异
必须分开写** —— 一视同仁会把 codex 段的正常条目误判成出池。

三条硬约束：

1. **不动任何现有值**，只在空档插入。
2. **不劫持顶层** —— 建议值不越过该候选所声明模型的现有顶层（按最低值取上界）。
3. **试用期定档**（默认）—— 新站进**挡站最少的那一档**，不按探测得分进高档。

### 为什么试用期是默认

探测得分只能证明「此刻这一次请求成功了」，证明不了余额够用、限流阈值、
长时间稳定性、深夜是否降级。所以新站默认不按得分抢高档 ——
一个刚探测的新站在 claude 段若拿到 975，会挡住已经跑了两夜、证明过自己的站，
层级隔离下它们只在新站也不可用时才被尝试，等于用未知替换已知。

### 「为什么以前只给几十」—— 三个叠加的缺陷（2026-08-30 修）

用户反馈「分析后很多只有几十的优先级，差距特别大」。复现确认了三个缺陷，
每一个都让定档更保守，合起来把**满分候选压到 12**：

| 缺陷 | 症状 |
|---|---|
| ① 目标函数错 | `_shadow_count` 等权计数所有下层站，于是「不挡任何站」成了优化目标，必然收敛到最低可插档 |
| ② tie-break 反向 | 挡站数相同时取更低值 —— 挡 0 站的 850 与挡 0 站的 25 打平后选 25。后果：**`score=100` 与 `score=60` 给出同一个值，分数彻底失效** |
| ③ 不看现存站健康度 | `gemini` 段下层 49 个站**全部实测不可用**（逐站 503/401/403/404），却被当成「要保护的现有站」。为了不挡死站而把可用新站压到 12 |

**修法**：

- 「挡住」在 CPA 里只是「排在后面」—— 挡住一个**已经不可用**的站，代价是**零**。
  所以现在只数「活着的站」。健康信号两个来源，原来一个都没读：
  - `weight: 0` —— **强信号**，CPA 的 `positiveWeightAuths`（`selector.go:423-430`）已把它整个剔除
  - 注释里的实测结论 —— **弱信号**，是两夜排障的唯一记录（`# xxx：实测 503 …`、`# xxx 永久排除`）
- tie-break 改为**取最高**：挡 0 个活站意味着没有任何代价，此时压低档位没有收益，
  反而让新站排在一堆死站后面 —— 等于白探测一场。
- 定档必须传 `raw`（config.yaml 原文）才能读注释。**5 个调用点原来一个都没传**，
  健康度信号在实际运行中完全没生效。

修后同一份 config.yaml 上的结果：

| 段 | 修前（满分） | 修后（满分） | 修后（40 分） | 修后理由 |
|---|---|---|---|---|
| `gemini-api-key` | **12** | **465** | 17 | 其下 9 个站已实测不可用，挡住它们无代价 |
| `codex-api-key` | **25** | **850** | 75 | 其下 4 个站已实测不可用或 `weight:0` |
| `claude-api-key` | **25** | 25 | 25 | `relay-i.example`(30) 与 `relay-m.example`(120) 无失效记录，是真活站 —— 不越过它们是**正确**的 |

claude 段仍是 25，但这次是**有依据的**：那两个站在该段确实没有失效记录。
修前的 25 是算法缺陷，修后的 25 是正确结论 —— 数字相同，含义完全不同。

> **一个修 ③ 时踩到的新坑**：注释写人读短名，配置里是域名，两者**不保证有公共子串**：
> `jdw` → `relay-h.example`、`sm` → `relay-m.example`。
> 第一版用「短名是域名的点分标签」匹配，`jdw` 静默漏判 ——
> 不报错，只让定档悄悄变保守。修法：用 `openai-compatibility` 段的 `name` 字段
> 建**权威别名表**，不猜。

得分现在真的决定档位上限，理由里同时写明提权目标与代价：

```
试用期档位 850（不挡任何**在用**的站）；得分 100 支持的上限是 850；
其下 4 个站已实测不可用或 weight:0，挡住它们无代价（relay-c.example, relay-g.example…）
```

要一步到位按得分定档：CLI 加 `--by-score`。

### 同一空档内取值等效

`gemini` 段插 465 与插 200、890 挡的是**同一批站** —— 真正的决定是挑哪个空档，
不是空档内的数值。所以警告不说「手工调低」，直接给下一档的确切值与代价：

```
⚠ priority 465 会把 9 个现有站挡在其后（relay-c.example、relay-d.example…）
   —— 它们只在本站也不可用时才被尝试。改成 25 则只挡 4 站
```

这条警告**区分活站与死站**（2026-09-02 补上实现）—— 挡 9 个死站与挡 9 个活站
完全是两件事，只报数字会误导。此前只报总数，实测输出是「priority 280 会把 2 个
现有站挡在其后」，而那两个站在注释里都记着实测不可用、算法数出的在用站是 **0**：
警告说挡 2 个、算法说无代价，用户看前者就会去调低一个本来最优的档位。

现在下层全是死站时改说「排在 N 个现有站之前 —— 它们全部已实测不可用或
`weight:0`，挡住它们无代价」，并提醒这些站若日后恢复档位不会自动重算；
活死混合时主句只计活站，死站另说一句。

### 一批站不能都拿同一个值（2026-09-02 修）

上面那三条约束是**单个候选**的安全边界，`suggest_priority` 一次只回答「这一个站
该放哪」。串行调用它 79 次，每次问的都是「当前 config.yaml 有哪些空档」——
而 config.yaml 在整批算完之前不会变，于是 79 个凭据拿到同一个答案。

落盘实测：

```
claude   175 × 74            ← 74 个条目全同值
gemini   500×3   225 × 76    ← 76 个挤在 225
codex    700×4  475×5  425×3  155×67
compat   540×1  525×1   45×12
```

`priority` 的唯一作用是区分先后，全同值等于没写。修法是加一层
`assign_priorities`：收齐全批后按段分配，**站与站不同、同站所有 Key 同值**。

它不重新推导安全值 —— 每站的上限仍由 `suggest_priority` 给出，本函数只做
「把各站分开」：按探测质量降序取 `min(cap, 上一站 - 1)`，跳过与现有档位相撞的
值。详见上文「批量定档」一节的实测数据与两处退化提示。

**三条途径都要走它**：网页端全量重探、网页端增量导入、CLI。CLI 曾漏掉，
表现为同一份输入在命令行与网页端落盘出不同的 priority。

### 诊断工具：为什么 200+ 凭据顶不上

```bash
python3 tools/diag403.py /opt/deploy/config.yaml
```

只读、不发请求。它算出**每段真正在服务的池子有多大**：层级隔离下只有最高
priority 那一层参与轮询，其余全是死重量。实测你的部署曾是这样：

| 段 | 顶层 | 参与轮询 | 死重量 |
|---|---|---|---|
| `gemini` | 900 | **3** / 64 | 61 |
| `codex` | 800 | **7** / 65 | 58 |
| `claude` | 1000 | **5** / 70 | 65 |
| `compat` | 520 | **1** / 13 | 12 |

**四段全是单点** —— 那才是 403 的直接原因，不是重试配置不够。

它还会区分档位落差的含义：落差 ≥ 300 的下一档通常是「试用期最低档」或
「被降权处置过」，直接提上来等于把未验证的站放进生产轮询。工具给安全路径
（先探测、再逐个提档、观察成功率），并**强制提示提档前要现测** ——
`priority` 只反映「当初定的档」，不反映「现在还能不能用」。

> 这条提示是踩过坑加的：codex 段的 `cielo`(800) 落差只有 100，
> 看着像「稍差一点的备选」，实测却返回 **200 但正文是 `CF_APP_WAF` 拦截页**
> —— 状态码骗过了判定。光看 priority 落差会给出把必然失败的站提到顶层
> 这种危险建议。

---

## 写回

```
① 备份      config.yaml.bak-<时间戳>
② 行级编辑  只追加条目，保留全部注释与缩进
③ 本地校验  yaml.safe_load + 段内条目数核对
④ diff 预览 逐行呈现
⑤ 用户确认  ← 硬闸门，无跳过选项
⑥ 落盘      就地覆写（O_TRUNC，**inode 不变** —— 这一点是硬要求，见下）
⑦ 自动重载  PUT /v0/management/config.yaml + 读回校验
```

**为什么必须备份**：CPA 的 `PUT /v0/management/config.yaml` 落盘用 `O_TRUNC` 直写
（`config_basic.go:101-116`），且写后 `LoadConfig` 失败**不回滚**（`:163-167` 只返回 500）。
校验链本身是稳的（temp 文件 + `LoadConfigOptional` 全量校验才放行），危险的是通过校验之后的落盘阶段。

**为什么不用 PATCH**：`PATCH /{section}` 按 index/match 定位**已存在**条目，找不到返回 404，
不能新增。四段能新增的只有整段 `PUT /{section}`（丢注释）或 `PUT /config.yaml`（保注释）。

### 为什么落盘必须就地覆写、不能 `os.replace`

这是踩过的坑，别"优化"回去。

`config.yaml` 被 bind mount 进两个容器：

```
docker-compose.yml:364   ./config.yaml:/CLIProxyAPI/config.yaml:Z
docker-compose.yml:547   ./config.yaml:/data/config.yaml:Z
```

**单文件 bind mount 在容器启动时就把宿主 inode 解析定死了。** `os.replace` 换的是
目录项指向的 inode —— 宿主机看到新内容，而 `cli-proxy-api` 容器里的挂载点仍然指着
**旧 inode**（那个文件还被挂载引用着，没被回收），内容永远是旧的。

实测症状（2026-08-30）：宿主机 `wc -l` 14851 行、四段 212 条目，而 CPA 与 CPAMP 面板
都停在 206，**重启容器才对上**。这不是"通知没送到"，是 **CPA 读的根本是另一个文件**。

代价：就地覆写不原子，写一半崩溃会留下截断文件。所以备份是执行写入的前置条件。

### 写回后怎么让 CPA 立即生效（自动，不用你操作）

两条链刷的东西不一样，**缺一不可**：

| | 刷什么 | 谁触发 |
|---|---|---|
| fsnotify | `LoadConfig` + **`reloadClients()`** —— 真正重建凭据池，新上游才能被选中 | 容器内对该文件的 Write 事件 |
| `PUT /config.yaml` | 只更新管理 handler 的 `h.cfg`（`config_basic.go:162-168`） | 我们主动打 |

`PUT` 的作用**不是**代替 fsnotify，而是**保证 fsnotify 一定被触发**：CPA 的
`WriteConfig` 用 `O_TRUNC` 就地写（`config_basic.go:101-116`，inode 不变），
在容器内部产生一次确定的 Write 事件。真正让新上游可用的仍是随后那次 `reloadClients`。

为什么不能只靠 fsnotify：inotify 事件可能丢，而 CPA **没有轮询兜底** ——
`internal/watcher/` 全目录只有 debounce 定时器，没有任何 `Ticker`。事件一丢就永远
不重载，**也不会自愈**。

所以写回后会自动做：

```
PUT /v0/management/config.yaml   ← CPA 自己校验 + 就地落盘 + 触发自身 fsnotify
GET /v0/management/config.yaml   ← 读回校验：比对四段条目数
```

读回校验专门抓 inode 分叉 —— 若容器还在读旧文件，读回的条目数就对不上，会明确告诉你
去 `docker restart cli-proxy-api`，而不是让你以为写成功了。

**需要什么**：CPA 后台的**原始管理密码**（不是 `config.yaml` 里那串 `$2a$` 哈希——
`PUT` 用 `bcrypt.CompareHashAndPassword` 校验，`handler.go:387`）。

- **网页端**：用 CPA 管理密码登录投喂台，写回时自动复用，你什么都不用填
- **命令行**：`--mgmt-key "$MGMT"`，或 `export MGMT='<原始密码>'`

没给密码时不会静默跳过 —— 会明确告警并给出 `docker restart cli-proxy-api`。

CPAMP 面板另有 30 秒前端缓存（`apps/web/src/utils/constants.ts:13`），生效后等 30 秒
再硬刷新。

---

## CPA 地址千万别填公网域名

投喂台第 ④ 步的「CPA 地址」**留空**就对了 —— 留空走服务端配好的
`CPA_UPSTREAM_URL`，即容器内服务名 `http://cli-proxy-api:8317`。

填公网域名（`https://cpa.example.com` 这种）会这样失败：

```
CPA 尚未重载
403 失败：error code: 1010
```

`error code: 1010` 是 **Cloudflare 的拦截码**，不是 CPA 拒绝了配置 ——
**请求根本没到 CPA**，`config.yaml` 一个字节都没动。公网入口在 CF 后面，
管理端点这种非浏览器请求会被它挡下。

走容器内服务名的好处不只是绕开 CF：请求不出公网、不经 nginx、不受 CF 的
超时与 body 大小限制，而 `config.yaml` 有 870 KB。

> 这个坑的历史成因：那个输入框曾**硬编码** `https://cpa.example.com`，
> 于是容器里配好的 `cli-proxy-api:8317` 永远用不上。现在输入框默认留空，
> 且前端只在你**显式填了**才把地址传给服务端。`tests/test_web.py` 有断言
> 守着这一点，防止以后有人把 `value` 加回去。

**怎么判断配置到底生效了没有**：看写回结果面板那条「CPA 已重载 / 尚未重载」。
它带读回校验 —— 会去 `GET` 一次比对四段条目数，对不上就明确告诉你。
CPAMP 面板的数字（如 `全部 206`）反映的是 **CPA 的实际状态**，不是磁盘状态；
它和投喂台预览的数字不一致，说明配置还没被 CPA 用上。

---

## 「不代表新上游真能出活」是什么意思

写回成功后你会看到两层不同的结论，它们证明的事**完全不同**：

| 层 | 说什么 | 证明了 | **没**证明 |
|---|---|---|---|
| `CPA 已重载：PUT 200 + 读回一致` | CPA 接受并用上了这份 YAML | 配置语法对、CPA 内存已更新 | 客户端打过来能不能拿到东西 |
| `端到端验证：N/N 通过` | 拿 CPA 的客户端入口真打了一次业务请求 | **新上游确实能出活** | — |

只有第一层通过时，页面会提示「不代表新上游真能出活」。这不是警告有错，
是提醒你**还差一层没验**。

### 为什么这两层会分叉

`PUT /v0/management/config.yaml` 只是把 YAML 交给 CPA。CPA 转发请求时会：

- 加上它自己的标识头（UA、Originator）
- 走它自己的 translator 改写请求体

上游可能据此**换一个后端模型**回给你 —— 你请求 `claude-opus-5`，
CPA 转发后上游返回 `codex-auto-review`。直连测是 200，经 CPA 就不是你要的模型。
这是实测存在的情形（atlas 第 12 章），**只有真打一次 CPA 的业务端点才能发现**。

第二层按段打不同路径，和客户端的真实调用完全一致：

```
claude / compat  → POST /v1/messages
codex            → POST /v1/responses
gemini           → POST /v1beta/models/{model}:generateContent
```

拿到 200 后还要比对返回的 `model` 字段 —— 换模了就算失败。

### 现在默认会验，不用你填任何东西

客户端入口 Key 自动从 `config.yaml` 的 `api-keys` 取第一个。这个值
**只在服务端使用**，不进任何响应、不回填到页面 —— 它是 CPA 的入口凭据。

只有两种情况会跳过：`api-keys` 为空，或本次写入的段都没有可用模型。
两种都会明说原因，不再只丢一句「不代表能出活」让你猜。

> 这一层原来挂在一个需要你去 `config.yaml` 里翻 `api-keys` 的输入框上，
> 于是默认永远被跳过 —— 而它恰恰是唯一能发现换模的手段。`tests/test_web.py`
> 现在有断言守着「必须自动取」和「Key 不得进响应体」。

**第二层失败但配置已写入**是正常的：写回和验证是两件事，验证失败不会
自动回滚。要回滚就用面板上那份备份路径覆盖回去。

---

## 画像梯能不能跟着 CPA 自动更新

**部分能，而不能的那部分不是实现难度问题，是信息本身不存在。** 这个区分很重要，
否则会误以为「等 CPA 升级自动刷新」就万事大吉。

### 两类内容

**① 头的「值」—— 能自动跟随**

UA 版本号、X-Stainless 族、anthropic-beta 各项的具体值，在 CPA 源码里是
`const` 块与 `[]string` 切片，形态稳定可解析：

```go
// internal/runtime/executor/claude_executor_request.go:40-52
claudeCodeBeta          = "claude-code-20250219"
claudeMidConvSystemBeta = "mid-conversation-system-2026-04-07"
claudeEffortBeta        = "effort-2025-11-24"

// :61-67
var claudeCodeCLIConstantBetas = []string{
	"interleaved-thinking-2025-05-14",
	claudeRedactThinkingBeta,
	"thinking-token-count-2026-05-13",
	...
}
```

**② 档次划分（几档、每档含哪些头）—— 不能自动生成**

CPA 源码里**没有**这个信息。它只知道自己转发时发什么，从不问「少发几个行不行」。

而画像梯的全部意义正是问这个：config.yaml 里的 `headers` 越少越稳——站方改门禁
时，写了 12 个头的条目比写了 3 个的更容易整体失效。

举个具体的：`cc-min` = UA + anthropic-beta 这个分档，依据是实测发现
**golf 的门票恰好是那三项缺一不可**。CPA 源码里读不出这句话。

所以档次划分是「实测出来的站方行为」，不是 CPA 的数据。

### 已经做了什么

| 层 | 状态 | 说明 |
|---|---|---|
| 头的值从 CPA 派生 | ✅ | `profiles.defaults_from_config()` 读 `claude-header-defaults` / `codex.header-defaults` |
| beta 清单与 CPA 源码比对 | ✅ | `cpa_probe/cpa_source_probe.py` 解析 Go 常量表 |
| 漂移在界面显示 | ✅ | `/api/context` 返回 `profile_drift`，前端渲染 |
| 检测不阻塞页面 | ✅ | 后台线程算，接口只读缓存 —— 见下 |
| 档次划分 | ❌ 手工维护 | 信息上不成立，见上 |

### 这个检测曾经把首屏卡成白屏（2026-09-02 修）

**症状**：输入 token 后很长时间只有页头，正文全空，没有任何提示。

**根因两层叠加**：

1. `/api/context` **同步**调 `check_profile_drift`，远程模式要串行拉两个
   GitHub 文件。国内 VPS 直连 `raw.githubusercontent` 不通，实测每次干等
   15 秒；最坏还能叠上 codex 文件 15 秒与 `remote_commit` 10 秒 = **40 秒**。
2. `extract_remote` **只在成功时写缓存**，所以拉不通的环境每次打开网页都重付
   一遍 —— 实测第二次跟第一次一样慢。`remote_commit` 连缓存都没有。

前端那侧 `#gate` 与 `#app` 都 `hidden`，要等这个响应回来才 `#app.hidden = false`，
于是那 15-40 秒里只剩静态页头。

**三处都改了，一项功能没减**：

| 改动 | 效果 |
|---|---|
| `_drift_snapshot()`：接口只读缓存，缺失/过期丢后台线程 | `/api/context` 从不因它阻塞 |
| 失败也缓存（10 分钟，成功仍 6 小时） | 拉不通的环境第二次起瞬时返回 |
| `remote_commit` 纳入同一套缓存 | 少一次每次都走的网络请求 |
| 单次超时 15 → 8 秒 | 有负缓存兜底后长超时没有收益 |
| `X-CPA-COMMIT` 探测也挪进后台线程 | 那也是网络请求（3 秒），不该抬高接口下限 |
| 前端加启动骨架 `#bootbox` + 3 秒后换文案 | 首屏不再是白屏，慢也说得出为什么 |
| `pending` / `refreshing` 两个状态 | 「还没算完」与「这是几小时前的结论」界面上分得开 |

三条检测路径（本地源码 / 远程拉取 / `config.yaml` 的 header-defaults）与所有
输出字段全部保留，只是换成异步刷新。首次打开时漂移那一块显示「正在核对」，
每 3 秒自取一次（最多 10 次），算完就地替换 —— 只更新这一块，不整页重渲染
（否则会把你改过的并发数输入框重置回推荐值）。

实测：

```
              修前              修后
GitHub 可达    4.27s → 0.51s     0.26s → 0.02s
GitHub 不通    15.0s → 15.0s     0.49s → 0.06s   ← 关键差别在第二次
```

### 漂移检测抓到的第一个缺陷

上线这个检测当天就抓到一处真实问题：画像梯**无条件**发 `oauth-2025-04-20`，
而 CPA 只在 `oauthToken == true` 时才发它（`claude_executor_request.go:110-112`）。

我们是拿 **api-key** 探测的，不满足那个条件——无条件发它等于声称走 oauth 却
带着 api-key，按项检查 beta 的站会看到一个自相矛盾的请求。已修。

这类错误人工比对很难发现：那个清单有 8-17 项，而「哪几项是有条件的」要读函数体
才知道。

### 怎么开启

**精确模式**（读 CPA 源码，能区分有条件/无条件 beta）：

```bash
# .env
CPA_SOURCE=/opt/deploy/CLIProxyAPI
CPA_SOURCE_ROOT=/cpa-source
```

命令行直接跑：

```bash
python3 -m cpa_probe.cpa_source_probe /path/to/CLIProxyAPI
```

输出形如：

```
claude 无条件 beta（8 项）：
    claude-code-20250219
    interleaved-thinking-2025-05-14
    ...
claude 有条件 beta（9 项，探测时不发）：
    oauth-2025-04-20                       claudeOAuthBeta
    context-1m-2025-08-07                  claudeContext1MBeta
    ...
✓ 画像梯与 CPA 源码一致，无漂移
```

**退化模式**（读不到源码时自动走这条）：拿 config.yaml 的
`claude-header-defaults` 比对内置回落常量。覆盖面小得多——只有 UA 版本与
X-Stainless 族，**管不到 beta 清单**。界面会明确标注「未覆盖：anthropic-beta
清单（只有源码里有）」，不假装全都比过了。

两条都不成立时报 `checked: false` 并说明原因，而不是显示「一致」。

### CI 里的守卫

`tests/test_full_redetect.py` 有一项 `test_profile_matches_real_cpa_source`：
本机有 CPA 源码时，断言画像梯与它无 warn 级漂移；源码不在则跳过。

所以你更新 CPA 源码后跑一遍测试，漂移会直接让测试失败，而不是等某个站突然
401 才发现。

---

## 全量重探：把既有站也一起重新定档

默认行为是**只探新站**——粘贴的站探测完插进去，config.yaml 里已有的 121 个
条目一个字节不动。这是安全的默认值，但有一类问题它修不了：

- 某站的 `headers` 是站方上次改门禁之前配的，现在那套头已经不管用
- `priority` 在多次插档之后错位，高分站被压在死站后面
- `proxy-url` 指向的代理已经不通
- 站方新开了模型，或者下掉了原来声明的模型

这些都表现为「成功率下降」，但从 config.yaml 本身看不出来——要看实测响应。

**勾上「全量重探模式」**，工具会把 config.yaml 里所有既有站连同本次新增的站
一起重新探测，重新生成 `headers`、`proxy-url`、`priority`、`prefix`、模型清单
和上下文上限，最后给出**整个文件**的 diff 让你确认。

### 不加新账号也能跑：只体检既有站

**输入框留空，只勾「全量重探模式」**，就是纯体检——重新探测既有条目、按结果
更新配置，不新增任何站。这是常见需求：config.yaml 用久了想知道哪些站还能用、
哪些 headers 已经过期。

两种情形的确认弹窗不同（影响面不一样，用同一句话会让纯体检看起来也在往里加
东西）：

```
输入框留空  →  只体检既有站：重新探测 79 个凭据，按结果重新生成
                headers / 代理 / 优先级 / 前缀。不会新增任何站。

有新账号    →  重新探测 79 个既有凭据，并与本次新增的 3 行一起重新生成配置。
```

两者都在写回前给出完整 diff 供逐项确认。

### 界面

```
☑ 全量重探模式
    重新探测 config.yaml 中所有既有站，与新站一起重新生成配置。
    模型清单按实测替换；headers 合并；能力开关按实测开/关；
    priority 沿用原档（只有新站才定档）；weight / prefix / provider name
    只搬原值 —— 逐字段判据见「重探时每个字段以哪一侧为准」。

  站级并发数 [48]  [用推荐值]
    推荐 48（容器 4.0 核 / 24.0G）· 4.0 核 × 12 = 48 ·
    24576MB 的一半 ÷ 12MB = 1024 → 取 48（受限于核数）

⚠️ 全量重探将重新探测所有既有站。
   config.yaml 中有 121 个既有条目（去重后 79 个凭据）。
```

并发数的默认值**不是写死的 30**，而是启动时读 cgroup 算出来的。为什么必须读
cgroup：容器里 `os.cpu_count()` 返回的是**宿主机**核数——64 核宿主上跑
`--cpus=4` 的容器它仍报 64，按它算并发会超配 16 倍。

界面把完整依据摊开显示，因为一个凭空出现的数字你没法判断该不该改。docker 实测
五档配额：

| 容器规格 | 推荐并发 | 受限于 |
|---|---|---|
| `--cpus=0.5 --memory=256m` | 6 | 核数 |
| `--cpus=1 --memory=512m` | 12 | 核数 |
| `--cpus=2 --memory=1g` | 24 | 核数 |
| `--cpus=4 --memory=24g` | **48** | 核数 |
| `--cpus=8 --memory=2g` | 64 | 内存 85 与上界 64 |

读不到 cgroup 时回落 `sched_getaffinity`（能反映 taskset/cpuset），再回落
`os.cpu_count()`，并在界面上注明「在容器里但读不到 cgroup 配额，推荐值偏高，
请手工确认」。

### 高级：请求预算

折叠区里三项可调，每项都直接改变要打多少次请求：

```
▸ 高级：请求预算

  每段收模型 [4] 个      收够就停。写进 config.yaml 的模型数上限
  每段最多试 [10] 次     失败的也算。聚合站声明几百个模型时，这一项是唯一的刹车
  ☑ 画像结论按段复用     同段整梯全败后，后续种子不重跑

  预算估算：单站全不通约 30 次请求，全通约 68 次 ·
            78 个凭据 × 48 并发 ≈ 0.9 分钟
```

「每段最多试几次」与「每段收几个模型」是两件事：后者数**成功**的，前者数
**尝试**的。只有后者时，一个声明 838 个模型、但模型全都验不过的聚合站会把
白名单过滤后的全部打一遍。

### 请求数是怎么降下来的

三项优化，实测数字：

**① 按凭据去重** —— config.yaml 的条目是「(凭据, 段)」的组合，很多中转站用
同一把 Key 提供多种协议。你这份配置 **121 个 YAML 条目展开成 177 个 (凭据,段)
组合，去重后只有 79 个不同凭据**：
9 个跨全四段、11 个跨三段、49 个跨两段。而探测的语义本来就是「拿一个凭据把
四段各打一遍」，按条目喂它等于重复探 2-4 次。

去重键是 `(host, api_key)` 而不是 `(base_url, api_key)`——同一个站在不同段的
base-url 形态不同（codex/compat 带 `/v1`），用后者会把同一个凭据判成两个。

**② 画像结论按 (站, 段) 复用** —— 画像梯的调用点在种子模型循环内部，第一个
种子全败后循环走到下一个种子，**整梯重跑**。但门票是站+段的属性：站方查的是
headers 与 body 形态，不看模型名。假上游实测（全 403）：

```
优化前 57 次   browser-ua 打了 9 次、cc-min/cc-std/cc-full 各 5 次
优化后 30 次   每档恰好 1 次
```

**③ 模型验证补尝试上限** —— 把最坏情形从「目录长度」压到常数 10。

三项叠加：

| | 优化前 | 优化后 | 省 |
|---|---|---|---|
| 待探测单元 | 177 组合 | **79 凭据** | 55% |
| 单凭据请求数 | 57 次 | **30 次** | 47% |
| 总请求数（最坏） | 10,089 | **2,370** | **77%** |
| 48 并发耗时 | 3.9 分钟 | **0.9 分钟** | 4.3× |

**并发不放松任何单一端点的限频**。节流按 `(站, 段)` 分桶，同一个站的同一个段
两次请求之间仍然严格保持 `gap` 秒。并发放大的是「不同站之间」和「同一站不同段
之间」的吞吐——那些本来就互不相干。

进度是实时的：

```
[0.3s] 提取到 177 个 (凭据,段) 组合
[0.3s] 按 (站, Key) 去重：176 个条目 → 78 个凭据，省掉 98 次重复探测
[0.4s] 待探测 78 个凭据（每个凭据四段各探一遍，段级并发）
[0.5s] 开始批量探测（48 站并发）
[22.1s] 探测进度：42/78 · 成功 12 · 部分通 25 · 失败 5
[55.7s] 探测完成：31 成功，38 部分通，9 失败
```

### 与 `tools/recheck.py` 的分工

两者都是「复核既有站」，但用途不同：

| | `recheck.py` | 全量重探 |
|---|---|---|
| 形态 | CLI，只读报告 | 网页，能写回 |
| 用的模型 | 条目自己声明的 `models` | 重新做模型发现 |
| 输出 | 哪些站挂了 | 完整的新配置 + diff |
| 适合 | 例行体检、查某一段 | 配置全面老化后重整 |

`recheck.py` 告诉你「哪里坏了」，全量重探直接给「修好之后长什么样」。

### 探测发现原上游在别的段也能用：会加进去，并算进整体

一个凭据在 config.yaml 里往往只配了自己那几段（实测 79 个凭据里跨四段的只有
9 个，合计 177 个 `(凭据, 段)` 组合，79 × 4 = 316 里空着 139 个）。而中转站
常常先只卖 claude、后来加开 codex，配置里没人回头补。

全量重探对每个凭据的四段都发探测，所以这类新能力它是能发现的。发现之后**按证据
强弱决定加不加**：

| 本次这一段的模型清单来自 | 落盘 | 界面 |
|---|---|---|
| 实测跑通推理（`probed`） | 作为新条目写入 | 「新增段」徽标 + 建议写入 |
| 你手填的清单（`manual`） | 写入 | 「手填」徽标 |
| 站方 `/models` 目录声称有（`catalog`） | 写入，但默认不勾 | 「目录」徽标 + 需人工确认 |
| 工具兜底猜的（`seed`） | **不写** | 「不写入」，并说明手填真实清单即可放行 |

最后一档是硬闸，它挡的是另一个方向的事故：2026-09-02 那次条目从 121 变 246，
成因正是「四段都生成了方案、四段都写进去」，而那些新增段的清单只是工具猜测 ——
凭据在那一段没有任何依据可用，写进去只让 CPA 每次轮到它吃一次失败，耗掉
`request-retry × max-retry-credentials` 的预算。

`catalog` 与 `seed` 的差别不是「可信度高一档」这么模糊：目录里的名字是**这个站
自己报的**，种子清单是本工具写死的猜测，与这个站没有任何关系。

新增的段与既有条目**一起参与整体计算**：批量定档（站与站不同值、同站多 Key 同值）
和影响面（这些模型各自会挡住谁）都把它算进去，不是先定档再往里塞。反过来，被闸
拦下的段不参与——让它占一个档位会把各站挤得更低，而它根本不落盘。

写回后的警告会逐条列出新增了哪些 `(凭据, 段)` 以及各自的依据，被拦下的有几个。

### 全量重写与行级插入的区别

增量模式是**行级插入**：只在段尾追加新条目，既有的每一行（包括注释）原地不动。

全量重探是**全量重写**：所有条目重新渲染。注释不能靠「原地不动」保住了——
工具按 `(段, name/base-url)` 给每条人工注释建索引，渲染条目时再挂回去。
全局配置（`host`/`port`/`tls`/`remote-management` 那些）按「第一个 `*-api-key`
段之前」整段原样保留，不参与重建。

条目会按 `priority` 降序排列 —— 与 CPA 的优先级方向一致：**数值大的先被尝试**。
`priorityOrder` 按降序排（`scheduler.go:1197-1199`），取候选时取数值最大的那一层
（`scheduler.go:402`、`selector.go:541-543` 都是 `priority > bestPriority`）。
缺这个字段时 `authPriority` 返回 0（`selector.go:365-372`），也就是**最低**优先，
不是最高。

### 写回前后

写回走的还是原来那条链，一步都没少：

1. 基线比对——config.yaml 在生成方案之后被改过就 409，要求重新生成
2. `validate()` 校验 YAML 结构完整
3. `write_local()` 写前必备份，就地 O_TRUNC 覆写保 inode 不变
4. `PUT /v0/management/config.yaml` 触发 CPA 重载
5. 打 CPA 业务端点做端到端验证

### 一件要如实说明的事

全量重探在假上游、单元测试（`tests/test_full_redetect.py` 49 项）、一次端到端
演练（`tools/e2e_redetect.py`）和两次拿真实 config.yaml 的重建对账
（`tests/rehearse_real_rebuild.py`，各 52 项全对上）上验证过，但**没有对这 79 个
真实凭据发过一次真实探测请求**——对账用的是「把既有条目原样当探测结果」，验的是
写回链的守恒性，不是探测本身。

那份对账值得单独说，因为它抓到的十处缺陷单元测试全绿：

| 缺陷 | 实测影响 |
|---|---|
| **`headers` 整字段消失** | Desktop 版 24/24、fsdownload 版 66/66 条目；含 `anthropic-beta` 24 条 |
| compat 段组内没进方案的 Key 会消失 | gorou.example 15 把、tango.example 14 把 |
| per-key `proxy-url` / `weight` 被统一成 head 那把的值 | 8 把带 per-key 代理 |
| 同一个站两种 base-url 写法写出两个同名 provider | 同一把 Key 占两个轮询位 |
| **`prefix` 被 `dominant_prefix` 猜的值覆盖** | 121/121 条目，`ANT/xxx` 别名全失效 |
| **compat 的 `name` 被改成 host** | 12/13 provider 改名，冷却与能力缓存作废 |
| 注释索引把模型名当条目键、base-url 行尾注释没剥 | 4676 行注释里 118 行彻底丢失 |
| **模型级 `max-context-length` 消失** | 8/8 处，客户端按 CPA 内置目录的偏大值定压缩点 |
| **重探既有站被当新站重新定档** | claude 段 12 个站从 1000..50 变成 500..489 一片连号 |
| **留守条目的 priority 留在旧值** | 同站被拆成两层，「多 Key 并行」退化成「主备切换」 |

后四处是**逐字段 deep-equal** 才抓到的：之前只比字段的出现次数
（`text.count("prefix:")`），而「121 个条目的 prefix 全被抹掉、同时注释里多出
121 处提到 prefix」这种情况两边都数得对 —— 计数相等，值全错。

`headers` 那一处是**判据本身的漏洞**，比上面几处更深一层：值确实逐个比过了，
但它被列进了对账脚本的 `INTENT`（「本次有意改动，跳过」）豁免名单。于是
52 项全绿，而 24/24 条目的 headers 全丢没有任何一项能看见。

它是四段条目级字段里**唯一**「只生成、不搬运」的那一个：在 `_RENDERED_KEYS`
里所以 carry 不搬（那是给白名单**外**的字段用的），而 `existing_*` 查表以前
没有它。方案侧的 headers 只在探测当场判定 `need_ua` 时才有值 —— 重探时那个站
baseline 就通的话 `sp.headers` 是空 dict，那一行整个写不出来。

后果是真的改运行行为：条目级 headers 一路到达上游请求（`config.go` 的
`addConfigHeadersToAttrs` → attribute `header:<Name>` → `util/header_helpers.go`
的 `ApplyCustomHeadersFromAttrs`），所以丢掉
`anthropic-beta: context-1m-2025-08-07` 就是把那个站的 1m 上下文关掉，
而 YAML 合法、`validate` 报成功、写后验证也发现不了。

修法见「重探时每个字段以哪一侧为准」；`INTENT` 也从
`{priority, models, headers}` 收窄到 `{priority, models}`。

最后那一处落在**三方都不管的空档**里：`extract_carry_lines` 有意跳过整个
`models:` 块（清单由方案重新生成，搬原文行会与新清单打架），而方案只带**一个**
窗口值（`max_context_length` + `context_model`，本次探测实测的那一个）。本次没探
上下文时历史实测值全消失。CPA 把它写进 `/v1/models` 的 `context_length`
（`model_registry.go:1437`）与 Codex 的 `max_context_window`
（`codex/models/models.go:208`）—— 没有它就回落内置目录值，对中转站往往偏大，
客户端塞满上下文才发现被上游截断。现在按 (段, host, key, 模型名) 逐项搬，
优先级是「本次实测 > 原值搬运 > 不写」，绝不把 A 的窗口外推给 B。

合成样本一个都碰不到这些 —— 13 个 provider、69 把 compat Key、4676 行注释这种
形状只有真实文件有。

跑它：

```bash
python3 tests/rehearse_real_rebuild.py /opt/deploy/config.yaml
```

它不改任何文件，只在内存里重建一遍再逐项对账：

- 段条目数、`(凭据, 段)` 槽位数、顶层键数
- **四段之外的全局键逐个 deep-equal**（第一版事故是 `api-keys` 整段消失）
- **每个条目的每个字段 deep-equal**，只豁免本次有意改的 priority 与 models
  两个。`headers` 2026-09-04 从豁免里**拿掉** —— 它曾被当成「本次有意改」而跳过，
  于是「24/24 与 66/66 条目的 headers 全丢」这处 P0 一直没被这一关抓到
- 注释按**种类**比：零丢失、零多余（行数不比 —— 同一份注释在原文里被手工复制到
  同站 15 个条目上，重建后按站挂一次，行数必然减少且应该减少）
- `weight` / `proxy-url` / `headers` 逐 `(段, host, key)` 比值（行数相等还不够，
  跨段串了值行数也不变）
- `websockets` / `support-prompt-cache-key` 的生效行数守恒
- compat per-key 续行逐条一致
- **模型级 `max-context-length` 逐 `(段, host, key, 模型名)` 比值**
- **模型级白名单外字段逐项比值**（`display-name` / `thinking` / `image` /
  `force-mapping` / `is-compat` / `*-modalities` —— 当前配置一个都没用到，
  这一项守的是「将来手工加了之后不会被整段重写抹掉」）
- **每个 `(站, 段)` 只能有一个 `priority`**，且既有站的档位逐项不变。全勾与
  「隔一把勾一把」两种情形都比 —— 后者才暴露留守条目留在旧值那一处
- 外加「只勾一个段」「compat 只勾组内一把 Key」「跨段新增按证据放行」三种情形

第一次实跑仍建议看完 diff 再决定是否写回——如果某个站此刻恰好在临时维护，探测会
把它判成不可用，而 diff 里能看出来。

### 与 CPA-Manager-Plus 的并发写：CPAMP 会整份回滚（2026-09-05 实测订正）

**先说结论**：全量重探期间不要在 CPAMP 里碰任何 provider；写回完成后等 2 秒
再去 CPAMP 操作。

2026-09-04 那一版这里写「两边都有基线比对，都挡不住**同一秒**的并发」——
**那个结论方向是错的**。实际是 **CPAMP 覆盖本工具，而窗口宽得多**。

#### 危险的那条路：provider 分段 API

CPAMP 改一个 provider 走 `PUT /claude-api-key` 这类分段端点，落到 CPA 的
`PutClaudeKeys`（`internal/api/handlers/management/config_lists.go:567-598`）：

```go
h.cfg.ClaudeKey = arr     // 只换这一段
h.persistLocked(c)        // 但把【整个 h.cfg】重新序列化落盘
                          // handler.go:410 SaveConfigPreserveComments
```

而 `h.cfg` 只由 fsnotify 那一路刷新，前面有 150ms 与 1s 两道 debounce
（`internal/watcher/watcher.go:87` 与 `:89`）。这条路上**没有文件级基线比对**
—— `GET /config` 读的是 `h.cfg`（`config_basic.go:31` 的 `new(*h.cfg)`），
不是磁盘；30 秒前端缓存读的也是它。

三步实测（真实 config.yaml、CPA 自己的 `LoadConfig` +
`SaveConfigPreserveComments`、全在临时副本上）：

| 时刻 | 发生什么 | 磁盘状态 |
|---|---|---|
| T0 | CPA 加载配置 | claude=65、gemini[0].priority=280 |
| T1 | 本工具就地覆写：加一个 claude 条目、把 gemini[0] 改成 999 | claude=**66**、priority=**999** |
| T2 | CPAMP 用**陈旧的** h.cfg 发一次 `PUT /codex-api-key`（body 原封不动） | claude **回到 65**、priority **回到 280** |

新条目的 `api-key`、`prefix`，连同它那一行人工注释**一起消失**。
HTTP 200、`validate` 通过、零警告。

**所以窗口不是「同一秒」，而是「从 CPA 上一次 config 重载到现在」。**
全量重探跑几分钟，这几分钟里 CPAMP 任何一次 provider 编辑都会整份回滚。

反方向**不成立**：本工具的 `_api_apply` 在 `_apply_lock` 内做原文比对，
CPAMP 写过就回 409 要求重新生成。

#### 安全的那条路：配置页

CPAMP 的配置页走 `PUT /config.yaml`（`services/api/configFile.ts:19`），
提交整份内容，但两点都做对了：

- **不丢注释**：编辑走 `yaml` 库的 `parseDocument` + `setIn` 再 `toString()`
  （`hooks/useVisualConfig.ts:920` 的 `applyVisualChangesToYaml`）
- **有基线比对**：提交前重新拉一次 `fetchConfigYaml()`
  （`ConfigPage.tsx:893-912`），不一致就放弃保存、把改动重新应用到最新内容上
  让人再确认

但**这条安全通道与四段无关**：`setIn`/`deleteIn` 路径一个 provider 段都不碰
（只动 `auth.providers.config-api-key` 一处）—— 配置页管的是全局配置。

#### Accounts 页的批量操作碰不到 config.yaml 的条目

上一版把「凭据字段的批量改动走 `patchFields`」算成**另一条写 config.yaml 的路**
—— 那是错的。config.yaml 里的 api-key 条目**根本不出现在**
`GET /auth-files` 里：`buildAuthFileEntryLocked` 要求 `attrs["path"]` 非空，
而 config 合成器从不设它（只有文件来源的 `file.go` 设）。所以 Accounts 页的
批量 websockets / priority / weight 对 config.yaml 条目**完全不可达**。

这条待现场确认一次：

```bash
curl -s .../v0/management/auth-files \
  | jq '[.files[].source] | group_by(.)|map({(.[0]):length})'
```

如果线上的 filestore 给 config auth 回填了 `attrs["path"]`，那就会多出第三条
写 config.yaml 的路（`PatchAuthFileStatus` → `excluded-models: "*"`）。

#### CPAMP 落盘的保真度

同一轮实测：**注释不丢** —— 1758 行整行注释、152 处行尾注释、10 个空行、
全部键计数零变化，0 行孤儿注释。此前担心的「用了 CPAMP 就丢掉 4676 行注释」
不成立。

两处要知道的副作用：

- **行尾注释的列对齐被压掉**：144 行 `priority: 280        # …` 变成
  `priority: 280 # …`。本工具下一次原文比对时这 144 行会算成「文件被改过」——
  不是数据损坏，但会触发一次 409 让人重新生成方案。
- **条目级与模型级的未知键会被剪掉**（`pruneMissingMapKeys`，
  `config_yaml.go:721-750`）：比当前 CPA 版本更新的字段，经任何一次 CPAMP
  分段保存就没了。顶层未知键保留。这与本工具 `extract_carry_lines` 的原文行
  搬运恰好互补 —— 本工具保得住，CPAMP 保不住。

---

## 复核既有凭据：`tools/recheck.py`

```bash
python3 tools/recheck.py /opt/deploy/config.yaml                    # 全测
python3 tools/recheck.py /opt/deploy/config.yaml --top-only         # 只测顶层（快）
python3 tools/recheck.py /opt/deploy/config.yaml --section codex-api-key
```

**这个工具与投喂台主流程测的不是同一件事。**主流程探测**新站**（还没进
config.yaml，没有 models 字段，只能用种子模型猜）；本工具复核**既有站**，
必须用它们自己声明的模型。

### 四条铁律 —— 每条都对应一次实测踩坑（2026-08-30）

**① 模型必须取自条目的 `models` 字段，不能用种子模型**

我拿 `gpt-5.6-sol` 统一测 codex 段 11 个站，得出「0/11 可用」——
完全错误。**7 个站根本不声明这个模型**。CPA 只把请求路由到声明了该模型的
凭据，拿一个站没注册的模型去打，403/404/503 是必然的 ——
那不是站坏了，是测错了。

**② 段的协议不同，`500 not implemented` 不是故障**

`codex-api-key` 段走 `/v1/responses`（`codex_executor_execute.go:76`），
而多数中转站只实现 `/v1/chat/completions`。config.yaml 的注释早就记了：
「11 个站点中 2 个真正实现 Responses」。我把「协议不支持」误报成「站挂了」。

**③ 请求头必须与 CPA 实际转发时一致**

不带任何头直连测 `cielo` 的 codex 段，21 个组合全部 **401
unauthorized client** —— 而 CPAMP 面板显示它 **100% 成功率**。
逐头对照实测：

```
cpa-现状（带 codex UA）  200 ✓
originator-only          401
ua-only-codex            200 ✓
ua-only-browser          401
codex-全量               200 ✓
```

该站要的就是 codex 客户端的 `User-Agent`。CPA 转发时带了它，我没带。
所以工具的基线头取 `CPA_DEFAULT_UA[段]`（`cpa_probe/request.py:46-51`，
从 CPA 源码抄录的实际转发值），再叠加条目自己配的 `headers`。

**④ 别把好站打成限流**

用 12 线程跑全段复核时，compat 段的 `relay-i` 返回 **429 Too many
requests** —— 而低并发时它是可用的。**是我打太快把它打成限流了。**

假阴性比漏测更糟：它会让人把好站当坏站处理（降档、加 `weight: 0`），
而那是不可逆的判断错误。工具因此按 **(站, 段) 分桶节流**，默认 3 秒：

```bash
--gap 5      # 撞上限流严的站就调大
--gap 0      # 只在你确定站方不限频时用
--workers 4  # 降并发也能缓解，但节流更精准（不同站本来就该并行）
```

分桶键取 `(站, 段)` 而非全局 —— 站方限频按端点计，四段打不同路径，
各自计时不会放松任何一段的限制。与 `cpa_probe.pipeline` 同口径。

### 「可用」的含义

输出的可用只对 **(key, 站, 段, 模型) 四元组**成立。同一个站在不同段
结论可以完全相反：

| 站 | claude 段 | codex 段 |
|---|---|---|
| `relay-f.example` | **200**，3.6 秒 | **500 not implemented**（不实现 Responses） |

所以「某个站挂了」这种说法本身就不精确 —— 必须说清是哪个段、哪个模型。

### 一次真实的诊断链

用修好的工具跑出来的结果，直接定位了一个我自己造成的问题：

```
codex 段 gpt-5.6-sol 的承载站（按 priority 降序）
  900  relay-m.example      1 key   <-- 顶层，复测 3/3 全 502
  800  relay-c.example   7 key   <-- 实测可用，被上面挡住
```

层级隔离下 900 那层要全部进冷却才降到 800 —— **一个单 key 的坏站挡住了
7 个可用 key**。CPAMP 面板上 cielo 显示 100% 成功率而客户端仍报错，
成因就是这个。

> **提档的教训**：我当天下午基于「实测 200、3.59 秒」把 relay-m 从 180
> 提到 900，晚间它就全 502 了。**提档依据是「此刻这一次请求成功了」，
> 不是「这个站可靠」。**单点站（该段只有 1 个 key）提到顶层风险特别高 ——
> 它一挂整层就空，下面的可用站要等冷却期过完才轮到。
> 提档到顶层前应确认：① 多次复测稳定 ② 该站在本段有多个 key。

---

## 导出 CPA 错误日志：`tools/export-logs.sh`

排障要把日志发给别人时用这个，**别手动 tar**。原因见下面的警告。

```bash
cd /opt/deploy                                   # 必须在 config.yaml 旁边
bash upstream-importer/tools/export-logs.sh      # 最近 50 个错误日志
bash upstream-importer/tools/export-logs.sh 200  # 最近 200 个
```

产出 `log-export-<时间戳>.tar.gz`，内含四样：

| 文件 | 内容 |
|---|---|
| `error-logs/` | 错误日志原文（已脱敏），每次失败一个文件 |
| `stdout.log` | 容器 stdout 全量 |
| `digest.txt` | 摘要表，一行一个错误日志 —— **先看这个** |
| `context.txt` | 日志相关配置与容器状态（不含密钥） |

> ⚠ **错误日志含明文上游 API Key。** CPA 在 `debug: true` 下把完整请求体
> 写进错误日志，其中有 `Authorization` 头。手动打包发出去，那些 Key 就跟着走了。
>
> 脚本默认脱敏，覆盖 6 种形态：`sk-*`、`Bearer *`、JSON 里的
> `api_key` / `x-api-key` / `secret_key`、`x-goog-api-key` 头、URL 上的
> `?key=`。改的是副本，**原始日志一个字节都不动**；脱敏后还会自证一遍，
> 仍检出凭据形态就拒绝打包并保留目录待人工检查。
>
> `--raw` 跳过脱敏，只在「日志完全不出本机」时用。要发给别人、贴 issue、
> 传网盘，就绝对不要加。

排障信息不受脱敏影响 —— 上游 URL、状态码、错误消息都保留。

### 两类日志不在同一个地方

| 类型 | 位置 | 保留策略 |
|---|---|---|
| 错误日志（每次失败一个文件） | `logs/cli-proxy-api/error-*.log` | `error-logs-max-files`，默认 50，超了删最旧 |
| 常规运行日志 | 容器 stdout | Docker json-file 轮转，compose 里是 `10m × 3` |

### 想留更久：改文件数，别开 logging-to-file

```yaml
error-logs-max-files: 200      # 从 50 提高，这一项真的有用
logs-max-total-size-mb: 512    # 够用；只有错误日志时撞不到它
logging-to-file: false         # 保持 false —— 理由见下
request-log: false             # 保持 false
```

改完 `docker compose restart cli-proxy-api`。

**`logging-to-file: true` 会让常规日志挤掉错误日志。** 那个开关把常规运行
日志也写进同一个 `logs/` 目录，而 `logs-max-total-size-mb` 的清理是**按整个
目录总大小、从最旧开始删**，不区分类型。常规日志连续写、错误日志偶发，
两者共用一个配额的结果就是：常规日志的持续增长把错误日志顶出去 ——
真出故障时回看，只剩最近一小段。`config.yaml` 里那条注释已经点到这个机制
（「该清理机制作用于整个 logs 目录」）。保持 `false`，常规日志留在容器
stdout（`docker compose logs` 能取），错误日志独占 `logs/` 配额，互不挤压。

**`request-log: true` 不要开** —— 它记录每个请求与响应，单条可达 10MB+，
`config.yaml` 自己的注释就写着「硬盘不够大请不要开启」。排障用错误日志够了。

### 能回看多久

按 `config.yaml` 注释里的实测数据推算（单份 70KB–2.1MB，一次故障 10 个文件
在 75 分钟内被后续错误全部挤出）：

| `error-logs-max-files` | 故障**持续爆发**时可回看 |
|---|---|
| 50（默认改后值） | 约 6 小时 |
| 200 | 约 25 小时 |

这是**下限** —— 那个速率取自故障爆发期。平稳期错误稀少，200 份可能覆盖数天。

两个限制叠加生效，谁先撞到谁生效。只有错误日志时永远是**文件数**先撞：
最坏情况 200 × 2.1MB ≈ 420MB，连 512MB 都到不了。所以
`logs-max-total-size-mb` 在这个场景下只是兜底，调大它不会延长保留时间。

**要长期存档，靠的不是调这几个数** —— CPA 的清理机制设计目标是「防止撑爆
磁盘」，不是「长期保存」。真要留全量就定期归档出去，例如 cron 每天一次：

```cron
30 4 * * * cd /opt/deploy && bash upstream-importer/tools/export-logs.sh 200 >/dev/null
```

导出的包已脱敏，可以直接往别处搬。

### 只想看摘要，不打包

```bash
cd /opt/deploy
bash upstream-importer/legacy/logs-digest.sh 50
```

关键是「尝试」列：`0` 表示 CPA 在选择阶段就返回 503、请求根本没出门
（成因见 `config.yaml` 的 `transient-error-cooldown-seconds` 注释）；
`>0` 表示已发到上游，看「错误」列。

---

## 挂机时怎么不让 403 透传给客户端

目标：长时间挂机，客户端**绝不该看到 403** —— 要么换到能用的凭据，要么等。

### 为什么会透传（读 CPA 源码确认）

三个事实叠加，缺一不可：

| # | 事实 | 位置 |
|---|---|---|
| 1 | `request-scoped-errors` 的 `continue-and-cooldown` **仍消耗预算** —— `attempted[auth.ID]` 在 `executor.Execute()` **之前**就标记 | `conductor_execution.go:362` |
| 2 | 预算耗尽后**返回 `lastErr` 原样透传** —— 最后一个上游的 403 直接给客户端 | `conductor_execution.go:325-330` |
| 3 | CPA **没有**「耗尽则等待而非报错」的配置（全库无 `wait-for-available` 之类） | 全库搜索无匹配 |

所以哪怕 187 个条目都配了 `request-scoped-errors`、规则也对，预算不够就照样透传：
**claude 顶层曾有 35 个凭据，而预算只有 `1 × 4 = 4`** —— 只试 4 个就放弃，另外 31 个从没试过。

实测抽样印证：同一站内凭据状态不一致（`hotel` 3 个里 1 个 200、1 个 403 门禁、1 个超时）。
轮询撞上坏的就透传。

### 但直接加大预算会踩另一个坑

`config.yaml` 注释记着 2026-08-26 的实测：「8 次请求落在同一个 75 毫秒窗口内，
触发了 relay-l 前面 Cloudflare 的速率限制」。

我模拟了 CPA 的平滑加权轮询（`selector.go:539-560`）——
**权重相同时严格按数组顺序轮转**。而条目按站分组连续排列，
claude 顶层最长连续 **14 个同站**。设预算 35 就是连打 14 次 relay-l。

> `weight` 解决不了这个：模拟确认它改变**每个凭据被选中的频率**，
> 不改变同权重内的顺序。同站凭据权重相同 → 仍然连续。

### 实际改法：压缩单站在顶层的凭据数

| 每站留 k 个 | 池子 | 最长连续同站 | 最坏耗时 | 评价 |
|---|---|---|---|---|
| 1 | 3 | 1 | 6s | 安全但池子太小 |
| **3** | **9** | **3** | **18s** | **选它** |
| 5 | 15 | 5 | 30s | 偏多 |
| 15 | 35 | 15 | 70s | CF 限速风险 |

最终配置：

```yaml
request-retry: 1              # 不动 —— 注释实测 3×20=60 次会让客户端先读超时
max-retry-credentials: 9      # 4 → 9，覆盖顶层池（每站 3 个 × 3 站）
max-retry-interval: 30        # 0 → 30，愿意等 30 秒跨过短冷却窗口
```

claude 顶层 26 个多余的 key 降到 **990** —— 仍高于 950 的 `alfa`
（实测超时 90 秒），所以那个坏站仍轮不到。

`max-retry-interval` 这一改**推翻了**注释里 2026-08-27 那条「30 秒会被客户端读超时
先打断」的判断。那条在「宁可快速失败」的目标下成立；挂机场景下等待优于失败。

为 0 时的实际行为（`conductor_selection.go:1222-1231`）：

- 有立刻可用的凭据 → 立刻重试（不受这一项影响）
- **所有候选都在冷却 → `maxWait<=0` 就停止重试，把最后那个错误原样透传**

而普通 403 会让凭据冷却 **30 分钟**（`conductor_cooldown.go:813-821`，硬编码不可配）。
顶层 9 个接连 403 后整层在 30 分钟内全冷却 —— 为 0 时连「等几秒」都不做。

### 改完的实测结果

```
claude 顶层 9 个凭据：7/9 可用
  relay-l.example     2/3   （1 个 403 余额）
  relay-h.example 2/3   （1 个 403 门禁）
  relay-f.example      3/3
```

预算 9 能试遍全部 9 个 —— 撞上那 2 个坏的会自动跳到好的，**403 不透传**。

### 用 diag403.py 自查（命令行）

```bash
python3 tools/diag403.py /opt/deploy/config.yaml
```

算「预算 vs 顶层池」够不够、每段是不是单点、连续同站长度。**零请求，只读分析**。

### 用图形面板调优（网页，推荐）

访问 `http://127.0.0.1:8765/`，展开「**全局调优体检**」面板，点「**开始体检**」。

**面板给的不是数字，是带判据的完整建议**：

```
max-retry-credentials: 4 → 9    【必改】
当前预算 4 < 顶层池 9，有可用凭据永远轮不到 —— 预算耗尽后 CPA 透传 403

max-retry-interval: 0 → 8    【建议】
所有候选冷却时会直接透传，不等 8 秒让部分凭据退出冷却

streaming.bootstrap-retries: 3 → 3    无需改动
已是建议值
```

**顶层池实况**（每次档位变化后这个也跟着变）：

```
claude-api-key  顶层档位 900 · 9 个凭据 · 3 个站
                最长连续同站 3 个（relay-l.example）—— 会连打同一个站

openai-compatibility  顶层档位 500 · 12 个凭据 · 4 个站
                      最长连续同站 5 个（aggregator.example）—— 会连打同一个站
```

**为什么要这个面板**：

1. **预算与档位耦合** —— `priority` 每次重探都可能变，顶层池跟着变，预算也要跟着调
2. **判据要看见** —— 给个数字让你点确认，出事了你也不知道当时为什么改成这个数
3. **改值保留注释** —— 只改 YAML 的值，键序 / 注释 / 空行 / 缩进逐字保留

**对比**：

| | CLI `diag403.py` | 图形面板 |
|---|---|---|
| 输入 | 手工指定文件路径 | 自动读 `.env` 的 `CONFIG_PATH` |
| 输出 | 文本报告 | 改动预览 + diff |
| 操作 | 只读，不能写回 | 预览 → 确认 → 自动备份 → 写回 → 触发 CPA 重载 |
| 适合 | CI / 定时巡检 | 人工调优、快速修正 |

### gemini 段仍无解

顶层 `golf` 实测 8 次全 403（含经代理），该段其余站逐站实测全部 503/401/404。
**无论怎么调档都会失败** —— 需要补新上游，或该站方恢复。

---

## 探测速度

用户实测：单站四段曾要 10 多分钟。**慢在等待与冗余请求，不在计算** ——
换语言或单纯加线程救不了，瓶颈全在 I/O 与刻意的节流。已做四处：

| 改动 | 原状 | 现状 |
|---|---|---|
| 节流分桶 | 四段共享一个 gap 桶，单站 56 次请求串成 55×3s = **165 秒纯睡** | 按 `(host, section)` 分桶，同段内仍严格保持 gap |
| 四段并行 | 串行，总时间 = 四段之和 | 并行，总时间 = 最慢那一段 |
| 候选并行 | 多站串行 | 多站并行，上限取**不同主机数** |
| 上下文探测 | 先小后大，最多 6 次百万字符请求 | hi-first + 读错误正文里上游自报的上限，命中即 1 次 |

同主机多 Key 另有 `(host, section)` 的 **single-flight**：形态学习（最贵的动作，
最多 12 次请求含 4 次大 body）只做一次，其余 Key 只验凭证本身。

**并行不会放松任何站的限频。** 站方的 bulk probe guard 是按端点计的，四段打的是四个
不同路径（`/v1beta/models/…:generateContent`、`/v1/responses`、`/v1/messages`、
`/v1/chat/completions`），分桶后同段之间的 gap 一点没松。

万一撞上按账号（而非按端点）全局限频的站：把 `--gap` 调大即可，不需要退回串行。
要退回串行也行 —— 网页端取消勾选「并行探测」，命令行 `--workers 1 --candidate-workers 1`。

---

## 去重

四段行为**不一致**，两种失败模式相反，都必须挡：

| 段 | CPA 配置层行为 | 重复导入的后果 |
|---|---|---|
| `gemini-api-key` | 按五元组判重，**静默丢弃**冲突者 | 你以为加了，实际少一个 |
| 其余三段 | **完全不判重** | 注册成两个独立凭据，轮询池占两位 |

加 `-N` 后缀发生在另一层（Auth 合成时，`synthesizer/helpers.go:44-50`），不是配置层去重。

服务在写回前自己按五元组判重，且**批内**也判 —— 同一批粘贴里重复两行同样挡住。

---

## `max-context-length`

字段在四段的 Model 结构体都有（`config_types.go:430/526/623/716`），但当前 `config.yaml` 里
678 个模型条目**全部为空**。

作用链（不参与选站，只告诉客户端窗口有多大）：

```
config.yaml  models[].max-context-length
  → service_models.go:702-706     info.ContextLength / MaxContextLength
  → model_registry.go:1242        "max_context_length"
  → codex/models/models.go:207-211  context_window / max_context_window
  → Codex 客户端 /models 响应 → 客户端据此定自动压缩阈值
```

**这就是那条 400 的正解。** 客户端按 `[1M]` 算窗口、到 967k 才压缩，而 hotel 真实只吃
995,988 —— 只剩 3% 余量；且 400 不在 `isCredentialRetryRoundStatus` 白名单
（`conductor_selection.go:1038` 只含 403/408/429/500/502/503/504），命中即终止本轮，
其余上游一次都不试。写入真实上限后客户端在正确的点压缩，不必改客户端设置。

默认开启，只对本次新增的候选跑一次。二分带截断校验：200 但 `input_tokens < 发送量×50%`
说明上游截了，那个 200 不算通过（relay-m 发 105 万字符只回 132,696 tokens）。

---

## 安全

- 服务持有**明文上游 Key** 且能改写 `config.yaml`，等价于 CPA 写权限。
- 默认只绑 `127.0.0.1`。要外网访问请走 nginx 加 TLS + 访问控制，**不要**把 `--host` 改成
  `0.0.0.0` 直接暴露。
- 强制 Bearer token，无免鉴权模式。
- 完整 Key 只在内存；落库、日志、API 响应一律脱敏（`sk-abc...6789`）。
- 写回必须两步：先 `/api/plan` 拿 `plan_id`，再 `/api/apply` 带同一个 id + `confirm=true`。
- 并发保护：`config.yaml` 在生成方案后被改过则拒绝写入，要求重新生成。

### 2026-09-05 这一批加固

一轮安全审计（子代理，只读）报了 12 处，逐条核实后修了下面这些。每一处都跟了
**撤销验证**：把修复改回旧行为，确认对应断言真的变红——否则那条断言等于不存在。

#### 未认证可打的拒服务（唯一不需要登录的）

服务只在 `127.0.0.1:8765` 监听、由 nginx 反代，于是 `client_address`
对**每一个**访客都是 `127.0.0.1`。失败封锁按它索引 → 整张表只有一个桶：

```
任何人对任意路径连发 5 次带假 Bearer 的请求
  → 之后 30 分钟运维本人也进不来（_authed 在比对密钥【之前】就查封锁）
  → 容器 restart: "no"，不自愈
```

每 30 分钟重打 5 次即永久封锁，且它宣称的暴破防护对真实攻击者完全无效
（换 IP 与不换等价）。

修法是读 `X-Forwarded-For` 的**最右一跳**——nginx 用
`$proxy_add_x_forwarded_for`，链条是 `<客户端可伪造>, <nginx 看到的真实对端>`。
取最左（常见写法）等于让客户端自己声明 IP，那比不读还糟。只在直连对端是回环时
才信这个头；绑 `0.0.0.0` 直接暴露时对端就是客户端，那时任何人都能伪造。

同一批必须加的是**封锁表的容量上限**：修好真实 IP 之后这张表的键从「恒为
127.0.0.1」变成攻击者可控，不加上限就是把一个 DoS 换成另一个。淘汰顺序也是判据
——`(是否在封锁中, last)` 升序，否则攻击者能用大量新 IP 把自己的封锁记录挤掉。

#### 全量重探的预览把 177 行明文凭据回给了浏览器

`server.py` 开头自述「完整 key 不进 JSON 响应（一律 masked）」，而全量重探那条路的
diff **就是重建后的整个文件**——不是增量片段。生产配置里 177 行 `api-key:` 明文 +
1 行 `secret-key`，共 349KB 全部进浏览器 DOM，界面的「复制」按钮还会把它们写进
系统剪贴板。

修法是 `redact_yaml_secrets(text)`：按行脱敏，**只动值不动结构**。行号、缩进、
注释、其余字段全保留，diff 仍然完全可读。落盘走的是服务端内存里的原文，不受影响。

为什么不改成「不给全文」：全量重探的价值就在于「写回前看清整个文件会变成什么样」，
给一段掐头去尾的片段等于把这个功能废掉。

#### 并发与预算参数无上限

`max_workers` / `timeout` / `gap` / `swap_samples` 原来只做类型转换：
`{"full_redetect":true,"max_workers":50000}` 就是 5 万个站级线程，每个内部再开
最多 4 个段线程。后果不止本机资源——把大量出网请求打向 121 个第三方站，
可能触发站方的批量探测防护，**代价落在真实凭据上**（封号），那比服务挂掉更贵。

现在集中在 `_LIMITS` 表里钳制，并给输入行数设了 500 的上限。静默钳制而不报 400：
这些值多半来自前端滑块，用户手打一个大数字时更希望「按上限跑」；真正的攻击者
也一样被压到上限。

#### 内网去向

探测目标 URL 与代理地址都来自请求体，而校验只看形态不管去向。两条路都带回显，
其中代理那条更干净——`probe_proxy` 做**裸 TCP 连接**，把连通性、异常类名
（ConnectionRefused/timeout）、毫秒数经 `proxy-precheck` 事件回到 `/api/job`。

`is_private_target(host)` 挡回环、10/8、172.16/12、192.168/16、169.254/16、
IPv6 的 `::1`/`fc00::/7`/`fe80::/10`/`::ffff:` 映射，以及 `metadata.google.internal`
这类云元数据主机名与 `.local`/`.internal` 后缀。

**这道闸挡的是内网侦察，不是全部 SSRF**：它只看字面量地址，挡不住 DNS rebinding
（解析时公网 IP、连接时变私网）。要防那个得在 `client.py` 接管地址解析，代价不小。
这一点必须写清，不能让人以为它挡住了一切。云元数据端点本来也打不到——出网路径
总在 base 后追加固定后缀，拼不出 `/latest/meta-data/...`。

本项目自己的假上游套件与端到端脚本打 `127.0.0.1`，所以留了
`allow_private=True` 开关，生产的 HTTP 入口不传它。

#### 上游返回的模型名会被拼进出网 URL

模型名来自站方的 `/models` 目录（第三方完全可控），而它被直接拼进
`f"{base}/v1beta/models/{model}:generateContent"`。实测通过原来全部闸门的名字：

| 名字 | 后果 |
|---|---|
| `../../../gemini-3.1-pro` | 逃出 base 路径，打到同主机别的端点 |
| `gemini-3.1-pro-x?a=b` | `:generateContent` 落进 query，实际请求的是另一个端点；它回 200 就成了「该模型可用」的伪证 |
| `gemini-3.1-pro.%2e%2e%2fadmin` | 编码过的路径穿越 |

而且**不需要拿到 200**：方案的 catalog 分支把目录里的名字直接当候选，
这串字面量会进 `config.yaml`，CPA 用同样的方式拼 URL 再发一次（它也是裸拼）。
本工具是这条链上唯一有机会校验的一环。

关键约束是 **`/` 必须允许**——生产配置里 85 个模型名有 `Business/gemini-2.5-pro`、
`anthropic/claude-opus-5` 这种分组前缀（实测那 85 个名字用到的非字母数字字符只有
`-` `.` `/` 三个，最长 34 字符）。所以判据是「只许这三个符号 + 禁止路径穿越
+ 禁止 query/fragment 起始字符」，不是简单的「不许有 `/`」。

三层拦：目录解析入口就丢掉（并在事件流里说出来，静默丢站方数据不好）、
两个段级闸门、以及真正拼 URL 的 `build_request` 抛异常。最后一层是为了
「将来加新调用路径时，忘了过上游闸门也不会漏出去」。

#### 其余

- **traceback 不再进响应体**：换成 `err-3f2a1b` 这样的引用 id，完整栈只进 stderr。
  它原来也会进 `/api/job` 事件流**与 `/api/export` 的 txt**，而那个 txt 的设计
  用途就是「贴给别人看」。`format_exc` 不含局部变量所以不吐密钥值，泄露的是容器内
  文件布局与行号。
- **`do_GET` 加兜底**：`?since=x` 原来让 `ValueError` 冒到 socketserver，客户端拿到
  **连接重置**而不是 400，前端会把它计入「轮询失败」无限重试。现在畸形值回 400
  （不是 500——5xx 会被前端当「服务挂了」而重试）。
- **`push.base` 白名单**：`reload_cpa` 的请求体是整份 config.yaml、头里带管理密码。
  地址原来完全由请求体决定，填错一次就是把 177 行明文凭据发给第三方——**而这件事
  已经发生过一次**（前端那个输入框曾硬编码一个公网域名，那次请求确实出了公网，
  只是被 Cloudflare 挡在 403）。现在只放行回环、私网、docker 服务名，或服务端
  `--cpa-url` 配置的那个 host。
- **部署模板补 CSP 与四个安全头**（`deploy/nginx-snippet.conf`）。当前前端没有可
  利用的注入点（274 处 `esc()`、36 处 `innerHTML` 全核对过），所以这是纵深防御；
  但这个页面渲染的是**完全由第三方上游控制**的正文摘要、模型名、错误消息。
  CSP 必须写成**一行**——nginx 不支持反斜杠续行，续行会把真实换行塞进 HTTP 头值。
- **`access_log` 的替代方案改对了**：原来注释里给的 `combined` **仍然记
  `$request`**，也就是仍然把 `?token=` 写进日志。换成记 `$uri` 的自定义
  `log_format`，并注明别用 `$request_uri`（它带 query，等于没改）。

---

## 红线

1. 完整 api-key 不落库、不进日志、不出现在响应体。
2. 不修改任何现有条目的 `priority`，只在空档插入。
3. 不自动使用 `weight: 0` —— `positiveWeightAuths` 会把 weight≤0 的凭据从池里滤掉，
   导致 `auth_not_found: no auth candidates`（尝试=0，是消失而非降权）。
4. 写回前必须备份。
5. diff 确认是硬闸门。
6. 单 key 问题只处置该 key，不波及同站其他凭据。
7. 余额类永不降权 —— 充值即自愈。

---

## API

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/context` | 当前 config.yaml 规模与四段档位谱 |
| POST | `/api/parse` | 解析文本，返回有效/无效行（不发请求） |
| POST | `/api/probe` | 创建探测任务，返回 `job_id` |
| GET | `/api/job/<id>?since=N` | 轮询进度与事件流 |
| POST | `/api/plan` | 生成写入方案 + diff，返回 `plan_id` |
| POST | `/api/apply` | 落盘（需 `plan_id` + `confirm=true`） |

进度走 HTTP 轮询，不引入 SSE/WebSocket —— 与 CPAMP 现有做法一致。

---

## 依赖

**Python 3.9+**。代码里的 `X | None` 全在类型注解里，且每个模块都有
`from __future__ import annotations`（注解不求值），运行时没有 3.10 专属语法。
CentOS 9 自带的 3.9 直接够用，不必装第二个解释器。

**PyYAML 是硬依赖**（`server.py` 与 `cli.py` 启动时就要读 `config.yaml`）：

```bash
dnf install -y python3-pyyaml     # 别用 pip，会污染系统 Python
```

只有 `writeback.validate()` 那一处是可选的 —— 未装时跳过本地 YAML 语法校验，
但那时服务本身已经起不来了，所以实际上必须装。其余全部标准库。
