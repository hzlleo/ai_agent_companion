#!/usr/bin/env python3
"""纯本地微基准（bench core）——核心纯函数的耗时基线回归面。

用途：给聊天门控 / 关系摘要派生 / 语音句切与 VAD / 关系块渲染这类高频
纯函数建立**可重复、无外部依赖**的耗时基线。测量全部在本进程内完成：
``time.perf_counter_ns`` 计时、预热后多轮取样、报告 median / p95 微秒；
不等待、不联网、不碰数据库、不读真实配置——各用例显式构造配置对象，且
配置模型的每一个字段都显式传参（初始化参数优先级高于进程环境），因此开发者
shell / ``.env`` 里的同名变量既不改变被测输入，也不会让本脚本拒跑。

模式（互斥）：

    - 默认（无旗标）：测量并报告，可与既有基线对照，恒 exit 0；
    - ``--gate``：任何用例 median 超过基线 median × ``max_ratio`` 即红，
      打印实测 / 基线比值；基线键集与注册表不一致同样红（防静默漏测）；
    - ``--update``：按本轮测量重建基线（人工评审后提交）；
    - ``--self-check``：只校验注册表与基线键集合双向一致，不跑计时
      （供门禁链快速校验基线完整性）。

基线文件 ``scripts/.bench_baseline.json`` 记录每用例的 median / p95 上限
微秒与 ``max_ratio`` 环境容忍倍率。容忍倍率存在的原因：同一份源码在
共享 CI、容器 CPU 限流、笔记本省电模式下的绝对耗时可以相差数倍，硬
阈值会让基线变成噪声源；倍数内只报告漂移，倍数外才判定回归。基线随
硬件漂移，跨机比较没有意义——以同机复跑为准。

用法::

    cd backend && pixi run bench-core
    cd backend && pixi run bench-core --self-check
    cd backend && pixi run bench-core --gate
    cd backend && pixi run bench-core --update

Exit:
    0 = 报告 / 门禁通过 / 自检通过 / 基线更新成功；
    1 = 门禁超限、自检键集不一致，或被导入模块 / 构造用例模型时的
        ``ValidationError``——打印单行受控错误（含首个错误字段摘要），
        不展开 traceback；
    2 = 参数错误 / 基线缺失或损坏。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 仓库根（本脚本位于 <repo>/scripts/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 后端源码根（直接执行本脚本时补齐导入路径；pixi 任务已注入 PYTHONPATH）。
BACKEND_SRC = REPO_ROOT / "backend" / "src"

#: 基线文件（与其它棘轮基线同目录，随仓库提交）。
BASELINE_PATH = Path(__file__).resolve().parent / ".bench_baseline.json"

#: 基线 schema 版本（口径变化时递增）。
_BASELINE_VERSION = 1

#: 环境容忍倍率：实测中位数超过基线该倍数才判回归。
MAX_RATIO = 10.0

#: 预热批次数（首发调用含导入 / 缓存冷启动，不计入样本）。
_WARMUP_BATCHES = 3

#: 取样轮数（每轮内部循环到约 1 毫秒，降低单次计时噪声占比）。
_SAMPLES = 15

#: 单轮目标耗时纳秒（约 1 毫秒）。
_TARGET_BATCH_NS = 1_000_000

#: 单轮内部循环上限（防单次极快用例把循环数推到不合理量级）。
_MAX_LOOPS = 10_000


@dataclass(frozen=True)
class BenchCase:
    """单个基准用例登记。

    Attributes:
        name: 用例唯一标识（基线键 / 报告行标识）。
        description: 用例说明（产物 / 输入规模）。
        factory: 构造可重复执行的可调用对象（延迟导入被测模块）。
    """

    name: str
    description: str
    factory: Callable[[], Callable[[], None]]


@dataclass(frozen=True)
class CaseMeasurement:
    """单用例测量结果（报告与基线写入的中间形态）。

    Attributes:
        name: 用例标识。
        description: 用例说明。
        median_us: 中位耗时微秒。
        p95_us: 95 分位耗时微秒。
    """

    name: str
    description: str
    median_us: float
    p95_us: float


def _make_flow_gate_case() -> Callable[[], None]:
    """构造门控判定用例（合法配置 + 合成到达序列，命中正常回复分支）。

    Returns:
        执行一次 ``evaluate_flow_gate`` 的可调用对象。
    """
    from packages.harness.chat.flow_gate import evaluate_flow_gate
    from packages.harness.chat.flow_models import (
        FlowChatStateModel,
        FlowGateInputModel,
        FlowGateSettings,
        FlowRhythmStateModel,
    )

    settings = FlowGateSettings(
        _env_file=None,
        enabled=True,
        burst_window_ms=4000,
        burst_count=3,
        merge_quiet_ms=2500,
        merge_max_messages=5,
        min_reply_gap_ms=1200,
        backlog_defer_threshold=6,
        backlog_defer_step_ms=8000,
        backlog_defer_max_ms=90000,
        pending_grace_ms=60000,
    )
    now_ms = 1_700_000_000_000
    request = FlowGateInputModel(
        now_ms=now_ms,
        is_synthetic=False,
        rhythm=FlowRhythmStateModel(allowed=True, suggested_delay_ms=0),
        relationship=None,
        state=FlowChatStateModel(
            recent_arrival_ms=[now_ms - 3000, now_ms],
            backlog_count=0,
        ),
    )

    def run() -> None:
        evaluate_flow_gate(request, settings)

    return run


def _synth_relationship_facts(count: int) -> list[object]:
    """构造合成关系类事实（固定时刻阶梯，覆盖全部时间衰减档）。

    Args:
        count: 事实条数。

    Returns:
        ``FactRecordDTO`` 列表（按更新时刻倒序阶梯）。
    """
    from packages.harness.memory.models import MemoryProvenance
    from packages.harness.persona.facts_models import (
        FactRecordDTO,
        PersonaFactCategory,
        PersonaFactSource,
        PersonaFactStatus,
    )

    now_ms = 1_700_000_000_000
    day_ms = 86_400_000
    facts: list[object] = []
    for index in range(1, count + 1):
        updated_at = now_ms - (index % 400) * day_ms // 2
        content = f"我们一起经历过第 {index} 件小事"
        if index % 17 == 0:
            content = "说好不要熬夜，这是我们的边界"
        facts.append(
            FactRecordDTO(
                id=index,
                category=PersonaFactCategory.RELATIONSHIP,
                content=content,
                confidence=0.4 + (index % 6) * 0.1,
                status=PersonaFactStatus.ACTIVE,
                provenance=MemoryProvenance.OWNER,
                source=PersonaFactSource.MANUAL,
                evidence=[],
                superseded_by_id=None,
                version=1,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
    return facts


def _make_relationship_summary_case() -> Callable[[], None]:
    """构造关系摘要派生用例（200 条合成关系事实 + 固定派生时刻）。

    Returns:
        执行一次 ``build_relationship_summary`` 的可调用对象。
    """
    from packages.harness.persona.relationship import build_relationship_summary

    facts = _synth_relationship_facts(200)
    now_ms = 1_700_000_000_000

    def run() -> None:
        build_relationship_summary(facts, now_ms=now_ms)

    return run


def _make_relationship_summary_empty_case() -> Callable[[], None]:
    """构造关系摘要空事实用例（stranger 降级路径）。

    Returns:
        执行一次空事实 ``build_relationship_summary`` 的可调用对象。
    """
    from packages.harness.persona.relationship import build_relationship_summary

    now_ms = 1_700_000_000_000

    def run() -> None:
        build_relationship_summary((), now_ms=now_ms)

    return run


def _make_sentence_split_case() -> Callable[[], None]:
    """构造长文本句切用例（60 句中文长文本，长度上限 80 字符）。

    Returns:
        执行一次 ``split_assistant_sentences`` 的可调用对象。
    """
    from packages.harness.voice.realtime_sentence import split_assistant_sentences

    text = "".join(
        f"这是第 {index} 句用于验证长文本切分稳定性的中文句子，语气自然且足够长。"
        for index in range(1, 61)
    )

    def run() -> None:
        split_assistant_sentences(text, max_chars=80)

    return run


def _make_vad_feed_case() -> Callable[[], None]:
    """构造 VAD 逐帧喂入用例（合成 60 帧：起说 + 语音 + 静音切段）。

    配置逐字段显式传参：初始化参数优先级高于进程环境，缺任一个字段就等于
    让该字段从开发者环境解析——输入随机器漂移，或非法值让测量路径抛
    校验错误。因此这里把 ``RealtimeVoiceSettings`` 的全部字段按模型默认口径
    写死（含与 VAD 无关的下行字段），测量输入只由本函数决定。

    Returns:
        执行一次完整合成帧序列喂入的可调用对象。
    """
    import struct

    from packages.harness.voice.realtime_models import RealtimeVoiceSettings
    from packages.harness.voice.realtime_vad import EnergyVad, build_vad_config

    settings = RealtimeVoiceSettings(
        _env_file=None,
        enabled=False,
        sample_rate_hz=16000,
        channels=1,
        vad_speech_rms_threshold=0.06,
        vad_silence_rms_threshold=0.03,
        vad_start_consecutive_frames=3,
        vad_min_speech_ms=300,
        vad_silence_end_ms=700,
        vad_max_segment_ms=15000,
        partial_transcript_interval_ms=800,
        partial_min_speech_ms=600,
        sentence_max_chars=80,
        tts_chunk_bytes=16384,
        tts_queue_max=32,
        audio_queue_max_chunks=64,
        audio_batch_max_chunks=8,
        max_session_ms=600000,
        idle_timeout_ms=60000,
        heartbeat_interval_ms=15000,
        barge_in_enabled=True,
        max_sessions=2,
    )
    config = build_vad_config(settings)
    speech_frame = struct.pack("<h", 16_384) * 320
    silence_frame = b"\x00\x00" * 320
    frames = [speech_frame] * 20 + [silence_frame] * 40

    def run() -> None:
        vad = EnergyVad(config)
        now_ms = 1_700_000_000_000
        for offset, frame in enumerate(frames):
            vad.feed(frame, now_ms + offset * 20)

    return run


def _make_relationship_render_case() -> Callable[[], None]:
    """构造关系块渲染用例（合成摘要含最近事件与边界约定）。

    Returns:
        执行一次 ``render_relationship_block`` 的可调用对象。
    """
    from packages.harness.chat.prompts import render_relationship_block
    from packages.harness.persona.relationship_models import (
        RelationshipEventModel,
        RelationshipStage,
        RelationshipSummaryModel,
    )

    events = [
        RelationshipEventModel(
            fact_id=index,
            summary=f"最近一起做过的一件小事 {index}",
            importance=6.0,
            ts_ms=1_700_000_000_000 - index * 1000,
        )
        for index in range(1, 6)
    ]
    summary = RelationshipSummaryModel(
        stage=RelationshipStage.CLOSE,
        intimacy_level=2,
        score=12.5,
        event_count=200,
        recent_events=events,
        boundaries=["说好不要熬夜", "不许冷战过夜"],
        generated_at_ms=1_700_000_000_000,
    )

    def run() -> None:
        render_relationship_block(summary)

    return run


#: 用例注册表（name 即基线键；顺序即报告顺序）。
_CASES: tuple[BenchCase, ...] = (
    BenchCase(
        name="flow_gate.evaluate_flow_gate",
        description="门控判定（合法配置 + 两条合成到达记录）",
        factory=_make_flow_gate_case,
    ),
    BenchCase(
        name="persona.build_relationship_summary.200_facts",
        description="关系摘要派生（200 条合成关系事实 / 固定 now_ms）",
        factory=_make_relationship_summary_case,
    ),
    BenchCase(
        name="persona.build_relationship_summary.empty_facts",
        description="关系摘要空事实降级（stranger / 零事件）",
        factory=_make_relationship_summary_empty_case,
    ),
    BenchCase(
        name="voice.split_assistant_sentences.long_text",
        description="长文本句切（60 句中文 / 单句上限 80 字符）",
        factory=_make_sentence_split_case,
    ),
    BenchCase(
        name="voice.energy_vad.feed_60_frames",
        description="VAD 逐帧喂入（起说 + 语音 + 静音切段的 60 帧合成序列）",
        factory=_make_vad_feed_case,
    ),
    BenchCase(
        name="chat.render_relationship_block.synthetic",
        description="关系块渲染（合成摘要 / 最近事件 + 边界约定）",
        factory=_make_relationship_render_case,
    ),
)


def _case_names() -> list[str]:
    """返回注册表用例名（排序后——键集比较用）。

    Returns:
        用例名列表。
    """
    return sorted(case.name for case in _CASES)


def _batch_ns(run: Callable[[], None], loops: int) -> int:
    """执行一轮批量调用并返回总耗时纳秒。

    Args:
        run: 单次执行的可调用对象。
        loops: 本轮循环次数。

    Returns:
        本轮总耗时纳秒。
    """
    started = time.perf_counter_ns()
    for _ in range(loops):
        run()
    return time.perf_counter_ns() - started


def _percentile_us(samples_us: list[float], ratio: float) -> float:
    """按最近秩法计算分位耗时（样本已按微秒记录）。

    Args:
        samples_us: 单次耗时样本（微秒）。
        ratio: 分位比例（0-1）。

    Returns:
        对应分位耗时微秒。
    """
    ordered = sorted(samples_us)
    index = max(0, min(len(ordered) - 1, math.ceil(ratio * len(ordered)) - 1))
    return ordered[index]


def _measure_case(case: BenchCase) -> CaseMeasurement:
    """测量单个用例并返回 median / p95 微秒。

    Args:
        case: 用例登记。

    Returns:
        测量结果（保留三位小数微秒）。
    """
    run = case.factory()
    calibration_ns = max(_batch_ns(run, 1), 1)
    loops = max(1, min(_MAX_LOOPS, int(_TARGET_BATCH_NS // calibration_ns)))
    for _ in range(_WARMUP_BATCHES):
        _batch_ns(run, loops)
    samples_us = [
        _batch_ns(run, loops) / loops / 1000.0 for _ in range(_SAMPLES)
    ]
    return CaseMeasurement(
        name=case.name,
        description=case.description,
        median_us=round(statistics.median(samples_us), 3),
        p95_us=round(_percentile_us(samples_us, 0.95), 3),
    )


def _measure_all() -> list[CaseMeasurement]:
    """测量全部注册用例。

    Returns:
        按注册顺序排列的测量结果。
    """
    return [_measure_case(case) for case in _CASES]


def _load_baseline(*, required: bool) -> dict[str, dict[str, float]] | None:
    """读取基线文件并校验 schema。

    Args:
        required: True 时基线缺失 / 损坏直接退出 2；False 时返回 None。

    Returns:
        ``{用例名: {median_us, p95_us}}``；无需 / 无基线时返回 None。

    Raises:
        SystemExit: 基线缺失 / 损坏 / 版本不符且 ``required``（退出码 2）。
    """
    if not BASELINE_PATH.is_file():
        if required:
            print(
                f"[bench_core] 基线缺失: {BASELINE_PATH}（先运行 --update）",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return None
    try:
        payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[bench_core] 基线损坏: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if payload.get("version") != _BASELINE_VERSION:
        print(
            f"[bench_core] 基线版本不兼容（{payload.get('version')}）——重新 --update",
            file=sys.stderr,
        )
        raise SystemExit(2)
    cases: dict[str, dict[str, float]] = {}
    raw_cases = payload.get("cases", {})
    if not isinstance(raw_cases, dict):
        print("[bench_core] 基线 cases 字段不是对象", file=sys.stderr)
        raise SystemExit(2)
    for name, raw in raw_cases.items():
        if not isinstance(raw, dict):
            print(f"[bench_core] 基线用例 {name} 结构非法", file=sys.stderr)
            raise SystemExit(2)
        cases[str(name)] = {
            "median_us": float(raw.get("median_us", 0.0)),
            "p95_us": float(raw.get("p95_us", 0.0)),
        }
    return cases


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """原子写入 JSON 基线（同目录临时文件 + ``os.replace``）。

    先完整写入同目录 ``.tmp`` 再整体替换目标文件——写入中途失败时目标基线
    保持原内容，不会留下半截 JSON；临时文件在 ``finally`` 中清理。

    Args:
        path: 目标 JSON 文件路径。
        payload: 待序列化对象（UTF-8 / 两空格缩进 / 末尾换行）。
    """
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _write_baseline(measurements: list[CaseMeasurement]) -> None:
    """按本轮测量写入基线文件（人工评审后提交）。

    Args:
        measurements: 全部用例测量结果。
    """
    payload = {
        "version": _BASELINE_VERSION,
        "max_ratio": MAX_RATIO,
        "note": (
            "纯本地微基准基线——median/p95 上限微秒；门禁只按 median × max_ratio "
            "判定；再生成: cd backend && pixi run bench-core --update"
        ),
        "cases": {
            item.name: {"median_us": item.median_us, "p95_us": item.p95_us}
            for item in measurements
        },
    }
    _write_json_atomic(BASELINE_PATH, payload)
    print(
        f"[bench_core] 基线已写入 {BASELINE_PATH}（用例 {len(measurements)} 项，"
        f"max_ratio={MAX_RATIO:g}）"
    )


def _print_table(
    measurements: list[CaseMeasurement],
    baseline: dict[str, dict[str, float]] | None,
) -> None:
    """打印测量表（含基线与比值列）。

    Args:
        measurements: 测量结果。
        baseline: 基线（None = 无对照）。
    """
    header = f"{'用例':<52} {'median µs':>12} {'p95 µs':>12} {'基线 median µs':>16} {'比值':>8}"
    print("[bench_core] 本机实测（同机复跑为准，跨机绝对耗时不可比）")
    print(header)
    print("-" * len(header))
    for item in measurements:
        if baseline is None or item.name not in baseline:
            print(f"{item.name:<52} {item.median_us:>12.3f} {item.p95_us:>12.3f} {'—':>16} {'—':>8}")
            continue
        base_median = baseline[item.name]["median_us"]
        ratio = item.median_us / base_median if base_median > 0 else float("inf")
        print(
            f"{item.name:<52} {item.median_us:>12.3f} {item.p95_us:>12.3f} "
            f"{base_median:>16.3f} {ratio:>8.2f}"
        )


def _self_check() -> int:
    """校验注册表与基线键集合双向一致（不跑计时）。

    Returns:
        0 = 一致；1 = 键集不一致；基线问题由 :func:`_load_baseline` 退出 2。
    """
    baseline = _load_baseline(required=True)
    assert baseline is not None
    registry = set(_case_names())
    baseline_keys = set(baseline)
    missing = sorted(registry - baseline_keys)
    extra = sorted(baseline_keys - registry)
    if missing or extra:
        print("[bench_core] 基线键集与用例注册表不一致:", file=sys.stderr)
        for name in missing:
            print(f"  - 注册表有、基线缺: {name}", file=sys.stderr)
        for name in extra:
            print(f"  - 基线有、注册表缺: {name}", file=sys.stderr)
        return 1
    print(
        f"[bench_core] 自检通过：注册表 {len(registry)} 项与基线键集一致"
        f"（max_ratio={MAX_RATIO:g}）"
    )
    print(f"[bench_core] 完成模式=self-check 用例={len(registry)}")
    return 0


def _gate() -> int:
    """执行门禁测量并对照基线判定回归。

    Returns:
        0 = 全部在容忍倍率内；1 = 存在超限或键集不一致；基线问题退出 2。
    """
    baseline = _load_baseline(required=True)
    assert baseline is not None
    registry = set(_case_names())
    baseline_keys = set(baseline)
    if registry != baseline_keys:
        print("[bench_core] 基线键集与用例注册表不一致（门禁拒绝静默漏测）:", file=sys.stderr)
        for name in sorted(registry - baseline_keys):
            print(f"  - 注册表有、基线缺: {name}", file=sys.stderr)
        for name in sorted(baseline_keys - registry):
            print(f"  - 基线有、注册表缺: {name}", file=sys.stderr)
        return 1
    measurements = _measure_all()
    _print_table(measurements, baseline)
    regressions: list[str] = []
    for item in measurements:
        base_median = baseline[item.name]["median_us"]
        threshold = base_median * MAX_RATIO
        if item.median_us > threshold:
            regressions.append(
                f"{item.name}: median {item.median_us:.3f} µs > 基线 {base_median:.3f} µs × "
                f"{MAX_RATIO:g}（比值 {item.median_us / base_median:.2f}）"
            )
    if regressions:
        print(f"[bench_core] 性能回归（容忍倍率 {MAX_RATIO:g}）:", file=sys.stderr)
        for line in regressions:
            print(f"  - {line}", file=sys.stderr)
        print("[bench_core] 完成模式=gate 结果=FAIL", file=sys.stderr)
        return 1
    print(f"[bench_core] 门禁通过（{len(measurements)} 项均在 median × {MAX_RATIO:g} 内）")
    print(f"[bench_core] 完成模式=gate 用例={len(measurements)} 结果=PASS")
    return 0


def _report() -> int:
    """默认报告模式（恒 exit 0；基线存在时附比值）。

    Returns:
        0。
    """
    baseline = _load_baseline(required=False)
    measurements = _measure_all()
    _print_table(measurements, baseline)
    if baseline is None:
        print("[bench_core] 未找到基线（仅报告，不判定）——需要门禁时先 --update")
    print(f"[bench_core] 完成模式=report 用例={len(measurements)}")
    return 0


def _is_config_validation_error(exc: BaseException) -> bool:
    """判定异常是否为 pydantic 配置校验错误。

    延迟导入 pydantic，保持脚本模块导入期只依赖标准库。

    Args:
        exc: 捕获到的异常。

    Returns:
        True = 该异常是 ``pydantic.ValidationError``。
    """
    from pydantic import ValidationError

    return isinstance(exc, ValidationError)


def _format_config_validation_error(exc: BaseException) -> str:
    """把配置校验错误折叠为单行受控提示（不展开 traceback）。

    Args:
        exc: ``pydantic.ValidationError`` 实例。

    Returns:
        含首个错误字段摘要的单行错误文本（字段路径 + 校验消息 + 错误类型）。
    """
    collector = getattr(exc, "errors", None)
    raw_errors = collector() if callable(collector) else []
    if raw_errors:
        first = raw_errors[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "<root>"
        detail = (
            f"{location}: {first.get('msg', exc)}"
            f"（type={first.get('type', 'unknown')}）"
        )
    else:
        detail = str(exc)
    return (
        "[bench_core] 非本次基准相关环境变量取值非法"
        f"（导入期或构造用例模型校验失败）：{detail}"
        "——请清理 / 修正该环境变量后重跑（与真实 app 启动失败同源）"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口（模式互斥）。

    Args:
        argv: 命令行参数（None 取 ``sys.argv[1:]``）。

    Returns:
        进程退出码（见模块文档）。

    Raises:
        SystemExit: argparse 参数错误或基线不可用。
    """
    if str(BACKEND_SRC) not in sys.path:
        sys.path.insert(0, str(BACKEND_SRC))
    parser = argparse.ArgumentParser(description="纯本地微基准（核心纯函数耗时基线）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--gate", action="store_true", help="门禁：median 超过基线 × max_ratio 即红")
    group.add_argument("--update", action="store_true", help="按本轮测量重建基线")
    group.add_argument("--self-check", action="store_true", help="只校验注册表与基线键集一致（不跑计时）")
    args = parser.parse_args(argv)

    try:
        if args.self_check:
            return _self_check()
        if args.update:
            measurements = _measure_all()
            _print_table(measurements, None)
            _write_baseline(measurements)
            print(f"[bench_core] 完成模式=update 用例={len(measurements)}")
            return 0
        if args.gate:
            return _gate()
        return _report()
    except Exception as exc:
        if not _is_config_validation_error(exc):
            raise
        print(_format_config_validation_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
