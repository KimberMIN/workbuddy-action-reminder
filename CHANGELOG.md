# Changelog

## 1.1.0 — Agent Action Reminder

### 新增
- 对外名称由 WorkBuddy Action Reminder 改为 Agent Action Reminder。
- DeepSeek Harness 原生 Host 插件 bundle：被动观察 session 事件，重建 live-session 审批与原生 ask_user_question 状态，不注册 answerer、不自动批准。
- 完整快照接收与按宿主/bridge/会话/交互隔离；15 秒 lease，断线暂停，恢复保留 ACK；重启后先等待新快照。
- Harness macOS 聚焦与飞书“打开 Harness”首页按钮，不伪造未经验证的会话 deep-link。
- 完整快照验证、JSON 大小限制、可选本地 bridge token 与跨站写入拒绝。
- Harness 状态/接口/卡片测试、原生 JS 插件测试与运行配置隔离测试。

### 兼容与修复
- 新安装使用 ~/.agent-action-reminder；已有旧配置自动沿用 ~/.workbuddy/action-reminder；支持显式 ACTION_REMINDER_STATE_DIR。
- 保留旧 WorkBuddy 技术插件 ID、Hook 接口、旧主键和已有 ACK 数据。
- 新 launchd label 为 com.agent-action-reminder，存在旧部署时沿用；拒绝双实例配置，不再用 pkill 广泛匹配进程。
- 端口严格验证，健康检查识别服务；plist 使用安全 XML 序列化与私有原子写入。
- Harness 通知网络 I/O 不占用快照协调锁；坏 Unicode/事件类型不会破坏待办快照。
- 原 WorkBuddy 设计和验收记录移至 docs/workbuddy-history.md；新 README 区分支持、配置与真实部署状态。

### 明确限制
- Harness 以 0.2.0-rc.2 / hostProtocolVersion 4 源码合约验证，未来版本需回归。
- 不承诺覆盖 ask_user 别名、request_secret 或第三方自定义卡片；未扫描冷会话。
- 每个 bridge_id 同时仅一个 writer；不支持并发实例共享标识。
- 离线验证不替代真实安装、授权弹窗、飞书点击与 GUI 端到端验收。
