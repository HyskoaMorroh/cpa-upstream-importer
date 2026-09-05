#!/bin/bash
# 导出 CPA 的错误日志与运行日志，打包前**强制脱敏**。
#
# 为什么要有这个脚本：CPA 在 debug: true 下把完整请求体写进错误日志，
# 其中含 Authorization 头 —— 也就是明文上游 API Key。手动 tar 一打包发出去，
# 那些 Key 就跟着走了。这里把脱敏做成流程的一部分，不靠人记得。
#
# 用法（在 /opt/deploy，即 config.yaml 所在目录执行）
#   bash upstream-importer/tools/export-logs.sh              最近 50 个错误日志
#   bash upstream-importer/tools/export-logs.sh 200          最近 200 个
#   bash upstream-importer/tools/export-logs.sh 200 --raw    不脱敏（危险，见下）
#
# --raw 只在「日志完全不出本机」时才用。一旦要发给别人、贴进 issue、
# 传网盘，就绝对不要加它。
set -euo pipefail

N=${1:-50}
RAW=no
[ "${2:-}" = "--raw" ] && RAW=yes

LOGDIR=logs/cli-proxy-api
SVC=cli-proxy-api

# 必须在 config.yaml 旁边跑 —— logs/ 是相对它的路径
if [ ! -f config.yaml ]; then
  echo "错：没找到 ./config.yaml。请在 /opt/deploy（CPA 的部署目录）下执行。" >&2
  exit 1
fi

STAMP=$(date +%Y%m%d-%H%M%S)
OUT="log-export-$STAMP"
mkdir -p "$OUT"

# ── 1. 错误日志原文 ────────────────────────────────────────────────
# 每次失败一个文件，含完整请求体。只取最近 N 个 —— 全量可能上百 MB。
if [ -d "$LOGDIR" ]; then
  mkdir -p "$OUT/error-logs"
  ls -t "$LOGDIR"/error-*.log 2>/dev/null | head -"$N" | while read -r f; do
    cp -a "$f" "$OUT/error-logs/"
  done
  echo "错误日志: $(ls "$OUT/error-logs" 2>/dev/null | wc -l) 个"
else
  echo "错误日志: 目录 $LOGDIR 不存在，跳过"
fi

# ── 2. 容器 stdout ────────────────────────────────────────────────
# logging-to-file: false 时常规日志只在这里，且受 Docker json-file 轮转限制
# （compose 里是 max-size 10m × max-file 3）—— 轮转掉的部分找不回来。
if docker compose ps "$SVC" >/dev/null 2>&1; then
  docker compose logs --no-color --timestamps "$SVC" > "$OUT/stdout.log" 2>&1 || true
  echo "stdout: $(wc -l < "$OUT/stdout.log") 行"
else
  echo "stdout: 服务 $SVC 未在本 compose 中，跳过"
fi

# ── 3. 摘要表 ──────────────────────────────────────────────────────
# 一行一个错误日志。先看这个再决定翻哪几份原文。
DIGEST=upstream-importer/legacy/logs-digest.sh
[ -f "$DIGEST" ] && bash "$DIGEST" "$N" > "$OUT/digest.txt" 2>&1 || true

# ── 4. 环境快照（不含任何密钥）────────────────────────────────────
{
  echo "导出时间: $(date -Is)"
  echo "N: $N   脱敏: $([ "$RAW" = yes ] && echo 否 || echo 是)"
  echo
  echo "--- CPA 日志相关配置 ---"
  grep -nE '^(debug|request-log|logging-to-file|logs-max-total-size-mb|error-logs-max-files|commercial-mode):' \
    config.yaml 2>/dev/null || true
  echo
  echo "--- 容器状态 ---"
  docker compose ps "$SVC" 2>/dev/null || true
} > "$OUT/context.txt"

# ── 5. 脱敏 ────────────────────────────────────────────────────────
# 改的是副本，原始日志一个字节都不动。
if [ "$RAW" = no ]; then
  n=0
  # -I 跳过二进制；只处理确实命中的文件，避免无谓重写
  while read -r f; do
    # 逐种形态一条规则。2026-09-05 补了六种漏掉的 —— 每种都造样本验过，
    # 补之前它们原样出现在导出文件里。
    sed -i -E \
      -e 's/(sk-[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+/\1****REDACTED/g' \
      -e 's/(Bearer )[A-Za-z0-9._~+\/-]{12,}/\1****REDACTED/gI' \
      -e 's/("(api[_-]?key|x-api-key|secret[_-]?key)"[[:space:]]*:[[:space:]]*")[^"]{8,}/\1****REDACTED/gI' \
      -e 's/(x-goog-api-key:[[:space:]]*)[A-Za-z0-9._-]{12,}/\1****REDACTED/gI' \
      -e 's/([?&]key=)[A-Za-z0-9._-]{12,}/\1****REDACTED/g' \
      `# ① ?token= —— 投喂台的 query 登录方式。nginx 的 log_format main 记` \
      `#   $request，第一次访问就把它写进 access.log；而这个 token 等价于` \
      `#   CPA 写权限，比单个上游 Key 更值钱。这是最要紧的一条。` \
      -e 's/([?&](token|access_token|auth)=)[^&[:space:]"]{8,}/\1****REDACTED/gI' \
      `# ② AIza... —— gemini 段的 Key 形态，原来五条规则一个都没匹配上` \
      -e 's/(AIza[A-Za-z0-9_-]{4})[A-Za-z0-9_-]+/\1****REDACTED/g' \
      `# ③ bcrypt 哈希 —— config.yaml 的 remote-management.secret-key。` \
      `#   它不是明文密码，但拿到就能离线爆破` \
      -e 's/\$2[aby]\$[0-9]{2}\$[A-Za-z0-9.\/]{10,}/****REDACTED-BCRYPT/g' \
      `# ④ 裸值形态的鉴权头 —— 原来只覆盖 JSON 引号形态与 x-goog-api-key。` \
      `#   HTTP 日志里是 \`x-api-key: xxx\` 这种裸值` \
      -e 's/((x-)?(api|goog-api|anthropic)-key:[[:space:]]*)[^[:space:]",]{8,}/\1****REDACTED/gI' \
      `# ⑤ URL 内嵌凭据（proxy-url 的 user:pass@host）。只抹密码段 ——` \
      `#   主机与端口是排障最需要看的部分` \
      -e 's/(:\/\/[^:@[:space:]\/"]+:)[^@[:space:]\/"]+@/\1****REDACTED@/g' \
      `# ⑥ 环境变量与管理密码的赋值形态` \
      -e 's/((IMPORTER_TOKEN|MGMT|MGMT_KEY|SECRET_KEY|CPA_[A-Z_]*KEY)[[:space:]]*=[[:space:]]*)[^[:space:]"]{6,}/\1****REDACTED/g' \
      -e 's/(mgmt[_-]?key["]?[[:space:]]*[:=][[:space:]]*"?)[^[:space:]",}]{6,}/\1****REDACTED/gI' \
      "$f"
    n=$((n+1))
  done < <(grep -rlIE 'sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{10,}|Bearer [A-Za-z0-9._~+/-]{12,}|"api[_-]?key"|x-goog-api-key|[?&](key|token|access_token|auth)=|\$2[aby]\$[0-9]{2}\$|://[^:@[:space:]/"]+:[^@[:space:]/"]+@|(IMPORTER_TOKEN|MGMT_KEY|SECRET_KEY)[[:space:]]*=|mgmt[_-]?key' "$OUT" 2>/dev/null || true)
  echo "脱敏: 处理 $n 个文件"

  # 自证：脱敏后不该再有可识别的凭据形态。
  #
  # 这一步是硬闸 —— 检出就**不打包**并退 2。导出文件的用途是「贴给别人看」，
  # 宁可让人工介入，也不能产出一个带凭据的压缩包。
  #
  # 自证的模式必须与上面的替换规则**对应**（2026-09-05 补齐）：原来只自证
  # sk- 与 Bearer 两种，而漏掉的六种形态自证也看不见 —— 那时「脱敏自证通过」
  # 这句话是在给一个没验过的结论背书。
  # 排除占位符自身：`****REDACTED` 也满足「12 个以上非空白字符」，
  # 不排掉的话自证会把自己的替换结果当成残留凭据（实测踩到，3 行误报）。
  # 真实凭据不含 `*`，所以按这个字符排除是安全的。
  if grep -rIE 'sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{20,}|Bearer [A-Za-z0-9._~+/-]{20,}|[?&](token|access_token|auth)=[^&*[:space:]"]{12,}|\$2[aby]\$[0-9]{2}\$[A-Za-z0-9./]{20,}|://[^:@[:space:]/"]+:[^@*[:space:]/"]{6,}@' "$OUT" 2>/dev/null        | grep -qv 'REDACTED'; then
    echo "！脱敏后仍检出疑似凭据，已保留目录不打包，请人工检查：$OUT" >&2
    exit 2
  fi
  echo "脱敏自证: 未再检出凭据形态（sk- / AIza / Bearer / ?token= / bcrypt / URL 内嵌）"
else
  echo "！--raw：未脱敏，包内含明文 API Key，别外发" >&2
fi

# ── 6. 打包 ────────────────────────────────────────────────────────
tar -czf "$OUT.tar.gz" "$OUT"
rm -rf "$OUT"
echo
echo "完成: $(pwd)/$OUT.tar.gz  ($(du -h "$OUT.tar.gz" | cut -f1))"
# 不能写成 `[ "$RAW" = yes ] && echo ...` —— set -e 下它是脚本最后一条命令，
# RAW=no 时整体求值为假，脚本就以退出码 1 结束（包其实已经生成好了）。
# 接进 CI 或 `&&` 链会被当成失败。
if [ "$RAW" = yes ]; then
  echo "      未脱敏 —— 仅限本机使用"
fi
