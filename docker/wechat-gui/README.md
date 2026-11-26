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
  │     └─ 控制面 /run/wechat-gui/control.sock（JSON 行：ping/key/move/click）
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

## 构建与 PoC 门（本机）

前置：Docker 守护进程可用、宿主存在 `/dev/uinput`（内核 uinput 模块）。

```bash
cd docker/wechat-gui
pixi run tests-unit       # 单元测试（hermetic；覆盖率 ≥80 enforce）
pixi run coverage-gates   # 按模块覆盖率门禁（≥80 enforce）
pixi run lint             # ruff
pixi run typecheck        # mypy 严格模式
pixi run poc-stack        # 构建镜像 + 特权容器内跑 PoC 门（本机实测）
```

`poc-stack` 等价于直接执行 `poc/run_stack_poc.sh`：构建
`wechat-gui-poc:local`，以 `--privileged`（**仅 PoC**）运行
`poc_gate`，把单行 JSON 证据写到
`docs/evidence/poc/wechat-gui-stack-<hostname>-<UTC>.json`，并打印
`verdict`。构建对网络抖动最多重试 3 次；apt 源固定为清华镜像。

### PoC 门步骤与判定

门脚本在容器内按固定顺序执行，任一步失败即 `FAIL` 并跳过后续步骤：

| 步骤 | 断言 |
|---|---|
| `x_up` | `xdpyinfo :99` 可连接，分辨率 1280x800、根窗口色深 24 |
| `uinput_create` | 设备信息可读、键盘/指针事件节点可打开、控制面 ping 存活 |
| `xinput_visible` | `xinput list` 含两个虚拟设备且非漂浮（已绑定 Core 设备） |
| `event_roundtrip` | `xev -root` 采集到注入的 KeyPress / KeyRelease / ButtonPress（各 ≥1）与指针移动 |
| `screenshot` | `import -window root png:-` 取图，Pillow 校验 PNG、1280x800、非全黑、非全同色 |
| `clipboard` | `xclip` 写入剪贴板后读回一致 |
| `vnc_up` | 5900 可连接、`http://127.0.0.1:6080/vnc.html` 返回 200 |

退出码：0 = PASS，1 = FAIL，2 = 用法错误。报告含每步
`duration_perf_counter_ms`、通过/失败原因、容器 hostname、内核版本、
Docker 版本（容器内不可得为 `null`）。

### 本机实测结果

- 结果：**PASS**（7/7 步通过）。
- 证据：`docs/evidence/poc/wechat-gui-stack-LeoWorkLaptop-20261126T025721Z.json`
- 各步耗时（perf_counter ms）：`x_up` 3、`uinput_create` 62、
  `xinput_visible` 3、`event_roundtrip` 284（20 轮注入）、`screenshot`
  107、`clipboard` 10、`vnc_up` 10；总 483 ms。
- 生产形态非特权冒烟（`--device /dev/uinput` + `device_cgroup_rules:
  c 13:* rwm` + MKNOD/CHOWN/SETUID/SETGID/KILL + no-new-privileges，不
  privileged）：容器 HEALTHCHECK 转 `healthy`，容器内门 PASS 7/7
  （481 ms）。
- 非特权形态曾因仅映射 `/dev/uinput` 而被容器 device cgroup 以 EPERM
  拒绝打开 13:* 事件节点；补 `c 13:* rwm` 规则后通过（compose 已固化）。
- 实测中定位并修复的两处真实缺陷：Xorg 的 InputDevice **Identifier**
  才是 xinput 显示名（门脚本改按内核名匹配后，配置 Identifier 与内核名
  对齐）；`xclip -in` 会 fork 常驻 selection owner，继承 stdout/stderr
  管道导致 `communicate` 等到超时（写入路径改为 `/dev/null` 不采集输出）。
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
- **凭据**：VNC 密码走文件（compose secret）；缺省占位文件 `changeme`
  只用于开发，生产必须覆盖。
- **输入面**：uinput 设备名固定（`wechat-gui virtual *`），事件注入仅
  发生在该容器栈内；设备随 uinputd 退出而销毁。
- **可信边界**：本树不解析平台业务数据；截图与注入是唯一对外原语。
  视觉模型对截图的消费在通道层（过门后）承担。

## 测试面

| 测试文件 | 覆盖 |
|---|---|
| `src/tests/unit/test_uinput_devices.py` | ioctl 编号 / `uinput_user_dev` 结构体布局 / 能力注册序列 / sysfs 发现 / mknod / 事件字节 / 销毁幂等 |
| `src/tests/unit/test_uinputd.py` | 控制面协议矩阵 / 设备信息落盘与读取 / 属主调整 / Unix socket 服务与客户端 / 非法响应 / CLI 接线 |
| `src/tests/unit/test_poc_gate.py` | xdpyinfo / xinput / xev 解析 / 截图校验 / 命令执行与超时 / 步骤编排与跳过 / 报告与退出码 / 七个门步骤（临时 PATH 假命令） |
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
