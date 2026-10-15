#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "zeroconf>=0.80.0", "pywebpush>=2.0.0", "py-vapid>=1.9.0"]
# ///
"""herdr-remote relay — polls herdr, accepts push events (HTTP POST + WebSocket + UDP), broadcasts to clients."""
import asyncio
import json
import logging
import mimetypes
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

try:
    from websockets.asyncio.server import serve
except ImportError:
    from websockets.server import serve
from websockets.exceptions import ConnectionClosed

def _get_log_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/herdr-remote")
    if os.path.isdir("/var/log") and os.access("/var/log", os.W_OK):
        return "/var/log/herdr-remote"
    return os.path.expanduser("~/.local/state/herdr-remote/log")

LOG_DIR = os.environ.get("HERDR_LOG_DIR", _get_log_dir())
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "relay.log")
AUDIT_FILE = os.path.join(LOG_DIR, "audit.log")

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.setFormatter(_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

log = logging.getLogger("herdr-relay")
log.setLevel(logging.INFO)
log.addHandler(_file_handler)
log.addHandler(_console_handler)
logging.getLogger("websockets").setLevel(logging.WARNING)

def find_herdr_bin():
    candidates = (
        os.environ.get("HERDR_BIN"),
        os.environ.get("HERDR_BIN_PATH"),
        shutil.which("herdr"),
        str(pathlib.Path.home() / ".local/bin/herdr"),
        "/opt/homebrew/bin/herdr",
    )
    return next((candidate for candidate in candidates if candidate and pathlib.Path(candidate).is_file()), "herdr")


def positive_seconds_env(name, default):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


HERDR = find_herdr_bin()
WS_PORT = int(os.environ.get("HERDR_RELAY_PORT", "8375"))
POLL_INTERVAL = 2
BLOCKED_FALLBACK_DELAY = 0.8
REMOTE_HOST_TIMEOUT = positive_seconds_env("HERDR_REMOTE_HOST_TIMEOUT", 90)
CLIENT_SEND_TIMEOUT = positive_seconds_env("HERDR_CLIENT_SEND_TIMEOUT", 3)
AUTH_TOKEN = os.environ.get("HERDR_RELAY_TOKEN", "")  # Optional: shared secret for relay auth
WEB_DIR = pathlib.Path(__file__).parent / "web"
LOCAL_HOST = os.environ.get("HERDR_HOST") or os.uname().nodename.split(".")[0]

# VAPID Web Push
VAPID_PUBLIC_KEY = os.environ.get("HERDR_VAPID_PUBLIC", "")
VAPID_PRIVATE_KEY = os.environ.get("HERDR_VAPID_PRIVATE", "")
VAPID_SUBJECT = os.environ.get("HERDR_VAPID_SUBJECT", "mailto:herdr@localhost")
push_subscriptions = []  # list of PushSubscription dicts
PUSH_SUBS_FILE = os.path.join(LOG_DIR, "push_subs.json")

# Remote hosts: comma-separated SSH targets
REMOTES = [r.strip() for r in os.environ.get("HERDR_REMOTES", "").split(",") if r.strip()]

TOOL_OPTIONS = ["yes, single permission", "trust, always allow", "no (tab to edit)"]
CODEX_TOOL_OPTIONS = [
    "yes, proceed (y)",
    "yes, and don't ask again (p)",
    "no, and tell Codex what to do differently (esc)",
]
SUBAGENT_OPTIONS = ["approve all pending", "configure individually", "exit (cancel subagents)"]
CHROME_RE = re.compile(
    r"^[\s─━═_—│|╭╮╰╯┌┐└┘┬┴├┤┼◔◑◕●]+$"
    r"|Kiro\s[·•]"
    r"|esc to cancel"
    r"|type to queue"
    r"|^\s*[◔◑◕●]\s+(Shell|Bash)"
)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OPTION_LINE_RE = re.compile(
    r"^\s*[›❯>]?\s*(?:\d+[.)]\s*)?"
    r"(?:yes|trust|no|approve all|configure|cancel|exit)\b",
    re.IGNORECASE,
)
NUMBERED_OPTION_RE = re.compile(
    r"^\s*[›❯>]?\s*(\d+)[.)]\s+(.+?)\s*$",
    re.IGNORECASE,
)
HELP_LINE_RE = re.compile(
    r"(?:press enter to confirm|enter to submit|tab to (?:amend|edit)|"
    r"esc to cancel|type to queue)",
    re.IGNORECASE,
)
BLOCKED_HEADER_RE = re.compile(
    r"would you like to (?:run|use|execute)\b.*\?"
    r"|^\s*question\s+\d+\s*(?:/|of)\s*\d+\b",
    re.IGNORECASE,
)
clients = set()
event_queue = asyncio.Queue()
pane_remote_map = {}
last_statuses = {}

SAFE_RESPONSES = {"y", "n", "a", "yes", "no", "trust", "yes, single permission", "trust, always allow", "no (tab to edit)", "approve all pending", "configure individually", "exit (cancel subagents)"}
SAFE_KEYS = {"y", "n", "a", "Enter", "Tab", "Escape", "C-c", "Ctrl+c", "Ctrl+C", "Up", "Down", "Left", "Right", "BSpace"} | {
    str(number) for number in range(10)
}

# --- Audit logging ---
_audit_handler = RotatingFileHandler(AUDIT_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
audit_log = logging.getLogger("herdr-audit")
audit_log.setLevel(logging.INFO)
audit_log.addHandler(_audit_handler)
audit_log.propagate = False


def audit(action: str, ip: str, device: str, pane_id: str, detail: str = ""):
    """Append a write action to the audit log as structured JSONL."""
    import datetime
    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "action": action,
        "paneId": pane_id,
        "ip": ip,
        "device": device,
    }
    if detail:
        entry["detail"] = detail[:120]  # truncate like collie
    audit_log.info(json.dumps(entry, separators=(",", ":")))


# --- Web Push helpers ---
def _load_push_subs():
    global push_subscriptions
    if os.path.isfile(PUSH_SUBS_FILE):
        try:
            with open(PUSH_SUBS_FILE) as f:
                push_subscriptions = json.load(f)
        except Exception:
            push_subscriptions = []


def _save_push_subs():
    with open(PUSH_SUBS_FILE, "w") as f:
        json.dump(push_subscriptions, f)


async def send_web_push(title: str, body: str, url: str = "/", clear: bool = False):
    """Send push notification to all registered subscriptions.

    Uses collapse topic + TTL so offline devices get only the latest.
    If clear=True, sends a clear instruction instead of showing a notification.
    """
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush
    except ImportError:
        log.warning("pywebpush not installed, skipping push")
        return
    if clear:
        payload = json.dumps({"type": "clear", "tag": "herdr-blocked"})
    else:
        payload = json.dumps({"title": title, "body": body, "url": url})
    headers = {"Topic": "herdr-herd", "TTL": "21600"}  # 6h TTL, collapse key
    dead = []
    for i, sub in enumerate(push_subscriptions):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                headers=headers,
            )
        except Exception as e:
            log.warning("Push failed for sub %d: %s", i, e)
            if "410" in str(e) or "404" in str(e):
                dead.append(i)
    if dead:
        for i in reversed(dead):
            push_subscriptions.pop(i)
        _save_push_subs()

_load_push_subs()


def run_herdr_result(*args, remote=None):
    if remote:
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, HERDR, *args]
    else:
        cmd = [HERDR, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


agents_state = {}  # host:pane_id -> agent dict, accumulated across poll + push events
pending_blocked_tasks = {}
remote_commands = {}  # host -> queued commands for push-only machines
remote_command_events = {}  # host -> asyncio.Event for authenticated long polling
listener_synced_hosts = set()  # reset on relay restart; triggers one inventory sync per host
ordered_hosts = set()  # hosts upgraded to ordered push payloads
pane_event_seq = {}  # host:pane_id -> newest applied sender sequence
workspace_event_seq = {}  # (host, workspace_id) -> newest workspace removal
host_snapshot_seq = {}  # host -> newest full inventory snapshot
host_last_seen = {}  # push-only host -> newest event or listener heartbeat


def run_herdr(*args, remote=None):
    try:
        return run_herdr_result(*args, remote=remote).stdout.strip()
    except Exception:
        return ""


def get_workspace_labels(remote=None):
    raw = run_herdr("workspace", "list", remote=remote)
    try:
        data = json.loads(raw)
        workspaces = data.get("result", {}).get("workspaces", [])
        return {
            workspace["workspace_id"]: workspace.get("label", "")
            for workspace in workspaces
            if workspace.get("workspace_id")
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def get_tab_labels(remote=None):
    raw = run_herdr("tab", "list", remote=remote)
    try:
        data = json.loads(raw)
        tabs = data.get("result", {}).get("tabs", [])
        return {
            tab["tab_id"]: tab.get("label", "")
            for tab in tabs
            if tab.get("tab_id")
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}


def get_agents_from_host(remote=None):
    snapshot = get_agents_snapshot(remote=remote)
    return snapshot if snapshot is not None else []


def get_agents_snapshot(remote=None):
    """Return an authoritative host snapshot, or None when polling failed."""
    raw = run_herdr("pane", "list", remote=remote)
    host_label = remote or LOCAL_HOST
    try:
        data = json.loads(raw)
        panes = data.get("result", {}).get("panes", [])
        workspace_labels = get_workspace_labels(remote=remote)
        tab_labels = get_tab_labels(remote=remote)
        return [
            {
                "pane_id": p["pane_id"],
                "agent": p.get("agent", ""),
                "label": p.get("label", ""),
                "status": p.get("agent_status", "unknown"),
                "cwd": p.get("cwd", ""),
                "project": workspace_labels.get(
                    p.get("workspace_id"), os.path.basename(p.get("cwd", ""))
                ),
                "workspace_id": p.get("workspace_id", ""),
                "tab_id": p.get("tab_id", ""),
                "tab_label": tab_labels.get(p.get("tab_id"), ""),
                "host": host_label,
                "remote": remote,
            }
            for p in panes if p.get("agent")
        ]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def get_all_agents():
    agents = get_agents_from_host(remote=None)
    for remote in REMOTES:
        agents.extend(get_agents_from_host(remote=remote))
    return agents


def agent_key(host, pane_id):
    return f"{normalize_host(host)}:{pane_id}"


def normalize_host(host):
    return LOCAL_HOST if not host or host in ("local", "localhost") else host


def note_host_activity(host, seen_at=None):
    """Record an authenticated push-only host event or listener heartbeat."""
    host = normalize_host(host)
    if host == LOCAL_HOST:
        return
    host_last_seen[host] = time.time() if seen_at is None else seen_at


def reset_host_delivery_state(host):
    """Allow an expired or reinstalled sender to establish fresh ordering."""
    host = normalize_host(host)
    listener_synced_hosts.discard(host)
    ordered_hosts.discard(host)
    host_snapshot_seq.pop(host, None)
    remote_commands.pop(host, None)
    command_event = remote_command_events.pop(host, None)
    if command_event:
        command_event.set()

    prefix = f"{host}:"
    for key in list(pane_event_seq):
        if key.startswith(prefix):
            pane_event_seq.pop(key, None)
    for key in list(workspace_event_seq):
        if key[0] == host:
            workspace_event_seq.pop(key, None)


def expire_inactive_hosts(now=None):
    """Remove push-only hosts whose listener heartbeat has stopped."""
    now = time.time() if now is None else now
    expired_hosts = [
        host for host, last_seen in host_last_seen.items()
        if now - last_seen > REMOTE_HOST_TIMEOUT
    ]
    removed = []
    for host in expired_hosts:
        for key, agent in list(agents_state.items()):
            if normalize_host(agent.get("host")) != host:
                continue
            if remove_agent(key):
                removed.append(key)
        host_last_seen.pop(host, None)
        reset_host_delivery_state(host)
    return removed


def push_event_host(event):
    """Keep the sender's machine identity even when pane IDs collide."""
    return normalize_host(event.get("host"))


def workspace_label(event):
    value = event.get("workspace_label") or event.get("workspace")
    if isinstance(value, dict):
        value = value.get("label") or value.get("name")
    return str(value or "")


def parse_event_seq(event):
    value = event.get("event_seq")
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def event_floor(host, pane_id="", workspace_id=""):
    values = [host_snapshot_seq.get(host, 0)]
    if pane_id:
        values.append(pane_event_seq.get(agent_key(host, pane_id), 0))
    if workspace_id:
        values.append(workspace_event_seq.get((host, workspace_id), 0))
    return max(values)


def accept_agent_event(event, host):
    """Reject legacy or late status events after ordered delivery is active."""
    host = normalize_host(host)
    seq = parse_event_seq(event)
    if seq is None:
        return host not in ordered_hosts

    ordered_hosts.add(host)
    pane_id = event.get("pane_id", "")
    workspace_id = event.get("workspace_id", "")
    if seq <= event_floor(host, pane_id, workspace_id):
        return False
    pane_event_seq[agent_key(host, pane_id)] = seq
    return True


def remove_agent(key):
    cancel_blocked_fallback(key)
    state = agents_state.pop(key, None)
    pane_remote_map.pop(key, None)
    last_statuses.pop(key, None)
    return state


def pane_is_known(host, pane_id):
    return agent_key(host, pane_id) in agents_state


def reconcile_polled_host(host, active_keys):
    """Drop states absent from a successfully polled host snapshot."""
    host = normalize_host(host)
    removed = []
    for key, agent in list(agents_state.items()):
        if normalize_host(agent.get("host")) == host and key not in active_keys:
            if remove_agent(key):
                removed.append(key)
    return removed


def merge_polled_agent(agent):
    """Restore missing inventory without creating a status transition."""
    key = agent_key(agent.get("host"), agent["pane_id"])
    previous = agents_state.get(key, {})
    status = agent.get("status", "unknown")
    now = time.time()

    if previous:
        merged = {
            **previous,
            "agent": agent.get("agent") or previous.get("agent", ""),
            "cwd": agent.get("cwd") or previous.get("cwd", ""),
            "project": agent.get("project") or previous.get("project", ""),
            "workspace_id": agent.get("workspace_id") or previous.get("workspace_id", ""),
            "tab_id": (
                agent.get("tab_id", "")
                if "tab_id" in agent
                else previous.get("tab_id", "")
            ),
            "tab_label": (
                agent.get("tab_label", "")
                if "tab_label" in agent
                else previous.get("tab_label", "")
            ),
            "remote": agent.get("remote", previous.get("remote")),
            "last_seen_at": now,
        }
        agents_state[key] = merged
        return key, merged

    merged = {
        **agent,
        "status": status,
        "last_seen_at": now,
    }
    agents_state[key] = merged
    return key, merged


def merge_agent_event(event, host):
    host = normalize_host(host)
    key = agent_key(host, event["pane_id"])
    previous = agents_state.get(key, {})
    status = event.get("status") or previous.get("status", "unknown")
    now = time.time()
    merged = {
        **previous,
        "pane_id": event["pane_id"],
        "agent": event.get("agent") or previous.get("agent", ""),
        "status": status,
        "cwd": event.get("cwd") or previous.get("cwd", ""),
        "project": (
            workspace_label(event)
            or event.get("project")
            or previous.get("project", "")
        ),
        "workspace_id": (
            event.get("workspace_id")
            or previous.get("workspace_id")
            or event["pane_id"].split(":", 1)[0]
        ),
        "tab_id": (
            event.get("tab_id", "")
            if "tab_id" in event
            else previous.get("tab_id", "")
        ),
        "tab_label": (
            event.get("tab_label", "")
            if "tab_label" in event
            else previous.get("tab_label", "")
        ),
        "host": host,
        "last_seen_at": now,
    }

    agents_state[key] = merged
    return key, merged


def terminal_lines(raw):
    return [ANSI_RE.sub("", raw_line).rstrip() for raw_line in raw.splitlines()]


def find_numbered_option_group(lines):
    """Return the last consecutive 1..N menu and its starting line."""
    groups = []
    current = []
    start = None

    def finish_group():
        nonlocal current, start
        if start is not None and len(current) >= 2:
            groups.append((start, current))
        current = []
        start = None

    for index, line in enumerate(lines):
        stripped = line.strip()
        match = NUMBERED_OPTION_RE.match(stripped)
        if match:
            number = int(match.group(1))
            label = match.group(2).strip()
            if number == 1:
                finish_group()
                start = index
                current = [label]
            elif start is not None and number == len(current) + 1:
                current.append(label)
            else:
                finish_group()
            continue

        if start is not None and stripped:
            if HELP_LINE_RE.search(stripped):
                finish_group()
            elif line[:1].isspace() and not CHROME_RE.search(stripped):
                current[-1] = f"{current[-1]} {stripped}"

    finish_group()
    return groups[-1] if groups else None


def clean_blocked_content(raw):
    """Extract the request body while dropping its numbered choice menu."""
    raw_lines = terminal_lines(raw)
    option_group = find_numbered_option_group(raw_lines)
    content_lines = raw_lines[:option_group[0]] if option_group else raw_lines

    start = 0
    for index, line in enumerate(content_lines):
        if BLOCKED_HEADER_RE.search(line.strip()):
            start = index

    lines = []
    for line in content_lines[start:]:
        stripped = line.strip()
        if not stripped or CHROME_RE.search(stripped):
            continue
        if NUMBERED_OPTION_RE.match(stripped) or OPTION_LINE_RE.search(stripped):
            continue
        if HELP_LINE_RE.search(stripped):
            continue
        lines.append(line)
    return "\n".join(lines[-20:]).strip()


def read_pane(pane_id, remote=None):
    content, _ = read_blocked_pane(pane_id, remote=remote)
    return content


def read_blocked_pane(pane_id, remote=None):
    raw = run_herdr(
        "pane", "read", pane_id, "--lines", "60",
        "--source", "recent-unwrapped", remote=remote,
    )
    return clean_blocked_content(raw), detect_options(raw)


def detect_options(text):
    option_group = find_numbered_option_group(terminal_lines(text))
    if option_group:
        return option_group[1]

    lower = text.lower()
    if "yes, proceed" in lower and "don't ask again" in lower:
        return CODEX_TOOL_OPTIONS
    if "yes, single permission" in lower:
        return TOOL_OPTIONS
    if "approve all pending" in lower:
        return SUBAGENT_OPTIONS


def response_keys(response):
    choice = re.fullmatch(r"__choice__:(\d+)", response)
    if choice:
        choice_number = max(1, int(choice.group(1)))
        return ["Down"] * (choice_number - 1) + ["Enter"]
    return None


def response_is_safe(response):
    return (
        response.strip().lower() in SAFE_RESPONSES
        or re.fullmatch(r"__choice__:[1-9]", response) is not None
    )


def queue_remote_command(host, command):
    host = normalize_host(host)
    remote_commands.setdefault(host, []).append(command)
    event = remote_command_events.get(host)
    if event:
        event.set()


def pop_remote_command(host):
    queue = remote_commands.get(normalize_host(host), [])
    if not queue:
        return None
    command = queue.pop(0)
    if not queue:
        remote_commands.pop(normalize_host(host), None)
    return command


def command_for_listener(host):
    """Ask each listener for one inventory snapshot after a relay restart."""
    host = normalize_host(host)
    note_host_activity(host)
    if host not in listener_synced_hosts:
        listener_synced_hosts.add(host)
        return {"type": "sync_snapshot"}
    return pop_remote_command(host)


def direct_pane_target(host, pane_id):
    host = normalize_host(host)
    key = agent_key(host, pane_id)
    if host == LOCAL_HOST:
        return True, None
    if key in pane_remote_map:
        return True, pane_remote_map[key]
    return False, None


def dispatch_keys(host, pane_id, keys):
    direct, remote = direct_pane_target(host, pane_id)
    if direct:
        return run_herdr_result("pane", "send-keys", pane_id, *keys, remote=remote)
    else:
        queue_remote_command(host, {
            "type": "send_keys",
            "pane_id": pane_id,
            "keys": keys,
        })
    return None


def dispatch_text(host, pane_id, text):
    direct, remote = direct_pane_target(host, pane_id)
    if direct:
        run_herdr("pane", "send-text", pane_id, text, remote=remote)
    else:
        queue_remote_command(host, {
            "type": "send_text",
            "pane_id": pane_id,
            "text": text,
        })


def dispatch_response(host, pane_id, response):
    keys = response_keys(response)
    if keys:
        dispatch_keys(host, pane_id, keys)
    else:
        dispatch_text(host, pane_id, response + "\n")


async def broadcast(msg):
    data = json.dumps(msg)
    recipients = tuple(clients)
    if not recipients:
        return

    async def send(ws):
        try:
            await asyncio.wait_for(ws.send(data), timeout=CLIENT_SEND_TIMEOUT)
        except Exception:
            return ws
        return None

    results = await asyncio.gather(*(send(ws) for ws in recipients))
    dead = {ws for ws in results if ws is not None}
    if dead:
        log.debug("Removed %d dead client(s)", len(dead))
        clients.difference_update(dead)


def cancel_blocked_fallback(key):
    task = pending_blocked_tasks.pop(key, None)
    if task:
        task.cancel()


def blocked_message(agent, prompt, options=None, **details):
    return {
        "type": "blocked",
        "pane_id": agent["pane_id"],
        "agent": agent.get("agent", ""),
        "project": agent.get("project", ""),
        "tab_id": agent.get("tab_id", ""),
        "tab_label": agent.get("tab_label", ""),
        "host": agent.get("host", "local"),
        "prompt": (prompt or "Agent is blocked")[:1200],
        "options": options or [],
        "tool": details.get("tool", ""),
        "command": details.get("command", "")[:2000],
    }


def schedule_blocked_fallback(key, agent, event_prompt=""):
    if key in pending_blocked_tasks:
        return

    async def send_later():
        try:
            await asyncio.sleep(BLOCKED_FALLBACK_DELAY)
            current = agents_state.get(key, agent)
            if current.get("status") != "blocked":
                return
            content = ""
            options = None
            # Herdr's status event doesn't carry the approval reason. Read the
            # rendered approval card, retrying briefly while the TUI finishes
            # drawing it. event_prompt is only a last-resort fallback.
            can_read_directly = (
                normalize_host(current.get("host")) == LOCAL_HOST
                or current.get("remote") is not None
            )
            if can_read_directly:
                for attempt in range(4):
                    content, options = read_blocked_pane(
                        current["pane_id"], remote=current.get("remote")
                    )
                    if content and options:
                        break
                    if attempt < 3:
                        await asyncio.sleep(0.35)
            if not content and event_prompt:
                content = clean_blocked_content(event_prompt) or event_prompt
                options = options or detect_options(event_prompt)
            await broadcast(blocked_message(current, content, options))
        finally:
            pending_blocked_tasks.pop(key, None)

    pending_blocked_tasks[key] = asyncio.create_task(send_later())


async def notify_status_transition(key, agent, event_prompt=""):
    status = agent.get("status", "unknown")
    previous = last_statuses.get(key)
    if status == "blocked" and previous != "blocked":
        content = clean_blocked_content(event_prompt) if event_prompt else ""
        if not content and (
            normalize_host(agent.get("host")) == LOCAL_HOST
            or agent.get("remote") is not None
        ):
            content = read_pane(agent["pane_id"], remote=agent.get("remote"))
        await send_web_push(
            title=f"{agent.get('agent', 'Agent')} needs approval",
            body=(content or "Agent is blocked")[:120],
            url=f"/?pane={agent['pane_id']}",
        )
    elif status != "blocked" and previous == "blocked":
        await send_web_push("", "", clear=True)
    last_statuses[key] = status


async def apply_removal_event(event):
    """Remove a pane or every pane in a closed workspace on one host."""
    event_type = event.get("type")
    host = normalize_host(event.get("host"))
    pane_id = event.get("pane_id", "")
    workspace_id = event.get("workspace_id", "")
    seq = parse_event_seq(event)
    removed = []

    if seq is None and host in ordered_hosts:
        return removed
    if seq is not None:
        ordered_hosts.add(host)

    if event_type == "pane_removed" and pane_id:
        key = agent_key(host, pane_id)
        if seq is not None:
            if seq <= event_floor(host, pane_id, workspace_id):
                return removed
            pane_event_seq[key] = seq
        if remove_agent(key):
            removed.append(key)
    elif event_type == "workspace_removed" and workspace_id:
        if seq is not None:
            if seq <= max(
                workspace_event_seq.get((host, workspace_id), 0),
                host_snapshot_seq.get(host, 0),
            ):
                return removed
            workspace_event_seq[(host, workspace_id)] = seq
        for key, agent in list(agents_state.items()):
            if (
                normalize_host(agent.get("host")) == host
                and agent.get("workspace_id") == workspace_id
            ):
                if seq is not None and pane_event_seq.get(key, 0) > seq:
                    continue
                if seq is not None:
                    pane_event_seq[key] = seq
                if remove_agent(key):
                    removed.append(key)

    if removed:
        await broadcast({"type": "agents", "agents": list(agents_state.values())})
    return removed


async def apply_host_snapshot(event):
    """Restore missing agents and remove stale ones without changing live status."""
    host = normalize_host(event.get("host"))
    seq = parse_event_seq(event)
    snapshot_agents = event.get("agents")
    pane_ids = event.get("pane_ids")
    if not isinstance(snapshot_agents, list) and not isinstance(pane_ids, list):
        return []
    if seq is None and host in ordered_hosts:
        return []
    if seq is not None:
        if seq <= host_snapshot_seq.get(host, 0):
            return []
        ordered_hosts.add(host)

    snapshot_agents = list(snapshot_agents) if isinstance(snapshot_agents, list) else []
    pane_ids = list(pane_ids) if isinstance(pane_ids, list) else []
    restored = []
    changed = False
    for snapshot_agent in snapshot_agents:
        if not isinstance(snapshot_agent, dict):
            continue
        pane_id = snapshot_agent.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            continue
        pane_ids.append(pane_id)
        key = agent_key(host, pane_id)
        workspace_id = (
            snapshot_agent.get("workspace_id")
            or pane_id.split(":", 1)[0]
        )
        previous_seq = pane_event_seq.get(key, 0)
        if seq is not None and seq <= max(
            previous_seq,
            workspace_event_seq.get((host, workspace_id), 0),
        ):
            continue
        if key not in agents_state:
            merge_agent_event({**snapshot_agent, "pane_id": pane_id}, host)
            if seq is not None:
                pane_event_seq[key] = seq
            restored.append(key)
            continue

        # A recovery snapshot may refresh identity metadata, but it must never
        # manufacture a state transition for an existing agent.
        current = agents_state[key]
        if seq is not None and previous_seq == 0:
            snapshot_status = snapshot_agent.get("status")
            if snapshot_status and snapshot_status != current.get("status"):
                current["status"] = snapshot_status
                changed = True
        current["agent"] = snapshot_agent.get("agent") or current.get("agent", "")
        current["cwd"] = snapshot_agent.get("cwd") or current.get("cwd", "")
        current["project"] = (
            workspace_label(snapshot_agent)
            or snapshot_agent.get("project")
            or current.get("project", "")
        )
        current["workspace_id"] = (
            snapshot_agent.get("workspace_id")
            or current.get("workspace_id", "")
        )
        if "tab_id" in snapshot_agent:
            current["tab_id"] = snapshot_agent.get("tab_id", "")
        if "tab_label" in snapshot_agent:
            current["tab_label"] = snapshot_agent.get("tab_label", "")
        current["last_seen_at"] = time.time()
        if seq is not None:
            pane_event_seq[key] = seq

    active_keys = set()
    for pane_id in pane_ids:
        if not isinstance(pane_id, str) or not pane_id:
            continue
        workspace_id = pane_id.split(":", 1)[0]
        if seq is not None and seq <= workspace_event_seq.get((host, workspace_id), 0):
            continue
        active_keys.add(agent_key(host, pane_id))

    removed = []
    for key, agent in list(agents_state.items()):
        if normalize_host(agent.get("host")) != host or key in active_keys:
            continue
        if seq is not None and pane_event_seq.get(key, 0) > seq:
            continue
        if seq is not None:
            pane_event_seq[key] = seq
        if remove_agent(key):
            removed.append(key)

    if seq is not None:
        host_snapshot_seq[host] = seq
    if restored or removed or changed:
        await broadcast({"type": "agents", "agents": list(agents_state.values())})
    return removed


async def apply_agent_event(event):
    pane_id = event.get("pane_id", "")
    if not pane_id or event.get("type") != "agent_event":
        return False

    host = push_event_host(event)
    if not accept_agent_event(event, host):
        return False

    key, merged = merge_agent_event(event, host)
    if merged.get("status") == "blocked":
        schedule_blocked_fallback(key, merged, event.get("prompt", ""))
    else:
        cancel_blocked_fallback(key)
    await notify_status_transition(key, merged, event.get("prompt", ""))
    await broadcast({"type": "agents", "agents": list(agents_state.values())})
    return True


async def poll_loop():
    while True:
        try:
            for remote in [None, *REMOTES]:
                agents = get_agents_snapshot(remote=remote)
                if agents is None:
                    continue
                host = remote or LOCAL_HOST
                active_keys = set()
                for agent in agents:
                    key, merged = merge_polled_agent(agent)
                    pane_remote_map[key] = agent.get("remote")
                    active_keys.add(key)
                    await notify_status_transition(key, merged)
                reconcile_polled_host(host, active_keys)

            expire_inactive_hosts()
            await broadcast({"type": "agents", "agents": list(agents_state.values())})
        except Exception:
            log.exception("poll cycle failed; retrying")
        await asyncio.sleep(POLL_INTERVAL)


async def event_push():
    while True:
        event = await event_queue.get()
        note_host_activity(event.get("host"))

        if event.get("type") in ("pane_removed", "workspace_removed"):
            await apply_removal_event(event)
            continue

        if event.get("type") == "host_snapshot":
            await apply_host_snapshot(event)
            continue

        await apply_agent_event(event)


async def process_request(connection, request):
    """Handle HTTP GET/POST on the same port as WebSocket."""
    import urllib.parse

    from websockets.http11 import Response
    from websockets.datastructures import Headers

    path_only = (request.path or "/").split("?", 1)[0]

    upgrade = None
    for key, value in request.headers.raw_items():
        if key.lower() == "upgrade":
            upgrade = value.lower()

    # Serve the static web UI publicly (no token) — but never for a
    # WebSocket upgrade or the /push endpoint, which stay authenticated below.
    if upgrade != "websocket" and path_only != "/push":
        static_name = "index.html" if path_only == "/" else path_only.lstrip("/")
        static_file = (WEB_DIR / static_name).resolve()
        try:
            web_dir_resolved = WEB_DIR.resolve()
            in_web_dir = web_dir_resolved in static_file.parents or static_file == web_dir_resolved
        except Exception:
            in_web_dir = False
        if in_web_dir and static_file.is_file():
            content_type, _ = mimetypes.guess_type(str(static_file))
            headers = Headers([
                ("Content-Type", content_type or "application/octet-stream"),
                ("Access-Control-Allow-Origin", "*"),
            ])
            return Response(200, "OK", headers, static_file.read_bytes())

    # Token auth (if configured) — required for WebSocket upgrade and /push
    if AUTH_TOKEN:
        token = None
        for key, value in request.headers.raw_items():
            if key.lower() == "authorization":
                token = value.replace("Bearer ", "")
        # Also check query param ?token=
        if not token and "token=" in (request.path or ""):
            import urllib.parse
            _, qs = request.path.split("?", 1) if "?" in request.path else (request.path, "")
            params = urllib.parse.parse_qs(qs)
            token = params.get("token", [None])[0]
        if token != AUTH_TOKEN:
            headers = Headers([("Content-Type", "text/plain")])
            return Response(401, "Unauthorized", headers, b"Invalid token\n")

    # Check if this is a WebSocket upgrade
    if upgrade == "websocket":
        return None  # proceed with WebSocket handshake

    if path_only == "/command":
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.path or "").query)
        host = (query.get("host") or [""])[0]
        command = command_for_listener(host) if host else None
        if command is None and host:
            try:
                wait_seconds = min(max(float((query.get("wait") or ["0"])[0]), 0.0), 25.0)
            except ValueError:
                wait_seconds = 0.0
            if wait_seconds > 0:
                event = remote_command_events.setdefault(normalize_host(host), asyncio.Event())
                try:
                    await asyncio.wait_for(event.wait(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    pass
                finally:
                    event.clear()
                command = pop_remote_command(host)
        if command is None:
            headers = Headers([("Access-Control-Allow-Origin", "*")])
            return Response(204, "No Content", headers, b"")
        body = json.dumps(command).encode()
        headers = Headers([
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", "*"),
        ])
        return Response(200, "OK", headers, body)

    # For CORS preflight
    if request.path and "OPTIONS" in str(request.headers):
        headers = Headers([
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ])
        return Response(204, "No Content", headers, b"")

    # Serve web app for GET / or GET /index.html
    path = (request.path or "/").split("?")[0]
    if path in ("/", "/index.html"):
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        index_path = os.path.join(web_dir, "index.html")
        if os.path.isfile(index_path):
            with open(index_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-cache"),
            ])
            return Response(200, "OK", headers, body)

    # Serve service worker
    if path == "/sw.js":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        sw_path = os.path.join(web_dir, "sw.js")
        if os.path.isfile(sw_path):
            with open(sw_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "application/javascript"),
                ("Cache-Control", "no-cache"),
                ("Service-Worker-Allowed", "/"),
            ])
            return Response(200, "OK", headers, body)

    # Serve VAPID public key
    if path == "/api/vapid-public-key":
        body = json.dumps({"publicKey": VAPID_PUBLIC_KEY}).encode()
        headers = Headers([
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", "*"),
        ])
        return Response(200, "OK", headers, body)

    # Serve logo.svg
    if path == "/logo.svg":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        svg_path = os.path.join(web_dir, "logo.svg")
        if os.path.isfile(svg_path):
            with open(svg_path, "rb") as f:
                body = f.read()
            headers = Headers([("Content-Type", "image/svg+xml")])
            return Response(200, "OK", headers, body)

    # HTTP POST — parse event from URL query params as fallback
    # (since we can't read request body in websockets 16)
    # Plugins encode payload in the URL path: POST /push?d=...
    if "?" in (request.path or ""):
        _, qs = request.path.split("?", 1)
        params = urllib.parse.parse_qs(qs)
        if "d" in params:
            try:
                event = json.loads(urllib.parse.unquote(params["d"][0]))
                event_queue.put_nowait(event)
            except Exception:
                pass

    headers = Headers([("Access-Control-Allow-Origin", "*")])
    return Response(200, "OK", headers, b"ok\n")


async def handle_client(ws):
    remote_addr = ws.remote_address
    ip = remote_addr[0] if remote_addr else "unknown"
    ua = ws.request.headers.get("User-Agent", "unknown") if ws.request else "unknown"
    origin = ws.request.headers.get("Origin", "") if ws.request else ""

    device = "unknown"
    ua_lower = ua.lower()
    if "iphone" in ua_lower or "ipad" in ua_lower:
        device = "iOS"
    elif "android" in ua_lower:
        device = "Android"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        device = "macOS"
    elif "windows" in ua_lower:
        device = "Windows"
    elif "linux" in ua_lower:
        device = "Linux"
    elif "telegram" in ua_lower or "bot" in ua_lower:
        device = "bot"
    elif "python" in ua_lower:
        device = "script"

    log.info("Client connected: ip=%s device=%s origin=%s", ip, device, origin or "-")
    clients.add(ws)
    connected_at = time.monotonic()
    if agents_state:
        try:
            await ws.send(json.dumps({"type": "agents", "agents": list(agents_state.values())}))
        except Exception:
            pass
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type")
            if msg_type == "respond":
                pane_id = msg["pane_id"]
                host = msg.get("host")
                if not pane_is_known(host, pane_id):
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                if not response_is_safe(text):
                    await ws.send(json.dumps({"type": "error", "message": "response not in allowlist"}))
                    continue
                log.info("Response from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("respond", ip, device, pane_id, f"text={text!r}")
                dispatch_response(host, pane_id, text)
            elif msg_type == "agent_event":
                event_queue.put_nowait(msg)
            elif msg_type == "read_pane":
                pane_id = msg["pane_id"]
                if not pane_is_known(msg.get("host"), pane_id):
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                lines = msg.get("lines", "30")
                direct, remote = direct_pane_target(msg.get("host"), pane_id)
                if direct:
                    content = run_herdr(
                        "pane", "read", pane_id, "--lines", str(lines),
                        "--source", "recent", remote=remote,
                    )
                else:
                    content = "Remote pane reading is unavailable for this push-only host."
                await ws.send(json.dumps({
                    "type": "pane_content",
                    "pane_id": pane_id,
                    "host": normalize_host(msg.get("host")),
                    "content": content,
                }))
            elif msg_type == "send_keys":
                pane_id = msg["pane_id"]
                host = msg.get("host")
                if not pane_is_known(host, pane_id):
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                keys = msg.get("keys", [])
                if not all(k in SAFE_KEYS for k in keys):
                    await ws.send(json.dumps({"type": "error", "message": "keys contain disallowed values"}))
                    continue
                log.info("Keys from %s (%s): pane=%s keys=%s", ip, device, pane_id, keys)
                audit("send_keys", ip, device, pane_id, f"keys={keys}")
                try:
                    result = dispatch_keys(host, pane_id, keys)
                except Exception as exc:
                    log.warning("send_keys command failed for pane %s: %s", pane_id, exc)
                    await ws.send(json.dumps({"type": "error", "message": "send_keys command failed"}))
                    continue
                if result is not None:
                    if result.returncode != 0:
                        log.warning(
                            "send_keys command failed for pane %s with exit %s",
                            pane_id,
                            result.returncode,
                        )
                        await ws.send(json.dumps({"type": "error", "message": "send_keys command failed"}))
                        continue
                await ws.send(json.dumps({"type": "command_result", "command": "send_keys", "ok": True}))
            elif msg_type == "send_text":
                pane_id = msg["pane_id"]
                host = msg.get("host")
                if not pane_is_known(host, pane_id):
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                if not text or len(text) > 1000:
                    await ws.send(json.dumps({"type": "error", "message": "text empty or too long"}))
                    continue
                log.info("Text from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("send_text", ip, device, pane_id, f"text={text!r}")
                dispatch_text(host, pane_id, text)
            elif msg_type == "create_tab":
                workspace_id = msg.get("workspace_id", "")
                if workspace_id:
                    log.info("Create tab from %s (%s): workspace=%s", ip, device, workspace_id)
                    audit("create_tab", ip, device, "", f"workspace={workspace_id}")
                    run_herdr("tab", "create", "--workspace", workspace_id, "--focus")
                    await ws.send(json.dumps({"type": "tab_created", "ok": True}))
                else:
                    await ws.send(json.dumps({"type": "error", "message": "workspace_id required"}))
            elif msg_type == "push_subscribe":
                sub = msg.get("subscription")
                if sub and sub not in push_subscriptions:
                    push_subscriptions.append(sub)
                    _save_push_subs()
                    log.info("Push subscription added from %s (%s)", ip, device)
                await ws.send(json.dumps({"type": "push_subscribed", "ok": True}))
            elif msg_type == "push_unsubscribe":
                sub = msg.get("subscription")
                if sub and sub in push_subscriptions:
                    push_subscriptions.remove(sub)
                    _save_push_subs()
                await ws.send(json.dumps({"type": "push_unsubscribed", "ok": True}))
    except ConnectionClosed:
        pass
    finally:
        duration = int(time.monotonic() - connected_at)
        log.info("Client disconnected: ip=%s device=%s duration=%ds", ip, device, duration)
        clients.discard(ws)


class UDPPlugin(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        try:
            event_queue.put_nowait(json.loads(data.decode()))
        except Exception:
            pass


def start_mdns():
    try:
        from zeroconf import Zeroconf, ServiceInfo
        import socket as sock_mod
        import threading
        ip = sock_mod.gethostbyname(sock_mod.gethostname())
        info = ServiceInfo(
            "_herdr-remote._tcp.local.", "herdr-remote._herdr-remote._tcp.local.",
            addresses=[sock_mod.inet_aton(ip)], port=WS_PORT,
        )
        zc = Zeroconf()
        threading.Thread(target=zc.register_service, args=(info,), daemon=True).start()
        log.info("mDNS registering at %s", ip)
        return zc, info
    except Exception as e:
        log.warning("mDNS skipped: %s", e)
        return None, None


async def main():
    zc, info = start_mdns()
    loop = asyncio.get_running_loop()
    try:
        await loop.create_datagram_endpoint(UDPPlugin, local_addr=("127.0.0.1", 8376))
    except OSError:
        log.warning("UDP 8376 in use, plugin push disabled")
    asyncio.create_task(poll_loop())
    asyncio.create_task(event_push())
    server = await serve(handle_client, "0.0.0.0", WS_PORT, process_request=process_request)
    hosts = [LOCAL_HOST] + REMOTES
    log.info("herdr-remote relay on :%d (WebSocket + HTTP POST)", WS_PORT)
    log.info("Polling: %s", ", ".join(hosts))
    log.info("Push host timeout: %gs", REMOTE_HOST_TIMEOUT)
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)
    await stop
    server.close()
    if zc and info:
        try:
            zc.unregister_service(info)
        except Exception as exc:
            print(f"mDNS unregister skipped: {exc}")
        finally:
            try:
                zc.close()
            except Exception as exc:
                print(f"mDNS close skipped: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
