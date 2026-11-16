# 工具沙盒代理 —— 加固镜像（python:3.12-slim 基底）
#
# 加固面（与 docker-compose.yml 的运行期 flags 共同构成沙盒边界——
# 见本目录 README.md「安全边界」一节）：
#   - tini 作 PID 1（信号转发 + 僵尸进程收割）；
#   - 非 root 用户（uid/gid 1001，nologin shell）；
#   - 构建期 pip --no-cache-dir（无缓存残留）；运行期零写盘
#     （read_only 兼容——compose 侧挂 tmpfs /tmp）；
#   - HEALTHCHECK 经 stdlib urllib 探测 /health（slim 无 curl / wget）；
#   - 暴露面：仅 8765（与 compose 端口映射契约一致）。
#
# 构建上下文 = 本目录（compose 经 ./tool-sandbox-agent 引用）。

FROM python:3.12-slim

# tini：PID 1 信号转发 + 僵尸进程收割（slim 基镜像无 init）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行用户（uid/gid 1001）。
RUN groupadd --gid 1001 sandbox \
    && useradd --uid 1001 --gid sandbox --create-home --shell /usr/sbin/nologin sandbox

WORKDIR /app

# 依赖 + 源码（pyproject 声明运行时依赖——fastapi/uvicorn/pydantic 最小面）。
COPY pyproject.toml ./
COPY src ./src
COPY server.py ./
RUN pip install --no-cache-dir --disable-pip-version-check .

# 运行期环境（读兼容 + 日志直通 stdout）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SANDBOX_HOST=0.0.0.0 \
    SANDBOX_PORT=8765

USER 1001

EXPOSE 8765

# 存活探测：/health 无鉴权面（stdlib urllib——slim 无 curl）；
# 端口与 SANDBOX_PORT 缺省一致（改端口须同步此探针——README「运行」）。
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)"]

# tini 入口（-- 分隔 CMD）。
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "server.py"]
