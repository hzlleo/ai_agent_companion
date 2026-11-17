# 浏览器自动化 sidecar —— 加固镜像（mcr playwright/python 基底）
#
# 基底选择（与 tool-sandbox 的 python:3.12-slim 差异——文档化决策）：
#   - mcr.microsoft.com/playwright/python:<ver>-noble 预装 Python + playwright
#     驱动 + chromium 及全部系统依赖——浏览器运行时的依赖面（共享库 /
#     字体 / headless shell）由官方镜像维护，避免手工 apt 依赖漂移；
#   - playwright pip 版本钉版对齐基底标签（pyproject: playwright==1.53.0
#     ↔ v1.53.0-noble）——驱动与浏览器构建错配是部署期唯一致命形态；
#   - 基底 tag 变更须同步 pyproject 钉版 + 本注释。
#
# 加固面（与 docker-compose.yml 的运行期 flags 共同构成 sidecar 边界——
# 见本目录 README.md「安全边界」一节）：
#   - tini 作 PID 1（信号转发 + 僵尸进程收割——chromium 子进程树需要）；
#   - 非 root 用户（基底自带 pwuser uid 1000——chromium 以非 root 运行
#     时需要 --no-sandbox，已在启动参数中固定）；
#   - 构建期 pip --no-cache-dir（无缓存残留）；
#   - HEALTHCHECK 经 stdlib urllib 探测 /health；
#   - 暴露面：仅 8766（与 compose 端口映射契约一致）。
#
# 构建上下文 = 本目录（compose 经 ./browser-sidecar 引用）。
# 注意：CI 不构建本镜像（playwright 基底约 2GB——CI 时长 / 缓存面不成
# 比；见 .github/workflows/ci.yml browser-sidecar job 注释）——构建与
# 运行验证在部署机执行（README「部署验证」）。

FROM mcr.microsoft.com/playwright/python:v1.53.0-noble

# tini：PID 1 信号转发 + 僵尸进程收割（chromium 子进程树收割面）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖 + 源码（pyproject 声明运行时依赖——fastapi/uvicorn/pydantic/
# playwright 钉版面；playwright 基底已预装浏览器，pip 解析同名版本不
# 重复下载浏览器二进制）。
COPY pyproject.toml ./
COPY src ./src
COPY server.py ./
RUN pip install --no-cache-dir --disable-pip-version-check .

# profiles 持久化挂载点（compose 卷注入；非 root 可写）。
RUN mkdir -p /app/profiles && chown pwuser:pwuser /app/profiles

# 运行期环境（读兼容 + 日志直通 stdout）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BROWSER_SIDECAR_HOST=0.0.0.0 \
    BROWSER_SIDECAR_PORT=8766 \
    BROWSER_SIDECAR_PROFILES_DIR=/app/profiles

USER pwuser

EXPOSE 8766

# 存活探测：/health 无鉴权面（stdlib urllib——基底无 curl）；
# 端口与 BROWSER_SIDECAR_PORT 缺省一致（改端口须同步此探针——README「运行」）。
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=2)"]

# tini 入口（-- 分隔 CMD）。
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "server.py"]
