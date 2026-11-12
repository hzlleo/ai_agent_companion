"""usage 域值域 Model Classes——调用用途 / 结果状态 / 聚合维度枚举。

值域（本工程配置契约）：

    - :class:`UsagePurpose` —— 调用用途：五大模型角色（chat / duty /
      dream / look_at / embed，与 ``config.ModelRole`` 一一对应）加上
      辅助能力用途（tts / asr / tool——后续 chunk 落地的语音与工具面）；
    - :class:`UsageStatus`   —— 调用结果（success / error）；
    - :class:`UsageGroupBy`  —— 聚合维度（model_entry / purpose /
      time_bucket）；
    - :class:`UsageTimeBucket` —— 时间桶宽（minute / hour / day）。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "UsagePurpose",
    "UsageStatus",
    "UsageGroupBy",
    "UsageTimeBucket",
    "TIME_BUCKET_MS",
]


class UsagePurpose(StrEnum):
    """调用用途值域——用量记账的 purpose 轴。

    - ``chat``：主对话（日常陪聊、人格化回复）；
    - ``duty``：值班任务（日程提醒、例行巡检等结构化任务）；
    - ``dream``：梦境生成（记忆整理 / 梦境叙事写作）；
    - ``look_at``：多模态查看（图像理解）；
    - ``embed``：向量化（记忆检索 / 语义嵌入）；
    - ``tts``：语音合成（后续 chunk 落地）；
    - ``asr``：语音识别（后续 chunk 落地）；
    - ``tool``：工具调用编排（后续 chunk 落地）。
    """

    CHAT = "chat"
    DUTY = "duty"
    DREAM = "dream"
    LOOK_AT = "look_at"
    EMBED = "embed"
    TTS = "tts"
    ASR = "asr"
    TOOL = "tool"


class UsageStatus(StrEnum):
    """调用结果值域。

    - ``success``：调用成功（token 数可能为零——端点未回传用量）；
    - ``error``：调用失败（错误码见 error_code 列）。
    """

    SUCCESS = "success"
    ERROR = "error"


class UsageGroupBy(StrEnum):
    """聚合维度值域（管理面总览）。

    - ``model_entry``：按模型条目聚合（端点成本分布）；
    - ``purpose``：按调用用途聚合（业务面消耗分布）；
    - ``time_bucket``：按时间桶聚合（minute / hour / day 三档桶宽）。
    """

    MODEL_ENTRY = "model_entry"
    PURPOSE = "purpose"
    TIME_BUCKET = "time_bucket"


class UsageTimeBucket(StrEnum):
    """时间桶宽值域（epoch 毫秒对齐取整）。"""

    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"


#: 时间桶宽毫秒映射（桶起点 = ``floor(ts_ms / 桶宽) * 桶宽``）。
TIME_BUCKET_MS: dict[UsageTimeBucket, int] = {
    UsageTimeBucket.MINUTE: 60_000,
    UsageTimeBucket.HOUR: 3_600_000,
    UsageTimeBucket.DAY: 86_400_000,
}
