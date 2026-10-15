#!/usr/bin/env python3
"""Local plugin hook — pushes event to herdr-remote relay via UDP."""
import fcntl
import json
import os
import pathlib
import shutil
import socket
import subprocess
import time

event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}"))
context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
data = event.get("data", {})
event_name = str(
    event.get("event") or event.get("type") or data.get("type") or ""
).replace(".", "_")
workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
pane = data.get("pane") if isinstance(data.get("pane"), dict) else {}
cwd = (
    data.get("cwd")
    or context.get("focused_pane_cwd")
    or context.get("workspace_cwd")
    or context.get("cwd")
    or ""
)
workspace_label = context.get("workspace_label") or os.path.basename(cwd)
pane_id = (
    data.get("pane_id")
    or pane.get("pane_id")
    or context.get("focused_pane_id")
    or ""
)
workspace_id = (
    data.get("workspace_id")
    or workspace.get("workspace_id")
    or context.get("workspace_id")
    or (pane_id.split(":", 1)[0] if pane_id else "")
)
tab_id = (
    data.get("tab_id")
    or pane.get("tab_id")
    or context.get("tab_id")
    or context.get("focused_tab_id")
    or ""
)
tab_label = context.get("tab_label") or context.get("focused_tab_label") or ""
host = socket.gethostname().split(".")[0]
herdr = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")


def next_event_seq():
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR", "")
    if not config_dir:
        return time.time_ns()
    path = pathlib.Path(config_dir) / "event-seq"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        try:
            previous = int(handle.read().strip() or "0")
        except ValueError:
            previous = 0
        value = max(previous + 1, time.time_ns())
        handle.seek(0)
        handle.truncate()
        handle.write(str(value))
        handle.flush()
        os.fsync(handle.fileno())
        return value


payloads = []
event_seq = next_event_seq()
if event_name == "workspace_closed":
    payloads = [{
        "type": "workspace_removed",
        "workspace_id": workspace_id,
        "host": host,
        "event_seq": event_seq,
    }]
elif event_name == "pane_closed":
    payloads = [{
        "type": "pane_removed",
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "host": host,
        "event_seq": event_seq,
    }]
elif event_name == "pane_agent_status_changed" and pane_id and herdr:
    try:
        pane_result = subprocess.run(
            [herdr, "pane", "get", pane_id],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        )
        current = json.loads(pane_result.stdout).get("result", {}).get("pane", {})
    except Exception:
        current = {}
    if current:
        current_cwd = current.get("cwd") or cwd
        label = workspace_label or os.path.basename(current_cwd)
        current_tab_id = current.get("tab_id") or tab_id
        current_tab_label = tab_label
        if current_tab_id and not current_tab_label:
            try:
                tab_result = subprocess.run(
                    [herdr, "tab", "get", current_tab_id],
                    capture_output=True,
                    text=True,
                    timeout=4,
                    check=True,
                )
                current_tab_label = (
                    json.loads(tab_result.stdout)
                    .get("result", {})
                    .get("tab", {})
                    .get("label", "")
                )
            except Exception:
                current_tab_label = ""
        status = str(current.get("agent_status") or "unknown").lower()
        payload_data = {
            "type": "agent_event",
            "pane_id": current.get("pane_id") or pane_id,
            "status": status,
            "agent": str(current.get("agent") or data.get("agent") or
                         data.get("display_agent") or "").lower(),
            "project": label,
            "workspace_label": label,
            "workspace_id": current.get("workspace_id") or workspace_id,
            "tab_id": current_tab_id,
            "tab_label": current_tab_label,
            "cwd": current_cwd,
            "host": host,
            "event_seq": event_seq,
        }
        if status == "blocked":
            try:
                result = subprocess.run(
                    [
                        herdr, "pane", "read", pane_id, "--lines", "60",
                        "--source", "recent-unwrapped",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=4,
                )
                payload_data["prompt"] = result.stdout[-12000:]
            except Exception:
                pass
        payloads = [payload_data]

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for payload_data in payloads:
    sock.sendto(json.dumps(payload_data).encode(), ("127.0.0.1", 8376))
sock.close()
