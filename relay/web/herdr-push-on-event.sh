#!/bin/sh
RELAY="${HERDR_RELAY:-}"
TOKEN="${HERDR_RELAY_TOKEN:-}"
HOST_OVERRIDE="${HERDR_PUSH_HOST:-}"
EDGE_IP="${HERDR_PUSH_EDGE_IP:-}"
if [ -n "$HERDR_PLUGIN_CONFIG_DIR" ] && [ -f "$HERDR_PLUGIN_CONFIG_DIR/.env" ]; then
    [ -z "$RELAY" ] && RELAY=$(grep '^HERDR_RELAY=' "$HERDR_PLUGIN_CONFIG_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
    [ -z "$TOKEN" ] && TOKEN=$(grep '^HERDR_RELAY_TOKEN=' "$HERDR_PLUGIN_CONFIG_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
    [ -z "$HOST_OVERRIDE" ] && HOST_OVERRIDE=$(grep '^HERDR_PUSH_HOST=' "$HERDR_PLUGIN_CONFIG_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
    [ -z "$EDGE_IP" ] && EDGE_IP=$(grep '^HERDR_PUSH_EDGE_IP=' "$HERDR_PLUGIN_CONFIG_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
fi
[ -z "$RELAY" ] && exit 0
export HERDR_PUSH_HOST_RESOLVED="$HOST_OVERRIDE"
if command -v python3 >/dev/null 2>&1; then
    PAYLOADS=$(python3 << 'PYEOF'
import fcntl
import json
import os
import pathlib
import socket
import time

event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}"))
context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
data = event.get("data", {})
host = os.environ.get("HERDR_PUSH_HOST_RESOLVED") or socket.gethostname().split(".")[0]
event_name = str(
    event.get("event") or event.get("type") or data.get("type") or ""
).replace(".", "_")
workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
pane = data.get("pane") if isinstance(data.get("pane"), dict) else {}
pane_id = data.get("pane_id") or pane.get("pane_id") or context.get("focused_pane_id") or ""
workspace_id = (data.get("workspace_id") or workspace.get("workspace_id") or
                context.get("workspace_id") or (pane_id.split(":", 1)[0] if pane_id else ""))


def next_event_seq():
    """Allocate one host-local order value shared by hook and listener."""
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


event_seq = next_event_seq()


payloads = []
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
print("\n".join(json.dumps(payload, separators=(",", ":")) for payload in payloads))
PYEOF
)
else
    exit 0
fi
[ -z "$PAYLOADS" ] && exit 0
HTTP_RELAY=$(echo "$RELAY" | sed 's|^ws://|http://|;s|^wss://|https://|')
printf '%s\n' "$PAYLOADS" | while IFS= read -r PAYLOAD; do
    [ -z "$PAYLOAD" ] && continue
    ENCODED=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$PAYLOAD" 2>/dev/null)
    if [ -n "$EDGE_IP" ] && [ "$HTTP_RELAY" = "https://latent.ngrok.pro" ]; then
        curl -s --max-time 5 --resolve "latent.ngrok.pro:443:$EDGE_IP" \
            -H "Authorization: Bearer $TOKEN" "${HTTP_RELAY}/push?d=${ENCODED}" >/dev/null 2>&1
    else
        curl -s --max-time 5 -H "Authorization: Bearer $TOKEN" "${HTTP_RELAY}/push?d=${ENCODED}" >/dev/null 2>&1
    fi
done
