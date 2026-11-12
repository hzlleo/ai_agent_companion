"""chat 域 Model Classes——会话回合 / 会话状态 / 消息组装结果（Unit 文件）。

会话状态约定（P1 记忆层落地后）：

    - **历史内存态 + 可选持久化**：会话回合列表存活于
      :class:`ChatSessionStateModel` 对象内；``ChatService`` 绑定持久化
      服务（``session_id`` 非空）时，reply / stream_reply 的用户与助手
      回合同步落 ``ff_chat_messages``（消息行经 memory 域
      ``ChatSessionService`` 追加）；未绑定时行为与纯内存态一致
      （P0 兼容——既有测试面零变更）；
    - **卡片快照**：会话创建时从人格注册表快照 chara card（运行期管理面
      编辑不热更到在途会话——会话以创建时刻的正典为准，文档化决策；
      resume 恢复时按会话归属人格重取**当前**卡片）；
    - **token 预算近似上限**：无 tokenizer，以「回合数滑动窗口 +
      单回合字符上限」两个结构性上限近似 token 预算（防无限膨胀的
      in-memory 历史拖爆上下文窗口）；持久化消息不受滑动窗口约束
      （全量记录——内存窗口只是上下文组装面）；世界书条目预算的
      token 估算见 service 层 ``estimate_text_tokens``。

时间字段全部 epoch 毫秒（``core.time_utils.now_ms``）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from packages.harness.core.time_utils import now_ms
from packages.harness.llm.llm_models import ChatMessageModel, ChatMessageRole
from packages.harness.persona.chara_card import CharaCardModel

__all__ = [
    "ChatTurnModel",
    "ChatSessionStateModel",
    "MessageCompositionResult",
]


class ChatTurnModel(BaseModel):
    """会话回合（单条历史消息的会话内存态记录）。

    与 llm 域 :class:`~packages.harness.llm.llm_models.ChatMessageModel` 的
    分工：回合模型是**会话历史的持久语义面**（携带时间戳，P1 落库时即
    此形态）；组装请求时由 service 层映射为传输面消息模型（不含时间戳）。

    Attributes:
        role: 回合角色（user=用户输入；assistant=人格回复；system=
            显式系统注入——P0 组装链不落 system 回合，保留值域完备性）。
        content: 回合文本内容（非空）。
        ts_ms: 回合产生时间（epoch 毫秒）。
    """

    role: ChatMessageRole = Field(description="回合角色")
    content: str = Field(min_length=1, description="回合文本内容（非空）")
    ts_ms: int = Field(default_factory=now_ms, description="回合产生时间（epoch 毫秒）")


class ChatSessionStateModel(BaseModel):
    """人格感知聊天会话状态（内存态——每会话对象一份；P1 可选持久化）。

    Attributes:
        persona_id: 绑定人格主键（人格注册表 ``ff_personas.id``）。
        persona_name: 绑定人格注册名（展示 / 日志面）。
        display_name: 绑定人格展示名。
        card: 人格卡片快照（会话创建时刻的 chara card v2 正典）。
        session_id: 持久化会话主键（``ff_chat_sessions.id``；None = 纯
            内存态——未绑定持久化服务或创建时未提供数据库会话）。
        turns: 会话回合列表（时间序——追加式）。
        max_history_turns: 历史回合数上限（滑动窗口：超出丢最老回合）。
        max_turn_chars: 单回合字符上限（超长输入截断保护）。
    """

    persona_id: int = Field(gt=0, description="绑定人格主键")
    persona_name: str = Field(min_length=1, description="绑定人格注册名")
    display_name: str = Field(default="", description="绑定人格展示名")
    card: CharaCardModel = Field(description="人格卡片快照（chara card v2）")
    session_id: int | None = Field(
        default=None, description="持久化会话主键（None=纯内存态）"
    )
    turns: list[ChatTurnModel] = Field(default_factory=list, description="会话回合列表")
    max_history_turns: int = Field(default=64, ge=2, description="历史回合数上限（滑动窗口）")
    max_turn_chars: int = Field(default=2000, ge=1, description="单回合字符上限（截断保护）")

    def append_turn(self, role: ChatMessageRole, content: str) -> ChatTurnModel:
        """追加回合（空内容拒绝 + 超长截断 + 滑动窗口裁剪）。

        Args:
            role: 回合角色。
            content: 回合文本（空白内容抛业务异常；超 ``max_turn_chars``
                截断到上限——防单条巨幅输入拖爆上下文）。

        Returns:
            已追加的 :class:`ChatTurnModel`。

        Raises:
            ValueError: 内容为空 / 纯空白。
        """
        stripped = content.strip()
        if not stripped:
            raise ValueError("回合内容不能为空")
        capped = stripped[: self.max_turn_chars]
        turn = ChatTurnModel(role=role, content=capped)
        self.turns.append(turn)
        # 滑动窗口：超出上限时丢最老回合（保持最近 max_history_turns 条）
        overflow = len(self.turns) - self.max_history_turns
        if overflow > 0:
            del self.turns[:overflow]
        return turn

    def clear_turns(self) -> None:
        """清空会话回合（/clear 命令面——保留人格绑定与卡片快照）。"""
        self.turns.clear()


class MessageCompositionResult(BaseModel):
    """消息组装结果（compose_messages 的输出契约）。

    Attributes:
        messages: 完整请求消息序列（system 首块 + 全部历史回合 + 历史后
            再注入末块——可直接交给 llm 网关）。
        system_prompt: 组装后的完整 system 提示文本（章程 + 世界书
            before 段 + 人格正典 + 世界书 after 段）。
        post_history_message: 历史后再注入文本（卡片未声明时为 None）。
        included_book_entries: 实际注入的世界书条目数（constant + 触发）。
        estimated_system_tokens: system 提示的近似 token 估算值。
    """

    messages: list[ChatMessageModel] = Field(description="完整请求消息序列")
    system_prompt: str = Field(description="组装后的完整 system 提示文本")
    post_history_message: str | None = Field(
        default=None, description="历史后再注入文本（未声明为 None）"
    )
    included_book_entries: int = Field(default=0, ge=0, description="实际注入的世界书条目数")
    estimated_system_tokens: int = Field(default=0, ge=0, description="system 提示近似 token 估算")
