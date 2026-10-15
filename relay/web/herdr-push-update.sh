#!/bin/sh
set -eu

BASE_URL="${HERDR_PUSH_UPDATE_URL:-https://yn-herdr.vault.haus}"
EDGE_IP="${HERDR_PUSH_EDGE_IP:-}"
UPDATER_VERSION="2026-09-02.2"
CUSTOM_ID="herdr.push.remote"
CUSTOM_ROOT="$HOME/.config/herdr/plugins/local/$CUSTOM_ID"
STAMP=$(date +%Y%m%d%H%M%S)
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM

echo "Herdr push updater $UPDATER_VERSION"

HERDR_BIN=${HERDR_BIN:-$(command -v herdr || true)}
if [ -z "$HERDR_BIN" ]; then
    for candidate in "$HOME/.local/bin/herdr" /opt/homebrew/bin/herdr /usr/local/bin/herdr; do
        if [ -x "$candidate" ]; then
            HERDR_BIN=$candidate
            break
        fi
    done
fi
if [ -z "$HERDR_BIN" ] || [ ! -x "$HERDR_BIN" ]; then
    echo "herdr 실행 파일을 찾지 못했습니다" >&2
    exit 1
fi

ensure_herdr_server() {
    server_status=$("$HERDR_BIN" status server --json 2>/dev/null || true)
    if printf '%s' "$server_status" | grep -Eq '"running"[[:space:]]*:[[:space:]]*true'; then
        return
    fi

    HERDR_LOG_DIR="$HOME/.config/herdr"
    HERDR_START_LOG="$HERDR_LOG_DIR/herdr-push-update-server.log"
    mkdir -p "$HERDR_LOG_DIR"
    echo "Herdr server가 꺼져 있어 시작합니다..."
    nohup "$HERDR_BIN" server </dev/null >>"$HERDR_START_LOG" 2>&1 &

    attempt=0
    while [ "$attempt" -lt 10 ]; do
        server_status=$("$HERDR_BIN" status server --json 2>/dev/null || true)
        if printf '%s' "$server_status" | grep -Eq '"running"[[:space:]]*:[[:space:]]*true'; then
            return
        fi
        attempt=$((attempt + 1))
        sleep 1
    done

    echo "Herdr server 시작 실패: $HERDR_START_LOG" >&2
    tail -20 "$HERDR_START_LOG" >&2 || true
    exit 1
}

ensure_herdr_server
ORIGINAL_CONFIG=$("$HERDR_BIN" plugin config-dir herdr.push 2>/dev/null || true)

fetch_file() {
    source_url="$1"
    destination="$2"
    if [ -n "$EDGE_IP" ] && [ "$BASE_URL" = "https://latent.ngrok.pro" ]; then
        curl -fsSL --resolve "latent.ngrok.pro:443:$EDGE_IP" "$source_url" -o "$destination"
    else
        curl -fsSL "$source_url" -o "$destination"
    fi
}

fetch_file "$BASE_URL/herdr-push-on-event.sh" "$TMP_DIR/on_event.sh"
fetch_file "$BASE_URL/herdr-push-plugin.toml" "$TMP_DIR/herdr-plugin.toml"
fetch_file "$BASE_URL/herdr-push-listener.py" "$TMP_DIR/herdr-push-listener.py"
sh -n "$TMP_DIR/on_event.sh"
python3 -m py_compile "$TMP_DIR/herdr-push-listener.py"

mkdir -p "$CUSTOM_ROOT"
for name in on_event.sh herdr-plugin.toml herdr-push-listener.py; do
    [ ! -f "$CUSTOM_ROOT/$name" ] || cp "$CUSTOM_ROOT/$name" "$CUSTOM_ROOT/$name.bak.$STAMP"
    mv "$TMP_DIR/$name" "$CUSTOM_ROOT/$name"
done
chmod +x "$CUSTOM_ROOT/on_event.sh" "$CUSTOM_ROOT/herdr-push-listener.py"

# Register through Herdr's plugin API. Editing manifest/plugins.json in place
# doesn't refresh the runtime event registry.
"$HERDR_BIN" plugin unlink "$CUSTOM_ID" >/dev/null 2>&1 || true
"$HERDR_BIN" plugin link "$CUSTOM_ROOT" >/dev/null
"$HERDR_BIN" plugin disable herdr.push >/dev/null 2>&1 || true

CONFIG_DIR=$("$HERDR_BIN" plugin config-dir "$CUSTOM_ID")
ENV_FILE="$CONFIG_DIR/.env"
LISTENER_PID_FILE="$CONFIG_DIR/listener.pid"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$ENV_FILE" ] && [ -n "$ORIGINAL_CONFIG" ] && [ -f "$ORIGINAL_CONFIG/.env" ]; then
    cp "$ORIGINAL_CONFIG/.env" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
fi
RELAY="${HERDR_RELAY:-}"
TOKEN="${HERDR_RELAY_TOKEN:-}"
HOST_OVERRIDE="${HERDR_PUSH_HOST:-}"
if [ -f "$ENV_FILE" ]; then
    [ -z "$RELAY" ] && RELAY=$(grep '^HERDR_RELAY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
    [ -z "$TOKEN" ] && TOKEN=$(grep '^HERDR_RELAY_TOKEN=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
    [ -z "$HOST_OVERRIDE" ] && HOST_OVERRIDE=$(grep '^HERDR_PUSH_HOST=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
    [ -z "$EDGE_IP" ] && EDGE_IP=$(grep '^HERDR_PUSH_EDGE_IP=' "$ENV_FILE" | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
fi
if [ -z "$RELAY" ]; then
    echo "HERDR_RELAY가 없어 현재 workspace 동기화를 할 수 없습니다" >&2
    exit 1
fi

# Persist explicit first-install values as well as copied legacy values so the
# background listener and future plugin events use the same configuration.
{
    grep -Ev '^(HERDR_RELAY|HERDR_RELAY_TOKEN|HERDR_PUSH_HOST|HERDR_PUSH_EDGE_IP)=' \
        "$ENV_FILE" 2>/dev/null || true
    printf 'HERDR_RELAY=%s\n' "$RELAY"
    printf 'HERDR_RELAY_TOKEN=%s\n' "$TOKEN"
    [ -z "$HOST_OVERRIDE" ] || printf 'HERDR_PUSH_HOST=%s\n' "$HOST_OVERRIDE"
    [ -z "$EDGE_IP" ] || printf 'HERDR_PUSH_EDGE_IP=%s\n' "$EDGE_IP"
} > "$ENV_FILE.new"
mv "$ENV_FILE.new" "$ENV_FILE"
chmod 600 "$ENV_FILE"

ensure_herdr_server
"$HERDR_BIN" server reload-config >/dev/null
PYTHON_BIN=$(command -v python3)
OS_NAME=$(uname -s)

stop_macos_listener_processes() {
    listener_pids=""
    if [ -f "$LISTENER_PID_FILE" ]; then
        listener_pids=$(sed -n '1p' "$LISTENER_PID_FILE" 2>/dev/null || true)
    fi
    if command -v pgrep >/dev/null 2>&1; then
        discovered=$(pgrep -f "$CUSTOM_ROOT/herdr-push-listener.py" 2>/dev/null || true)
        listener_pids="$listener_pids $discovered"
    fi
    for listener_pid in $listener_pids; do
        case "$listener_pid" in
            ''|*[!0-9]*) continue ;;
        esac
        [ "$listener_pid" = "$$" ] && continue
        listener_command=$(ps -p "$listener_pid" -o command= 2>/dev/null || true)
        case "$listener_command" in
            *"$CUSTOM_ROOT/herdr-push-listener.py"*)
                kill "$listener_pid" >/dev/null 2>&1 || true
                ;;
        esac
    done
    rm -f "$LISTENER_PID_FILE"
}

if [ "$OS_NAME" = "Linux" ]; then
    SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
    LISTENER_UNIT="$SYSTEMD_USER_DIR/herdr-push-listener.service"
    mkdir -p "$SYSTEMD_USER_DIR"
    cat > "$LISTENER_UNIT" <<EOF
[Unit]
Description=Herdr push reverse command listener
After=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON_BIN $CUSTOM_ROOT/herdr-push-listener.py --config $ENV_FILE --herdr $HERDR_BIN
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now herdr-push-listener.service >/dev/null
    systemctl --user restart herdr-push-listener.service
    LISTENER_STATUS="user systemd: herdr-push-listener.service"
elif [ "$OS_NAME" = "Darwin" ]; then
    rm -f "$HOME/.config/systemd/user/herdr-push-listener.service"
    LISTENER_LABEL="pro.herdr.push-listener"
    LAUNCH_DIR="$HOME/Library/LaunchAgents"
    LOG_DIR="$HOME/Library/Logs"
    LISTENER_PLIST="$LAUNCH_DIR/$LISTENER_LABEL.plist"
    mkdir -p "$LAUNCH_DIR" "$LOG_DIR"
    cat > "$LISTENER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LISTENER_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$CUSTOM_ROOT/herdr-push-listener.py</string>
    <string>--config</string>
    <string>$ENV_FILE</string>
    <string>--herdr</string>
    <string>$HERDR_BIN</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/herdr-push-listener.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/herdr-push-listener.log</string>
</dict>
</plist>
EOF
    # launchd rejects user LaunchAgents that are group/world writable.  A
    # redirection keeps the mode of an existing plist, so normalize it.
    chmod 600 "$LISTENER_PLIST"
    plutil -lint "$LISTENER_PLIST" >/dev/null
    UID_NUMBER=$(id -u)
    LAUNCH_DOMAINS=""
    for candidate in "gui/$UID_NUMBER" "user/$UID_NUMBER"; do
        if launchctl print "$candidate" >/dev/null 2>&1; then
            LAUNCH_DOMAINS="$LAUNCH_DOMAINS $candidate"
        fi
    done
    if [ -z "$LAUNCH_DOMAINS" ]; then
        echo "사용 가능한 macOS launchd user domain을 찾지 못했습니다" >&2
        exit 1
    fi

    # Clear a stale job from every available domain first.  A job left loaded
    # from an older plist commonly makes bootstrap fail with macOS error 5.
    for candidate in $LAUNCH_DOMAINS; do
        launchctl bootout "$candidate/$LISTENER_LABEL" >/dev/null 2>&1 || true
        launchctl bootout "$candidate" "$LISTENER_PLIST" >/dev/null 2>&1 || true
    done
    stop_macos_listener_processes

    LAUNCH_DOMAIN=""
    : > "$TMP_DIR/launchctl-bootstrap.log"
    for candidate in $LAUNCH_DOMAINS; do
        if launchctl bootstrap "$candidate" "$LISTENER_PLIST" 2>>"$TMP_DIR/launchctl-bootstrap.log"; then
            LAUNCH_DOMAIN="$candidate"
            break
        fi
    done
    if [ -z "$LAUNCH_DOMAIN" ]; then
        # Some macOS sessions reject bootstrap even though a user agent may
        # be loaded through the legacy command in the caller's own domain.
        launchctl unload "$LISTENER_PLIST" >/dev/null 2>&1 || true
        if launchctl load -w "$LISTENER_PLIST" >/dev/null 2>&1; then
            for candidate in $LAUNCH_DOMAINS; do
                if launchctl print "$candidate/$LISTENER_LABEL" >/dev/null 2>&1; then
                    LAUNCH_DOMAIN="$candidate"
                    break
                fi
            done
        fi
    fi
    if [ -n "$LAUNCH_DOMAIN" ]; then
        launchctl enable "$LAUNCH_DOMAIN/$LISTENER_LABEL"
        launchctl kickstart -k "$LAUNCH_DOMAIN/$LISTENER_LABEL"
        launchctl print "$LAUNCH_DOMAIN/$LISTENER_LABEL" >/dev/null
        rm -f "$LISTENER_PID_FILE"
        LISTENER_STATUS="LaunchAgent: $LAUNCH_DOMAIN/$LISTENER_LABEL"
        LISTENER_CHECK="launchctl print '$LAUNCH_DOMAIN/$LISTENER_LABEL' | grep state"
    else
        # The close hook is already active and must not be rolled back merely
        # because launchd is unhealthy. Keep remote choices working now; the
        # valid plist will be picked up automatically at the next login.
        launchctl unload "$LISTENER_PLIST" >/dev/null 2>&1 || true
        for candidate in $LAUNCH_DOMAINS; do
            launchctl bootout "$candidate/$LISTENER_LABEL" >/dev/null 2>&1 || true
            launchctl bootout "$candidate" "$LISTENER_PLIST" >/dev/null 2>&1 || true
        done
        stop_macos_listener_processes
        nohup "$PYTHON_BIN" "$CUSTOM_ROOT/herdr-push-listener.py" \
            --config "$ENV_FILE" --herdr "$HERDR_BIN" </dev/null \
            >>"$LOG_DIR/herdr-push-listener.log" 2>&1 &
        LISTENER_PID=$!
        printf '%s\n' "$LISTENER_PID" > "$LISTENER_PID_FILE"
        LISTENER_STATUS="background fallback PID $LISTENER_PID (LaunchAgent plist installed)"
        LISTENER_CHECK="kill -0 $LISTENER_PID"
        echo "주의: launchd bootstrap 실패로 현재 session은 background fallback을 사용합니다" >&2
    fi
else
    echo "지원하지 않는 OS: $OS_NAME" >&2
    exit 1
fi

echo "업데이트 완료: $CUSTOM_ROOT"
echo "활성 plugin: $CUSTOM_ID"
echo "상태 이벤트: listener가 Herdr socket을 직접 구독"
echo "close hook: pane.closed, workspace.closed"
echo "초기 snapshot: listener 시작 시 1회"
echo "역방향 명령 수신기: $LISTENER_STATUS"
[ -z "${LISTENER_CHECK:-}" ] || echo "확인: $LISTENER_CHECK"
