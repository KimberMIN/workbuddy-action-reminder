# Agent Action Reminder

**智能体待办提醒 · macOS + 飞书 · 只提醒，不自动批准**

原名 **WorkBuddy Action Reminder**。从 v1.1.0 起使用不绑定宿主的名称，同时支持 **WorkBuddy / CodeBuddy** 和 **DeepSeek Harness**。当智能体真正等待权限确认或用户回答时，通过 macOS 和飞书跨设备提醒；处理后停止，避免长任务一直卡在等待状态。

作者：Kimber Min · MIT © KimberMIN

## 支持范围

| 宿主 | 接入方式 | 覆盖 | 打开行为 |
|---|---|---|---|
| WorkBuddy / CodeBuddy | 原有插件 Hook | 实际到达插件的权限请求、Elicitation、精确 AskUserQuestion | 飞书回调尝试 `workbuddy://chat/<session_id>`；macOS 聚焦 WorkBuddy |
| DeepSeek Harness | 原生 Host 插件，被动监听已提交 session 事件 | `approval/asked` / `approval/decided`、原生 `ask_user_question` | 飞书回调打开本机 Harness **首页**，用户手动选会话；macOS 聚焦 Harness |

**不要把源码兼容与离线测试等同于已部署。** Harness 适配以 `0.2.0-rc.2 / hostProtocolVersion 4` 的实际安装包源码为依据；没有修改 ASAR，也没有自动安装 Host 插件、修改用户 profile 或发送真实飞书测试消息。真实授权弹窗、按钮点击、浏览器登录状态仍需部署后验收。

Harness 的 `ask_user` 别名、`request_secret` 和第三方卡片尚无已验证事件契约，**本版不承诺覆盖**。普通回答、空闲、任务结束与普通工具调用不触发提醒。WorkBuddy GUI 未向插件发出的审批事件也无法由本项目补齐。

## 工作方式

```text
WorkBuddy / CodeBuddy ── Hook ── hook-dispatch.sh ── /event ──┐
                                                          │
DeepSeek Harness ── 原生被动 Host 插件 ── /harness/snapshot ──┤
                                                          ▼
                                        reminder-daemon.py（127.0.0.1）
                                        来源隔离 · 去重 · 持久化 · 重复提醒
                                                │                  │
                                                ▼                  ▼
                                           macOS 通知        飞书消息/双按钮卡片
```

- 首次提醒后，默认再按 **3、10、15、15 分钟**间隔提醒（最多 4 次重复）；总等待超时 2 小时。
- WorkBuddy 继续采用已有事件精确解除逻辑；旧无 ID 泛权限通知仅首次提醒，不误推断具体请求生命周期。
- Harness 每 5 秒发完整 live-session 等待快照，事件变动后约 100ms 合并发送。daemon 由调度器在约 3 秒内开始首次通知（发送耗时另计）。
- Harness 去重键包含 **宿主来源 + bridge_id + session_id + interaction_id**；不会和 WorkBuddy 同名会话互相解除。
- 快照中消失的请求精确清除；**超过 15 秒未收到快照只暂停，不视作批准或已完成**。恢复后同一请求保留 ACK、次数与 token；daemon 重启后也要等新快照才恢复 Harness 通知。
- 重建失败、部分读取、超量列表不发送空快照。只读 live sessions，不扫描冷历史、不启动 Agent、不抢占审批 answerer。

## 快速开始

### 1. 依赖与运行目录

- macOS、Python 3；Harness 插件使用宿主 Node 运行时，离线 Node 测试建议 Node 22+。
- 飞书通知需要已配置应用与身份的 `lark-cli`；本项目不包含 CLI、账户或密钥。
- 可选 `terminal-notifier`（`brew install terminal-notifier`）；没有时使用 macOS `osascript` 通知，点击不保证聚焦。

在稳定位置克隆仓库；仓库历史地址暂保留，目录可以使用新名称：

```bash
git clone https://github.com/KimberMIN/workbuddy-action-reminder.git agent-action-reminder
cd agent-action-reminder
```

**状态目录选择顺序**：

1. 显式 `ACTION_REMINDER_STATE_DIR`；
2. 旧 `~/.workbuddy/action-reminder/config.json` 存在时继续使用旧目录；
3. 否则 `~/.agent-action-reminder`。

旧状态、ACK、飞书配置不自动搬迁或覆盖。修改 Python 代码后需要重启 daemon；更新磁盘文件不等于运行实例已加载新版本。

### 2. 配置 daemon

在上述状态目录手工创建 `config.json`，不要覆盖已有配置；已有配置只合并需要的字段。示例：

```json
{
  "port": 18789,
  "enabled": true,
  "sound": "Ping",
  "quiet_start": null,
  "quiet_end": null,
  "max_repeats": 4,
  "total_timeout": 7200,
  "harness": {
    "enabled": true
  },
  "feishu": {
    "enabled": true,
    "target_type": "user",
    "target_id": "ou_替换为你的open_id",
    "as": "bot",
    "ack_callback_enabled": true,
    "ack_callback_no_proxy": true
  }
}
```

`harness.enabled` **默认 false**，启用后才接收快照。建议在 `harness.token` 和 Harness 插件 config 中设置相同的独立随机本地令牌；不要使用 Harness 登录 URL token 或业务密钥。未设置时仅有 loopback/本机用户信任边界，其他本机进程可能伪造提醒，但不能通过本项目批准原生操作。

`port` 必须是 JSON 整数 `1–65535`；无效配置会明确失败，不悄悄控制另一个端口。配置中的 `feishu.cli_path` 或环境变量 `ACTION_REMINDER_LARK_CLI` 可指定可执行 CLI 的绝对路径；未指定时使用 PATH，并兼容旧 WorkBuddy CLI 位置。

`quiet_start` / `quiet_end` 是小时，可跨午夜；仅静音本地提示音，仍显示通知及发送飞书消息。群通知使用 `target_type: "chat"` 与 `chat_id`，且须设置 `ack_operator_id` 才允许指定用户操作按钮。

```bash
bash scripts/action-reminderctl.sh start
bash scripts/action-reminderctl.sh status
```

### 3A. WorkBuddy / CodeBuddy

继续按宿主的本地插件安装机制加载仓库根目录的 `.codebuddy-plugin/plugin.json` 与 `hooks/hooks.json`，安装后重启或重载宿主。已有用户更新原安装位置，不重复注册同一 Hook。

为避免已有 `enabledPlugins` 注册失效，**WorkBuddy 技术插件 ID 暂保留 `workbuddy-action-reminder`**，版本升级到 1.1.0；这是兼容标识，不是新的对外项目名称。旧 launchd label 与状态目录同理。新用户不需要复制原开发机器的绝对路径或注册表。

完整事件矩阵、原开发环境的限制与回调排障记录见 [WorkBuddy 历史说明](docs/workbuddy-history.md)。历史“已安装 / 已验收”仅描述当时环境，不代表你的安装状态。

### 3B. DeepSeek Harness

安装 [原生 Host 插件 bundle](integrations/deepseek-harness/README.md)。它位于 `integrations/deepseek-harness`，不是 WorkBuddy 的 Hook 注册文件。

1. 按需修改 bundle 的 `cordis.patch.yml` 配置：`bridge_id`、`host_url`、`daemon_url`，以及可选本地 bridge `token`。
2. 在 Harness 的 **Plugin Manager** 安装该目录的绝对路径，按宿主提示授权并启用。
3. 核对实际 activation 状态；若提示需重启，完成重启后再验收。

默认 daemon 地址 `http://127.0.0.1:18789`，Harness 地址 `http://127.0.0.1:19387`，不要混淆。每个实例使用不同 `bridge_id`，同实例升级保持其稳定；**一个 bridge_id 同时只能有一个 writer**。

Host 插件具有宿主进程权限，不受 Agent workspace sandbox 限制，安装前应审核。它只观察已提交事件并向固定 loopback HTTP 端口发送等待快照，不执行 shell、不读取凭据文件、不修改原生权限或会话。

## 飞书按钮与隐私

卡片提供两个等宽按钮：

- **打开对应任务 / 打开 Harness**：在运行 daemon 的 Mac 上打开；手机点击也是唤起该 Mac，不是在手机里运行智能体。Harness 目前只打开首页，**没有实现未经验证的会话 URL**，浏览器仍须有有效登录状态。
- **已处理**：只停止该条提醒，**不等于批准授权、回答问题或终止任务**。真正处理必须回宿主进行。

按钮通过可信 `lark-cli event consume card.action.trigger` 回调处理。飞书应用需启用长连接卡片回传事件、发布配置，并满足 CLI 所需权限。只有真实 ready 标记及存活消费者同时成立才启用按钮；未连接时按钮禁用，不能用配置强行绕过。旧灰卡不会自动更新。

回调严格匹配操作者、256 位随机 token 与当前 generation；不接受调用方 URL、session_id 或任何批准动作。状态原子持久化，失败不确认 ACK。不要停止共享飞书 event bus 来修复单个消费者。

通知只使用简短问题/原因/任务摘要并做常见敏感字段脱敏；不发送完整工具参数、工作目录或原始会话历史。自动脱敏并非绝对保密保证，请仅配置可信收件人。Harness 当前没有额外扫描用户消息来猜测任务标题，缺少标题时如实显示未获取。

## 管理、自启与卸载

```bash
bash scripts/action-reminderctl.sh start
bash scripts/action-reminderctl.sh stop
bash scripts/action-reminderctl.sh restart
bash scripts/action-reminderctl.sh status
bash scripts/action-reminderctl.sh health  # 仅退出码
bash scripts/action-reminderctl.sh log
bash scripts/action-reminderctl.sh test    # 会真的发通知，按需执行
bash scripts/action-reminderctl.sh install-launchd
bash scripts/action-reminderctl.sh uninstall-launchd
```

自启请在有用户 launchd 权限的普通终端执行。新部署使用 `com.agent-action-reminder`，检测到旧 `com.tencent.workbuddy.action-reminder` 时沿用旧项；若两项并存，拒绝自动操作。plist 原子生成且正确转义路径；停止时不使用广泛 `pkill`。

卸载：先停服务/卸载自启，再在各宿主禁用并移除对应插件。状态目录由用户按需保留或删除。不要随意删除其他插件注册表或覆盖整个 settings/profile。

## 本机接口

| 方法/路径 | 功能 |
|---|---|
| `GET /health` | 含 `service=agent-action-reminder`、版本 |
| `GET /status` | 等待项，Harness 断连时 `paused=true`；隐藏回调 token |
| `GET /preview-existing?session_id=…` | 只读预览，不制造待办、不发送 |
| `POST /event` | 旧 WorkBuddy Hook，拒绝其他 source 冒充 |
| `POST /harness/snapshot` | 完整 Harness 快照，可选 Bearer 本地 token；仅显式启用 |
| `POST /test` | 合成通知通道测试，不能证明真实会话定位 |
| `POST /stop` | 优雅停止 daemon |

仅监听 `127.0.0.1`，不是公网服务。写入接口拒绝浏览器 Origin / cross-site 请求；快照仅接受 JSON，限制大小并在完整验证后更新。不要反向代理到公网。断线、静默暂停和普通已完成状态必须区分。

## 开发与验证

v1.1.0 离线验证：**118 项 Python + 15 项 Node 测试全部通过**。详见 [验证记录与尚未验收项](docs/verification.md)。

```bash
python3 -m unittest discover -s scripts -p 'test*.py' -v
node --test integrations/deepseek-harness/test/*.test.js
for file in scripts/*.sh; do bash -n "$file" || exit 1; done
git diff --check
```

离线测试使用临时目录和 mock 通知，不发送飞书消息、不修改真实服务。覆盖原有 WorkBuddy 行为、配置迁移、Harness 状态重建/快照/租约/隔离、按钮安全与故障处理。

真实端到端验收应使用独立会话，分别触发权限确认、拒绝、问题回答与两个并发请求；处理一个不能清除另一个；关闭插件应在 15 秒后暂停；重启不能恢复已 ACK 的重复提醒。**不要为验收自动批准危险命令、降低沙箱或创建真实密钥。**

## 版本与项目资源

- [v1.1.0 变更记录](CHANGELOG.md)
- [Harness 接入、安装与源码依据](integrations/deepseek-harness/README.md)
- [WorkBuddy 历史记录](docs/workbuddy-history.md)
- [GitHub 仓库（历史地址）](https://github.com/KimberMIN/workbuddy-action-reminder)
- [飞书设计文档](https://xiaobaotop.feishu.cn/docx/D9bWdJuUGosGBLxQwnvcGcnUnLc)

MIT © KimberMIN
