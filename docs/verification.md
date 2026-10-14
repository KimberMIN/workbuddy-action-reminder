# v1.1.1 验证记录

## 离线验证

- Python：`python3 -m unittest discover -s scripts -p 'test*.py' -q`，**123/123 通过**。
  - 原 WorkBuddy 回归：70 项。
  - 运行配置与控制脚本：17 项。
  - Harness 快照、租约、顺序保护、接口与卡片：26 项。
  - 确定性并发回归：5 项。
  - 图标尺寸、PNG/ICO完整性、自包含SVG及原生manifest：5 项。
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

## 真实环境首次验收与修复进展

- 原生 bundle 已在 desktop profile 中注册并选入，实际服务读取到带唯一标签的真实 Harness `ask_user_question`，且只产生一条 Harness pending。
- 用户点击首轮飞书“打开 Harness”后，浏览器显示 `dsh web authentication required`；该轮未通过。
- 修复 callback 后，Harness 固定调用 `/usr/bin/open -b com.deepseek.dsh` 唤起已安装桌面应用；WorkBuddy 会话 deep link 保持不变，不添加登录 token 或伪造 session URL。回归测试断言随之更新，完整离线回归仍为123项Python、15项Node通过。
- 经用户确认空闲并授权，先备份并原位部署桌面启动修复，再更新至daemon 1.1.1；保留旧 WorkBuddy 入口与 launchd。最终新 PID=21744，health=1.1.1、callback ready=true。
- 用户首次点开飞书卡片进入 `dsh web authentication required` 网页。修复采用固定 `/usr/bin/open -b com.deepseek.dsh`。后续真实问答测试 AAR-E2E-20261014-D：daemon 记录 `feishu ok=True`、OPEN请求获系统接受、`callback ACK: 本地状态已确认持久化`；用户报告实际桌面Harness被打开。
- 只读观察器依独一标签确认真实 Harness 原生问句持续 pending；ACKED记录在原生问句仍挂起时存续49秒，之后用户回答，快照消失并清除该提醒。观察 errors=0。此证据支持“已处理”不批准或回答原生请求；用户操作反馈与daemon日志相互印证。

## 尚未完成的真实环境验收

实际 desktop profile 中 Harness bundle 已注册并选中，真实插件已发出 native pending 快照，证明本机激活运行。daemon 1.1.1 与桌面直启按钮、用户实际GUI反馈、ACK持久化、原生问句独立存续49秒均有记录。未修改 ASAR。尚未做原生 approval 弹窗按钮验收，以及多个并发待办、断线重连与长历史性能测试。

`ask_user` 别名、`request_secret` 与第三方自定义卡片不在本版已验证支持范围。Harness 桌面只手动选会话，不实现未经证实的会话 deep-link。
