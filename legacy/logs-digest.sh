#!/bin/bash
# 错误日志摘要：一行一个日志，不下载原文。
#
# 为什么需要：CPA 的错误日志每份都含完整请求体（debug: true），
# 单文件 70KB - 2.1MB，前端逐个点"下载"不可行。今晚排查全靠在 VPS 上
# 直接提取摘要，本脚本把那套命令固化下来。
#
# 用法（在 /opt/deploy 下执行）
#   ./logs-digest.sh              最近 20 个
#   ./logs-digest.sh 50           最近 50 个
#   ./logs-digest.sh 50 '2026-08-28 08:00'   只看该时刻之后
#
# 关键列是「尝试」：
#   0     = 零上游尝试，CPA 在选择阶段就返回 503 auth_unavailable，
#           请求根本没出门。60 个日志里 42 个是这种，成因见 config.yaml
#           transient-error-cooldown-seconds 处的注释。
#   >0    = 请求已发到上游，失败原因看「错误」列。
set -u
N=${1:-20}
SINCE=${2:-}
D=logs/cli-proxy-api

if [ -n "$SINCE" ]; then
  FILES=$(find "$D" -name 'error-*.log' -newermt "$SINCE" 2>/dev/null | sort -r | head -"$N")
else
  FILES=$(ls -t "$D"/error-*.log 2>/dev/null | head -"$N")
fi
[ -z "$FILES" ] && { echo "无匹配日志（目录 $D）"; exit 0; }

printf '%-16s %-18s %-5s %-5s %-28s %s\n' 时间 模型 状态 尝试 上游 错误
printf '%s\n' '--------------------------------------------------------------------------------------------------------------'
for f in $FILES; do
  ts=$(basename "$f" | sed 's/.*completions-//; s/-[0-9a-f]\{8\}\.log$//; s/^2026-//')
  model=$(grep -oE '"model":"[^"]+"' "$f" 2>/dev/null | head -1 | cut -d'"' -f4)
  # ^ 锚定行首：请求体 JSON 里也含 "Upstream URL" 字样，不锚定会数错
  n=$(grep -c '^Upstream URL:' "$f")
  hosts=$(grep '^Upstream URL:' "$f" | sed 's|.*//||; s|/.*||' | sort -u | paste -sd, -)
  st=$(awk '/^=== RESPONSE/{getline; print $2; exit}' "$f")
  err=$(awk '/^=== RESPONSE/{p=1} p' "$f" | grep -oE '"message":"[^"]{0,88}' | head -1 | cut -c12-)
  [ -z "$err" ] && err=$(awk '/^=== RESPONSE/{p=1} p' "$f" | grep -oE '"error":"[^"]{0,88}' | head -1 | cut -c10-)
  printf '%-16s %-18s %-5s %-5s %-28s %s\n' \
    "$ts" "${model:--}" "${st:--}" "$n" "${hosts:--}" "${err:--}"
done

echo
echo "统计："
printf '  零上游尝试(503 选择阶段失败): '
c0=0; for f in $FILES; do [ "$(grep -c '^Upstream URL:' "$f")" = 0 ] && c0=$((c0+1)); done
echo "$c0 / $(echo "$FILES" | wc -l)"
printf '  状态码分布: '
for f in $FILES; do awk '/^=== RESPONSE/{getline; print $2; exit}' "$f"; done | sort | uniq -c | tr '\n' ' '
echo
echo
echo "需要原文时打包成一个文件（比逐个下载快）："
echo "  tar czf /tmp/errlogs.tgz -C $D \$(ls -t $D/error-*.log | head -10 | xargs -n1 basename)"
