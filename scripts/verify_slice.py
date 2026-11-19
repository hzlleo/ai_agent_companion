#!/usr/bin/env python3
"""切片验收证据包生成器（verify slice）——全链逐步校验 + 可复核证据落盘。

按固定顺序执行既有质量链，并对**每一步**做双重校验：

    1. 进程退出码必须为 0；
    2. 输出末尾（tail 窗口内）必须匹配该步骤的已知摘要正则（pytest 通过
       计数 / ruff 全绿 / mypy 成功摘要 / 根门禁链尾标记）。摘要缺失一律
       判失败——退出码 0 但没有真实执行摘要，视为「空跑」而非通过。

证据产物（幂等覆盖，同一源码状态重复运行内容一致）：

    - ``docs/evidence/logs/<slice>-<sha8>-<step>.log``：该步全量输出；
    - ``docs/evidence/<slice>-<sha8>.json``：命令、退出码、解析计数、源码
      树文件级 sha256 与聚合哈希、逐步状态。

``sha8`` 由源码树内容哈希计算（文件级 sha256 → 排序聚合），不依赖 git；
构建产物目录（``.egg-info`` / 缓存 / 依赖）不参与哈希——editable 安装会在
源码树里生成 ``*.egg-info`` 且其内容随安装环境漂移，剔除后同一份源码在
任何机器上复算得到同一标识（与 ``red_probe.py`` 复制副本时的忽略口径
一致）。CI / 审查者可用 ``--hash-only`` 复算同一标识。脚本只读源码，不改
任何仓库状态（coverage.json / 前端 coverage 目录属工具产物，不在哈希根内）。

用法::

    cd backend && pixi run verify-slice
    cd backend && pixi run verify-slice -- --with-seekdb
    cd backend && pixi run verify-slice -- --with-frontend --slice r2-hardening

Exit:
    0 = 全部步骤通过且摘要齐全；1 = 任一步失败 / 摘要缺失；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 证据产物根。
EVIDENCE_ROOT = REPO_ROOT / "docs" / "evidence"

#: 日志目录。
LOG_ROOT = EVIDENCE_ROOT / "logs"

#: 摘要匹配的 tail 窗口（字符）。
_TAIL_WINDOW = 4000

#: 参与源码树哈希的文件 / 目录根（相对仓库根；目录递归，跳过构建产物）。
_HASH_ROOTS: tuple[str, ...] = (
    "backend/src",
    "backend/pixi.toml",
    "backend/pyproject.toml",
    "backend/red_manifest.toml",
    "scripts",
    "docker/tool-sandbox-agent/src",
    "docker/tool-sandbox-agent/server.py",
    "docker/browser-sidecar/src",
    "docker/browser-sidecar/server.py",
    "frontend/src",
    "frontend/package.json",
    "frontend/pixi.toml",
    "pixi.toml",
)

#: 哈希时跳过的路径段（构建产物 / 依赖 / 缓存）。
_SKIP_PARTS = frozenset(
    {"__pycache__", ".pixi", "node_modules", ".mypy_cache", ".pytest_cache", "coverage"}
)

#: 哈希时跳过的目录名前缀（``*.egg-info`` 由 editable 安装生成，内容随环境漂移）。
_SKIP_DIR_SUFFIXES = (".egg-info",)

#: 哈希时跳过的文件名模式。
_SKIP_SUFFIXES = (".pyc", ".pyo")


@dataclass(frozen=True)
class SliceStep:
    """单个验收步骤定义。

    Attributes:
        name: 步骤标识（日志与 JSON 键）。
        cwd: 执行工作目录（相对仓库根）。
        argv: 命令参数列表。
        summary_regex: 输出尾部必须匹配的摘要正则（第 1 组为解析计数）。
        description: 步骤说明。
        env: 额外环境变量。
    """

    name: str
    cwd: str
    argv: tuple[str, ...]
    summary_regex: str
    description: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class StepResult:
    """单步执行结果（证据 JSON 的 steps 元素）。

    Attributes:
        step: 步骤定义。
        status: passed / failed / skipped。
        exit_code: 进程退出码（未执行为 None）。
        summary_matched: 摘要正则是否命中。
        parsed_count: 摘要中解析出的计数（无捕获组为 None）。
        duration_ms: 执行耗时毫秒。
        log_path: 全量日志相对路径。
        failure: 失败说明。
    """

    step: SliceStep
    status: str = "skipped"
    exit_code: int | None = None
    summary_matched: bool = False
    parsed_count: float | None = None
    duration_ms: int = 0
    log_path: str = ""
    failure: str = ""


def _base_steps() -> list[SliceStep]:
    """返回固定基线段（顺序即执行顺序）。"""
    return [
        SliceStep(
            name="tests-all",
            cwd="backend",
            argv=("pixi", "run", "tests-all"),
            summary_regex=r"== (\d+) passed",
            description="后端全量测试（单元 + 集成 + harness 内嵌）",
        ),
        SliceStep(
            name="coverage-gates",
            cwd="backend",
            argv=("pixi", "run", "coverage-gates"),
            summary_regex=r"\[check_coverage_per_module\] 全部 (\d+) 组 ≥",
            description="全量测试 + 按模块覆盖率门禁（≥80% enforce）",
        ),
        SliceStep(
            name="lint",
            cwd="backend",
            argv=("pixi", "run", "lint"),
            summary_regex=r"All checks passed",
            description="ruff 静态检查",
        ),
        SliceStep(
            name="typecheck",
            cwd="backend",
            argv=("pixi", "run", "typecheck"),
            summary_regex=r"Success: no issues found in (\d+) source files",
            description="mypy 严格类型检查",
        ),
        SliceStep(
            name="gates",
            cwd=".",
            argv=("pixi", "run", "gates"),
            summary_regex=r"\[prose_pin_check\] 报告完成",
            description="根工程门禁链（注释 / 棘轮 / noqa / 漂移 / 红探针 / 文档真相 / 断言棘轮 / 导出面 / prose pin）",
        ),
    ]


def _optional_steps(*, with_seekdb: bool, with_frontend: bool) -> list[SliceStep]:
    """返回可选追加步骤（SeekDB 真引擎 / 前端全链）。"""
    steps: list[SliceStep] = []
    if with_seekdb:
        steps.append(
            SliceStep(
                name="tests-seekdb",
                cwd="backend",
                argv=("pixi", "run", "tests-seekdb"),
                summary_regex=r"== (\d+) passed",
                description="SeekDB 真引擎强制套件（前置：本地 seekdb 容器）",
                env={"SEEKDB_TEST_URL": "127.0.0.1:2881"},
            )
        )
    if with_frontend:
        steps.extend(
            [
                SliceStep(
                    name="frontend-test",
                    cwd="frontend",
                    argv=("pixi", "run", "test"),
                    summary_regex=r"Tests\s+(\d+) passed",
                    description="前端全量测试（vitest）",
                ),
                SliceStep(
                    name="frontend-coverage",
                    cwd="frontend",
                    argv=("pixi", "run", "coverage"),
                    summary_regex=r"Statements\s*:\s*([\d.]+)%",
                    description="前端覆盖率门禁（perFile ≥80% enforce）",
                ),
                SliceStep(
                    name="frontend-lint",
                    cwd="frontend",
                    argv=("pixi", "run", "lint"),
                    summary_regex=r"Pixi task \(lint\)",
                    description="前端 tsc + eslint",
                ),
                SliceStep(
                    name="frontend-build",
                    cwd="frontend",
                    argv=("pixi", "run", "build"),
                    summary_regex=r"built in ([0-9.]+)s",
                    description="前端生产构建",
                ),
            ]
        )
    return steps


def _iter_hash_files() -> list[Path]:
    """枚举参与源码树哈希的文件（排序稳定，跳过构建产物）。"""
    files: list[Path] = []
    for raw in _HASH_ROOTS:
        path = REPO_ROOT / raw
        if path.is_file():
            files.append(path)
            continue
        if not path.is_dir():
            continue
        for candidate in path.rglob("*"):
            if not candidate.is_file():
                continue
            rel_parts = candidate.relative_to(REPO_ROOT).parts
            if any(part in _SKIP_PARTS for part in rel_parts):
                continue
            if any(part.endswith(_SKIP_DIR_SUFFIXES) for part in rel_parts):
                continue
            if candidate.suffix in _SKIP_SUFFIXES:
                continue
            files.append(candidate)
    return sorted(files, key=lambda item: item.relative_to(REPO_ROOT).as_posix())


def compute_tree_digest() -> tuple[str, dict[str, str]]:
    """计算源码树聚合哈希与文件级 sha256。

    Returns:
        ``(sha8, {相对路径: sha256})``——sha8 为聚合哈希前 8 位。
    """
    file_hashes: dict[str, str] = {}
    for path in _iter_hash_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        file_hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    aggregate = hashlib.sha256()
    for rel in sorted(file_hashes):
        aggregate.update(f"{file_hashes[rel]}  {rel}\n".encode())
    return aggregate.hexdigest()[:8], file_hashes


def _sanitized_env(extra: dict[str, str]) -> dict[str, str]:
    """构造子进程环境（剔除外层 pixi 注入的清单 / 项目根，防嵌套串味）。"""
    env = dict(os.environ)
    env.pop("PIXI_WORKSPACE_MANIFEST", None)
    env.pop("PIXI_PROJECT_ROOT", None)
    env.pop("PYTEST_ADDOPTS", None)
    env.update(extra)
    return env


def _tail(text: str) -> str:
    """取输出尾部窗口（摘要必须出现在末尾附近）。"""
    return text[-_TAIL_WINDOW:]


def _run_step(step: SliceStep, *, slice_name: str, sha8: str) -> StepResult:
    """执行单个步骤并校验退出码 + 摘要。

    Args:
        step: 步骤定义。
        slice_name: 切片名（日志文件名）。
        sha8: 源码树哈希前缀（日志文件名）。

    Returns:
        步骤结果（失败说明写入 failure 字段）。
    """
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_file = LOG_ROOT / f"{slice_name}-{sha8}-{step.name}.log"
    command = " ".join(shlex.quote(part) for part in step.argv)
    started = time.monotonic()
    completed = subprocess.run(
        list(step.argv),
        cwd=REPO_ROOT / step.cwd,
        env=_sanitized_env(step.env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    output = completed.stdout
    log_file.write_text(output, encoding="utf-8")

    result = StepResult(
        step=step,
        exit_code=completed.returncode,
        duration_ms=duration_ms,
        log_path=log_file.relative_to(REPO_ROOT).as_posix(),
    )
    if completed.returncode != 0:
        result.status = "failed"
        result.failure = f"退出码 {completed.returncode}（命令: cd {step.cwd} && {command}）"
        return result
    match = re.search(step.summary_regex, _tail(output), re.MULTILINE)
    if match is None:
        result.status = "failed"
        result.failure = (
            f"输出末尾缺少摘要 {step.summary_regex!r}（空跑视为失败；命令: cd {step.cwd} && {command}）"
        )
        return result
    result.summary_matched = True
    if match.groups():
        raw = match.group(1)
        try:
            result.parsed_count = float(raw)
        except ValueError:
            result.parsed_count = None
    result.status = "passed"
    return result


def _write_evidence(
    *,
    slice_name: str,
    sha8: str,
    before_hashes: dict[str, str],
    results: list[StepResult],
    started_ms: int,
    finished_ms: int,
) -> Path:
    """写出证据 JSON（确定性字段 + 逐步状态）。

    Args:
        slice_name: 切片名。
        sha8: 源码树哈希前缀。
        before_hashes: 执行前的文件级哈希。
        results: 步骤结果列表。
        started_ms: 开始时间（epoch 毫秒）。
        finished_ms: 结束时间（epoch 毫秒）。

    Returns:
        证据 JSON 路径。
    """
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_ROOT / f"{slice_name}-{sha8}.json"
    failed = [item.step.name for item in results if item.status == "failed"]
    payload = {
        "slice": slice_name,
        "sha8": sha8,
        "started_at_ms": started_ms,
        "finished_at_ms": finished_ms,
        "result": "pass" if not failed and all(
            item.status == "passed" for item in results
        ) else "fail",
        "failed_steps": failed,
        "source_tree": {
            "algorithm": "sha256 文件级逐文件 → 排序聚合",
            "roots": list(_HASH_ROOTS),
            "file_count": len(before_hashes),
            "aggregate_sha256": hashlib.sha256(
                "".join(
                    f"{before_hashes[rel]}  {rel}\n" for rel in sorted(before_hashes)
                ).encode()
            ).hexdigest(),
        },
        "file_hashes": before_hashes,
        "steps": [
            {
                "name": item.step.name,
                "description": item.step.description,
                "command": " ".join(item.step.argv),
                "cwd": item.step.cwd,
                "status": item.status,
                "exit_code": item.exit_code,
                "summary_regex": item.step.summary_regex,
                "summary_matched": item.summary_matched,
                "parsed_count": item.parsed_count,
                "duration_ms": item.duration_ms,
                "log_path": item.log_path,
                "failure": item.failure,
            }
            for item in results
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：按序执行验收链并落证据包。

    Args:
        argv: 命令行参数（None 取 sys.argv[1:]）。

    Returns:
        进程退出码：0 全绿 / 1 任一步失败；``--hash-only`` 时恒 0。

    Raises:
        SystemExit: argparse 参数错误。
    """
    parser = argparse.ArgumentParser(description="切片验收证据包生成器（全链逐步校验）")
    parser.add_argument("--slice", default="slice", help="切片名（证据文件名前缀）")
    parser.add_argument("--with-seekdb", action="store_true", help="追加 SeekDB 真引擎套件")
    parser.add_argument("--with-frontend", action="store_true", help="追加前端 test / coverage / lint / build")
    parser.add_argument("--only", default="", help="只执行指定步骤（逗号分隔，调试用）")
    parser.add_argument("--hash-only", action="store_true", help="只打印源码树 sha8 与文件数")
    args = parser.parse_args(argv)

    sha8, before_hashes = compute_tree_digest()
    if args.hash_only:
        print(f"[verify_slice] sha8={sha8} files={len(before_hashes)}")
        return 0

    steps = _base_steps() + _optional_steps(
        with_seekdb=args.with_seekdb, with_frontend=args.with_frontend
    )
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        steps = [step for step in steps if step.name in wanted]
        if not steps:
            print(f"[verify_slice] --only 未命中任何步骤: {args.only}", file=sys.stderr)
            return 2

    started_ms = time.time_ns() // 1_000_000
    results: list[StepResult] = []
    stop = False
    print(f"[verify_slice] slice={args.slice} sha8={sha8} steps={len(steps)}")
    for step in steps:
        if stop:
            results.append(StepResult(step=step, failure="前序步骤失败，未执行"))
            continue
        print(f"[verify_slice] 执行 {step.name}: cd {step.cwd} && {' '.join(step.argv)}")
        result = _run_step(step, slice_name=args.slice, sha8=sha8)
        results.append(result)
        if result.status == "passed":
            count = "" if result.parsed_count is None else f" count={result.parsed_count:g}"
            print(f"[verify_slice]   PASS {step.name} ({result.duration_ms} ms){count}")
        else:
            print(f"[verify_slice]   FAIL {step.name}: {result.failure}")
            stop = True

    finished_ms = time.time_ns() // 1_000_000
    evidence_path = _write_evidence(
        slice_name=args.slice,
        sha8=sha8,
        before_hashes=before_hashes,
        results=results,
        started_ms=started_ms,
        finished_ms=finished_ms,
    )
    after_sha8, _ = compute_tree_digest()
    if after_sha8 != sha8:
        print(
            f"[verify_slice] 警告：执行后源码树哈希变化（{sha8} -> {after_sha8}）——"
            "步骤污染了源码树，请复查",
            file=sys.stderr,
        )
    failed = [item.step.name for item in results if item.status != "passed"]
    print(f"[verify_slice] 证据包: {evidence_path.relative_to(REPO_ROOT)}")
    if failed:
        print(f"[verify_slice] 结果 FAIL: {', '.join(failed)}", file=sys.stderr)
        return 1
    counts = ", ".join(
        f"{item.step.name}={item.parsed_count:g}"
        for item in results
        if item.parsed_count is not None
    )
    print(f"[verify_slice] 结果 PASS（{counts or '无计数步骤'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
