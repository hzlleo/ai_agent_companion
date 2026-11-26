"""日预算计数上限——游标分页参数与配置硬上限的唯一真源。

预算计数按 usage 行游标分页累加（LLM / 语音分面共用同一分页口径）：
每页取 ``BUDGET_COUNT_PAGE_LIMIT`` 行、最多翻 ``BUDGET_COUNT_PAGE_CAP``
页，因此单日可计数的有效行数上限是两者乘积 ``DAILY_BUDGET_MAX``。配置层
（LLM / TTS / ASR / 声纹的 ``daily_budget_*``）的 ``le`` 校验与两处预算
计数器的页参数都引用本模块，避免「配置允许的上限高于计数器实际能数到
的行数」这类静默截断。

依赖方向：本模块只依赖标准库；usage 域、config 域与 voice 域均可安全
引用，不引入循环导入。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "BUDGET_COUNT_PAGE_CAP",
    "BUDGET_COUNT_PAGE_LIMIT",
    "DAILY_BUDGET_MAX",
    "budget_count_capacity",
]

#: 预算计数单页行数（usage 查询分页上限）。
BUDGET_COUNT_PAGE_LIMIT: Final[int] = 200
#: 预算计数最大翻页数（防止异常记录集拖死调用路径）。
BUDGET_COUNT_PAGE_CAP: Final[int] = 50
#: 单日预算可计数的行数上限（配置 ``le`` 与计数器容量同源）。
DAILY_BUDGET_MAX: Final[int] = BUDGET_COUNT_PAGE_LIMIT * BUDGET_COUNT_PAGE_CAP


def budget_count_capacity() -> int:
    """返回预算计数器单日可计数的行数容量。

    容量与配置硬上限同源（``DAILY_BUDGET_MAX``）；调用方需要运行时读取该
    数值（诊断 / 容量说明 / 结构测试）时使用本函数，避免重新拼页参数。

    Returns:
        单日可计数的行数上限（页数上限 × 单页行数）。
    """
    return BUDGET_COUNT_PAGE_CAP * BUDGET_COUNT_PAGE_LIMIT
