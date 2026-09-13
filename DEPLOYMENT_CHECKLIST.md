# VPS 部署检查清单（2026-09-13）

本文档供运维人员在 VPS 上部署 upstream-importer + mihomo 时使用。

## 一、部署前准备

### 1. 环境变量配置（/opt/deploy/.env）

```bash
# ── upstream-importer ────────────────────────────────────
# Bearer token（可选，不设则随机生成或用 CPA 管理密码）
IMPORTER_TOKEN=$(openssl rand -hex 20)

# UID/GID 必须与 config.yaml 属主一致
IMPORTER_UID=$(stat -c '%u' /opt/deploy/config.yaml)
IMPORTER_GID=$(stat -c '%g' /opt/deploy/config.yaml)

# ── mihomo 代理 ─────────────────────────────────────────
# API 鉴权密钥（可选，不设则同网段容器可无鉴权访问 9090）
MIHOMO_SECRET=$(openssl rand -hex 20)

# 订阅地址（必填，含 token）
MIHOMO_SUB_WOG=https://vpn.example.com/...
MIHOMO_SUB_WOGB=https://sub.example.net/api/v1/client/subscribe?token=REPLACE_ME
```

### 2. 确认文件权限

```bash
ls -l /opt/deploy/config.yaml
# 输出示例：-rw-r--r-- 1 1000 1000 354321 Sep 13 10:00 config.yaml
# IMPORTER_UID=1000, IMPORTER_GID=1000
```

### 3. 确认 nginx 8443 TLS 代理可达性

```bash
# 错误配置（容器内 127.0.0.1 到不了宿主机 nginx）
proxy-url: http://127.0.0.1:8443

# 正确配置（需在 docker-compose.yml 添加 extra_hosts）
# cli-proxy-api:
#   extra_hosts:
#     - "host.docker.internal:host-gateway"
proxy-url: http://host.docker.internal:8443

# 或者不配 proxy-url，让 upstream-importer 自动判定
```

## 二、部署步骤

### 1. 更新镜像地址（首次部署）

编辑 `/opt/deploy/docker-compose.yml`，找到 `upstream-importer` 服务（约 507 行），将镜像地址改为你的 Docker Hub 仓库：

```yaml
# 旧地址
image: swhesong/cpa-upstream-importer:latest

# 新地址（替换为你的用户名）
image: hyskoamorroh/cpa-upstream-importer:latest
```

### 2. 拉取最新镜像

```bash
cd /opt/deploy
docker compose pull upstream-importer
```

**预期输出**：
- `hyskoamorroh/cpa-upstream-importer:latest` 拉取成功（或你配置的镜像地址）
- `metacubex/mihomo:latest` 已集成在 upstream-importer 镜像内（无需单独拉取）

### 3. 启动服务

```bash
# 首次部署：启动 mihomo 代理
docker compose up -d mihomo-init
docker compose logs mihomo-init
docker compose up -d mihomo

# 启动 upstream-importer
docker compose up -d upstream-importer
```

**启动顺序**：
1. `mihomo-init` 容器先启动，从模板生成 `mihomo/config.yaml`
2. `mihomo-init` 退出（exit 0）
3. `mihomo` 容器启动，读取生成的配置
4. `cli-proxy-api`、`upstream-importer` 等其他服务启动

### 3. 验证 mihomo 配置生成

```bash
ls -lh /opt/deploy/mihomo/config.yaml
cat /opt/deploy/mihomo/config.yaml | grep -E "secret:|proxy-providers:" -A 2
```

**预期输出**：
- `secret: <40位hex>` （从 MIHOMO_SECRET 展开）
- `proxy-providers:` 下有 `wog:` 和 `wogb:` 两个订阅

### 4. 检查容器状态

```bash
docker ps | grep -E "mihomo|upstream-importer|cli-proxy-api"
```

**预期输出**：
```
CONTAINER ID   IMAGE                                    STATUS
abc123def456   metacubex/mihomo:latest                  Up 2 minutes (healthy)
789ghi012jkl   swhesong/cpa-upstream-importer:latest    Up 2 minutes (healthy)
345mno678pqr   eceasy/cli-proxy-api:latest              Up 2 minutes
```

**注意**：
- `mihomo-init` 不应出现在 `docker ps` 中（已退出）
- `mihomo` 和 `upstream-importer` 都应显示 `(healthy)`

### 5. 检查 mihomo 健康状态

```bash
docker exec mihomo python3 /root/.config/mihomo/healthcheck.py
echo $?
```

**预期输出**：
- 退出码 `0`（健康）
- 若节点全挂会输出 "节点全部失效，已切换到 DIRECT"，但仍返回 0（降级成功）

### 6. 检查 upstream-importer 日志

```bash
docker logs upstream-importer 2>&1 | tail -20
```

**预期输出**：
- `启动在 http://0.0.0.0:8765`
- `CPA 管理端点: http://cli-proxy-api:8317`
- `代理: http://mihomo:7890`
- 无 Python traceback

**常见错误**：
- `PermissionError: [Errno 13] Permission denied: '/data/config.yaml'`  
  → IMPORTER_UID/GID 与 config.yaml 属主不一致
- `NameError: name 'os' is not defined`  
  → 旧镜像，需 `docker compose pull` 更新

## 三、功能验证

### 1. 访问 Web 界面

```bash
curl -I http://127.0.0.1:8765/
```

**预期输出**：`HTTP/1.0 200 OK`

### 2. 全量重探（空跑测试）

通过 nginx 反代域名访问：`https://importer.example.com`

1. 点击左上角"全量重探"
2. 不勾选任何站点，直接点"开始探测"
3. 观察是否报错

**预期结果**：
- 返回空方案，无 JavaScript 错误
- 不应出现 `POST /api/plan 500` 或 `NameError`

### 3. 单站探测（真实测试）

1. 在"增量探测"页面输入一个真实上游 URL + Key
2. 点"探测"
3. 查看模型列表、priority、上下文窗口是否正确

**预期结果**：
- Claude 段显示 `claude-opus-5`、`claude-sonnet-4.5` 等（若站方支持）
- priority 为整数（100-999 档位）
- 上下文窗口显示具体数值（如 200000）或 "—"（未探测）

### 4. mihomo 代理测试

在 VPS 上通过 mihomo 访问外网：

```bash
curl -x http://127.0.0.1:7890 -I https://www.google.com
```

**预期输出**：`HTTP/2 200`（若订阅节点可用）

**故障排查**：
```bash
# 检查 mihomo 9090 API
curl http://127.0.0.1:9090/proxies 2>&1 | jq '.proxies | keys'

# 检查订阅拉取
ls -lh /opt/deploy/mihomo/providers/
cat /opt/deploy/mihomo/providers/wog.yaml | head -20
```

## 四、常见问题

### Q1: mihomo-init 一直重启

**原因**：
- .env 缺少 `MIHOMO_SUB_WOG` 或 `MIHOMO_SUB_WOGB`
- 订阅 URL 格式错误（缺 `https://`）

**解决**：
```bash
grep MIHOMO_SUB /opt/deploy/.env
# 补全后重启：docker compose up -d mihomo-init
```

### Q2: upstream-importer 显示 PermissionError

**原因**：容器 user 与 config.yaml 属主不一致

**解决**：
```bash
stat -c '%u:%g' /opt/deploy/config.yaml
# 输出 1000:1000
# 在 .env 中设置：
# IMPORTER_UID=1000
# IMPORTER_GID=1000
docker compose up -d --force-recreate upstream-importer
```

### Q3: 全量检测结果全是"待定"

**根因**（已在 cc2ef5f 修复）：
- 同站多 Key priority 分裂
- request-scoped-errors 未回填
- 首轮定档失败被 silent 抑制

**验证修复**：
```bash
docker exec upstream-importer python3 -c "
import sys; sys.path.insert(0, '/app')
from cpa_probe import __version__
print(__version__ if hasattr(sys.modules['cpa_probe'], '__version__') else 'dev')
"
# 输出应为 cc2ef5f 或更新的 commit hash
```

### Q4: 8443 TLS 代理不生效

**原因**：cli-proxy-api 容器内的 127.0.0.1:8443 到不了宿主机 nginx

**解决**：
1. 在 `docker-compose.yml` 的 `cli-proxy-api` 服务下添加：
   ```yaml
   extra_hosts:
     - "host.docker.internal:host-gateway"
   ```
2. 在 `config.yaml` 中把 `proxy-url: http://127.0.0.1:8443` 改为 `http://host.docker.internal:8443`
3. `docker compose up -d --force-recreate cli-proxy-api`

## 五、回滚方案

若新镜像出现问题，回滚到上一版本：

```bash
docker compose down
docker pull swhesong/cpa-upstream-importer:a3d6563
# 在 docker-compose.yml 中改 image: swhesong/cpa-upstream-importer:a3d6563
docker compose up -d
```

## 六、监控与维护

### 定期检查

```bash
# 每日检查容器健康
docker ps --filter "name=mihomo|upstream-importer" --format "table {{.Names}}\t{{.Status}}"

# 每周检查磁盘空间（mihomo providers/ 会不断拉取）
du -sh /opt/deploy/mihomo/providers/

# 每月检查 nginx 日志（8443 TLS 代理使用率）
grep "127.0.0.1:8443" /var/log/nginx/access.log | wc -l
```

### 订阅更新

mihomo 会按 `interval` 自动更新订阅（默认 21600 秒 = 6 小时），无需手工干预。

若需强制更新：

```bash
docker exec mihomo rm -rf /root/.config/mihomo/providers/*.yaml
docker restart mihomo
```

---

**最后更新**: 2026-09-13  
**对应镜像**: swhesong/cpa-upstream-importer:cc2ef5f  
**GitHub Actions**: 推送 main 分支自动触发 Docker Hub 构建
