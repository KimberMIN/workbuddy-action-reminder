# v1.1.0 验证记录

## 离线验证

- Python：`python3 -m unittest discover -s scripts -p 'test*.py' -q`，**118/118 通过**。
  - 原 WorkBuddy 回归：70 项。
  - 运行配置与控制脚本：17 项。
  - Harness 快照、租约、顺序保护、接口与卡片：26 项。
  - 确定性并发回归：5 项。
- Node：`node --test integrations/deepseek-harness/test/*.test.js`，**15/15 通过**。
- 所有 Shell 脚本逐个 `bash -n` 通过，`git diff --check` 通过。
- 全部通知/CLI/原生打开动作使用 mock。HTTP 测试仅绑定临时 loopback 端口，不使用真实 daemon。

## 独立审查与修复

1. 旧快照乱序覆盖：要求 boot_id 与严格递增 sequence；旧代次退休、15秒租约交接及 host 绑定，拒绝请求不续租也不修改状态。
2. WorkBuddy 慢通知持锁：首次发送在锁内原子领取，锁外 I/O；Harness 快照与 WorkBuddy 解除不再被通知等待阻塞。首次与调度两种抢先顺序均验证只发送一次。
3. 非法 Unicode 与事件类型：完整快照先验证 UTF-8 与字段类型，避免部分修改；非字符串旧 Hook 事件不再导致 HTTP 处理异常。
4. 异步启动测试稳定性：用临时 FIFO 与事件屏障替代固定 sleep，原不稳定用例连续8次通过。

## Harness 合约核对

以实际 DeepSeek Harness 0.2.0-rc.2 / hostProtocolVersion 4 源码核对：被动 session Hook、live observation、timed/PTC/晚到回复、bundle manifest/patch/apply/inject/effect 契约一致。详细依据见 [Harness 说明](../integrations/deepseek-harness/README.md)。这不是对未来版本的兼容承诺。

## 尚未完成的真实环境验收

本次没有修改用户 settings/profile、安装 Host 插件、修改 ASAR、控制真实 launchd/daemon 或发送真实飞书消息，因此不宣称以下项目已通过：

- Plugin Manager 的真实 activation 与替换版本后的重启加载。
- 原生确认弹窗与问题回答全链路。
- 飞书回调 ready、用户实际点击、Mac 聚焦和浏览器登录。
- 多个真实并发待办、断线重连与长历史性能。

`ask_user` 别名、`request_secret` 与第三方自定义卡片不在本版已验证支持范围。Harness 卡片只打开首页，不实现未经证实的会话深链。
