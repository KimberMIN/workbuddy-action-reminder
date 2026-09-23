#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy Action Reminder — 本地提醒守护进程
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
APP_BUNDLE = "com.tencent.workbuddy.mac"
DEFAULT_PORT = 18789
STATE_DIR = os.path.expanduser("~/.workbuddy/action-reminder")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")
LOG_FILE = os.path.join(STATE_DIR, "daemon.log")
DQ = chr(34)  # 双引号 "

# 重复提醒间隔（秒）。第 1 次立即弹出，之后按此序列追加。
REPEAT_DELAYS = [180, 600, 900, 900]  # 3 分钟、10 分钟、15 分钟、15 分钟
MAX_REPEATS = len(REPEAT_DELAYS)
TOTAL_TIMEOUT = 7200  # 超过 2 小时停止声音提醒，保留待处理中心记录

# 等待类事件 -> 待处理记录类型
WAIT_EVENTS = {
    "permission_prompt": "permission",   # 工具权限审批
    "PermissionRequest": "permission",
    "idle_prompt": "idle",                # 等待用户输入超过 60 秒
    "Elicitation": "elicitation",        # MCP 工具请求输入
}
# 解除类事件 -> 取消该会话/交互的待处理
RESOLVE_EVENTS = {
    "ElicitationResult",
    "UserPromptSubmit",
    "PostToolUse",
    "PermissionDenied",
    "SessionEnd",
}

lock = threading.RLock()
pending = {}        # key -> record(dict)
config = {}
httpd = None
stop_evt = threading.Event()

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
    print(line, flush=True)


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
    }
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            user = json.load(f)
        defaults.update(user)
    except Exception:
        pass
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
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("save_state error: %s" % e)


def load_state():
    global pending
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except Exception:
        pending = {}


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


def stable_key(payload):
    """为一次待处理生成稳定主键，优先使用 interaction_id。"""
    iid = payload.get("interaction_id")
    if iid:
        return "iid:" + str(iid)
    sid = payload.get("session_id") or "unknown"
    ntype = payload.get("notification_type") or payload.get("event") or "event"
    msg = payload.get("message") or ""
    digest = hashlib.md5(("%s|%s|%s" % (sid, ntype, msg)).encode("utf-8")).hexdigest()[:12]
    return "key:" + digest


# ---------------------------------------------------------------------------
# macOS 通知
# ---------------------------------------------------------------------------

def notify(title, subtitle, body, group, sound=True):
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
        # 点击通知：聚焦 WorkBuddy（当前无官方深链接，先聚焦应用）
        cmd += ["-activate", APP_BUNDLE]
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


# ---------------------------------------------------------------------------
# 待处理记录管理
# ---------------------------------------------------------------------------

def create_pending(payload):
    key = stable_key(payload)
    with lock:
        rec = pending.get(key)
        etype = WAIT_EVENTS.get(payload.get("event"), "idle")
        if rec and rec.get("status") == "WAITING":
            # 已存在且等待中：刷新最后出现时间，避免重复创建
            rec["last_seen"] = now_ts()
            rec["message"] = payload.get("message", rec.get("message", ""))
            save_state()
            return key, rec, False
        rec = {
            "key": key,
            "session_id": payload.get("session_id"),
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
        pending[key] = rec
        save_state()
    log("CREATE pending %s type=%s session=%s" % (key, etype, rec.get("session_id")))
    send_reminder(key, rec, first=True)
    return key, rec, True


def resolve(payload):
    """取消待处理。优先按 interaction_id，否则按 session_id 整会话清理。"""
    with lock:
        iid = payload.get("interaction_id")
        target_keys = []
        if iid:
            k = "iid:" + str(iid)
            if k in pending:
                target_keys.append(k)
        sid = payload.get("session_id")
        if sid:
            for k, rec in list(pending.items()):
                if rec.get("session_id") == sid and rec.get("status") == "WAITING":
                    target_keys.append(k)
        removed = []
        for k in set(target_keys):
            rec = pending.pop(k, None)
            if rec:
                removed.append(k)
                log("RESOLVE pending %s (event=%s)" % (k, payload.get("event")))
        if removed:
            save_state()
        return removed


def send_reminder(key, rec, first=False):
    etype = rec.get("type", "idle")
    label = {"permission": "需要权限确认", "idle": "等待你的输入", "elicitation": "需要补充信息"}.get(etype, "待处理")
    title = "WorkBuddy · 待操作提醒"
    subtitle = label
    wait_sec = now_ts() - rec.get("created_at", now_ts())
    count = rec.get("reminder_count", 0)
    session = rec.get("session_id") or "未知会话"
    cwd = rec.get("cwd") or ""
    msg = rec.get("message") or ""
    body_parts = []
    if msg:
        body_parts.append(msg)
    body_parts.append("会话：%s" % session)
    if cwd:
        body_parts.append("目录：%s" % cwd)
    body_parts.append("已等待 %s" % fmt_wait(wait_sec))
    with lock:
        total = sum(1 for r in pending.values() if r.get("status") == "WAITING")
    if total > 1:
        body_parts.append("当前共有 %d 项待处理" % total)
    if not first and count >= 1:
        body_parts.append("（第 %d 次提醒，点击聚焦 WorkBuddy）" % count)
    body = " | ".join(body_parts)
    notify(title, subtitle, body, group="wb-ar-" + key, sound=not in_quiet_hours())
    rec["reminder_count"] = count + 1
    cfg_repeats = config.get("max_repeats", MAX_REPEATS)
    if rec["reminder_count"] <= cfg_repeats:
        idx = min(rec["reminder_count"] - 1, len(REPEAT_DELAYS) - 1)
        rec["next_reminder_at"] = now_ts() + REPEAT_DELAYS[idx]
    else:
        rec["next_reminder_at"] = None
        rec["status"] = "ESCALATED"
        log("ESCALATED pending %s after %d reminders" % (key, rec["reminder_count"]))
    save_state()


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
                   if r.get("status") == "WAITING" and r.get("next_reminder_at") and now_ts() >= r["next_reminder_at"]]
            for k in due:
                rec = pending.get(k)
                if not rec:
                    continue
                if now_ts() - rec.get("created_at", now_ts()) > config.get("total_timeout", TOTAL_TIMEOUT):
                    rec["next_reminder_at"] = None
                    rec["status"] = "EXPIRED"
                    log("EXPIRED pending %s (total timeout)" % k)
                    continue
                send_reminder(k, rec)
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
                waiting = [r for r in pending.values() if r.get("status") == "WAITING"]
            self._send(200, {"ok": True, "waiting": len(waiting), "pending": waiting})
        elif parsed.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._send(400, {"ok": False, "error": "bad json: %s" % e})
            return

        if parsed.path == "/event":
            event = payload.get("event") or payload.get("hook_event_name")
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
    log("shutdown requested")
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
    load_config()
    load_state()
    port = int(config.get("port", DEFAULT_PORT))
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
    signal.signal(signal.SIGTERM, lambda *_: shutdown())
    signal.signal(signal.SIGINT, lambda *_: shutdown())
    try:
        srv.serve_forever()
    finally:
        save_state()
        log("Action Reminder daemon stopped")


if __name__ == "__main__":
    main()
