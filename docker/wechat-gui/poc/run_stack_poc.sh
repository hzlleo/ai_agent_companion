#!/usr/bin/env bash
# GUI 微信实验轨沙盒 —— 宿主 PoC 门运行器。
#
# 职责：构建本目录镜像，在本机以容器运行容器内 Xorg + uinput 栈的门脚本，
# 把单行 JSON 摘要落到 docs/evidence/poc/ 并打印 verdict。
#
# **宿主注入面（必须读）**：uinput 设备是内核全局对象——容器内 uinputd 创建
# 的 `wechat-gui virtual keyboard/pointer` 会被宿主图形会话（本机 KDE :0 或
# 同机其它 X 会话）当作从设备接入并接收注入。本运行器因此：
#   1. 构建 / 运行前探测宿主活动图形会话（任何 X11 / Wayland 套接字存在即
#      视为活动，不要求可连接）；默认拒绝运行并打印说明；Wayland 会话直接
#      拒绝（xinput float 摘不掉原生接入的设备）；
#   2. 起容器前若 sysfs 已存在同名残留设备（serve 容器仍在跑，或上次异常
#      退出后未清理）直接拒绝——同名旧设备与本次新建设备并存时无法按名字
#      区分浮动结果；
#   3. 仅 WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 放行，放行后：先用镜像内
#      xinput 验证可连宿主全部 X 会话，再逐会话探测 / 浮动；运行器把设备
#      置入安全态（每个同名行均 floating，或经 udevadm 正证据确认已忽略）
#      后落 /run/wechat-gui/host-float.done，容器才启动 Xorg；证据 JSON 的
#      host_session_guard 记录探测 / 放行 / 浮动 / 忽略 / 同步耗时；
#   4. 运行结束校验宿主已无该设备残留（非空即 exit 1）。
# 生产形态的根治手段是仓库内 udev 参考件
# （docker/wechat-gui/udev/90-wechat-gui-ignore.rules，开发机已实测）；
# 部署机须复验；无法配置 udev 的部署机按 VM 隔离评估。
#
# 运行形态：WECHAT_GUI_POC_RUN_MODE=privileged（缺省，PoC 兼容性）或
# unprivileged（生产同款：--device /dev/uinput + c 13:* rwm + 最小能力集 +
# no-new-privileges，不 privileged）。
#
# 幂等：同一机器重复运行生成带新 UTC 时间戳的证据文件；容器名带 PID，
# trap 保证异常退出也清理容器与临时密码文件。

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../../.." && pwd -P)"
EVIDENCE_DIR="${REPO_ROOT}/docs/evidence/poc"
IMAGE_TAG="${WECHAT_GUI_IMAGE:-wechat-gui-poc:local}"
RUN_MODE="${WECHAT_GUI_POC_RUN_MODE:-privileged}"
BUILD_ATTEMPTS=3
HOSTNAME_SAFE="$(hostname | tr -cd 'A-Za-z0-9._-')"
UTC_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_FILE="wechat-gui-stack-${HOSTNAME_SAFE}-${RUN_MODE}-${UTC_STAMP}.json"
CONTAINER_NAME="wechat-gui-poc-$$"
CONTAINER_EVIDENCE_PATH="/tmp/wechat-gui-poc-evidence.json"
TMP_DIR=""
HOST_SESSION_ACTIVE=0
HOST_SESSION_WAYLAND=0
HOST_DISPLAY=""
HOST_DISPLAYS=()
ALLOW_HOST_SESSION=0
FLOAT_WAIT=0
FLOAT_SYNC_MS=0
FLOATED_DEVICES=()
IGNORED_DEVICES=()
IGNORED_BASIS=""
DEVICE_NAMES=("wechat-gui virtual keyboard" "wechat-gui virtual pointer")
X11_SOCKET_DIR="${WECHAT_GUI_POC_X11_SOCKET_DIR:-/tmp/.X11-unix}"
WAYLAND_SOCKET_DIR="${WECHAT_GUI_POC_WAYLAND_SOCKET_DIR:-${XDG_RUNTIME_DIR:-}}"
WAYLAND_SCAN_ROOT="${WECHAT_GUI_POC_WAYLAND_SCAN_ROOT:-/run/user}"
SYSFS_INPUT_DIR="${WECHAT_GUI_POC_SYSFS_INPUT_DIR:-/sys/class/input}"
HOST_INPUT_DIR="${WECHAT_GUI_POC_HOST_INPUT_DIR:-/dev/input}"

log() {
    printf '[wechat-gui-poc] %s\n' "$*" >&2
}

cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    if [[ -n "${TMP_DIR}" ]]; then
        rm -rf -- "${TMP_DIR}"
    fi
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        log "缺少命令：$1"
        exit 1
    fi
}

# 宿主会话探测按「套接字存在即活动」fail-closed：不依赖 xdpyinfo 可连
# （连接失败恰恰可能是会话存在但授权受限，此时更不能假设没有会话）。
# 同一宿主可能有多个 X 会话（:0 / :1 …），每个都会独立接入内核全局的
# uinput 设备，因此全部显示号都要收集并逐个探测 / 浮动。Wayland 目录先取
# XDG_RUNTIME_DIR，再回落扫描 /run/user/*（每用户运行时目录）。
detect_host_session() {
    local socket dir
    HOST_DISPLAYS=()
    for socket in "${X11_SOCKET_DIR}"/X*; do
        [[ -e "${socket}" ]] || continue
        HOST_DISPLAYS+=(":${socket##*/X}")
    done
    if [[ "${#HOST_DISPLAYS[@]}" -gt 0 ]]; then
        HOST_DISPLAY="${HOST_DISPLAYS[0]}"
        HOST_SESSION_ACTIVE=1
    fi
    local -a wayland_dirs=()
    if [[ -n "${WAYLAND_SOCKET_DIR}" ]]; then
        wayland_dirs+=("${WAYLAND_SOCKET_DIR}")
    fi
    if [[ -z "${WECHAT_GUI_POC_WAYLAND_SOCKET_DIR:-}" ]]; then
        for dir in "${WAYLAND_SCAN_ROOT}"/*; do
            [[ -d "${dir}" ]] || continue
            wayland_dirs+=("${dir}")
        done
    fi
    for dir in "${wayland_dirs[@]}"; do
        for socket in "${dir}"/wayland-*; do
            [[ -e "${socket}" ]] || continue
            HOST_SESSION_ACTIVE=1
            HOST_SESSION_WAYLAND=1
            break 2
        done
    done
    [[ "${HOST_SESSION_ACTIVE}" -eq 1 ]]
}

host_device_present() {
    local name="$1"
    local path
    for path in "${SYSFS_INPUT_DIR}"/event*/device/name; do
        [[ -e "${path}" ]] || continue
        if [[ "$(head -n 1 "${path}" 2>/dev/null)" == "${name}" ]]; then
            return 0
        fi
    done
    return 1
}

host_devices_present() {
    local name
    for name in "${DEVICE_NAMES[@]}"; do
        if ! host_device_present "${name}"; then
            return 1
        fi
    done
    return 0
}

# 起容器前宿主不得已有同名设备：旧设备（仍在运行的 serve 容器或上次异常
# 退出残留）与新设备同名并存时，xinput 按名字浮动 / 判定无法区分二者，
# 「任一行 floating」会把仍接在宿主上的新设备误判为安全。逐名字判定：
# 两台中的**任一**残留即拒绝——只残留一台同样会让该名字的浮动结果含混。
reject_residual_host_devices() {
    local name
    for name in "${DEVICE_NAMES[@]}"; do
        if ! host_device_present "${name}"; then
            continue
        fi
        cat >&2 <<EOF
[wechat-gui-poc] 拒绝运行：宿主已存在 wechat-gui virtual keyboard/pointer。
[wechat-gui-poc] 常见来源：仍在运行的 serve 容器，或上次异常退出后的内核残留。
[wechat-gui-poc] 同名旧设备与本次新建设备并存时无法按名字区分浮动结果；
[wechat-gui-poc] 请先停掉 serve 容器 / 持有进程，确认
[wechat-gui-poc] /sys/class/input/event*/device/name 中这两个名字消失后再复跑。
EOF
        exit 1
    done
    return 0
}

# 同名设备在 sysfs 中的 event 节点目录（/sys/class/input/eventN）。
host_device_event_nodes() {
    local name="$1"
    local path
    for path in "${SYSFS_INPUT_DIR}"/event*/device/name; do
        [[ -e "${path}" ]] || continue
        if [[ "$(head -n 1 "${path}" 2>/dev/null)" == "${name}" ]]; then
            printf '%s\n' "${path%/device/name}"
        fi
    done
}

# 「udev 已忽略」的正证据：对同名设备的宿主事件节点取 udev 属性，须含
# LIBINPUT_IGNORE_DEVICE=1 且无任何 ID_INPUT* 行。返回 0=正证据成立，
# 1=证据不足（不可判忽略），2=宿主无 udevadm（调用方回落到旧启发式）。
host_device_ignored_evidence() {
    local name="$1"
    local sysfs node props
    local found=0
    if ! command -v udevadm >/dev/null 2>&1; then
        return 2
    fi
    while IFS= read -r sysfs; do
        found=1
        node="${HOST_INPUT_DIR}/${sysfs##*/}"
        props="$(udevadm info -q property -n "${node}" 2>/dev/null)" || return 1
        if ! printf '%s\n' "${props}" | grep -Eq '^(E: )?LIBINPUT_IGNORE_DEVICE=1$'; then
            return 1
        fi
        if printf '%s\n' "${props}" | grep -Eq '^(E: )?ID_INPUT'; then
            return 1
        fi
    done < <(host_device_event_nodes "${name}")
    [[ "${found}" -eq 1 ]]
}

container_running() {
    docker inspect -f '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null | grep -q '^true$'
}

# 容器内探测脚本：先一次性取全部设备名，列举失败（授权 / 连接问题）以
# exit 3 退出，绝不让「看不到」冒充 ABSENT。逐名要求**全部同名行**均含
# floating 才算 SAFE；float 成功后必须重新列举复核，复核通过才结算。
# float 只按该名字的第一台设备生效，故调用方已保证起容器前无同名残留。
host_float_probe_script() {
    cat <<'SCRIPT'
set -u
listing="$(xinput list --name-only)" || exit 3
for name in "$@"; do
  if ! printf '%s\n' "${listing}" | grep -Fxq -- "${name}"; then
    printf 'ABSENT %s\n' "${name}"
    continue
  fi
  full="$(xinput list)" || exit 3
  lines="$(printf '%s\n' "${full}" | grep -F -- "${name}" || true)"
  if [[ -n "${lines}" ]] && ! printf '%s\n' "${lines}" | grep -qv 'floating'; then
    printf 'SAFE %s\n' "${name}"
    continue
  fi
  xinput float "${name}" >/dev/null 2>&1 || true
  full="$(xinput list)" || exit 3
  lines="$(printf '%s\n' "${full}" | grep -F -- "${name}" || true)"
  if [[ -n "${lines}" ]] && ! printf '%s\n' "${lines}" | grep -qv 'floating'; then
    printf 'SAFE %s\n' "${name}"
  else
    printf 'RETRY %s\n' "${name}"
  fi
done
SCRIPT
}

# 以镜像内的 xinput 对指定宿主显示会话浮动虚拟设备（宿主未安装 xinput；
# 镜像内的 xinput 只在需要一个 X 连接时使用，不参与门禁判定）。输出逐行
# ``SAFE <name>``（全部同名行均已浮动）/ ``ABSENT <name>``（xinput 看不到
# ——候选 udev 忽略，由调用方取正证据）/ ``RETRY <name>``（可见但浮动未
# 成功）。探测失败以非零退出让调用方重试。
float_host_devices() {
    local display="$1"
    local x11_mounts=()
    if [[ -n "${XAUTHORITY:-}" && -r "${XAUTHORITY}" ]]; then
        x11_mounts+=(-v "${XAUTHORITY}:/xauth:ro" -e XAUTHORITY=/xauth)
    fi
    docker run --rm \
        -e DISPLAY="${display}" \
        "${x11_mounts[@]}" \
        -v "${X11_SOCKET_DIR}:/tmp/.X11-unix:ro" \
        --entrypoint bash "${IMAGE_TAG}" \
        -c "$(host_float_probe_script)" runner "${DEVICE_NAMES[@]}"
}

# 放行前先证明镜像内的 xinput 真能连上宿主每一个活动显示会话（授权 /
# 套接字问题在此提前拒绝，而不是等到浮动阶段才发现一台都没浮动成）。
host_xinput_probe() {
    local display
    local x11_mounts=()
    if [[ -n "${XAUTHORITY:-}" && -r "${XAUTHORITY}" ]]; then
        x11_mounts+=(-v "${XAUTHORITY}:/xauth:ro" -e XAUTHORITY=/xauth)
    fi
    for display in "${HOST_DISPLAYS[@]}"; do
        if ! docker run --rm \
            -e DISPLAY="${display}" \
            "${x11_mounts[@]}" \
            -v "${X11_SOCKET_DIR}:/tmp/.X11-unix:ro" \
            --entrypoint xinput "${IMAGE_TAG}" list >/dev/null 2>&1; then
            return 1
        fi
    done
    return 0
}

# 在截止内等待两台设备在每个宿主 X 会话都进入安全态：SAFE 要求该名字的
# 全部行均 floating 且连续两轮确认；ABSENT 同样连续两轮后，再以 udevadm
# 正证据（LIBINPUT_IGNORE_DEVICE=1 且无 ID_INPUT*）判定「已忽略」，宿主
# 无 udevadm 时才回落旧启发式并记 ignored_basis=heuristic。容器提前退出
# 或截止时仍不满足返回 1。
float_until_settled() {
    local limit="$1"
    local i output token name display key evidence_rc
    local -A safe_streak=()
    local -A absent_streak=()
    local -a floated=()
    local -a ignored=()
    local saw_positive=0 saw_heuristic=0
    FLOATED_DEVICES=()
    IGNORED_DEVICES=()
    IGNORED_BASIS=""
    for ((i = 0; i < limit; i++)); do
        if host_devices_present; then
            for display in "${HOST_DISPLAYS[@]}"; do
                if output="$(float_host_devices "${display}")"; then
                    while IFS=' ' read -r token name; do
                        [[ -n "${name}" ]] || continue
                        key="${display}|${name}"
                        case "${token}" in
                            SAFE)
                                safe_streak["${key}"]=$(( ${safe_streak["${key}"]:-0} + 1 ))
                                absent_streak["${key}"]=0
                                ;;
                            ABSENT)
                                absent_streak["${key}"]=$(( ${absent_streak["${key}"]:-0} + 1 ))
                                safe_streak["${key}"]=0
                                ;;
                            *)
                                safe_streak["${key}"]=0
                                absent_streak["${key}"]=0
                                ;;
                        esac
                    done <<<"${output}"
                fi
            done
            floated=()
            ignored=()
            saw_positive=0
            saw_heuristic=0
            for name in "${DEVICE_NAMES[@]}"; do
                if ! host_device_present "${name}"; then
                    continue
                fi
                local all_safe=1 all_absent=1
                for display in "${HOST_DISPLAYS[@]}"; do
                    key="${display}|${name}"
                    [[ "${safe_streak["${key}"]:-0}" -ge 2 ]] || all_safe=0
                    [[ "${absent_streak["${key}"]:-0}" -ge 2 ]] || all_absent=0
                done
                if [[ "${all_safe}" -eq 1 ]]; then
                    floated+=("${name}")
                    continue
                fi
                if [[ "${all_absent}" -eq 1 ]]; then
                    evidence_rc=0
                    host_device_ignored_evidence "${name}" || evidence_rc=$?
                    case "${evidence_rc}" in
                        0)
                            ignored+=("${name}")
                            saw_positive=1
                            ;;
                        2)
                            ignored+=("${name}")
                            saw_heuristic=1
                            ;;
                    esac
                fi
            done
            if [[ "$(( ${#floated[@]} + ${#ignored[@]} ))" -eq "${#DEVICE_NAMES[@]}" ]]; then
                FLOATED_DEVICES=("${floated[@]}")
                IGNORED_DEVICES=("${ignored[@]}")
                if [[ "${saw_heuristic}" -eq 1 ]]; then
                    IGNORED_BASIS="heuristic"
                elif [[ "${saw_positive}" -eq 1 ]]; then
                    IGNORED_BASIS="udev_positive"
                fi
                return 0
            fi
        fi
        if ! container_running; then
            break
        fi
        sleep 0.1
    done
    return 1
}

write_run_metadata() {
    local evidence_path="$1"
    local container_status="$2"
    local floated_json residual_json ignored_json displays_json
    shift 2
    floated_json="$(python3 - "${FLOATED_DEVICES[@]:-}" <<'PY'
import json
import sys

print(json.dumps([item for item in sys.argv[1:] if item], ensure_ascii=False))
PY
)"
    ignored_json="$(python3 - "${IGNORED_DEVICES[@]:-}" <<'PY'
import json
import sys

print(json.dumps([item for item in sys.argv[1:] if item], ensure_ascii=False))
PY
)"
    residual_json="$(python3 - "$@" <<'PY'
import json
import sys

print(json.dumps([item for item in sys.argv[1:] if item], ensure_ascii=False))
PY
)"
    displays_json="$(python3 - "${HOST_DISPLAYS[@]:-}" <<'PY'
import json
import sys

print(json.dumps([item for item in sys.argv[1:] if item], ensure_ascii=False))
PY
)"
    python3 - "${evidence_path}" "${RUN_MODE}" "${BUILD_ATTEMPTS_USED}" \
        "${HOST_SESSION_ACTIVE}" "${HOST_DISPLAY}" "${ALLOW_HOST_SESSION}" \
        "${floated_json}" "${ignored_json}" "${residual_json}" "${container_status}" \
        "${FLOAT_WAIT}" "${FLOAT_SYNC_MS}" "${displays_json}" "${IGNORED_BASIS}" <<'PY'
import json
import sys
from pathlib import Path

(
    path,
    run_mode,
    build_attempts,
    host_active,
    host_display,
    allow_host,
    floated_json,
    ignored_json,
    residual_json,
    container_status,
    float_wait,
    float_sync_ms,
    displays_json,
    ignored_basis,
) = sys.argv[1:]
target = Path(path)
payload = json.loads(target.read_text(encoding="utf-8"))
payload["run_mode"] = run_mode
payload["build_attempts"] = int(build_attempts)
payload["container_exit_code"] = int(container_status)
payload["host_session_guard"] = {
    "host_session_active": host_active == "1",
    "host_display": host_display or None,
    "host_displays": json.loads(displays_json),
    "allow_host_session": allow_host == "1",
    "floated_devices": json.loads(floated_json),
    "ignored_devices": json.loads(ignored_json),
    "ignored_basis": ignored_basis or None,
    "residual_devices": json.loads(residual_json),
    "float_wait": float_wait == "1",
    "float_sync_ms": int(float_sync_ms),
}
target.write_text(
    json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
}

# 被单元测试 source 时只定义函数（可注入 socket 根做 fail-closed 自证），
# 不执行构建 / 运行编排。
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
    return 0
fi

trap cleanup EXIT

require_command docker
require_command hostname
require_command date
require_command python3
require_command openssl

if [[ ! -e /dev/uinput ]]; then
    log "/dev/uinput 不存在——内核未提供 uinput（或容器宿主未加载模块）"
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    log "Docker 守护进程不可用（当前用户无权限或未启动）"
    exit 1
fi

# ─── 宿主图形会话前置判定（构建 / 运行前） ────────────────────────────────────
detect_host_session || true
if [[ "${HOST_SESSION_WAYLAND}" -eq 1 ]]; then
    cat >&2 <<EOF
[wechat-gui-poc] 拒绝运行：检测到宿主 Wayland 会话（wayland-* 套接字）。
[wechat-gui-poc] 容器创建的 wechat-gui virtual* 是内核全局设备，Wayland 合成器
[wechat-gui-poc] 会直接接入；镜像内 xinput 经 XWayland 的 float 无法把它从
[wechat-gui-poc] 原生 Wayland 会话上摘除，故浮动方案在此不可行。
[wechat-gui-poc] 处理方式：部署机配置宿主 udev 规则（对 wechat-gui virtual*
[wechat-gui-poc] 置 LIBINPUT_IGNORE_DEVICE="1" 或清空 ID_INPUT*），或按 VM 隔离。
EOF
    exit 1
fi
if [[ "${HOST_SESSION_ACTIVE}" -eq 1 ]]; then
    if [[ "${WECHAT_GUI_POC_ALLOW_HOST_SESSION:-0}" == "1" ]]; then
        ALLOW_HOST_SESSION=1
        FLOAT_WAIT=1
        log "检测到宿主图形会话 ${HOST_DISPLAYS[*]}：已显式放行，设备出现后将在每个会话浮动"
    else
        cat >&2 <<EOF
[wechat-gui-poc] 拒绝运行：检测到宿主图形会话 ${HOST_DISPLAY}。
[wechat-gui-poc] uinput 设备是内核全局对象：宿主图形会话（或同机其它 X 会话）
[wechat-gui-poc] 会接入 wechat-gui virtual keyboard/pointer 并接收注入的按键
[wechat-gui-poc] 与点击，操作者桌面会被干扰。
[wechat-gui-poc] 处理方式：
[wechat-gui-poc]   - 本机复跑：WECHAT_GUI_POC_ALLOW_HOST_SESSION=1（运行器会在宿主
[wechat-gui-poc]     逐一 xinput float 这两个设备，并在证据里记录浮动结果）；
[wechat-gui-poc]   - 生产形态：部署机配置宿主 udev 规则（对 wechat-gui virtual*
[wechat-gui-poc]     置 LIBINPUT_IGNORE_DEVICE="1" 或清空 ID_INPUT*）并实测确认——
[wechat-gui-poc]     该缓解已在开发机验证，部署机待复验；
[wechat-gui-poc]   - 无法配置 udev 的部署机：按 VM 隔离评估。
EOF
        exit 1
    fi
else
    log "未检测到宿主活动图形会话（无 X11 / Wayland 套接字）：无需浮动虚拟设备"
fi

reject_residual_host_devices

case "${RUN_MODE}" in
    privileged)
        RUN_FLAGS=(--privileged)
        ;;
    unprivileged)
        RUN_FLAGS=(
            --device /dev/uinput:/dev/uinput
            --device-cgroup-rule "c 13:* rwm"
            --cap-drop ALL
            --cap-add MKNOD
            --cap-add CHOWN
            --cap-add SETUID
            --cap-add SETGID
            --cap-add KILL
            --security-opt no-new-privileges:true
        )
        ;;
    *)
        log "未知 WECHAT_GUI_POC_RUN_MODE=${RUN_MODE}（可选 privileged / unprivileged）"
        exit 1
        ;;
esac

mkdir -p "${EVIDENCE_DIR}"
TMP_DIR="$(mktemp -d)"
PASS_FILE="${TMP_DIR}/vnc.pass"
PASS_DIR_MODE="$(stat -c '%a' "${TMP_DIR}")"
if [[ "${PASS_DIR_MODE}" != "700" ]]; then
    chmod 0700 "${TMP_DIR}"
fi
# 临时密码文件 0644：容器内绑定挂载保留宿主 uid / 权限位，运行用户与宿主
# 调用者 uid 未必相同——0600 会让 x11vnc（wechat 用户）读不到；目录 0700
# 已挡住同机其他用户。
(
    umask 022
    printf '%s\n' "$(openssl rand -hex 16)" >"${PASS_FILE}"
)
chmod 0644 "${PASS_FILE}"

log "构建镜像 ${IMAGE_TAG}（最多 ${BUILD_ATTEMPTS} 次，网络抖动重试）"
BUILD_ATTEMPTS_USED=0
build_ok=0
for attempt in $(seq 1 "${BUILD_ATTEMPTS}"); do
    log "构建尝试 ${attempt}/${BUILD_ATTEMPTS}"
    BUILD_ATTEMPTS_USED="${attempt}"
    if docker build -t "${IMAGE_TAG}" "${SCRIPT_DIR}/.."; then
        build_ok=1
        log "构建成功（第 ${attempt} 次尝试）"
        break
    fi
    if [[ "${attempt}" -lt "${BUILD_ATTEMPTS}" ]]; then
        log "构建失败，5 秒后重试"
        sleep 5
    fi
done
if [[ "${build_ok}" -ne 1 ]]; then
    log "构建失败（已尝试 ${BUILD_ATTEMPTS} 次）"
    exit 1
fi

if [[ "${ALLOW_HOST_SESSION}" -eq 1 ]] && ! host_xinput_probe; then
    cat >&2 <<EOF
[wechat-gui-poc] 拒绝运行：镜像内 xinput 无法连接宿主显示 ${HOST_DISPLAYS[*]}
[wechat-gui-poc]（常见为 "Authorization required" / 套接字不可访问）。放行后
[wechat-gui-poc] 的 float 无法执行，注入会落到宿主桌面；请检查 XAUTHORITY
[wechat-gui-poc] 挂载或改用 udev 规则 / VM 隔离。
EOF
    exit 1
fi

# 证据经容器内路径写出后 docker cp 取回：不把仓库目录 bind mount 进容器，
# 因此不依赖容器 root 对宿主目录的写权限（unprivileged 形态 cap_drop ALL
# 时容器内 root 无 DAC_OVERRIDE），也不需要事后 chown 回收属主。
log "运行容器（${RUN_MODE}；证据落 ${EVIDENCE_DIR}/${EVIDENCE_FILE}）"
docker run -d \
    --name "${CONTAINER_NAME}" \
    "${RUN_FLAGS[@]}" \
    -e WECHAT_GUI_HOST_FLOAT_WAIT="${FLOAT_WAIT}" \
    -v "${PASS_FILE}:/run/secrets/wechat_gui_vnc_password:ro" \
    "${IMAGE_TAG}" poc --out "${CONTAINER_EVIDENCE_PATH}" >/dev/null
if [[ "${ALLOW_HOST_SESSION}" -eq 1 ]]; then
    log "等待设备在每个宿主会话（${HOST_DISPLAYS[*]}）进入安全态，完成后放行容器继续启动 Xorg"
    float_start_ms="$(date +%s%3N)"
    if ! float_until_settled 600; then
        log "设备未在截止内全部进入安全态：移除容器并失败，注入不发生"
        docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        exit 1
    fi
    float_end_ms="$(date +%s%3N)"
    FLOAT_SYNC_MS="$((float_end_ms - float_start_ms))"
    log "安全态结果：floated=[${FLOATED_DEVICES[*]:-}] ignored=[${IGNORED_DEVICES[*]:-}] ignored_basis=${IGNORED_BASIS:-none}（${FLOAT_SYNC_MS} ms）"
    if ! docker exec "${CONTAINER_NAME}" touch /run/wechat-gui/host-float.done; then
        log "无法在容器内落 host-float.done（root 对 /run/wechat-gui 应有写权）"
        docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        exit 1
    fi
fi

set +e
run_status="$(docker wait "${CONTAINER_NAME}" 2>/dev/null)"
wait_code=$?
set -e
if [[ -z "${run_status}" ]]; then
    run_status="${wait_code}"
fi

if ! docker cp "${CONTAINER_NAME}:${CONTAINER_EVIDENCE_PATH}" \
    "${EVIDENCE_DIR}/${EVIDENCE_FILE}"; then
    log "未取回证据文件（容器退出码 ${run_status}）"
    docker logs --tail 40 "${CONTAINER_NAME}" >&2 || true
    exit 1
fi
if [[ ! -s "${EVIDENCE_DIR}/${EVIDENCE_FILE}" ]]; then
    log "证据文件为空（容器退出码 ${run_status}）"
    exit 1
fi

residual=()
for name in "${DEVICE_NAMES[@]}"; do
    if host_device_present "${name}"; then
        residual+=("${name}")
    fi
done
write_run_metadata "${EVIDENCE_DIR}/${EVIDENCE_FILE}" "${run_status}" "${residual[@]:-}"

VERDICT="$(python3 - "${EVIDENCE_DIR}/${EVIDENCE_FILE}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
print(payload.get("verdict", "UNKNOWN"))
PY
)"
printf 'verdict=%s\n' "${VERDICT}"
log "容器退出码 ${run_status}；证据：${EVIDENCE_DIR}/${EVIDENCE_FILE}"

if [[ "${#residual[@]}" -gt 0 ]]; then
    log "宿主仍有虚拟设备残留：${residual[*]}（容器退出后应按 uinputd 生命周期消失）"
else
    log "宿主虚拟设备已随容器退出清理：无残留"
fi

if [[ "${#residual[@]}" -gt 0 ]]; then
    exit 1
fi
if [[ "${VERDICT}" != "PASS" ]]; then
    exit 1
fi
exit 0
