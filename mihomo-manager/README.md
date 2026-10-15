# mihomo 订阅管理工具

自动化管理 mihomo 代理订阅配置的工具集。

## 文件说明

- **mihomo-subscriptions.conf**: 旧宿主挂载脚本流程使用的订阅配置文件。VPS 镜像部署在共享 `/opt/deploy/.env` 中填写 `MIHOMO_SUB_*`、`MIHOMO_SUBSCRIPTIONS` 和 `MIHOMO_SECRET`，不以此文件作为配置入口。
- **bootstrap-mihomo.sh**: 由 mihomo-init 容器执行，把模板物化进共享卷
- **mihomo/config.template.yaml**: 配置模板，全 `${ENV}` 占位符，无凭据
- **mihomo/healthcheck.sh**: 给 **mihomo 容器**用的健康检查（纯 busybox）
- **mihomo/healthcheck.py**: 给 **本镜像自己**用的健康检查（python3）
- **update-mihomo-subscriptions.sh**: 自动更新脚本
- **README.md**: 本文档

### 为什么有两份 healthcheck

两个容器的可用解释器不同，2026-09-19 实测：

| 容器 | 有 | 没有 |
|---|---|---|
| `metacubex/mihomo:latest`（busybox） | `wget` `nc` `sed` `awk` `grep` `wc` `head` `tr` | `curl` `python` `python3` `bash` `jq` |
| `cpa-upstream-importer`（python:3.12-slim） | `python3`（标准库 urllib） | `curl` `wget` |

所以 `healthcheck.sh` 只用 busybox 自带件（HTTP 请求靠 `nc` 手写），
`healthcheck.py` 用 urllib。`.py` **不能**在 mihomo 官方容器里跑（`python3: not found`）。
`.sh` 两边都能跑：本镜像的 Dockerfile 装了 busybox 与 wget，`.sh` 会先找
`nc`/`wget`，找不到就回退到 `busybox <applet>`（2026-09-19 起 compose 里
mihomo-proxy 的 healthcheck 调的就是 `.sh`）。

`bootstrap-mihomo.sh` 每次启动都把两份一起同步到共享卷（`healthcheck.sh`
`healthcheck.py`），各自被自己的容器按文件名调用。

### healthcheck.sh 做什么

健康检查判活，并在代理出口不可用时降级到直连 —— 这个脚本**两个项目共用**
（`cpa2sub2api` 与本项目），`cpa2sub2api` 依赖其中的可切换出口来绕开按 IP
拦截的上游站点，所以降级能力不能去掉。

1. 请求 9090 API 的 `/version`，拿不到 HTTP 状态行即判进程异常，退出非零。
2. 读 `AUTO` 组的节点数，再经 7890 代理实际访问一次探测地址。节点数大于 0
   不等于能出网，所以必须真发一次请求。
3. 代理能出网：若上一轮处于 `DIRECT`，把 `MIHOMO_GROUP`（默认 `PROXY`）切回
   `AUTO` 并更新状态文件；退出 0。
4. 代理出不去：把该组切到 `DIRECT` 并记状态，然后试直连。直连可用即退出 0
   —— 降级本身不算失败，报 `unhealthy` 会让 `depends_on` 的服务连不上，反而
   放大故障。直连也不通才退出非零。

API 存活不代表代理出口可用。经代理那次探测会显式指定代理并清空
`no_proxy` / `NO_PROXY`，避免绕过规则让直连成功掩盖代理故障。

<!-- 2026-10-13：本节曾被改写成「只读、不发 PUT、不切换 DIRECT/AUTO」，
     并声称 MIHOMO_STATE_FILE / MIHOMO_GROUP「不再使用」。那与共用契约矛盾：
     同一份脚本由共享 compose 的单个 mihomo 容器执行，而 cpa2sub2api 的
     README 把「可切换的代理出口」写在第一行当核心用途。已按两项目共用的
     基线恢复，并保留此后新增的 30 秒硬上限与 no_proxy 清除。 -->

**对探测任务的影响**：降级把整条代理链路切成直连，此时上游看到的是本机真实
出口 IP。本项目的「失败时试代理」依赖代理真的在代理，所以探测前应确认组选路
（`GET /proxies/<组名>` 的 `now` 字段）与状态文件一致；处于 `DIRECT` 时
`via-proxy` 处置等同直连，不能据此断定「挂代理也不通」。

`restart: unless-stopped` **不会因为 unhealthy 自动重启容器**。

shell 脚本使用系统 `timeout` 或 BusyBox `timeout`，总期限 30 秒，必要时再等待最多 2 秒强制结束，低于 Compose 的 40 秒上限。检查通过 `setsid` 建立独立进程组，结束后清理残留的网络请求子进程；缺少期限或进程组工具时直接失败。Python 脚本使用独立子进程，30 秒超时后终止并回收子进程，覆盖 DNS 阻塞与持续缓慢响应。

shell 可调环境变量：`MIHOMO_API_HOST/PORT`、`MIHOMO_PROXY_HOST/PORT`、`MIHOMO_SECRET`、`MIHOMO_PROBE_URL`、`MIHOMO_STATE_FILE`（默认 `/tmp/mihomo-egress-state`）、`MIHOMO_GROUP`（默认 `PROXY`，即要切换的组名）。Python 对应使用完整 URL：`MIHOMO_API` 与 `MIHOMO_PROXY`，其余变量相同。`MIHOMO_SECRET` 必须与 mihomo API 鉴权一致；不配置时不发送 Authorization，此时同网络内任何容器都能切换出口，共享部署务必配置。API 密钥不会转发至出网探测地址。

bootstrap 启动时会刷新挂载目录中的两份检查脚本。更新镜像后应重建 bootstrap 与代理服务，确保持久卷没有继续运行旧脚本。

手动执行只读检查：

```bash
docker compose exec mihomo sh /root/.config/mihomo/healthcheck.sh; echo rc=$?
```

## VPS 部署（镜像方式，推荐）

VPS 只拉镜像，宿主上不放 mihomo 配置文件。mihomo 二进制、地理数据、模板和
`bootstrap-mihomo.sh` 都在 `swhesong/cpa-upstream-importer` 镜像里。compose 里
两个服务都用这个镜像：

| 服务 | 做什么 | 生命周期 |
|---|---|---|
| `mihomo-init` | 执行 `bootstrap-mihomo.sh`，把模板按订阅展开成卷里的 `config.yaml` | 跑完即退 |
| `mihomo`（本仓库 compose 里叫 `mihomo-proxy`，网络别名 `mihomo`） | 运行 `mihomo -d <卷>` | 常驻 |

### 首次配置

1. 在 `/opt/deploy/.env` 写订阅（二选一，可混用；同名时 `MIHOMO_SUB_<名称>` 优先）：

   ```bash
   # 写法一：每个订阅一个变量，名称部分就是 provider 名（自动转小写）
   MIHOMO_SUB_WOG=https://sub.example.com/api/v1/client/subscribe?token=xxxx
   MIHOMO_SUB_WOGB=https://sub2.example.com/link/yyyy?clash=1
   # 写法二：一行写全，分号分隔
   MIHOMO_SUBSCRIPTIONS=wog=https://sub.example.com/...;backup=https://sub2.example.com/...
   # 9090 管理 API 的密钥，建议必填
   MIHOMO_SECRET=换成一串随机字符
   ```

   provider 名、两个节点组的 `use:` 列表、订阅域名的 DIRECT 规则都由这里的
   订阅清单生成。模板里的 `wog` / `wogb` 只是示例：**一个订阅都不配**时，
   bootstrap 会报「未展开的占位符」并以非零状态退出，mihomo 不会启动。

2. 启动并看结果：

   ```bash
   cd /opt/deploy
   docker compose pull mihomo-init mihomo
   docker compose up -d mihomo-init mihomo
   docker compose logs mihomo-init        # 应看到「订阅 N 个: ...」「自举完成」
   docker compose ps mihomo               # 约 1 分钟后应为 healthy
   ```

### VPS 更换订阅

1. 改 `/opt/deploy/.env` 里的 `MIHOMO_SUB_*` 或 `MIHOMO_SUBSCRIPTIONS`。
2. 重跑 init，先检查它成功退出，再更新代理：

   ```bash
   cd /opt/deploy
   docker compose up -d --force-recreate mihomo-init
   docker compose logs mihomo-init        # 确认自举完成，无初始化错误
   docker compose ps -a mihomo-init       # 确认退出码为 0
   ```

   初始化失败时先修复错误，不要继续更新代理。确认成功后，如果仅更换订阅，执行 `docker compose restart mihomo`。如果同时修改了 `MIHOMO_SECRET`，或更新了镜像、健康检查脚本，则执行：

   ```bash
   docker compose up -d --force-recreate mihomo
   ```

   只重启旧容器不会重新读取 `.env` 中的密钥；当前健康检查从容器环境读取 `MIHOMO_SECRET`，必须通过重新创建同步。更新镜像前先执行 `docker compose pull mihomo-init mihomo`，再按上述顺序运行 init 和重建代理。这里的 `mihomo` 是共享部署服务名；独立 compose 使用 `mihomo-proxy`。共享代理重建会短暂影响两个项目，应先完成正在运行的探测和写回；保留共享卷，不要删除它。

bootstrap 在卷里记着订阅相关变量（`MIHOMO_SUB_*`、`MIHOMO_SUBSCRIPTIONS`、
`MIHOMO_SECRET`）的 sha256 指纹（`.bootstrap-fingerprint`，只存哈希）。指纹变了
就先备份旧配置（`config.yaml.backup-时间戳`），再按新订阅重建。指纹没变就保留
现有配置，探测脚本写进 AUTO 组 filter 的结果也跟着保留。

- 2026-09-27 之前的老卷里没有指纹文件。这类卷**第一次**启动只补记指纹、不重建，
  所以升级后第一次改订阅要加 `MIHOMO_FORCE_REBUILD=1`：

  ```bash
  MIHOMO_FORCE_REBUILD=1 docker compose up -d --force-recreate mihomo-init
   docker compose logs mihomo-init
   docker compose ps -a mihomo-init
  ```

   确认 init 成功退出后，再按上面的规则重启或重新创建代理。

  改完就把 `.env` 里的 `MIHOMO_FORCE_REBUILD` 清空。否则之后每次启动都会重建，
  探测结果每次都被冲掉。
- 查当前生效的订阅：`docker compose exec mihomo sh -c 'grep -A1 "^  [a-z0-9_-]*:$" /root/.config/mihomo/config.yaml | head'`
  （路径以 compose 里的 `MIHOMO_TARGET_DIR` 为准）。

## 快速开始（宿主机目录挂载的老部署）

下面这套用 `update-mihomo-subscriptions.sh` 改宿主机上的 `mihomo/config.yaml`，
只适用于「mihomo 配置目录挂在宿主机」的老部署。按上一节用镜像部署的，不需要这套。

### 1. 配置订阅

编辑 `mihomo-subscriptions.conf`，每行一个订阅，格式：

```
名称|订阅URL
```

示例：
```
wog|https://vpn.example.com
wogb|https://sub.example.net/api/v1/client/subscribe?token=YOUR_TOKEN
```

### 2. 运行更新脚本

```bash
cd /opt/deploy/upstream-importer/mihomo-manager
bash update-mihomo-subscriptions.sh
```

### 3. 重启 mihomo

```bash
cd /opt/deploy
docker-compose restart mihomo
```

### 4. 验证

```bash
# 检查订阅是否拉取成功
curl -s http://127.0.0.1:9090/providers/proxies | jq 'keys'

# 检查 AUTO 组当前节点
curl -s http://127.0.0.1:9090/proxies/AUTO | jq '{now: .now, all: (.all | length)}'
```

## 脚本功能

`update-mihomo-subscriptions.sh` 自动完成：

1. ✓ 读取 `mihomo-subscriptions.conf` 中的订阅列表
2. ✓ 生成 `proxy-providers` 配置（type: http）
3. ✓ 更新 `proxy-groups` 的 `use` 列表（PROXY 和 AUTO 组）
4. ✓ 更新 `rules` 中的 DIRECT 规则（订阅域名必须直连）
5. ✓ 自动备份原配置文件

## 添加新订阅

1. 在 `mihomo-subscriptions.conf` 添加一行：
   ```
   新订阅名|https://example.com/subscribe?token=xxx
   ```

2. 运行更新脚本：
   ```bash
   bash update-mihomo-subscriptions.sh
   ```

3. 重启 mihomo：
   ```bash
   docker-compose restart mihomo
   ```

## 删除订阅

1. 从 `mihomo-subscriptions.conf` 删除对应行
2. 运行更新脚本
3. 重启 mihomo
4. 可选：删除缓存文件 `rm /opt/deploy/mihomo/providers/订阅名.yaml`

## 注意事项

- **订阅域名自动直连**：脚本会自动为每个订阅的域名添加 `DOMAIN,域名,DIRECT` 规则，避免"要先有代理才能拉到代理"的死循环
- **自动备份**：每次运行脚本都会备份原配置到 `config.yaml.backup-时间戳`
- **服务器路径**：脚本使用 `/opt/deploy` 作为部署根目录

## 故障排查

### 订阅拉取失败

```bash
# 查看 mihomo 日志
docker logs mihomo --tail 50

# 手动测试订阅 URL
curl -I "订阅URL"
```

### 节点未出现在 AUTO 组

```bash
# 检查 provider 是否加载
curl -s http://127.0.0.1:9090/providers/proxies/wogb | jq '{
  name: .name,
  type: .vehicleType,
  count: (.proxies | length)
}'

# 检查是否被 filter 过滤
grep "filter:" /opt/deploy/mihomo/config.yaml
```

### mihomo 启动失败

```bash
# 检查配置文件语法
docker run --rm -v /opt/deploy/mihomo:/root/.config/mihomo \
  metacubex/mihomo:latest -t

# 恢复备份
cp /opt/deploy/mihomo/config.yaml.backup-最新时间戳 \
   /opt/deploy/mihomo/config.yaml
```

## 与探测脚本配合

mihomo-manager 负责**订阅管理**，`probe-upstreams.py` 负责**节点探测和筛选**：

1. mihomo-manager 更新订阅配置
2. mihomo 拉取所有节点
3. probe-upstreams.py 探测哪些节点未被目标站拦截
4. probe-upstreams.py --apply 写入 filter 到 AUTO 组

## 相关文档

- mihomo 配置参考：`/opt/deploy/mihomo/config.yaml`
- CPA 代理配置：`/opt/deploy/config.yaml` 的 `proxy-url` 字段
- upstream-importer 主文档：`/opt/deploy/upstream-importer/README.md`
