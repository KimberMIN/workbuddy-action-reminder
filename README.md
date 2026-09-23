# WorkBuddy Action Reminder

> 当 WorkBuddy / CodeBuddy 长任务进入「等待用户确认 / 选择选项」状态时，触发 **macOS 系统级置顶提醒**并定期重复，直到用户处理或会话结束；点击通知可聚焦 WorkBuddy。

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
└───────────┬─────────────┘
            │ 调用
            ▼
┌─────────────────────────┐
│   notify-macos.sh        │  terminal-notifier（优先）→ osascript（兜底）
└───────────┬─────────────┘
            │ 系统通知
            ▼
       macOS 通知中心（置顶 / 重复）
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

清除逻辑按 `session_id` 或 `interaction_id` 精确匹配，多个并行任务互不干扰。

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
  "reminder_intervals": [180, 600, 900, 900],
  "max_wait_seconds": 7200,
  "quiet_hours": {"start": "23:00", "end": "08:00"},
  "app_bundle": "com.tencent.workbuddy.mac"
}
```

- `reminder_intervals`：每次提醒的间隔（秒），数组用尽后沿用最后一个值。
- `max_wait_seconds`：单条交互最长等待时间，超时后停止提醒（避免无限骚扰）。
- `quiet_hours`：静默时段，期间不打扰，但仍记录待处理状态。
- `app_bundle`：点击通知时尝试聚焦的 App Bundle ID（需 terminal-notifier 才生效）。

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

## 11. 许可证

MIT © 闵金波 / 晓葆科技

---

## 12. 开源仓库

已发布至 GitHub：<https://github.com/KimberMIN/workbuddy-action-reminder>（main 分支）。

发布环境对 GitHub 直连不稳定，实际通过 `gh-proxy.com` 镜像中转完成 push——`api.github.com` 经沙箱代理创建仓库，git 推送经 gh-proxy 转发 `Authorization` 头鉴权。
