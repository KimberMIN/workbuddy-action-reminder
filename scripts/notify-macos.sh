#!/usr/bin/env bash
# 独立的 macOS 通知助手（供手动测试 / 兜底使用）
# 用法: notify-macos.sh "<title>" "<subtitle>" "<body>" "<group>"
# 优先 terminal-notifier（点击聚焦 WorkBuddy），否则 osascript。

set -u

TITLE="${1:-WorkBuddy 提醒}"
SUB="${2:-}"
BODY="${3:-}"
GROUP="${4:-wb-ar-manual}"

TN=""
for p in /opt/homebrew/bin/terminal-notifier /usr/local/bin/terminal-notifier; do
  [ -x "$p" ] && TN="$p" && break
done

BODY_SAFE="$(printf '%s' "$BODY" | sed "s/\"/'/g")"
SUB_SAFE="$(printf '%s' "$SUB" | sed "s/\"/'/g")"
TITLE_SAFE="$(printf '%s' "$TITLE" | sed 's/"/'"'"'/g')"

if [ -n "$TN" ]; then
  ARGS=("$TN" -title "$TITLE_SAFE")
  [ -n "$SUB_SAFE" ] && ARGS+=(-subtitle "$SUB_SAFE")
  ARGS+=(-message "$BODY_SAFE" -group "$GROUP" -sound Ping -activate com.tencent.workbuddy.mac)
  exec "${ARGS[@]}"
else
  SCRIPT="display notification \"$BODY_SAFE\" with title \"$TITLE_SAFE\""
  [ -n "$SUB_SAFE" ] && SCRIPT="$SCRIPT subtitle \"$SUB_SAFE\""
  SCRIPT="$SCRIPT sound name \"Ping\""
  exec /usr/bin/osascript -e "$SCRIPT"
fi
