"""usage 域 DTO——创建 / 记录 / 查询 / 聚合的传输契约（Unit 文件）。

契约分层：

    - :class:`UsageCreateDTO`        —— 写入面（llm 网关事件映射 /
      批量回填）；
    - :class:`UsageRecordDTO`        —— 读取面（ORM 行 → DTO）；
    - :class:`UsageQueryDTO`         —— 记录查询（时间范围 + 多轴过滤 +
      游标分页）；
    - :class:`UsageAggregateQueryDTO`—— 聚合查询（过滤面 + 分组维度）；
    - :class:`UsageAggregateDTO`     —— 聚合结果（分组键 + 求和 / 计数 /
      均时延）。

时间语义：全部 epoch 毫秒；范围查询为**左闭右开**区间
（``start_ms <= ts_ms < end_ms``——时间桶对齐无重叠）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.harness.usage.models import (
    UsageGroupBy,
    UsagePurpose,
    UsageStatus,
    UsageTimeBucket,
)

__all__ = [
    "UsageFilterFields",
    "UsageCreateDTO",
    "UsageRecordDTO",
    "UsageQueryDTO",
    "UsageAggregateQueryDTO",
    "UsageAggregateDTO",
]


class UsageFilterFields(BaseModel):
    """用量查询公共过滤面（记录查询与聚合查询共用的过滤字段）。

    Attributes:
        start_ms: 范围起点（epoch 毫秒，含；None = 不限）。
        end_ms: 范围终点（epoch 毫秒，不含；None = 不限）。
        persona_id: 人格 ID 过滤（None = 全人格——管理面口径）。
        model_entry: 模型条目名过滤（None = 全条目）。
        purpose: 调用用途过滤（None = 全用途）。
        status: 调用结果过滤（None = 全状态）。
    """

    start_ms: int | None = Field(default=None, description="范围起点（epoch 毫秒，含）")
    end_ms: int | None = Field(default=None, description="范围终点（epoch 毫秒，不含）")
    persona_id: str | None = Field(default=None, description="人格 ID 过滤")
    model_entry: str | None = Field(default=None, description="模型条目名过滤")
    purpose: UsagePurpose | None = Field(default=None, description="调用用途过滤")
    status: UsageStatus | None = Field(default=None, description="调用结果过滤")


class UsageCreateDTO(BaseModel):
    """用量记录创建 DTO（写入面）。

    Attributes:
        persona_id: 人格 ID（None = 系统调用）。
        ts_ms: 调用发起时间（epoch 毫秒）。
        model_entry: 模型条目名（非空）。
        provider: provider 类型。
        purpose: 调用用途。
        input_tokens: 输入 token 数。
        output_tokens: 输出 token 数。
        total_tokens: 总 token 数（未提供时按 input + output 归一）。
        latency_ms: 端到端时延毫秒。
        status: 调用结果。
        request_id: 请求追踪 ID（可空）。
        error_code: 失败错误码（成功为 None）。
    """

    model_config = ConfigDict(protected_namespaces=())

    persona_id: str | None = Field(default=None, description="人格 ID（系统调用为 None）")
    ts_ms: int = Field(description="调用发起时间（epoch 毫秒）")
    model_entry: str = Field(min_length=1, description="模型条目名")
    provider: str = Field(default="openai_compatible", description="provider 类型")
    purpose: UsagePurpose = Field(description="调用用途")
    input_tokens: int = Field(default=0, ge=0, description="输入 token 数")
    output_tokens: int = Field(default=0, ge=0, description="输出 token 数")
    total_tokens: int = Field(default=0, ge=0, description="总 token 数")
    latency_ms: int = Field(default=0, ge=0, description="端到端时延（毫秒）")
    status: UsageStatus = Field(description="调用结果")
    request_id: str | None = Field(default=None, description="请求追踪 ID")
    error_code: str | None = Field(default=None, description="失败错误码")

    @model_validator(mode="after")
    def _fill_total(self) -> UsageCreateDTO:
        """total_tokens 归一：未提供（0）且分项非零时按 input + output 补齐。

        Returns:
            归一后的实例。
        """
        if self.total_tokens == 0:
            self.total_tokens = self.input_tokens + self.output_tokens
        return self


class UsageRecordDTO(BaseModel):
    """用量记录读取 DTO（ORM 行归一）。

    Attributes:
        id: 记录主键。
        persona_id: 人格 ID（None = 系统调用）。
        ts_ms: 调用发起时间（epoch 毫秒）。
        model_entry: 模型条目名。
        provider: provider 类型。
        purpose: 调用用途。
        input_tokens: 输入 token 数。
        output_tokens: 输出 token 数。
        total_tokens: 总 token 数。
        latency_ms: 端到端时延毫秒。
        status: 调用结果。
        request_id: 请求追踪 ID。
        error_code: 失败错误码。
        created_at: 记录落库时间（epoch 毫秒）。
    """

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int = Field(description="记录主键")
    persona_id: str | None = Field(default=None, description="人格 ID")
    ts_ms: int = Field(description="调用发起时间（epoch 毫秒）")
    model_entry: str = Field(description="模型条目名")
    provider: str = Field(description="provider 类型")
    purpose: UsagePurpose = Field(description="调用用途")
    input_tokens: int = Field(description="输入 token 数")
    output_tokens: int = Field(description="输出 token 数")
    total_tokens: int = Field(description="总 token 数")
    latency_ms: int = Field(description="端到端时延（毫秒）")
    status: UsageStatus = Field(description="调用结果")
    request_id: str | None = Field(default=None, description="请求追踪 ID")
    error_code: str | None = Field(default=None, description="失败错误码")
    created_at: int = Field(description="记录落库时间（epoch 毫秒）")


class UsageQueryDTO(UsageFilterFields):
    """用量记录查询 DTO（游标分页——id 降序，新记录在前）。

    Attributes:
        cursor: 上一页返回的不透明游标（None = 首页）。
        limit: 页大小（1 - 200）。
    """

    cursor: str | None = Field(default=None, description="分页游标（首页为 None）")
    limit: int = Field(default=20, ge=1, le=200, description="页大小")


class UsageAggregateQueryDTO(UsageFilterFields):
    """用量聚合查询 DTO（分组维度 + 过滤面）。

    Attributes:
        group_by: 分组维度（model_entry / purpose / time_bucket）。
        time_bucket: 时间桶宽（``group_by=time_bucket`` 时必填；其余
            维度忽略）。
    """

    group_by: UsageGroupBy = Field(description="分组维度")
    time_bucket: UsageTimeBucket | None = Field(
        default=None, description="时间桶宽（time_bucket 维度必填）"
    )


class UsageAggregateDTO(BaseModel):
    """用量聚合结果 DTO（单分组行）。

    Attributes:
        group: 分组键——条目名 / 用途值 / 时间桶起点 epoch 毫秒的字符串
            形态（JSON 键统一字符串化）。
        bucket_ms: 时间桶起点（epoch 毫秒；仅 ``time_bucket`` 维度非
            None——数值面供前端排序 / 定位）。
        input_tokens: 输入 token 求和。
        output_tokens: 输出 token 求和。
        total_tokens: 总 token 求和。
        call_count: 调用次数。
        avg_latency_ms: 平均时延毫秒（四舍五入取整）。
        error_count: 失败次数（status=error 计数）。
    """

    group: str = Field(description="分组键（条目名 / 用途值 / 桶起点毫秒字符串）")
    bucket_ms: int | None = Field(
        default=None, description="时间桶起点（epoch 毫秒；非时间维度为 None）"
    )
    input_tokens: int = Field(default=0, ge=0, description="输入 token 求和")
    output_tokens: int = Field(default=0, ge=0, description="输出 token 求和")
    total_tokens: int = Field(default=0, ge=0, description="总 token 求和")
    call_count: int = Field(default=0, ge=0, description="调用次数")
    avg_latency_ms: int = Field(default=0, ge=0, description="平均时延（毫秒）")
    error_count: int = Field(default=0, ge=0, description="失败次数")

    @model_validator(mode="after")
    def _validate_bucket_key(self) -> UsageAggregateDTO:
        """时间维度一致性：``bucket_ms`` 非空时 ``group`` 必须与其一致。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: 时间维度行缺 ``bucket_ms`` 或 ``group`` 与之不一致。
        """
        if self.bucket_ms is not None and self.group != str(self.bucket_ms):
            raise ValueError("时间桶分组的 group 必须为 bucket_ms 的字符串形态")
        return self
