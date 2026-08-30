#!/usr/bin/env bash
# 在 VPS 上安装 upstream-importer 服务。幂等 —— 重跑不会覆盖已有 token。
#
#   cd /opt/deploy/upstream-importer && bash deploy/install.sh
#
# 只做四件事：查依赖、生成 token、装 systemd unit、自检。
# 不碰 config.yaml，不碰 docker-compose.yml，不装任何 pip 包。

set -euo pipefail

DEPLOY=/opt/deploy
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS="$DEPLOY/secrets"
ENVFILE="$SECRETS/importer.env"
TOKENFILE="$SECRETS/importer_token"
UNIT=/etc/systemd/system/upstream-importer.service
PORT=8765

say()  { printf '\033[96m▸\033[0m %s\n' "$*"; }
ok()   { printf '\033[92m  ✓\033[0m %s\n' "$*"; }
bad()  { printf '\033[91m  ✗\033[0m %s\n' "$*" >&2; }
die()  { bad "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "需要 root（要写 /etc/systemd/system）"

say "1/4 检查依赖"

command -v python3 >/dev/null || die "没有 python3"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
ok "python3 $PYV"
python3 - <<'EOF' || exit 1
import sys
# 3.9 就够：所有 X | Y 类型写法都在注解里，且每个模块都有
# `from __future__ import annotations`（注解不求值）。运行时没有
# match/case，也没有 isinstance(x, A | B) 这类真正需要 3.10 的用法。
# CentOS 9 自带 3.9，不必为此装第二个解释器。
if sys.version_info < (3, 9):
    sys.exit("需要 Python 3.9+")
EOF

python3 -c 'import yaml' 2>/dev/null \
  && ok "PyYAML 已装" \
  || die "缺 PyYAML。装：dnf install -y python3-pyyaml（别用 pip，会污染系统 Python）"

[[ -f "$DEPLOY/config.yaml" ]] || die "找不到 $DEPLOY/config.yaml"
ok "config.yaml $(wc -l < "$DEPLOY/config.yaml") 行"

[[ -f "$HERE/server.py" ]] || die "找不到 $HERE/server.py，脚本没放对位置"
[[ -d "$HERE/web" ]]       || die "找不到 $HERE/web 前端目录"
ok "服务文件齐全"

say "2/4 准备 token"

install -d -m 700 "$SECRETS"
if [[ -s "$TOKENFILE" ]]; then
  ok "token 已存在，保留不动（要换：rm $TOKENFILE 后重跑）"
else
  openssl rand -hex 20 > "$TOKENFILE"
  chmod 600 "$TOKENFILE"
  ok "已生成新 token"
fi
# EnvironmentFile 不支持命令替换，所以把值展开进去
printf 'IMPORTER_TOKEN=%s\n' "$(cat "$TOKENFILE")" > "$ENVFILE"
chmod 600 "$ENVFILE"
ok "$ENVFILE 已写（600）"

say "3/4 装 systemd unit"

install -m 644 "$HERE/deploy/upstream-importer.service" "$UNIT"
systemctl daemon-reload
systemctl enable upstream-importer >/dev/null 2>&1 || true
systemctl restart upstream-importer
ok "服务已启动"

say "4/4 自检"

for _ in $(seq 20); do
  sleep 0.5
  systemctl is-active --quiet upstream-importer && break
done
systemctl is-active --quiet upstream-importer \
  || { journalctl -u upstream-importer -n 30 --no-pager; die "服务没起来"; }
ok "systemd active"

TOK=$(cat "$TOKENFILE")
code=$(curl -s -o /dev/null -w '%{http_code}' \
       -H "Authorization: Bearer $TOK" \
       "http://127.0.0.1:$PORT/api/context" || echo 000)
[[ "$code" == "200" ]] || { journalctl -u upstream-importer -n 30 --no-pager; die "API 自检失败（HTTP $code）"; }
ok "API 200"

code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/context" || echo 000)
[[ "$code" == "401" ]] || die "无 token 竟返回 $code，鉴权没生效 —— 立刻停服务排查"
ok "无 token 正确拒绝（401）"

# 确认没监听外网
if ss -ltn 2>/dev/null | grep -q "0.0.0.0:$PORT"; then
  bad "警告：$PORT 监听在 0.0.0.0，外网可达。这个服务持有明文 Key 且能改 config.yaml"
  bad "     改回 --host 127.0.0.1，或在 nginx 后加 TLS + 访问控制"
else
  ok "只监听 127.0.0.1，外网不可达"
fi

cat <<EOF

══════════════════════════════════════════════════════════════
安装完成

  从本机开（SSH 隧道，推荐）：
    ssh -L $PORT:127.0.0.1:$PORT root@<VPS>
    浏览器打开：
    http://127.0.0.1:$PORT/?token=$TOK

  命令行用（不开网页）：
    cd $DEPLOY
    python3 upstream-importer/cli.py -i accounts.txt --dry-run

  日志：journalctl -u upstream-importer -f
  停止：systemctl stop upstream-importer

  写回前会自动备份 config.yaml，备份留在 $DEPLOY/
══════════════════════════════════════════════════════════════
EOF
