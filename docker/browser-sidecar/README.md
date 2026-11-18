# browser-sidecar — 浏览器自动化 sidecar

浏览器自动化的**隔离执行面**（JS 渲染回退）：后端把需要真实浏览器运行
时的工具（`navigate` / `read_text` / `screenshot` / `click` / `fill` /
`list_tabs` / `close_tab` / `evaluate` 八工具）代理到本 sidecar 进程执行，与后端
**零代码共享**——唯一的耦合点是人为镜像的 wire 契约 Model Classes 与
工具名 / 部署契约常量（`src/browser_sidecar/wire_models.py` + `browser.py`
↔ 后端 `harness/tools/browser_models.py` + `browser.py`，byte 级一致，
契约漂移由仓库根漂移门禁看护）。

wire 契约 **REST 同构镜像 tool-sandbox 树**（`POST /invoke` `{tool,
arguments}` → `{ok, result, error, latency_ms}` + `GET /health` + Bearer
token）——不是新协议；后端消费范式与沙盒工具一致。

架构与完整链路文档见 `docs/backend/tools-browser.md`（后端侧建档）。

## 目录布局（文档化决策）

```
docker/browser-sidecar/
├── pixi.toml          # 独立 pixi 项目（Python 3.12 + dev 工具 + tasks）
├── pyproject.toml     # 依赖单一数据源（运行时 fastapi/uvicorn/pydantic/
│                      #   playwright 钉版 + dev extras）
├── server.py          # ASGI 入口薄层（app 工厂 + uvicorn main + token 鉴权
│                      #   + lifespan 管理器起停）
├── src/browser_sidecar/  # 常规包：wire_models / runtime / policy / browser
├── src/tests/unit/    # 单元测试（hermetic——零真实浏览器；fake face / fake
│                      #   playwright 对象驱动；全部 async def test_）
├── Dockerfile         # 加固镜像（mcr playwright/python 基底 + tini +
│                      #   非 root pwuser + HEALTHCHECK）
└── README.md          # 本文件
```

与 tool-sandbox 树的布局差异（有意为之，均为文档化决策）：

- 运行时依赖多 **playwright**（钉版对齐 Dockerfile 基底标签——驱动与
  浏览器构建错配是部署期唯一致命形态）；
- **上下文池即共享池**：本树无 httpx 池；「池」= `BrowserManager` 的
  context-per-profile 注册表（profile → 持久化 BrowserContext，cookies /
  localStorage 跨重启留存）；
- server 带 **lifespan**（默认运行体的收割任务随应用起停——标签页空闲
  10 分钟关闭的后台任务需要事件循环）。

## 快速开始（开发 / 测试）

```bash
cd docker/browser-sidecar
pixi install          # 安装 default 环境（runtime + dev 工具）
pixi run tests-unit   # 单元测试（hermetic——零真实浏览器；覆盖率 ≥80 enforce）
pixi run coverage-gates   # 按模块覆盖率门禁（≥80 enforce）
pixi run lint         # ruff 静态检查
pixi run typecheck    # mypy 严格模式
```

## 运行（本地裸跑）

```bash
export BROWSER_SIDECAR_TOKEN="<随机长 token>"      # 必填——缺失即拒绝启动
export BROWSER_SIDECAR_ALLOWLIST="example.com"     # 导航允许清单（空 = 拒绝一切）
export BROWSER_SIDECAR_HOST=127.0.0.1              # 裸跑建议显式回环（缺省 0.0.0.0 为容器形态）
pixi run python server.py                          # 监听 0.0.0.0:8766（缺省；BROWSER_SIDECAR_PORT 可覆写）
```

存活探测：`GET /health`（无鉴权）→ `{"status": "ok"}`。

真实浏览器在首个工具调用时惰性启动（`profiles/` 目录落在 CWD 下——
裸跑形态；容器形态挂载点 `/app/profiles`）。

## 环境变量

| 变量 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `BROWSER_SIDECAR_TOKEN` | 是 | —— | 鉴权 token（`/invoke` Bearer 面；缺失 / 为空启动期 fail fast）。**永不落日志 / 错误响应** |
| `BROWSER_SIDECAR_ALLOWLIST` | 否 | 空 | 导航 / 子资源域名允许清单（逗号分隔域名后缀，点边界匹配；**空 = navigate 拒绝一切 / 默认拒绝**——零配置部署永不产生出站流量） |
| `BROWSER_SIDECAR_NAV_TIMEOUT_SECONDS` | 否 | `20` | 导航超时秒数（1-120；非法值启动期 fail fast） |
| `BROWSER_SIDECAR_PROFILE` | 否 | `default` | 生效 profile 名（context-per-profile 注册表键——`profiles/<name>` 持久化登录态） |
| `BROWSER_SIDECAR_PROFILES_DIR` | 否 | `profiles` | profiles 根目录（容器形态 `/app/profiles`——compose 卷挂载点） |
| `BROWSER_SIDECAR_HOST` | 否 | `0.0.0.0` | 监听地址（容器形态缺省；裸跑建议 `127.0.0.1`） |
| `BROWSER_SIDECAR_PORT` | 否 | `8766` | 监听端口（改端口须同步 Dockerfile HEALTHCHECK 探针） |

## 鉴权（安全红线）

- `POST /invoke` 须携带 `Authorization: Bearer <BROWSER_SIDECAR_TOKEN>`；
- token 比较为**恒时**（`secrets.compare_digest`——防时序侧信道）；
- 全部鉴权失败统一 `401` + 统一 detail `鉴权失败`（不区分头缺失 / 形态
  错误 / token 不匹配——杜绝错误面 oracle）；
- `GET /health` 无鉴权（存活面——容器 HEALTHCHECK / 后端启动探测消费）。

## 工具契约（wire）

```
POST /invoke {"tool": "navigate", "arguments": {"url": "https://example.com/x"}}
            → {"ok": true, "result": {"url": "...", "title": "..."}, "error": null, "latency_ms": 842}
```

- 端点恒 `200` 携带成败终态（工具失败在载荷 `ok=false` 表达；非 200 =
  401 鉴权失败 / 422 请求形态非法——传输层失败面）；
- sidecar 侧运行时**恒不抛**：未知工具 / 参数校验失败 / 受控失败 /
  执行器异常四类失败均归一 `ok=false` + error（镜像 tool-sandbox 语义）。

### 八工具

| 工具 | 参数 | 结果 | 说明 |
|---|---|---|---|
| `navigate` | `url`（http/https + 主机名，≤2048） | `url`（终态）/ `title` | 导航（策略链见下；默认 20s 超时，env 可覆写） |
| `read_text` | `max_chars`（1-100000，默认 8000） | `text` / `truncated` / `char_count` / `url` | 当前页正文（**原始文本零包装**——不可信标注由后端消费层承担，见下） |
| `screenshot` | `full_page`（默认 false） | `image_base64` / `width` / `height` / `full_page` | 截图（视口或整页；尺寸上限 1920x4096 clip 裁剪 + 6MiB 体积上限） |
| `click` | `selector`（CSS，≤512） | `url` / `title` | 点击元素（选择器等待 10s） |
| `fill` | `selector` + `value`（≤8192，空 = 清空） | `filled` / `selector` | 填充表单字段 |
| `list_tabs` | （无） | `count` / `tabs[{index,url,title}]` | 列举标签页（上限 4） |
| `close_tab` | `tab_index`（≥0） | `closed_tab_index` | 按索引关闭标签页 |
| `evaluate` | `script`（JS 表达式，≤20000）+ `max_chars`（默认 262144）+ `timeout_ms`（默认 15000） | `result` / `char_count` | 页面上下文 JS 求值（结果 JSON 序列化规模超 `max_chars` 即受控失败——不静默截断） |

### 导航策略链（顺序锁定）

```
① 协议面      仅 http/https（about: / javascript: / file: / data: 拒绝）
② 主机提取    小写 + 去尾点归一
③ 私网拒绝    IP 字面量私网（loopback/RFC1918/link-local 等）无论清单一律拒；
              域名先 DNS 解析——任一解析 IP 私网即拒（DNS rebinding 对策：
              解析先行、按主机名导航）
④ 允许清单    BROWSER_SIDECAR_ALLOWLIST 点边界后缀匹配（空 = 拒绝一切）
⑤ 路由拦截    context 创建期 page.route("**/*")——子资源与重定向逐跳同判定，
              被拒即静默 abort（页面仍渲染清单内来源的内容）
```

**子资源面权衡（文档化取舍）**：路由拦截按请求宿主判定——跨域 CDN
（第三方脚本 / 字体 / 图片）默认全部阻断，需要跨域资源的站点由运维向
允许清单追加后缀。默认面宁可渲染残缺，不放行未知宿主。

**不可信内容分工（锁定决策）**：`read_text` 返回原始页面文本（sidecar
零提示词包装）；「平台内容不可信」标注与包裹由**后端消费层**承担
（backend `harness/tools/browser.py` 按记忆域 provenance=untrusted 约定
包裹后才进 LLM / 记忆链路）。

## Docker 构建 / 运行

镜像经上层 `docker/docker-compose.yml` 编排（构建上下文 = 本目录）：

```bash
cd docker
export BROWSER_SIDECAR_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export BROWSER_SIDECAR_ALLOWLIST="example.com"
docker compose up -d browser-sidecar     # 127.0.0.1:8766 → 容器 8766
docker compose logs browser-sidecar
```

加固面横跨 Dockerfile 与 compose（tini / 非 root pwuser / HEALTHCHECK /
shm_size 1g / mem_limit / 端口仅回环暴露 / profiles 卷持久化）。完整
清单与 cap_drop 的 Playwright 注意事项见 compose 注释与
`docs/backend/tools-browser.md`。

## 部署验证（真实浏览器面——单测不覆盖）

单测 hermetic（fake face / fake playwright 对象）——**真实浏览器行为
在部署机验证**（镜像 CI 不构建：playwright 基底约 2GB，CI 时长 / 缓存
面不成本比）：

```bash
cd docker && docker compose up -d browser-sidecar
TOKEN="$BROWSER_SIDECAR_TOKEN"
# ① 存活
curl -s http://127.0.0.1:8766/health                     # {"status":"ok"}
# ② 导航 + 读取（允许清单内目标）
curl -s -X POST http://127.0.0.1:8766/invoke \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"tool":"navigate","arguments":{"url":"https://example.com/"}}'
curl -s -X POST http://127.0.0.1:8766/invoke \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"tool":"read_text","arguments":{"max_chars":500}}'
# ③ 策略拒绝面（清单外目标 → ok=false + 中文受控文案）
curl -s -X POST http://127.0.0.1:8766/invoke \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"tool":"navigate","arguments":{"url":"https://not-allowlisted.example/"}}'
```

## 与后端的关系（零代码共享）

- 后端不进 compose（经 pixi 运行）；经 `http://127.0.0.1:8766` 消费本
  sidecar（共享 httpx 池 + Bearer token——`Settings.tools_browser_sidecar`
  分节）；
- wire 契约在两树各持一份文本镜像——**修改任一侧必须同步另一侧**
  （byte 级一致；漂移门禁在后端侧看护）；
- 八工具参数面同样双树各持一份（后端侧用于本地校验 + 默认值物化；
  sidecar 侧为权威执行面——双侧校验为有意冗余）。
