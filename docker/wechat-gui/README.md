# wechat-gui — GUI 微信实验轨沙盒（PoC 门阶段）

GUI 微信的**隔离执行面**：容器内以 Xorg（dummy 显卡）+ uinput 虚拟输入
设备托管一个可截图、可注入键鼠的图形会话，经 x11vnc / noVNC 供人登录与
观察。本树与后端**零代码共享**，是独立 pixi 工程 / 独立镜像 / 独立 CI
job；本阶段只交付 **PoC 门制品与本机栈实测**，微信通道本身（收发、会话
键、人格路由）在过门后才实现。

主轨与实验轨的关系（已拍板）：企微智能机器人长连接是**生产主轨**；
GUI 微信是**实验轨**，用于企微覆盖不到的个人微信场景。实验轨不通过
Xorg + uinput 栈门则整轨推迟。

## 目录布局

```
docker/wechat-gui/
├── pixi.toml                 # 独立 pixi 项目（Python 3.12 + dev 工具 + tasks）
├── pyproject.toml            # 依赖单一数据源（纯 stdlib + Pillow；dev extras）
├── Dockerfile                # debian:bookworm-slim（tag+digest 固定）加固镜像
├── entrypoint.sh             # 入口编排（uinputd → Xorg → x11vnc → noVNC）
├── src/wechat_gui_poc/
│   ├── uinput_devices.py     # uinput ioctl 编码 / 设备创建 / 事件注入 / 节点发现
│   ├── uinputd.py            # 设备持有守护进程 + Unix socket 控制面 + 客户端
│   ├── wire_models.py        # 数据模型与 wire 契约（控制面请求/响应、设备制品、门报告）
│   ├── xorg.py               # Xorg 静态配置生成（evdev + dummy）
│   └── poc_gate.py           # 七步 PoC 门 + 解析 / 校验 / 单行 JSON 报告
├── src/tests/unit/           # hermetic 异步单测（零真实 X / 零真实 uinput）
├── poc/run_stack_poc.sh      # 宿主运行器（构建 + 特权容器内跑门 + 证据落盘）
└── vnc.pass.default          # compose 缺省 VNC 密码文件（开发占位，生产覆盖）
```

## 架构与数据流

```
entrypoint（root）
  ├─ uinputd（root）── 创建 虚拟键盘 + 绝对指针（/dev/uinput）
  │     ├─ 落盘 /run/wechat-gui/uinput-devices.json
  │     ├─ 生成 /etc/X11/xorg.conf.d/99-wechat-gui.conf（显式 evdev 设备段）
  │     ├─ 事件节点 mknod（容器无 udev）并 chown 给 wechat 用户
  │     └─ 控制面 /run/wechat-gui/control.sock（JSON 行：ping/key/move/click/shutdown）
  ├─ Xorg :99（wechat 用户，dummy 1280x800x24，AutoAddDevices 关闭）
  ├─ x11vnc 5900（-forever -shared -passwdfile）
  └─ websockify/noVNC 6080（--web=/usr/share/novnc）
```

关键机制（实现时的事实约束）：

- **无 udev**：uinput 创建的内核设备只在 sysfs（`/sys/class/input/eventN`）
  可见；容器内 `/dev/input/eventN` 节点不会自动出现，uinputd 按 sysfs
  `dev`（major:minor）自行 `mknod`（容器默认能力含 MKNOD；生产 compose
  显式补 MKNOD/CHOWN）。
- **静态输入段**：`evdev` 驱动 + 显式 `Option "Device"`，不依赖 libinput
  的 udev 枚举；键盘 / 指针在 ServerLayout 中绑定为 CoreKeyboard /
  CorePointer，xinput 中非漂浮。
- **绝对指针**：注册 `ABS_X/ABS_Y` + 三键；evdev 将其归类为 touchscreen
  类型的 slave pointer（坐标点击 / 拖拽语义等价）。若后续需要鼠标相对
  移动语义，可补 REL 轴，门脚本断言面不变。
- **注入归属**：uinput 设备生命周期 = 持有 fd 的进程生命周期；因此由
  uinputd 统一创建并长期持有，门脚本与后续通道经控制面注入，避免重复
  创建设备或跨进程写事件节点（event 节点只读）。

## 控制面协议与落盘字段

### 控制面 JSON 行协议（Unix socket，仅容器内可见）

单请求单响应；请求与响应模型在 `src/wechat_gui_poc/wire_models.py`，解码与
注入在 `uinputd.py`。`command` 非法或字段类型不符时回 `ok=false` 且
`command` 为空字符串（无法确定命令名）。

| 请求 | 载荷 | 成功响应 |
|---|---|---|
| 健康探测 | `{"command":"ping"}` | `{"ok":true,"command":"ping","keyboard":"wechat-gui virtual keyboard","pointer":"wechat-gui virtual pointer","width":1280,"height":800}` |
| 按键注入 | `{"command":"key","code":30,"pressed":true}` | `{"ok":true,"command":"key"}` |
| 指针移动 | `{"command":"move","x":640,"y":400}` | `{"ok":true,"command":"move"}` |
| 鼠标点击 | `{"command":"click","button":272}` | `{"ok":true,"command":"click"}` |
| 收口 | `{"command":"shutdown"}` | `{"ok":true,"command":"shutdown"}`（随后守护关闭服务并销毁设备） |

注入失败（设备已销毁 / 写入失败）回 `{"ok":false,"command":"key","error":"..."}`，
守护不中断；字段错误回 `{"ok":false,"command":"","error":"字段 code 必须是整数"}`。
`click` 的 `button` 缺省为 `272`（`BTN_LEFT`）；`key` 的 `code` 为 Linux
input-event-codes 数值（`30` = `KEY_A`）。

### 设备制品 `uinput-devices.json`（uinputd 落盘，root 持有）

```json
{"width":1280,"height":800,"keyboard":{"name":"wechat-gui virtual keyboard","event_path":"/dev/input/event24"},"pointer":{"name":"wechat-gui virtual pointer","event_path":"/dev/input/event25"}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `width` / `height` | integer | 绝对指针轴上限对应的屏幕尺寸（正数） |
| `keyboard.name` / `pointer.name` | string | 内核设备名（Xorg 静态段的 Identifier 与此一致） |
| `keyboard.event_path` / `pointer.event_path` | string | 容器内事件节点（按 sysfs `dev` 号 mknod 后 chown 给运行用户） |

### 门报告字段（`--out` 落盘与 stdout 单行 JSON 同源）

| 字段 | 类型 | 说明 |
|---|---|---|
| `verdict` | string | `PASS` / `FAIL` |
| `steps[].name` | string | 门步骤名（固定七步顺序） |
| `steps[].status` | string | `passed` / `failed` / `skipped` |
| `steps[].duration_perf_counter_ms` | integer | 该步 `perf_counter` 毫秒耗时 |
| `steps[].detail` | string | 成功摘要或失败原因 |
| `hostname` / `kernel` | string | 容器主机名 / 内核版本 |
| `docker` | string \| null | Docker 版本（容器内不可得为 `null`） |
| `started_at_ms` | integer | 启动时刻，Unix Epoch 毫秒 |
| `duration_perf_counter_ms` | integer | 总耗时毫秒 |
| `run_mode` | string | 宿主运行器追加：`privileged` / `unprivileged` |
| `build_attempts` | integer | 宿主运行器追加：镜像构建实际尝试次数（1-3） |
| `container_exit_code` | integer | 宿主运行器追加：PoC 容器退出码 |
| `host_session_guard` | object | 宿主运行器追加：`host_session_active`（bool）/ `host_display`（string\|null）/ `allow_host_session`（bool）/ `floated_devices`（array）/ `residual_devices`（array） |

## 构建与 PoC 门（本机）

前置：Docker 守护进程可用、宿主存在 `/dev/uinput`（内核 uinput 模块）。

```bash
cd docker/wechat-gui
pixi run tests-unit       # 单元测试（hermetic；覆盖率 ≥80 enforce）
pixi run coverage-gates   # 按模块覆盖率门禁（≥80 enforce）
pixi run lint             # ruff
pixi run typecheck        # mypy 严格模式
WECHAT_GUI_POC_ALLOW_HOST_SESSION=1 pixi run poc-stack   # 有宿主图形会话时必须显式放行
```

`poc-stack` 等价于直接执行 `poc/run_stack_poc.sh`：构建
`wechat-gui-poc:local`，按 `WECHAT_GUI_POC_RUN_MODE` 选择
`privileged`（缺省，仅 PoC 兼容性）或 `unprivileged`（生产同款：
`--device /dev/uinput` + `c 13:* rwm` + `--cap-drop ALL` + MKNOD/CHOWN/
SETUID/SETGID/KILL + `no-new-privileges`），运行 `poc_gate`，把单行 JSON
证据写到
`docs/evidence/poc/wechat-gui-stack-<hostname>-<run_mode>-<UTC>.json`，并
打印 `verdict`。构建对网络抖动最多重试 3 次（尝试次数写入证据
`build_attempts`）；apt 源固定为清华镜像；缺 `openssl` 直接失败（不回退
弱口令）。

**宿主图形会话守卫（uinput 是内核全局设备）**：运行器在构建 / 运行前探测
宿主活动图形会话（`DISPLAY` 可连，或 `/tmp/.X11-unix/X*` 存在且 `xdpyinfo`
可连）。检测到即默认拒绝运行并打印说明——容器内创建的
`wechat-gui virtual keyboard/pointer` 会被宿主 X 会话接入并接收注入的按键
与点击。显式 `WECHAT_GUI_POC_ALLOW_HOST_SESSION=1` 才放行，放行后在设备
出现时用镜像内的 `xinput` 于宿主逐一 `float`（只处理确实出现的设备）并
写入证据 `host_session_guard`；运行结束校验宿主无残留设备。生产形态的
根治是部署机 udev 规则（对 `wechat-gui virtual*` 置
`LIBINPUT_IGNORE_DEVICE="1"` 或清空 `ID_INPUT*`）并实测确认——该缓解未在
本门验证，属通道实现（R4-6b）开工前置项；无法配置 udev 的部署机按 VM
隔离评估。

### PoC 门步骤与判定

门脚本在容器内按固定顺序执行，任一步失败即 `FAIL` 并跳过后续步骤：

| 步骤 | 断言 |
|---|---|
| `x_up` | `xdpyinfo :99` 可连接，分辨率 1280x800、根窗口色深 24 |
| `uinput_create` | 设备信息可读、键盘/指针事件节点可打开、控制面 ping 存活 |
| `xinput_visible` | `xinput list` 含两个虚拟设备且非漂浮（已绑定 Core 设备） |
| `event_roundtrip` | `stdbuf -oL xev -root` 行缓冲采集；单轮注入 KEY_A 按下/抬起、绝对指针移动、左键按下/抬起，断言 KeyPress / KeyRelease / ButtonPress / ButtonRelease / Motion 各 ≥1 |
| `screenshot` | `import -window root png:-` 取图，Pillow 校验 PNG、1280x800、非全黑、非全同色 |
| `clipboard` | `xclip` 写入剪贴板后读回一致 |
| `vnc_up` | 5900 可连接、`http://127.0.0.1:6080/vnc.html` 返回 200 |

退出码：0 = PASS，1 = FAIL，2 = 用法错误。报告字段见上一节「门报告字段」
（每步 `duration_perf_counter_ms` 与失败原因、`started_at_ms`、宿主运行器
追加的 `run_mode` / `build_attempts` / `host_session_guard` 等）。

### 本机实测结果

宿主有活动 KDE 图形会话（`:0`）：两次运行都经
`WECHAT_GUI_POC_ALLOW_HOST_SESSION=1` 放行，运行器在设备出现后于宿主
`xinput float` 了 `wechat-gui virtual keyboard` 与 `wechat-gui virtual
pointer`，证据 `host_session_guard` 记录 `host_session_active=true` /
`floated_devices` 两项 / `residual_devices=[]`；运行结束后宿主
`/sys/class/input` 无该设备、无残留容器。未设置放行变量时运行器直接拒绝
运行（退出码 1）并打印宿主注入面说明。

| 形态 | 结果 | 证据 | 总耗时 |
|---|---|---|---|
| 特权 PoC（`--privileged`，仅本门兼容性） | **PASS**（7/7） | `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-privileged-20261126T034706Z.json` | 221 ms |
| 非特权（`--device /dev/uinput` + `c 13:* rwm` + 最小能力集 + `no-new-privileges`） | **PASS**（7/7） | `docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-unprivileged-20261126T034711Z.json` | 240 ms |

- 各步耗时（perf_counter ms，非特权形态）：`x_up` 3、`uinput_create` 83、
  `xinput_visible` 3、`event_roundtrip` 54（单轮注入，五类事件各 1）、
  `screenshot` 77、`clipboard` 8、`vnc_up` 8；`build_attempts=1`、
  `container_exit_code=0`。
- 单轮注入把事件回环从「每读一行注入一批」（旧证据 284 ms / 20 轮）收敛
  到 54 ms；`stdbuf -oL` 行缓冲是「注入一次即可采到完整回环」的前提。
- 非特权形态曾因仅映射 `/dev/uinput` 而被容器 device cgroup 以 EPERM
  拒绝打开 13:* 事件节点；补 `c 13:* rwm` 规则后通过（compose 已固化）。
- 证据经容器内路径写出后 `docker cp` 取回（不把仓库目录 bind mount 进
  容器）：非特权形态 `cap_drop ALL` 时容器内 root 无 DAC_OVERRIDE，绑定
  挂载写仓库目录会 `PermissionError`。
- 实测中定位并修复的真实缺陷（早期轮次，保留记录）：Xorg 的 InputDevice
  **Identifier** 才是 xinput 显示名（门脚本改按内核名匹配后，配置
  Identifier 与内核名对齐）；`xclip -in` 会 fork 常驻 selection owner，
  继承 stdout/stderr 管道导致 `communicate` 等到超时（写入路径改为
  `/dev/null` 不采集输出）。
- 门内 `docker` 字段为 `null`：门脚本在容器内运行，镜像不含 docker CLI。

## 生产运行（非特权）

生产**禁止 `--privileged`**：只映射 uinput 设备与最小能力集。compose
服务在 `profiles: [wechat-gui]` 下默认不启动：

```bash
cd docker
export WECHAT_GUI_VNC_PASSWORD_FILE=/secure/wechat-gui-vnc.pass  # 真实密码文件
docker compose --profile wechat-gui up -d wechat-gui
docker compose --profile wechat-gui logs -f wechat-gui
```

等价的裸 `docker run`（如需 GPU 渲染，追加 `--device /dev/dri:/dev/dri`）：

```bash
docker run -d --name wechat-gui \
  --device /dev/uinput:/dev/uinput \
  --device-cgroup-rule "c 13:* rwm" \
  --cap-drop ALL --cap-add MKNOD --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add KILL \
  --security-opt no-new-privileges:true \
  -p 127.0.0.1:5900:5900 -p 127.0.0.1:6080:6080 \
  -v /secure/wechat-gui-vnc.pass:/run/secrets/wechat_gui_vnc_password:ro \
  wechat-gui:0.1.0 serve
```

- 能力解释：MKNOD 用于容器内事件节点落位（无 udev），CHOWN 用于把节点
  交给 `wechat` 运行用户，SETUID/SETGID 供 `runuser` 降权，KILL 供入口
  清理降权子进程；入口以 root 启动后 Xorg / x11vnc / websockify 均降权
  到 `wechat`。
- **device cgroup 规则**：uinput 动态创建的事件节点是字符设备 13:*；仅
  映射 `/dev/uinput` 时容器 device cgroup 会以 EPERM 拒绝打开事件节点，
  必须显式放行 `c 13:* rwm`（compose 已声明）。
- 端口只映射到宿主回环；VNC 密码只经文件 secret 注入（镜像不含密码）。
- **密码文件权限**：文件由入口以运行用户身份探测可读性——`wechat:wechat
  0400` 直接使用；root 可读而 `wechat` 不可读时由入口复制到运行目录
  （`wechat:wechat 0400`）后使用；两者都不可读时入口失败退出（不会让
  x11vnc 以不可读密码文件空跑）。宿主侧无需固定 uid：运行器把临时密码
  文件置 `0644` 放在 `0700` 临时目录内，容器内绑定挂载因此不依赖「宿主
  调用者 uid == wechat uid」这一隐含前提。
- **密码解析冒烟（root 0400 复制路径）**：入口函数可在镜像内直调，验证
  复制后的属主与权限（期望输出 `wechat:wechat 400`）：

  ```bash
  docker run --rm --entrypoint bash wechat-gui:0.1.0 -c '
    mkdir -p /tmp/run && printf "smoke\n" > /tmp/pass
    chown root:root /tmp/pass && chmod 0400 /tmp/pass
    source /usr/local/bin/entrypoint.sh
    target="$(resolve_vnc_passfile /tmp/pass wechat /tmp/run)"
    printf "resolved=%s\n" "${target}"; stat -c "%U:%G %a" "${target}"'
  ```
- 资源限额（compose：1g / 1.0 CPU / 256m shm）按部署机实测收紧。

## noVNC 登录与微信客户端安装（Live 阶段）

本阶段**不安装微信客户端**（仓库不分发 `.deb`；由部署者提供官方 Linux
安装包并负责来源可信）。过门后的 Live 步骤：

1. 启动栈（上节），宿主浏览器打开
   `http://127.0.0.1:6080/vnc.html`，输入 VNC 密码；
2. 获得 Xorg `:99` 的图形会话（1280x800）；
3. 装入微信客户端（二选一）：
   - 运行中的容器内安装：把官方 `.deb` 挂载/复制进容器后
     `docker exec -u root wechat-gui apt-get install -y /tmp/wechat.deb`；
   - 或在部署侧基于本镜像加一层 `COPY` + `apt-get install` 的自定义镜像；
4. 以运行用户启动客户端：
   `docker exec -d -u wechat -e DISPLAY=:99 wechat-gui wechat`（可执行名
   以安装包为准），随后在 noVNC 窗口中扫码 / 登录；
5. 关闭 noVNC 前确认登录态持久化（客户端自有配置目录；如需跨容器重启
   留存，为其挂载命名卷——Live 阶段落地）。

已知 Live 前置项（不阻塞本门）：

- **窗口管理器**：当前镜像不装 WM，客户端窗口无装饰/不可拖动；Live
  阶段按需加入轻量 WM（如 openbox）并固定启动顺序。
- **中文输入法 / 字体**：按客户端要求补字体与输入法（镜像未预装）。
- **音频**：Xorg dummy + noVNC 默认无音频回传；语音消息播放在 Live
  阶段评估。
- 截图 / 点击坐标以 1280x800 为基准；客户端缩放策略在 Live 阶段固化。

## 会话键设计预告（过门后接后端）

按已拍板的 **B 方案（P4）**：GUI 微信会话键 = **联系人备注名 + VLM 校验**。
门阶段不产生 wire 契约、不接后端；R4-6b 落地时再定义：

- 入站会话从当前聊天窗口标题 / 联系人备注提取候选键，截图经视觉模型
  校验联系人身份后才落定（防止同名 / 改备注串会话）；
- 出站按同一键定位会话窗口（必要时搜索切换）；
- 群聊与个人会话区分、重名消歧、备注变更后的键迁移均在通道层实现；
- 本树只提供「截图 + 键鼠注入 + 剪贴板」原语，不感知微信业务语义。

## 安全边界

- **镜像**：debian:bookworm-slim（digest 固定）；无微信客户端、无
  编译工具链；`tini` 作 PID 1；HEALTHCHECK 同时探测 X 与 noVNC。
- **运行期**：生产不 privileged；只映射 `/dev/uinput`（可选 `/dev/dri`）；
  `cap_drop ALL` + MKNOD/CHOWN；`no-new-privileges`；端口仅宿主回环。
- **凭据**：VNC 密码走文件（compose secret），且必须对**运行用户**
  （`wechat`）可读——推荐 `wechat:wechat 0400`；root 持有（如部署机生成的
  0400 root 文件）时由入口在运行期复制为 `/run/wechat-gui/vnc.pass`
  （`wechat:wechat 0400`），复制失败即整体失败。`WECHAT_GUI_VNC_PASSWORD`
  明文优先于密码文件，写入同一路径（0400、运行用户所有）。缺省占位文件
  `changeme` 只用于开发，生产必须覆盖。
- **输入面**：uinput 设备名固定（`wechat-gui virtual *`），设备随 uinputd
  退出而销毁；但设备是**内核全局**对象——宿主图形会话（或同机其它 X 会话）
  会接入这两个设备并接收注入。本机复跑前必须在宿主 `xinput float` 该
  设备（`poc/run_stack_poc.sh` 在有宿主会话时默认拒绝运行，放行后自动浮动
  并记录证据）；生产形态必须在部署机配置宿主 udev 规则（对
  `wechat-gui virtual*` 置 `LIBINPUT_IGNORE_DEVICE="1"` 或清空 `ID_INPUT*`）
  并实测确认——该缓解未在本次验证，属通道实现（R4-6b）开工前置项；无法
  满足时按 VM 隔离评估。
- **可信边界**：本树不解析平台业务数据；截图与注入是唯一对外原语。
  视觉模型对截图的消费在通道层（过门后）承担。

## 测试面

| 测试文件 | 覆盖 |
|---|---|
| `src/tests/unit/test_uinput_devices.py` | ioctl 编号 / `uinput_user_dev` 结构体布局 / 能力注册序列 / sysfs 发现 / mknod / 事件字节 / 销毁幂等 |
| `src/tests/unit/test_uinputd.py` | 控制面五命令解码为具体模型 / 非法载荷矩阵 / 设备信息落盘与读取 / 属主调整 / Unix socket 服务与客户端 / 探测字段不完整与类型非法 / CLI 接线 |
| `src/tests/unit/test_poc_gate.py` | xdpyinfo / xinput / xev 解析 / 截图校验 / 命令执行与超时 / 步骤编排与跳过 / 报告与退出码 / 七个门步骤（临时 PATH 假命令）/ 单轮注入与 Motion·ButtonRelease 断言 / 注入失败 / 剪贴板回读不一致 / noVNC 非 200 |
| `src/tests/unit/test_entrypoint_password.py` | 入口密码解析：运行用户可读直接使用 / 明文密码回退（0400）/ 明文优先于文件 / 两者不可读即失败 |
| `src/tests/unit/test_xorg.py` | 静态 evdev 段 / dummy 屏幕 / 配置落盘 |

## 故障排查

- Xorg 未起：看 `/run/wechat-gui/xorg.log`（容器内）；常见为事件节点
  不存在 / 属主不可读——确认 uinputd 已先启动且节点在 `/dev/input`。
- `xinput` 无设备：看 Xorg 日志 `evdev: ... Using input driver` 段；
  Identifier 与内核名必须一致（本树生成配置时已对齐）。
- 截图全黑：根窗口织纹（`xsetroot -mod`）未设置或 X 未就绪。
- noVNC 打不开：确认 6080 监听与 `/usr/share/novnc/vnc.html` 存在；
  查看 `/run/wechat-gui/novnc.log`。
- 容器内 `dbus-core` 报错：dummy Xorg 无 system bus，属非致命告警。

## 与后端的关系（零代码共享）

本阶段不产生 wire 契约、不引入后端依赖。过门后的通道实现（R4-6b）按
沙盒树范式经独立传输面接后端；届时再评估是否加入 sandbox-drift 门
（当前无镜像契约，故未纳入）。
