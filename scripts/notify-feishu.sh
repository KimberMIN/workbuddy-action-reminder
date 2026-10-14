#!/usr/bin/env bash
# Agent Action Reminder — 飞书通知通道
# 中文标点前使用 ${TITLE} 明确变量边界，避免被当成变量名的一部分。
set -u
STATE_DIR="${ACTION_REMINDER_STATE_DIR:-}"
if [ -z "$STATE_DIR" ]; then
  if [ -f "$HOME/.workbuddy/action-reminder/config.json" ]; then
    STATE_DIR="$HOME/.workbuddy/action-reminder"
  else
    STATE_DIR="$HOME/.agent-action-reminder"
  fi
fi
CONFIG="${ACTION_REMINDER_CONFIG:-$STATE_DIR/config.json}"
TITLE="${1:-Agent 待操作提醒}"
BODY="${2:-}"
PYTHON="${ACTION_REMINDER_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x /usr/bin/python3 ]; then
    PYTHON=/usr/bin/python3
  else
    PYTHON="$(command -v python3 2>/dev/null)"
  fi
fi
[ -n "$PYTHON" ] || { printf '%s\n' 'feishu failed: Python 不可用' >&2; exit 1; }
LINK="${3:-}"
exec "$PYTHON" - "$CONFIG" "$TITLE" "$BODY" "$LINK" <<'PY'
import json
import re
import string
from urllib.parse import quote, unquote
import os
import shutil
import subprocess
import sys


def fail(reason, code=1):
    # 不回显 CLI 原始输出、命令参数或配置，防止凭据进入日志。
    print('feishu failed: ' + reason, file=sys.stderr)
    sys.exit(code)


title, body, link = sys.argv[2:5]
ack = None
raw_ack = os.environ.get('ACTION_REMINDER_ACK_VALUE', '')
if raw_ack:
    try:
        ack = json.loads(raw_ack)
    except ValueError:
        fail('已处理按钮参数无效')
    if (not isinstance(ack, dict) or ack.get('action') != 'ack_reminder'
            or set(ack) != {'action', 'reminder_token', 'generation'}
            or not isinstance(ack.get('reminder_token'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{43}', ack['reminder_token'])
            or not isinstance(ack.get('generation'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', ack['generation'])):
        fail('已处理按钮参数无效')
if link:
    harness = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})', link)
    is_harness = bool(harness and int(harness.group(1)) <= 65535)
    if not is_harness:
        # 固定 scheme/host/单个编码路径段；拒绝查询、片段、畸形编码和控制字符。
        if not re.fullmatch(r"workbuddy://chat/(?:[A-Za-z0-9_.!~*'()-]|%[0-9A-F]{2})+", link):
            fail('任务链接无效')
        encoded_id = link[len('workbuddy://chat/'):]
        try:
            session_id = unquote(encoded_id, errors='strict')
            valid = (session_id.strip() and session_id not in ('.', '..')
                     and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in session_id)
                     and quote(session_id, safe="-_.!~*'()") == encoded_id)
        except UnicodeError:
            valid = False
        if not valid:
            fail('任务链接无效')

    # 飞书 Markdown 用 HTML 实体展示特殊字符；单次逐字符处理防止实体注入。
    escaped_body = ''.join('&#%d;' % ord(c) if c in string.punctuation else c for c in body)
    card = {
        'schema': '2.0',
        'config': {'width_mode': 'default'},
        'header': {'template': 'blue', 'title': {'tag': 'plain_text', 'content': title}},
        'body': {'direction': 'vertical', 'vertical_spacing': '12px',
                 'padding': '12px 12px 20px 12px', 'elements': [
            {'tag': 'column_set', 'flex_mode': 'none', 'columns': [
                {'tag': 'column', 'width': 'weighted', 'weight': 1,
                 'background_style': 'blue-50', 'padding': '12px',
                 'elements': [{'tag': 'markdown', 'content': '**任务详情**\n' + escaped_body}]}]},
            {'tag': 'column_set', 'flex_mode': 'none', 'horizontal_spacing': '8px',
             'columns': []},
            {'tag': 'markdown', 'text_size': 'notation',
             'content': '<font color="grey">打开任务会唤起运行提醒服务的 Mac（手机点击也一样）；已处理仅停止本条提醒，不批准操作。</font>'}
        ]}
    }
    if is_harness:
        card['body']['elements'][-1]['content'] = (
            '<font color="grey">打开运行提醒服务的 Mac 上的 Harness 桌面应用，请手动选择对应会话；已处理仅停止本条提醒，不批准操作。</font>')
    for label, action, style in [('打开 Harness' if is_harness else '打开对应任务', 'open_reminder', 'primary_filled'),
                                  ('已处理', 'ack_reminder', 'default')]:
        button = {'tag': 'button', 'type': style, 'width': 'fill',
                  'text': {'tag': 'plain_text', 'content': label}, 'behaviors': []}
        if ack:
            button['behaviors'] = [{'type': 'callback', 'value': dict(ack, action=action)}]
        else:
            button.update(disabled=True, disabled_tips={
                'tag': 'plain_text', 'content': '提醒服务尚未连接，请回 Mac 处理'})
        card['body']['elements'][1]['columns'].append({
            'tag': 'column', 'width': 'weighted', 'weight': 1, 'elements': [button]})
    if not ack:
        card['body']['elements'][-1]['content'] = (
            '<font color="grey">提醒服务尚未连接，按钮暂不可用，请回 Mac 的对应智能体应用处理。</font>')
    content_args = ['--msg-type', 'interactive', '--content', json.dumps(card, ensure_ascii=False)]
else:
    content_args = ['--text', '【%s】\n%s' % (title, body)]

try:
    with open(sys.argv[1], encoding='utf-8') as f:
        cfg = json.load(f).get('feishu', {})
except (OSError, ValueError, AttributeError):
    fail('配置文件无法读取或格式错误')
if not cfg.get('target_id'):
    print('feishu skipped: target_id 未配置')
    sys.exit(0)
cli = os.environ.get('ACTION_REMINDER_LARK_CLI') or cfg.get('cli_path')
if cli:
    if not isinstance(cli, str) or not os.path.isabs(cli) or not os.access(cli, os.X_OK):
        fail('指定 CLI 必须是可执行文件的绝对路径')
else:
    cli = shutil.which('lark-cli')
    if not cli:
        candidate = os.path.expanduser('~/.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli')
        if os.access(candidate, os.X_OK):
            cli = candidate
if not cli:
    fail('lark-cli 不可用')
kind = cfg.get('target_type', 'user')
identity = cfg.get('as', 'bot')
if kind not in ('user', 'chat') or identity not in ('bot', 'user'):
    fail('target_type 或发送身份无效')
try:
    result = subprocess.run([cli, 'im', '+messages-send', '--as', identity,
                             '--' + kind + '-id', str(cfg['target_id']),
                             '--format', 'json'] + content_args,
                            capture_output=True, timeout=12)
except subprocess.TimeoutExpired:
    fail('CLI 调用超时')
except OSError:
    fail('CLI 无法启动，请检查路径、权限和 CPU 架构')
if result.returncode:
    fail('CLI 非零退出（exit=%d；原始输出已隐藏）' % result.returncode,
         result.returncode if result.returncode > 0 else 1)
try:
    data = json.loads(result.stdout)
except (ValueError, UnicodeDecodeError):
    fail('CLI 返回非 JSON 内容（原始输出已隐藏）')
if not isinstance(data, dict) or data.get('ok') is not True:
    fail('CLI 未确认成功（ok 非 true；错误详情已隐藏）')
print('feishu ok=True')
PY
