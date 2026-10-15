#!/usr/bin/env python3
"""Poll relay commands and execute them against the local Herdr pane."""

import argparse
import fcntl
import json
import os
import pathlib
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = "herdr-push/0.5.2"


def read_env(path):
    values = {}
    for raw_line in pathlib.Path(path).read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def next_event_seq(config_path):
    """Allocate one host-local order value shared with the event hook."""
    path = pathlib.Path(config_path).parent / "event-seq"
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


def command_url(relay, host):
    base = relay.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    # Keep the long poll below common reverse-proxy request timeouts.
    return f"{base.rstrip('/')}/command?host={urllib.parse.quote(host)}&wait=5"


def edge_ip_for_relay(relay, edge_ip):
    """Use the legacy DNS bypass only with the ngrok hostname it belongs to."""
    hostname = urllib.parse.urlsplit(relay_http_url(relay)).hostname
    return edge_ip if hostname == "latent.ngrok.pro" else ""


def relay_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }


def curl_request(url, token, edge_ip, timeout):
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    command = [
        "curl", "-sS", "--max-time", str(timeout),
        "--resolve", f"{parsed.hostname}:{port}:{edge_ip}",
        "-A", USER_AGENT,
        "-H", f"Authorization: Bearer {token}",
        "-w", "\n__HERDR_STATUS__:%{http_code}",
        url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 2)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"curl failed: {result.returncode}")
    body, marker, raw_status = result.stdout.rpartition("\n__HERDR_STATUS__:")
    if not marker:
        raise RuntimeError("curl response status is missing")
    return int(raw_status), body


def poll_command(relay, token, host, edge_ip=""):
    url = command_url(relay, host)
    if edge_ip:
        status, body = curl_request(url, token, edge_ip, 8)
        if status == 204:
            return None
        if status != 200:
            raise RuntimeError(f"command poll failed: HTTP {status}")
        return json.loads(body)
    request = urllib.request.Request(
        url,
        headers=relay_headers(token),
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status == 204:
                return None
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 204:
            return None
        raise


def relay_http_url(relay):
    return relay.replace("wss://", "https://", 1).replace("ws://", "http://", 1).rstrip("/")


def push_event(relay, token, event, edge_ip=""):
    encoded = urllib.parse.quote(json.dumps(event, separators=(",", ":")))
    url = f"{relay_http_url(relay)}/push?d={encoded}"
    if edge_ip:
        status, _ = curl_request(url, token, edge_ip, 5)
        if status != 200:
            raise RuntimeError(f"push failed: HTTP {status}")
        return
    request = urllib.request.Request(
        url,
        headers=relay_headers(token),
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"push failed: HTTP {response.status}")


def herdr_json(herdr, *args):
    raw = subprocess.check_output([herdr, *args], text=True, timeout=10)
    return json.loads(raw).get("result", {})


def get_tab_labels(herdr):
    try:
        tabs = herdr_json(herdr, "tab", "list").get("tabs", [])
    except Exception:
        return {}
    return {
        item.get("tab_id"): item.get("label", "")
        for item in tabs
        if item.get("tab_id")
    }


def herdr_socket_path():
    return os.environ.get("HERDR_SOCKET_PATH") or str(
        pathlib.Path.home() / ".config" / "herdr" / "herdr.sock"
    )


def event_messages(sock):
    buffer = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            yield {}
            continue
        if not chunk:
            raise ConnectionError("Herdr event socket closed")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line:
                yield json.loads(line)


def subscribe_socket(subscriptions):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(herdr_socket_path())
    request = {
        "id": f"herdr-push-{time.time_ns()}",
        "method": "events.subscribe",
        "params": {"subscriptions": subscriptions},
    }
    sock.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode())
    return sock


def current_agent_payload(herdr, pane_id, host, config_path):
    event_seq = next_event_seq(config_path)
    try:
        pane = herdr_json(herdr, "pane", "get", pane_id).get("pane", {})
    except Exception:
        return None
    if not pane:
        return None

    workspace_id = pane.get("workspace_id") or pane_id.split(":", 1)[0]
    if not pane.get("agent"):
        return {
            "type": "pane_removed",
            "pane_id": pane_id,
            "workspace_id": workspace_id,
            "host": host,
            "event_seq": event_seq,
        }

    try:
        workspaces = herdr_json(herdr, "workspace", "list").get("workspaces", [])
    except Exception:
        workspaces = []
    labels = {item.get("workspace_id"): item.get("label", "") for item in workspaces}
    cwd = pane.get("cwd", "")
    label = labels.get(workspace_id) or os.path.basename(cwd)
    tab_id = pane.get("tab_id", "")
    tab_label = get_tab_labels(herdr).get(tab_id, "")
    payload = {
        "type": "agent_event",
        "pane_id": pane_id,
        "status": str(pane.get("agent_status") or "unknown").lower(),
        "agent": str(pane.get("agent") or "").lower(),
        "project": label,
        "workspace_label": label,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "tab_label": tab_label,
        "cwd": cwd,
        "host": host,
        "custom_status": pane.get("custom_status") or "",
        "event_seq": event_seq,
    }
    if payload["status"] == "blocked":
        try:
            raw = subprocess.check_output(
                [herdr, "pane", "read", pane_id, "--lines", "60",
                 "--source", "recent-unwrapped"],
                text=True,
                timeout=4,
            )
            payload["prompt"] = raw[-12000:]
        except Exception:
            pass
    return payload


def lifecycle_event_loop(refresh_subscriptions):
    subscriptions = [
        {"type": "pane.created"},
        {"type": "pane.closed"},
        {"type": "tab.renamed"},
        {"type": "workspace.renamed"},
        {"type": "workspace.closed"},
    ]
    while True:
        sock = None
        try:
            sock = subscribe_socket(subscriptions)
            for message in event_messages(sock):
                if message.get("event") in (
                    "pane_created", "pane_closed", "tab_renamed",
                    "workspace_renamed", "workspace_closed",
                ):
                    refresh_subscriptions.set()
        except Exception as exc:
            print(f"herdr push lifecycle subscription: {exc}", flush=True)
            refresh_subscriptions.set()
            time.sleep(1)
        finally:
            if sock is not None:
                sock.close()


def panes_to_refresh(known_panes, current_panes):
    """Replay current status after listener start, then only newly added panes."""
    return current_panes if known_panes is None else current_panes - known_panes


def status_event_loop(relay, token, host, herdr, config_path, edge_ip,
                      refresh_subscriptions):
    known_panes = None
    while True:
        sock = None
        try:
            refresh_subscriptions.clear()
            panes = herdr_json(herdr, "pane", "list").get("panes", [])
            subscriptions = [
                {"type": "pane.agent_status_changed", "pane_id": pane["pane_id"]}
                for pane in panes
                if pane.get("pane_id")
            ]
            if not subscriptions:
                refresh_subscriptions.wait(1)
                continue
            sock = subscribe_socket(subscriptions)
            sock.settimeout(0.5)
            current_panes = {item["pane_id"] for item in subscriptions}
            for pane_id in panes_to_refresh(known_panes, current_panes):
                payload = current_agent_payload(
                    herdr, pane_id, host, config_path
                )
                if payload:
                    push_event(relay, token, payload, edge_ip)
            known_panes = current_panes
            for message in event_messages(sock):
                if refresh_subscriptions.is_set():
                    sync_agents(
                        relay, token, host, herdr, config_path, edge_ip
                    )
                    break
                if message.get("event") != "pane.agent_status_changed":
                    continue
                pane_id = message.get("data", {}).get("pane_id", "")
                if not pane_id:
                    continue
                payload = current_agent_payload(herdr, pane_id, host, config_path)
                if payload:
                    push_event(relay, token, payload, edge_ip)
        except Exception as exc:
            print(f"herdr push status subscription: {exc}", flush=True)
            time.sleep(1)
        finally:
            if sock is not None:
                sock.close()


def sync_agents(relay, token, host, herdr, config_path, edge_ip=""):
    panes = herdr_json(herdr, "pane", "list").get("panes", [])
    workspaces = herdr_json(herdr, "workspace", "list").get("workspaces", [])
    labels = {item.get("workspace_id"): item.get("label", "") for item in workspaces}
    tab_labels = get_tab_labels(herdr)
    active_panes = [pane for pane in panes if pane.get("agent")]
    agents = []
    for pane in active_panes:
        workspace_id = pane.get("workspace_id", "")
        cwd = pane.get("cwd", "")
        label = labels.get(workspace_id) or os.path.basename(cwd)
        agents.append({
            "type": "agent_event",
            "pane_id": pane.get("pane_id", ""),
            "status": str(pane.get("agent_status") or "unknown").lower(),
            "agent": str(pane.get("agent") or "").lower(),
            "project": label,
            "workspace_label": label,
            "workspace_id": workspace_id,
            "tab_id": pane.get("tab_id", ""),
            "tab_label": tab_labels.get(pane.get("tab_id"), ""),
            "cwd": cwd,
            "host": host,
        })
    push_event(relay, token, {
        "type": "host_snapshot",
        "host": host,
        "pane_ids": [pane.get("pane_id", "") for pane in active_panes],
        "agents": agents,
        "event_seq": next_event_seq(config_path),
    }, edge_ip)


def execute_command(command, herdr):
    pane_id = command.get("pane_id", "")
    if not pane_id:
        return
    if command.get("type") == "send_keys":
        args = [herdr, "pane", "send-keys", pane_id, *command.get("keys", [])]
    elif command.get("type") == "send_text":
        args = [herdr, "pane", "send-text", pane_id, command.get("text", "")]
    else:
        return
    subprocess.run(args, check=False, timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--herdr", default="herdr")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    config = read_env(args.config)
    relay = config.get("HERDR_RELAY", "")
    token = config.get("HERDR_RELAY_TOKEN", "")
    host = config.get("HERDR_PUSH_HOST") or socket.gethostname().split(".")[0]
    edge_ip = edge_ip_for_relay(
        relay, config.get("HERDR_PUSH_EDGE_IP", "")
    )
    if not relay:
        raise SystemExit("HERDR_RELAY is not configured")

    try:
        sync_agents(relay, token, host, args.herdr, args.config, edge_ip)
    except Exception as exc:
        print(f"herdr push listener initial sync: {exc}", flush=True)

    refresh_subscriptions = threading.Event()
    threading.Thread(
        target=lifecycle_event_loop,
        args=(refresh_subscriptions,),
        daemon=True,
    ).start()
    threading.Thread(
        target=status_event_loop,
        args=(relay, token, host, args.herdr, args.config, edge_ip,
              refresh_subscriptions),
        daemon=True,
    ).start()

    while True:
        try:
            command = poll_command(relay, token, host, edge_ip)
            if command and command.get("type") == "sync_snapshot":
                sync_agents(relay, token, host, args.herdr, args.config, edge_ip)
            elif command:
                execute_command(command, args.herdr)
        except Exception as exc:
            print(f"herdr push listener: {exc}", flush=True)
            time.sleep(2)
        if args.once:
            return
        time.sleep(0.1)


if __name__ == "__main__":
    main()
