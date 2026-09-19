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

# mihomo 代理镜像。**必须声明在第一个 FROM 之前** —— 只有"全局 ARG"
# （位于任何 FROM 之上）才能被后面的 FROM 引用；写在 FROM 之后的 ARG
# 属于那个 stage 的局部作用域，对下一个 FROM 不可见，构建会报
# "base name (${MIHOMO_IMAGE}) should not be blank"。
#
# 版本可 pin：--build-arg MIHOMO_IMAGE=metacubex/mihomo:v1.19.x
ARG MIHOMO_IMAGE=metacubex/mihomo:latest

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

# ── mihomo 代理二进制 ───────────────────────────────────────────────
# 用户要求「拉一个镜像，代理和工具都跑起来」（2026-09-13）。
#
# 为什么从官方镜像 COPY 而不是自己下载：`COPY --from=image` 由 buildx 按
# **目标平台**解析 —— 构建 arm64 时取的就是 arm64 那层，amd64 同理。
# 自己 curl 下载则要手工映射 TARGETPLATFORM 到发布包名，多一处会漂的地方。
#
# MIHOMO_IMAGE 的默认值在文件顶端以全局 ARG 声明（见那里的说明）。
FROM ${MIHOMO_IMAGE} AS mihomo

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

# mihomo 二进制。上面那个 stage 只是"声明"，不 COPY 出来的话它不会进最终
# 镜像 —— stage 未被引用时 buildx 直接跳过整层。
#
# 官方镜像里二进制就在根目录 /mihomo（entrypoint 也是它），大小约 50 MB。
# 放到 /usr/local/bin 让 PATH 直接能找到。
COPY --from=mihomo /mihomo /usr/local/bin/mihomo

# 地理数据。GEOIP/GEOSITE 规则要靠这两份，缺了 mihomo 会报
# "geoip.dat not found" 并把每条 GEOIP 规则静默当成不匹配 ——
# 表现是"规则看起来在，实际全走 MATCH 兜底"，很难查。
#
# 放在 /usr/local/share/mihomo 而不是官方镜像的默认目录
# /root/.config/mihomo：bootstrap-mihomo.sh 会用 -d 指向的**挂载卷**，
# 卷一挂上去就把镜像里那份遮掉了，所以放在一个不会被遮的位置，
# 每次启动由 bootstrap 复制进卷。
COPY --from=mihomo /root/.config/mihomo/geoip.dat \
     /root/.config/mihomo/geosite.dat \
     /root/.config/mihomo/geoip.metadb \
     /usr/local/share/mihomo/

WORKDIR /app

# busybox 与 wget：只有**一个**用途 —— mihomo-proxy 容器用本镜像跑，
# 而它调的健康检查脚本 healthcheck.sh 需要这两个工具。
#
# 为什么不能用 python3 那份代替：那份是本镜像自己的容器用的（探自己的
# server.py 端口），跑在 mihomo-proxy 里探不到任何东西。
#
# 为什么必须显式装 busybox：Debian 装了 busybox **不会**自动生成
# /usr/bin/nc 这个软链（实测 2026-09-19：have busybox，MISS nc），
# 而切换出口是 PUT，busybox 的 wget 只能发 GET/POST —— 必须有 nc。
# healthcheck.sh 对 nc/wget 各做一次"命令探测 + 回退到 busybox <applet>"，
# 所以两个包都在就行，不需要额外建软链。合计约 1.3 MB。
RUN apt-get update && \
    apt-get install -y --no-install-recommends busybox wget && \
    rm -rf /var/lib/apt/lists/*

# 只拷运行需要的。legacy/ 与 docs/ 是本机排障用，不进容器。
# tests/ 也不进 —— 镜像里跑不了它们（要 config.yaml），CI 里已经跑过。
COPY cpa_probe/ /app/cpa_probe/
COPY web/       /app/web/
COPY tools/     /app/tools/
COPY server.py cli.py /app/

# mihomo 代理的配置模板与运维脚本。
#
# 为什么必须进镜像：部署方式是"VPS 只拉镜像"，宿主上不预置任何文件，
# 所以 config.yaml 必须由容器自己从模板物化（bootstrap-mihomo.sh 干这件事）。
#
# 两份 healthcheck 都要进镜像，它们服务的容器不同（实测 2026-09-19 复核）：
#   · healthcheck.py —— 给**本镜像**用（python:3.12-slim，有 python3 无 wget）
#   · healthcheck.sh —— 给 **mihomo 容器**用（metacubex/mihomo 是 busybox：
#     有 wget/nc/sed/awk，但没有 curl、也没有 python3）
# 两个都拷进来，由 bootstrap-mihomo.sh 按可用解释器挑一份物化到共享卷，
# 这样 compose 侧不需要挂任何脚本文件 —— 部署只要 4 个文件。
#
# 真实的 mihomo-subscriptions.conf 与 providers/ 含凭据，被 .dockerignore
# 排除，运行时由 compose 从环境变量提供。
COPY mihomo-manager/mihomo/config.template.yaml \
     mihomo-manager/mihomo/healthcheck.py \
     mihomo-manager/mihomo/healthcheck.sh \
     /app/mihomo-manager/mihomo/
COPY mihomo-manager/update-mihomo-subscriptions.sh \
     mihomo-manager/bootstrap-mihomo.sh \
     mihomo-manager/mihomo-subscriptions.conf.example \
     mihomo-manager/README.md \
     /app/mihomo-manager/
RUN chmod +x /app/mihomo-manager/*.sh \
             /app/mihomo-manager/mihomo/healthcheck.py \
             /app/mihomo-manager/mihomo/healthcheck.sh

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
