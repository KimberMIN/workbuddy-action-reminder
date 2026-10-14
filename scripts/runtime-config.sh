#!/usr/bin/env bash
# Shared runtime contract. Source this file, then call reminder_runtime_init.
reminder_runtime_init() {
  if [ -n "${ACTION_REMINDER_STATE_DIR:-}" ]; then
    STATE_DIR="$ACTION_REMINDER_STATE_DIR"
  elif [ -f "$HOME/.workbuddy/action-reminder/config.json" ]; then
    STATE_DIR="$HOME/.workbuddy/action-reminder"
  else
    STATE_DIR="$HOME/.agent-action-reminder"
  fi
  case "$STATE_DIR" in /*) ;; *) STATE_DIR="$PWD/$STATE_DIR" ;; esac
  export ACTION_REMINDER_STATE_DIR="$STATE_DIR"
  PY="${ACTION_REMINDER_PYTHON:-}"
  if [ -z "$PY" ]; then
    if [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3
    else PY="$(command -v python3 2>/dev/null)"; fi
  fi
  PY="$(command -v "$PY" 2>/dev/null)" || return 1
  [ -n "$PY" ] && [ -x "$PY" ] || return 1
  case "$PY" in /*) ;; *) PY="$PWD/$PY" ;; esac
  export ACTION_REMINDER_PYTHON="$PY"
  PORT="$("$PY" - "$STATE_DIR/config.json" <<'PY'
import json, sys
try:
    try:
        with open(sys.argv[1], encoding='utf-8') as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict):
        raise ValueError()
    port = config.get('port', 18789)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError()
    print(port)
except (OSError, ValueError, TypeError):
    print('Invalid reminder config or port (expected integer 1..65535)', file=sys.stderr)
    sys.exit(1)
PY
)" || return 1
  unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
  export NO_PROXY="127.0.0.1,localhost" no_proxy="127.0.0.1,localhost"
}

# A bare ok=true is not identity. Older daemons need a second, read-only
# reminder-specific status signature. This is compatibility, not authentication.
reminder_is_running() {
  local health kind status
  health="$(curl -fsS --noproxy 127.0.0.1 --max-time 2 \
    "http://127.0.0.1:${PORT}/health" 2>/dev/null)" || return 1
  kind="$(printf '%s' "$health" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
    if not isinstance(d, dict) or d.get("ok") is not True: sys.exit(1)
    if d.get("service") == "agent-action-reminder": print("current")
    elif d == {"ok": True}: print("legacy")
    else: sys.exit(1)
except (ValueError, TypeError): sys.exit(1)
' 2>/dev/null)" || return 1
  [ "$kind" = current ] && return 0
  status="$(curl -fsS --noproxy 127.0.0.1 --max-time 2 \
    "http://127.0.0.1:${PORT}/status" 2>/dev/null)" || return 1
  printf '%s' "$status" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
    valid = (isinstance(d, dict) and d.get("ok") is True
             and type(d.get("waiting")) is int and d["waiting"] >= 0
             and isinstance(d.get("pending"), list)
             and d["waiting"] == len(d["pending"])
             and type(d.get("ack_callback_ready")) is bool)
    sys.exit(0 if valid else 1)
except (ValueError, TypeError): sys.exit(1)
' 2>/dev/null
}

# Pure writer: no launchctl invocation. Persist only explicit CLI configuration,
# not arbitrary environment secrets. Atomic private plist handles XML escaping.
reminder_write_plist() {
  "$PY" - "$PLIST" "$LABEL" "$DAEMON_PY" <<'PY'
import os, plistlib, sys, tempfile
path, label, daemon = sys.argv[1:]
env = {k: os.environ[k] for k in (
    'HOME', 'PATH', 'ACTION_REMINDER_STATE_DIR', 'ACTION_REMINDER_PYTHON',
    'ACTION_REMINDER_CONFIG', 'ACTION_REMINDER_LARK_CLI',
    'LARK_CLI_CONFIG_DIR', 'LARKSUITE_CLI_CONFIG_DIR', 'XDG_CONFIG_HOME',
    'LARK_CLI_NO_PROXY', 'LARKSUITE_CLI_NO_UPDATE_NOTIFIER',
    'LARKSUITE_CLI_NO_SKILLS_NOTIFIER') if k in os.environ}
env.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
log = os.path.join(env['ACTION_REMINDER_STATE_DIR'], 'daemon.log')
data = dict(Label=label, ProgramArguments=[env['ACTION_REMINDER_PYTHON'], daemon],
            RunAtLoad=True, KeepAlive=True, StandardOutPath=log,
            StandardErrorPath=log, EnvironmentVariables=env)
tmp = None
try:
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), delete=False) as f:
        tmp = f.name
        plistlib.dump(data, f)
    os.replace(tmp, path)
    tmp = None
finally:
    if tmp is not None:
        os.unlink(tmp)
PY
}

# Reuse legacy deployment; refuse ambiguous double deployments.
reminder_select_launchd() {
  local base legacy current old_present=0 new_present=0
  base="$HOME/Library/LaunchAgents"
  legacy=com.tencent.workbuddy.action-reminder
  current=com.agent-action-reminder
  DOMAIN="gui/$(id -u)"
  if [ -f "$base/$legacy.plist" ] || launchctl print "$DOMAIN/$legacy" >/dev/null 2>&1; then old_present=1; fi
  if [ -f "$base/$current.plist" ] || launchctl print "$DOMAIN/$current" >/dev/null 2>&1; then new_present=1; fi
  if [ "$old_present$new_present" = 11 ]; then
    echo '检测到新旧两个 launchd 配置/任务；请先明确保留哪一个，拒绝创建或启动双实例。' >&2
    return 1
  fi
  LABEL="$current"
  [ "$old_present" = 1 ] && LABEL="$legacy"
  PLIST="$base/$LABEL.plist"
}
