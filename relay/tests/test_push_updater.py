import http.server
import json
import os
import pathlib
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse


RELAY_DIR = pathlib.Path(__file__).resolve().parents[1]
UPDATER = RELAY_DIR / "web" / "herdr-push-update.sh"
WEB_INDEX = RELAY_DIR / "web" / "index.html"
# Support both the runtime checkout and dotfiles/herdr/source/relay mirror.
HERDR_HELPERS = next(
    candidate
    for parent in RELAY_DIR.parents
    for candidate in (parent, parent / "dotfiles" / "herdr")
    if (candidate / "update-push-hosts.sh").is_file()
)
HOST_UPDATER = HERDR_HELPERS / "update-push-hosts.sh"
TELEGRAM_DECRYPT_HELPER = HERDR_HELPERS / "decrypt-dotfiles-env.sh"


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, format, *args):
        return


class PushUpdaterTests(unittest.TestCase):
    def test_public_updater_defaults_to_cloudflare_hostname(self):
        updater = UPDATER.read_text()

        self.assertIn(
            'BASE_URL="${HERDR_PUSH_UPDATE_URL:-https://yn-herdr.vault.haus}"',
            updater,
        )
        self.assertIn('UPDATER_VERSION="2026-09-02.2"', updater)

    def test_host_updater_defaults_to_new_public_hostname(self):
        updater = HOST_UPDATER.read_text()

        self.assertIn(
            "https://yn-herdr.vault.haus/herdr-push-update.sh", updater
        )
        self.assertIn("https://yn-herdr.vault.haus", updater)
        self.assertIn('DEFAULT_HOSTS=${HERDR_PUSH_HOSTS:-"dev"}', updater)
        self.assertIn("HERDR_PUSH_HOST='$host'", updater)

    def test_decrypt_helper_requires_the_ward_bot_identity(self):
        helper = TELEGRAM_DECRYPT_HELPER.read_text()

        self.assertIn("HERDR_EXPECTED_TG_USERNAME:-cw_ward_bot", helper)
        self.assertIn('expected_username = sys.argv[2]', helper)
        self.assertIn("if not expected_found:", helper)
        self.assertIn("sys.exit(1)", helper)

    def test_web_normalizes_https_relay_and_token_assignment(self):
        web = WEB_INDEX.read_text()

        self.assertIn("function normalizeRelayUrl(value)", web)
        self.assertIn(".replace(/^https:\\/\\//i, 'wss://')", web)
        self.assertIn(".replace(/^HERDR_RELAY_TOKEN=/, '')", web)

    def test_close_event_forwards_ordered_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            fake_bin = root / "bin"
            curl_log = root / "curl.log"
            fake_bin.mkdir()
            self.write_executable(
                fake_bin / "curl",
                f'#!/bin/sh\nprintf \'%s\\n\' "$*" > "{curl_log}"\n',
            )
            env = os.environ.copy()
            env.update({
                "PATH": f"{fake_bin}:{env['PATH']}",
                "HERDR_RELAY": "https://relay.example",
                "HERDR_RELAY_TOKEN": "test-token",
                "HERDR_PUSH_HOST": "mac-test",
                "HERDR_PLUGIN_EVENT_JSON": json.dumps({
                    "event": "workspace.closed",
                    "data": {
                        "type": "workspace_closed",
                        "workspace_id": "w9",
                    },
                }),
                "HERDR_PLUGIN_CONTEXT_JSON": "{}",
                "HERDR_PLUGIN_CONFIG_DIR": str(root / "missing-config"),
            })

            result = subprocess.run(
                ["sh", str(RELAY_DIR / "web" / "herdr-push-on-event.sh")],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            url = next(
                part for part in curl_log.read_text().split()
                if part.startswith("https://relay.example/push?")
            )
            encoded = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["d"][0]
            payload = json.loads(encoded)
            self.assertEqual("workspace_removed", payload["type"])
            self.assertEqual("w9", payload["workspace_id"])
            self.assertEqual("mac-test", payload["host"])
            self.assertIsInstance(payload["event_seq"], int)

    def test_macos_finds_user_local_herdr_starts_server_and_uses_user_domain(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            fake_bin = root / "bin"
            user_bin = home / ".local" / "bin"
            state = root / "herdr-running"
            launch_log = root / "launchctl.log"
            fake_bin.mkdir()
            home.mkdir()
            user_bin.mkdir(parents=True)

            config = home / ".config" / "herdr" / "plugins" / "config" / "herdr.push"
            config.mkdir(parents=True)

            with socketserver.TCPServer(("127.0.0.1", 0), QuietHandler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                (config / ".env").write_text(
                    f"HERDR_RELAY=http://127.0.0.1:{server.server_address[1]}\n"
                    "HERDR_RELAY_TOKEN=test-token\n"
                    "HERDR_PUSH_HOST=mac-test\n"
                )

                self.write_executable(user_bin / "herdr", f"""#!/bin/sh
case "$1 $2" in
  "status server")
    if [ -f "{state}" ]; then
      echo '{{"running":true}}'
    else
      echo '{{"running":false}}'
    fi
    ;;
  "server ") touch "{state}" ;;
  "server reload-config") exit 0 ;;
  "plugin config-dir")
    if [ "$3" = "herdr.push" ]; then
      echo "$HOME/.config/herdr/plugins/config/herdr.push"
    else
      mkdir -p "$HOME/.config/herdr/plugins/config/herdr.push.remote"
      echo "$HOME/.config/herdr/plugins/config/herdr.push.remote"
    fi
    ;;
  "plugin unlink"|"plugin link"|"plugin disable") exit 0 ;;
  "pane list") echo '{{"result":{{"panes":[]}}}}' ;;
  "workspace list") echo '{{"result":{{"workspaces":[]}}}}' ;;
  *) echo "unexpected herdr args: $*" >&2; exit 2 ;;
esac
""")
                self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Darwin\n")
                self.write_executable(fake_bin / "plutil", "#!/bin/sh\nexit 0\n")
                self.write_executable(fake_bin / "launchctl", f"""#!/bin/sh
echo "$*" >> "{launch_log}"
case "$1" in
  print) exit 0 ;;
  bootout|enable|kickstart) exit 0 ;;
  bootstrap)
    case "$2" in
      gui/*) echo 'Bootstrap failed: 5: Input/output error' >&2; exit 5 ;;
      user/*) exit 0 ;;
    esac
    ;;
esac
exit 0
""")

                env = os.environ.copy()
                env.update({
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "HERDR_PUSH_UPDATE_URL": f"file://{RELAY_DIR / 'web'}",
                    "HERDR_PUSH_EDGE_IP": "127.0.0.1",
                })
                result = subprocess.run(
                    ["sh", str(UPDATER)],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=20,
                )
                server.shutdown()

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(state.exists(), "updater did not start the stopped Herdr server")
            self.assertIn("Herdr server가 꺼져 있어 시작합니다", result.stdout)
            self.assertIn("LaunchAgent: user/", result.stdout)
            calls = launch_log.read_text()
            self.assertIn("bootstrap gui/", calls)
            self.assertIn("bootstrap user/", calls)
            remote_env = (
                home / ".config" / "herdr" / "plugins" / "config"
                / "herdr.push.remote" / ".env"
            ).read_text()
            self.assertIn(f"HERDR_RELAY=http://127.0.0.1:{server.server_address[1]}", remote_env)
            self.assertIn("HERDR_RELAY_TOKEN=test-token", remote_env)
            self.assertIn("HERDR_PUSH_HOST=mac-test", remote_env)
            self.assertIn("HERDR_PUSH_EDGE_IP=127.0.0.1", remote_env)

    def test_macos_background_fallback_stays_singleton_across_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            home = root / "home"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            home.mkdir()
            legacy_config = (
                home / ".config" / "herdr" / "plugins" / "config" / "herdr.push"
            )
            legacy_config.mkdir(parents=True)

            with socketserver.TCPServer(("127.0.0.1", 0), QuietHandler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                (legacy_config / ".env").write_text(
                    f"HERDR_RELAY=http://127.0.0.1:{server.server_address[1]}\n"
                    "HERDR_RELAY_TOKEN=test-token\n"
                    "HERDR_PUSH_HOST=mac-fallback\n"
                )
                self.write_executable(fake_bin / "herdr", """#!/bin/sh
case "$1 $2" in
  "status server") echo '{"running":true}' ;;
  "server reload-config") exit 0 ;;
  "plugin config-dir")
    if [ "$3" = "herdr.push" ]; then
      echo "$HOME/.config/herdr/plugins/config/herdr.push"
    else
      mkdir -p "$HOME/.config/herdr/plugins/config/herdr.push.remote"
      echo "$HOME/.config/herdr/plugins/config/herdr.push.remote"
    fi
    ;;
  "plugin unlink"|"plugin link"|"plugin disable") exit 0 ;;
  "pane list") echo '{"result":{"panes":[]}}' ;;
  "workspace list") echo '{"result":{"workspaces":[]}}' ;;
  *) echo "unexpected herdr args: $*" >&2; exit 2 ;;
esac
""")
                self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Darwin\n")
                self.write_executable(fake_bin / "plutil", "#!/bin/sh\nexit 0\n")
                self.write_executable(fake_bin / "launchctl", """#!/bin/sh
case "$1" in
  print) exit 0 ;;
  bootstrap|load) exit 5 ;;
  *) exit 0 ;;
esac
""")
                env = os.environ.copy()
                env.update({
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "HERDR_PUSH_UPDATE_URL": f"file://{RELAY_DIR / 'web'}",
                })
                pid2 = None
                try:
                    first = subprocess.run(
                        ["sh", str(UPDATER)], env=env, text=True,
                        capture_output=True, timeout=20,
                    )
                    self.assertEqual(0, first.returncode, first.stderr)
                    pid_file = (
                        home / ".config" / "herdr" / "plugins" / "config"
                        / "herdr.push.remote" / "listener.pid"
                    )
                    pid1 = int(pid_file.read_text().strip())
                    os.kill(pid1, 0)

                    second = subprocess.run(
                        ["sh", str(UPDATER)], env=env, text=True,
                        capture_output=True, timeout=20,
                    )
                    self.assertEqual(0, second.returncode, second.stderr)
                    pid2 = int(pid_file.read_text().strip())
                    self.assertNotEqual(pid1, pid2)
                    os.kill(pid2, 0)

                    for _ in range(20):
                        try:
                            os.kill(pid1, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.05)
                    else:
                        self.fail("previous fallback listener is still running")
                finally:
                    if pid2 is not None:
                        try:
                            os.kill(pid2, 15)
                        except ProcessLookupError:
                            pass
                    server.shutdown()

    @staticmethod
    def write_executable(path, content):
        path.write_text(content)
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
