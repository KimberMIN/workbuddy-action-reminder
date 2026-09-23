#!/usr/bin/env bash
# WorkBuddy Action Reminder — 飞书通知通道
# 由 reminder-daemon.py 调用，参数: $1=title  $2=body
# 经 lark-cli 发送飞书私聊/群消息（默认 bot 身份），实现跨设备（手机/桌面）提醒。
# 注意：macOS 自带 /bin/bash 为 3.2 版本，set -u 与 ${1:-默认值} 组合存在已知 bug
#（赋值成功但后续引用报 unbound variable），故此处不启用 set -u，改用显式兜底守卫。

CONFIG="${ACTION_REMINDER_CONFIG:-$HOME/.workbuddy/action-reminder/config.json}"
TITLE="${1:-WorkBuddy 待操作提醒}"
BODY="${2:-}"

# 解析 lark-cli 路径：优先 PATH，其次 WorkBuddy 内置位置（兼容 launchd 等窄 PATH 环境）
LARK="$(command -v lark-cli 2>/dev/null)"
if [ -z "$LARK" ] && [ -x "$HOME/.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli" ]; then
  LARK="$HOME/.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli"
fi
[ -z "$LARK" ] && { echo "lark-cli not found"; exit 0; }

# 从 config 读取飞书目标（target_type / target_id / as）
PARSED="$(python3 -c '
import json,sys
try:
    c=json.load(open(sys.argv[1]))
except Exception:
    c={}
f=c.get("feishu",{})
print(f.get("target_type","user"))
print(f.get("target_id","") or "")
print(f.get("as","bot"))
' "$CONFIG" 2>/dev/null)"

TARGET_TYPE="$(printf '%s' "$PARSED" | sed -n 1p)"
TARGET_ID="$(printf '%s' "$PARSED" | sed -n 2p)"
AS="$(printf '%s' "$PARSED" | sed -n 3p)"

[ -z "$TARGET_ID" ] && { echo "feishu target_id 未配置，跳过"; exit 0; }

if [ "$TARGET_TYPE" = "chat" ]; then
  TGT="--chat-id $TARGET_ID"
else
  TGT="--user-id $TARGET_ID"
fi

TEXT="【$TITLE】$BODY"
"$LARK" im +messages-send --as "$AS" $TGT --text "$TEXT" --format json 2>&1 \
  | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print("feishu ok=",d.get("ok"),"msg_id=",(d.get("data") or {}).get("message_id"))
except Exception:
    print("feishu send output unparsable")' 2>/dev/null || echo "feishu send failed"
