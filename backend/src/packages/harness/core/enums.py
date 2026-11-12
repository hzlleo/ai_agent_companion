"""业务枚举常量。

使用 Python 3.12 ``StrEnum``，所有枚举值可直接作为字符串使用
（如存入数据库、序列化为 JSON、用于 API 响应）。

本模块只承载**跨域通用**枚举（P0-2 交付面）：

- ``Locale`` —— i18n locale 值域（context / i18n / logging 共用）；
- ``TaskStatus`` —— 后台任务状态值域（background_tasks / 后续 duty 调度共用）。

各业务域专属枚举（persona 状态机 / chat 会话状态 / usage 维度等）由
各自域包定义（后续 chunk 交付），core 不前置实现。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Locale", "SUPPORTED_LOCALES", "TaskStatus", "ACTIVE_TASK_STATUSES"]


class Locale(StrEnum):
    """支持的 locale 值域。

    - ``zh-CN``：默认 locale（消息表回退目标）；
    - ``en``：英文。

    与 ``core.i18n`` 的 ``_DEFAULT_LOCALE`` / ``core.context`` 的
    ``locale_var`` 默认值保持一致。
    """

    ZH_CN = "zh-CN"
    EN = "en"


#: 支持的 locale 集合（i18n 查找入口据此判定回退）。
#: 注：不能定义在 Enum 类体内（类体执行期成员尚未成形）。
SUPPORTED_LOCALES: frozenset[str] = frozenset({Locale.ZH_CN, Locale.EN})


class TaskStatus(StrEnum):
    """后台任务状态值域（``background_tasks`` 的 ``TaskRecord.status``）。

    - PENDING：已入队，等待 worker 消费；
    - RUNNING：worker 已拾取，执行中；
    - COMPLETED：执行成功（``result`` 有值）；
    - FAILED：执行抛异常（``error`` 携带原因）；
    - CANCELLED：执行中被取消。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 活跃任务状态集合——处于此集合时任务视为「在途」（停机 drain /
#: 周期清扫的打捞面共用此真源，禁止再复制字面量集合）。
ACTIVE_TASK_STATUSES: frozenset[str] = frozenset({TaskStatus.PENDING, TaskStatus.RUNNING})
