# canvas — 渲染产物（画布）持久化与导出面

`backend/src/packages/harness/canvas/` + `app/api/canvas.py` + 前端
`/canvas` 面板共同构成渲染产物能力：**按人格作用域持久化自包含渲染产物
（HTML / SVG / Markdown）、在管理台沙箱预览、单文件导出**。产物是纯文本
（不落文件、不经渲染服务），导出字节可直接作为通道协议 v2 的 `file` 附件
投递（三通道均支持 `file` 类）。

能力边界（诚实边界）：

- **不是会话画布可视化**：本切片交付的是「产物面」（生成 / 存储 / 预览 /
  导出），不是把会话消息流画成图谱的可视化；会话侧的可视化若需要，属后续
  在既有会话 API 之上叠加的展示面。
- **不是 LLM 工具**：当前没有 `render_canvas` 类工具——产物由管理台创建 /
  修订（与核心记忆面板同形态：人可编辑，门面在管理台）。让模型在对话中直接
  产出画布属下一步（接工具注册表 + 人格上下文即可，存储与 API 已就绪）。
- **预览不执行脚本**：见「安全边界」。

## 锁定决策

| 决策 | 内容 | 理由 |
|---|---|---|
| 存储形态 | 人格作用域的行内文本列（`ff_canvas_documents`），不落文件 | HTML / SVG / Markdown 均为 UTF-8 文本；行删即内容删，没有「行没了文件还在」的清理面 |
| 类型固定 | 类型创建后不可改（更新只改标题 / 内容） | 类型决定预览方式与导出 MIME / 扩展名；跨类型改写在语义上是「换一份产物」 |
| 修订号 | `revision` 创建为 1、每次修改递增；导出文件名带修订号 | 同一产物多次导出不互相覆盖，历史版本可从命名区分 |
| 预览沙箱 | `iframe sandbox=""`（**不带 allow-scripts**） | 模型生成的 HTML / SVG 里的脚本不在管理台上下文执行——生成内容不产生副作用 |
| 导出 | 原始文本字节直出（`Content-Disposition: attachment`） | 产物自包含；下载后的执行由打开者决定，MIME 按类型固定 |
| 人格隔离 | `PersonaMixin` 上下文自动注入 / 过滤 | 与他人格数据同轴；他人格产物与不存在同为 `NOT_FOUND` |

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `canvas/canvas_models.py` | Model Classes（Unit 文件）：类型值域 / 创建 / 更新 / 查询 / 记录 / 删除结果 / 导出载荷 + 上限常量 + 导出 MIME / 扩展名映射 | `CanvasKind` / `CanvasCreateDTO` / `CanvasUpdateDTO` / `CanvasQueryDTO` / `CanvasRecordDTO` / `CanvasDeleteResultModel` / `CanvasExportModel` / `canvas_export_media_type` / `canvas_export_extension` |
| `canvas/canvas_orm.py` | ORM 行（`ff_canvas_documents`——人格作用域 / 类型 / 内容 / 修订号） | `CanvasDocumentRow` |
| `canvas/canvas_service.py` | 服务：创建 / 列举 / 读取 / 修订 / 删除 / 导出（人格隔离 + 修订号推进 + 导出命名） | `CanvasService` |
| `app/api/canvas.py` | 管理 API（列表 / 创建 / 读取 / 修订 / 删除 / 导出；人格作用域解析 + 鉴权挂接） | `router` |
| `alembic/versions/000000000017_canvas_documents.py` | 迁移 0017（建表 + 人格索引 + 人格 × 更新时间复合索引） | — |

依赖方向：canvas → core（errors / time_utils）+ database（Base / Mixin）；API 层
→ canvas + persona（`resolve_target_persona` / `bind_persona_context`）+ core
（`ResultDTO`）。canvas 域不依赖 chat / llm / channels——产物面与会话面零耦合。

## 数据模型（`ff_canvas_documents`）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT 自增 | 主键 |
| `persona_id` | VARCHAR(64) | 人格 ID（请求上下文自动注入 + 查询自动过滤） |
| `title` | VARCHAR(120) | 标题（展示与导出文件名原料） |
| `kind` | VARCHAR(16) | 产物类型（html / svg / markdown） |
| `content` | TEXT | 自包含内容（上限 262144 字符——模型层强制） |
| `revision` | INT | 修订号（创建为 1，修改递增） |
| `created_at` / `updated_at` | BIGINT | epoch 毫秒（`updated_at` 每次 UPDATE 刷新） |

索引：`ix_ff_canvas_documents_persona_id`（人格作用域）与
`ix_ff_canvas_documents_persona_updated (persona_id, updated_at)`
（「当前人格最近修改」列表查询）。无唯一键——同名产物合法（标题是展示面，
主键与修订号才是标识）。

迁移语义：0017 单表建 / 降（降级删表，数据整体丢弃——文档化）。真引擎
（SeekDB / MySQL 方言）round-trip 由
`src/tests/integration_seekdb/test_migration_chain.py` 强制覆盖（0017 升 /
降 base / 复升）；SQLite 方言可逆性由 `test_migration_canvas.py` 覆盖。

## API 链路

```
管理台 /canvas 面板
    ↓ Bearer owner token（管理面鉴权单点挂接——与其余管理路由一致）
GET /api/v1/canvas?persona_id=&kind=&limit=
    ↓ resolve_target_persona（显式人格 / 当前主人格）
    ↓ bind_persona_context(persona.id)
    ↓ CanvasService.list_documents（更新时间倒序 + 类型过滤 + 上限）
    ↓ ResultDTO[list[CanvasRecordDTO]]
POST /api/v1/canvas
    ↓ 请求模型校验（标题 1-120 裁剪后非空 / 类型值域 / 内容非纯空白且 ≤262144）
    ↓ CanvasService.create_document（revision=1）
GET /api/v1/canvas/{id}            → 单条读取（含内容）
PATCH /api/v1/canvas/{id}          → 修订（标题 / 内容按需；revision 递增）
DELETE /api/v1/canvas/{id}         → 删除（缺失主键幂等空报告）
GET /api/v1/canvas/{id}/export     → 原始文本字节（attachment + 文件名）
```

响应形态：JSON 端点统一 `ResultDTO`；导出端点直出字节（`Content-Type` 按
类型固定，`Content-Disposition` 暴露文件名——后端 CORS 已显式
`expose_headers=["Content-Disposition"]`，前端才能读文件名）。

请求 / 响应 JSON 示例：

```json
{
  "title": "今日心情卡",
  "kind": "html",
  "content": "<div style=\"padding:24px\">今天很好</div>"
}
```

```json
{
  "id": 7,
  "title": "今日心情卡",
  "kind": "html",
  "content": "<div style=\"padding:24px\">今天很好</div>",
  "revision": 1,
  "created_at_ms": 1731000000000,
  "updated_at_ms": 1731000000000
}
```

```json
{"title": "夜间版", "content": null}
```

```json
{"deleted_ids": [7]}
```

导出响应（头部 + 字节）：

```
HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
Content-Disposition: attachment; filename="canvas-7-r2-night.html"

<div style="padding:24px">今天很好</div>
```

导出文件名规则：`canvas-<id>-r<revision>[-<slug>]<扩展名>`——标题中的
ASCII 字母数字折成 slug（中文标题退化为无 slug 段，避免乱码文件名）；扩展名
按类型（`.html` / `.svg` / `.md`）。

## 字段校验与错误码

| 失败类别 | 错误码 | HTTP | 说明 |
|---|---|---|---|
| 目标人格不存在 / 无主人格 | `NOT_FOUND` | 404 | 与其余人格作用域端点同文案口径 |
| 画布不存在 / 不属于当前人格 | `NOT_FOUND` | 404 | 删除路径例外——幂等空报告 |
| 标题纯空白 / 内容纯空白 / 更新两项皆缺 / 类型非法 | `VALIDATION_ERROR` | 400 | 请求模型层拒绝（管理面统一映射 400） |
| 标题超 120 / 内容超 262144 字符 | `VALIDATION_ERROR` | 400 | 上限与 ORM 列宽 / 模型字段一致 |

## 安全边界

- **预览沙箱**：前端 iframe 恒 `sandbox=""`（无 `allow-same-origin`、无
  `allow-scripts`）——产物里的脚本不执行、不能触达管理台 DOM / storage；
  样式与结构照常渲染。Markdown 按纯文本展示（不引入 Markdown 渲染依赖）。
- **导出**：原始文本直出；`Content-Disposition` 为 attachment（浏览器下载
  而非导航），不改变管理台页面状态。
- **人格隔离**：全部读写经 `PersonaMixin` 上下文过滤，覆盖跨人格读取 /
  修订 / 删除 / 导出（均 404 或空报告）。
- **内容不参与提示词**：产物不进 compose 链——模型上下文零污染（接
  `render_canvas` 工具后再评估注入面）。

## 前端面板（`/canvas`）

| 能力 | 实现 |
|---|---|
| 列表 | 人格选择器 + 类型过滤；卡片展示标题 / 类型徽章 / 修订号 / 更新时间 |
| 预览 | HTML / SVG：沙箱 iframe（`srcDoc`）；Markdown：等宽文本块 |
| 创建 | 标题（≤120）/ 类型 / 内容（≤262144）——前端同步提示，后端模型层强校验 |
| 修订 | 卡片内联编辑（标题 / 内容）→ PATCH；成功后提示新修订号 |
| 导出 | 经带 Bearer 的请求取字节 → 对象 URL 下载 → 立即释放；文件名取服务端响应头 |
| 删除 | 确认对话框 → DELETE；缺失主键提示「已不存在」 |

类型 / API / hooks / msw / 页面测试见「测试面」。

## 测试面

| 测试文件 | 覆盖 |
|---|---|
| `tests/unit/test_canvas_service.py` | 创建（revision=1 / 标题裁剪 / 内容逐字节保留）/ 列举（倒序 / 类型过滤 / 上限）/ 读取 / 修订（递增 / 部分更新）/ 删除（幂等）/ 导出（文件名 / MIME / 修订号）/ 人格隔离 / 模型边界 |
| `tests/unit/test_canvas_api.py` | ASGITransport 全操作面（创建 / 列表过滤 / 读取 / 修订 / 删除 / 导出头与字节）/ 跨人格隔离 404 / 人格解析 / 请求边界 400 |
| `tests/unit/test_migration_canvas.py` | 0017 SQLite 升 / 降 / 复升 + 列 / 索引 / 必填列约束 |
| `src/tests/integration_seekdb/test_migration_chain.py` | 0017 真引擎迁移链 round-trip 与列断言 |
| 前端 `src/api/canvas.test.ts` / `src/hooks/useCanvasApi.test.ts` / `src/pages/canvas/CanvasPage.test.tsx` | 端点查询串与载荷 / 失败归一 / 缓存失效 / 面板全操作（预览 / 创建 / 修订 / 导出下载 / 删除 / 错误面） |
| 契约门 | `CanvasCreateDTO` / `CanvasUpdateDTO` / `CanvasQueryDTO` / `CanvasRecordDTO` / `CanvasDeleteResultModel` 双树 schema fixture 比对 |

零真实网络（后端 fake / ASGITransport；前端 msw）；全部 `async def test_*`（后端）。

## 扩展路径

1. **对话内产出**：把 `CanvasService.create_document` 包成 chat 工具
   （`render_canvas`）——工具闭包持有会话主键与人格上下文，产物创建后可在
   回复里附上导出链接 / `file` 附件。
2. **通道投递**：`export_document` 的产物可直接转
   `ChannelMessageAttachment(kind=file, base64=...)`——三通道均支持 `file`
   类（协议 v2 映射表已就绪）。
3. **历史版本**：当前只保留最新修订（`revision` 仅作命名与展示）；需要
   版本回滚时按核心记忆修订表的 append-only 形态增修订表，不改变现有列。
