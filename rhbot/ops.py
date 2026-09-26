"""Heartbeat, kill file, and the systemd notify ping.

The kill switch is a file. If the file exists, or cannot be read, new
simulated risk is blocked. Clearing it is an explicit operator action.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from rhbot.money import canonical

KILL_NAME = "KILL"
FREEZE_NAME = "DRAWDOWN_FREEZE"
HEARTBEAT_NAME = "heartbeat.json"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return ts.astimezone(timezone.utc).isoformat()


def kill_path(state_dir: Path) -> Path:
    return Path(state_dir) / KILL_NAME


def freeze_path(state_dir: Path) -> Path:
    return Path(state_dir) / FREEZE_NAME


def heartbeat_path(state_dir: Path) -> Path:
    return Path(state_dir) / HEARTBEAT_NAME


def kill_active(state_dir: Path) -> bool:
    """Fail closed: a kill file we cannot parse still counts as on."""
    path = kill_path(state_dir)
    if not path.exists():
        return False
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    if not raw:
        return True
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return True
    return True


def read_kill(state_dir: Path) -> dict | None:
    path = kill_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"reason": "unreadable kill file", "by": "unknown", "at": None}
    if not isinstance(data, dict):
        return {"reason": "malformed kill file", "by": "unknown", "at": None}
    return data


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def engage_kill(state_dir: Path, reason: str, by: str, *, ack_required: bool = False) -> dict:
    """Create the kill file if it is not already there. The first reason wins.

    A drawdown kill passes ``ack_required``. If a kill file is already present,
    that flag is upgraded to true. The file is never cleared here.
    """
    path = kill_path(state_dir)
    if path.exists():
        existing = read_kill(state_dir) or {}
        if existing.get("by") == "unknown" or not existing.get("reason"):
            return existing
        if ack_required and not existing.get("ack_required"):
            existing["ack_required"] = True
            existing["drawdown_reason"] = reason
            atomic_write(path, json.dumps(existing, sort_keys=True))
        return existing
    payload = {
        "ack_required": bool(ack_required),
        "at": iso(utcnow()),
        "by": by,
        "reason": reason,
    }
    atomic_write(path, json.dumps(payload, sort_keys=True))
    return payload


def resume_needs_ack(payload: dict | None) -> bool:
    """A drawdown kill, or a kill file we cannot read, needs ``rhbot resume --ack``."""
    if not payload:
        return True
    if payload.get("by") == "unknown":
        return True
    return bool(payload.get("ack_required"))


def clear_kill(state_dir: Path) -> bool:
    path = kill_path(state_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


def _file_active(path: Path) -> bool:
    """Fail closed: a control file we cannot parse still counts as on."""
    if not path.exists():
        return False
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return True
    if not raw:
        return True
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return True
    return True


def freeze_active(state_dir: Path) -> bool:
    """A paper drawdown freeze blocks new buys until a human acknowledges it."""
    return _file_active(freeze_path(state_dir))


def read_freeze(state_dir: Path) -> dict | None:
    path = freeze_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"reason": "unreadable drawdown freeze", "by": "unknown", "at": None}
    if not isinstance(data, dict):
        return {"reason": "malformed drawdown freeze", "by": "unknown", "at": None}
    return data


def engage_freeze(state_dir: Path, reason: str, by: str) -> dict:
    """Create the freeze file if it is not already there. The first reason wins.

    The engine never deletes this file. The operator clears it with
    ``rhbot ack-drawdown --reason``. That does not reset the drawdown peak
    and does not clear a kill. A 40% kill stays human-only ``rhbot resume --ack``.
    """
    path = freeze_path(state_dir)
    if path.exists():
        return read_freeze(state_dir) or {}
    payload = {
        "at": iso(utcnow()),
        "by": by,
        "reason": reason,
    }
    atomic_write(path, json.dumps(payload, sort_keys=True))
    return payload


def clear_freeze(state_dir: Path) -> bool:
    path = freeze_path(state_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


def write_heartbeat(state_dir: Path, body: dict) -> None:
    payload = {"ts": iso(utcnow()), "pid": os.getpid(), "mode": "paper", **body}
    atomic_write(heartbeat_path(state_dir), canonical(payload))


def read_heartbeat(state_dir: Path) -> dict | None:
    path = heartbeat_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"_unreadable": True}
    if not isinstance(data, dict):
        return {"_unreadable": True}
    return data


def sd_notify(message: str) -> None:
    """Ping systemd if NOTIFY_SOCKET is set. No-op otherwise."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(message.encode("utf-8"), addr)
    except OSError:
        return
    finally:
        sock.close()
