# reminders — 持久化提醒

`backend/src/packages/harness/reminders/` 承载**跨重启存活的定时提醒**：主人预约一条提醒
（标题 + 到期时刻），落库到 `ff_reminders`（PersonaMixin 人格作用域），由中央调度器的
扫描作业在到期后经属主通道（Telegram 适配器——与 duty 属主投递同一发送面）投递到
属主会话；投递尝试次数与失败原因在库，进程重启不丢状态。

## 模块一览

| 模块 | 职责 | 关键导出 |
|---|---|---|
| `models.py` | 值域枚举（状态 / 来源）+ 创建 / 记录 / 查询 / 扫描结果 / 批量取消 DTO + 出站发送面协议（Unit 文件） | `ReminderStatus` / `ReminderSource` / `ReminderCreateModel` / `ReminderRecordDTO` / `ReminderQueryDTO` / `ReminderScanResultModel` / `ReminderBatchCancel*` / `OutboundSenderFace` |
| `orm.py` | 提醒表 ORM（`ff_reminders`——PersonaMixin + 到期 / 复合扫描索引） | `ReminderRow` |
| `service.py` | CRUD / 取消 / 批量取消（部分成功）/ 到期扫描 / 投递终态回写（成功 / 失败计数与上限） | `ReminderService` / `DELIVERY_FAILURE_LIMIT` / `SCAN_BATCH_LIMIT` |
| `job.py` | 调度作业工厂（主人格解析 → 扫描 → 属主通道投递 → 终态回写） | `build_reminder_scan_job` / `REMINDER_SCAN_INTERVAL_SECONDS` / `REMINDER_MESSAGE_TEMPLATE` |
| `app/api/reminders.py` | 管理 API（创建 / 列表 / 读取 / 取消 / 批量取消） | `router` / `ReminderService` 实例 |

依赖方向：reminders → channels（出站消息模型 / 路由器 Telegram 适配器读取面）+ persona
（主人格解析 / 人格上下文绑定）+ core（errors / dtos / time_utils）+ database（Base
Mixins）。job 另依赖 duty 的公共发送面解析 helper（`resolve_telegram_send_face`——
duty / reminders 共用同一 Telegram 适配器读取面，不复制第三份）。

## 链路顺序（到期投递——锁定语义）

```
中央调度器（app/main.py _startup_scheduler——telegram 启用 + 属主会话配置时注册
  register_interval("reminders", ..., every_seconds=60.0)）
  ↓ 作业轮次：get_primary_persona（无主人格 → 日志跳过，正常返回）
  ↓ bind_persona_context(persona.id)（扫描在单一人格作用域内进行）
  ↓ scan_due_reminders(now_ms)                        pending 且 due_at_ms <= now
  ↓   语句带 FOR UPDATE SKIP LOCKED（MySQL 系并发抢占不重叠；SQLite 忽略——
  ↓   单写者语义天然串行）+ 事务结束即释放行锁（投递在事务之外）
  ↓ 发送面解析：ChannelRouter.get_adapter(TELEGRAM)
  ↓   ├─ 未装配路由器 / 未注册适配器 / 属主会话未配置 → 提醒【保持 pending】
  ↓   │   （不消耗失败次数——通道恢复后下一轮扫描补投）
  ↓   └─ 逐条 send_message(OutboundMessageModel(chat_id=属主, text="【提醒】{title}"))
  ↓        ├─ 成功 → 新事务 mark_delivered（status=delivered + delivered_at_ms）
  ↓        └─ 异常 → 新事务 mark_failed（attempts++；≥3 次转 failed + last_error）
```

拒绝语义（锁定决策，逐条）：

- **无通道 = 保持 pending**：提醒不丢、不消耗失败次数，通道接通的下一轮扫描补投；
- **至少一次投递**：投递成功与终态回写之间存在进程崩溃窗口——行保持 pending，下轮
  重投（重复投递优于漏投，文档化代价）；
- **失败上限**：连续失败 3 次后转 `failed`（不再重试，避免坏通道无限打点）；
- **作业异常隔离**：人格解析 / 扫描链路残余异常由调度器 per-job 隔离兜底（日志，
  调度循环不受影响）；
- **装配门禁**：无投递出口（Telegram 未启用 / 属主会话未配置）时作业不注册——
  提醒行保持 pending（管理面可查询 / 取消），通道启用后作业随调度器装配自然生效。

## 表结构

### ff_reminders

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT | PK 自增 | 主键 |
| `persona_id` | VARCHAR(64) | NOT NULL，索引 | 人格 ID（PersonaMixin——写路径自动注入 / 读路径自动过滤） |
| `title` | VARCHAR(200) | NOT NULL | 提醒标题（投递文案正文；服务层裁剪后 1-200 校验） |
| `due_at_ms` | BIGINT | NOT NULL，索引 | 到期时刻（epoch 毫秒——扫描判定轴） |
| `status` | VARCHAR(16) | NOT NULL | `pending` / `delivered` / `cancelled` / `failed`（单向推进，无回退） |
| `delivery_attempts` | INT | NOT NULL，默认 0 | 投递失败累计次数（达 3 转 failed） |
| `last_error` | VARCHAR(512) | NULL | 最近一次投递失败原因（落库截断 512 字符） |
| `delivered_at_ms` | BIGINT | NULL | 投递成功时刻（NULL = 未投递） |
| `source` | VARCHAR(16) | NOT NULL | `manual`（管理面）/ `chat`（对话捕获——观测面） |
| `meta` | JSON | NULL | 来源附加信息（不参与业务判定） |
| `created_at` / `updated_at` | BIGINT | NOT NULL | 时间戳（epoch 毫秒） |

索引：`ix_ff_reminders_persona_id`（人格过滤）、`ix_ff_reminders_due_at_ms`（到期判定）、
`ix_ff_reminders_persona_status_due`（复合——扫描主查询形态，顺序与 WHERE / ORDER BY
一致）。迁移：`alembic/versions/000000000011_add_reminders.py`（升 / 降级 + 真引擎
round-trip 见 `src/tests/integration_seekdb/test_migration_chain.py`）。

## 管理 API 面（app/api/reminders.py）

前缀 `/api/v1/reminders`（受保护管理路由——`require_admin_token`）；全部响应经
`ResultDTO` 封装，分页经 `PageResponse`，DB Session 经 `get_async_db` 注入（事务边界
归服务层）。人格作用域：`persona_id` 查询参数显式指定（缺省 = 当前主人格；无主人格
404 fail loud）；他人格提醒不可见（同样 404——隔离语义）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/reminders` | 创建（body `{title, due_at_ms, source?, meta?}`；到期允许过去——创建即到期的补投语义） |
| GET | `/api/v1/reminders` | 游标分页列表（`status` 过滤；id 降序——新提醒在前） |
| GET | `/api/v1/reminders/{id}` | 读取（管理面观测） |
| POST | `/api/v1/reminders/{id}/cancel` | 取消（仅 pending；其余状态 422 `VALIDATION_ERROR`） |
| POST | `/api/v1/reminders/batch-cancel` | 批量取消（body `{ids}` 1-100 且不重复；**部分成功形态**——逐 id 独立判定，返回 `{total, succeeded, failed, items}`） |

请求示例（创建）::

    POST /api/v1/reminders?persona_id=1
    {"title": "起来喝水", "due_at_ms": 1731000600000, "source": "manual"}

响应（创建）::

    {
        "success": true,
        "code": "200",
        "message": "提醒已创建",
        "data": {
            "id": 1,
            "persona_id": "1",
            "title": "起来喝水",
            "due_at_ms": 1731000600000,
            "status": "pending",
            "delivery_attempts": 0,
            "last_error": null,
            "delivered_at_ms": null,
            "source": "manual",
            "meta": null,
            "created_at": 1731000000000,
            "updated_at": 1731000000000
        }
    }

批量取消响应（部分成功——逐 id 结果与提交顺序一致）::

    {
        "success": true,
        "code": "200",
        "message": "批量取消已完成",
        "data": {
            "total": 2,
            "succeeded": 1,
            "failed": 1,
            "items": [
                {"id": 3, "success": true, "code": null, "message": "已取消"},
                {"id": 999, "success": false, "code": "NOT_FOUND", "message": "提醒不存在（或不属于当前人格）"}
            ]
        }
    }

## 配置面

**零新增 env 键**：扫描间隔是调度器作业注册参数常量
（`REMINDER_SCAN_INTERVAL_SECONDS = 60.0`——分钟级提醒精度），投递出口复用既有
`TELEGRAM_ENABLED` / `TELEGRAM_OWNER_CHAT_ID`。作业注册门禁 = Telegram 通道启用 +
属主会话已配置（无出口时作业不注册——见链路顺序装配门禁）。

## 前端消费面

`frontend/src/pages/memories/RemindersTab.tsx`（记忆管理模块「提醒」页签）：状态过滤
（pending / delivered / cancelled / failed）+ 创建对话框（标题 + `datetime-local` 到期
本地时间）+ 取消（仅 pending 可点——确认对话框）+ 批量取消（部分成功结果对话框复用
`BatchDeleteResultDialog`）。类型 / 端点 / hooks 见 `frontend/src/types/reminders.ts` /
`src/api/reminders.ts` / `src/hooks/useRemindersApi.ts`。

## 测试与门禁

- `src/tests/unit/test_reminders_service.py`（CRUD / 扫描过滤 / 批量部分成功 / 失败上限 /
  错误截断 / 人格隔离 / 批量请求唯一性）；
- `src/tests/unit/test_reminders_api.py`（HTTP 面：创建 / 列表 / 取消状态守卫 / 批量部分
  成功 / 人格解析与隔离 / 边界 422）；
- `src/tests/unit/test_reminders_job.py`（作业：投递成功回写 / 无适配器 / 无路由器 /
  无属主会话保持 pending / 发送异常达上限转 failed / 无主人格跳过）；
- `src/tests/unit/test_app_reminders_lifespan.py`（装配：仅 reminders 作业注册 +
  投递面门禁 + 停机复位）；
- `src/tests/unit/test_migration_reminders.py`（SQLite 升 / 降 / 复升 + 列 / 索引 /
  NOT NULL 守卫）；
- `src/tests/integration_seekdb/test_migration_chain.py`（真引擎 0001→0011 全链
  round-trip + `ff_reminders` 列断言）。
- 全部 `async def test_`；共享会话级 SQLite fixture；HTTP 面 ASGITransport 零真实网络；
  作业面 fake 适配器发送面 + 注入时钟（零真实等待）。
