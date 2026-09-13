# mihomo 订阅管理工具

自动化管理 mihomo 代理订阅配置的工具集。

## 文件说明

- **mihomo-subscriptions.conf**: 订阅配置文件（唯一需要编辑的）
- **update-mihomo-subscriptions.sh**: 自动更新脚本
- **README.md**: 本文档

## 快速开始

### 1. 配置订阅

编辑 `mihomo-subscriptions.conf`，每行一个订阅，格式：

```
名称|订阅URL
```

示例：
```
wog|https://vpn.chiangma.com
wogb|https://www.wogb.top/abiabi/vip/client/subscribe?token=YOUR_TOKEN
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
