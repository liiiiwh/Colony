# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

# ADR-028 D2 · MCP 平台侧运行时工具链：在 python:3.12-slim 上装 git + go + node/npm。
# 为什么：第三方 MCP server（如 Go 写的 xiaohongshu-mcp）就地跑在 backend 容器里——
# Builder 的 run_shell 需要 git clone 仓库 + go build 构建 + Popen 拉起；node 系 MCP
# 则需 node/npm。基础镜像默认没有这三者，故此处显式安装（用户接受镜像变大换最快打通，
# 后续可迁 sidecar）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
        gnupg \
        chromium \
    && rm -rf /var/lib/apt/lists/*

# 基础依赖 · headless Chromium：浏览器自动化类 MCP（如 xiaohongshu-mcp 的 go-rod）
# 需要一个本机浏览器来渲染登录二维码 / 页面。go-rod 的 LookPath 在本镜像匹配不到
# /usr/bin/chromium，故用 ROD_BROWSER_BIN 显式指路（xiaohongshu-mcp main.go 读它）；
# ENV 全局设置 → 平台 _spawn_and_wait 重启 MCP 时子进程自动继承，无需逐个 startup_command 塞。
ENV ROD_BROWSER_BIN=/usr/bin/chromium

# go 工具链（官方 tar，apt 的 golang 通常过旧；版本可按需升级）
ENV GO_VERSION=1.22.5
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) GOARCH=amd64 ;; \
         arm64) GOARCH=arm64 ;; \
         *) echo "unsupported arch: $ARCH" && exit 1 ;; \
       esac \
    && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz" -o /tmp/go.tgz \
    && tar -C /usr/local -xzf /tmp/go.tgz \
    && rm /tmp/go.tgz
ENV PATH="/usr/local/go/bin:${PATH}" \
    GOPATH=/go \
    GOBIN=/go/bin
ENV PATH="${GOBIN}:${PATH}"

# node + npm（NodeSource LTS）
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN pip install --no-cache-dir uv

WORKDIR /app

# 先拷贝依赖描述文件以利用 Docker 构建缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# 再拷贝源码
COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 9022

CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9022"]
