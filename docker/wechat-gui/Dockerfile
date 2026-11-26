# GUI 微信实验轨沙盒 —— Xorg + uinput 容器化 PoC 门镜像
#
# 基底：debian:bookworm-slim（tag + digest 固定；digest 为构建时拉取的
# 官方多架构索引，tag 变更须同步本注释与 digest）。
#
# 驱动选择（文档化决策）：容器内不运行 udev；libinput 依赖 udev 枚举输入
# 设备，因此输入面固定用 xserver-xorg-input-evdev + Xorg 显式 InputDevice
# 段（entrypoint 启动期由 uinputd 按实际事件节点生成配置）。
#
# 刻意不安装微信客户端：Live 阶段由部署者经 noVNC 登录后按部署文档装入
# 官方 .deb（仓库不分发；见 README「noVNC 登录与微信客户端安装」）。
#
# 安全边界：镜像本身无 privileged 要求；生产用 `--device /dev/uinput`
# （可选 `--device /dev/dri`）与最小能力集运行。仅 PoC 宿主运行器使用
# `--privileged`（见 poc/run_stack_poc.sh 与本目录 README）。
#
# 构建上下文 = 本目录（compose 经 ./wechat-gui 引用）。

ARG DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security

FROM debian:bookworm-slim@sha256:e67eeead736ff7bbe258ef088bff683635c561b1fcfefad0806c1df4ab49ce0e

ARG DEBIAN_MIRROR
ARG DEBIAN_SECURITY_MIRROR

# 镜像源固定为清华镜像（构建机与部署机网络面一致；bookworm 基镜像使用
# deb822 源文件，先移除再写传统 sources.list）。
RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf 'deb %s bookworm main\ndeb %s bookworm-updates main\ndeb %s bookworm-security main\n' \
        "${DEBIAN_MIRROR}" "${DEBIAN_MIRROR}" "${DEBIAN_SECURITY_MIRROR}" > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        xserver-xorg-core \
        xserver-xorg-video-dummy \
        xserver-xorg-input-evdev \
        x11-xserver-utils \
        x11-utils \
        xinput \
        x11vnc \
        novnc \
        websockify \
        imagemagick \
        xclip \
        python3-minimal \
        python3-pil \
        util-linux \
        tini \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行用户（Xorg / x11vnc / websockify 以该用户运行；uinputd 以
# root 打开 /dev/uinput 并在创建后把事件节点交给该用户）。
RUN groupadd --gid 1000 wechat \
    && useradd --uid 1000 --gid wechat --create-home --shell /bin/bash wechat

WORKDIR /app

# 树内源码（PoC 门 / 控制面 / 配置生成）。容器内经 PYTHONPATH 导入，
# 不经 pip（运行面只有 stdlib + Debian python3-pil）。
COPY src ./src
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    WECHAT_GUI_DISPLAY=:99 \
    WECHAT_GUI_WIDTH=1280 \
    WECHAT_GUI_HEIGHT=800 \
    WECHAT_GUI_VNC_PASSWDFILE=/run/secrets/wechat_gui_vnc_password

# 暴露面：x11vnc 与 noVNC/websockify（compose 仅映射到宿主回环地址）。
EXPOSE 5900 6080

# 存活探测：X 服务可连接 + noVNC 页面可访问（stdlib；镜像无 curl）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import subprocess, urllib.request; subprocess.run(['xdpyinfo', '-display', ':99'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3); urllib.request.urlopen('http://127.0.0.1:6080/vnc.html', timeout=3).read(1)"]

# tini 作 PID 1（信号转发 + 子进程收割）；入口脚本编排 Xorg / VNC / noVNC。
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
