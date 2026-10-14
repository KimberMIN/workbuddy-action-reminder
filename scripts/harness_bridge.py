"""Validated, read-only Harness snapshot protocol (no authorization actions)."""
import re

SOURCE = "deepseek-harness"
LEASE_SECONDS = 15
MAX_PENDING = 512


def valid_host_url(value):
    """Never accept remote origins, credentials, paths, query tokens or fragments."""
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", value)
    return bool(match and int(match.group(1)) <= 65535)


def utf8_text(value):
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def identifier(value, limit=256):
    return (utf8_text(value) and 0 < len(value) <= limit
            and value.strip() == value
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value))


def validate_snapshot(payload):
    """Validate the ENTIRE snapshot before reconciling any existing request."""
    if not isinstance(payload, dict):
        raise ValueError("snapshot must be an object")
    bridge_id = payload.get("bridge_id")
    if not isinstance(bridge_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", bridge_id):
        raise ValueError("invalid bridge_id")
    boot_id, sequence = payload.get("boot_id"), payload.get("sequence")
    if not isinstance(boot_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", boot_id):
        raise ValueError("invalid boot_id")
    if type(sequence) is not int or not 1 <= sequence <= 9007199254740991:
        raise ValueError("invalid sequence")
    host_url = payload.get("host_url")
    if not valid_host_url(host_url):
        raise ValueError("invalid host_url")
    items = payload.get("pending")
    if not isinstance(items, list) or len(items) > MAX_PENDING:
        raise ValueError("invalid pending list")
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid pending item")
        sid, iid, kind = (item.get(k) for k in ("session_id", "interaction_id", "kind"))
        if not identifier(sid) or not identifier(iid) or kind not in ("permission", "question"):
            raise ValueError("invalid request identity")
        key = (sid, iid)
        if key in seen:
            raise ValueError("duplicate request identity")
        seen.add(key)
        event = {
            "source": SOURCE, "bridge_id": bridge_id, "host_url": host_url,
            "event": "PermissionRequest" if kind == "permission" else "Elicitation",
            "session_id": sid, "interaction_id": iid,
        }
        for field in ("tool_name", "question", "message", "task_title"):
            value = item.get(field, item.get("title", "") if field == "task_title" else "")
            if not utf8_text(value) or len(value) > 4096:
                raise ValueError("invalid request summary")
            event[field] = value
        result.append(event)
    return bridge_id, host_url, result
