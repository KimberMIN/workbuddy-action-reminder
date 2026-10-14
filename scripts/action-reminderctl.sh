#!/usr/bin/env bash
# Agent Action Reminder — local control; never kill processes by name.
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DAEMON_PY="$SCRIPT_DIR/reminder-daemon.py"
. "$SCRIPT_DIR/runtime-config.sh" || exit 1
reminder_runtime_init || { echo '提醒运行配置无效或 Python 不可用' >&2; exit 1; }

is_running() { reminder_is_running; }

start() {
  if is_running; then
    echo "守护进程已在运行 (127.0.0.1:$PORT)"
    return 0
  fi
  reminder_select_launchd || return 1
  mkdir -p "$STATE_DIR" || return 1
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl kickstart "$DOMAIN/$LABEL" || return 1
  elif [ -f "$PLIST" ]; then
    launchctl bootstrap "$DOMAIN" "$PLIST" || return 1
  else
    nohup "$PY" "$DAEMON_PY" >>"$STATE_DIR/daemon.log" 2>&1 &
  fi
  sleep 1
  if is_running; then
    echo "已启动。日志: $STATE_DIR/daemon.log"
  else
    echo "启动失败或端口非本服务，请查看日志: $STATE_DIR/daemon.log" >&2
    return 1
  fi
}

stop() {
  reminder_select_launchd || return 1
  # Boot out only the selected exact launchd label, so KeepAlive cannot respawn.
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL" || return 1
    sleep 1
  fi
  if ! is_running; then
    echo '未检测到本提醒守护进程；不会向其他服务发送停止请求'
    return 0
  fi
  curl -fsS --noproxy 127.0.0.1 --max-time 3 -X POST \
    "http://127.0.0.1:${PORT}/stop" >/dev/null 2>&1 || return 1
  sleep 1
  if is_running; then
    echo '守护进程尚未停止；不使用 pkill 或按名称杀进程' >&2
    return 1
  fi
  echo '已停止'
}

status() {
  if is_running; then
    echo '== 运行中 =='
    curl -fsS --noproxy 127.0.0.1 --max-time 3 "http://127.0.0.1:${PORT}/status" || return 1
    echo
  else
    echo '未检测到本提醒守护进程'
    return 1
  fi
}

test_notify() {
  is_running || start || return 1
  echo '发送测试提醒...'
  curl -fsS --noproxy 127.0.0.1 --max-time 3 -X POST "http://127.0.0.1:${PORT}/test" || return 1
  echo
}

install_launchd() {
  reminder_select_launchd || return 1
  mkdir -p "$HOME/Library/LaunchAgents" "$STATE_DIR" || return 1
  # Stop a verified manual instance before handing ownership to launchd.
  stop || return 1
  reminder_write_plist || return 1
  launchctl bootstrap "$DOMAIN" "$PLIST" || return 1
  echo "已写入并加载自启: $PLIST"
}

uninstall_launchd() {
  reminder_select_launchd || return 1
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL" || return 1
  fi
  # PLIST is constructed only from HOME and one of two fixed labels above.
  rm -f -- "$PLIST" || return 1
  echo "已卸载自启 ($PLIST)"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop && sleep 1 && start ;;
  status) status ;;
  health) is_running ;;
  test) test_notify ;;
  log) tail -n 50 "$STATE_DIR/daemon.log" ;;
  install-launchd) install_launchd ;;
  uninstall-launchd) uninstall_launchd ;;
  *) echo "用法: $0 {start|stop|restart|status|health|test|log|install-launchd|uninstall-launchd}"; exit 1 ;;
esac
