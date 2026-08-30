# 投喂台 · CPA 上游批量导入
#
# 多架构镜像。构建与运行都不硬编码任何域名、账号或路径 ——
# 全部由环境变量或 compose 注入（见下面 ENV 块与 CMD）。
#
# 单架构本地构建：
#   docker build -f deploy/Dockerfile -t cpa-upstream-importer:local .
#
# 多架构（amd64 + arm64 + armv7）：
#   docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 \
#     -f deploy/Dockerfile -t <你的仓库>/cpa-upstream-importer:latest --push .
#
# 只用 Python 标准库 + PyYAML + bcrypt。不装 curl/wget —— 探测走 urllib，
# 容器里没有它们（cli-proxy-api 容器里也没有，2026-08-29 曾因此把
# 「wget: not found」误读成代理不通）。

# ── 构建阶段：把依赖装进独立的 prefix ──────────────────────────────
#
# 为什么要两阶段：bcrypt 在 arm/v7 与部分 musl 平台上**没有预编译 wheel**，
# 必须现场用 Rust 编译（它的 _bcrypt 是 Rust 扩展）。那需要 gcc、
# rustc、cargo、libffi-dev —— 加起来 400 MB 以上。
# 把这些留在 builder 里，运行镜像就不用背它们。
#
# amd64 与 arm64 有 manylinux wheel，走的是纯下载路径，builder 里那些
# 编译工具用不上 —— 但保留它们让**同一个 Dockerfile 在所有架构上都能构建**，
# 这正是「支持 arm64 和其他系统」的要求。
FROM python:3.12-slim AS builder

# TARGETPLATFORM 由 buildx 自动注入（linux/amd64、linux/arm64、linux/arm/v7…）。
# 单架构 docker build 时它为空，下面的判断会走「装编译工具」的保守分支。
ARG TARGETPLATFORM

# 依赖版本集中在这里，改一处即可。用 == 钉死 —— 探测工具的行为依赖
# PyYAML 的 safe_load 语义与 bcrypt 的 checkpw，不能让它们随意漂移。
ARG PYYAML_VERSION=6.0.2
ARG BCRYPT_VERSION=4.2.1

RUN set -eux; \
    # arm/v7、riscv、s390x 等没有 bcrypt wheel 的平台需要编译链。
    # amd64/arm64 有 wheel，跳过这一步能省 3-5 分钟构建时间。
    case "${TARGETPLATFORM:-unknown}" in \
      linux/amd64|linux/arm64) \
        echo "有 manylinux wheel，跳过编译工具" ;; \
      *) \
        apt-get update; \
        apt-get install -y --no-install-recommends \
          build-essential libffi-dev curl; \
        # bcrypt 4.x 的 Rust 扩展需要 cargo。用 rustup 而非 apt 的 rustc ——
        # Debian bookworm 的 rustc 版本对 pyo3 偏旧。
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
          | sh -s -- -y --profile minimal --default-toolchain stable; \
        rm -rf /var/lib/apt/lists/* ;; \
    esac

ENV PATH="/root/.cargo/bin:${PATH}" \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# 装到独立 prefix，方便整块拷进运行镜像
RUN pip install --prefix=/deps \
      "PyYAML==${PYYAML_VERSION}" \
      "bcrypt==${BCRYPT_VERSION}"

# ── 运行阶段 ────────────────────────────────────────────────────────
FROM python:3.12-slim

# OCI 标准标签。source 由 CI 注入 —— 不写死任何仓库地址，fork 也能用。
ARG VERSION=dev
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=""

LABEL org.opencontainers.image.title="cpa-upstream-importer" \
      org.opencontainers.image.description="批量导入 CPA 上游账号：探测 → 定档 → diff → 写回" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.licenses="MIT"

COPY --from=builder /deps /usr/local

WORKDIR /app

# 只拷运行需要的。legacy/ 与 docs/ 是本机排障用，不进容器。
# tests/ 也不进 —— 镜像里跑不了它们（要 config.yaml），CI 里已经跑过。
COPY cpa_probe/ /app/cpa_probe/
COPY web/       /app/web/
COPY tools/     /app/tools/
COPY server.py cli.py /app/

# 非 root 用户作为镜像默认。**实际生效的 uid 由 compose 的 user: 决定** ——
# 它必须与宿主 config.yaml 的属主一致，否则写回时 PermissionError。
#
# uid 用 ARG 而不是写死 10001：某些部署的 config.yaml 属主是特定 uid，
# 让构建方能对齐，省掉 compose 里再覆盖一次。
ARG APP_UID=10001
ARG APP_GID=10001
RUN set -eux; \
    groupadd -g "${APP_GID}" -r importer 2>/dev/null || true; \
    useradd -r -u "${APP_UID}" -g "${APP_GID}" -m importer 2>/dev/null || \
      useradd -r -u "${APP_UID}" -m importer; \
    mkdir -p /data /backups; \
    chown -R "${APP_UID}:${APP_GID}" /app /backups
USER ${APP_UID}

# 容器内绑 0.0.0.0 是对的 —— 只监听 127.0.0.1 的话 compose 的端口映射
# 到不了。暴露面由 compose 的 `ports: 127.0.0.1:8765:8765` 控制。
#
# 这些值全部可被 compose / docker run -e 覆盖，镜像里没有任何硬编码假设。
# CPA_UPSTREAM_URL 默认用 compose 网络里的服务名；独立部署时改成可达地址。
#
# 这里**故意不声明 IMPORTER_TOKEN**。
#
# 原来有一行 IMPORTER_TOKEN=""，buildx 报 SecretsUsedInArgOrEnv 警告。
# 那个警告是对的，尽管值是空串：ENV 会写进镜像元数据，docker inspect 与
# 镜像层历史里都能看到这个键。声明一个空的密钥键没有任何好处 ——
# server.py 的 --token 默认值本就是 os.environ.get("IMPORTER_TOKEN", "")，
# 环境变量不存在时取到同样的空串，行为完全一致。
#
# 真实 token 由 compose 从 .env 注入（docker-compose.yml 的 environment 段），
# 或 docker run -e IMPORTER_TOKEN=...。两条路都不经过镜像。
ENV IMPORTER_HOST=0.0.0.0 \
    IMPORTER_PORT=8765 \
    IMPORTER_CONFIG=/data/config.yaml \
    IMPORTER_BACKUP_DIR=/backups \
    CPA_UPSTREAM_URL=http://cli-proxy-api:8317 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8765

# 容器里没有 curl/wget，用 python 自己探活
HEALTHCHECK --interval=30s --timeout=8s --start-period=10s --retries=3 \
  CMD ["python3", "-c", \
       "import urllib.request,sys,os; p=os.environ.get('IMPORTER_PORT','8765'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/',timeout=5).status==200 else 1)"]

# 显式传每个参数，不靠 argparse 读环境变量的默认值 —— 默认值是隐式行为，
# 改了代码可能悄悄失效，而「写盘了但 CPA 没生效」是这个工具最难被发现的故障。
#
# token 不注入也能用 —— 直接拿 CPA 后台管理密码登录（服务端 bcrypt 比对）。
# 要固定 token：docker run -e IMPORTER_TOKEN=$(openssl rand -hex 20) ...
ENTRYPOINT ["sh", "-c", "exec python3 server.py \
--config \"$IMPORTER_CONFIG\" \
--host \"$IMPORTER_HOST\" \
--port \"$IMPORTER_PORT\" \
--cpa-url \"$CPA_UPSTREAM_URL\" \
--backup-dir \"$IMPORTER_BACKUP_DIR\" \
\"$@\"", "--"]
