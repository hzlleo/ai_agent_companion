"""memory 域检索层 DTO——索引条目 / 检索查询 / 检索结果的传输契约（Unit 文件）。

P1 chunk 2 检索层契约分层：

    - :class:`MemorySearchMode`   —— 检索模式值域（dense / keyword / hybrid）；
    - :class:`MemoryRefType`      —— 索引引用类型值域（episode / curated）；
    - :class:`MemoryIndexEntryDTO` —— 索引写入面（append → embed → upsert 链的
      条目形态；**向量不在此 DTO**——由 embedder（gateway.embed_by_role，
      embed 角色）产出后随条目一并交给存储层）；
    - :class:`MemorySearchQueryDTO` —— 检索查询面（人格绑定 + 模式 + 标量
      过滤 + limit）；
    - :class:`MemorySearchResultDTO` —— 检索结果面（score 语义随模式而变，
      见类 docstring）；
    - :class:`MemorySearchRequestDTO` —— API 请求体（POST
      /api/v1/memories/search 的 body 契约）；
    - :class:`SeekDBStoreConfig`  —— SeekDB 存储的装配配置 Model Class。

时间语义：全部 epoch 毫秒；检索时间范围过滤为**闭区间**
（``start_ms <= ts_ms <= end_ms``——与 SeekDB 混合检索事实卡一致；
与情景事件查询 DTO 的左闭右开约定不同，差异文档化于
docs/backend/memory.md 检索层一节）。

人格作用域：检索索引表（ff_memory_index）为 raw SQL 面（无
PersonaMixin 概念）——``persona_id`` 是显式列 + 每条查询的必带谓词
（隔离由 SQL 谓词强制，见 store 模块构建器）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from packages.harness.memory.models import MemoryProvenance

__all__ = [
    "MemorySearchMode",
    "MemoryRefType",
    "MemoryIndexEntryDTO",
    "MemorySearchQueryDTO",
    "MemorySearchResultDTO",
    "MemorySearchRequestDTO",
    "SeekDBStoreConfig",
]


class MemorySearchMode(StrEnum):
    """记忆检索模式值域。

    - ``dense``：稠密向量检索（查询文本经 embed 角色向量化后 ANN 近邻）；
    - ``keyword``：全文关键词检索（MATCH ... AGAINST / 回退存储 LIKE）；
    - ``hybrid``：混合检索（关键词谓词预过滤 + 向量距离排序）。
    """

    DENSE = "dense"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


class MemoryRefType(StrEnum):
    """检索索引引用类型值域（索引行指向的记忆层来源）。

    - ``episode``：情景层事件（ff_memory_episodes 行）；
    - ``curated``：策展层记忆（ff_curated_memory 行）。
    """

    EPISODE = "episode"
    CURATED = "curated"


class MemoryIndexEntryDTO(BaseModel):
    """记忆索引条目 DTO（写入面——append → embed → upsert 链的条目形态）。

    向量**不在本 DTO**：条目只承载标量与正文文本；向量化是 embedder
    （gateway.embed_by_role，embed 角色）的职责，产出后与条目同序一并
    交给存储层 upsert（条目与向量的配对由调用方保证）。

    Attributes:
        persona_id: 人格主键（数值形态——索引表显式 BigInt 列；与
            PersonaMixin 的字符串形态经构建器转换）。
        ref_type: 引用类型（episode / curated）。
        ref_id: 引用行主键（ff_memory_episodes.id / ff_curated_memory.id）。
        kind: 记忆类型值（episode kind 或 curated kind 的字符串值——
            两层词表不同，索引层不做枚举收口）。
        provenance: 来源可信度（全记忆层统一 taxonomy）。
        ts_ms: 事件时间（epoch 毫秒——时间范围过滤轴）。
        content: 索引正文（向量化的输入文本 + 全文检索的匹配面；
            非空）。
    """

    persona_id: int = Field(ge=1, description="人格主键（索引表 BigInt 列）")
    ref_type: MemoryRefType = Field(description="引用类型（episode/curated）")
    ref_id: int = Field(ge=1, description="引用行主键")
    kind: str = Field(min_length=1, max_length=32, description="记忆类型值（episode/curated kind）")
    provenance: MemoryProvenance = Field(description="来源可信度")
    ts_ms: int = Field(ge=0, description="事件时间（epoch 毫秒）")
    content: str = Field(min_length=1, description="索引正文（嵌入输入 + 全文匹配面）")


class MemorySearchQueryDTO(BaseModel):
    """记忆检索查询 DTO（人格绑定 + 模式 + 标量过滤 + limit）。

    Attributes:
        persona_id: 目标人格主键（必带——索引隔离谓词；由入口层解析
            目标人格后填充，非调用方自由填写）。
        query: 检索文本（keyword 模式 = 关键词；dense / hybrid 模式 =
            待向量化查询文本）。
        mode: 检索模式（默认 dense——生产主路径；回退存储仅支持
            keyword）。
        kinds: 记忆类型过滤（None = 全部；SQL IN 谓词）。
        provenance: 来源可信度过滤（None = 全部）。
        start_ms: 时间范围起点（epoch 毫秒，含；None = 不限）。
        end_ms: 时间范围终点（epoch 毫秒，含；None = 不限——检索层
            闭区间语义，见模块 docstring）。
        limit: 返回条数上限（1-50，默认 10——上限由 DTO 边界强制，
            即 SQL LIMIT 的钳位面）。
    """

    persona_id: int = Field(ge=1, description="目标人格主键（隔离谓词）")
    query: str = Field(min_length=1, max_length=512, description="检索文本")
    mode: MemorySearchMode = Field(
        default=MemorySearchMode.DENSE, description="检索模式（默认 dense）"
    )
    kinds: list[str] | None = Field(
        default=None, max_length=10, description="记忆类型过滤（None = 全部）"
    )
    provenance: MemoryProvenance | None = Field(default=None, description="来源可信度过滤")
    start_ms: int | None = Field(default=None, ge=0, description="范围起点（epoch 毫秒，含）")
    end_ms: int | None = Field(default=None, ge=0, description="范围终点（epoch 毫秒，含）")
    limit: int = Field(default=10, ge=1, le=50, description="返回条数上限（1-50）")


class MemorySearchResultDTO(BaseModel):
    """记忆检索结果 DTO（单条命中——引用定位 + 正文 + 相关度分）。

    score 语义随模式而变（文档化决策——两种度量的天然方向差异）：

        - dense / hybrid：cosine 距离（**越小越相关**，升序返回）；
        - keyword：全文相关度（SeekDB MATCH 分数 / 回退存储朴素
          子串计数——**越大越相关**）。

    调用方按 ``mode`` 解释 score 方向；结果已按对应方向排好序。

    Attributes:
        ref_type: 引用类型（episode / curated——回源定位用）。
        ref_id: 引用行主键（回源定位用）。
        kind: 记忆类型值。
        provenance: 来源可信度。
        ts_ms: 事件时间（epoch 毫秒）。
        content: 索引正文（命中片段展示面）。
        score: 相关度分（语义见类 docstring）。
    """

    ref_type: MemoryRefType = Field(description="引用类型（episode/curated）")
    ref_id: int = Field(description="引用行主键")
    kind: str = Field(description="记忆类型值")
    provenance: MemoryProvenance = Field(description="来源可信度")
    ts_ms: int = Field(description="事件时间（epoch 毫秒）")
    content: str = Field(description="索引正文")
    score: float = Field(description="相关度分（方向随模式——见类 docstring）")


class MemorySearchRequestDTO(BaseModel):
    """记忆检索 API 请求体 DTO（POST /api/v1/memories/search）。

    与 :class:`MemorySearchQueryDTO` 的分工：请求体承载入口层可自由
    填写的字段（``persona_id`` 可选——缺省当前激活人格）；目标人格
    解析后由入口层构建查询 DTO（persona_id 必填面）。

    Attributes:
        persona_id: 目标人格主键（None = 当前激活人格；无激活则 404）。
        query: 检索文本（1-512 字符）。
        mode: 检索模式（默认 dense）。
        kinds: 记忆类型过滤（None = 全部）。
        provenance: 来源可信度过滤（None = 全部）。
        start_ms: 时间范围起点（epoch 毫秒，含；None = 不限）。
        end_ms: 时间范围终点（epoch 毫秒，含；None = 不限）。
        limit: 返回条数上限（1-50，默认 10）。
    """

    persona_id: int | None = Field(
        default=None, ge=1, description="目标人格主键（None = 当前激活人格）"
    )
    query: str = Field(min_length=1, max_length=512, description="检索文本")
    mode: MemorySearchMode = Field(
        default=MemorySearchMode.DENSE, description="检索模式（默认 dense）"
    )
    kinds: list[str] | None = Field(
        default=None, max_length=10, description="记忆类型过滤（None = 全部）"
    )
    provenance: MemoryProvenance | None = Field(default=None, description="来源可信度过滤")
    start_ms: int | None = Field(default=None, ge=0, description="范围起点（epoch 毫秒，含）")
    end_ms: int | None = Field(default=None, ge=0, description="范围终点（epoch 毫秒，含）")
    limit: int = Field(default=10, ge=1, le=50, description="返回条数上限（1-50）")


class SeekDBStoreConfig(BaseModel):
    """SeekDB 存储装配配置 Model Class（存储面的非 env 配置形态）。

    env 面（``SEEKDB_*`` 前缀）由 config 域 :class:`SeekDBSettings`
    承载；本类是存储构造的纯配置形态（不含 env 读取），经装配工厂
    （``build_memory_store``）从 Settings 映射而来——config 域不反向
    依赖 memory 域（依赖方向单向：memory → config）。

    密码经环境变量间接：本配置只持有变量名 ``password_env``，实际
    密码由存储层在创建连接池时从该环境变量读取（与 llm 条目
    ``api_key_env`` 同模式——密码永不落 yaml / 配置树）。

    Attributes:
        host: SeekDB 服务地址（server 模式）。
        port: SeekDB MySQL 线协议端口（默认 2881）。
        user: 连接用户名。
        password_env: 密码环境变量名。
        database: 连接库名。
        vector_dim: 向量维度（须与 embed 角色模型输出一致；默认 1024
            = Qwen3-Embedding-0.6B——插值进索引表 DDL 的 VECTOR(dim)）。
        pool_min: 连接池常驻连接数。
        pool_max: 连接池连接上限（不得小于 pool_min）。
    """

    host: str = Field(default="127.0.0.1", description="SeekDB 服务地址")
    port: int = Field(default=2881, ge=1, le=65535, description="SeekDB MySQL 线协议端口")
    user: str = Field(default="root", description="连接用户名")
    password_env: str = Field(default="SEEKDB_PASSWORD", min_length=1, description="密码环境变量名")
    database: str = Field(default="test", description="连接库名")
    vector_dim: int = Field(
        default=1024, ge=1, le=65535, description="向量维度（与 embed 模型输出一致）"
    )
    pool_min: int = Field(default=1, ge=1, description="连接池常驻连接数")
    pool_max: int = Field(default=4, ge=1, description="连接池连接上限")

    @model_validator(mode="after")
    def _verify_pool_bounds(self) -> SeekDBStoreConfig:
        """校验连接池边界（pool_max 不得小于 pool_min）。

        Returns:
            校验通过的实例。

        Raises:
            ValueError: pool_max < pool_min（装配错误面）。
        """
        if self.pool_max < self.pool_min:
            raise ValueError(f"pool_max({self.pool_max}) 不得小于 pool_min({self.pool_min})")
        return self
