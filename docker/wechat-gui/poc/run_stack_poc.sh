#!/usr/bin/env bash
# GUI 微信实验轨沙盒 —— 宿主 PoC 门运行器。
#
# 职责：构建本目录镜像，在本机以特权容器运行容器内 Xorg + uinput 栈的
# 门脚本，把单行 JSON 摘要落到 docs/evidence/poc/ 并打印 verdict。
#
# 特权说明：**仅 PoC 使用 `--privileged`**——需要访问宿主 /dev/uinput 与
# 内核 uinput 设备命名空间。生产运行改为 `--device /dev/uinput` +
# `--cap-add MKNOD --cap-add CHOWN`（见 README「安全边界」与 compose）。
#
# 幂等：同一机器重复运行生成带新 UTC 时间戳的证据文件；容器名带 PID，
# trap 保证异常退出也清理容器与临时密码文件。

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../../.." && pwd -P)"
EVIDENCE_DIR="${REPO_ROOT}/docs/evidence/poc"
IMAGE_TAG="${WECHAT_GUI_IMAGE:-wechat-gui-poc:local}"
BUILD_ATTEMPTS=3
HOSTNAME_SAFE="$(hostname | tr -cd 'A-Za-z0-9._-')"
UTC_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_FILE="wechat-gui-stack-${HOSTNAME_SAFE}-${UTC_STAMP}.json"
CONTAINER_NAME="wechat-gui-poc-$$"
TMP_DIR=""

log() {
    printf '[wechat-gui-poc] %s\n' "$*" >&2
}

cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    if [[ -n "${TMP_DIR}" ]]; then
        rm -rf -- "${TMP_DIR}"
    fi
}
trap cleanup EXIT

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        log "缺少命令：$1"
        exit 1
    fi
}

require_command docker
require_command hostname
require_command date
require_command python3

if [[ ! -e /dev/uinput ]]; then
    log "/dev/uinput 不存在——内核未提供 uinput（或容器宿主未加载模块）"
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    log "Docker 守护进程不可用（当前用户无权限或未启动）"
    exit 1
fi

mkdir -p "${EVIDENCE_DIR}"
TMP_DIR="$(mktemp -d)"
PASS_FILE="${TMP_DIR}/vnc.pass"
umask 077
printf '%s\n' "$(openssl rand -hex 16 2>/dev/null || printf '%s%s' "${RANDOM}" "${RANDOM}")" >"${PASS_FILE}"

log "构建镜像 ${IMAGE_TAG}（最多 ${BUILD_ATTEMPTS} 次，网络抖动重试）"
build_ok=0
for attempt in $(seq 1 "${BUILD_ATTEMPTS}"); do
    log "构建尝试 ${attempt}/${BUILD_ATTEMPTS}"
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

log "运行容器（privileged PoC；证据落 ${EVIDENCE_DIR}/${EVIDENCE_FILE}）"
set +e
docker run --rm --privileged \
    --name "${CONTAINER_NAME}" \
    -v "${EVIDENCE_DIR}:/out" \
    -v "${PASS_FILE}:/run/secrets/wechat_gui_vnc_password:ro" \
    "${IMAGE_TAG}" poc --out "/out/${EVIDENCE_FILE}"
run_status=$?
set -e

if [[ ! -s "${EVIDENCE_DIR}/${EVIDENCE_FILE}" ]]; then
    log "未生成证据文件（容器退出码 ${run_status}）"
    exit 1
fi

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

if [[ "${VERDICT}" != "PASS" ]]; then
    exit 1
fi
exit 0
