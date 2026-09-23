#!/usr/bin/env bash
# WorkBuddy Action Reminder — 控制脚本
# 用法:
#   action-reminderctl.sh start | stop | restart | status | test | log
#   action-reminderctl.sh install-launchd | uninstall-launchd

set -u

# 关键：避免 WorkBuddy 沙箱代理（HTTP_PROXY=127.0.0.1:55134）拦截 127.0.0.1，
# 否则 curl 健康检查会收到代理的假响应，误判"守护进程已在运行"而从不真正启动。
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

PY=/usr/bin/python3
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DAEMON_PY="$SCRIPT_DIR/reminder-daemon.py"
STATE_DIR="$HOME/.workbuddy/action-reminder"
PORT=18789
HEALTH="http://127.0.0.1:${PORT}/health"
PLIST="$HOME/Library/LaunchAgents/com.tencent.workbuddy.action-reminder.plist"
LABEL="com.tencent.workbuddy.action-reminder"

is_running() {
  curl -s --noproxy 127.0.0.1 --max-time 2 "$HEALTH" >/dev/null 2>&1
}

start() {
  if is_running; then
    echo "守护进程已在运行 (127.0.0.1:$PORT)"
    return 0
  fi
  echo "启动守护进程..."
  mkdir -p "$STATE_DIR"
  nohup "$PY" "$DAEMON_PY" >>"$STATE_DIR/daemon.log" 2>&1 &
  sleep 1
  if is_running; then
    echo "已启动。日志: $STATE_DIR/daemon.log"
  else
    echo "启动失败，请查看日志: $STATE_DIR/daemon.log"
    return 1
  fi
}

stop() {
  if ! is_running; then
    echo "守护进程未运行"
    return 0
  fi
  echo "停止守护进程..."
  curl -s --noproxy 127.0.0.1 --max-time 3 -X POST "http://127.0.0.1:${PORT}/stop" >/dev/null 2>&1
  sleep 1
  pkill -f "$DAEMON_PY" 2>/dev/null
  echo "已停止"
}

status() {
  if is_running; then
    echo "== 运行中 =="
    curl -s --noproxy 127.0.0.1 --max-time 3 "http://127.0.0.1:${PORT}/status"
    echo
  else
    echo "守护进程未运行"
  fi
}

test_notify() {
  if ! is_running; then
    start || return 1
  fi
  echo "发送测试提醒..."
  curl -s --noproxy 127.0.0.1 --max-time 3 -X POST "http://127.0.0.1:${PORT}/test" 2>/dev/null
  echo
  echo "如果未看到系统通知，请检查 macOS 系统设置 → 通知 中 WorkBuddy/终端的通知权限。"
}

log() {
  tail -n 50 "$STATE_DIR/daemon.log" 2>/dev/null
}

install_launchd() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DAEMON_PY</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$STATE_DIR/daemon.log</string>
  <key>StandardErrorPath</key>
  <string>$STATE_DIR/daemon.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
    || launchctl load "$PLIST" 2>/dev/null
  echo "已写入并加载自启: $PLIST"
  echo "立即启动..."
  launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || start
}

uninstall_launchd() {
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  launchctl unload "$PLIST" 2>/dev/null
  rm -f "$PLIST"
  echo "已卸载自启 ($PLIST)"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  test) test_notify ;;
  log) log ;;
  install-launchd) install_launchd ;;
  uninstall-launchd) uninstall_launchd ;;
  *)
    echo "用法: $0 {start|stop|restart|status|test|log|install-launchd|uninstall-launchd}"
    exit 1
    ;;
esac
