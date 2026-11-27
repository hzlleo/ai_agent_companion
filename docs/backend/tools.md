# 外部工具扩展（extensions_config.json → ToolRegistry）

管理员可在本地 `extensions_config.json` 中声明 HTTP 工具，启动时装配进
`ToolRegistry`，由 duty / chat 工具循环经 LLM function calling 调用。
工具的执行面在 `packages/harness/tools/extension_tools.py`（执行器）与
`extension_registry.py`（装配），配置模型在 `config/models.py`。

## 配置文件与开关

- 路径：`backend/extensions_config.json`（由 `pixi run setup` 从
  `extensions_config.example.json` 复制；运行时由 `config/loader.py` 与
  `config.yaml` 同一 `config_dir` 加载）。
- 总开关：`enabled`（默认 `false`）。文件缺失、`enabled=false` 或
  `tools=[]` 时**零注册**，默认部署不产生该面的日志噪声。
- 同名覆盖：`allow_override_builtin`（默认 `false`）。为 `false` 时，扩展
  工具与任何已注册工具（内置 `get_current_time` / `search_memory`、沙盒 /
  浏览器 / 搜索工具）同名即拒绝注册并记 warning；置 `true` 才放行覆盖。
- 示例数据单一真相源：`ExtensionsConfig.default_tools()`；
  `pixi run gen-examples` 重新生成 `extensions_config.example.json`。

## 字段表

`ExtensionsConfig` 顶层：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 外部工具扩展总开关 |
| `allow_override_builtin` | bool | `false` | 允许扩展工具覆盖已注册工具 |
| `tools` | list[ExtensionTool] | `[]` | 工具声明列表 |

`ExtensionTool`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 工具名称（唯一标识，进入 LLM 工具面；`^[A-Za-z0-9_-]{1,64}$`） |
| `description` | str | 工具描述（模型调用时机说明） |
| `parameters` | JSON Schema | 工具参数 schema；执行前按此 schema 校验并物化默认值（执行面见下节） |
| `kind` | `"http"` | 执行面类型（当前仅 HTTP） |
| `http` | HttpToolTargetModel | 执行目标（`kind=http` 时必填） |
| `expose_to_peer` | bool | 默认 `false`：仅 owner 回合可见；`true` 才对群聊 peer / 平台 untrusted 回合暴露 |

`HttpToolTargetModel`：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `url` | str | — | 目标地址（http/https；可含 `{参数名}` 占位；**不支持** `{env:}`——密钥请走 header） |
| `method` | `GET` / `POST` | `POST` | POST 发送 JSON body=参数；GET 发送未占位参数为查询串 |
| `headers` | mapping[str, str] | `{}` | 请求头；值可含 `{env:VAR_NAME}` 占位（密钥只存变量名） |
| `timeout_ms` | int | `5000` | 单次请求超时（上限 `30000`） |
| `response_max_bytes` | int | `65536` | 响应体字节上限（流式读取累计超限即中止并受控失败） |
| `result_path` | str | `""` | 从响应 JSON 提取结果的点路径（空 = 整个文档） |

## 参数 JSON Schema 执行面

`parameters` 在构造期编译为参数校验模型，执行前按下列**实际执行面**校验；
不在清单内的关键字不生效（声明了也不会拒绝越界值）：

- 支持：`type`（严格类型——`string` / `integer` / `number` / `boolean` /
  `array` / `object`，**不做字符串强转**，`"2"` 不满足 `integer`、`1` 不满足
  `boolean`）、`required`、`enum`、`pattern`、`minimum` / `maximum`、
  `minLength` / `maxLength`、`items`（数组元素类型与元素级上述约束）、
  `minItems` / `maxItems`、`additionalProperties`（`false` = 拒绝额外键）。
- 不支持：嵌套 `object` 内部属性的约束（`object` 只校验为映射）、`format`、
  `oneOf` / `anyOf` / `allOf`、`const`、`$ref`。

## 密钥规则

- header 值以 `{env:VAR_NAME}` 引用进程环境变量；构造期缺失即装配失败。
- 占位剔除后的剩余字面仍按密钥形态检查（大小写不敏感）：`Bearer ` /
  `Basic ` / `sk-` 前缀（后跟非空字面）或剩余字面长度 > 32 → 拒绝。
- URL **不支持** `{env:}`；URL 禁止 userinfo（`http://user:pass@host`）。
  查询串中的常见密钥参数名（`key` / `token` / `apikey` / `api_key` /
  `api-key` / `secret` / `access_token` / `auth_token`）先剔除 `{...}`
  参数占位，剩余字面 ≥16 字符 → 拒绝，密钥必须改走 header 的 `{env:}`
  占位（`?access_token={user_access_token}` 合法）。

## 装配与可见性

聊天通道与 duty 两个工具面在启动期共用同一装配入口：总开关开启时，扩展
工具同时进入 duty 注册表与通道 chat 注册表（chat 装配不依赖
`DUTY_ENABLED`——只关 duty 时聊天工具面照常携带扩展）。

可见性默认 **owner-only**：`expose_to_peer=false` 的扩展工具名交给
`ChatService`，在非 owner 回合（群聊 peer / 平台 untrusted）从工具列表
结构性剔除；执行面由服务端按本轮可执行名集拦截，不依赖模型是否看到——
模型即使回传被剔除的名字也不进入执行器，反馈与「未注册工具」走同一
受控路径（不区分「存在但本轮不可见」与「不存在」，隐藏工具名不进入
模型上下文）。`expose_to_peer=true` 才在 peer 回合可见并可执行。内置
`edit_core_memory` 恒为 owner-only（不受扩展配置影响）。

## 完整示例

```json
{
  "enabled": true,
  "allow_override_builtin": false,
  "tools": [
    {
      "name": "query_local_calendar",
      "description": "查询本地日历服务在指定日期的日程事件。",
      "parameters": {
        "type": "object",
        "properties": {
          "date": {"type": "string", "description": "日期（YYYY-MM-DD）"}
        },
        "required": ["date"],
        "additionalProperties": false
      },
      "kind": "http",
      "http": {
        "url": "http://127.0.0.1:8091/calendar/query",
        "method": "POST",
        "headers": {"X-Auth-Token": "{env:CALENDAR_API_TOKEN}"},
        "timeout_ms": 5000,
        "response_max_bytes": 65536,
        "result_path": "events"
      }
    }
  ]
}
```

## 占位与密钥规则

- **参数占位**：`url` 与 `headers` 值中的 `{参数名}` 在执行期替换为调用
  参数。`url` 占位值做 URL 编码（`/` 也编码）；占位引用的参数必须在
  `parameters.properties` 中声明，否则装配期拒绝该工具。
- **header 换行**：header 占位替换值含 `\r` / `\n` 时受控失败，防止头
  注入。
- **密钥只存 env 名**：header 值用 `{env:VAR_NAME}` 引用环境变量，构造期
  解析；变量缺失时该工具装配失败并被跳过（warn-and-continue），其它工具
  照常注册。模型层拒绝直接写入 `Bearer …`、`sk-…` 或长度超过 32 的
  字面值——长密钥必须走 env 占位。
- **method 与 body**：POST 的 JSON body 为调用参数；GET 仅把未在 URL 占位
  中消费的参数放入查询串。

## 信任边界

扩展端点**由管理员在本地 `extensions_config.json` 中定义**，装配层不做
私网地址拦截（允许 `127.0.0.1` 等本地服务）；信任边界就是该文件本身。
运行时仍强制：协议白名单（`HttpToolTargetModel` 仅接受 http/https）、
请求超时上限、响应字节上限、**禁重定向**（`follow_redirects=False`——端点
不能把请求带向配置之外的地址）。若需限制出站目标，应在管理员配置层
只填受控端点，而不是期待本层做 SSRF 判定。

## 错误语义

工具失败**永不抛出到对话层**：传输失败 / 超时 / 非 2xx / 响应超限 /
非 JSON / `result_path` 未命中均转为受控中文错误，经 `ToolRegistry`
归一为 `ok=False`，由 LLM 自行决定后续动作（与内置 / 沙盒工具一致）。
非 2xx 错误文本包含状态码与截断后的响应片段（不含响应头）。

## 与内置工具的优先级

装配顺序在所有内置 / 平台工具之后；默认 `allow_override_builtin=false`
时扩展工具不能覆盖已注册工具。装配结果记录注册数与跳过数（
`ExtensionRegistrationReportModel`），跳过原因进入 warning 日志。
