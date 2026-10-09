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
| `Notification` | `idle_prompt` | 空闲/等待输入 | ✅ |
| `PermissionRequest` | — | 插件级权限请求 | ✅ |
| `Elicitation` | — | 等待用户做选项选择 | ✅ |
| `ElicitationResult` | — | 用户已选择 → **清除** | 清除 |
| `UserPromptSubmit` | — | 用户已提交 → **清除** | 清除 |
| `PostToolUse` | — | 工具调用后（去抖，避免误报） | — |
| `SessionEnd` | — | 会话结束 → **清除该会话全部** | 清除 |
| `PermissionDenied` | — | 权限被拒 → **清除** | 清除 |

清除逻辑优先按 `interaction_id` 精确匹配；仅当未提供交互 ID 时才按 `session_id` 清除整个会话，包含 `WAITING`、`ESCALATED` 和 `EXPIRED` 状态。多个并行任务互不干扰。

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

1. **无官方深链协议**：目前 WorkBuddy 没有「打开指定会话/指定交互」的 URI Scheme，因此点击通知只能聚焦 App，不能精准跳转到对应确认框。这是产品侧能力缺失，待官方开放 deep-link 后可升级。
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

`reminder-daemon.py` 在每次 `send_reminder()` 中先弹 macOS 通知，再调用 `notify_feishu(title, body)`。后者执行同目录的 `notify-feishu.sh`，由脚本解析 `config.json` 的 `feishu` 配置并通过 `lark-cli im +messages-send` 发出。飞书发送失败只记日志、不阻断主流程（macOS 通知照常）。

### 11.4 验证

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

- 飞书消息为**文本内容**（标题 + 正文），不含点击深链；点击通知聚焦 WorkBuddy 仍是 macOS 通知的能力。
- `config.json` 含个人 `open_id`，已被仓库 `.gitignore` 忽略，**不会随仓库提交**，可放心分享仓库。
- 标题使用 `【${TITLE}】` 明确变量边界，避免 `$TITLE】` 在部分 locale/Bash 组合下把中文标点作为变量名的一部分；这不是 `${1:-默认值}` 与 `set -u` 的已知 bug。脚本保留严格未定义变量检查。
- CLI 非零退出码会传回守护进程；`ok:false`、未确认成功、非 JSON 和超时均按失败处理。日志只记录安全状态，不回显 CLI 原始输出、凭据或完整命令。
- 提醒正文按多行显示任务、待确认操作或问题、等待时长和返回 WorkBuddy 的提示，不发送完整工作目录或会话 UUID。内容来自 Hook 实际提供的 `task_title`、`tool_name`、`tool_input.description`、`question/questions`；`UserPromptSubmit.prompt` 可更新内存中的任务摘要。缺失信息会明确标记未提供，不扫描会话文件，也不推测任务。
- 工具命令及完整参数不发送；自然语言摘要会做常见凭据、路径、命令和长标识脱敏。摘要不等同于完整会话内容，具体操作请回 WorkBuddy 核对。
- 离线回归测试（不发送飞书或 macOS 消息）：`/usr/bin/python3 -m unittest discover -s scripts -p test_reminder.py -v`。

---

## 12. 开源仓库

已发布至 GitHub：<https://github.com/KimberMIN/workbuddy-action-reminder>（main 分支）。

发布环境对 GitHub 直连不稳定，实际通过 `gh-proxy.com` 镜像中转完成 push——`api.github.com` 经沙箱代理创建仓库，git 推送经 gh-proxy 转发 `Authorization` 头鉴权。

---

## 13. 许可证

MIT © KimberMIN
