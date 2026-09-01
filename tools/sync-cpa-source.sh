#!/usr/bin/env bash
# 同步两个 CPA 项目的源码到宿主机，供漂移检测只读挂载。
#
# 为什么在宿主机做而不是容器内
# ----------------------------
# 容器里没有 git（python:3.12-slim 不带）。装上它意味着：镜像变大、
# 且给一个**持有明文上游 Key 且能改写 config.yaml** 的服务加出网 clone 能力。
# 那个权限组合不值得为了省一条 cron 换。
#
# 所以：宿主机 clone/pull，容器只读挂载。目录挂载是实时的 —— pull 完下次
# 打开网页就是新的，不用重启容器。
#
# 用法
# ----
#     ./tools/sync-cpa-source.sh                 # 默认落到 /opt/deploy/cpa-src
#     ./tools/sync-cpa-source.sh /srv/cpa-src    # 自定义位置
#     CPA_ONLY=1 ./tools/sync-cpa-source.sh      # 只同步 CLIProxyAPI
#
# 之后在 .env 里指到 CLIProxyAPI 那个子目录：
#     CPA_SOURCE=/opt/deploy/cpa-src/CLIProxyAPI
#     CPA_SOURCE_ROOT=/cpa-source
#
# 放进 cron 每天一次即可（漂移不是秒级的事）：
#     0 5 * * * /opt/deploy/upstream-importer/tools/sync-cpa-source.sh >>/var/log/cpa-src.log 2>&1

set -euo pipefail

DEST="${1:-/opt/deploy/cpa-src}"

# 上游仓库。CPAMP 目前只用于人工对照（它的 header 逻辑在 TypeScript 前端里，
# 不参与画像梯的自动比对）—— 拉下来是为了排障时能查证，不是给程序读的。
CPA_URL="https://github.com/router-for-me/CLIProxyAPI.git"
CPAMP_URL="https://github.com/seakee/CPA-Manager-Plus.git"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

need() {
  command -v "$1" >/dev/null 2>&1 || { log "缺少 $1，装上再跑"; exit 2; }
}
need git

sync_one() {
  local url="$1" name="$2" dir="$DEST/$2"

  if [ -d "$dir/.git" ]; then
    log "$name: 已存在，pull"
    # --ff-only：绝不产生合并提交。这个目录是只读镜像，出现本地改动说明
    # 有人手工改过 —— 那种情况应当报错停下，而不是悄悄合并。
    git -C "$dir" fetch --quiet origin
    local before after
    before="$(git -C "$dir" rev-parse --short HEAD)"
    if ! git -C "$dir" merge --ff-only --quiet FETCH_HEAD 2>/dev/null; then
      log "$name: ⚠ 无法 fast-forward —— 这个目录被本地改动过？"
      log "$name:   查看：git -C $dir status"
      log "$name:   丢弃本地改动重来：rm -rf $dir 后重跑本脚本"
      return 1
    fi
    after="$(git -C "$dir" rev-parse --short HEAD)"
    if [ "$before" = "$after" ]; then
      log "$name: 已是最新 ($after)"
    else
      log "$name: $before → $after"
      # 列出与画像梯相关的文件是否有变动 —— 有变动才需要跑漂移检测
      local touched
      touched="$(git -C "$dir" diff --name-only "$before" "$after" \
        -- 'internal/runtime/executor/*.go' 'internal/config/*.go' 2>/dev/null | head -8)"
      if [ -n "$touched" ]; then
        log "$name: ⚠ 身份头相关文件有变动，建议跑一次漂移检测："
        printf '           %s\n' "$touched"
        log "$name:   python3 -m cpa_probe.cpa_source_probe $dir"
      fi
    fi
  else
    log "$name: clone（只取最近 50 个提交，够看常量变化）"
    mkdir -p "$DEST"
    git clone --quiet --depth 50 "$url" "$dir"
    log "$name: $(git -C "$dir" rev-parse --short HEAD)"
  fi
}

log "目标目录：$DEST"
sync_one "$CPA_URL" "CLIProxyAPI"
if [ "${CPA_ONLY:-}" != "1" ]; then
  sync_one "$CPAMP_URL" "CPA-Manager-Plus"
fi

cat <<EOF

下一步（只需做一次）：在 upstream-importer 的 .env 里加两行 ——

    CPA_SOURCE=$DEST/CLIProxyAPI
    CPA_SOURCE_ROOT=/cpa-source

然后 docker compose up -d upstream-importer。之后每次本脚本 pull 完，
下次打开网页就用上新源码，不用重启容器（目录挂载是实时的）。
EOF
