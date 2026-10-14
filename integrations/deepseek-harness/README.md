<p><img src="assets/icon-256.png" width="96" height="96" alt="Agent Action Reminder icon"></p>

# DeepSeek Harness 原生被动提醒插件

这是 **Host 插件 bundle**，不是浏览器抓屏脚本，也不修改 Harness 安装包。插件只旁观已有确认请求，将当前等待项发给 Agent Action Reminder；所有批准、拒绝、答案与密钥输入仍在 Harness 完成。

## 支持范围与验证状态

以本机实际 ASAR 中 `@deepseek-ai/dsh` **0.2.0-rc.2 / hostProtocolVersion 4** 的源码为依据：

- 支持原生 `approval/asked` / `approval/decided`，包括允许一次、拒绝、取消和不可用结果。
- 支持原生 `ask_user_question`：legacy blocking、timed pending、恢复后的 unknown outcome、PTC pending 子调用、晚到用户回复。
- **不支持未经证实的 `ask_user` 别名、`request_secret` 或第三方插件自定义卡片**。不能把自然语言疑问句当作原生待办。
- **仅跟踪当前 live sessions**。不扫描全部冷历史、不启动或恢复 Agent；live session 被卸载后不再作为当前可交互待办发送，重新成为 live 后再重建。
- 已做离线事件/接口/lifecycle 合约测试；**尚未在真实用户 profile 安装或做真人点击端到端验证**。安装、用户配置、ASAR 都没有被修改。

## 安装（由用户显式执行；本研究未执行）

1. 先启动并配置 Agent Action Reminder daemon；启用其 `harness.enabled`。默认 daemon 是 `http://127.0.0.1:18789`，Harness 是 `http://127.0.0.1:19387`，二者不是同一服务。
2. 按需要修改本 bundle 的 `cordis.patch.yml` 中 **config 整体对象**：

   ```yaml
   bridge_id: desktop
   host_url: http://127.0.0.1:19387
   daemon_url: http://127.0.0.1:18789
   # 可选，仅用于本地 Reminder bridge，须与 daemon 的 harness.token 相同。
   # token: 一个独立生成的本地令牌
   ```

   同一宿主保持稳定 `bridge_id`，不同实例必须不同；允许 1–64 个英文字母、数字、下划线、连字符。不要粘贴 Harness 启动 URL 的 token、浏览器 cookie 或任何业务密钥。
3. 使用 Harness 自己的 **Plugin Manager → 安装 bundle**，传本目录的绝对本地路径/安装 spec。对应原生工具操作是：

   ```json
   {"action":"install_bundle","target":"/absolute/path/to/workbuddy-action-reminder/integrations/deepseek-harness","enabled":true}
   ```

   这只是原生 `plugin_manager` 的参数示例，不是 shell 命令。安装具有 profile 全局影响、需要该工具要求的授权；若当前 session 禁止提权，请由用户在 UI 操作，不绕过限制。插件本身没有依赖或 install/build scripts，不能额外批准无关 build scripts。
4. 本版本的 UI 入口为侧栏「插件 → 添加插件」，填写本地绝对路径；UI 安装完成后还须点击「立即启用」，不能把仅下载完成当作已启用。若使用 CLI，须先保存任务并**完全退出 Desktop**，再在外部终端执行：

   ```bash
   "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" \
     plugin --profile desktop add "/absolute/path/to/private/deepseek-harness" --ignore-scripts
   ```

   使用用户私有配置的 bundle 路径，不要误用缺少本地 token 的示例目录。官方 launcher 已随应用提供，不要求全局安装 dsh；新增且有效的 bundle 会自动进入 profile 的启用列表，已有但被禁用的包不会因此自动重新启用。执行后重新打开 Desktop。不要在仍运行的 Desktop 旁边直接运行 CLI 写 profile。
5. 查看 Plugin Manager 实际 activation 状态；`failed` / `overridden` / `restart-required` 都不能当作已生效。替换已安装 JS 版本通常需要重启；不承诺修改目录自动 HMR。
6. 在独立测试会话发起真正需要用户确认的操作，检查提醒只在原生 pending 时出现；手工批准/拒绝后检查停止。不要为测试降低 sandbox、自动批准危险操作或创建真实密钥。

此插件不懒启动 daemon，也不创建常驻子进程。daemon 未运行、bridge 未开启、token 不匹配或状态读取失败时，只输出不含 payload 的摘要警告并重试。

## 接口与生命周期

每 5 秒心跳；相关 session 事件后 100ms debounce。启动时先重建当前全部 live sessions，**不是先发空列表**。完整读取失败、会话集合在读取中变动、超过 512 项时不发送部分 baseline，以免误清真实 pending。

```text
POST http://127.0.0.1:18789/harness/snapshot
Content-Type: application/json
Authorization: Bearer <可选独立本地bridge token>

{
  "bridge_id": "desktop",
  "boot_id": "每次插件激活的随机UUID",
  "sequence": 1,
  "host_url": "http://127.0.0.1:19387",
  "pending": [
    {
      "session_id": "session-...",
      "interaction_id": "approval:原生审批id",
      "kind": "permission",
      "tool_name": "bash",
      "message": "原生reason（最多4096字符）"
    },
    {
      "session_id": "session-...",
      "interaction_id": "question:原生callId或subCallId",
      "kind": "question",
      "tool_name": "ask_user_question",
      "question": "原生问题文本（最多4096字符）"
    }
  ]
}
```

- 协议状态隔离键应包含 `source=deepseek-harness`、`bridge_id`、`session_id`、`interaction_id`。两个请求类型加前缀，避免相同 callId 冲突。
- `boot_id` / `sequence` 必填。相同 boot 只接受严格递增的正整数序号，乱序或重复快照不清理、不续租。新 boot 必须等待旧 lease 过期后接替（插件重载可能延迟约 15 秒）；被替换的旧 boot 在本 daemon 进程内始终拒绝。同一 bridge 的 host_url 不允许中途重绑，改端口请用新的 bridge_id 或重启 daemon。这不是远端身份认证；仍必须保证每个 bridge_id 同时仅一个 writer，不同实例不可复用 default。
- daemon 对成功完整快照做 authoritative reconcile；15 秒没有更新则暂停陈旧提醒，而不是认定用户批准。恢复同 id 保留用户 ACK；真正 resolved/removed 项由新快照清除。
- 整个重建与 HTTP 发送共享 2.8 秒 AbortSignal；请求串行；不跟随 redirect，不发 Origin header。不接受远程 host、localhost 别名、HTTPS、路径、URL凭据、query token 或 fragment。
- 插件卸载/Host shutdown 会清定时器、移除监听、abort 在途请求；**不发空快照伪装成全部完成**，由 lease 暂停。
- 事件回调只调度任务，不等待网络，不 append session log、不修改原生会话。只读 observation 在 finally 中 `Symbol.dispose`。
- 使用 session 对象与 `session.seq` 缓存：未变化的会话不会每 5 秒重复读取历史。发生变化时重新 fold 会话完整 owned event prefix，因此超长活跃会话仍有 O(history) 成本；超过时间预算保留旧状态并暂停，而不发送不完整数据。后续可以改增量 fold，但需要保留 fork/restore 一致性测试。

## Harness 首页链接，不伪造会话 deep-link

已查源码的原生导航是 Client `ctx.uiWorkspace.openSession(sessionId)` → `replaceMain`，所选会话持久化于 `dsh.sessions.current`，不是 URL query/hash router。没有证据支持 `/?session=...`、`/sessions/:id` 或 `/session/:id`。

因此卡片只打开配置的 **Harness 首页**；通知展示简短问题/原因，不跨设备展示完整会话标识。浏览器还须已有 Harness 登录 cookie；不能把启动 token 填进卡片 URL。将来若另写 Client 插件，可明确实现自定义路由再调用 `uiWorkspace.openSession`，但那不是本版原生 URL 能力。

## 为什么不用 SSE/通用 pending API

本版本不是假设中的 `permission pending/resolved` SSE：

- WebSocket physical mux 是 `/api/remote.mux`；HTTP RPC 是 `/api/<namespace>/<method>`。
- `session/projections({sessionId})` 可只读取得 `{asOfSeq, values}`，但 `userQuestions` 只覆盖带 `timeout` schema 的 timed questions，不覆盖 legacy blocking questions，也不是全部原生 pending 的 REST snapshot。
- `approval/request` 与 `user-questions/request` 是 **waterfall answerer**。把 `$events` stream 当作被动订阅者可能抢占/阻塞回答链；本插件绝不订阅它们。
- `session/follow` 可在 opening snapshot 后 promote 普通冷 session，不适合不加区分地作为纯只读 watcher。
- 原生 API/WS 通过根 `/?token=...` 换取 authority-bound、HttpOnly、SameSite=Strict 的签名 cookie，并检查 Host/Origin；不是已证实的 Bearer API。直接给 daemon 整个 Harness cookie 增加攻击面，没有必要。

## 安全边界

Host 插件代码运行于 Harness Host，**不受 Agent workspace sandbox 保护**。安装必须由用户审核；本插件不读取配置/凭据文件、不执行 shell、不写安装包、不给 daemon 传 Harness cookie。请求中的问题与 reason 本身仍可能包含路径或业务敏感数据；默认只发本机，由 daemon 二次脱敏，用户须评估外部通知渠道。

未配 bridge token 时只有 loopback/本机信任边界，其他本机进程仍可能伪造/覆盖 pending；建议给 daemon 和插件配置相同的独立 token。Token 是本地配置里的 secret，提交代码前检查不要写入真实值。即使提示被伪造，通知端也只能提醒，不能替用户作原生授权。

## 精确源码依据

以下路径均相对于从真实 ASAR 提取的 `dsh/node_modules/@deepseek-ai/`，行号对应本次安装版本的文本，不是猜测未来公开 API：

| 能力 | 包内文件与行号 |
|---|---|
| 构建版本 | `dsh/desktop-runtime.json:1–8`（此一项相对 ASAR 根） |
| 审批 id / payload / 全部结果 / policy | `dsh-user-approval/lib/index.js:29–37,128–176` |
| 原生 post-commit 被动 hook `(session,event)` | `dsh-session/lib/index.js:1404–1410,1465–1473` |
| live store service 名 `sessions` | `dsh-session/lib/index.js:1621` |
| created / disposed / live list | `dsh-session/lib/index.js:1782–1814,1856–1869` |
| live-only observation / cursor / dispose | `dsh-session-query/lib/index.js:475–505` |
| timed question state机与 PTC/late-reply | `dsh-user-questions/lib/types/projection.js:180–270` |
| timed projection key与数据结构 | `dsh-user-questions/lib/types/projection.js:50–75,280–298` |
| approval Client pending 生命周期 | `dsh-client-ui-approval/lib/client.js:283–309,343–357` |
| answerer forwarding allowlist | `dsh-api-remotes/lib/index.js:17–125,147–157` |
| WS/RPC transport与admission | `dsh-api-gateway/lib/index.js:11–16,623–643` |
| cold-safe projections RPC | `dsh-api-session-controller/lib/index.js:3143–3175` |
| follow可promote风险 | `dsh-api-session-controller/lib/index.js:1383` |
| Cookie / Host / Origin认证 | `dsh-client-connection/lib/index.js:205–219,284–297,388–442` |
| 会话导航非URL | `dsh-client-ui-workspace/lib/client.js:761,821–822,894–914` |
| bundle manifest / patch / 导出 / 安装 | `dsh-agent-preset/skills/cordis-plugin-development/references/host-plugin.md:3–27,47–60` |
| 原生安装工具与Host执行风险 | `dsh-plugin-manager/lib/types/tools.js:14–22,30–36,69–76` |

## 离线验证

```bash
cd integrations/deepseek-harness
node --test test/*.test.js
```

覆盖所有审批结果、policy-never瞬时关闭、并发隔离、legacy/timed/PTC问题、超时与晚到回复、fork继承排除、自然语言/secret不误识别、启动重建、缓存、dispose、读失败/overflow不误清、URL/token校验、串行发送及关闭清理。测试不访问真实 Harness 或 daemon，也不需要密钥。
