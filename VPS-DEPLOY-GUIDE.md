# VPS 部署与更新指南

## 快速命令参考

### 查看服务状态
```bash
cd /opt/deploy
docker compose ps | grep -E 'mihomo|upstream-importer|cli-proxy-api'
```

### 拉取最新镜像
```bash
cd /opt/deploy
docker compose pull upstream-importer
```

### 重启服务
```bash
cd /opt/deploy
docker compose restart upstream-importer
```

### 停止服务
```bash
cd /opt/deploy
docker compose stop upstream-importer
```

### 查看日志
```bash
cd /opt/deploy
docker compose logs -f upstream-importer
docker compose logs --tail=100 upstream-importer
```

### 完整重建（拉取新镜像后）
```bash
cd /opt/deploy
docker compose pull upstream-importer
docker compose up -d upstream-importer
```

---

## 一、首次部署

### 1. 更新 docker-compose.yml 镜像地址

编辑 `/opt/deploy/docker-compose.yml`，找到 `upstream-importer` 服务（约 507 行），将：
```yaml
image: swhesong/cpa-upstream-importer:latest
```
改为：
```yaml
image: hyskoamorroh/cpa-upstream-importer:latest
```

**注意**：如果你的 Docker Hub 用户名不是 `hyskoamorroh`，改成实际用户名。

### 2. 配置环境变量

编辑 `/opt/deploy/.env`，确保有以下变量：

```bash
# upstream-importer Bearer token（可选）
IMPORTER_TOKEN=$(openssl rand -hex 20)

# config.yaml 文件属主（必须）
IMPORTER_UID=$(stat -c '%u' /opt/deploy/config.yaml)
IMPORTER_GID=$(stat -c '%g' /opt/deploy/config.yaml)

# mihomo 代理订阅（必填）
MIHOMO_SUB_WOG=https://你的机场订阅地址1
MIHOMO_SUB_WOGB=https://你的机场订阅地址2
MIHOMO_SECRET=$(openssl rand -hex 20)

# CPA 地址（默认值）
CPA_UPSTREAM_URL=http://cli-proxy-api:8317
```

### 3. 启动服务

```bash
cd /opt/deploy

# 拉取镜像
docker compose pull upstream-importer mihomo

# 启动 mihomo 代理
docker compose up -d mihomo-init
docker compose logs mihomo-init  # 检查配置生成
docker compose up -d mihomo

# 启动 upstream-importer
docker compose up -d upstream-importer

# 检查状态
docker compose ps | grep upstream-importer
```

### 4. 验证部署

```bash
# 检查健康状态
docker compose ps upstream-importer
# STATUS 应显示 "healthy"

# 检查日志
docker compose logs --tail=50 upstream-importer

# 访问前端
curl -I http://127.0.0.1:8765/
# 应返回 200 OK
```

访问 https://importer.chiangma.com，登录后点击「全量检测」。

---

## 二、日常更新

### GitHub Actions 构建完成后

1. **拉取新镜像**
```bash
cd /opt/deploy
docker compose pull upstream-importer
```

2. **重启服务**
```bash
docker compose up -d upstream-importer
```

3. **验证更新**
```bash
# 查看镜像 ID 是否变化
docker images | grep cpa-upstream-importer

# 查看启动日志
docker compose logs --tail=30 upstream-importer

# 检查健康状态
docker compose ps upstream-importer
```

### 回滚到旧版本

```bash
cd /opt/deploy

# 停止当前服务
docker compose stop upstream-importer

# 使用特定版本（如果打了 tag）
# 编辑 docker-compose.yml，将 :latest 改为 :v1.0.0
# 或临时覆盖：
docker compose up -d upstream-importer \
  -e IMAGE_TAG=v1.0.0

# 查看日志确认
docker compose logs upstream-importer
```

---

## 三、故障排查

### 服务无法启动

```bash
# 查看完整日志
docker compose logs upstream-importer

# 常见问题：
# 1. config.yaml 权限错误
ls -l /opt/deploy/config.yaml
stat -c '%u:%g' /opt/deploy/config.yaml
# 应与 .env 中 IMPORTER_UID:IMPORTER_GID 一致

# 2. 端口冲突
netstat -tlnp | grep 8765

# 3. CPA 不可达
docker compose exec upstream-importer curl http://cli-proxy-api:8317
```

### 健康检查失败

```bash
# 进入容器手动检查
docker compose exec upstream-importer sh

# 容器内执行
python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/', timeout=5).status)"
# 应返回 200

# 检查进程
ps aux | grep python
```

### 前端报错「503 No available accounts」

这是 **CPA 问题**，不是 importer 问题：
- CPA 的 `cli-proxy-api` 配置了 Claude 上游，但该上游的 group 设置为「仅限 Claude Code 客户端」
- 解决：编辑 CPA 后台 AI 提供者配置，将该账号的「请求伪装」改为「启用」或分组改为「全部」

### mihomo 代理不通

```bash
# 检查 mihomo 状态
docker compose ps mihomo
docker compose logs mihomo

# 测试代理端口
curl -x http://127.0.0.1:7890 https://www.google.com -I

# 检查配置
cat /opt/deploy/mihomo/config.yaml | grep -E "port:|allow-lan:"
```

---

## 四、服务依赖关系

```
nginx (宿主机:443/8443)
  ↓
cli-proxy-api:8317 ← upstream-importer:8765
  ↓                          ↓
mihomo:7890 ← (HTTP_PROXY) ← 向上游探测
```

- **upstream-importer** 通过 `CPA_UPSTREAM_URL` 连接 `cli-proxy-api`
- **upstream-importer** 通过 `HTTP_PROXY=http://mihomo:7890` 出网（可选）
- **cli-proxy-api** 的上游如果配了 `proxy-url: http://127.0.0.1:8443`，走宿主机 nginx TLS 代理

---

## 五、环境变量完整列表

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `IMPORTER_HOST` | 0.0.0.0 | 监听地址（容器内） |
| `IMPORTER_PORT` | 8765 | 监听端口 |
| `IMPORTER_CONFIG` | /data/config.yaml | CPA 配置文件路径 |
| `IMPORTER_BACKUP_DIR` | /backups | 备份目录 |
| `IMPORTER_TOKEN` | （空） | Bearer token（可选） |
| `CPA_UPSTREAM_URL` | http://cli-proxy-api:8317 | CPA 地址 |
| `HTTP_PROXY` | （空） | 出网代理（可选） |
| `HTTPS_PROXY` | （空） | 同上 |
| `IMPORTER_UID` | 10001 | 容器内用户 UID |
| `IMPORTER_GID` | 10001 | 容器内用户 GID |

---

## 六、常用维护命令

```bash
# 查看所有服务
docker compose ps

# 重启所有服务
docker compose restart

# 查看资源占用
docker stats upstream-importer mihomo cli-proxy-api

# 清理旧镜像
docker image prune -a

# 备份配置
cp /opt/deploy/config.yaml /root/backup/config.yaml.$(date +%Y%m%d)

# 导出容器日志
docker compose logs --no-color upstream-importer > /tmp/importer.log
```
