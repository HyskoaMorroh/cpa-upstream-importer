#!/usr/bin/env bash
# 一键跑完部署前的全部检查，输出可直接贴回来的报告。
#
#   cd /opt/deploy/upstream-importer && bash deploy/preflight.sh
#
# 只读：不发外网请求、不改 config.yaml、不装任何东西。
# 每一项都打印实际值而不只是「通过」—— 出问题时那个值就是线索。

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$(dirname "$HERE")"
CFG="$DEPLOY/config.yaml"

pass=0; fail=0; warn=0
ok()   { printf '  \033[92m✓\033[0m %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '  \033[91m✗\033[0m %s\n' "$*"; fail=$((fail+1)); }
wrn()  { printf '  \033[93m!\033[0m %s\n' "$*"; warn=$((warn+1)); }
say()  { printf '\n\033[96m── %s\033[0m\n' "$*"; }

echo "=================================================================="
echo "upstream-importer 部署前自检"
echo "  时间     : $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  主机     : $(hostname)"
echo "  仓库路径 : $HERE"
echo "=================================================================="

say "1. 运行环境"

if command -v python3 >/dev/null; then
  PYV=$(python3 -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')
  PYOK=$(python3 -c 'import sys;print(1 if sys.version_info>=(3,9) else 0)')
  [[ "$PYOK" == 1 ]] && ok "python3 $PYV（需 >= 3.9）" || bad "python3 $PYV 低于 3.9"
else
  bad "没有 python3"
fi

if python3 -c 'import yaml' 2>/dev/null; then
  ok "PyYAML $(python3 -c 'import yaml;print(yaml.__version__)')"

if python3 -c 'import bcrypt' 2>/dev/null; then
  ok "bcrypt $(python3 -c 'import bcrypt;print(bcrypt.__version__)') —— 可用 CPA 后台密码登录"
else
  wrn "未装 bcrypt：只能用 IMPORTER_TOKEN 登录，不能用 CPA 后台密码
      装它：dnf install -y python3-bcrypt"
fi
else
  bad "缺 PyYAML —— 服务启动即需。装：dnf install -y python3-pyyaml"
fi

ARCH=$(uname -m); ok "架构 $ARCH · 内核 $(uname -r)"

say "2. 文件完整性"

n_files=0

for f in server.py cli.py README.md \
         cpa_probe/__init__.py cpa_probe/parse.py cpa_probe/classify.py \
         cpa_probe/request.py cpa_probe/client.py cpa_probe/fingerprint.py \
         cpa_probe/pipeline.py cpa_probe/plan.py cpa_probe/writeback.py \
         web/index.html web/app.js \
         tests/run.py tests/test_probe.py tests/test_server.py tests/test_pipeline.py \
         tests/test_edges.py tests/test_reload.py tests/test_speed.py tests/test_web.py \
         deploy/install.sh deploy/upstream-importer.service          deploy/Dockerfile deploy/nginx-snippet.conf docker-compose.yml; do
  n_files=$((n_files + 1))
  [[ -f "$HERE/$f" ]] || bad "缺文件 $f"
done
[[ $fail -eq 0 ]] && ok "$n_files 个必需文件齐全"

n_legacy=$(ls -1 "$HERE/legacy" 2>/dev/null | wc -l)
[[ "$n_legacy" -ge 6 ]] && ok "legacy/ 原脚本 $n_legacy 个" \
                        || wrn "legacy/ 只有 $n_legacy 个（应 6）"

say "3. 语法编译"

if python3 -m compileall -q "$HERE" >/tmp/_pf_compile 2>&1; then
  ok "全部 .py 编译通过"
else
  bad "编译失败："; sed 's/^/      /' /tmp/_pf_compile | head -20
fi
rm -f /tmp/_pf_compile

for s in deploy/install.sh legacy/logs-digest.sh; do
  bash -n "$HERE/$s" 2>/dev/null && ok "$s 语法 OK" || bad "$s 语法错误"
done

say "4. config.yaml"

if [[ -f "$CFG" ]]; then
  LN=$(wc -l < "$CFG"); KB=$(( $(stat -c%s "$CFG") / 1024 ))
  ok "找到 $CFG（$LN 行 · ${KB} KB）"
  # PYTHONIOENCODING 必须给：Windows/GBK 终端上 ✓ 会抛 UnicodeEncodeError，
  # 把整段 config.yaml 报告吞掉（VPS 上是 UTF-8，但这脚本要两边都能跑）
  PYTHONIOENCODING=utf-8 python3 - "$CFG" <<'EOF'
import io,sys,yaml
p=sys.argv[1]
try:
    c=yaml.safe_load(io.open(p,encoding='utf-8').read())
except Exception as e:
    print("  \033[91m✗\033[0m YAML 语法错误：%s" % e); sys.exit(1)
S=['gemini-api-key','codex-api-key','claude-api-key','openai-compatibility']
n=sum(len(c.get(s) or []) for s in S)
print("  \033[92m✓\033[0m YAML OK · %d 个顶层键 · 四段共 %d 条目" % (len(c), n))
for s in S:
    e=c.get(s) or []
    pr=sorted({x.get('priority') for x in e if isinstance(x,dict)
               and isinstance(x.get('priority'),int)}, reverse=True)
    print("      %-22s %3d 条目 · %2d 档 · 顶 %s" %
          (s, len(e), len(pr), pr[0] if pr else '-'))
ak=c.get('api-keys') or []
print("  \033[92m✓\033[0m 客户端入口 Key %d 个（--client-key 从这里取）" % len(ak))
rm=c.get('remote-management') or {}
print("  \033[92m✓\033[0m management secret-key %s" %
      ("已配置" if rm.get('secret-key') else "\033[91m未配置\033[0m"))
print("      CPA 客户端端口 %s" % c.get('port'))
EOF
  BAKS=$(ls -1 "$DEPLOY"/config.yaml.bak-* 2>/dev/null | wc -l)
  [[ "$BAKS" -gt 0 ]] && ok "已有 $BAKS 个历史备份（保留，别删）" \
                      || wrn "无历史备份 —— 首次写回会自动建"
else
  bad "找不到 $CFG"
fi

say "5. 主栈集成（docker-compose）"

MAIN_COMPOSE="$(dirname "$HERE")/docker-compose.yml"
if [[ -f "$MAIN_COMPOSE" ]]; then
  if python3 - "$MAIN_COMPOSE" <<'PYEOF' 2>/dev/null
import io, sys, yaml
d = yaml.safe_load(io.open(sys.argv[1], encoding="utf-8").read())
sys.exit(0 if "upstream-importer" in (d.get("services") or {}) else 1)
PYEOF
  then
    ok "主栈已含 upstream-importer 服务"
    python3 - "$MAIN_COMPOSE" <<'PYEOF'
import io, sys, yaml
d = yaml.safe_load(io.open(sys.argv[1], encoding="utf-8").read())
u = d["services"]["upstream-importer"]
for label, val in (("ports", u.get("ports")), ("user", u.get("user")),
                   ("restart", u.get("restart"))):
    print(f"      {label:9} {val}")
vols = u.get("volumes") or []
print(f"      volumes   {len(vols)} 项")
if not any(str(v).startswith("127.0.0.1:") for v in (u.get("ports") or [])):
    print("      ! ports 未限定 127.0.0.1 —— 会暴露到公网")
PYEOF
  else
    wrn "主栈 docker-compose.yml 里没有 upstream-importer 服务
      —— 只能用 systemd 或直接跑 python3 server.py"
  fi
  if python3 -c "import yaml,io,sys; yaml.safe_load(io.open(sys.argv[1],encoding='utf-8').read())"        "$MAIN_COMPOSE" 2>/dev/null; then
    ok "主栈 YAML 语法 OK（$(wc -l < "$MAIN_COMPOSE") 行）"
  else
    bad "主栈 YAML 语法错误 —— 整栈会起不来，立刻用备份恢复"
  fi
else
  wrn "找不到主栈 docker-compose.yml：$MAIN_COMPOSE"
fi

# 路径经 argv 传入，不做字符串插值 —— Windows 路径里的反斜杠会被
# Python 当转义符，之前那样写会把「模板没问题」误报成语法错误。
if python3 -c "import yaml,io,sys; yaml.safe_load(io.open(sys.argv[1],encoding='utf-8').read())"      "$HERE/docker-compose.yml" 2>/dev/null; then
  ok "本项目参考模板 docker-compose.yml 语法 OK"
else
  bad "参考模板 docker-compose.yml 语法错误"
fi

say "6. 端口占用"

PORT=8765
if command -v ss >/dev/null; then
  if ss -ltnH 2>/dev/null | grep -qE "[:.]$PORT\b"; then
    wrn "$PORT 已被占用："
    ss -ltnpH 2>/dev/null | grep -E "[:.]$PORT\b" | sed 's/^/      /'
    wrn "换端口：server.py --port <其他>，同时改 deploy/*.service|conf"
  else
    ok "$PORT 空闲"
  fi
else
  wrn "没有 ss，跳过端口检查"
fi

say "7. 回归测试（零外网请求）"

if [[ -f "$CFG" ]]; then
  PYTHONIOENCODING=utf-8 python3 "$HERE/tests/run.py" "$CFG" >/tmp/_pf_test 2>&1
  rc=$?
  tail -4 /tmp/_pf_test | sed 's/^/      /'
  if [[ $rc -eq 0 ]]; then
    ok "全部套件通过"
  else
    bad "测试失败，详情："
    grep -A3 '✗' /tmp/_pf_test | head -30 | sed 's/^/      /'
  fi
  rm -f /tmp/_pf_test
else
  wrn "无 config.yaml，跳过测试"
fi

say "8. 网络出口（只查连通，不打上游 API）"

if command -v curl >/dev/null; then
  IP=$(curl -s --max-time 8 https://api.ipify.org 2>/dev/null)
  [[ -n "$IP" ]] && ok "直连出口 IP $IP" || wrn "取不到出口 IP（不影响本地探测）"
  # mihomo 有两个地址，取决于**从哪里**访问：
  #   容器内（compose 起的服务，同 default 网络）→ http://mihomo:7890
  #   宿主机（直接跑 python3）                    → http://127.0.0.1:7890
  # 之前这里只测容器名，宿主机上必然不通 —— 那是 Docker 内部 DNS，
  # 宿主的 resolver 解析不了。曾把「mihomo 正常运行」误报成「代理挂了」
  # （实测当时 docker compose ps 显示 Up 2 days healthy）。
  PROXY_OK=""
  for CAND in http://127.0.0.1:7890 http://mihomo:7890; do
    if curl -s --max-time 5 -x "$CAND" -o /dev/null \
         -w '%{http_code}' https://api.ipify.org 2>/dev/null | grep -q '^[23]'; then
      PIP=$(curl -s --max-time 8 -x "$CAND" https://api.ipify.org 2>/dev/null)
      ok "代理可用 $CAND · 出口 IP $PIP"
      PROXY_OK="$CAND"
      break
    fi
  done
  if [[ -z "$PROXY_OK" ]]; then
    if docker compose -f "$(dirname "$HERE")/docker-compose.yml" ps mihomo 2>/dev/null \
         | grep -qi 'up'; then
      wrn "两个地址都不通，但 mihomo 容器在运行 —— 宿主机跑 CLI 时用
      --proxy http://127.0.0.1:7890；容器内跑用默认的 http://mihomo:7890"
    else
      wrn "代理不通（127.0.0.1:7890 与 mihomo:7890 都试过）
      宿主机跑 CLI 加 --no-proxy；或先 docker compose up -d mihomo。
      注意 config.yaml 里有 23 个凭据配了 proxy-url，它们此刻走不通"
    fi
  fi
else
  wrn "没有 curl，跳过网络检查"
fi

echo
echo "=================================================================="
printf "通过 %d · 警告 %d · 失败 %d\n" "$pass" "$warn" "$fail"
if [[ $fail -gt 0 ]]; then
  echo "有失败项，先修再往下走。"
  exit 1
fi
cat <<EOF
自检通过，可以进下一步：

  1) 建输入文件（每行 url,key）
       cd $DEPLOY
       cat > accounts.txt <<'TXT'
       https://例子.com,sk-xxxxxxxx
       TXT

  2) 零请求解析，先看格式对不对
       python3 upstream-importer/cli.py -i accounts.txt --dry-run

  3) 探测（开始花钱；先关上下文探测）
       python3 upstream-importer/cli.py -i accounts.txt --no-context

  4) 写回（--write 是硬闸门，不给只预览）
       python3 upstream-importer/cli.py -i accounts.txt --write
EOF
[[ $warn -gt 0 ]] && echo "注意上面 $warn 条警告。"
exit 0
