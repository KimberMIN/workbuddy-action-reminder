# WorkBuddy Action Reminder

> 当 WorkBuddy / CodeBuddy 长任务进入「等待用户确认 / 选择选项」状态时，触发 **macOS 系统级置顶提醒**并定期重复，同时经 **飞书** 跨设备送达（手机 / 桌面客户端），直到用户处理或会话结束；点击 macOS 通知可聚焦 WorkBuddy，飞书消息让你离开电脑也不漏接。

作者：Kimber Min

---

## 1. 这是什么

在使用 WorkBuddy 做 vibe coding 时，智能体会频繁停在 **权限确认**（如 `rm -rf`、写入文件）或 **选项询问**（如「选 A 还是 B」）上等待你操作。一旦你切走窗口去干别的，很容易忘记回来，导致长任务卡死几十分钟。

本插件解决这个痛点：

- **状态检测**：通过 WorkBuddy 的 Hook 机制（`Notification` / `PermissionRequest` / `Elicitation` 等事件）精确感知「正在等人」的状态。
- **系统级提醒**：调用 macOS 通知中心弹出提醒，**跨窗口可见**、可置顶。
- **重复提醒**：按 `[3min, 10min, 15min, 15min …]` 节奏反复提醒，直到你回来处理，避免「响一声就忘了」。
- **多任务区分**：每条待处理交互用 `session_id` / `interaction_id` 区分，通知里显示具体是「哪个会话、在等什么」。
- **误报避免**：仅在真正的等待类事件触发；用户一旦提交（`UserPromptSubmit`）或做出选择（`ElicitationResult`），对应提醒立即清除。
- **不阻塞 WorkBuddy**：Hook 只做毫秒级 HTTP 转发，所有定时、重复、通知逻辑都在独立守护进程里，绝不影响智能体执行。

---

## 2. 架构

```
┌─────────────────────────┐
│   WorkBuddy / CodeBuddy │
│   （长任务运行中）        │
└───────────┬─────────────┘
            │ 触发 Hook 事件（PermissionRequest / Elicitation / Notification…）
            │   stdin 传入 JSON
            ▼
┌─────────────────────────┐
│   hook-dispatch.sh       │  快速解析 + 转发（超时 3s，绝不阻塞）
└───────────┬─────────────┘
            │ HTTP POST http://127.0.0.1:18789/event  （绕过沙箱代理）
            ▼
┌─────────────────────────┐
│   reminder-daemon.py     │  常驻守护进程（127.0.0.1:18789）
│   - 维护待处理队列        │
│   - 调度重复提醒          │
│   - 2h 超时 / 静默时段    │
│   - 接收 resolve 事件清除 │
│   - 双通道通知（mac/飞书）│
└───────────┬─────────────┘
            │ 调用
       ┌────┴─────┐
       ▼          ▼
┌─────────────────┐   ┌─────────────────────────┐
│ notify-macos.sh │   │ notify-feishu.sh         │
│ terminal-notifier│   │ lark-cli（飞书 CLI）      │
│ → osascript 兜底 │   │ 私聊(open_id)/群(chat_id)│
└────────┬────────┘   └────────────┬────────────┘
         │ 系统通知                 │ 飞书消息
         ▼                          ▼
   macOS 通知中心            飞书（手机 / 桌面客户端）
   （置顶 / 重复）           —— 离开 Mac 也能收到
```

**为什么是「Hook 转发 + 守护进程」两步？**

WorkBuddy 的 Hook 必须是同步、快速的，否则会卡住智能体。因此 Hook 脚本只负责把事件 JSON 用 HTTP POST 给本地守护进程就立刻返回（失败也返回 0，绝不阻断 WorkBuddy）。所有耗时、重复、定时的逻辑都在守护进程里异步完成。守护进程若未运行，Hook 脚本会懒启动它一次。

---

## 3. 支持的事件

| 事件 | matcher | 含义 | 触发提醒 |
|------|---------|------|----------|
| `Notification` | `permission_prompt` | 权限确认弹窗 | ✅ |
| `Notification` | `idle_prompt` | 普通空闲也会产生，不能证明需要介入 | 不提醒 |
| `PermissionRequest` | — | 插件级权限请求 | ✅ |
| `Elicitation` | — | 等待用户做选项选择 | ✅ |
| `ElicitationResult` | — | 用户已选择 → **清除** | 清除 |
| `UserPromptSubmit` | — | 用户已提交 → **清除** | 清除 |
| `PostToolUse` | — | 工具调用后（去抖，避免误报） | — |
| `SessionEnd` | — | 会话结束 → **清除该会话全部** | 清除 |
| `PermissionDenied` | — | 权限被拒 → **清除** | 清除 |

去重优先使用会话内 `interaction_id`、`tool_use_id`/`call_id`、`elicitation_id`；缺 ID 时使用规范化工具参数的 SHA-256 指纹，原始参数不持久化。同请求的重复 Hook 在 `WAITING`、`ACKED`、`ESCALATED`、`EXPIRED` 均不重新发送。`PostToolUse`/`PostToolUseFailure`/`PermissionDenied` 仅按调用 ID 精确解除；原等待项缺 ID 时允许匹配参数指纹，不再整会话清理。无 ID 的 `ElicitationResult` 不猜测关联项。只有 `UserPromptSubmit`/`SessionEnd` 会清理整会话。升级加载状态时将旧 idle 项停止调度，保留 ACK 标记及其 token/generation。

`PreToolUse` 仅精确匹配 `AskUserQuestion`：这是明确要求用户作答的工具，不将普通 Write/Edit/Bash 的调用前事件当作授权。它与后续相同 `tool_use_id` 的 PermissionRequest 合并；不返回批准或改变权限。无法关联调用的泛 `permission_prompt` 仅首次提醒，保留记录用于去重和按钮，不继续重复；旧泛通知重启后也停止重复。只有携带明确 interaction_id 的旧键可迁移，旧无 ID 权限键可能碰撞不同操作，不将其绑定到新调用而吞掉新授权。

**事件信息边界**：本机 CLI 实现的 PermissionRequest 和 PostToolUse 提供 `tool_use_id`，但仅含 message 的 permission_prompt 无法可靠关联某个具体调用，不能用时间窗口合并而吞掉并发授权。若同一授权分别发出有 ID 和无 ID 两路 Hook，仍可能产生两条提醒；同工具同参数的无 ID 事件与后来有 ID 的事件也不会冒险按指纹合并，可能产生两条首提醒；不同调用 ID 即使参数相同仍应视为并发独立请求。缺 ID 的相同参数请求在没有生命周期结束信号时也无法区分新旧。无 ID 泛通知同文再次出现会保持去重，直到同会话 PostToolUse/PostToolUseFailure 或 UserPromptSubmit/SessionEnd 结束窗口。其中工具进展仅清除无 ID 且无参数指纹的泛通知，不触碰其他具体请求；这不是已识别某个具体授权完成，不能称为完整的新授权周期识别。自然语言中的提问不等于可验证阻塞：普通回答、Stop、任务完成、idle 一律不提醒。具体决策依赖宿主真正发出的 PermissionRequest/Elicitation，未发出事件的弹窗不能由本插件保证覆盖。

---

## 4. 文件结构

```
workbuddy-action-reminder/
├── .codebuddy-plugin/
│   └── plugin.json            # 插件元信息（name / version / hooks 入口）
├── hooks/
│   └── hooks.json             # 声明要监听的 Hook 事件
├── scripts/
│   ├── reminder-daemon.py     # 核心：常驻 HTTP 守护进程（纯 stdlib，无依赖）
│   ├── hook-dispatch.sh       # Hook 入口：解析并转发事件到守护进程
│   ├── notify-macos.sh        # macOS 通知封装（terminal-notifier / osascript）
│   ├── notify-feishu.sh       # 飞书通知封装（lark-cli，跨设备可达）
│   └── action-reminderctl.sh  # 控制脚本：start/stop/status/test/install-launchd…
├── README.md
└── LICENSE
```

状态目录（运行时生成）：`~/.workbuddy/action-reminder/`
- `state.json`   待处理交互的持久化状态
- `config.json` 用户可改的配置（重复节奏、超时、静默时段）
- `daemon.log`  守护进程日志

---

## 5. 安装与部署（本地）

### 方式 A：作为 WorkBuddy 本地插件（推荐）

插件文件已放在：

```
~/.workbuddy/plugins/cache/local/workbuddy-action-reminder/1.0.0/
```

并在以下注册表中登记（自动完成）：

- `~/.workbuddy/plugins/installed_plugins.json` → `workbuddy-action-reminder@local`
- `~/.workbuddy/plugins/known_marketplaces.json` → `local` marketplace
- `~/.workbuddy/settings.json` → `enabledPlugins["workbuddy-action-reminder@local"] = true`

**重启 WorkBuddy** 后插件即生效。首次触发等待事件时，守护进程会被自动懒启动。

### 2026-10-13 事件过滤修复的部署与验收

本轮仅修改插件文件并离线测试，未改用户 settings、未重启 daemon、未发送真实飞书消息。Python 修改需要经用户批准重启提醒 daemon 后才生效；Hook 注册修改需要宿主重新加载。本机只读核查确认用户 settings 无 hooks 覆盖且插件已启用，无需 settings 补丁。插件自身增加 AskUserQuestion 的精确 PreToolUse 注册并移除 idle_prompt。若以后发现用户直接注册的旧 idle 匹配，只移除该提醒脚本对应项，不整段覆盖 hooks。

本机脱敏历史日志聚合：CREATE 为 permission 17、elicitation 1、idle 72；RESOLVE 为 UserPromptSubmit 45、ElicitationResult 1、SessionEnd 1、PostToolUse 11。这证明历史存在大量 idle 提醒，不证明每种文件授权均收到。安装包 `resetIdleTimer` 在普通空闲且无后台任务后 60 秒产生 idle；独立安装包核验确认：普通 HookManager 读取 settings/plugin/scoped command；SdkHooksManager 是独立 Map，只由 initialize/update_sdk_config 控制协议的 request.hooks 设置，invokeCallback 通过 stdout control_request/hook_callback 调用宿主，不执行插件 shell。requestHostPermission/requestSdkPermission 只读取后者；needsApproval 中 PreToolUse ask/tool.check ask 的提前返回会绕过普通 PermissionRequest。因此只改 hooks.json 不能补齐所有 GUI 文件审批，需要 WorkBuddy 宿主桥接。本轮未改安装包或权限机制，也未用文件工具的 PreToolUse 猜授权，仅修复误报、精确解除和实际到达插件的授权事件，不承诺无遗漏。

部署前运行 `/usr/bin/python3 -m unittest discover -s scripts -p test_reminder.py -v`。批准后再通过现有受控重启流程加载 daemon 并核对回调 ready，普通空闲和任务完成应无提醒；在真实会话逐项停留于删除、文件覆盖/改写、授权、选项决策，核对即时提醒和摘要；并行两个授权时完成一个，不得解除另一个。双按钮仍只打开对应任务或停止单条提醒，不批准任何操作。离线测试不能替代这些真实宿主和飞书端到端验收。

### 方式 B：开机自启（launchd）

> 注意：在 WorkBuddy 内置的 Bash 沙箱里 `launchctl` 无法与主用户 launchd 会话通信（会报 `I/O error`）。请在**普通终端（Terminal.app / iTerm）**里执行：

```bash
bash ~/.workbuddy/plugins/cache/local/workbuddy-action-reminder/1.0.0/scripts/action-reminderctl.sh install-launchd
```

这会在 `~/Library/LaunchAgents/com.tencent.workbuddy.action-reminder.plist` 写入自启项，并立即启动守护进程。`KeepAlive` 保证崩溃后自动拉起。

卸载自启：

```bash
bash ~/.workbuddy/plugins/cache/local/workbuddy-action-reminder/1.0.0/scripts/action-reminderctl.sh uninstall-launchd
```

---

## 6. 日常管理

```bash
cd ~/.workbuddy/plugins/cache/local/workbuddy-action-reminder/1.0.0/scripts

./action-reminderctl.sh start            # 启动守护进程
./action-reminderctl.sh stop             # 停止
./action-reminderctl.sh restart          # 重启
./action-reminderctl.sh status           # 查看运行态 + 当前待处理列表
./action-reminderctl.sh test             # 发一条测试通知（验证通道）
./action-reminderctl.sh log              # 查看最近日志
```

守护进程也提供 HTTP 接口（调试用）：

| 接口 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 `{"ok": true}` |
| `/status` | GET | 返回当前所有待处理交互 |
| `/event` | POST | 接收 Hook 事件 JSON（`{"event": "...", ...}`） |
| `/test` | POST | 注入一条测试提醒 |
| `/stop` | POST | 优雅停止守护进程 |

---

## 7. 配置

编辑 `~/.workbuddy/action-reminder/config.json`（不存在时会用默认值创建）：

```json
{
  "port": 18789,
  "sound": "Ping",
  "quiet_start": 23,
  "quiet_end": 8,
  "enabled": true,
  "max_repeats": 4,
  "total_timeout": 7200,
  "feishu": {
    "enabled": true,
    "target_type": "user",
    "target_id": "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "as": "bot"
  }
}
```

- `port`：守护进程监听端口（默认 `18789`，仅 127.0.0.1）。
- `sound`：macOS 通知提示音名称（设为空字符串 `""` 可关闭声音）。
- `quiet_start` / `quiet_end`：静默时段起止小时（24 制，支持跨午夜，如 `23`→`8`）；仅关闭 macOS 本地提示音，仍弹本地通知、发送飞书提醒并记录待处理状态。设为 `null` 表示不静默。
- `enabled`：总开关，设为 `false` 时守护进程直接退出。
- `max_repeats`：重复提醒次数上限；用尽后状态置为 `ESCALATED`，保留待处理记录但不再弹窗。
- `total_timeout`：单条交互最长等待秒数（默认 7200 = 2h），超时后状态置为 `EXPIRED` 并停止提醒。
- `feishu`：飞书跨设备通知通道配置，详见第 11 节。

---

## 8. macOS 权限与体验说明

- **通知权限**：系统设置 → 通知 → 允许来自「终端」/「WorkBuddy」的通知。
- **点击聚焦（可选增强）**：安装 [terminal-notifier](https://github.com/julienXX/terminal-notifier) 后，点击通知可直接聚焦 WorkBuddy：

  ```bash
  brew install terminal-notifier
  ```

  未安装时自动降级为 `osascript`，通知**照样弹出**，只是点击不会跳转（MVP 行为）。
- **沙箱代理兼容**：WorkBuddy 沙箱会给 `127.0.0.1` 注入 `HTTP_PROXY`，本插件所有脚本已用 `--noproxy 127.0.0.1` + 清除代理环境变量绕过，确保本地通信不被代理吞掉。

---

## 9. 已知限制

1. **会话深链属于内部能力**：WorkBuddy 5.7.6 安装包的静态验证发现，Info.plist 注册了 `workbuddy` 协议，main 的 `showTaskNotification` 使用 `workbuddy://chat/${encodeURIComponent(sessionId)}`，renderer 将 chat 路径映射到 `/task/id`。这不是稳定公开 API，升级后可能变化。2026-10-10 已通过飞书回调按钮的真实点击验收，正确进入目标会话；该结果不代表所有会话 ID 或未来版本均兼容。飞书按钮尝试定位会话，不保证定位某个确认框；macOS 通知仍仅聚焦 App。
2. **仅 macOS**：通知实现基于 `terminal-notifier` / `osascript`，Windows 需改用 `powershell toast`（已在设计阶段评估，本仓库当前仅提供 macOS 实现）。
3. **守护进程需存活**：提醒由常驻守护进程驱动；若进程被杀且未配置 launchd 自启，需等下一次 Hook 事件触发懒启动（或手动 `start`）。

---

## 10. 卸载

```bash
# 1. 停守护进程 + 卸载自启
bash ~/.workbuddy/plugins/cache/local/workbuddy-action-reminder/1.0.0/scripts/action-reminderctl.sh uninstall-launchd

# 2. 在 ~/.workbuddy/settings.json 的 enabledPlugins 中删除 workbuddy-action-reminder@local
# 3. 在 ~/.workbuddy/plugins/installed_plugins.json 中删除对应条目
# 4. 删除插件目录
rm -rf ~/.workbuddy/plugins/cache/local/workbuddy-action-reminder
```

---

## 11. 飞书跨设备通知

macOS 通知要求人坐在电脑前；**飞书通道**让提醒经「飞书私聊 / 群」送达你的手机与桌面客户端，离开 Mac 也能第一时间收到「WorkBuddy 在等你确认」的提醒。

### 11.1 前置条件

- 已安装并登录飞书客户端；`lark-cli`（WorkBuddy 飞书连接器）可用且身份有效。
- `lark-cli` 路径：优先取环境变量 `ACTION_REMINDER_LARK_CLI`，其次取 `feishu.cli_path`（二者均须为本机可执行 CLI 的绝对路径）；未指定时取 `PATH`，最后回退到 `~/.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli`。换机后须确认 CLI 与本机 CPU 架构兼容，指定路径无效时会明确失败，不悄悄改用别的 CLI。
- 脚本使用守护进程传入的 `ACTION_REMINDER_PYTHON`（即 `sys.executable`）；单独调用时优先 `/usr/bin/python3`，再取 `PATH` 中的 Python，避免硬编码迁移前的 managed Python 路径。

### 11.2 配置

在 `~/.workbuddy/action-reminder/config.json` 的 `feishu` 块中设置：

```json
{
  "feishu": {
    "enabled": true,
    "target_type": "user",
    "target_id": "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "as": "bot"
  }
}
```

- `enabled`：`true` 开启飞书通知；`false` 仅用 macOS 通知。
- `target_type`：`user` = 发给指定用户私聊（用 `open_id`）；`chat` = 发到群（用 `chat_id`）。
- `target_id`：飞书 `open_id`（私聊）或 `chat_id`（群）。**为空则跳过飞书通道**，不会报错。
- `as`：发送身份，`bot`（机器人身份，无需用户在线）或 `user`（以你的身份发送）。

> 获取你的 `open_id`：在飞书对话里 `@机器人` 或用 `lark-cli` 查询；也可从 WorkBuddy 飞书连接器的用户档案中取得。

### 11.3 工作原理

`reminder-daemon.py` 在每次 `send_reminder()` 中先弹 macOS 通知，再调用 `notify_feishu(title, body, session_id=...)`。目标取 Hook 提供的 `session_id`（父会话优先语义由 Hook 提供，不改用交互 ID 或猜测子任务 ID）。daemon 按 `encodeURIComponent` 的字符规则编码 ID，将 `workbuddy://chat/<encoded-id>` 作为 `notify-feishu.sh` 第三个可选参数传入；原来的双参数调用仍有效。

脚本先严格校验链接：仅允许固定 `workbuddy://chat/` 前缀和一个规范 UTF-8 编码的路径段，不允许任意 URL、查询参数、片段、控制字符或畸形编码。非法非空链接直接失败且不调用 CLI；无有效会话 ID 则使用原有纯文本提醒，不生成假按钮。卡片采用 Card 2.0、蓝色 header、default 宽度；两个按钮在 `column_set flex_mode=none` 中横排，两列均 `width=weighted, weight=1`，按钮均 `width=fill`，标签保持“打开对应任务 / 已处理”。动态正文转义 HTML 实体，防止链接、@人及标签注入。

两个按钮都通过已部署的可信 CLI 回调通道处理，不再依赖飞书客户端打开自定义 scheme。打开按钮仅携带 `action=open_reminder` 与随机 token/generation；本机按现有 record 的 session_id 构造链接，再以 argv 执行 `/usr/bin/open`（`shell=False`）。回调携带任意 URL、session_id 等多余字段一律拒绝。严格核对记录绑定操作者与当前已配置操作者；旧 token/generation、其他操作者及已 resolve 的记录无效。打开不会 ACK、approve、修改调度或影响其他事项；ACKED 后仍允许打开，直到记录被正常 resolve。

手机点击也只会唤起运行守护进程的 Mac，不会在手机打开 WorkBuddy。系统接受 open 请求不等于 GUI 正确定位；日志明确区分失败和“系统已接受请求，未验证 GUI 定位”。

脚本通过 `lark-cli im +messages-send --msg-type interactive --content ...` 发送卡片，文本降级继续使用 `--text`；配置、发送身份及失败处理保持不变。飞书发送失败只记日志、不阻断主流程（macOS 通知照常）。

### 11.4 验证

已有 daemon 已接入回调监听。历史上的 `console_event_published: blocked` 不是当前状态结论；运行时以真实 ready 标记与存活子进程共同控制门禁。2026-10-10 排障确认 CLI 在 ready 后输出正常的 `[source] feishu-websocket: connected`，旧逻辑误判为故障并关闭 stdin，导致消费者反复退出、按钮灰显。现仅精确放行该正常状态；它本身不能打开门禁，断线、未知诊断、管道 EOF 和进程退出仍关闭门禁并重连。不得通过强制 ready 或无条件停止共享 bus 修复；确需重建失效代理 bus 时，必须遵守下述独占消费者检查流程。

管理员需在发送提醒的同一飞书应用中进入 **事件与回调 → 回调配置**，使用长连接接收，添加 **卡片回传交互 `card.action.trigger`**，保存并按后台提示发布生效。该回调仅支持 bot；CLI 声明 `im:message:readonly` 用于自动读取卡片。无需公网地址或 localhost 回调 URL。应先再次 dry-run 确认不再 blocked，再授权进行实际接收验收；仅进程启动或 ready 标记不等于已完成端到端验收。

daemon 内单实例长期 CLI 消费保持 stdin 管道存活，等 stderr ready 后开放按钮；收到断线诊断后关闭门禁。SDK 若发生未向消费者报告的静默断线，当前门禁无法单凭进程存活检测，应区分本机监听恢复与端到端回调验收。部署时可调用本机 `/stop`，由 launchd 自动重启；须确认新 daemon 和其消费者仍存活且 ready，而非仅看 dry-run。恢复后的新卡自动按当前门禁生成可用按钮，已经发送的灰色旧卡不会自动更新。尚未 ready 的新卡两个按钮明确禁用且不携带 callback，不能仅修改配置启用；旧卡回调在断线期间同样不会执行。本机不提供可伪造操作者身份的 HTTP 打开/确认接口。CLI 自动响应不代表打开或 ACK 业务成功。

离线逻辑仅接受目标 open_id、随机 256 位 reminder token 与当前 generation 的匹配组合；群目标需显式 `ack_operator_id`，未配置则拒绝。确认只将一条记录标为 `ACKED`，停止后续提醒，绝不批准命令。状态以 0600 临时文件 fsync 后原子替换；持久化失败不确认成功。ACKED 记录保留到已有 resolve 生命周期边界，重复 Hook 与重启不会重新提醒；新交互或 resolve 后的新记录使用新 token/generation，旧卡失效。无 interaction_id 时沿用原稳定键，完全相同且无 resolve 边界的 Hook 无法判断是重复还是新事项，保守保持已处理；不凭时间猜测新事项。已在发送中的提醒无法撤回。

#### 回调直连与失效代理 bus 恢复

2026-10-10 进一步定位到：远端 WebSocket 于 13:34 断线，至 14:18 仍重试已经失效的本机代理 `127.0.0.1:51166`。consumer 重启会复用旧 bus，不能改变旧 bus 的代理环境。

- `feishu.ack_callback_no_proxy` 默认 `true`：daemon 仅向回调 consumer 的环境副本设置官方 `LARK_CLI_NO_PROXY=1`，新 bus 继承此设置。显式配置为 JSON 布尔值 `false` 时移除子进程的该开关，恢复 CLI 原有代理选择；不修改父进程、系统代理或消息发送路径。
- 回调 consumer 同时设置 `LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1` 和 `LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1`，避免通知混入长期 stderr；不使用 `--quiet`，保留 ready、断线和丢失诊断。
- 首先执行 `LARK_CLI_NO_PROXY=1 LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 ~/.workbuddy/action-reminder/bin/lark-cli event consume card.action.trigger --as bot --dry-run`，确认身份及前置检查通过。预检不等于真实 WebSocket 已连接或点击业务已验收。
- 重建前先用 `event stop --help` 确认支持 `--app-id`，再用 `event status --json` 核对目标 app 的全部消费者，并通过 PID/PPID 证明仅属于本提醒 daemon。有其他消费者、身份不明或不能按 app 停止时立即停止操作并请求确认。
- 优雅 `POST /stop`，同时仅对 `com.tencent.workbuddy.action-reminder` 执行 `launchctl bootout`，防止 KeepAlive 自动重拉；保留原 plist，不调用会广泛匹配进程的控制脚本 stop。确认 daemon 已退出，重新运行 `event status --json`，只有目标 app 的消费者为零才执行 `event stop --app-id <你的应用ID> --json`。禁止 `--all`、`--force` 和直接杀 bus；如出现新消费者或停止被拒绝，停止并报告，不扩大操作范围。
- 用原提醒 plist 执行 `launchctl bootstrap`，让更新后的 daemon 启动 consumer 并创建直连 bus。核对新 bus PID、新时间的 connected、新增日志中没有旧代理 refused，以及 `/status` 的 `ack_callback_ready=true`。日志只提取安全时间、连接标志和计数，不打印 URL 查询参数、事件内容、token 或凭据。
- 验证只运行离线测试和只读状态，不调用 `/test`、消息发送或修改远端配置；真实按钮点击、Mac GUI 定位和 ACK 持久化仍需用户实点验收。

#### 真实会话验收

2026-10-10 已完成用户侧实点验收：飞书测试卡发送成功，用户确认“打开对应任务”正确进入指定真实会话；此前“已处理”按钮也已通过单条 ACK 持久化和停止重复提醒的验证。本次打开测试沿用已有 ACKED 记录，未恢复重复提醒。上述结论仅覆盖已测会话与本机环境，不保证所有 Hook ID、移动端组合或后续 WorkBuddy 版本均兼容。

以下为后续回归验收的注意事项：

不要为验收向 `/event` 注入当前会话的等待事件：下一次 PostToolUse 可能立即 resolve 掉它。也不要用合成 session_id 伪装真实链接目标。已增加只读 `GET /preview-existing?session_id=<编码后的真实 Hook sid>`：只筛选此 session 的已有记录，返回脱敏正文、状态、key 和该 record 实际链接，不创建记录、不更换 token、不发送通知、不改变调度。无记录返回空数组；不会为凑测试伪造目标。本轮未读取真实状态文件或其他会话。

安全顺序：主代理授权部署后等待回调 ready；由外部终端查询上述只读预览并核对真实目标；暂停在该目标会话继续调用工具，避免 PostToolUse 清理记录。若已有记录仍有效，可由 daemon 已有 `notify_feishu` 发送路径对同一 record 直接发送（不调用 `create_pending` 或 `send_reminder`，不增加次数/重复提醒），必须从活跃 daemon 取得当前门禁及该 record 的 token/generation，不能另起模块伪造 ready。**当前没有暴露直接重发接口**；若主代理无法安全调用活跃实例，就停在只读预览，不要手工拷 token 或制造新测试项。用户自然进入真实等待状态后，由正常首次提醒完成实点验收也可。

独立测试 session 与真实 GUI 目标的映射尚无证据，因此没有实现随意指定 link_sid 的测试捷径。安装包注册了 workbuddy scheme，GUI 导航使用 `workbuddy://chat/<conversationId>`；本轮限定安装包检索未找到 CLI session_id 到 GUI conversationId 的映射，不能断言两个 ID 相同。应人工核对 GUI 实际定位，不扫描其他会话或密钥。

以下旧测试命令仅用于通知通道，不用于真实会话跳转验收，本次未执行。

```bash
# 触发一条测试提醒（会同时弹 macOS 通知 + 发飞书消息）
cd ~/.workbuddy/plugins/cache/local/workbuddy-action-reminder/1.0.0/scripts
./action-reminderctl.sh test
# 或
curl -s --noproxy 127.0.0.1 -X POST http://127.0.0.1:18789/test
```

查看是否发送成功：

```bash
grep "feishu notify" ~/.workbuddy/action-reminder/daemon.log
# 正常会看到：feishu notify: feishu ok= True msg_id= om_xxxxxxxx
```

### 11.5 限制与注意

- 有有效会话 ID 时生成两个等宽按钮；无 ID 时发送文本。Card 2.0 需要飞书客户端 ≥ 7.20。横排 schema 已离线验证，实际渲染仍需客户端验收。
- 本轮绕开客户端 scheme 跳转，改由可信回调在 Mac 打开；离线测试不证明端到端可用。运行中的 daemon 不会自动加载 Python 改动，须由主代理安排部署/重启。由于 shell 脚本会被运行中 daemon 动态调用，新旧代码混用期间打开回调可能被旧 Python 拒绝，不能将未重启状态当作完成部署。
- 后续经授权实点验收应使用真实存在的会话：在电脑飞书点击按钮，核对打开的是该任务且没有自动批准操作；验证目标会话不存在、移动端及客户端拦截时的表现。`/test` 使用合成的 `test-session`，只能测试通知通道，不能证明真实任务定位成功。
- `config.json` 含个人 `open_id`，已被仓库 `.gitignore` 忽略，**不会随仓库提交**，可放心分享仓库。
- 标题使用 `【${TITLE}】` 明确变量边界，避免 `$TITLE】` 在部分 locale/Bash 组合下把中文标点作为变量名的一部分；这不是 `${1:-默认值}` 与 `set -u` 的已知 bug。脚本保留严格未定义变量检查。
- CLI 非零退出码会传回守护进程；`ok:false`、未确认成功、非 JSON 和超时均按失败处理。日志只记录安全状态，不回显 CLI 原始输出、凭据或完整命令。
- 提醒正文按多行显示任务、待确认操作或问题、等待时长和返回 WorkBuddy 的提示，正文不发送完整工作目录或会话 UUID；卡片回调仅携带随机 token/generation、不携带会话 ID；仍仅应发送给可信收件人。内容来自 Hook 实际提供的 `task_title`、`tool_name`、`tool_input.description`、`question/questions`；`UserPromptSubmit.prompt` 可更新内存中的任务摘要。“已点击 / 继续 / 好的”等短确认以及仅含 `[地址已隐藏]` 的截图路径占位不会成为任务名称，也不会覆盖已有有效摘要；历史记录里的此类标题展示时同样过滤。缺失原始任务会显示“未获取原始任务名称”，不扫描会话文件，也不推测任务。
- 工具命令及完整参数不发送；自然语言摘要会做常见凭据、路径、命令和长标识脱敏。摘要不等同于完整会话内容，具体操作请回 WorkBuddy 核对。
- 离线回归测试（不发送飞书或 macOS 消息）：`/usr/bin/python3 -m unittest discover -s scripts -p test_reminder.py -v`。

---

## 12. 开源仓库

已发布至 GitHub：<https://github.com/KimberMIN/workbuddy-action-reminder>（main 分支）。

发布环境对 GitHub 直连不稳定，实际通过 `gh-proxy.com` 镜像中转完成 push——`api.github.com` 经沙箱代理创建仓库，git 推送经 gh-proxy 转发 `Authorization` 头鉴权。

---

## 13. 许可证

MIT © KimberMIN
