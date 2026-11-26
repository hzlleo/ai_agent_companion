#!/usr/bin/env bash
# GUI 微信实验轨沙盒 —— 容器入口编排。
#
# 启动顺序（任一环节失败即整体失败，不留半起状态）：
#   1. 建立非 root 运行用户与运行目录；
#   2. 以 root 启动 uinputd：创建键盘 / 绝对指针内核设备、落盘设备信息与
#      Xorg 静态配置、把事件节点交给运行用户、开放控制面 socket；
#   3. 以运行用户启动 Xorg（dummy 1280x800x24、AutoAddDevices 关闭）；
#   4. 设置根窗口织纹（截图校验需要非全同色画面）；
#   5. 启动 x11vnc（-forever -shared -passwdfile）与 websockify/noVNC；
#   6. `serve` 模式等待子进程；`poc` 模式执行门脚本并按门禁结果退出。
#
# 用法：
#   entrypoint.sh serve            # 常驻（compose / 手工运行）
#   entrypoint.sh poc [参数...]    # 跑 PoC 门（参数透传 poc_gate）
#
# 安全：本脚本以 root 启动，但 Xorg / x11vnc / websockify 均以 wechat
# 用户运行；生产不需要 privileged（见 README「安全边界」）。

set -Eeuo pipefail
IFS=$'\n\t'

readonly DISPLAY_NUM="${WECHAT_GUI_DISPLAY:-:99}"
readonly WIDTH="${WECHAT_GUI_WIDTH:-1280}"
readonly HEIGHT="${WECHAT_GUI_HEIGHT:-800}"
readonly VNC_PORT="${WECHAT_GUI_VNC_PORT:-5900}"
readonly NOVNC_PORT="${WECHAT_GUI_NOVNC_PORT:-6080}"
readonly RUNTIME_DIR="/run/wechat-gui"
readonly DEVICES_JSON="${RUNTIME_DIR}/uinput-devices.json"
readonly CONTROL_SOCKET="${RUNTIME_DIR}/control.sock"
readonly XORG_CONF="/etc/X11/xorg.conf.d/99-wechat-gui.conf"
readonly XORG_LOG="${RUNTIME_DIR}/xorg.log"
readonly X11VNC_LOG="${RUNTIME_DIR}/x11vnc.log"
readonly NOVNC_LOG="${RUNTIME_DIR}/novnc.log"
readonly PASSFILE="${WECHAT_GUI_VNC_PASSWDFILE:-/run/secrets/wechat_gui_vnc_password}"
readonly RUN_USER="wechat"

pids=()

log() {
    printf '[entrypoint] %s\n' "$*" >&2
}

cleanup() {
    local pid
    for pid in "${pids[@]:-}"; do
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
        fi
    done
    for pid in "${pids[@]:-}"; do
        if [[ -n "${pid}" ]]; then
            wait "${pid}" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT
trap 'exit 143' TERM INT

wait_for_file() {
    local path="$1"
    local limit="$2"
    local i
    for ((i = 0; i < limit; i++)); do
        if [[ -e "${path}" ]]; then
            return 0
        fi
        sleep 0.05
    done
    log "等待文件超时：${path}"
    return 1
}

wait_for_tcp() {
    local host="$1"
    local port="$2"
    local limit="$3"
    local i
    for ((i = 0; i < limit; i++)); do
        if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
            exec 3>&-
            return 0
        fi
        sleep 0.05
    done
    log "等待端口超时：${host}:${port}"
    return 1
}

start_as_user() {
    runuser -u "${RUN_USER}" -- env HOME="/home/${RUN_USER}" "$@"
}

if [[ "$(id -u)" -ne 0 ]]; then
    log "入口脚本须以 root 启动（随后降权运行 Xorg / VNC）"
    exit 1
fi

if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
    log "运行用户 ${RUN_USER} 不存在（镜像构建异常）"
    exit 1
fi

mkdir -p "${RUNTIME_DIR}"
# root 持有目录（写设备信息 / socket），wechat 组可写（Xorg / VNC 日志）。
chown -R "root:${RUN_USER}" "${RUNTIME_DIR}"
chmod 0770 "${RUNTIME_DIR}"
# 非特权容器默认不提供 /dev/input 目录（无 udev）；由入口创建。
mkdir -p /dev/input
chmod 0755 /dev/input
# X 的本地套接字目录：非 root Xorg 无法自行创建，由入口以 root 预建。
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix

if [[ ! -r "${PASSFILE}" ]]; then
    if [[ -n "${WECHAT_GUI_VNC_PASSWORD:-}" ]]; then
        umask 077
        printf '%s\n' "${WECHAT_GUI_VNC_PASSWORD}" > "${RUNTIME_DIR}/vnc.pass"
        chown "${RUN_USER}:${RUN_USER}" "${RUNTIME_DIR}/vnc.pass"
        PASSFILE="${RUNTIME_DIR}/vnc.pass"
    else
        log "VNC 密码文件不可读：${PASSFILE}（设置 WECHAT_GUI_VNC_PASSWDFILE 或 WECHAT_GUI_VNC_PASSWORD）"
        exit 1
    fi
fi

log "启动 uinput 控制面（键盘 + 绝对指针）"
python3 -m wechat_gui_poc.uinputd \
    --socket "${CONTROL_SOCKET}" \
    --devices-json "${DEVICES_JSON}" \
    --xorg-conf "${XORG_CONF}" \
    --width "${WIDTH}" \
    --height "${HEIGHT}" \
    --username "${RUN_USER}" &
pids+=("$!")

wait_for_file "${DEVICES_JSON}" 200
wait_for_file "${CONTROL_SOCKET}" 200
log "设备与 Xorg 配置就绪：${XORG_CONF}"

log "启动 Xorg（${DISPLAY_NUM}，${WIDTH}x${HEIGHT}x24）"
start_as_user Xorg "${DISPLAY_NUM}" \
    -noreset -nolisten tcp -ac \
    -logfile "${XORG_LOG}" &
pids+=("$!")

for ((i = 0; i < 300; i++)); do
    if xdpyinfo -display "${DISPLAY_NUM}" >/dev/null 2>&1; then
        break
    fi
    sleep 0.05
done
if ! xdpyinfo -display "${DISPLAY_NUM}" >/dev/null 2>&1; then
    log "Xorg 未就绪；日志尾部："
    tail -n 40 "${XORG_LOG}" >&2 || true
    exit 1
fi

# 根窗口织纹：dummy 屏默认全黑，截图校验要求非全同色画面。
start_as_user xsetroot -display "${DISPLAY_NUM}" -mod 4 4 || true

log "启动 x11vnc（${VNC_PORT}）"
start_as_user x11vnc -display "${DISPLAY_NUM}" \
    -forever -shared -noxdamage -rfbport "${VNC_PORT}" \
    -passwdfile "${PASSFILE}" -o "${X11VNC_LOG}" &
pids+=("$!")
wait_for_tcp 127.0.0.1 "${VNC_PORT}" 200

log "启动 noVNC / websockify（${NOVNC_PORT}）"
start_as_user websockify --web=/usr/share/novnc "${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" \
    >"${NOVNC_LOG}" 2>&1 &
pids+=("$!")
wait_for_tcp 127.0.0.1 "${NOVNC_PORT}" 200

log "栈就绪：X ${DISPLAY_NUM} / VNC ${VNC_PORT} / noVNC ${NOVNC_PORT}"

if [[ "${1:-serve}" == "poc" ]]; then
    shift
    log "执行 PoC 门"
    python3 -m wechat_gui_poc.poc_gate "$@"
    exit $?
fi

# 常驻模式：任一子进程退出即视为栈失效（避免半死状态静默存活）。
set +e
wait -n
status=$?
set -e
log "子进程退出（status=${status}），收口"
exit "${status}"
