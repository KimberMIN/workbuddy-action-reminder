#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Action Reminder — 本地提醒守护进程
=============================================

职责：
  1. 通过本机 HTTP 接口接收 WorkBuddy Hook 转发的事件；
  2. 维护「等待用户操作」的待处理记录（pending interactions）；
  3. 按升级策略重复发送 macOS 系统通知，直到用户处理或主动忽略；
  4. 收到「用户已响应 / 会话结束」事件时自动取消对应提醒；
  5. 暴露 status / stop / test 接口，便于运维与验证。

设计约束：
  - 纯标准库实现，不依赖第三方包；
  - 仅在 127.0.0.1 监听，不暴露到网络；
  - 单实例：端口已被占用时直接退出，由启动器负责重启；
  - Hook 转发脚本必须快速返回，本进程负责所有耗时与重复逻辑。

状态文件：~/.workbuddy/action-reminder/state.json
日志文件：~/.workbuddy/action-reminder/daemon.log
"""

import json
import os
import sys
import threading
import time
import datetime
import hashlib
import subprocess
import re
import secrets
import tempfile
import selectors
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse
from harness_bridge import SOURCE as HARNESS_SOURCE, LEASE_SECONDS, validate_snapshot, valid_host_url

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
APP_BUNDLE = "com.tencent.workbuddy.mac"
DEFAULT_PORT = 18789
LEGACY_STATE_DIR = os.path.expanduser("~/.workbuddy/action-reminder")
STATE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("ACTION_REMINDER_STATE_DIR") or
    (LEGACY_STATE_DIR if os.path.isfile(os.path.join(LEGACY_STATE_DIR, "config.json"))
     else "~/.agent-action-reminder")))
STATE_FILE = os.path.join(STATE_DIR, "state.json")
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")
LOG_FILE = os.path.join(STATE_DIR, "daemon.log")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DQ = chr(34)  # 双引号 "

# 重复提醒间隔（秒）。第 1 次立即弹出，之后按此序列追加。
REPEAT_DELAYS = [180, 600, 900, 900]  # 3 分钟、10 分钟、15 分钟、15 分钟
MAX_REPEATS = len(REPEAT_DELAYS)
TOTAL_TIMEOUT = 7200  # 超过 2 小时停止声音提醒，保留待处理中心记录

# 等待类事件 -> 待处理记录类型
WAIT_EVENTS = {
    "permission_prompt": "permission",   # 工具权限审批
    "PermissionRequest": "permission",
    "Elicitation": "elicitation",        # MCP 工具请求输入
}
# 解除类事件 -> 取消该会话/交互的待处理
RESOLVE_EVENTS = {
    "ElicitationResult",
    "UserPromptSubmit",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionDenied",
    "SessionEnd",
}

lock = threading.RLock()
pending = {}        # key -> record(dict)
session_tasks = {}  # 仅保存 Hook 提供的已脱敏任务摘要，不扫描会话文件
config = {}
harness_bridges = {}  # monotonic leases, intentionally NOT persisted
httpd = None
stop_evt = threading.Event()
# 仅消费者的真实 ready 标记可以打开门禁；配置不能绕过它。
ACK_CALLBACK_READY = False
callback_process = None
CALLBACK_READY_MARKER = b"[event] ready event_key=card.action.trigger"


def callback_ready():
    proc = callback_process
    return (ACK_CALLBACK_READY and not stop_evt.is_set()
            and proc is not None and proc.poll() is None)


def consume_callback_process(proc):
    """只信任 CLI 的扁平 NDJSON；不记录事件、身份、token 或 stderr 原文。"""
    global ACK_CALLBACK_READY
    ACK_CALLBACK_READY = False
    started = time.monotonic()
    buffers = {"stderr": b"", "stdout": b""}
    with selectors.DefaultSelector() as selector:
        os.set_blocking(proc.stderr.fileno(), False)
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        while not stop_evt.is_set() and proc.poll() is None:
            if not ACK_CALLBACK_READY and time.monotonic() - started > 45:
                log("callback listener: ready 超时")
                return
            for key, _ in selector.select(0.2):
                stream = key.data
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    ACK_CALLBACK_READY = False
                    return
                buffers[stream] += chunk
                if len(buffers[stream]) > 2 * 1024 * 1024:
                    ACK_CALLBACK_READY = False
                    log("callback listener: 输入超限，重新连接")
                    return
                while b"\n" in buffers[stream]:
                    line, buffers[stream] = buffers[stream].split(b"\n", 1)
                    if stream == "stderr":
                        if line.strip() == CALLBACK_READY_MARKER:
                            if not ACK_CALLBACK_READY and not stop_evt.is_set() and proc.poll() is None:
                                ACK_CALLBACK_READY = True
                                selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
                                log("callback listener: ready event_key=card.action.trigger")
                        elif line.strip() == b"[source] feishu-websocket: connected":
                            # CLI 会在 ready 后报告正常连接；此行本身不能打开门禁。
                            continue
                        elif line.startswith((b"Event received: type=", b"[event] logs: ",
                                              b"[event] to stop gracefully: ")):
                            # CLI 正常诊断可能携带事件标识或路径，只忽略、不转存。
                            continue
                        elif line.strip() and ACK_CALLBACK_READY:
                            # 无法证明诊断行不影响连接完整性时，保守关闭门禁。
                            ACK_CALLBACK_READY = False
                            log("callback listener: 收到运行诊断，关闭门禁并重连")
                            return
                    elif callback_ready():
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            log("callback listener: 忽略无效 JSON")
                            continue
                        acknowledge_card(event)


def callback_listener_loop():
    global ACK_CALLBACK_READY, callback_process
    fcfg = config.get("feishu", {})
    if not (fcfg.get("enabled") and fcfg.get("ack_callback_enabled")
            and fcfg.get("as", "bot") == "bot"):
        return
    cli = os.environ.get("ACTION_REMINDER_LARK_CLI") or fcfg.get("cli_path") or shutil.which("lark-cli")
    if not isinstance(cli, str) or not os.path.isabs(cli) or not os.access(cli, os.X_OK):
        log("callback listener: CLI 路径不可用，门禁保持关闭")
        return
    # 仅隔离回调消费者环境；新 bus 继承此环境，已有共享 bus 不会被改动。
    env = dict(os.environ)
    if fcfg.get("ack_callback_no_proxy", True):
        env["LARK_CLI_NO_PROXY"] = "1"
    else:
        env.pop("LARK_CLI_NO_PROXY", None)
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    delay = 1
    while not stop_evt.is_set():
        proc = None
        started = time.monotonic()
        try:
            proc = subprocess.Popen([cli, "event", "consume", "card.action.trigger", "--as", "bot"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env)
            callback_process = proc
            consume_callback_process(proc)
        except Exception:
            log("callback listener: 连接失败（详情已隐藏）")
        finally:
            ACK_CALLBACK_READY = False
            callback_process = None
            if proc is not None:
                # 只关闭本消费者，绝不停止共享 bus 或其他消费者。
                proc.stdin.close()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait()
                proc.stdout.close()
                proc.stderr.close()
            log("callback listener: 门禁关闭，消费者已清理")
        if time.monotonic() - started >= 60:
            delay = 1
        if stop_evt.wait(delay):
            break
        delay = min(delay * 2, 60)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (ts, msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config():
    global config
    defaults = {
        "port": DEFAULT_PORT,
        "sound": "Ping",
        "quiet_start": None,    # 例如 23 (23:00)
        "quiet_end": None,      # 例如 8  (08:00)
        "enabled": True,
        "max_repeats": MAX_REPEATS,
        "total_timeout": TOTAL_TIMEOUT,
        "harness": {"enabled": False},
        "feishu": {
            "enabled": True,        # 是否经飞书发送提醒（跨设备可达）
            "target_type": "user",  # user=私聊(open_id) / chat=群(chat_id)
            "target_id": "",        # 飞书 open_id 或 chat_id；为空则跳过
            "as": "bot",            # bot / user
        },
    }
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("config must be an object")
        defaults.update(user)
    except FileNotFoundError:
        pass
    if type(defaults.get("port")) is not int or not 1 <= defaults["port"] <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    hcfg = defaults.get("harness", {})
    if (not isinstance(hcfg, dict) or not isinstance(hcfg.get("token", ""), str)
            or type(hcfg.get("enabled", False)) is not bool):
        raise ValueError("invalid harness configuration")
    config = defaults


def in_quiet_hours():
    qs = config.get("quiet_start")
    qe = config.get("quiet_end")
    if qs is None or qe is None:
        return False
    h = datetime.datetime.now().hour
    if qs <= qe:
        return qs <= h < qe
    return h >= qs or h < qe  # 跨午夜，例如 23 -> 8


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------

def save_state():
    tmp = None
    try:
        with lock:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=os.path.dirname(STATE_FILE), delete=False) as f:
                tmp = f.name
                json.dump(pending, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)
        return True
    except Exception:
        log("save_state error: 状态写入失败（详情已隐藏）")
        return False
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def load_state():
    global pending
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except Exception:
        pending = {}
    # 旧版本的空闲项不能在升级后恢复调度；保留 ACK 及按钮绑定数据。
    changed = False
    for rec in pending.values():
        if rec.get("type") == "idle" or rec.get("event") == "idle_prompt":
            rec["next_reminder_at"] = None
            if rec.get("status") != "ACKED":
                rec["status"] = "IGNORED"
            changed = True
        elif (rec.get("event") == "permission_prompt" and not rec.get("request_identity")
              and not rec.get("operation_fingerprint") and rec.get("status") == "WAITING"):
            rec.update(next_reminder_at=None, status="ESCALATED")
            changed = True
    if changed:
        save_state()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def now_ts():
    return time.time()


def fmt_wait(sec):
    sec = int(sec)
    if sec < 60:
        return "%d 秒" % sec
    m = sec // 60
    if m < 60:
        return "%d 分钟" % m
    h = m // 60
    return "%d 小时 %d 分钟" % (h, m % 60)


def request_identity(payload):
    # 字段来自实际 Hook；不同会话即使复用调用 ID 也不应合并。
    for field in ("interaction_id", "tool_use_id", "call_id", "elicitation_id"):
        if payload.get(field):
            kind = "tool" if field in ("tool_use_id", "call_id") else field
            return [kind, str(payload[field])]
    return None


def operation_fingerprint(payload):
    if payload.get("tool_name") and isinstance(payload.get("tool_input"), dict):
        raw = json.dumps([payload["tool_name"], payload["tool_input"]], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return None


def source_host(payload):
    """Old records/hooks remain WorkBuddy; other hosts cannot resolve them."""
    return payload.get("source", "workbuddy")


def host_label(rec):
    return {"workbuddy": "WorkBuddy", "deepseek-harness": "DeepSeek Harness"}.get(
        source_host(rec), "Agent")


def stable_key(payload):
    """不将原始参数持久化；缺调用 ID 时按规范化参数指纹去重。"""
    identity = request_identity(payload)
    family = WAIT_EVENTS.get(payload.get("event"), payload.get("event"))
    discriminator = identity or [family, operation_fingerprint(payload) or payload.get("message", "")]
    parts = [payload.get("session_id"), discriminator]
    if source_host(payload) != "workbuddy":
        parts[:0] = [source_host(payload), payload.get("bridge_id", "default")]
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return "key:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def safe_summary(value, limit=180):
    """仅发送简短自然语言；凭据、路径和命令片段不进入跨设备提醒。"""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"```[\s\S]*?```|`[^`]*`", "[代码已隐藏]", value)
    value = re.sub(r"(?i)\b(?:bearer\s+\S+|(?:sk|ghp|gho|xoxb|xoxp)[-_][\w-]+)", "[凭据已隐藏]", value)
    value = re.sub(r"(?i)(?:[\"']?(?:[\w-]*(?:token|secret|password|passwd|api[_-]?key|authorization|cookie)[\w-]*)[\"']?)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)", "[敏感参数已隐藏]", value)
    value = re.sub(r"https?://\S+|(?:~?/|[A-Za-z]:\\)[^\s，。；]+", "[地址已隐藏]", value)
    value = re.sub(r"(?i)\b(?:curl|wget|bash|zsh|sh|python3?|node|npm|git|ssh|sudo|rm)\s+[^\n]+", "[命令已隐藏]", value)
    value = re.sub(r"--[\w-]+(?:[=\s]+[^\s，。；]+)?", "[参数已隐藏]", value)
    value = re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b|\b[A-Za-z0-9_=-]{32,}\b", "[标识已隐藏]", value)
    value = re.sub(r"(?i)codebuddy", "WorkBuddy", value)
    return " ".join(value.split())[:limit]


def task_summary(value):
    text = safe_summary(value)
    compact = re.sub(r"[\s，。！？,.!?；;：:]", "", text).lower()
    if compact == "[地址已隐藏]":
        return ""
    if len(compact) < 4 or compact in {
            "已点击", "点击了", "继续", "继续吧", "请继续", "继续执行", "好的", "好", "收到",
            "已处理", "处理好了", "已完成", "完成了", "可以", "同意", "确认", "确认执行",
            "ok", "okay", "yes", "done", "continue", "谢谢"}:
        return ""
    return text


def context_fields(payload):
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    questions = payload.get("questions") or tool_input.get("questions") or []
    question = payload.get("question") or tool_input.get("question")
    if not question and isinstance(questions, list):
        question = "；".join(q.get("question", "") for q in questions
                            if isinstance(q, dict) and isinstance(q.get("question"), str))
    return {
        "task_title": task_summary(payload.get("task_title")) or (
            task_summary(session_tasks.get(payload.get("session_id"), ""))
            if source_host(payload) == "workbuddy" else ""),
        "tool_name": safe_summary(payload.get("tool_name"), 60),
        "operation": (safe_summary(tool_input.get("description")) or
                      {"Write": "写入文件（已有文件将被覆盖）", "Edit": "替换文件中的指定内容"}.get(payload.get("tool_name"), "")),
        "question": safe_summary(question),
        "message": safe_summary(payload.get("message")),
    }


def reminder_body(rec, wait_sec):
    task = task_summary(rec.get("task_title")) or "未获取原始任务名称"
    question = safe_summary(rec.get("question"))
    tool = safe_summary(rec.get("tool_name"), 60)
    operation = safe_summary(rec.get("operation"))
    message = safe_summary(rec.get("message"))
    generic = re.search(r"(?i)(?:waiting for (?:your |user )?input|needs your (?:input|attention)|等待.{0,8}输入|空闲)", message)
    if question:
        detail = "待回答问题：" + question
    elif tool:
        detail = "待确认操作：%s%s" % (tool, " — " + operation if operation else "（未提供操作说明，详细参数请在会话中查看）")
    elif message and not generic:
        detail = ("待确认操作：" if rec.get("type") == "permission" else "待处理内容：") + message
    else:
        detail = "待处理内容：等待你的输入；未提供具体操作或问题"
    return "\n".join(["任务：" + task, detail,
                      "等待时长：%s · 第 %d 次提醒" % (fmt_wait(wait_sec), rec.get("reminder_count", 0) + 1),
                      "请返回 %s 对应会话处理。" % host_label(rec)])


# ---------------------------------------------------------------------------
# macOS 通知
# ---------------------------------------------------------------------------

def notify(title, subtitle, body, group, sound=True, app_bundle=APP_BUNDLE):
    """优先 terminal-notifier（点击聚焦 WorkBuddy），否则 osascript 兜底。"""
    tn = None
    for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier"):
        if os.path.exists(p):
            tn = p
            break

    body = (body or "").replace(DQ, "'")
    subtitle = (subtitle or "").replace(DQ, "'")
    title = (title or "").replace(DQ, "")

    if tn:
        cmd = [tn, "-title", title]
        if subtitle:
            cmd += ["-subtitle", subtitle]
        cmd += ["-message", body, "-group", group]
        if sound and config.get("sound"):
            cmd += ["-sound", config.get("sound")]
        # macOS 通知仍仅聚焦应用；会话深链用于飞书卡片按钮。
        cmd += ["-activate", app_bundle]
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
            return True
        except Exception as e:
            log("terminal-notifier failed: %s" % e)

    # 兜底：osascript display notification（点击无动作，已在文档说明）
    script = 'display notification "%s" with title "%s"' % (body, title)
    if subtitle:
        script += ' subtitle "%s"' % subtitle
    if sound and config.get("sound"):
        script += ' sound name "%s"' % config.get("sound")
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=10)
        return True
    except Exception as e:
        log("osascript notify failed: %s" % e)
        return False


def session_deeplink(session_id):
    """仅编码 Hook 的会话 ID，不接受调用方提供任意跳转 URL。"""
    if (not isinstance(session_id, str) or not session_id.strip()
            or session_id in (".", "..")
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in session_id)):
        return ""
    try:
        return "workbuddy://chat/" + quote(session_id, safe="-_.!~*'()")
    except UnicodeError:
        return ""


def notify_feishu(title, body, session_id=None, ack_value=None, harness_url=None):
    """经飞书（lark-cli）发送提醒，跨设备可达（手机/桌面都能收到）。"""
    fcfg = config.get("feishu", {})
    if not fcfg.get("enabled", False):
        return
    if not fcfg.get("target_id"):
        return
    script = os.path.join(SCRIPT_DIR, "notify-feishu.sh")
    if not os.path.exists(script):
        return
    try:
        env = dict(os.environ, ACTION_REMINDER_PYTHON=sys.executable,
                   ACTION_REMINDER_CONFIG=CONFIG_FILE)
        env.pop("ACTION_REMINDER_ACK_VALUE", None)
        if ack_value and callback_ready():
            env["ACTION_REMINDER_ACK_VALUE"] = json.dumps(ack_value)
        res = subprocess.run(["/bin/bash", script, title or "", body or "",
                              (harness_url if valid_host_url(harness_url) else "")
                              if harness_url is not None else session_deeplink(session_id)],
                             capture_output=True, timeout=15, env=env)
        if res.returncode != 0:
            log("feishu notify failed: exit=%d" % res.returncode)
            return False
        if res.stdout.decode("utf-8", "replace").strip() != "feishu ok=True":
            log("feishu notify skipped: 未确认发送成功")
            return False
        log("feishu notify: feishu ok=True")
        return True
    except Exception:
        log("feishu notify failed: 调用异常或超时（原始参数已隐藏）")
        return False


# ---------------------------------------------------------------------------
# 待处理记录管理
# ---------------------------------------------------------------------------

def normalize_event(payload):
    event = payload.get("event") or payload.get("hook_event_name")
    if event == "Notification":
        event = payload.get("notification_type")
    if event == "PreToolUse" and payload.get("tool_name") == "AskUserQuestion":
        return "PermissionRequest"
    return event if isinstance(event, str) else None


def create_pending(payload, defer_notification=False):
    payload = dict(payload, event=normalize_event(payload))
    if payload.get("event") not in WAIT_EVENTS:
        return None, None, False
    key = stable_key(payload)
    with lock:
        rec = pending.get(key)
        if rec is None and source_host(payload) == "workbuddy" and payload.get("interaction_id"):
            # 旧无 ID 主键可能碰撞不同操作，只有明确交互 ID 才能迁移。
            legacy = "iid:" + str(payload["interaction_id"])
            old = pending.get(legacy)
            if old and old.get("session_id") == payload.get("session_id") and old.get("type") != "idle":
                rec = pending.pop(legacy)
                rec.update(key=key, request_identity=request_identity(payload),
                           operation_fingerprint=operation_fingerprint(payload))
                pending[key] = rec
        etype = WAIT_EVENTS[payload["event"]]
        if rec and rec.get("status") in ("WAITING", "ACKED", "ESCALATED", "EXPIRED"):
            # 已存在且等待中：刷新最后出现时间，避免重复创建
            rec["last_seen"] = now_ts()
            rec.update({k: v for k, v in context_fields(payload).items() if v})
            save_state()
            return key, rec, False
        rec = {
            "key": key,
            "reminder_token": secrets.token_urlsafe(32),
            "generation": secrets.token_hex(16),
            "ack_operator_id": (config.get("feishu", {}).get("target_id", "")
                                if config.get("feishu", {}).get("target_type", "user") == "user"
                                else config.get("feishu", {}).get("ack_operator_id", "")),
            "source": source_host(payload),
            "bridge_id": payload.get("bridge_id"),
            "host_url": payload.get("host_url"),
            "session_id": payload.get("session_id"),
            "request_identity": request_identity(payload),
            "operation_fingerprint": operation_fingerprint(payload),
            "cwd": payload.get("cwd"),
            "permission_mode": payload.get("permission_mode"),
            "tool_name": payload.get("tool_name"),
            "notification_type": payload.get("notification_type"),
            "event": payload.get("event"),
            "type": etype,
            "message": payload.get("message", ""),
            "created_at": now_ts(),
            "last_seen": now_ts(),
            "next_reminder_at": now_ts(),  # 立即首次提醒
            "reminder_count": 0,
            "status": "WAITING",
        }
        rec.update(context_fields(payload))
        pending[key] = rec
        save_state()
    # send_reminder atomically claims first delivery; never hold this outer lock
    # during notification I/O, including legacy WorkBuddy requests.
    if not defer_notification:
        send_reminder(key, rec, first=True)
    log("CREATE pending %s type=%s session=%s" % (key, etype, rec.get("session_id")))
    return key, rec, True


def harness_record_live(rec):
    bridge = harness_bridges.get(rec.get("bridge_id"), {})
    return (config.get("harness", {}).get("enabled") is True
            and time.monotonic() - bridge.get("last_seen", float("-inf")) < LEASE_SECONDS)


def reconcile_harness(payload):
    """Authoritative pending snapshot; disappearance resolves, silence only pauses."""
    bridge_id, host_url, events = validate_snapshot(payload)
    if config.get("harness", {}).get("enabled") is not True:
        raise ValueError("Harness integration is disabled")
    with lock:
        clock = time.monotonic()
        boot_id, sequence = payload["boot_id"], payload["sequence"]
        previous = harness_bridges.get(bridge_id)
        retired = set(previous.get("retired", ())) if previous else set()
        if previous:
            if previous["host_url"] != host_url or boot_id in retired:
                raise ValueError("bridge identity conflict")
            if previous["boot_id"] == boot_id:
                if sequence <= previous["sequence"]:
                    raise ValueError("stale snapshot")
            else:
                if clock - previous["last_seen"] < LEASE_SECONDS:
                    raise ValueError("previous bridge lease is still active")
                retired.add(previous["boot_id"])
        active = {stable_key(event) for event in events}
        harness_bridges[bridge_id] = {"last_seen": clock, "host_url": host_url,
                                     "boot_id": boot_id, "sequence": sequence, "retired": retired}
        for key, rec in list(pending.items()):
            if (source_host(rec) == HARNESS_SOURCE and rec.get("bridge_id") == bridge_id
                    and key not in active):
                pending.pop(key)
        for event in events:
            key, rec, _ = create_pending(event, defer_notification=True)
            rec["host_url"] = host_url
        save_state()
    return len(active)


def record_link(rec):
    if source_host(rec) == HARNESS_SOURCE:
        # This Harness version has no verified session URL route. Open home only.
        value = rec.get("host_url")
        return value if valid_host_url(value) else ""
    return session_deeplink(rec.get("session_id"))


def acknowledge_card(event):
    """仅供可信 CLI 长连接调用；不是 HTTP 接口，也不执行任何批准操作。"""
    if not callback_ready() or not isinstance(event, dict):
        return False
    if event.get("type") != "card.action.trigger" or event.get("action_tag") != "button":
        return False
    try:
        value = json.loads(event.get("action_value", ""))
    except (ValueError, TypeError):
        return False
    if (not isinstance(value, dict)
            or set(value) != {"action", "reminder_token", "generation"}
            or value.get("action") not in ("ack_reminder", "open_reminder")):
        return False
    token, generation = value.get("reminder_token"), value.get("generation")
    if not isinstance(token, str) or not isinstance(generation, str):
        return False
    # compare_digest 只接受 ASCII 字符串，拒绝畸形值而非抛出异常。
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        return False
    with lock:
        if not callback_ready():
            return False
        for rec in pending.values():
            if not secrets.compare_digest(rec.get("reminder_token", ""), token):
                continue
            operator = rec.get("ack_operator_id")
            fcfg = config.get("feishu", {})
            configured_operator = (fcfg.get("target_id") if fcfg.get("target_type", "user") == "user"
                                   else fcfg.get("ack_operator_id"))
            if (not operator or operator != configured_operator or event.get("operator_id") != operator
                    or rec.get("generation") != generation):
                return False
            if value["action"] == "open_reminder":
                if rec.get("status") not in ("WAITING", "ESCALATED", "EXPIRED", "ACKED"):
                    return False
                link = record_link(rec)
                if not link:
                    return False
                try:
                    result = subprocess.run(["/usr/bin/open", link], shell=False,
                                            capture_output=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    log("callback OPEN: 本机打开请求失败")
                    return False
                log("callback OPEN: " + ("系统已接受请求，未验证 GUI 定位" if result.returncode == 0 else "本机打开请求失败"))
                return result.returncode == 0
            if rec.get("status") == "ACKED":
                return True
            if rec.get("status") not in ("WAITING", "ESCALATED", "EXPIRED"):
                return False
            previous = dict(rec)
            rec.update(status="ACKED", next_reminder_at=None, acked_at=now_ts())
            if not save_state():
                rec.clear()
                rec.update(previous)
                return False
            log("callback ACK: 本地状态已确认持久化")
            return True
    return False


def resolve(payload):
    """工具完成仅解除同一请求；普通工具结果不能解除并发授权。"""
    with lock:
        identity = request_identity(payload)
        fingerprint = operation_fingerprint(payload)
        target_keys = []
        sid = payload.get("session_id")
        if sid and payload.get("event") == "UserPromptSubmit":
            task = task_summary(payload.get("task_title") or payload.get("prompt"))
            if task and (sid not in session_tasks or payload.get("task_title")):
                session_tasks[sid] = task
        if sid and payload.get("event") == "SessionEnd":
            session_tasks.pop(sid, None)
        for k, rec in list(pending.items()):
            if source_host(rec) != source_host(payload):
                continue
            if source_host(rec) == HARNESS_SOURCE and rec.get("bridge_id") != payload.get("bridge_id"):
                continue
            if sid and rec.get("session_id") != sid:
                continue
            if (sid and payload.get("event") in ("PostToolUse", "PostToolUseFailure")
                    and rec.get("event") == "permission_prompt"
                    and not rec.get("request_identity") and not rec.get("operation_fingerprint")):
                # 工具已有进展时只结束泛通知去重窗口，绝不推断具体授权完成。
                target_keys.append(k)
            elif identity:
                if (rec.get("request_identity") == identity
                        or (sid and fingerprint and not rec.get("request_identity")
                            and rec.get("operation_fingerprint") == fingerprint)):
                    target_keys.append(k)
            elif sid and payload.get("event") in ("UserPromptSubmit", "SessionEnd"):
                target_keys.append(k)
            elif sid and fingerprint and not rec.get("request_identity"):
                if rec.get("operation_fingerprint") == fingerprint:
                    target_keys.append(k)
        if not sid and len(target_keys) > 1:
            return []
        removed = []
        for k in set(target_keys):
            rec = pending.pop(k, None)
            if rec:
                removed.append(k)
                log("RESOLVE pending %s (event=%s)" % (k, payload.get("event")))
        if removed:
            save_state()
        return removed


def send_reminder(key, rec, first=False, scheduled=False):
    # Claim/schedule under lock, but do not hold it during Harness notification I/O:
    # heartbeat reconciliation must remain available even if lark-cli is slow.
    with lock:
        if pending.get(key) is not rec or rec.get("status") == "ACKED":
            return
        if first and rec.get("reminder_count", 0) > 0:
            return  # The scheduler already claimed this first delivery.
        if scheduled and (rec.get("status") != "WAITING" or not rec.get("next_reminder_at")
                          or rec["next_reminder_at"] > now_ts()):
            return  # A Hook or another claimant rescheduled it after the due scan.
        if source_host(rec) == HARNESS_SOURCE and not harness_record_live(rec):
            return
        label = {"permission": "需要权限确认", "elicitation": "需要补充信息"}.get(rec.get("type"), "待处理")
        title = host_label(rec) + "｜" + label
        body = reminder_body(rec, now_ts() - rec.get("created_at", now_ts()))
        local_kwargs = {"app_bundle": "com.deepseek.dsh"} if source_host(rec) == HARNESS_SOURCE else {}
        kwargs = {"session_id": rec.get("session_id")}
        if source_host(rec) == HARNESS_SOURCE:
            kwargs["harness_url"] = record_link(rec)
        if (callback_ready() and rec.get("ack_operator_id")
                and rec.get("reminder_token") and rec.get("generation")):
            kwargs["ack_value"] = {"action": "ack_reminder",
                                   "reminder_token": rec["reminder_token"],
                                   "generation": rec["generation"]}
        rec["reminder_count"] = rec.get("reminder_count", 0) + 1
        generic_permission = (rec.get("event") == "permission_prompt"
                              and not rec.get("request_identity") and not rec.get("operation_fingerprint"))
        if generic_permission or rec["reminder_count"] > config.get("max_repeats", MAX_REPEATS):
            rec.update(next_reminder_at=None, status="ESCALATED")
        else:
            idx = min(rec["reminder_count"] - 1, len(REPEAT_DELAYS) - 1)
            rec["next_reminder_at"] = now_ts() + REPEAT_DELAYS[idx]
        save_state()
    notify(title, label, body, group="ar-" + key, sound=not in_quiet_hours(), **local_kwargs)
    with lock:
        if pending.get(key) is not rec or rec.get("status") == "ACKED":
            return
        if source_host(rec) == HARNESS_SOURCE and not harness_record_live(rec):
            return
    notify_feishu(title, body, **kwargs)


# ---------------------------------------------------------------------------
# 调度线程
# ---------------------------------------------------------------------------

def scheduler_loop():
    while not stop_evt.is_set():
        stop_evt.wait(3)
        if stop_evt.is_set():
            break
        with lock:
            due = [k for k, r in pending.items()
                   if r.get("status") == "WAITING" and r.get("next_reminder_at")
                   and (source_host(r) != HARNESS_SOURCE or harness_record_live(r))
                   and now_ts() >= r["next_reminder_at"]]
        for k in due:
            with lock:
                rec = pending.get(k)
                if not rec or rec.get("status") != "WAITING":
                    continue
                if now_ts() - rec.get("created_at", now_ts()) > config.get("total_timeout", TOTAL_TIMEOUT):
                    rec["next_reminder_at"] = None
                    rec["status"] = "EXPIRED"
                    log("EXPIRED pending %s (total timeout)" % k)
                    save_state()
                    continue
            send_reminder(k, rec, scheduled=True)
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/status":
            with lock:
                waiting = [dict({k: v for k, v in r.items()
                                 if k not in ("reminder_token", "generation", "ack_operator_id")},
                                paused=(source_host(r) == HARNESS_SOURCE and not harness_record_live(r)))
                           for r in pending.values() if r.get("status") == "WAITING"]
            self._send(200, {"ok": True, "waiting": len(waiting), "pending": waiting,
                             "ack_callback_ready": callback_ready()})
        elif parsed.path == "/preview-existing":
            # 只预览，不创建测试项、不泄露回调凭据，也不改变调度。
            from urllib.parse import parse_qs
            sid = parse_qs(parsed.query).get("session_id", [""])[0]
            with lock:
                records = [{"key": r["key"], "status": r.get("status"),
                            "body": reminder_body(r, now_ts() - r.get("created_at", now_ts())),
                            "link": record_link(r)}
                           for r in pending.values() if sid and r.get("session_id") == sid]
            self._send(200, {"ok": True, "records": records,
                             "callback_ready": callback_ready(), "created": False})
        elif parsed.path == "/health":
            self._send(200, {"ok": True, "service": "agent-action-reminder", "version": "1.1.0"})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        # A local service is not an invitation for cross-origin browser writes.
        if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(403, {"ok": False, "error": "browser writes are not allowed"})
            return
        if parsed.path == "/harness/snapshot":
            hcfg = config.get("harness", {})
            if hcfg.get("enabled") is not True:
                self._send(403, {"ok": False, "error": "Harness integration is disabled"})
                return
            token = hcfg.get("token", "")
            supplied = self.headers.get("Authorization", "")
            if token and not secrets.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                self._send(403, {"ok": False, "error": "invalid bridge authorization"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                self._send(415, {"ok": False, "error": "application/json required"})
                return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 <= length <= 1024 * 1024:
                raise ValueError("invalid body size")
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("expected object")
        except (ValueError, UnicodeError):
            self._send(400, {"ok": False, "error": "invalid JSON object or body size"})
            return

        if parsed.path == "/harness/snapshot":
            try:
                count = reconcile_harness(payload)
            except ValueError:
                self._send(400, {"ok": False, "error": "invalid Harness snapshot"})
                return
            self._send(200, {"ok": True, "pending": count})
        elif parsed.path == "/event":
            # Harness may enter only through its validated authoritative snapshots.
            if source_host(payload) != "workbuddy":
                self._send(400, {"ok": False, "error": "use the host snapshot endpoint"})
                return
            event = normalize_event(payload)
            payload["event"] = event
            if event in WAIT_EVENTS:
                key, rec, created = create_pending(payload)
                self._send(200, {"ok": True, "action": "create", "key": key, "created": created})
            elif event in RESOLVE_EVENTS:
                removed = resolve(payload)
                self._send(200, {"ok": True, "action": "resolve", "removed": removed})
            else:
                self._send(200, {"ok": True, "action": "ignore", "event": event})
        elif parsed.path == "/test":
            test_payload = {
                "event": "permission_prompt",
                "session_id": "test-session",
                "notification_type": "permission_prompt",
                "message": "这是一条测试提醒：WorkBuddy 正在请求操作确认",
                "cwd": os.getcwd(),
                "permission_mode": "default",
            }
            key, rec, created = create_pending(test_payload)
            self._send(200, {"ok": True, "action": "test", "key": key})
        elif parsed.path == "/stop":
            self._send(200, {"ok": True, "action": "stopping"})
            threading.Thread(target=shutdown).start()
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt, *args):
        return


def shutdown():
    global ACK_CALLBACK_READY
    log("shutdown requested")
    ACK_CALLBACK_READY = False
    stop_evt.set()
    if httpd:
        try:
            httpd.shutdown()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def main():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    load_config()
    load_state()
    port = config.get("port", DEFAULT_PORT)
    if not config.get("enabled", True):
        log("daemon disabled by config, exit")
        sys.exit(0)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if "Address already in use" in str(e):
            log("port %d already in use, another instance running, exit" % port)
            sys.exit(0)
        raise

    global httpd
    httpd = srv

    sched = threading.Thread(target=scheduler_loop, daemon=True)
    sched.start()

    log("Action Reminder daemon started on 127.0.0.1:%d (pid=%d)" % (port, os.getpid()))
    import signal
    # shutdown 必须由其他线程调用，避免 serve_forever 主线程自锁。
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=shutdown).start())
    signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=shutdown).start())
    listener = threading.Thread(target=callback_listener_loop, name="card-callback")
    listener.start()
    try:
        srv.serve_forever()
    finally:
        stop_evt.set()
        listener.join()
        sched.join()
        srv.server_close()
        save_state()
        log("Action Reminder daemon stopped")


if __name__ == "__main__":
    main()
