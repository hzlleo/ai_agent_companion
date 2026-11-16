# 工具沙盒（tool sandbox）— 隔离工具运行时（P3 chunk 3 交付）

`docker/tool-sandbox-agent/`（独立 pixi 项目 + 加固镜像——沙盒树）+ `backend/src/packages/harness/tools/sandbox_models.py` / `sandbox.py`（后端侧客户端 + 注册接线）+ `docker/docker-compose.yml`（部署编排），共同构成「执行器无关工具的隔离执行面」：后端把**必须隔离运行的工具**（当前 `http_fetch`；P4 平台工具按同一范式接入）代理到沙盒进程执行。

## 为什么隔离（架构决策）

| 决策 | 内容 | 理由 |
|---|---|---|
| 两树零代码共享 | 沙盒是独立 pixi 项目（`docker/tool-sandbox-agent/`），**不 import 后端任何模块**；后端也不 import 沙盒树 | 工具执行面是攻击面（网络出站 / 未来平台写入）——与后端进程物理隔离；后端依赖演进（数据库 / LLM 栈）不裹挟沙盒；沙盒镜像最小化（fastapi / uvicorn / pydantic 三依赖） |
| wire 契约人为镜像 | `InvokeRequestModel` / `InvokeResponseModel` / `HealthResponseModel` 在两树各持一份 **byte 级一致**的类定义（沙盒 `tool_sandbox/wire_models.py` ↔ 后端 `harness/tools/sandbox_models.py`） | 不抽公共包（共享即耦合）；契约漂移由仓库根漂移门禁脚本对两份定义做文本比对看护（后端侧职责——`修改任一侧必须同步另一侧`） |
| 内置工具永不经沙盒 | `get_current_time` / `search_memory` 留在进程内（P3 chunk 2 交付面不动） | 时钟 / 记忆检索需要 DB 与人格上下文（contextvar 作用域）；零外部副作用的本就不需要隔离——沙盒只承载**执行器无关**的隔离工具 |
| 沙盒是可选运行时 | `Settings.tools_sandbox.enabled=False`（默认）；全工程无沙盒照常运行 | 零配置开发可用、测试 hermetic；duty 工具面在沙盒不可达时回落到内置工具（warn-and-continue） |
| 共享 httpx 池出站 | 后端侧 `ToolSandboxClient` 经 `pools.PoolManager` 下发的共享 `httpx.AsyncClient` 调用沙盒 | 硬规则：无私建连接（与 llm / channels 域同约定） |

## 链路顺序（duty → … → 工具——顺序锁定）

```
duty 轮 tool_calls（DutyService 工具循环）
    ↓ registry.execute(name, arguments)        harness/tools/registry（隔离 + 计时）
    ↓ 代理执行器                                harness/tools/sandbox.register_sandbox_tool 注册的闭包
    ↓ ToolSandboxClient.invoke                  harness/tools/sandbox（POST /invoke + Bearer；恒不抛）
    ↓ 共享 httpx 池 → http://127.0.0.1:8765     docker/tool-sandbox-agent（compose 部署）
    ↓ ASGI /invoke（token 恒时比较）            沙盒树 server.py
    ↓ SandboxToolRuntime.invoke                 沙盒树 runtime.py（寻址 → 参数校验 → 执行 → 计时；恒不抛）
    ↓ 工具执行器                                沙盒树 http_fetch.py（域名允许清单 + 8KB 截断）
```

失败语义（三层恒不抛）：客户端层——token 缺失 / 沙盒不可达 / 非成功状态 / wire 破坏 → `ToolResultModel(ok=False)`；代理层——客户端 ok=False 转 `INTERNAL_ERROR` 业务异常；registry 层——异常归一 ok=False（既有隔离面）。**duty 工具循环恒不见异常。**

## wire 契约（两树镜像）

| 端点 | 载荷 | 鉴权 |
|---|---|---|
| `GET /health` | `HealthResponseModel`：`{"status": "ok"}` | 无（存活面——HEALTHCHECK / 启动探测） |
| `POST /invoke` | `InvokeRequestModel`：`{"tool": str, "arguments": {}}` → `InvokeResponseModel`：`{"ok": bool, "result": {}, "error": str|null, "latency_ms": int}` | Bearer token |

- 端点**恒 200** 携带成败终态（工具失败在载荷 `ok=False` 表达）；非 200 = 401（鉴权失败）/ 422（请求形态非法）——客户端按传输层失败归一；
- `InvokeRequestModel` 为 `extra=forbid` 严格面；`latency_ms` 为沙盒侧执行时延（epoch 毫秒差）。

## 鉴权（安全红线）

- token 经环境变量**间接**（`Settings.tools_sandbox.token_env`，默认 `SANDBOX_TOKEN`）——密钥永不落 yaml / 配置树（与 llm `api_key_env` / telegram `bot_token_env` 同模式）；
- 沙盒侧比较为**恒时**（`secrets.compare_digest`——防时序侧信道）；全部鉴权失败统一 401 + 统一 detail（不区分头缺失 / 形态错误 / 不匹配——无错误面 oracle）；
- 沙盒侧 `SANDBOX_TOKEN` 缺失 / 为空 → app 工厂 fail fast（安全边界无 token 拒绝启动）；
- token 永不入日志 / 错误响应（两树同红线）；
- 端口仅回环暴露（compose `127.0.0.1:8765:8765`）。

## 示例工具 `http_fetch`（P4 平台工具的落地范式）

| 项 | 值 |
|---|---|
| 参数 | `url`（http/https + 主机名，必填，≤2048）、`timeout_seconds`（1-30，默认 10） |
| 域名允许清单 | env `SANDBOX_HTTP_ALLOWLIST`（逗号分隔域名后缀）；**空 = 工具禁用 / 默认拒绝**——零配置部署永不产生出站流量 |
| 匹配语义 | 点边界后缀匹配：`api.example.com` 命中 `example.com`；`evil-example.com` / `evil.com`（对条目 `vil.com`）**不**命中 |
| 重定向 | 30x **逐跳**过允许清单（自定义 `HTTPRedirectHandler`——防经重定向跳出清单）+ 终态 URL 复检 |
| 正文 | 按字节截断 8192（8KB）；字符集取响应头（回退 utf-8）；解码 `errors="replace"` |
| 抓取实现 | stdlib urllib 经 `asyncio.to_thread` 下沉（沙盒树零 httpx 依赖） |
| 失败 | 全部归一受控失败（`SandboxToolError` → `ok=False` 终态）——执行器对运行时恒不抛未受控异常 |

参数面双树各持一份（后端 `HttpFetchArgsModel` 用于 registry 本地校验 + 默认值物化（fail-fast，免网络往返）；沙盒侧为权威执行面——双侧校验为**有意冗余**，改任一侧须同步另一侧）。

## 模块一览（两侧）

| 树 | 模块 | 职责 |
|---|---|---|
| 沙盒 | `server.py` | ASGI 入口薄层（app 工厂 + uvicorn main + token 鉴权依赖） |
| 沙盒 | `tool_sandbox/wire_models.py` | wire 契约 Model Classes（镜像源） |
| 沙盒 | `tool_sandbox/runtime.py` | 沙盒工具运行时（注册 / 隔离执行 / 计时——镜像后端 ToolRegistry 语义） |
| 沙盒 | `tool_sandbox/http_fetch.py` | http_fetch 工具（允许清单 / 重定向校验 / 8KB 截断 / to_thread 抓取） |
| 后端 | `harness/tools/sandbox_models.py` | Unit 文件：wire 契约（镜像副本）+ `SandboxSettingsFace` / `SandboxClientFace` 协议 + `HttpFetchArgsModel` + `build_http_fetch_spec` |
| 后端 | `harness/tools/sandbox.py` | `ToolSandboxClient`（共享池 + Bearer + 恒不抛）+ `build_sandbox_client` + `register_sandbox_tool`（registry 代理注册） |
| 后端 | `app/main.py` `_register_sandbox_tools` | 启动接线（enabled → token 检查 → /health 探测 → 注册 http_fetch） |

依赖方向：tools 域内部（sandbox → sandbox_models / registry / tool_models）+ core（errors / time_utils）；沙盒树零后端依赖。

## Settings（`Settings.tools_sandbox`）

| 字段 | env 变量 | 默认 | 说明 |
|---|---|---|---|
| `tools_sandbox.enabled` | `TOOLS_SANDBOX_ENABLED` | `false` | 是否启用工具沙盒（false = 不探测 / 不注册沙盒工具） |
| `tools_sandbox.base_url` | `TOOLS_SANDBOX_BASE_URL` | `http://127.0.0.1:8765` | 沙盒服务基础 URL |
| `tools_sandbox.token_env` | `TOOLS_SANDBOX_TOKEN_ENV` | `SANDBOX_TOKEN` | 鉴权 token 环境变量名（间接模式） |
| `tools_sandbox.request_timeout_seconds` | `TOOLS_SANDBOX_REQUEST_TIMEOUT_SECONDS` | `15` | 单次请求超时（invoke / 健康探测共用——启动探测的 bounded 上限） |

沙盒容器内 env：`SANDBOX_TOKEN`（必填）、`SANDBOX_HTTP_ALLOWLIST`（可选，空 = http_fetch 禁用）、`SANDBOX_HOST` / `SANDBOX_PORT`（缺省 `0.0.0.0:8765`——容器形态）。

> 注：`backend/.env.example` 分节登记属根 scripts/ 生成器（主 agent 面）——本 chunk 新增 env 键面以本表 + `src/conftest.py` 硬覆盖清单为准（`TOOLS_SANDBOX_*` / `SANDBOX_TOKEN` / `SANDBOX_TEST_TOKEN`）。

## app 装配（`app/main.py`——顺序锁定）

```
_startup_scheduler（duty / dream 任一启用时）
    ↓ duty 分支：ToolRegistry + register_builtin_tools（内置工具面）
    ↓ _register_sandbox_tools(app, settings, registry)：
        ↓ TOOLS_SANDBOX_ENABLED=false → no-op（info 日志）
        ↓ build_sandbox_client(manager.get_http_client(), settings.tools_sandbox)
        ↓ token 未配置（token_env 变量为空）→ 跳过 + 告警（warn-and-continue）
        ↓ probe_health() 失败（bounded 超时 request_timeout_seconds）→ 跳过 + 告警
        ↓ register_sandbox_tool(registry, client, build_http_fetch_spec())
    ↓ DutyService（工具面此刻已含 http_fetch——list_specs 进入 LLM 请求）
```

装配矩阵：

| duty | tools_sandbox | 探测 / token | duty 工具面 |
|---|---|---|---|
| off | 任意 | ——（零注册表——沙盒消费面是 duty 工具循环） | 无 |
| on | off | —— | 内置工具 |
| on | on | 失败 / token 缺失 | 内置工具（告警） |
| on | on | 成功 | 内置工具 + `http_fetch` |

## 部署（compose——后端不进 compose）

```bash
cd docker
export SANDBOX_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export SANDBOX_HTTP_ALLOWLIST="example.com,api.github.com"
docker compose up -d tool-sandbox-agent   # 127.0.0.1:8765 → 容器 8765
```

- 后端经 pixi 运行（`cd backend && pixi run …`），经 `http://127.0.0.1:8765` 消费沙盒——compose 只编排沙盒；
- 加固面横跨 Dockerfile 与 compose：tini（PID 1）/ 非 root（uid 1001）/ HEALTHCHECK（stdlib urllib 探测 /health）/ `cap_drop: ALL` / `no-new-privileges` / `read_only` + tmpfs `/tmp` / 资源限额 mem 256m + cpus 0.5 / restart unless-stopped / 端口仅回环；
- 网络为 compose 项目内专用桥接网（**不设 `internal: true`**——http_fetch 需要受控出站 egress；出站管控面是允许清单而非网络层全断）；
- 沙盒树本地开发 / 测试：`cd docker/tool-sandbox-agent && pixi install && pixi run tests-unit`（hermetic——零真实网络）。

## P4 扩展路径

1. 平台工具（读取类优先）按 `http_fetch` 范式接入沙盒树：工厂函数 + 参数 Model Class + `SandboxToolError` 受控失败 + `asyncio.to_thread` 阻塞下沉；
2. 后端侧为每个新工具镜像规格（`build_<tool>_spec()`——名称 / 描述 / schema / args_model）+ `register_sandbox_tool` 装配；
3. 写入类平台工具属 P4 safety 审批面（须过审批红线后才入沙盒——本 chunk 不预设）；
4. wire 契约若需演进（新端点 / 字段）：两树同步改 + 漂移门禁基线同步。

## 覆盖率与测试面

`harness/tools` 分组（含 sandbox 两个新文件）≥80%（coverage-gates 门禁）；沙盒树 `pixi run tests-unit` 覆盖率阈值 80（`--cov-fail-under`；逐模块读数经 term-missing 报告核对）。

| 测试文件（后端） | 覆盖 |
|---|---|
| `tests/unit/test_tools_sandbox_client.py` | MockTransport 客户端：token 间接 / 鉴权头形状 / ok-error 映射 / 传输异常 / 超时 / 非成功状态 / wire 破坏 / 健康探测矩阵 / 工厂 face 构造 |
| `tests/unit/test_tools_sandbox_registry.py` | 代理回路（fake 客户端 ok / 失败归一）/ 本地校验 fail-fast / 默认值物化经 wire / 规格呈现面 / HttpFetchArgsModel 校验矩阵 / wire 模型基础面 |
| `tests/unit/test_tools_sandbox_settings.py` | env 面映射 / 默认值 / 校验 / 聚合 + 隔离构造 / conftest 硬覆盖锚点 |
| `tests/unit/test_app_sandbox_lifespan.py` | 装配矩阵（disabled / healthy / unhealthy / token 缺失 / duty 关闭）+ 工厂接线参数 + 代理回路 |

| 测试文件（沙盒树） | 覆盖 |
|---|---|
| `src/tests/unit/test_wire_models.py` | wire 契约模型校验面 |
| `src/tests/unit/test_runtime.py` | 运行时四类隔离 + 计时（注入时钟）+ 默认值物化 |
| `src/tests/unit/test_http_fetch.py` | 允许清单矩阵 / URL 校验 / 重定向逐跳 / 抓取（monkeypatch opener）/ 执行器注入回路 |
| `src/tests/unit/test_server.py` | ASGI 手写调用：鉴权矩阵 / 端点面 / 422 / 默认运行时全链 / fail-fast / 恒时比较 / 绑定解析 |
