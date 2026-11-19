"""chat 域服务——人格感知消息组装 + 会话循环（compose → gateway → 回合）。

组装链（单一真相，文档化于 docs/backend/chat.md）::

    compose_messages(state)
        ↓ ① prompts.render_charter()                      会话章程帧
        ↓ ② 世界书 before_char 条目块                      检索增强（前段）
        ↓ ③ prompts.render_persona_system_prompt(card)     人格正典（覆写感知）
        ↓ ④ prompts.render_constitution()                 宪章（注入面防护五条
              ——身份 / 规则 / 引用 / 标签 / 挑衅应对）
        ↓ ⑤ prompts.render_persona_contract(card.name)     角色契约（人格输出
              契约五条——不自我声明 AI / 不道歉式开场 / 不总结腔软化 /
              亲密场景不升维 / 被纠正不辩论）
        ↓ ⑥ 非中性情绪摘要块（render_emotion_state_block——R2b 轻情绪引擎；
              无状态 / 全中性跳过）
        ↓ ⑦ 长期指令块（render_directives_block——R2b D7-14；现行指令
              注入，20 条 / 2000 字符帽截断；无指令跳过）
        ↓ ⑧ 人物事实块（render_facts_block——R2c；【关于对方】现行高置信
              事实注入，12 条 / 1500 字符帽由读取面裁剪；无事实跳过）
        ↓ ⑨ 核心记忆块（render_core_memory_block——R2c；【自我认知】四块
              自编辑自我模型的非空块注入，锁定块带锁定标识，总字符 2400
              帽由渲染面截断；全空跳过）
        ↓ ⑩ 关系进展块（render_relationship_block——R2b+；【关系进展】由
              关系类事实派生的阶段 / 最近变化 / 边界约定摘要，恒注入——
              空事实渲染「初识」，总字符 1200 帽）
        ↓ ⑪ 相关技能块（render_skills_block——R2c；【相关技能】仅当命中
              本轮用户文本时注入，2 条 / 2000 字符帽；无命中跳过）
        ↓ ⑫ prompts.render_output_protocol()               输出格式协议（心声 /
              情绪标签指令——R2b）
        ↓ ⑬ 世界书 after_char 条目块                       检索增强（后段）
        ↓ ⑭ 历史回合 → ChatMessageModel 序列                （含刚追加的用户回合；
              当轮入站回合经 nonce 定界包裹——历史 user 回合最近 10 条同样
              定界包裹（标签「历史消息」），更早与 assistant 回合保持原文）
        ↓ ⑮ 注入提示块（检测命中时）                        紧接当轮包裹消息之后
        ↓ ⑯ prompts.render_post_history_message(card)      历史后再注入（末块）

注入面防护（结构隔离 + 检测提示——本域新增防线，攻击面矩阵见
docs/backend/defense.md）：

    - **当轮入站消息定界包裹**：``compose_messages(..., wrap_current_message=True,
      current_message_label=...)`` 时，**最新一条 user 回合**被
      ``wrap_untrusted_content`` 包进 nonce 定界的「内容开始 / 内容结束」
      帧（标签区分主人消息 / 群友消息）；历史中的 **user 角色**回合同样
      定界包裹（统一标签「历史消息」），上限
      :data:`HISTORY_WRAP_MAX_TURNS`（最近 10 条）——更早回合与 assistant
      回合保持原文：长会话逐条包裹会让上下文线性膨胀，且边界信号边际
      收益递减；包裹只发生在请求消息面，不改变 ``state.turns`` 原文与
      落库内容；
    - **工具结果定界包裹**：``_render_tool_result_content`` 对执行器回灌的
      tool 轮正文统一包裹（标签「工具结果」）——搜索摘要 / 浏览器
      ``read_text`` / 任意执行器结果里的「指令」都不再裸奔；duty 域
      ``render_tool_result`` 同语义（见 duty 域文档）；
    - **检测提示**：``reply`` / ``stream_reply`` 在组装前对当轮用户文本跑
      ``detect_injection``；越过阈值时记 debug 日志（命中种类列表），并在
      包裹消息之后追加 ``render_injection_hint`` 块（宪章复述 + 一句按
      ``INJECTION_COUNTER_STYLE`` 确定性选出的参考回怼）——模型可改写或
      无视提示，护栏是宪章与结构边界本身；
    - **配置**：``INJECTION_`` env 面（``InjectionGuardSettings``：启用 /
      风格 / 阈值 / 提示开关）；``enabled=False`` 时包裹与检测全关，回复
      链路回到原始形态。世界书 / 长期指令 / 人物事实 / 核心记忆 / 技能
      属**受信层**（主人或人格自身写入），不做包裹。

人格输出合规守卫（助手腔检测 + 单次强化重试——``COMPLIANCE_`` env 面
``ComplianceGuardSettings``）：

    - **输出契约**：compose 链在宪章之后恒注入【角色契约】块
      （``render_persona_contract``——不自我声明 AI / 不道歉式开场 /
      不总结腔软化 / 亲密场景不升维 / 被纠正不辩论），属 system 提示的
      常驻约束，不随开关变化；
    - **检测面**：``reply`` 在网关响应之后、输出协议解析之前对原始回复跑
      ``detect_assistant_violation``（助手腔种类：AI 自我声明 / 道歉开场 /
      短回复里的总结腔 / 客服话术），越过阈值时记一条 warning（人格 +
      命中种类，不含回复正文）；
    - **重试语义（锁定）**：命中的非流式回复**只重试一次**——同一段对话
      末尾追加角色提醒块（``render_compliance_nudge``）并把温度提升
      ``temperature_bump``（请求未携带采样参数时以
      ``COMPLIANCE_RETRY_BASELINE_TEMPERATURE`` 为基线，叠加结果夹取
      2.0 上限；请求自带温度时以其为基线）。重试结果是**无条件接受**的：
      不再检测、不循环，重试后仍失守也照常解析落回合；``max_retries=0``
      等价于只记日志不重试；
    - **工具循环分支**：重试请求只带组装消息 + 提醒块、不携带工具面——
      工具步是循环内部状态，重写的是最终可见文本，不需要重新跑工具；
    - **流式边界**：``stream_reply`` 恒不重试——逐块产出时已经交给了
      消费方，无法回头重说一遍；检测面因此只覆盖非流式回复。

世界书筛选语义（chara card v2 规格 + R2b 高级触发——算法链全文见
``world_book`` 模块文档，此处为消费面摘要）：

    - ``constant=True`` 条目恒注入（世界观保底——**不占 token 预算**、
      无视分组互斥 / 概率门 / 冷却门）；
    - 关键词条目对「最近 ``scan_depth`` 条回合」做关键词匹配
      （``keys`` 任一命中；``selective`` 再要求 ``secondary_keys`` 任一
      命中；大小写按条目 ``case_sensitive``）；
    - 递归扩展（书级 ``recursive_scanning`` 总开关 × 条目级
      ``recursive`` 收窄，2 跳上限 + visited 集合环安全）；
    - 互斥分组（``include_group`` 同组非 constant 命中条目只注入
      优先级最高者）→ 概率门（``trigger_probability``，rng 可注入）→
      冷却门（``cooldown_turns``，会话作用域回合状态——功能式传递，
      组装结果携带推进态由本服务回存）→ token 预算装箱（既有截止
      语义）；
    - 渲染序：``position`` 决定段位（既有）；段内 ``(depth 降序,
      insertion_order 升序, 索引升序)``——缺省 depth=4 同槽退化为既有
      纯 insertion_order 序（兼容证明：既有测试零变更）。

调用链（网关面）：``chat_by_role / stream_by_role``，role=chat、
purpose="chat"、persona_id 显式参数直传（网关用量面直接携带；显式参数
优先于上下文变量——与 ``bind_persona_context`` 语义等价，选直传因其
可测性与可读性）。

可选工具循环（R2a-4——构造注入 ``tools`` + ``tool_executor`` 后，
``reply`` 走有界工具循环；缺省 ``tools=None`` 保持既有零工具形态）：

    - 循环机械与 duty 域同构（``duty_service._tool_loop`` 镜像）：
      gateway ``chat_by_role(chat, messages, tools=specs)`` →
      ``tool_calls`` 非空则 assistant 轮回放 + 逐调用经 ``tool_executor``
      （:class:`ToolExecutorFace`——``ToolRegistry.execute_call`` 结构
      满足）执行 + tool 轮反馈（``tool_call_id`` 配对）；``tool_calls``
      空则 content 即最终回复；
    - 步数上限 :data:`CHAT_TOOL_STEP_CAP`（锁定 5——chat 单轮收敛快于
      duty 巡检）；超限追加收尾指令（system）+ **无工具面**最终调用
      （final-report forcing）；
    - **持久化不变**：会话回合只落用户回合 + 最终 assistant 文本
      （工具步是循环内部消息——不进入 ``state.turns`` / 不落
      ``ff_chat_messages``；resume / 后续组装不可见工具中间态，
      文档化决策）；
    - 流式 ``stream_reply`` 恒 content-only（网关面 tools+stream 已
      拒绝——本域不组合二者）。

输出协议 + 轻情绪引擎（R2b D7-2 / D7-7——模块文档锁定边界）：

    - ``reply`` 的模型原始输出经 ``output_protocol.parse_assistant_output``
      解析：``<心声>`` / ``<情绪>`` 前导标签被剥离，**返回 / 落回合 /
      进历史 / 发通道的只有可见正文**；心声与情绪增量只进回合 meta
      （``ff_chat_messages.meta``）与会话详情 API；
    - 情绪引擎仅在「持久化会话绑定且已落库」时激活（M 态存储面依赖
      数据库——纯内存形态零附加 I/O）；激活时 reply 在组装前读 M 态
      产出摘要行注入心情块，回复后按 ``<情绪>`` 增量推进并持久化 M 态，
      同时把推进后快照写入回合 meta；
    - ``stream_reply`` **不跑完整协议**（流式通道是操作员可见面——
      既有 content-only 契约不变）：仅经 ``StreamTagStripper`` 对流的
      起始前缀做 best-effort 标签剥离（未闭合标签原样放行），
      心声 / 情绪**不落 meta、不推进 M 态**；
    - 不合规模型（无标签 / 标签残缺）优雅降级：原文可见、零状态变化
      ——协议面永不因模型输出抛异常。

长期指令注入（R2b D7-14——构造注入 ``directive_service`` 后）：

    - compose 链在心情块之后、输出格式协议之前注入【长期指令】块
      （``render_directives_block``——现行指令按写入倒序，20 条 / 2000
      字符双帽截断 + 截断说明行）；
    - 注入面与情绪引擎同轴：仅在持久化会话绑定且已落库时读取（
      ``session_scope`` 独立短事务 + 人格上下文绑定）——纯内存形态零
      附加 I/O，既有测试面零变更；读取失败不阻断回复（日志 + 空块，
      文档化：常备指令是增强面而非回复前提）。

人物事实注入（R2c——构造注入 ``facts_reader`` 后）：

    - compose 链在长期指令块之后、输出格式协议之前注入【关于对方】块
      （``render_facts_block``——现行高置信事实按类别标签逐行列出，
      12 条 / 1500 字符帽由 ``FactsService.top_facts_for_compose`` 裁剪）；
    - 注入面与长期指令同轴：仅在持久化会话绑定且已落库时读取（
      ``session_scope`` 独立短事务 + 人格上下文绑定）；读取失败不阻断
      回复（日志 + 空块——画像注入是增强面而非回复前提），无现行事实时
      整块跳过。

关系进展注入（R2b+——复用构造注入 ``facts_reader`` 的事实读面）：

    - compose 链在核心记忆块之后、相关技能块之前恒注入【关系进展】块
      （``render_relationship_block``——由关系类现行事实派生的阶段 / 最近
      变化 / 边界约定摘要，总字符 1200 帽由渲染函数执行确定性截断）；
    - **来源只读既有 facts 层**：经 ``facts_reader.list_facts``（类别 =
      relationship、状态 = active）读取事实记录，再用
      ``persona.relationship.build_relationship_summary`` 纯函数聚合——
      不新增存储、不迁移、不改 DTO；得分公式 / 衰减档 / 阈值阶梯集中在
      ``persona.relationship_models`` 的锁定阈值表，可经
      ``ChatService(relationship_thresholds=...)`` 注入替换（无新 env 键）；
    - 注入面与人物事实同轴：仅在持久化会话绑定且已落库时读取（
      ``session_scope`` 独立短事务 + 人格上下文绑定）——纯内存形态零附加
      I/O；空事实产出 stranger / 0 空摘要并照常注入（关系状态是恒注入面，
      不是「有内容才注入」）；
    - **降级语义（锁定）**：读取失败记 warning 并降级为空摘要（stranger /
      0 / 零事件），回复链路照常——关系摘要不阻断回复；未接线读取面
      （CLI / 单元直调形态）整块跳过，既有行为零变更；
    - **人格侧等级上限**：预设 ``intimacy_level`` 只存在于生成期，聊天
      上下文拿不到该值——摘要只输出事实派生值（构建函数保留
      ``intimacy_ceiling`` 形参供未来接线），边界文档化于
      ``persona.relationship_models`` 模块文档。

亲密通道路由输入（R2b+ 乙分轨——可选独立模型端点，默认关闭）：

    - 本服务**只声明路由输入**：把本轮关系摘要的 ``intimacy_level`` 原样
      传给网关的 ``chat_by_role`` / ``stream_by_role``（``intimacy_level``
      关键字）；开关 / 配置齐全性 / 阈值 / 端点与凭据选择全部在 llm 域
      完成（见 ``llm.intimate_router`` 与 docs/backend/llm.md「亲密通道」
      一节）；
    - 一致性：``reply`` 的单次调用、工具循环的每一步与收尾调用、合规
      强化重试、``stream_reply`` 的全部网关调用携带同一输入——同一回合
      不会被换到另一条通道；
    - 摘要缺失（读取面未接线 / 未落库）传 ``None``、读取降级传空摘要的
      等级 0——两种情况都低于缺省阈值，自然回退默认通道；亲密通道调用
      失败按网关既有错误语义上抛，本服务不做静默换道。

相关技能注入（R2c——构造注入 ``skills_reader`` 后）：

    - compose 链在关系进展块之后、输出格式协议之前注入【相关技能】块
      （``render_skills_block``——仅当命中**本轮用户文本**时注入；命中
      由 ``SkillsService.match_for_compose`` 按技能触发短语的字面包含
      判定，条数 / 字符帽由服务与渲染函数分别施加）；
    - 注入面与人物事实同轴：仅在持久化会话绑定且已落库时读取（
      ``session_scope`` 独立短事务 + 人格上下文绑定）；读取失败不阻断
      回复（日志 + 空块——技能是操作增强面而非回复前提），无命中时整块
      跳过；命中后的使用计数写入由服务层 fire-and-forget 调度（回复
      链路不等待、不因计数失败而失败）。

核心记忆注入与自编辑工具闸（R2c——构造注入 ``core_memory_reader`` 后）：

    - compose 链在人物事实块之后、相关技能块之前注入【自我认知】块
      （``render_core_memory_block``——四块自编辑自我模型的非空块按固定
      块序渲染，锁定块标题带「（锁定）」标识；总字符 2400 帽由渲染函数
      截断停止）；注入面与人物事实同轴：仅在持久化会话已落库时读取，
      读取失败不阻断回复（日志 + 空块），四块全空时整块跳过；
    - **自编辑工具的结构性写入闸**：``edit_core_memory`` 工具注册在聊
      天工具注册表（app 装配——duty 不注册，见 docs/backend/persona.md
      装配一节），每轮工具列表按 ``sender_provenance`` 过滤——仅
      ``owner`` 回合保留该工具；群聊 ``peer`` 回合的工具列表里根本没有
      它（模型无从忘记不调用——peer 的临时插话不会污染人格自我模型）。
      工具执行期的会话主键经 ``core_memory_session_id_var`` 绑定（修订
      行的 ``session_id`` 归属），循环结束即复位。

意图快径前钩（R2a-6——构造注入 ``intent_shortcut`` 后，``reply`` 前端
先试快径；缺省 ``intent_shortcut=None`` 保持既有零快径形态）：

    - 匹配链：``reply`` 前端调用 ``intent_shortcut.try_shortcut``（
      :class:`IntentShortcutFace`——intent 域 ``IntentShortcutService``
      结构满足）；命中（``handled=True``）时**完全跳过网关调用**——
      罐头回复直接落回合；
    - 拟真延迟：命中后经 ``intent_shortcut.realism_delay``（800-1500ms
      随机——短区间「顺手秒回」语义，不走节律门完整延迟；决策文档化
      于 harness/intent/service）；
    - **持久化不变式**：快径回合照常落用户回合 + 罐头 assistant 回合
      （``state.turns`` + ``ff_chat_messages``——记忆 / 梦境可见该轮，
      文档化决策：快径对话是真实对话史的一部分）；
    - 注入面（通道专属）：app 装配仅在**通道路径**的 ChatService 工厂
      注入快径面（CLI / duty 不注入——行为零变化；意图快径是会话性
      能，duty 是无头计划任务，见 docs/backend/intent.md 装配一节）；
    - 流式 ``stream_reply`` 不走快径（CLI 消费面——通道回复全量走
      ``reply``，零分叉）。

会话持久化（**可选绑定，缺省关闭**）：

    - 构造注入 ``session_service``（memory 域 ``ChatSessionService``）
      后，``create_session(db_session=...)`` 同步落 ``ff_chat_sessions``
      行并在状态上携带 ``session_id``；
    - 绑定 + 已落库时，``reply / stream_reply`` 的用户与助手回合经
      ``session_scope`` 逐条落 ``ff_chat_messages``（seq 递增；开场白
      不落库——正典确定性回放，resume 后幂等重现）；
    - ``resume_session(session_id)`` 全量读取持久化会话消息，经
      ``max_history_turns`` 窗口截断后回放进内存态（保留最新回合；
      卡片按会话归属人格重取当前正典）；
    - 未绑定持久化服务（纯内存形态）时行为与既有完全一致——既有测试面
      零变更。

情景落库（对话回合 → 情景层管道——**可选绑定，缺省关闭**）：

    - 构造注入 ``episode_sink``（memory 域的 chat 回合情景落库面）后，
      ``reply / stream_reply`` 的每个**已落库**回合随即写入 kind=chat
      情景事件（user 回合 provenance=owner / assistant 回合
      provenance=agent）——检索索引 / 夜间梦境 / 生命周期巩固由此获得
      真实对话数据；
    - 时序锁定：消息行提交**之后**才写情景（消息是事实源，情景是派生
      镜像）；未落库会话（纯内存态）零写入；
    - 失败语义：sink 恒不抛（实现吞没），回复链路不受影响——fire-and-
      forget 分发与失败日志见 ``harness.memory.episode_sink``；
    - 未注入 sink（CLI / 单元测试 / 未装配记忆服务的形态）行为与既有
      完全一致。
"""

from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final, Protocol

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from packages.harness.chat.chat_models import (
    ChatSessionStateModel,
    ChatTurnModel,
    MessageCompositionResult,
)
from packages.harness.chat.compliance_guard import (
    build_retry_params,
    detect_assistant_violation,
)
from packages.harness.chat.compliance_models import ComplianceGuardSettings
from packages.harness.chat.injection_guard import (
    build_injection_hint,
    detect_injection,
    wrap_untrusted_content,
)
from packages.harness.chat.injection_models import InjectionGuardSettings
from packages.harness.chat.output_protocol import (
    ParsedAssistantOutputModel,
    StreamTagStripper,
    parse_assistant_output,
)
from packages.harness.chat.prompts import (
    UNTRUSTED_LABEL_DEFAULT,
    UNTRUSTED_LABEL_HISTORY,
    UNTRUSTED_LABEL_OWNER,
    UNTRUSTED_LABEL_PEER,
    UNTRUSTED_LABEL_TOOL_RESULT,
    WORLD_BOOK_SECTION_SEPARATOR,
    render_charter,
    render_compliance_nudge,
    render_constitution,
    render_core_memory_block,
    render_directives_block,
    render_emotion_state_block,
    render_facts_block,
    render_first_greeting,
    render_output_protocol,
    render_persona_contract,
    render_persona_system_prompt,
    render_post_history_message,
    render_relationship_block,
    render_skills_block,
    render_world_book_block,
)
from packages.harness.chat.world_book import (
    WorldBookSelectionResult,
    estimate_text_tokens,
    select_world_book_entries,
)
from packages.harness.config.model_entry import ModelRole
from packages.harness.core import time_utils
from packages.harness.core.dtos import PageResponse
from packages.harness.core.errors import BusinessException, ErrorCode
from packages.harness.database.session import session_scope
from packages.harness.emotion.service import EmotionService
from packages.harness.intent.intent_models import IntentMatchModel, PersonaNaming
from packages.harness.llm.gateway import LlmGateway
from packages.harness.llm.llm_models import (
    ChatMessageModel,
    ChatMessageRole,
    ChatResponseModel,
    ToolDefinitionModel,
)
from packages.harness.memory.episode_sink import EpisodeSinkFace
from packages.harness.memory.models import MemoryProvenance
from packages.harness.memory.service import ChatSessionService
from packages.harness.persona.chara_card import (
    CharaBookEntryModel,
    CharaBookPosition,
)
from packages.harness.persona.core_memory import core_memory_session_id_var
from packages.harness.persona.core_memory_models import (
    CORE_MEMORY_TOOL_NAME,
    CoreMemoryBlockDTO,
)
from packages.harness.persona.directives import DirectivesService
from packages.harness.persona.dtos import PersonaRecordDTO
from packages.harness.persona.facts_models import (
    FactQueryDTO,
    FactRecordDTO,
    PersonaFactStatus,
)
from packages.harness.persona.relationship import build_relationship_summary
from packages.harness.persona.relationship_models import (
    DEFAULT_RELATIONSHIP_THRESHOLDS,
    RELATIONSHIP_FACT_CATEGORY,
    RelationshipSummaryModel,
    RelationshipThresholdsModel,
)
from packages.harness.persona.service import PersonaService, bind_persona_context
from packages.harness.persona.skills_models import SkillRecordDTO
from packages.harness.tools.tool_models import (
    ToolCallRequestModel,
    ToolResultModel,
    ToolSpecModel,
)

__all__ = [
    "ChatService",
    "ToolExecutorFace",
    "IntentShortcutFace",
    "CHAT_TOOL_STEP_CAP",
    "HISTORY_WRAP_MAX_TURNS",
    "compose_messages",
    "estimate_text_tokens",
]

#: 用途常量——chat 会话调用恒以 chat 用途落用量账（usage 域枚举校验）。
CHAT_PURPOSE: str = "chat"

#: 历史回合包裹上限（最近 N 条 user 回合定界包裹；更早回合保持原文——
#: 长会话逐条包裹会让上下文线性膨胀，且边界信号边际收益递减）。
HISTORY_WRAP_MAX_TURNS: Final[int] = 10

#: chat 工具循环步数上限（锁定决策：5——chat 单轮回复的工具面收敛快于
#: duty 巡检的 8 步；超限触发收尾指令 + 无工具面最终调用）。
CHAT_TOOL_STEP_CAP: Final[int] = 5

#: 收尾指令（步数上限触发——system 追加块，本轮不再携带工具面）。
#: 就地定义说明：chat/prompts.py 不在本变更允许清单内（prompt Unit
#: 文件迁移属后续独立变更）；文本经测试锁定。
_TOOL_LOOP_FINAL_INSTRUCTION: Final[str] = (
    "【收尾指令】\n工具调用步数已用尽。请立即基于已有信息直接回复用户，不再调用任何工具。"
)


# ---------------------------------------------------------------------------
# 工具结果反馈渲染（chat 本地——duty render_tool_result 的 chat 侧对应物）
# ---------------------------------------------------------------------------


def _render_tool_result_content(result: ToolResultModel, *, wrap: bool) -> str:
    """工具执行结果 → tool 角色消息正文（可选定界包裹）。

    优先取执行器预格式化 ``text`` 块（web_search 的紧凑中文结果块）；
    其余结果整体紧凑 JSON（与 duty ``render_tool_result`` 同信息量——
    ok / result / error / 时延全量反馈，失败形态原样供模型自行调整）。
    ``wrap=True`` 时正文经 :func:`wrap_untrusted_content` 包进「工具结果」
    定界帧——工具输出可能夹带外部网页 / 评论原文，不能直接当规则读。

    Args:
        result: 执行结果（registry 归一终态）。
        wrap: 是否加不可信内容定界包裹（注入防御启用时由调用方传 True）。

    Returns:
        tool 消息正文文本（包裹形态为定界帧 + 原正文）。
    """
    if result.ok:
        preformatted = result.result.get("text")
        if isinstance(preformatted, str) and preformatted:
            body = preformatted
            if wrap:
                return wrap_untrusted_content(
                    body, label=UNTRUSTED_LABEL_TOOL_RESULT
                ).text
            return body
    body = json.dumps(result.model_dump(), ensure_ascii=False, separators=(",", ":"))
    if wrap:
        return wrap_untrusted_content(body, label=UNTRUSTED_LABEL_TOOL_RESULT).text
    return body


# ---------------------------------------------------------------------------
# 消息组装（纯函数——测试直调面）
# ---------------------------------------------------------------------------


def _split_sections(
    book_entries: Sequence[CharaBookEntryModel], selection: WorldBookSelectionResult
) -> tuple[list[CharaBookEntryModel], list[CharaBookEntryModel]]:
    """选择结果 → before/after 两段条目模型列表（按入选索引回查）。

    Args:
        book_entries: 世界书条目列表（索引回查源——卡片快照会话期
            不可变，索引即稳定身份）。
        selection: 选择结果（渲染序平铺列表）。

    Returns:
        ``(before_char 段, after_char 段)`` 二元组（各自保持渲染序）。
    """
    before: list[CharaBookEntryModel] = []
    after: list[CharaBookEntryModel] = []
    for selected in selection.entries:
        target = before if selected.section == CharaBookPosition.BEFORE_CHAR else after
        target.append(book_entries[selected.index])
    return before, after


def compose_messages(
    state: ChatSessionStateModel,
    *,
    rng: random.Random | None = None,
    emotion_summary: str | None = None,
    directives: Sequence[str] | None = None,
    facts: Sequence[str] | None = None,
    core_memory: Sequence[CoreMemoryBlockDTO] | None = None,
    relationship: RelationshipSummaryModel | None = None,
    skills: Sequence[SkillRecordDTO] | None = None,
    wrap_current_message: bool = False,
    current_message_label: str = "",
    injection_hint: str | None = None,
) -> MessageCompositionResult:
    """组装完整请求消息序列（纯函数——无 I/O / 无网关调用 / 无会话回写）。

    消息序列结构::

        [system]  章程 + 世界书 before 段 + 人格正典 + 宪章 + 角色契约 +
                  心情状态块 + 长期指令块 + 人物事实块 + 核心记忆块 +
                  关系进展块 + 相关技能块 + 输出格式协议块 + 世界书 after 段
        [user/assistant ...]  全部历史回合（含刚追加的用户回合；当轮入站
                  回合与最近 10 条历史 user 回合在 ``wrap_current_message=True``
                  时被定界包裹）
        [system]  注入提示块（仅检测命中且调用方传入 ``injection_hint`` 时；
                  紧接当轮包裹消息之后）
        [system]  历史后再注入（卡片声明 post_history_instructions 时；
                  位于**最后一条历史回合之后**——deep anti-drift 位置）

    宪章块（``render_constitution``）恒注入且紧跟人格正典——五条注入面
    防护规则先于一切记忆层与对话内容生效；角色契约块
    （``render_persona_contract``）紧随宪章之后、先于一切记忆层注入——
    宪章管身份与规则从哪来，契约管说出来的话像不像这个人（输出腔调
    约束）。

    心情状态块仅在 ``emotion_summary`` 非空时注入（非中性情绪——由
    ChatService 经 emotion 域读取 M 态产出）；长期指令块仅在
    ``directives`` 有有效条目时注入（现行指令——由 ChatService 经
    DirectivesService 读取；20 条 / 2000 字符帽由
    ``render_directives_block`` 施加）；人物事实块仅在 ``facts`` 有有效
    条目时注入（【关于对方】——由 ChatService 经事实读取面提供；条数 /
    字符帽在读取面施加）；核心记忆块仅在 ``core_memory`` 有非空块时注入
    （【自我认知】——由 ChatService 经核心记忆读取面提供；总字符帽在
    ``render_core_memory_block`` 施加）；关系进展块在 ``relationship`` 非空时
    恒注入（【关系进展】——由 ChatService 经事实读取面派生；空事实摘要同样
    注入，总字符帽在 ``render_relationship_block`` 施加）；相关技能块仅在
    ``skills`` 有命中条目时注入（【相关技能】——由 ChatService 经技能读取面
    按本轮用户文本匹配；条数帽在读取面、字符帽在 ``render_skills_block``
    施加）；输出格式协议块恒注入（心声 / 情绪标签指令——解析面见
    ``chat/output_protocol``）。

    当轮入站包裹（结构隔离，锁定边界）：``wrap_current_message=True``
    时**最后一条 user 回合**（即本轮刚追加的入站消息）被定界包裹；历史
    中的 **user 角色**回合也定界包裹，上限为最近
    :data:`HISTORY_WRAP_MAX_TURNS` 条（更早回合保持原文），assistant 回合
    不包裹（人格自己的发言是受信输出）。包裹用
    ``wrap_untrusted_content``（nonce 定界 + 来源标签）；当轮标签缺省回落
    「消息」，历史回合统一使用「历史消息」标签。``injection_hint`` 非空且
    确实发生包裹时，提示块作为 system 消息紧接当轮包裹回合之后追加（先于
    历史后再注入）。包裹仅作用于请求消息面，不改变 ``state.turns`` 原文
    与落库内容。

    世界书选择经 ``world_book.select_world_book_entries``（算法链与
    渲染序裁定见该模块文档）；推进后的回合状态（冷却时钟 + 注入
    追踪）随结果携带——**本函数不回写会话态**（功能式：调用方
    ``ChatService`` 回存；直调测试忽略即丢弃）。

    Args:
        state: 会话状态（卡片快照 + 回合列表 + 世界书回合状态）。
        rng: 概率门随机源（None = 现场构造非种子实例；仅卡片声明
            ``trigger_probability < 1.0`` 时消耗——缺省卡片零随机性）。
        emotion_summary: 非中性情绪摘要行（None / 空白 = 不注入心情块；
            M 态读取与衰减在调用方——本函数保持纯函数无 I/O）。
        directives: 现行长期指令正文列表（None / 空 = 不注入长期指令块；
            读取在调用方——本函数保持纯函数无 I/O）。
        facts: 现行人物事实注入行列表（``【类别标签】正文``——None / 空 =
            不注入人物事实块；读取与裁剪在调用方——本函数保持纯函数无
            I/O）。
        core_memory: 核心记忆块记录列表（None / 空 / 全空正文 = 不注入
            核心记忆块；读取在调用方、总字符帽在
            ``render_core_memory_block`` ——本函数保持纯函数无 I/O）。
        relationship: 关系进展摘要（None = 不注入关系进展块——读取面未接线
            形态；非 None 时恒注入，空事实摘要渲染「初识」；派生与时间注入
            在调用方——本函数保持纯函数无 I/O）。
        skills: 命中本轮用户文本的技能记录列表（None / 空 = 不注入相关
            技能块；匹配与条数帽在调用方、字符帽在 ``render_skills_block``
            ——本函数保持纯函数无 I/O）。
        wrap_current_message: 是否把当轮入站 user 回合与最近
            :data:`HISTORY_WRAP_MAX_TURNS` 条历史 user 回合包进不可信内容
            定界帧（注入防御启用时由 ChatService 传 True；直调缺省 False
            保持既有组装形态）。
        current_message_label: 当轮入站消息的来源标签（主人消息 / 群友消息
            ／……；空白回落 ``UNTRUSTED_LABEL_DEFAULT``）。
        injection_hint: 注入提示块文本（检测命中时由调用方渲染；None = 不
            追加提示——仅在发生包裹时生效）。

    Returns:
        组装结果（消息序列 + 观测元数据 + 推进后的世界书回合状态）。
    """
    selection = select_world_book_entries(
        state.card,
        [turn.content for turn in state.turns],
        rng=rng,
        turn_state=state.world_book_state,
    )
    book = state.card.character_book
    before_entries: list[CharaBookEntryModel] = []
    after_entries: list[CharaBookEntryModel] = []
    if book is not None:
        before_entries, after_entries = _split_sections(book.entries, selection)
    system_parts: list[str] = [render_charter()]
    before_block = render_world_book_block(before_entries)
    if before_block:
        system_parts.append(before_block)
    system_parts.append(render_persona_system_prompt(state.card))
    system_parts.append(render_constitution())
    system_parts.append(render_persona_contract(state.card.name))
    emotion_block = render_emotion_state_block(emotion_summary)
    if emotion_block:
        system_parts.append(emotion_block)
    directives_block = render_directives_block(directives or [])
    if directives_block:
        system_parts.append(directives_block)
    facts_block = render_facts_block(facts or [])
    if facts_block:
        system_parts.append(facts_block)
    core_memory_block = render_core_memory_block(core_memory or [])
    if core_memory_block:
        system_parts.append(core_memory_block)
    relationship_block = render_relationship_block(relationship)
    if relationship_block:
        system_parts.append(relationship_block)
    skills_block = render_skills_block(skills or [])
    if skills_block:
        system_parts.append(skills_block)
    system_parts.append(render_output_protocol())
    after_block = render_world_book_block(after_entries)
    if after_block:
        system_parts.append(after_block)
    system_prompt = WORLD_BOOK_SECTION_SEPARATOR.join(system_parts)

    messages: list[ChatMessageModel] = [
        ChatMessageModel(role=ChatMessageRole.SYSTEM, content=system_prompt)
    ]
    messages.extend(ChatMessageModel(role=turn.role, content=turn.content) for turn in state.turns)
    if wrap_current_message:
        current_is_user = bool(state.turns) and state.turns[-1].role == ChatMessageRole.USER
        history_end = len(state.turns) - 1 if current_is_user else len(state.turns)
        history_user_indices = [
            index
            for index in range(history_end)
            if state.turns[index].role == ChatMessageRole.USER
        ]
        for index in history_user_indices[-HISTORY_WRAP_MAX_TURNS:]:
            message_index = index + 1
            history_content = messages[message_index].content
            if isinstance(history_content, str):
                messages[message_index] = ChatMessageModel(
                    role=ChatMessageRole.USER,
                    content=wrap_untrusted_content(
                        history_content, label=UNTRUSTED_LABEL_HISTORY
                    ).text,
                )
    current_message_wrapped = False
    applied_hint: str | None = None
    current_content = messages[-1].content
    if (
        wrap_current_message
        and messages[-1].role == ChatMessageRole.USER
        and isinstance(current_content, str)
    ):
        wrapped = wrap_untrusted_content(
            current_content,
            label=current_message_label.strip() or UNTRUSTED_LABEL_DEFAULT,
        )
        messages[-1] = ChatMessageModel(role=ChatMessageRole.USER, content=wrapped.text)
        current_message_wrapped = True
        if injection_hint:
            applied_hint = injection_hint
            messages.append(
                ChatMessageModel(role=ChatMessageRole.SYSTEM, content=injection_hint)
            )
    post_history_message: str | None = None
    if state.card.post_history_instructions:
        post_history_message = render_post_history_message(state.card)
        messages.append(ChatMessageModel(role=ChatMessageRole.SYSTEM, content=post_history_message))
    return MessageCompositionResult(
        messages=messages,
        system_prompt=system_prompt,
        post_history_message=post_history_message,
        included_book_entries=selection.included,
        estimated_system_tokens=estimate_text_tokens(system_prompt),
        world_book_state=selection.turn_state,
        emotion_summary=emotion_summary if emotion_block else None,
        current_message_wrapped=current_message_wrapped,
        injection_hint=applied_hint,
    )


# ---------------------------------------------------------------------------
# 会话服务
# ---------------------------------------------------------------------------


class ToolExecutorFace(Protocol):
    """chat 工具执行面协议（duty 工具注册表的结构化消费契约）。

    结构化类型：真实 :class:`~packages.harness.tools.registry.ToolRegistry`
    天然满足（同签名 ``execute_call``——恒不抛异常的隔离执行契约）；
    测试注入 fake 同形态。
    """

    async def execute_call(self, call: ToolCallRequestModel) -> ToolResultModel:
        """执行单次工具调用（异常隔离——归一 ``ok=False`` 结果，不上抛）。

        Args:
            call: 执行请求（工具名 + 参数 dict）。

        Returns:
            执行结果（ok / result / error / 时延）。
        """
        ...


class IntentShortcutFace(Protocol):
    """chat 意图快径面协议（intent 域服务的结构化消费契约）。

    结构化类型：真实
    :class:`~packages.harness.intent.service.IntentShortcutService` 天然
    满足（同签名 ``try_shortcut`` / ``realism_delay``）；测试注入 fake
    同形态。注入面为**通道路径专属**（app 装配决策——CLI / duty 不注入，
    见模块 docstring「意图快径前钩」一节）。
    """

    async def try_shortcut(
        self, text: str, persona: PersonaNaming, now_ms: int | None = None
    ) -> IntentMatchModel:
        """尝试意图快径匹配（恒不抛异常——内部异常归一未命中）。

        Args:
            text: 用户消息原文。
            persona: 人格命名面（会话状态同形——称呼提取原料）。
            now_ms: 判定时刻（epoch 毫秒；None = 服务注入时钟）。

        Returns:
            匹配结果（``handled=False`` = 未命中——照常走 LLM）。
        """
        ...

    async def realism_delay(self) -> None:
        """快径拟真延迟（800-1500ms 随机 + 休眠面等待——注入式可测）。

        Raises:
            Exception: 休眠面异常（透传——注入面契约）。
        """
        ...


class FactsReaderFace(Protocol):
    """compose 人物事实读取面协议（persona 域事实服务的结构化消费契约）。

    结构化类型：真实 :class:`~packages.harness.persona.facts.FactsService`
    天然满足（``top_facts_for_compose``——现行高置信事实的注入行读取，
    条数 / 字符帽在实现侧施加；``list_facts``——类别 / 状态过滤的公开
    分页读面，关系进展摘要据此读取关系类事实）；测试注入 fake 同形态。
    注入面与长期指令同轴：仅在持久化会话已落库时读取（见模块 docstring
    「人物事实注入」／「关系进展注入」）。
    """

    async def top_facts_for_compose(self, session: AsyncSession) -> list[str]:
        """读取 compose 注入行（现行高置信事实——实现侧裁剪）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。

        Returns:
            注入行列表（``【类别标签】正文``；空列表 = 无现行事实）。
        """
        ...

    async def list_facts(
        self, session: AsyncSession, query: FactQueryDTO
    ) -> PageResponse[FactRecordDTO]:
        """读取事实分页记录（类别 / 状态过滤 + 游标分页——关系进展摘要的
        事实来源面）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            query: 查询入参（关系进展读取面固定类别 = relationship、状态 =
                active，页大小取阈值表的扫描上限）。

        Returns:
            分页响应（``items`` 为事实记录；空页 = 无匹配事实）。
        """
        ...


class SkillsReaderFace(Protocol):
    """compose 技能读取面协议（persona 域技能服务的结构化消费契约）。

    结构化类型：真实 :class:`~packages.harness.persona.skills.SkillsService`
    天然满足（``match_for_compose``——按本轮用户文本匹配现行技能，条数帽
    在实现侧施加；命中后的使用计数写入由实现侧 fire-and-forget 调度）；
    测试注入 fake 同形态。注入面与人物事实同轴：仅在持久化会话已落库时
    读取（见模块 docstring「相关技能注入」）。
    """

    async def match_for_compose(
        self, session: AsyncSession, user_text: str
    ) -> list[SkillRecordDTO]:
        """匹配本轮用户文本可用的技能（实现侧裁剪与强化）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。
            user_text: 本轮用户输入原文。

        Returns:
            命中技能记录列表（最多两条；空列表 = 无命中）。
        """
        ...


class CoreMemoryReaderFace(Protocol):
    """compose 核心记忆读取面协议（persona 域核心记忆服务的结构化消费契约）。

    结构化类型：真实 :class:`~packages.harness.persona.core_memory.CoreMemoryService`
    天然满足（``list_blocks``——四块自我模型的读取，首次读取懒建空行）；
    测试注入 fake 同形态。注入面与人物事实同轴：仅在持久化会话已落库时
    读取（见模块 docstring「核心记忆注入与自编辑工具闸」）。
    """

    async def list_blocks(self, session: AsyncSession) -> list[CoreMemoryBlockDTO]:
        """读取四个核心记忆块（固定块序——空块正文为空串）。

        Args:
            session: 注入的异步 Session（人格上下文已绑定）。

        Returns:
            四块记录列表。
        """
        ...


class ChatService:
    """人格感知聊天会话服务（组装 + 网关调用 + 会话内存态维护 + 可选持久化）。

    构造注入（测试 / 装配友好）：网关 / 人格服务 / 会话持久化服务 /
    LLM 工具面均可显式注入；测试注入 fake 网关记录组装消息断言组装契约
    （零真实网络）。``session_service=None``（缺省）即纯内存形态——
    持久化可选。``tools=None``（缺省）即零工具形态——既有 reply 行为
    全量保持（CLI 不传工具面——零变更）。
    """

    def __init__(
        self,
        *,
        gateway: LlmGateway,
        persona_service: PersonaService | None = None,
        session_service: ChatSessionService | None = None,
        tools: Sequence[ToolSpecModel] | None = None,
        tool_executor: ToolExecutorFace | None = None,
        intent_shortcut: IntentShortcutFace | None = None,
        emotion_service: EmotionService | None = None,
        directive_service: DirectivesService | None = None,
        facts_reader: FactsReaderFace | None = None,
        core_memory_reader: CoreMemoryReaderFace | None = None,
        skills_reader: SkillsReaderFace | None = None,
        injection_settings: InjectionGuardSettings | None = None,
        compliance_settings: ComplianceGuardSettings | None = None,
        relationship_thresholds: RelationshipThresholdsModel | None = None,
        episode_sink: EpisodeSinkFace | None = None,
    ) -> None:
        """初始化服务。

        Args:
            gateway: LLM 网关（chat 角色调用面）。
            persona_service: 人格注册表服务（主人格解析；None = 现场
                构造无状态默认实例）。
            session_service: 会话持久化服务（None = 纯内存态——兼容既有
                形态；绑定后 create_session(db_session=...) 落库、reply /
                stream_reply 逐回合持久化、resume_session 可用）。
            tools: LLM 工具规格列表（None / 空 = 零工具形态——既有
                行为；非空时 ``reply`` 走有界工具循环，与
                ``tool_executor`` 成对注入）。
            tool_executor: 工具执行面（``ToolRegistry`` 结构满足；
                与 ``tools`` 成对注入——缺一即装配错误 fail loud）。
            intent_shortcut: 意图快径面（None = 零快径形态——既有行为；
                注入后 ``reply`` 前端先试快径，命中即罐头回复 + 跳过
                网关——通道装配专属注入面，见模块 docstring）。
            emotion_service: 轻情绪引擎服务（None = 现场构造默认实例——
                读 ``EMOTION_`` env；M 态读写仅在持久化会话绑定且已落库
                时发生——纯内存形态零附加 I/O，见模块 docstring）。
            directive_service: 长期指令服务（None = 不注入长期指令块——
                行为与既有完全一致；注入后持久化会话回合在 compose 前
                读取现行指令——纯内存形态零附加 I/O，见模块 docstring）。
            facts_reader: 人物事实读取面（None = 不注入人物事实块——行为
                与既有完全一致；注入后持久化会话回合在 compose 前读取
                现行高置信事实——纯内存形态零附加 I/O，见模块
                docstring「人物事实注入」）。
            core_memory_reader: 核心记忆读取面（None = 不注入核心记忆块——
                行为与既有完全一致；注入后持久化会话回合在 compose 前读取
                四块自我模型——纯内存形态零附加 I/O，见模块
                docstring「核心记忆注入与自编辑工具闸」）。
            skills_reader: 技能读取面（None = 不注入相关技能块——行为与
                既有完全一致；注入后持久化会话回合在 compose 前按本轮
                用户文本匹配现行技能——纯内存形态零附加 I/O，见模块
                docstring「相关技能注入」）。
            injection_settings: 注入防御配置（None = 现场构造默认实例——
                读 ``INJECTION_`` env；缺省启用结构隔离与检测提示，见
                模块 docstring「注入面防护」）。
            compliance_settings: 人格输出合规守卫配置（None = 现场构造
                默认实例——读 ``COMPLIANCE_`` env；缺省启用一次强化重试，
                见模块 docstring「人格输出合规守卫」）。
            relationship_thresholds: 关系进展派生阈值表（None = 锁定缺省表
                :data:`~packages.harness.persona.relationship_models.
                DEFAULT_RELATIONSHIP_THRESHOLDS`——测试 / 运营调参注入面，
                无新 env 键；见模块 docstring「关系进展注入」）。
            episode_sink: chat 回合情景落库面（None = 零情景写入——CLI /
                未装配记忆服务的形态行为与既有完全一致；注入后每回合
                消息落库即写入 kind=chat 情景事件——fire-and-forget 语义
                与失败吞没见 ``_record_episode``）。

        Raises:
            BusinessException: ``tools`` 与 ``tool_executor`` 未成对注入
                （``INTERNAL_ERROR``——装配面编程错误）。
        """
        self._gateway = gateway
        self._persona_service = persona_service if persona_service is not None else PersonaService()
        self._session_service = session_service
        #: 意图快径面绑定（None = 零快径形态——reply 前钩判定面）。
        self._intent_shortcut = intent_shortcut
        #: 轻情绪引擎绑定（默认实例读 EMOTION_ env——停用时零读写）。
        self._emotion_service = emotion_service if emotion_service is not None else EmotionService()
        #: 长期指令服务绑定（None = 不注入——纯内存 / 未接线形态零 I/O）。
        self._directive_service = directive_service
        #: 人物事实读取面绑定（None = 不注入——纯内存 / 未接线形态零 I/O）。
        self._facts_reader = facts_reader
        #: 核心记忆读取面绑定（None = 不注入——纯内存 / 未接线形态零 I/O）。
        self._core_memory_reader = core_memory_reader
        #: 技能读取面绑定（None = 不注入——纯内存 / 未接线形态零 I/O）。
        self._skills_reader = skills_reader
        #: 注入防御配置（默认实例读 INJECTION_ env——见模块 docstring）。
        self._injection_settings = (
            injection_settings if injection_settings is not None else InjectionGuardSettings()
        )
        #: 合规守卫配置（默认实例读 COMPLIANCE_ env——见模块 docstring）。
        self._compliance_settings = (
            compliance_settings if compliance_settings is not None else ComplianceGuardSettings()
        )
        #: 关系进展阈值表（默认即锁定表——派生参数集中地，无 env 面）。
        self._relationship_thresholds = (
            relationship_thresholds
            if relationship_thresholds is not None
            else DEFAULT_RELATIONSHIP_THRESHOLDS
        )
        #: 情景落库面绑定（None = 零情景写入——记忆管道未接线形态）。
        self._episode_sink = episode_sink
        bound_tools = list(tools) if tools else []
        if bound_tools and tool_executor is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="chat 工具面装配错误：注入 tools 须同时注入 tool_executor",
            )
        if not bound_tools and tool_executor is not None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="chat 工具面装配错误：注入 tool_executor 须同时注入 tools",
            )
        #: 工具面绑定（None = 零工具形态；成对原子绑定——定义列表 +
        #: 执行面，杜绝单侧注入）。定义面经 ToolSpecModel →
        #: ToolDefinitionModel 一次转换（name / description / parameters
        #: 三键直映——args_model 校验留在 executor 侧 registry）。
        self._tools_binding: tuple[list[ToolDefinitionModel], ToolExecutorFace] | None = None
        if bound_tools and tool_executor is not None:
            self._tools_binding = (
                [
                    ToolDefinitionModel(
                        name=spec.name,
                        description=spec.description,
                        parameters=spec.args_schema,
                    )
                    for spec in bound_tools
                ],
                tool_executor,
            )

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        persona: PersonaRecordDTO | None = None,
        db_session: AsyncSession | None = None,
        title: str | None = None,
    ) -> ChatSessionStateModel:
        """创建聊天会话（快照人格正典 + 绑定 persona 轴；持久化可选）。

        持久化条件：``session_service`` 已绑定 **且** ``db_session`` 已
        提供（二者缺一即纯内存态）；落库经
        ``bind_persona_context`` 绑定人格上下文（PersonaMixin 自动注入）。

        Args:
            persona: 显式人格记录（CLI / 测试直供形态；优先于查库）。
            db_session: 数据库会话（``persona`` 未给时用于解析当前
                主人格；持久化服务绑定时兼作会话落库通道）。
            title: 持久化会话标题（None / 空 = 缺省「未命名会话」）。

        Returns:
            新建的 :class:`ChatSessionStateModel`（空回合列表；持久化
            时携带 ``session_id``）。

        Raises:
            BusinessException: 既未提供人格也未提供数据库会话
                （``INTERNAL_ERROR``）；当前无主人格（``NOT_FOUND``——
                服务层清晰中文错误直传，指引先 set-primary）。
        """
        record = persona
        if record is None:
            if db_session is None:
                raise BusinessException(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="创建聊天会话需要显式人格或数据库会话（二者至少其一）",
                )
            record = await self._persona_service.get_primary_persona(db_session)
        state = ChatSessionStateModel(
            persona_id=record.id,
            persona_name=record.name,
            display_name=record.display_name,
            card=record.card,
        )
        if self._session_service is not None and db_session is not None:
            async with bind_persona_context(record.id):
                summary = await self._session_service.create_session(db_session, title=title)
            state.session_id = summary.id
            logger.info("聊天会话已落库: persona_id={}, session_id={}", record.id, summary.id)
        logger.info("聊天会话已创建: persona_id={}, persona_name={}", record.id, record.name)
        return state

    async def resume_session(
        self, session_id: int, *, db_session: AsyncSession
    ) -> ChatSessionStateModel:
        """恢复持久化会话到内存态（全量读取、窗口截断后回放；卡片重取当前正典）。

        回放语义：持久化消息**全量读取**（append-only 完整记录不丢失），
        但仅回放**最近 ``max_history_turns`` 条**进内存态 ``turns``——
        与 ``append_turn`` 的滑动窗口同口径（内存窗口是上下文组装面，
        超窗历史不再进入后续 compose；持久化行不受影响）。

        人格作用域契约：会话行经 PersonaMixin 自动过滤——**调用方须先
        绑定目标人格上下文**（``bind_persona_context``）；他人格会话
        不可见（``NOT_FOUND``）。

        Args:
            session_id: 持久化会话主键（ff_chat_sessions.id）。
            db_session: 数据库会话（会话读取 + 人格正典重取）。

        Returns:
            恢复后的 :class:`ChatSessionStateModel`（携带 session_id +
            窗口截断后的回放回合列表——保留最新回合）。

        Raises:
            BusinessException: 持久化服务未绑定（``INTERNAL_ERROR``）；
                会话不存在 / 他人格会话 / 会话归属人格已删除
                （``NOT_FOUND``）。
        """
        if self._session_service is None:
            raise BusinessException(
                code=ErrorCode.INTERNAL_ERROR,
                message="会话持久化服务未绑定，无法恢复会话",
            )
        detail = await self._session_service.get_session(db_session, session_id)
        persona = await self._persona_service.get(db_session, int(detail.persona_id))
        state = ChatSessionStateModel(
            persona_id=persona.id,
            persona_name=persona.name,
            display_name=persona.display_name,
            card=persona.card,
            session_id=detail.id,
        )
        replay_window = detail.messages[-state.max_history_turns :]
        for message in replay_window:
            state.turns.append(
                ChatTurnModel(role=message.role, content=message.content, ts_ms=message.ts_ms)
            )
        logger.info(
            "聊天会话已恢复: session_id={}, persona_id={}, persisted={}, replayed={}",
            detail.id,
            persona.id,
            len(detail.messages),
            len(replay_window),
        )
        return state

    def opening(self, state: ChatSessionStateModel) -> str:
        """生成会话开场白（首条人格消息——正典 ``first_mes`` 优先）。

        开场白同时追加为首个 assistant 回合（进入会话历史——后续组装
        作为人格自己的先发言回放）。

        Args:
            state: 会话状态（空回合时生成开场；已有回合时返回末条
                assistant 回合内容——幂等入口）。

        Returns:
            开场白文本（卡片 ``first_mes`` 为空时回落 prompts 兜底模板）。
        """
        for turn in reversed(state.turns):
            if turn.role == ChatMessageRole.ASSISTANT:
                return turn.content
        greeting = render_first_greeting(state.card)
        state.append_turn(ChatMessageRole.ASSISTANT, greeting)
        return greeting

    # ------------------------------------------------------------------
    # 对话循环
    # ------------------------------------------------------------------

    async def reply(
        self,
        state: ChatSessionStateModel,
        user_text: str,
        *,
        sender_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> str:
        """单轮回复（非流式）：追加用户回合 → 组装 → 网关 chat_by_role。

        意图快径分派（R2a-6——``intent_shortcut`` 绑定时）：``reply``
        前端先试快径（``try_shortcut``）；命中（``handled=True``）时
        追加用户回合 → 拟真延迟（``realism_delay``——800-1500ms 随机）
        → 罐头文本追加为 assistant 回合（持久化语义与正常回复**完全
        同构**——用户 + 助手回合均落库，记忆 / 梦境可见；网关调用完全
        跳过）；未命中照常走组装 / 工具循环 / 网关链路。快径面自身
        恒不抛异常（未命中兜底——见 :class:`IntentShortcutFace`）。

        工具面分派：``tools`` 绑定时走有界工具循环（步数上限 + 收尾
        forcing——见模块 docstring「可选工具循环」；工具步为循环内部
        消息，不进入会话回合 / 持久化）；未绑定为既有单次调用形态。工具
        列表按 ``sender_provenance`` 裁剪：群聊 ``peer`` 回合不携带
        ``edit_core_memory``（自编辑写入闸——见模块 docstring「核心记忆
        注入与自编辑工具闸」）。

        注入面防护（``INJECTION_ENABLED`` 开启——见模块 docstring「注入面
        防护」）：当轮入站回合按来源分级加定界包裹（标签「主人消息」/
        「群友消息」），检测命中时在包裹回合之后追加注入提示块；工具循环
        回灌的 tool 轮正文同样定界包裹（「工具结果」）。

        人格输出合规守卫（``COMPLIANCE_ENABLED`` 开启——见模块 docstring
        「人格输出合规守卫」）：网关响应之后、协议解析之前检测助手腔，
        命中时按配置做一次强化重试（提醒块 + 温度提升），重试结果无条件
        采用；``stream_reply`` 不重试。

        失败语义：网关异常原样上抛（用户回合保留在历史——调用方决定
        重试或提示）。重试语义（文档化决策）：调用方对 ``reply`` 的任何
        重试都是**完整重发**——用户消息再次进入会话历史；持久化绑定且
        已落库时同样再落一行用户消息（append-only 日志不静默丢行）。
        显式的「重生成 / 复用上一条用户输入」API 有意不在本方法提供
        （推迟至后续 swipes 重试面特性）。

        Args:
            state: 会话状态。
            user_text: 用户输入（空白拒绝）。
            sender_provenance: 发送者来源分级（私聊 owner / 群聊 peer——
                情景落档按真实层级；缺省 owner 保持既有语义）。

        Returns:
            **可见正文**（已追加为 assistant 回合；持久化绑定且已落库时
            用户 + 助手回合均落 ``ff_chat_messages``——仅最终 assistant
            可见文本，工具中间态与协议标签均不落库正文；心声 / 情绪
            增量只进回合 meta；快径命中时为罐头文本，网关零调用）。

        Raises:
            BusinessException: 输入为空（``VALIDATION_ERROR``）；模型返回
                空回复（``LLM_CALL_FAILED``）；网关链路异常（透传）；
                持久化写入失败（透传——append-only 日志不静默丢行）。
        """
        if self._intent_shortcut is not None:
            match = await self._intent_shortcut.try_shortcut(user_text, state)
            if match.handled:
                logger.debug(
                    "意图快径命中（跳过网关）: persona={}, kind={}, confidence={:.2f}",
                    state.persona_name,
                    match.kind.value if match.kind is not None else "none",
                    match.confidence,
                )
                return await self._reply_via_shortcut(
                    state,
                    user_text,
                    match.response_text,
                    self._intent_shortcut,
                    sender_provenance=sender_provenance,
                )
        user_turn = self._append_user_turn(state, user_text)
        await self._persist_turn(state, user_turn)
        await self._record_episode(state, user_turn, user_provenance=sender_provenance)
        emotion_summary = await self._current_emotion_summary(state)
        directives = await self._current_directives(state)
        facts = await self._current_facts(state)
        core_memory = await self._current_core_memory(state)
        relationship = await self._current_relationship(state)
        skills = await self._current_skills(state, user_text)
        wrap_current, current_label, injection_hint = self._injection_compose_inputs(
            state, user_text, sender_provenance
        )
        result = compose_messages(
            state,
            emotion_summary=emotion_summary,
            directives=directives,
            facts=facts,
            core_memory=core_memory,
            relationship=relationship,
            skills=skills,
            wrap_current_message=wrap_current,
            current_message_label=current_label,
            injection_hint=injection_hint,
        )
        # 功能式回合状态回存：组装纯函数不回写会话态，冷却时钟推进由
        # 本服务消费后落回（快径路径不组装、不推进——时钟只度量真实
        # 组装回合）。
        state.world_book_state = result.world_book_state
        # 亲密通道路由输入：只声明本轮关系摘要的等级；端点选择与阈值裁决
        # 在 llm 网关完成（摘要缺失 / 降级为空摘要 → None / 0 → 默认通道）。
        intimacy_level = relationship.intimacy_level if relationship is not None else None
        if self._tools_binding is not None:
            raw_content = await self._tool_loop(
                result.messages,
                state,
                self._tools_binding,
                sender_provenance=sender_provenance,
                intimacy_level=intimacy_level,
            )
        else:
            response: ChatResponseModel = await self._gateway.chat_by_role(
                ModelRole.CHAT,
                result.messages,
                persona_id=str(state.persona_id),
                purpose=CHAT_PURPOSE,
                intimacy_level=intimacy_level,
            )
            raw_content = response.content
        raw_content = await self._apply_compliance_retry(
            result.messages, raw_content, state, intimacy_level=intimacy_level
        )
        parsed = parse_assistant_output(raw_content)
        content = parsed.visible_text
        if not content:
            raise BusinessException(
                code=ErrorCode.LLM_CALL_FAILED,
                message="模型返回空回复",
                details={"persona_id": state.persona_id},
            )
        meta = await self._emotion_round_meta(state, parsed)
        assistant_turn = state.append_turn(ChatMessageRole.ASSISTANT, content)
        await self._persist_turn(state, assistant_turn, meta=meta)
        await self._record_episode(state, assistant_turn)
        return content

    async def stream_reply(
        self,
        state: ChatSessionStateModel,
        user_text: str,
        *,
        sender_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> AsyncIterator[str]:
        """单轮流式回复：追加用户回合 → 组装 → 网关 stream_by_role。

        增量语义：逐块经 ``StreamTagStripper`` 做**起始前缀** best-effort
        标签剥离后 yield（空增量块跳过；未知 / 未闭合标签原样放行）；
        流自然结束后聚合已放行文本追加为 assistant 回合。本路径**不跑
        完整输出协议**：心声 / 情绪增量不落 meta、不推进 M 态（流式
        通道是操作员可见面——边界文档化于模块 docstring），也不跑人格
        输出合规重试（增量已逐块交给消费方，无法回头重说）。消费方提前终止
        （``GeneratorExit``）时**不追加助手回合**——半截回复不入历史
        （文档化决策；持久化面同语义——半截助手回合不落库）。重试语义
        同 ``reply``：调用方重试是完整重发——用户回合已落库时再落一行
        （append-only；显式重生成 / 复用 API 推迟至后续 swipes 特性）。

        Args:
            state: 会话状态。
            user_text: 用户输入（空白拒绝）。
            sender_provenance: 发送者来源分级（私聊 owner / 群聊 peer——
                情景落档层级与当轮包裹标签同源；缺省 owner）。

        Yields:
            回复增量文本块。

        Raises:
            BusinessException: 输入为空（``VALIDATION_ERROR``）；流结束但
                聚合全文为空（``LLM_CALL_FAILED``）；网关链路异常（透传）；
                持久化写入失败（透传）。
        """
        user_turn = self._append_user_turn(state, user_text)
        await self._persist_turn(state, user_turn)
        await self._record_episode(state, user_turn, user_provenance=sender_provenance)
        emotion_summary = await self._current_emotion_summary(state)
        directives = await self._current_directives(state)
        facts = await self._current_facts(state)
        core_memory = await self._current_core_memory(state)
        relationship = await self._current_relationship(state)
        skills = await self._current_skills(state, user_text)
        wrap_current, current_label, injection_hint = self._injection_compose_inputs(
            state, user_text, sender_provenance
        )
        result = compose_messages(
            state,
            emotion_summary=emotion_summary,
            directives=directives,
            facts=facts,
            core_memory=core_memory,
            relationship=relationship,
            skills=skills,
            wrap_current_message=wrap_current,
            current_message_label=current_label,
            injection_hint=injection_hint,
        )
        # 功能式回合状态回存（与 reply 同构——冷却时钟只度量真实组装回合）。
        state.world_book_state = result.world_book_state
        intimacy_level = relationship.intimacy_level if relationship is not None else None
        stripper = StreamTagStripper()
        parts: list[str] = []
        async for chunk in self._gateway.stream_by_role(
            ModelRole.CHAT,
            result.messages,
            persona_id=str(state.persona_id),
            purpose=CHAT_PURPOSE,
            intimacy_level=intimacy_level,
        ):
            if not chunk.content:
                continue
            emitted = stripper.feed(chunk.content)
            if not emitted:
                continue
            parts.append(emitted)
            yield emitted
        tail = stripper.flush()
        if tail:
            parts.append(tail)
            yield tail
        content = "".join(parts).strip()
        if not content:
            raise BusinessException(
                code=ErrorCode.LLM_CALL_FAILED,
                message="模型流式回复为空",
                details={"persona_id": state.persona_id},
            )
        assistant_turn = state.append_turn(ChatMessageRole.ASSISTANT, content)
        await self._persist_turn(state, assistant_turn)
        await self._record_episode(state, assistant_turn)

    # ------------------------------------------------------------------
    # 会话观测 / 维护
    # ------------------------------------------------------------------

    def history(self, state: ChatSessionStateModel) -> list[ChatTurnModel]:
        """读取会话回合列表（时间序副本）。

        Args:
            state: 会话状态。

        Returns:
            回合列表副本（外部修改不影响会话态）。
        """
        return list(state.turns)

    def clear_history(self, state: ChatSessionStateModel) -> None:
        """清空会话回合（/clear 命令面——人格绑定与卡片快照保留）。

        Args:
            state: 会话状态。
        """
        state.clear_turns()
        logger.info("会话历史已清空: persona_id={}", state.persona_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _injection_compose_inputs(
        self,
        state: ChatSessionStateModel,
        user_text: str,
        sender_provenance: MemoryProvenance,
    ) -> tuple[bool, str, str | None]:
        """构建注入防御的组装入参（包裹开关 / 来源标签 / 注入提示）。

        运行判定：``INJECTION_ENABLED`` 关闭时返回未启用形态（不包裹、不
        检测——回复链路回到原始组装）。启用时：标签按发送者来源分级
        （owner = 主人消息；peer / untrusted = 群友消息）；检测命中且
        ``INJECTION_ANTI_INJECTION_HINT`` 开启时，按 ``INJECTION_COUNTER_STYLE``
        渲染注入提示块（台词按本轮文本确定性选取）。检测只记 debug 日志、
        只追加提示——从不拦截回合。

        Args:
            state: 会话状态（日志归属人格）。
            user_text: 本轮入站文本（检测与台词种子）。
            sender_provenance: 本轮发送者来源分级（决定包裹标签）。

        Returns:
            ``(是否包裹当轮消息, 来源标签, 注入提示或 None)`` 三元组。
        """
        if not self._injection_settings.enabled:
            return False, "", None
        label = (
            UNTRUSTED_LABEL_OWNER
            if sender_provenance == MemoryProvenance.OWNER
            else UNTRUSTED_LABEL_PEER
        )
        signal = detect_injection(user_text, threshold=self._injection_settings.threshold)
        if not signal.suspected:
            return True, label, None
        logger.debug(
            "注入信号命中: persona_id={}, score={:.2f}, kinds={}",
            state.persona_id,
            signal.score,
            [reason.kind.value for reason in signal.reasons],
        )
        if not self._injection_settings.anti_injection_hint:
            return True, label, None
        hint = build_injection_hint(self._injection_settings.counter_style, user_text)
        return True, label, hint

    async def _apply_compliance_retry(
        self,
        messages: Sequence[ChatMessageModel],
        raw_content: str,
        state: ChatSessionStateModel,
        *,
        intimacy_level: int | None = None,
    ) -> str:
        """人格输出合规守卫：命中助手腔时做一次强化重试。

        运行判定：``COMPLIANCE_ENABLED`` 关闭时原样返回；启用时对原始回复
        跑 ``detect_assistant_violation``，未越过阈值原样返回。命中即记一条
        warning（人格 + 命中种类，不含回复正文），并在
        ``COMPLIANCE_MAX_RETRIES=1`` 时重试一次：同一段组装对话末尾追加
        【角色提醒】system 块，温度按 ``COMPLIANCE_TEMPERATURE_BUMP`` 提升
        （参数构建见 :func:`build_retry_params`）。重试结果**无条件返回**
        ——不再检测、不循环；重试仍失守的回复照常进入协议解析与落回合。

        工具循环分支的重试请求只带组装消息与提醒块、不携带工具面：工具步
        是循环内部状态，本次重写的是最终可见文本，不需要重新执行工具。

        Args:
            messages: 组装结果消息序列（重试请求的基底——不含工具循环内部
                的 assistant 调用轮与 tool 反馈轮）。
            raw_content: 网关返回的原始回复文本（含可能的协议标签）。
            state: 会话状态（卡片人格名进提醒块——与角色契约同名；人格
                ID 进日志与请求参数）。
            intimacy_level: 本轮关系摘要的亲密等级（0-3；None = 摘要不可用）
                ——重试请求沿用同一路由输入，避免重试回合被换到另一条
                通道。

        Returns:
            最终采用的原始回复文本（未命中 / 守卫关闭 / 重试禁用时为入参
            原样；命中且重试时为重试请求的回复文本）。
        """
        if not self._compliance_settings.enabled:
            return raw_content
        signal = detect_assistant_violation(
            raw_content, threshold=self._compliance_settings.threshold
        )
        if not signal.violated:
            return raw_content
        logger.warning(
            "人格输出失守（助手腔），触发强化重试: persona_id={}, score={:.2f}, kinds={}",
            state.persona_id,
            signal.score,
            [reason.kind.value for reason in signal.reasons],
        )
        if self._compliance_settings.max_retries < 1:
            return raw_content
        retry_messages = [
            *messages,
            ChatMessageModel(
                role=ChatMessageRole.SYSTEM,
                content=render_compliance_nudge(state.card.name),
            ),
        ]
        response: ChatResponseModel = await self._gateway.chat_by_role(
            ModelRole.CHAT,
            retry_messages,
            build_retry_params(
                None, temperature_bump=self._compliance_settings.temperature_bump
            ),
            persona_id=str(state.persona_id),
            purpose=CHAT_PURPOSE,
            intimacy_level=intimacy_level,
        )
        return response.content

    async def _tool_loop(
        self,
        messages: Sequence[ChatMessageModel],
        state: ChatSessionStateModel,
        binding: tuple[list[ToolDefinitionModel], ToolExecutorFace],
        *,
        sender_provenance: MemoryProvenance = MemoryProvenance.OWNER,
        intimacy_level: int | None = None,
    ) -> str:
        """有界工具循环（返回最终回复文本；机械镜像 duty_service._tool_loop）。

        循环消息为本地副本（组装结果 + assistant 工具调用轮回放 + tool
        反馈轮）——**不进入 ``state.turns`` / 持久化**（仅最终 assistant
        文本由 ``reply`` 落回合）。

        工具列表按本轮发送者来源分级（**结构性写入闸**）：仅 ``owner``
        回合携带完整工具面；``peer`` / ``untrusted`` 回合从列表里剔除
        ``edit_core_memory``——群成员的插话不能改写人格的自我模型（模型
        看不到该工具，无从「忘记不调用」）。剔除后若列表为空，网关请求
        不携带工具面（与零工具形态同 wire 语义）。

        工具执行期把会话主键绑定到 ``core_memory_session_id_var``（修订行
        的会话归属面），循环结束（含异常路径）复位。

        Args:
            messages: 组装结果消息序列（system + 历史 + 历史后再注入）。
            state: 会话状态（persona_id 归属面 + 会话主键绑定源）。
            binding: 工具面绑定（定义列表 + 执行面——reply 分派传入）。
            sender_provenance: 本轮发送者来源分级（缺省 owner——私聊 /
                本地入口；群聊 peer 会剔除自编辑工具）。
            intimacy_level: 本轮关系摘要的亲密等级（0-3；None = 摘要不可用
                ——逐次网关调用原样透传，通道选择在 llm 网关完成；工具循环
                的每一步与收尾调用保持同一路由输入）。

        Returns:
            最终回复文本（空串 = 模型未产出——调用方归
            ``LLM_CALL_FAILED``）。

        Raises:
            Exception: 网关链路异常（原样上抛——与零工具形态同失败语义；
            ``tool_executor.execute_call`` 自身恒不抛）。
        """
        definitions, executor = binding
        active_definitions = self._definitions_for_provenance(
            definitions, sender_provenance
        )
        loop_messages: list[ChatMessageModel] = list(messages)
        persona_id = str(state.persona_id)
        session_token = core_memory_session_id_var.set(state.session_id)
        try:
            for _ in range(CHAT_TOOL_STEP_CAP):
                response = await self._gateway.chat_by_role(
                    ModelRole.CHAT,
                    loop_messages,
                    tools=active_definitions or None,
                    persona_id=persona_id,
                    purpose=CHAT_PURPOSE,
                    intimacy_level=intimacy_level,
                )
                if not response.tool_calls:
                    return response.content.strip()
                loop_messages.append(
                    ChatMessageModel(
                        role=ChatMessageRole.ASSISTANT,
                        content=response.content,
                        tool_calls=response.tool_calls,
                    )
                )
                for call in response.tool_calls:
                    result = await executor.execute_call(
                        ToolCallRequestModel(name=call.name, arguments=call.arguments)
                    )
                    loop_messages.append(
                        ChatMessageModel(
                            role=ChatMessageRole.TOOL,
                            content=_render_tool_result_content(
                                result, wrap=self._injection_settings.enabled
                            ),
                            tool_call_id=call.id,
                        )
                    )
            # 步数上限触发：收尾指令 + 无工具面最终调用（final-report forcing）。
            loop_messages.append(
                ChatMessageModel(role=ChatMessageRole.SYSTEM, content=_TOOL_LOOP_FINAL_INSTRUCTION)
            )
            final_response = await self._gateway.chat_by_role(
                ModelRole.CHAT,
                loop_messages,
                persona_id=persona_id,
                purpose=CHAT_PURPOSE,
                intimacy_level=intimacy_level,
            )
            return final_response.content.strip()
        finally:
            core_memory_session_id_var.reset(session_token)

    @staticmethod
    def _definitions_for_provenance(
        definitions: Sequence[ToolDefinitionModel], sender_provenance: MemoryProvenance
    ) -> list[ToolDefinitionModel]:
        """按发送者来源分级裁剪工具列表（自编辑工具的结构性写入闸）。

        语义（锁定）：``owner`` 回合原样返回；其余分级（群聊 ``peer`` /
        平台 ``untrusted``）剔除 ``edit_core_memory``——自我模型只能由
        主人交互触发改写，群成员 / 平台内容只用不能改。

        Args:
            definitions: 装配期绑定的工具定义列表。
            sender_provenance: 本轮发送者来源分级。

        Returns:
            本轮实际进入 LLM 工具面的定义列表（可能为空）。
        """
        if sender_provenance == MemoryProvenance.OWNER:
            return list(definitions)
        return [definition for definition in definitions if definition.name != CORE_MEMORY_TOOL_NAME]

    async def _reply_via_shortcut(
        self,
        state: ChatSessionStateModel,
        user_text: str,
        canned_text: str,
        shortcut: IntentShortcutFace,
        *,
        sender_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> str:
        """快径回复落回合（用户回合 → 拟真延迟 → 罐头助手回合；零网关）。

        持久化语义与 ``reply`` 正常路径**完全同构**（``_append_user_turn``
        空白校验 + ``_persist_turn`` 逐回合落库）——快径对话是真实对话
        史的一部分（记忆 / 梦境消费面可见）。拟真延迟位于用户回合落库
        之后、助手回合落库之前（时间戳语义：用户消息到达时刻 → 人格
        「打字」耗时 → 回复产出时刻）。

        Args:
            state: 会话状态。
            user_text: 用户输入（空白拒绝——与正常路径同校验）。
            canned_text: 罐头回复文本（快径匹配层产出——非空契约）。
            shortcut: 快径面（拟真延迟消费——调用方已判非 None）。

        Returns:
            罐头回复文本（已追加为 assistant 回合）。

        Raises:
            BusinessException: 输入为空（``VALIDATION_ERROR``——与正常
                路径同边界）；持久化写入失败（透传——append-only 日志
                不静默丢行）。
        """
        user_turn = self._append_user_turn(state, user_text)
        await self._persist_turn(state, user_turn)
        await self._record_episode(state, user_turn, user_provenance=sender_provenance)
        await shortcut.realism_delay()
        assistant_turn = state.append_turn(ChatMessageRole.ASSISTANT, canned_text)
        await self._persist_turn(state, assistant_turn)
        await self._record_episode(state, assistant_turn)
        return assistant_turn.content

    def _append_user_turn(self, state: ChatSessionStateModel, user_text: str) -> ChatTurnModel:
        """追加用户回合（空白输入拒绝——边界校验）。

        Args:
            state: 会话状态。
            user_text: 用户输入。

        Returns:
            已追加的 :class:`ChatTurnModel`（持久化面消费）。

        Raises:
            BusinessException: 输入为空 / 纯空白（``VALIDATION_ERROR``）。
        """
        try:
            return state.append_turn(ChatMessageRole.USER, user_text)
        except ValueError as exc:
            raise BusinessException(
                code=ErrorCode.VALIDATION_ERROR,
                message="用户输入不能为空",
            ) from exc

    async def _persist_turn(
        self,
        state: ChatSessionStateModel,
        turn: ChatTurnModel,
        *,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """持久化单回合（持久化绑定 + 会话已落库时；否则 no-op）。

        经 ``session_scope`` 独立短事务写入（CLI / API 入口零 Session
        透传负担）；人格上下文按会话归属人格绑定（消息行非 PersonaMixin
        ——上下文为会话行更新面服务）。失败原样上抛（append-only 日志
        不静默丢行——文档化决策）。

        Args:
            state: 会话状态（``session_id`` 为 None 时 no-op）。
            turn: 待落库回合。
            meta: 回合元数据（心声 / 情绪增量 / 情绪快照；None = 无
                元数据——既有调用方零变更）。
        """
        if self._session_service is None or state.session_id is None:
            return
        async with session_scope() as db_session, bind_persona_context(state.persona_id):
            await self._session_service.append_message(
                db_session,
                state.session_id,
                role=turn.role,
                content=turn.content,
                ts_ms=turn.ts_ms,
                meta=meta,
            )

    async def _record_episode(
        self,
        state: ChatSessionStateModel,
        turn: ChatTurnModel,
        user_provenance: MemoryProvenance = MemoryProvenance.OWNER,
    ) -> None:
        """把已落库回合并入情景层（sink 未接线 / 会话未落库时 no-op）。

        时序：**消息行提交之后**调用（回合持久化是情景镜像的前提）；
        ``session_id`` 为 None（纯内存态）时零写入——情景事件必须可回溯
        到会话。失败仅日志：sink 契约恒不抛，本方法仍显式兜底（回复链路
        的失败路径不应包含记忆管道面）。

        Args:
            state: 会话状态（``session_id`` / ``persona_id`` 为事件归属面）。
            turn: 已落库回合（角色 / 正文 / 时间——事件载荷来源）。
        """
        if self._episode_sink is None or state.session_id is None:
            return
        try:
            await self._episode_sink.record_chat_turn(
                state.persona_id,
                state.session_id,
                turn.role.value,
                turn.content,
                turn.ts_ms,
                user_provenance=user_provenance,
            )
        except Exception:  # best-effort: 记忆管道面不进回复失败路径（显式兜底）
            logger.opt(exception=True).warning(
                "chat 回合情景落库失败（已吞没——不影响回复）: session_id={}",
                state.session_id,
            )

    def _emotion_runtime_active(self, state: ChatSessionStateModel) -> bool:
        """情绪引擎运行判定（持久化会话已落库 + 引擎启用）。

        M 态存储面依赖数据库会话——纯内存形态 / 未落库会话一律停机
        （零附加 I/O，既有纯内存测试面零变更）。

        Args:
            state: 会话状态。

        Returns:
            True = 本次回复可读 / 写 M 态情绪。
        """
        return (
            self._session_service is not None
            and state.session_id is not None
            and self._emotion_service.enabled
        )

    async def _current_emotion_summary(self, state: ChatSessionStateModel) -> str | None:
        """读取当前情绪摘要行（compose 心情块注入面——非中性才非空）。

        Args:
            state: 会话状态。

        Returns:
            中文摘要行；引擎未激活 / 无状态 / 全中性时为 None。
        """
        if not self._emotion_runtime_active(state):
            return None
        return await self._emotion_service.summary(state.persona_id)

    async def _current_directives(self, state: ChatSessionStateModel) -> list[str]:
        """读取现行长期指令（compose 长期指令块注入面）。

        运行判定：持久化会话已落库 + 服务已接线（与情绪引擎同轴——M 态 /
        指令一律依赖数据库；纯内存形态零附加 I/O）。读取失败不阻断回复
        （日志 + 空列表——常备指令是增强面）。

        Args:
            state: 会话状态。

        Returns:
            现行指令正文列表（新写入在前）；未接线 / 未落库 / 读取失败
            时为空列表。
        """
        if (
            self._directive_service is None
            or self._session_service is None
            or state.session_id is None
        ):
            return []
        try:
            async with session_scope() as db_session, bind_persona_context(state.persona_id):
                return await self._directive_service.list_active_texts(db_session)
        except Exception:  # best-effort：指令读取失败不阻断回复链路
            logger.opt(exception=True).warning(
                "长期指令读取失败（本轮不注入长期指令块）: persona_id={}", state.persona_id
            )
            return []

    async def _current_facts(self, state: ChatSessionStateModel) -> list[str]:
        """读取现行人物事实注入行（compose 人物事实块注入面）。

        运行判定：持久化会话已落库 + 读取面已接线（与长期指令同轴——画像
        依赖数据库；纯内存形态零附加 I/O）。读取失败不阻断回复（日志 +
        空列表——画像是增强面）。

        Args:
            state: 会话状态。

        Returns:
            注入行列表（置信度降序、读取面已裁剪）；未接线 / 未落库 /
            读取失败时为空列表。
        """
        if self._facts_reader is None or self._session_service is None or state.session_id is None:
            return []
        try:
            async with session_scope() as db_session, bind_persona_context(state.persona_id):
                return await self._facts_reader.top_facts_for_compose(db_session)
        except Exception:  # best-effort：事实读取失败不阻断回复链路
            logger.opt(exception=True).warning(
                "人物事实读取失败（本轮不注入人物事实块）: persona_id={}", state.persona_id
            )
            return []

    async def _current_relationship(
        self, state: ChatSessionStateModel
    ) -> RelationshipSummaryModel | None:
        """派生本轮关系进展摘要（compose 关系进展块注入面）。

        运行判定：持久化会话已落库 + 事实读取面已接线（与人物事实同轴——
        关系摘要依赖事实库；纯内存形态零附加 I/O，未接线形态整块跳过）。
        读取面经 ``facts_reader.list_facts`` 取关系类现行事实（类别 =
        ``relationship``、状态 = ``active``、页大小 = 阈值表扫描上限），
        聚合由 :func:`~packages.harness.persona.relationship.
        build_relationship_summary` 完成（确定性纯函数，``now_ms`` 在此处
        注入——模块内不读系统时钟）。

        降级语义（锁定）：读取 / 派生异常记 warning 并降级为空摘要
        （stranger / 0 / 零事件）——关系摘要不阻断回复；空事实同样产出
        空摘要并照常注入（关系状态是恒注入面）。

        Args:
            state: 会话状态。

        Returns:
            关系进展摘要；读取面未接线 / 未落库时为 None（组装链跳过整块）。
        """
        if self._facts_reader is None or self._session_service is None or state.session_id is None:
            return None
        now_ms = time_utils.now_ms()
        try:
            async with session_scope() as db_session, bind_persona_context(state.persona_id):
                page = await self._facts_reader.list_facts(
                    db_session,
                    FactQueryDTO(
                        category=RELATIONSHIP_FACT_CATEGORY,
                        status=PersonaFactStatus.ACTIVE,
                        limit=self._relationship_thresholds.event_scan_limit,
                    ),
                )
            return build_relationship_summary(
                page.items,
                now_ms=now_ms,
                thresholds=self._relationship_thresholds,
            )
        except Exception:  # best-effort：关系摘要失败不阻断回复链路（降级空摘要）
            logger.opt(exception=True).warning(
                "关系进展派生失败（本轮降级为空摘要）: persona_id={}", state.persona_id
            )
            return build_relationship_summary(
                (),
                now_ms=now_ms,
                thresholds=self._relationship_thresholds,
            )

    async def _current_core_memory(
        self, state: ChatSessionStateModel
    ) -> list[CoreMemoryBlockDTO]:
        """读取核心记忆四块（compose 核心记忆块注入面）。

        运行判定：持久化会话已落库 + 读取面已接线（与人物事实同轴——自我
        模型依赖数据库；纯内存形态零附加 I/O）。读取失败不阻断回复（日志
        + 空列表——核心记忆是增强面）；四块全空时渲染函数返回空串、组装
        链跳过整块。

        Args:
            state: 会话状态。

        Returns:
            四块记录列表（固定块序）；未接线 / 未落库 / 读取失败时为空
            列表。
        """
        if (
            self._core_memory_reader is None
            or self._session_service is None
            or state.session_id is None
        ):
            return []
        try:
            async with session_scope() as db_session, bind_persona_context(state.persona_id):
                return await self._core_memory_reader.list_blocks(db_session)
        except Exception:  # best-effort：核心记忆读取失败不阻断回复链路
            logger.opt(exception=True).warning(
                "核心记忆读取失败（本轮不注入核心记忆块）: persona_id={}", state.persona_id
            )
            return []

    async def _current_skills(
        self, state: ChatSessionStateModel, user_text: str
    ) -> list[SkillRecordDTO]:
        """匹配本轮可用的技能（compose 相关技能块注入面）。

        运行判定：持久化会话已落库 + 读取面已接线（与人物事实同轴——
        技能依赖数据库；纯内存形态零附加 I/O）。读取失败不阻断回复
        （日志 + 空列表——技能是操作增强面）。服务质量面自身已按条数帽
        裁剪，命中计数写入由其 fire-and-forget 调度（本方法不等待）。

        Args:
            state: 会话状态。
            user_text: 本轮用户输入原文（匹配面输入——只有命中当前输入
                的技能才会注入）。

        Returns:
            命中技能记录列表；未接线 / 未落库 / 读取失败时为空列表。
        """
        if self._skills_reader is None or self._session_service is None or state.session_id is None:
            return []
        try:
            async with session_scope() as db_session, bind_persona_context(state.persona_id):
                return await self._skills_reader.match_for_compose(db_session, user_text)
        except Exception:  # best-effort：技能读取失败不阻断回复链路
            logger.opt(exception=True).warning(
                "技能匹配失败（本轮不注入相关技能块）: persona_id={}", state.persona_id
            )
            return []

    async def _emotion_round_meta(
        self, state: ChatSessionStateModel, parsed: ParsedAssistantOutputModel
    ) -> dict[str, Any] | None:
        """构建本轮回合元数据（心声 + 情绪增量 + 推进后快照）。

        引擎未激活时仍保留心声 / 增量留档（观测面），快照为 None；
        无心声且无增量时返回 None（零 meta 写入——既有行形态不变）。

        Args:
            state: 会话状态。
            parsed: 输出协议解析结果。

        Returns:
            回合元数据字典；无任何元数据时为 None。
        """
        if parsed.thought is None and not parsed.emotion_deltas:
            return None
        snapshot: dict[str, float] | None = None
        if self._emotion_runtime_active(state) and parsed.emotion_deltas:
            advanced = await self._emotion_service.apply_deltas(
                state.persona_id, parsed.emotion_deltas
            )
            if advanced is not None:
                snapshot = dict(advanced.values)
        return {
            "thought": parsed.thought,
            "emotion_deltas": dict(parsed.emotion_deltas),
            "emotion_snapshot": snapshot,
        }
