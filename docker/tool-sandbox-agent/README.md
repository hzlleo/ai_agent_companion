# tool-sandbox-agent — 工具沙盒代理

执行器无关工具的**隔离运行时**：后端把「必须隔离执行的工具」（当前
`http_fetch` + B 站只读工具组 `bili_*`；P4 平台工具按同一范式接入）代理
到本沙盒进程执行，与后端**零代码共享**——唯一的耦合点是人为镜像的
wire 契约 Model Classes（`src/tool_sandbox/wire_models.py` ↔ 后端
`harness/tools/sandbox_models.py`，两侧类定义块 byte 级一致，契约漂移由
仓库根漂移门禁看护）。

架构与完整链路文档见 `docs/backend/tools-sandbox.md`（后端侧建档）。

## 目录布局（文档化决策）

```
docker/tool-sandbox-agent/
├── pixi.toml          # 独立 pixi 项目（Python 3.12 + dev 工具 + tasks）
├── pyproject.toml     # 依赖单一数据源（运行时 fastapi/uvicorn/pydantic + dev extras）
├── server.py          # ASGI 入口薄层（app 工厂 + uvicorn main + token 鉴权）
├── src/tool_sandbox/  # 常规包：wire_models / runtime / http_fetch / bili_tools
├── src/tests/unit/    # 单元测试（镜像后端测试布局——全部 async def test_）
├── Dockerfile         # 加固镜像（tini / 非 root / HEALTHCHECK）
└── README.md          # 本文件
```

与后端子项目的布局差异（有意为之，均为文档化决策）：

- `tool_sandbox` 为**常规包**（含 `__init__.py`）——本树是独立分发单元，
  无多项目命名空间拼接需求，取最简单正确形态；
- 日志走 **stdlib logging**（后端栈的 loguru 不入本树——最小依赖面）；
- server 测试经**手写 ASGI 调用**（scope/receive/send 直驱）——本树零
  httpx 依赖（锁定决策：HTTP 抓取经 stdlib urllib；不因测试引入 httpx）。

## 快速开始（开发 / 测试）

```bash
cd docker/tool-sandbox-agent
pixi install          # 安装 default 环境（runtime + dev 工具）
pixi run tests-unit   # 单元测试（hermetic——零真实网络；覆盖率 ≥80 enforce）
pixi run lint         # ruff 静态检查
pixi run typecheck    # mypy 严格模式
```

## 运行（本地裸跑）

```bash
export SANDBOX_TOKEN="<随机长 token>"          # 必填——缺失即拒绝启动
export SANDBOX_HTTP_ALLOWLIST="example.com,api.github.com"  # http_fetch 域名允许清单
export SANDBOX_HOST=127.0.0.1                  # 裸跑建议显式回环（缺省 0.0.0.0 为容器形态）
pixi run python server.py                     # 监听 0.0.0.0:8765（缺省；SANDBOX_PORT 可覆写）
```

存活探测：`GET /health`（无鉴权）→ `{"status": "ok"}`。

## 环境变量

| 变量 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `SANDBOX_TOKEN` | 是 | —— | 鉴权 token（`/invoke` Bearer 面；缺失 / 为空启动期 fail fast）。**永不落日志 / 错误响应** |
| `SANDBOX_HTTP_ALLOWLIST` | 否 | 空 | `http_fetch` 域名允许清单（逗号分隔域名后缀；**空 = 工具禁用 / 默认拒绝**——零配置部署永不产生出站流量） |
| `BILI_API_ALLOWLIST` | 否 | `api.bilibili.com,app.bilibili.com` | B 站工具 API 域名允许清单（**未设置 = 默认清单**——与 http_fetch 相反；**空串 = 显式禁用** B 站工具组） |
| `BILI_COOKIE` | 否 | 空 | B 站可选 Cookie 头原值（匿名公共访问为缺省；设置时作为原生 `Cookie` 头直发，提升限速余量。**永不落日志 / 错误响应**） |
| `SANDBOX_HOST` | 否 | `0.0.0.0` | 监听地址（容器形态缺省；裸跑建议 `127.0.0.1`） |
| `SANDBOX_PORT` | 否 | `8765` | 监听端口（改端口须同步 Dockerfile HEALTHCHECK 探针） |

## 鉴权（安全红线）

- `POST /invoke` 须携带 `Authorization: Bearer <SANDBOX_TOKEN>`；
- token 比较为**恒时**（`secrets.compare_digest`——防时序侧信道）；
- 全部鉴权失败统一 `401` + 统一 detail `鉴权失败`（不区分头缺失 / 形态
  错误 / token 不匹配——杜绝错误面 oracle）；
- `GET /health` 无鉴权（存活面——容器 HEALTHCHECK / 后端启动探测消费）。

## 工具契约（wire）

```
POST /invoke {"tool": "http_fetch", "arguments": {"url": "https://example.com/x", "timeout_seconds": 10}}
           → {"ok": true, "result": {...}, "error": null, "latency_ms": 42}
```

- 端点恒 `200` 携带成败终态（工具失败在载荷 `ok=false` 表达；非 200 =
  401 鉴权失败 / 422 请求形态非法——传输层失败面）；
- 沙盒侧运行时**恒不抛**：未知工具 / 参数校验失败 / 受控失败 / 执行器
  异常四类失败均归一 `ok=false` + error（镜像后端 ToolRegistry 语义）。

### 示例工具 `http_fetch`（P4 平台工具的落地范式）

| 项 | 值 |
|---|---|
| 参数 | `url`（http/https + 主机名，必填）、`timeout_seconds`（1-30，默认 10） |
| 允许清单 | `SANDBOX_HTTP_ALLOWLIST` 域名后缀；点边界匹配（`api.example.com` 命中 `example.com`；`evil-example.com` 不命中） |
| 重定向 | 30x 逐跳过允许清单 + 终态 URL 复检（防经重定向跳出清单） |
| 正文 | 按字节截断 8192（8KB）；字符集取响应头，回退 utf-8；解码 `errors="replace"` |
| 失败 | 全部归一受控失败（ok=false 终态——执行器恒不抛未受控异常） |
| 阻塞 I/O | stdlib urllib 经 `asyncio.to_thread` 下沉（零 httpx 依赖） |

### B 站只读工具组 `bili_*`（P4 平台工具——公开 JSON 接口读取面）

四工具全部**只读**（红线：零评论 / 弹幕发布等写入端点），共享一个客户端
核心（进程内限速 1 秒最小间隔 + HTTP 412 硬冷却 600→1200→1800 秒封顶 +
WBI 键 TTL 缓存）：

| 工具 | 端点（全部 GET） | 说明 |
|---|---|---|
| `bili_search_videos` | `/x/web-interface/wbi/search/type`（WBI 签名） | 关键词视频搜索（`search_type=video`；keyword 1-64 / page 1-5 / page_size 10/20/30） |
| `bili_video_info` | `/x/web-interface/view`（非 WBI） | 视频详情白名单裁剪（owner / stat 子集 / 首 P cid） |
| `bili_video_comments` | `/x/v2/reply/main`（mode=2 时间序游标） | view→aid 换算 + 游标分页（ps≤20；page 1-50） |
| `bili_video_subtitle` | `/x/player/v2`（view→cid 换算） | 字幕清单 + 可选首个字幕正文抓取（`fetch_subtitle` 默认 false；CDN 后缀清单 `biliapi.net` / `bilivideo.com` 管制） |

- **响应白名单裁剪 + JSON 序列化上限约 16KB**（超限自列表尾部整项裁剪 + `truncated` 标记）；
- **JSON code -352**（风控）按不重试受控失败处理；**code!=0** 携带 code + message 入受控文案；
- 出站**不走系统代理**（`ProxyHandler({})` 显式禁用）；UA + Referer 恒发；
- 参数面（四 args 模型）与后端 `harness/tools/sandbox_models.py` 人为镜像（同 http_fetch 范式）。

## Docker 构建 / 运行

镜像经上层 `docker/docker-compose.yml` 编排（构建上下文 = 本目录）：

```bash
cd docker
export SANDBOX_TOKEN="<随机长 token>"
export SANDBOX_HTTP_ALLOWLIST="example.com"
docker compose up -d tool-sandbox-agent     # 127.0.0.1:8765 → 容器 8765
docker compose logs tool-sandbox-agent
```

加固面横跨 Dockerfile 与 compose（tini / 非 root 1001 / HEALTHCHECK /
`cap_drop: ALL` / `no-new-privileges` / `read_only` + tmpfs `/tmp` /
mem 256m / cpus 0.5 / 端口仅回环暴露）——完整清单见 compose 注释与
`docs/backend/tools-sandbox.md`。

## 与后端的关系（零代码共享）

- 后端不进 compose（经 pixi 运行）；经 `http://127.0.0.1:8765` 消费本
  沙盒（共享 httpx 池 + Bearer token）；
- wire 契约在两树各持一份文本镜像——**修改任一侧必须同步另一侧**
  （byte 级一致；漂移门禁在后端侧看护）；
- `http_fetch` 的参数面（url / timeout_seconds 及其校验规则）同样双树
  各持一份（后端侧用于本地校验 + 默认值物化；沙盒侧为权威执行面）。
