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

```bash
python3 tools/diag403.py /path/to/config.yaml    # 结构性诊断（只读、零请求）
python3 tools/recheck.py /path/to/config.yaml --top-only   # 复核既有凭据
```

---

## 发布到 GitHub 与 Docker Hub

### 提交前先隐去域名

```bash
python3 tools/scrub_domains.py --dry-run   # 看会改什么
python3 tools/scrub_domains.py            # 真改（自动备份）
python3 tests/run.py                      # 必须仍全过
```

要隐去的有两类，**第二类最容易漏**：

- **自有域名** —— 客户端入口、管理面板、本服务自身、以及自己那台机器
  作为上游时的域名。同一个域名四种角色，替换目标各不相同。
- **第三方中转站域名** —— 这些是排障案例的主体。哪个站 403、哪个静默
  换模、哪个按出口 IP 拉黑，公开出去等于公布「我在用这些站、它们各自
  什么毛病」。它们散在 20 个文件里，`grep` 一遍远比想象的多。

替换不是简单 sed，有三个性质必须在替换后仍然成立，改规则前先读脚本的
docstring：

1. **占位符字母按原名字母序分配**。`test_probe.py` 有断言把运行时
   `sorted()` 的结果和源码里的字面量比对，字母乱分配会让两边排序不一致 ——
   测试失败，而原因跟被测逻辑毫无关系。
2. **短名 == 域名首标签**（`relay-f` / `relay-f.example`）。
   `host_matches_note` 的无别名表兜底靠这个性质。
3. **但有一对故意不共享子串**（`jdw` / `relay-h.example`）。
   `test_tiering.py` 那条「必须靠别名表才认得」的断言靠它。
   只顾 2 不顾 3，那条断言就恒真而不再验证任何东西。

不替换 `aliyuncs` / `oaipro` / `oaiproxy` —— 那三个是测试里的**前缀误匹配
反例**（`aliyuncs.startswith(aliyun)`），换成占位符后断言恒真。

**`docs/cpa-atlas.html` 与 `graphify-out/` 已在 `.gitignore` 里**，不进公开
仓库，所以也不参与替换：前者的价值就在于点名真实站点，换成占位符等于把
排障记录变成一堆无法对应现实的字母；后者是代码图谱生成物，它把源码字符串
抄进 JSON，会绕过替换（实测 `graph.json` 里就留着一个真实站名）。

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

docker buildx build   --platform linux/amd64,linux/arm64   -f deploy/Dockerfile   -t <你的仓库>/cpa-upstream-importer:latest   --push .

# 确认 manifest list 里真的有多个平台
docker buildx imagetools inspect <你的仓库>/cpa-upstream-importer:latest
```

加 `linux/arm/v7` 会慢很多 —— `bcrypt` 在那个平台没有预编译 wheel，
要现场用 Rust 编译。Dockerfile 按 `TARGETPLATFORM` 自动判断，
amd64/arm64 跳过编译工具链的安装。

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

## 目录

```
cpa-upstream-importer/
├ cpa_probe/          共享判定库（9 模块，无第三方依赖，PyYAML 可选）
│  ├ parse.py         解析 url,key；按段规范化 base-url
│  ├ classify.py      响应定性：余额/封号/限流/门禁/IP封/反测活/死路/临时/注入
│  ├ request.py       按段构造请求，路径与 CPA executor 对齐
│  ├ client.py        统一 HTTP 传输（原三脚本用了三种底层）
│  ├ fingerprint.py   后端 id 指纹、静默换模判定、截断校验
│  ├ pipeline.py      四阶段探测编排 · 段/候选并行 · single-flight 形态复用
│  ├ plan.py          去重、priority 定档（读 weight:0 与实测注释）、影响面
│  └ writeback.py     行级 YAML 编辑（保注释）、备份、diff、重载 CPA + 读回校验
├ server.py           HTTP 服务（标准库，VPS 免装依赖）
├ cli.py              命令行入口
├ web/                前端：index.html + app.js
├ docker-compose.yml  独立部署模板（全部走 .env 变量，零硬编码）
├ .env.example        环境变量样例（CONFIG_PATH 必填）
├ .github/workflows/  CI：3 个 Python 版本跑测试 + 多架构镜像发布
├ LICENSE             MIT
├ CONTRIBUTING.md     贡献指南
├ tests/              回归测试（八个套件，零外网请求，自带最小样本）
│  ├ run.py           跑全部，退出码 0/1，可接 CI
│  ├ fixture_cfg.py   自带的最小 config.yaml（各套件共用；不传路径时就用它）
│  ├ test_probe.py    解析/判定/指纹/去重/定档/影响面/写回
│  ├ test_server.py   HTTP 契约：鉴权/封锁/静态/路径穿越/写回闸门/非 ASCII 密码
│  ├ test_pipeline.py 假上游端到端：四阶段编排 + 性能 + 事件流
│  ├ test_edges.py    写回边界 12 形状：同站 100 Key / 撞已有站 / prefix
│  ├ test_reload.py   生效链：inode 恒定 / 读回校验 / 403-CF 与状态码分类
│  ├ test_speed.py    并发正确性：节流分桶 / single-flight / 缓存失效
│  ├ test_web.py      前后端静态契约：DOM id / 事件 / 重试 / 地址 / 验证不跳过
│  └ test_tiering.py  定档算法：分数生效 / 死站零代价 / 别名表 / 逐轮自查缺陷
├ tools/
│  ├ recheck.py       复核**既有**凭据：按各站声明的模型 + CPA 的真实转发头
│  ├ diag403.py       403 结构性诊断：预算 vs 顶层池、单点、档位落差
│  ├ rehearse.py      整链演练（假 CPA，零外网）
│  └ scrub_domains.py 把自有域名**与第三方中转站域名**换成占位符（发布前用）
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
└ legacy/             原探测脚本，原样保留可继续单独使用
   ├ audit-upstreams.py    逐组合可用性审计 · HTML 报告
   ├ probe-fix.py          四问诊断：基线→换模→最小必需头→代理
   ├ context-probe.py      二分探测上下文上限
   ├ swap-watch.py         换模率采样
   ├ probe-upstreams.py    mihomo 节点探测 · AUTO 组 filter 回写
   └ logs-digest.sh        错误日志一行摘要
```

`config.yaml` / `docker-compose.yml` / `.env` / `nginx.conf` / `mihomo/` **仍在 `/opt/deploy` 根**——
`docker-compose.yml` 里有 `./config.yaml`、`./mihomo`、`.env` 三处相对挂载，移动会破坏部署。

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

所以这一层只能用不占用该头的方式：**nginx IP 白名单**（`allow`/`deny`），
或 Cloudflare Access（走 cookie）。片段里用的是白名单。

两个方案怎么选：

| | SSH 隧道 | nginx 反代 |
|---|---|---|
| 公网暴露 | 无 | 有（一个域名） |
| 要配证书 | 不要 | 要（沿用通配证书） |
| 要改 nginx.conf | 不要 | 要（1757 行的生产文件） |
| 手机能用 | 不方便 | 能 |
| 每次操作 | 先开隧道 | 直接输网址 |
| 出口 IP 变动 | 无影响 | 白名单失效，要改配置 |

偶尔加几个账号 —— 用隧道。经常用或要手机看 —— 配反代，
但 TLS + IP 白名单 + 长随机 token 三层缺一不可。

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

这件事有实际后果：`config.yaml` 里 **23 个凭据**配了 `proxy-url`
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

12 个现存站点、206 个条目零例外。

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

---

## priority 定档

**数值大者优先，且分层隔离**（`sdk/cliproxy/auth/selector.go:325-333`）。
低档凭据只在更高档**全部**不可用时才参与 —— 插错档不是「略微靠后」，而是永远轮不到。

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

现在这条警告还会区分被挡的是活站还是死站 —— 挡 9 个死站与挡 9 个活站
完全是两件事，只报数字会误导。

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

> 这条提示是踩过坑加的：codex 段的 `relay-c`(800) 落差只有 100，
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

不带任何头直连测 `relay-c` 的 codex 段，21 个组合全部 **401
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
7 个可用 key**。CPAMP 面板上 relay-c 显示 100% 成功率而客户端仍报错，
成因就是这个。

> **提档的教训**：我当天下午基于「实测 200、3.59 秒」把 relay-m 从 180
> 提到 900，晚间它就全 502 了。**提档依据是「此刻这一次请求成功了」，
> 不是「这个站可靠」。**单点站（该段只有 1 个 key）提到顶层风险特别高 ——
> 它一挂整层就空，下面的可用站要等冷却期过完才轮到。
> 提档到顶层前应确认：① 多次复测稳定 ② 该站在本段有多个 key。

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

实测抽样印证：同一站内凭据状态不一致（`relay-h` 3 个里 1 个 200、1 个 403 门禁、1 个超时）。
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

claude 顶层 26 个多余的 key 降到 **990** —— 仍高于 950 的 `relay-a`
（实测超时 90 秒），所以那个坏站仍轮不到。

`max-retry-interval` 这一改**推翻了**注释里 2026-08-27 那条「30 秒会被客户端读超时
先打断」的判断。那条在「宁可快速失败」的目标下成立；挂机场景下等待优于失败。

为 0 时的实际行为（`conductor_selection.go:977-982`）：

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

### 用 diag403.py 自查

```bash
python3 tools/diag403.py /opt/deploy/config.yaml
```

它会算出「预算 vs 顶层池」够不够，以及每段是不是单点。

### gemini 段仍无解

顶层 `relay-g` 实测 8 次全 403（含经代理），该段其余站逐站实测全部 503/401/404。
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

**这就是那条 400 的正解。** 客户端按 `[1M]` 算窗口、到 967k 才压缩，而 relay-h 真实只吃
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
