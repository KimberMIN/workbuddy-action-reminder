#!/usr/bin/env bash
# WorkBuddy Action Reminder — Hook 转发脚本
# 由 WorkBuddy Hook 调用（stdin 接收 JSON），将事件转发给本地提醒守护进程。
# 设计原则：必须快速返回，绝不阻塞 WorkBuddy；守护进程负责所有耗时与重复逻辑。

set -u

# 关键：避免 WorkBuddy 沙箱代理拦截 127.0.0.1，否则转发请求会被代理吞掉而静默失败。
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

PY=/usr/bin/python3
DAEMON_PY="$(cd "$(dirname "$0")" && pwd)/reminder-daemon.py"
STATE_DIR="$HOME/.workbuddy/action-reminder"
PORT=18789
URL="http://127.0.0.1:${PORT}/event"

# 读取 stdin（Hook 事件 JSON）
RAW="$(cat)"

[ -z "$RAW" ] && exit 0

# 解析并规整事件类型：
#  - Notification 事件用 notification_type 作为具体事件（permission_prompt / idle_prompt）
#  - 其余事件（PermissionRequest / Elicitation / ElicitationResult / UserPromptSubmit / PostToolUse / SessionEnd / PermissionDenied）直接用 hook_event_name
PAYLOAD="$(printf '%s' "$RAW" | "$PY" -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
he = d.get("hook_event_name", "")
nt = d.get("notification_type", "")
ev = nt if he == "Notification" else he
d["event"] = ev
print(json.dumps(d, ensure_ascii=False))
' 2>/dev/null)"

[ -z "$PAYLOAD" ] && exit 0

post_event() {
  printf '%s' "$PAYLOAD" | curl -s --noproxy 127.0.0.1 --max-time 3 -X POST "$URL" \
    -H "Content-Type: application/json" --data-binary @- >/dev/null 2>&1
}

if post_event; then
  exit 0
fi

# 守护进程未运行：懒启动一次后重试（仅一次，避免拖慢 Hook）
if [ -x "$DAEMON_PY" ] || [ -f "$DAEMON_PY" ]; then
  nohup "$PY" "$DAEMON_PY" >>"$STATE_DIR/daemon.log" 2>&1 &
  sleep 1
  post_event && exit 0
fi

# 无论如何都不要因为提醒失败而阻断 WorkBuddy
exit 0
