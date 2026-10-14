#!/usr/bin/env bash
# WorkBuddy Action Reminder — Hook 转发脚本
# 由 WorkBuddy Hook 调用（stdin 接收 JSON），将事件转发给本地提醒守护进程。
# 设计原则：必须快速返回，绝不阻塞 WorkBuddy；守护进程负责所有耗时与重复逻辑。

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 0
DAEMON_PY="$SCRIPT_DIR/reminder-daemon.py"
# Hook failures must stay silent and fail-open; never emit an approval decision.
. "$SCRIPT_DIR/runtime-config.sh" 2>/dev/null || exit 0
reminder_runtime_init >/dev/null 2>&1 || exit 0
URL="http://127.0.0.1:${PORT}/event"

# 读取 stdin（Hook 事件 JSON）
RAW="$(cat)"

[ -z "$RAW" ] && exit 0

# 解析并规整事件类型：
#  - Notification 仅转发明确权限请求，不把空闲或任务完成当作阻塞
#  - 其余事件（PermissionRequest / Elicitation / ElicitationResult / UserPromptSubmit / PostToolUse / SessionEnd / PermissionDenied）直接用 hook_event_name
PAYLOAD="$(printf '%s' "$RAW" | "$PY" -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
if not isinstance(d, dict):
    sys.exit(0)
he = d.get("hook_event_name", "")
nt = d.get("notification_type", "")
ev = nt if he == "Notification" else he
if ev == "PreToolUse" and d.get("tool_name") == "AskUserQuestion":
    ev = "PermissionRequest"
if ev not in {"permission_prompt", "PermissionRequest", "Elicitation", "ElicitationResult",
              "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "PermissionDenied", "SessionEnd"}:
    sys.exit(0)
d["event"] = ev
print(json.dumps(d, ensure_ascii=False))
' 2>/dev/null)"

[ -z "$PAYLOAD" ] && exit 0

post_event() {
  printf '%s' "$PAYLOAD" | curl -fsS --noproxy 127.0.0.1 --max-time 3 -X POST "$URL" \
    -H "Content-Type: application/json" --data-binary @- >/dev/null 2>&1
}

if post_event; then
  exit 0
fi

# 守护进程未运行：懒启动一次后重试（仅一次，避免拖慢 Hook）
if [ -x "$DAEMON_PY" ] || [ -f "$DAEMON_PY" ]; then
  mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
  # Delegate ownership to the controller: reuse an existing launchd job rather
  # than racing its KeepAlive with a second manually spawned daemon.
  bash "$SCRIPT_DIR/action-reminderctl.sh" start >/dev/null 2>&1 || exit 0
  post_event && exit 0
fi

# 无论如何都不要因为提醒失败而阻断 WorkBuddy
exit 0
