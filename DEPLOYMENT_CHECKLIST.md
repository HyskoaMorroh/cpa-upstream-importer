# VPS 部署与验收检查清单

本清单适用于 `/opt/deploy` 中两个项目复用 mihomo 的部署。VPS 拉取已发布镜像；真实 `.env`、完整 `docker-compose.yml`、`nginx.conf`、`config.yaml` 留在部署目录。不要用仓库的独立模板覆盖这些共享文件。

本文是操作说明，不代表当前候选镜像已完成验收。记录实际镜像摘要、网页结果和写回回读后，才能确认本次升级是否通过。

## 一、配置与升级前检查

### 环境变量

在可信本机编辑器中修改 `/opt/deploy/.env`，保留另一个项目的所有变量。Compose 的 `.env` 不执行 shell 命令：不要把 `$(openssl ...)` 或 `$(stat ...)` 当作变量值粘贴进去。

| 变量 | 填写与核对方法 |
|---|---|
| `IMPORTER_TOKEN` | 本地生成并保存随机访问令牌，填写实际值；不贴进报告、截图或共享终端记录 |
| `IMPORTER_UID`、`IMPORTER_GID` | 与挂载的 `config.yaml` 属主一致；用下方 `stat` 查看数字 |
| `CPA_MANAGEMENT_PASSWORD` | importer 使用的 CPA 管理凭证；填写管理密码，不用已哈希的 `secret-key` 代替 |
| `CPA_MANAGEMENT_KEY` | 若另一个项目显式设置此项，保留其现有值及共享 compose 的回退关系 |
| `MIHOMO_SECRET` | 控制器鉴权密钥；初始化服务和运行服务必须读取同一值 |
| `MIHOMO_SUB_WOG`、`MIHOMO_SUB_WOGB` | 按实际订阅填写；地址可能包含令牌，仅在本地编辑器查看 |
| `MIHOMO_IMAGE` | 与验证过的代理版本对应；共享部署需要同时包含 mihomo 二进制和 `/app/mihomo-manager/bootstrap-mihomo.sh`，不能换成仅含二进制的基础镜像 |

```bash
cd /opt/deploy
stat -c '%u:%g' config.yaml
docker compose config --quiet
docker compose config --services
docker compose config --images
docker compose ps -a
docker compose images
```

`config --quiet` 验证配置而不输出展开后的环境变量。不要上传不带选项的 `docker compose config` 输出，它可能包含密钥和订阅。

结束两个项目正在进行的探测、定档和写回，再安排共享代理维护窗口。将当前配置备份保存在仅管理员可读的本地目录，记录当前镜像摘要。真实备份不能进入 Git 或 Docker 构建目录。

同一 Docker 网络中的客户端通过 `http://mihomo:7890` 使用共享代理。容器里的 `127.0.0.1` 指容器自身。nginx HTTPS 反向代理端口不能仅凭端口号当作 HTTP CONNECT 正向代理；应按实际服务协议和网络配置验证。

## 二、按镜像更新共享服务

先在共享 compose 或对应环境变量中设置经过验证的目标镜像版本。不要用浮动标签推断具体代码版本；保留可追溯的镜像摘要。

### 拉取并运行初始化

```bash
cd /opt/deploy
docker compose pull upstream-importer mihomo-init mihomo
docker compose up -d --force-recreate mihomo-init
docker compose ps -a mihomo-init
```

等待 `mihomo-init` 成功退出（退出码 0），再执行下一步。必要时仅在本机查看初始化日志并脱敏；不能只看容器创建成功。

初始化把配置和健康检查脚本写入与 mihomo 共享的命名卷，容器路径为 `/root/.config/mihomo`，不等于宿主机存在 `/opt/deploy/mihomo/config.yaml`。正常升级保留命名卷，不设置 `MIHOMO_FORCE_REBUILD=1`；订阅或密钥变更可能触发配置重建，应提前审查。

### 更新代理和 importer

```bash
docker compose up -d --force-recreate mihomo
docker compose up -d --no-deps upstream-importer
docker compose ps -a mihomo upstream-importer
```

只更新 importer 不会刷新已有代理卷内的脚本。若本次只改 importer，且代理镜像与脚本未变，可只拉取并更新 importer，不必重建共享代理。

### 检查必要文件与真实出口

以下命令只报告存在性，不显示配置、订阅或密钥内容。

```bash
docker compose exec -T mihomo sh -c 'test -s /root/.config/mihomo/config.yaml && test -s /root/.config/mihomo/healthcheck.sh'
echo "必要文件检查退出码=$?"
docker compose exec -T mihomo sh /root/.config/mihomo/healthcheck.sh
echo "健康检查退出码=$?"
```

健康脚本验证带鉴权的控制器 API 和经代理的出口请求，全部成功返回 0，失败返回非零。它不切换 DIRECT/AUTO，也不修改路由、订阅或配置。节点不可用不能解释成“降级成功”。共享 compose 在脚本缺失时报告初始化错误并返回失败，不以仅 API 存活代替出口健康。

不要假设 mihomo 基础镜像中有 Python。共享部署使用上述 `.sh` 路径；仓库独立代理 profile 的服务名和卷路径不同，以对应 compose 为准。

## 三、四个网页流程验收

通过实际反向代理入口登录。网页能打开、容器 healthy、单元测试通过，均不能代替业务结果。

| 流程 | 必须检查的结果 |
|---|---|
| 投喂 → 写回 | 实际账号输入完成探测、定档、方案生成；模型与参数有来源或明确未知原因；只提交有依据的变更；写回后回读确认，重复操作不产生重复条目 |
| 路由批量管理 | 筛选后只操作所选范围；优先级和启停保存后回读正确；未选条目及另一项目配置保持；核对恢复操作 |
| 全局调优体检 | 建议与真实配置一致；无建议时不能冒充成功写回；有建议时核对差异、应用与回读 |
| 全量重探 | 从真实配置枚举输入，探测、定档、方案和写回均到达终态；失败项说明原因；不得将缺失证据当成支持，也不得以空跑代替完整验收 |

另测单站探测与刷新恢复：刷新后恢复同一任务，网络中断重试不重复提交写回；检查控制台和网络请求是否出现 `Failed to fetch`、长期待定、黑屏、缺字段或轮询无响应。

模型名称及上下文参数以实际证据为准，不能把文档示例模型当作站点支持事实。分别记录“探测完成”“方案允许/阻断”“写回结果”和“配置回读”。共享代理更新后，还需验证两个项目的实际代理请求及 importer 到 CPA 管理接口的连接。另一项目若要求管理员接受条款，应由管理员完成后继续验收。

## 四、故障排查

### 初始化失败或配置缺失

检查初始化退出状态，在可信编辑器中核对订阅、密钥与引号。不要运行 `grep MIHOMO_SUB .env`、打印生成配置、读取订阅前几行或上传完整日志，这些内容可能暴露令牌、节点和上游地址。确认卷挂载和镜像脚本正确，再重新初始化；不要删除卷或清空 providers 目录。

### 写回权限不足

用第一节 `stat` 核对 UID/GID 和挂载权限，再仅重建 importer。不要对共享目录递归放宽权限；检查挂载方式是否仍允许项目的原子写回。

### 探测待定、失败或方案阻断

保留任务标识、时间、脱敏错误类型与镜像摘要。区分网络失败、定档失败、证据不足、写回拒绝和回读失败，按阶段排查。版本字符串或历史“已修复”记录不能证明当前部署已解决。

### 代理不健康

运行第二节健康脚本，按 API 或出口失败分别检查密钥同步、网络和订阅。控制器开启鉴权后，无鉴权 `curl /proxies` 的结果不能证明服务正常。诊断报告只保留状态与数量，不公开完整代理列表。

## 五、回滚与维护

回滚前核对已发生的配置写回。把 importer 镜像恢复为此前记录且验证过的摘要，再仅更新该服务；不要对共享项目执行 `docker compose down`。代理回滚需要独立维护窗口，并核对初始化脚本与运行镜像兼容性。

```bash
# 先在共享配置中设置已验证的回滚镜像，再执行：
cd /opt/deploy
docker compose pull upstream-importer
docker compose up -d --no-deps upstream-importer
docker compose ps -a upstream-importer
```

订阅按配置中的 interval 更新。刷新失败时保留缓存，排查鉴权、网络或订阅响应；不要用 `rm -rf` 清除共享代理数据。订阅变更参见 [共享代理说明](mihomo-manager/README.md)，审查配置、初始化并验证两个项目。

发布前记录最终镜像的验收证据，检查源码、Git 历史、构建上下文及镜像是否混入私密配置、账号、域名、密钥或个人信息。真实记录、MHTML 和完整私有教程不随源码发布；发布动作遵循用户确认要求。
