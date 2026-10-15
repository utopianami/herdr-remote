import asyncio
import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


RELAY_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELAY_DIR))


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, RELAY_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


relay = load_module("herdr_relay_test", "herdr_relay.py")
os.environ.setdefault("HERDR_TG_TOKEN", "test-token")
telegram_bot = load_module("herdr_telegram_test", "herdr_telegram.py")
push_listener = load_module("herdr_push_listener_test", "web/herdr-push-listener.py")


class EmptyHeaders:
    def raw_items(self):
        return []


class RelayWebAssetTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_updater_is_served_from_custom_web_directory(self):
        request = type(
            "Request",
            (),
            {"path": "/herdr-push-update.sh", "headers": EmptyHeaders()},
        )()

        with patch.object(relay, "AUTH_TOKEN", "configured-token"):
            response = await relay.process_request(None, request)

        expected = (RELAY_DIR / "web/herdr-push-update.sh").read_bytes()
        self.assertEqual(200, response.status_code)
        self.assertEqual(expected, response.body)


class BlockedContentTests(unittest.TestCase):
    def test_permission_options_are_removed_but_request_is_kept(self):
        raw = """
╭────────────────────────────────────────╮
Allow command?
Remove the generated cache before rebuilding.
$ rm -rf .cache/build

› 1. Yes, single permission
  2. Trust, always allow
  3. No (tab to edit)
Press enter to confirm or esc to cancel
╰────────────────────────────────────────╯
"""
        content = relay.clean_blocked_content(raw)

        self.assertIn("Allow command?", content)
        self.assertIn("Remove the generated cache", content)
        self.assertIn("rm -rf .cache/build", content)
        self.assertNotIn("single permission", content)
        self.assertNotIn("always allow", content)
        self.assertNotIn("tab to edit", content)
        self.assertNotIn("╭", content)
        self.assertNotIn("╰", content)

    def test_options_are_detected_from_unfiltered_content(self):
        self.assertEqual(
            ["Approve all pending", "Configure individually"],
            relay.detect_options("1. Approve all pending\n2. Configure individually"),
        )

    def test_arbitrary_four_choice_menu_is_preserved(self):
        raw = """
Question 1/1
Which deployment target should I use?

› 1. Staging
  2. Production
  3. Local Docker
  4. Cancel
Press enter to confirm or esc to cancel
"""
        self.assertEqual(
            ["Staging", "Production", "Local Docker", "Cancel"],
            relay.detect_options(raw),
        )
        content = relay.clean_blocked_content(raw)
        self.assertIn("Which deployment target", content)
        self.assertNotIn("Staging", content)

    def test_current_codex_request_drops_all_wrapped_choices(self):
        raw = """
• Running touch /tmp/herdr-approval-probe

Would you like to run the following command?

Environment: local

Reason: Do you want to allow creating the requested empty probe file in
/tmp?

$ touch /tmp/herdr-approval-probe

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `touch /tmp/herdr-
     approval-probe` (p)
  3. No, and tell Codex what to do differently (esc)
Press enter to confirm or esc to cancel
"""
        content = relay.clean_blocked_content(raw)

        self.assertIn("Would you like to run", content)
        self.assertIn("Reason:", content)
        self.assertIn("touch /tmp/herdr-approval-probe", content)
        self.assertNotIn("don't ask again", content)
        self.assertNotIn("approval-probe` (p)", content)
        options = relay.detect_options(raw)
        self.assertEqual(3, len(options))
        self.assertEqual("Yes, proceed (y)", options[0])
        self.assertIn("don't ask again", options[1])
        self.assertEqual("No, and tell Codex what to do differently (esc)", options[2])

    def test_fourth_choice_moves_to_fourth_row_and_confirms(self):
        self.assertEqual(
            ["Down", "Down", "Down", "Enter"],
            relay.response_keys("__choice__:4"),
        )

    def test_unknown_blocked_state_does_not_invent_permission_buttons(self):
        message = relay.blocked_message(
            {"pane_id": "w1:p1", "agent": "codex"}, "Needs approval"
        )
        self.assertEqual([], message["options"])


class UnifiedStatusStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        relay.agents_state.clear()
        relay.host_last_seen.clear()
        relay.listener_synced_hosts.clear()
        relay.ordered_hosts.clear()
        relay.pane_event_seq.clear()
        relay.workspace_event_seq.clear()
        relay.host_snapshot_seq.clear()
        relay.remote_commands.clear()
        relay.remote_command_events.clear()
        self.key = f"{relay.LOCAL_HOST}:w1:p1"

    def push(self, status, agent="codex"):
        _, state = relay.merge_agent_event({
            "type": "agent_event",
            "pane_id": "w1:p1",
            "agent": agent,
            "status": status,
            "cwd": "/work/demo",
            "project": "demo",
            "workspace_id": "w1",
        }, "local")
        return state

    async def test_done_survives_inventory_poll_until_idle_status_event(self):
        state = self.push("done")
        self.assertEqual("done", state["status"])

        _, state = relay.merge_polled_agent({
            "pane_id": "w1:p1",
            "agent": "codex",
            "status": "idle",
            "cwd": "/work/demo",
            "project": "demo",
            "host": "local",
            "remote": None,
        })
        self.assertEqual("done", state["status"])

        _, state = relay.merge_agent_event({
            "pane_id": "w1:p1",
            "agent": "codex",
            "status": "idle",
            "workspace_id": "w1",
        }, "local")
        self.assertEqual("idle", state["status"])

    async def test_idle_push_replaces_done_immediately(self):
        relay.merge_agent_event({
            "pane_id": "w1:p1",
            "agent": "claude",
            "status": "done",
            "cwd": "/work/demo",
            "project": "demo",
            "workspace_id": "w1",
        }, "dev")

        _, state = relay.merge_agent_event({
            "pane_id": "w1:p1",
            "agent": "claude",
            "status": "idle",
            "workspace_id": "w1",
            "workspace_label": "demo",
        }, "dev")
        self.assertEqual("idle", state["status"])

    async def test_late_status_cannot_replace_newer_status(self):
        with patch.object(relay, "broadcast", new=AsyncMock()) as broadcast:
            applied = await relay.apply_agent_event({
                "type": "agent_event",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "host": "dev",
                "agent": "claude",
                "status": "working",
                "event_seq": 200,
            })
            stale_applied = await relay.apply_agent_event({
                "type": "agent_event",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "host": "dev",
                "agent": "claude",
                "status": "idle",
                "event_seq": 100,
            })

        self.assertTrue(applied)
        self.assertFalse(stale_applied)
        self.assertEqual("working", relay.agents_state["dev:w1:p1"]["status"])
        broadcast.assert_awaited_once()

    async def test_unsequenced_legacy_status_is_rejected_after_upgrade(self):
        with patch.object(relay, "broadcast", new=AsyncMock()):
            await relay.apply_agent_event({
                "type": "agent_event",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "host": "galio",
                "status": "done",
                "event_seq": 200,
            })
            applied = await relay.apply_agent_event({
                "type": "agent_event",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "host": "galio",
                "status": "idle",
            })

        self.assertFalse(applied)
        self.assertEqual("done", relay.agents_state["galio:w1:p1"]["status"])

    async def test_late_status_cannot_resurrect_closed_pane(self):
        with patch.object(relay, "broadcast", new=AsyncMock()):
            await relay.apply_agent_event({
                "type": "agent_event",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "host": "dev",
                "status": "done",
                "event_seq": 100,
            })
            await relay.apply_removal_event({
                "type": "pane_removed",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "host": "dev",
                "event_seq": 200,
            })
            applied = await relay.apply_agent_event({
                "type": "agent_event",
                "pane_id": "w1:p1",
                "workspace_id": "w1",
                "host": "dev",
                "status": "idle",
                "event_seq": 150,
            })

        self.assertFalse(applied)
        self.assertNotIn("dev:w1:p1", relay.agents_state)

    async def test_late_status_cannot_resurrect_closed_workspace(self):
        with patch.object(relay, "broadcast", new=AsyncMock()):
            await relay.apply_agent_event({
                "type": "agent_event",
                "pane_id": "w7:p1",
                "workspace_id": "w7",
                "host": "dev",
                "status": "done",
                "event_seq": 100,
            })
            await relay.apply_removal_event({
                "type": "workspace_removed",
                "workspace_id": "w7",
                "host": "dev",
                "event_seq": 200,
            })
            applied = await relay.apply_agent_event({
                "type": "agent_event",
                "pane_id": "w7:p1",
                "workspace_id": "w7",
                "host": "dev",
                "status": "idle",
                "event_seq": 150,
            })

        self.assertFalse(applied)
        self.assertNotIn("dev:w7:p1", relay.agents_state)

    async def test_older_snapshot_cannot_remove_newer_agent(self):
        with patch.object(relay, "broadcast", new=AsyncMock()):
            await relay.apply_agent_event({
                "type": "agent_event",
                "pane_id": "w9:p1",
                "workspace_id": "w9",
                "host": "dev",
                "status": "working",
                "event_seq": 300,
            })
            removed = await relay.apply_host_snapshot({
                "type": "host_snapshot",
                "host": "dev",
                "pane_ids": [],
                "agents": [],
                "event_seq": 200,
            })

        self.assertEqual([], removed)
        self.assertIn("dev:w9:p1", relay.agents_state)

    async def test_done_does_not_expire_without_a_new_status_push(self):
        _, state = relay.merge_agent_event({
            "pane_id": "w1:p1",
            "agent": "claude",
            "status": "done",
            "workspace_id": "w1",
            "workspace_label": "demo",
        }, "dev")
        self.assertNotIn("lifecycle_updated_at", state)
        self.assertEqual("done", state["status"])

    async def test_every_state_uses_the_same_agent_event_path(self):
        for status in ("working", "blocked", "done", "idle"):
            state = self.push(status)
            self.assertEqual(status, state["status"])
        self.assertNotIn("lifecycle_status", state)
        self.assertNotIn("raw_status", state)

    async def test_workspace_removal_is_scoped_to_host_and_workspace(self):
        relay.merge_agent_event({
            "pane_id": "w7:p1",
            "agent": "claude",
            "status": "idle",
            "workspace_id": "w7",
            "workspace_label": "old",
        }, "dev")
        relay.merge_agent_event({
            "pane_id": "w7:p1",
            "agent": "codex",
            "status": "working",
            "workspace_id": "w7",
            "workspace_label": "keep",
        }, "gpu2")

        with patch.object(relay, "broadcast", new=AsyncMock()):
            removed = await relay.apply_removal_event({
                "type": "workspace_removed",
                "workspace_id": "w7",
                "host": "dev",
            })

        self.assertEqual(["dev:w7:p1"], removed)
        self.assertNotIn("dev:w7:p1", relay.agents_state)
        self.assertIn("gpu2:w7:p1", relay.agents_state)

    async def test_successful_poll_snapshot_removes_missing_panes(self):
        relay.merge_agent_event({
            "pane_id": "w2:p1",
            "agent": "codex",
            "status": "idle",
            "workspace_id": "w2",
        }, "gpu2")
        relay.merge_agent_event({
            "pane_id": "w3:p1",
            "agent": "codex",
            "status": "idle",
            "workspace_id": "w3",
        }, "gpu2")

        active = {"gpu2:w3:p1"}
        removed = relay.reconcile_polled_host("gpu2", active)

        self.assertEqual(["gpu2:w2:p1"], removed)
        self.assertNotIn("gpu2:w2:p1", relay.agents_state)
        self.assertIn("gpu2:w3:p1", relay.agents_state)

    async def test_push_host_snapshot_removes_only_missing_agents_on_that_host(self):
        for host, pane_id in (("dev", "w1:p1"), ("dev", "w2:p1"), ("gpu2", "w1:p1")):
            relay.merge_agent_event({
                "pane_id": pane_id,
                "agent": "claude",
                "status": "idle",
                "workspace_id": pane_id.split(":", 1)[0],
            }, host)

        with patch.object(relay, "broadcast", new=AsyncMock()):
            removed = await relay.apply_host_snapshot({
                "type": "host_snapshot",
                "host": "dev",
                "pane_ids": ["w2:p1"],
            })

        self.assertEqual(["dev:w1:p1"], removed)
        self.assertIn("dev:w2:p1", relay.agents_state)
        self.assertIn("gpu2:w1:p1", relay.agents_state)

    async def test_host_snapshot_does_not_change_existing_done_status(self):
        relay.merge_agent_event({
            "pane_id": "w1:p1",
            "agent": "claude",
            "status": "done",
            "workspace_id": "w1",
            "workspace_label": "old-label",
        }, "dev")

        with patch.object(relay, "broadcast", new=AsyncMock()):
            await relay.apply_host_snapshot({
                "type": "host_snapshot",
                "host": "dev",
                "agents": [{
                    "pane_id": "w1:p1",
                    "agent": "claude",
                    "status": "idle",
                    "workspace_id": "w1",
                    "workspace_label": "new-label",
                }],
            })

        state = relay.agents_state["dev:w1:p1"]
        self.assertEqual("done", state["status"])
        self.assertEqual("new-label", state["project"])

    async def test_host_snapshot_restores_agent_missing_after_relay_restart(self):
        with patch.object(relay, "broadcast", new=AsyncMock()) as broadcast:
            await relay.apply_host_snapshot({
                "type": "host_snapshot",
                "host": "dev",
                "agents": [{
                    "pane_id": "w3:p1",
                    "agent": "claude",
                    "status": "working",
                    "workspace_id": "w3",
                    "workspace_label": "restored",
                }],
            })

        self.assertEqual("working", relay.agents_state["dev:w3:p1"]["status"])
        broadcast.assert_awaited_once()

    async def test_inactive_push_host_expires_without_removing_active_hosts(self):
        for host in ("mini", "dev", relay.LOCAL_HOST):
            relay.merge_agent_event({
                "pane_id": "w1:p1",
                "agent": "codex",
                "status": "idle",
                "workspace_id": "w1",
            }, host)
        relay.note_host_activity("mini", seen_at=100)
        relay.note_host_activity("dev", seen_at=180)

        with patch.object(relay, "REMOTE_HOST_TIMEOUT", 60):
            removed = relay.expire_inactive_hosts(now=200)

        self.assertEqual(["mini:w1:p1"], removed)
        self.assertNotIn("mini:w1:p1", relay.agents_state)
        self.assertIn("dev:w1:p1", relay.agents_state)
        self.assertIn(f"{relay.LOCAL_HOST}:w1:p1", relay.agents_state)

    async def test_expired_listener_is_asked_for_a_fresh_snapshot(self):
        self.assertEqual(
            {"type": "sync_snapshot"}, relay.command_for_listener("mini")
        )
        relay.host_last_seen["mini"] = 100
        relay.ordered_hosts.add("mini")
        relay.host_snapshot_seq["mini"] = 200

        with patch.object(relay, "REMOTE_HOST_TIMEOUT", 60):
            relay.expire_inactive_hosts(now=200)

        self.assertNotIn("mini", relay.ordered_hosts)
        self.assertNotIn("mini", relay.host_snapshot_seq)
        self.assertEqual(
            {"type": "sync_snapshot"}, relay.command_for_listener("mini")
        )

    async def test_blocked_fallback_prefers_rendered_pane_over_event_prompt(self):
        key = self.key
        agent = {
            "pane_id": "w1:p1",
            "agent": "codex",
            "status": "blocked",
            "project": "demo",
            "host": "local",
        }
        relay.agents_state[key] = agent

        with (
            patch.object(relay, "BLOCKED_FALLBACK_DELAY", 0),
            patch.object(
                relay,
                "read_blocked_pane",
                return_value=("Actual reason\n$ touch /tmp/probe", relay.CODEX_TOOL_OPTIONS),
            ),
            patch.object(relay, "broadcast", new=AsyncMock()) as broadcast,
        ):
            relay.schedule_blocked_fallback(key, agent, "Yes / Trust / No")
            task = relay.pending_blocked_tasks[key]
            await task

        message = broadcast.await_args.args[0]
        self.assertIn("Actual reason", message["prompt"])
        self.assertNotIn("Trust", message["prompt"])

    async def test_push_only_blocked_uses_sender_content_and_four_choices(self):
        key = "dev:w1:p1"
        agent = {
            "pane_id": "w1:p1",
            "agent": "claude",
            "status": "blocked",
            "project": "memory-html",
            "host": "dev",
        }
        relay.agents_state[key] = agent
        prompt = """Question 1/1
Choose a target.
1. Staging
2. Production
3. Local Docker
4. Cancel
Press enter to confirm
"""

        with (
            patch.object(relay, "BLOCKED_FALLBACK_DELAY", 0),
            patch.object(relay, "read_blocked_pane") as read_pane,
            patch.object(relay, "broadcast", new=AsyncMock()) as broadcast,
        ):
            relay.schedule_blocked_fallback(key, agent, prompt)
            await relay.pending_blocked_tasks[key]

        read_pane.assert_not_called()
        message = broadcast.await_args.args[0]
        self.assertEqual(
            ["Staging", "Production", "Local Docker", "Cancel"],
            message["options"],
        )


class HerdrBinaryTests(unittest.TestCase):
    def test_installed_herdr_binary_is_discovered(self):
        self.assertTrue(pathlib.Path(relay.HERDR).is_file())

    def test_workspace_label_beats_missing_project(self):
        relay.agents_state.clear()
        _, state = relay.merge_agent_event({
            "pane_id": "w7:p1",
            "agent": "claude",
            "status": "working",
            "workspace_label": "payments",
            "workspace_id": "w7",
        }, "dev")
        self.assertEqual("payments", state["project"])
        self.assertEqual("w7", state["workspace_id"])

    def test_tab_identity_is_preserved_in_relay_messages(self):
        relay.agents_state.clear()
        _, state = relay.merge_agent_event({
            "pane_id": "w7:p1",
            "agent": "claude",
            "status": "blocked",
            "workspace_label": "payments",
            "workspace_id": "w7",
            "tab_id": "w7:t2",
            "tab_label": "api",
        }, "dev")

        self.assertEqual("w7:t2", state["tab_id"])
        self.assertEqual("api", state["tab_label"])
        self.assertEqual("api", relay.blocked_message(state, "Approve?")["tab_label"])

    def test_polled_inventory_resolves_tab_label(self):
        responses = {
            ("pane", "list"): {
                "result": {"panes": [{
                    "pane_id": "w7:p1",
                    "workspace_id": "w7",
                    "tab_id": "w7:t2",
                    "cwd": "/work/payments",
                    "agent": "codex",
                    "agent_status": "working",
                }]},
            },
            ("workspace", "list"): {
                "result": {"workspaces": [{
                    "workspace_id": "w7", "label": "payments",
                }]},
            },
            ("tab", "list"): {
                "result": {"tabs": [{"tab_id": "w7:t2", "label": "api"}]},
            },
        }

        def fake_run(*args, remote=None):
            self.assertEqual("gpu2", remote)
            return __import__("json").dumps(responses[args])

        with patch.object(relay, "run_herdr", side_effect=fake_run):
            agents = relay.get_agents_snapshot(remote="gpu2")

        self.assertEqual("payments", agents[0]["project"])
        self.assertEqual("w7:t2", agents[0]["tab_id"])
        self.assertEqual("api", agents[0]["tab_label"])

    def test_remote_push_host_is_not_rewritten_on_local_pane_id_collision(self):
        relay.pane_remote_map["w1:p1"] = None

        host = relay.push_event_host({"host": "dev", "pane_id": "w1:p1"})

        self.assertEqual("dev", host)

    def test_same_pane_id_on_two_hosts_keeps_two_agents(self):
        relay.agents_state.clear()
        relay.merge_agent_event({
            "pane_id": "w1:p1",
            "workspace_id": "w1",
            "workspace_label": "dotfiles",
            "status": "working",
            "agent": "codex",
        }, "gpu2")
        relay.merge_agent_event({
            "pane_id": "w1:p1",
            "workspace_id": "w1",
            "workspace_label": "memory-html",
            "status": "done",
            "agent": "claude",
        }, "dev")

        self.assertEqual({"gpu2:w1:p1", "dev:w1:p1"}, set(relay.agents_state))
        self.assertEqual("dotfiles", relay.agents_state["gpu2:w1:p1"]["project"])
        self.assertEqual("memory-html", relay.agents_state["dev:w1:p1"]["project"])

    def test_push_manifest_subscribes_to_removal_events(self):
        manifest = (RELAY_DIR / "web" / "herdr-push-plugin.toml").read_text()
        self.assertNotIn('on = "pane.agent_status_changed"', manifest)
        self.assertNotIn('on = "workspace.focused"', manifest)
        self.assertIn('on = "pane.closed"', manifest)
        self.assertIn('on = "workspace.closed"', manifest)

    def test_push_listener_refreshes_saved_labels_after_rename(self):
        listener = (RELAY_DIR / "web" / "herdr-push-listener.py").read_text()

        self.assertIn('{"type": "tab.renamed"}', listener)
        self.assertIn('{"type": "workspace.renamed"}', listener)


class TelegramFormattingTests(unittest.TestCase):
    def test_blocked_message_has_reason_and_command_and_escapes_html(self):
        text = telegram_bot.format_blocked_message(
            "codex",
            "demo",
            "Needs <approval>",
            tool="Bash",
            command="echo '<unsafe>'",
        )

        self.assertIn("Needs &lt;approval&gt;", text)
        self.assertIn("Tool: <code>Bash</code>", text)
        self.assertIn("echo &#x27;&lt;unsafe&gt;&#x27;", text)

    def test_location_always_contains_machine_name(self):
        text = telegram_bot.format_blocked_message(
            "codex", "dotfiles", "Choose an option", host="gpu2"
        )
        self.assertIn("dotfiles @gpu2", text)

    def test_location_includes_saved_tab_name(self):
        text = telegram_bot.format_blocked_message(
            "codex",
            "dotfiles",
            "Choose an option",
            host="gpu2",
            tab_label="api",
        )

        self.assertIn("dotfiles / api @gpu2", text)
        self.assertEqual(
            "dotfiles / api @gpu2",
            telegram_bot.agent_location({
                "project": "dotfiles", "tab_label": "api", "host": "gpu2",
            }),
        )

    def test_location_never_falls_back_to_agent_name(self):
        self.assertEqual(
            "unknown @dev",
            telegram_bot.agent_location({
                "pane_id": "w7:p1", "agent": "claude", "host": "dev"
            }),
        )

    def test_keyboard_matches_every_actual_choice(self):
        options = ["Staging", "Production", "Local Docker", "Cancel"]
        keyboard = telegram_bot.make_keyboard("w1:p1", options)
        responses = [
            f"__choice__:{__import__('json').loads(row[0].callback_data)['c']}"
            for row in keyboard.inline_keyboard
        ]
        self.assertEqual(
            ["__choice__:1", "__choice__:2", "__choice__:3", "__choice__:4"],
            responses,
        )
        self.assertEqual(
            ["1. Staging", "2. Production", "3. Local Docker", "4. Cancel"],
            [row[0].text for row in keyboard.inline_keyboard],
        )

    def test_keyboard_carries_remote_host_within_callback_limit(self):
        keyboard = telegram_bot.make_keyboard("w1:p1", ["One", "Two"], "dev")
        payload = keyboard.inline_keyboard[1][0].callback_data

        self.assertLessEqual(len(payload.encode()), 64)
        self.assertEqual({"p": "w1:p1", "h": "dev", "c": 2}, __import__("json").loads(payload))


class TelegramCompletionTimingTests(unittest.TestCase):
    def setUp(self):
        telegram_bot.completion_work_seconds.clear()
        telegram_bot.completion_work_started_at.clear()

    def tearDown(self):
        telegram_bot.completion_work_seconds.clear()
        telegram_bot.completion_work_started_at.clear()

    def test_completion_timer_counts_working_only_and_resets(self):
        key = "dev:w1:p1"
        telegram_bot.update_completion_work(key, None, "working", 0.0)
        telegram_bot.update_completion_work(key, "working", "blocked", 240.0)
        telegram_bot.update_completion_work(key, "blocked", "working", 600.0)

        short_total = telegram_bot.update_completion_work(
            key, "working", "done", 959.0
        )

        self.assertEqual(short_total, 599.0)
        self.assertLess(short_total, telegram_bot.DONE_NOTIFICATION_MIN_SECONDS)
        self.assertNotIn(key, telegram_bot.completion_work_seconds)
        self.assertNotIn(key, telegram_bot.completion_work_started_at)

        telegram_bot.update_completion_work(key, "done", "working", 1000.0)
        long_total = telegram_bot.update_completion_work(
            key, "working", "done", 1600.0
        )

        self.assertEqual(long_total, 600.0)
        self.assertGreaterEqual(long_total, telegram_bot.DONE_NOTIFICATION_MIN_SECONDS)


class TelegramBlockedNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        telegram_bot.pending.clear()
        telegram_bot.pending_hosts.clear()
        telegram_bot.approval_tokens.clear()
        telegram_bot.completion_work_seconds.clear()

    async def asyncTearDown(self):
        telegram_bot.pending.clear()
        telegram_bot.pending_hosts.clear()
        telegram_bot.approval_tokens.clear()
        telegram_bot.completion_work_seconds.clear()

    async def test_blocked_notification_ignores_completion_time_threshold(self):
        key = "dev:w1:p1"
        telegram_bot.completion_work_seconds[key] = 1.0
        app = AsyncMock()
        app.bot.send_message.return_value.message_id = 42

        with patch.object(telegram_bot, "CHAT_ID", "12345"):
            await telegram_bot.notify_blocked(
                app,
                pane_id="w1:p1",
                agent="codex",
                project="dotfiles",
                prompt="Choose a deployment target",
                options=["Staging", "Production"],
                host="dev",
            )

        app.bot.send_message.assert_awaited_once()
        self.assertIn(key, telegram_bot.approval_tokens)


class ClientBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        relay.clients.clear()

    async def asyncTearDown(self):
        relay.clients.clear()

    async def test_slow_client_does_not_block_or_drop_healthy_client(self):
        slow = AsyncMock()
        fast = AsyncMock()

        async def hang(_data):
            await asyncio.sleep(1)

        slow.send.side_effect = hang
        relay.clients.update((slow, fast))

        with patch.object(relay, "CLIENT_SEND_TIMEOUT", 0.01):
            await relay.broadcast({"type": "agents", "agents": []})

        self.assertNotIn(slow, relay.clients)
        self.assertIn(fast, relay.clients)
        fast.send.assert_awaited_once()


class RemoteCommandTests(unittest.TestCase):
    def setUp(self):
        relay.remote_commands.clear()
        relay.remote_command_events.clear()
        relay.pane_remote_map.clear()
        relay.listener_synced_hosts.clear()
        relay.host_last_seen.clear()

    def test_fourth_choice_is_queued_for_push_only_host(self):
        relay.dispatch_response("dev", "w1:p1", "__choice__:4")

        self.assertEqual({
            "type": "send_keys",
            "pane_id": "w1:p1",
            "keys": ["Down", "Down", "Down", "Enter"],
        }, relay.pop_remote_command("dev"))
        self.assertIsNone(relay.pop_remote_command("dev"))

    def test_listener_gets_one_snapshot_request_after_relay_restart(self):
        self.assertEqual({"type": "sync_snapshot"}, relay.command_for_listener("dev"))
        self.assertIsNone(relay.command_for_listener("dev"))

    def test_listener_command_poll_refreshes_host_heartbeat(self):
        with patch.object(relay.time, "time", return_value=123):
            relay.command_for_listener("dev")

        self.assertEqual(123, relay.host_last_seen["dev"])

    def test_user_command_follows_initial_snapshot_request(self):
        relay.dispatch_response("dev", "w1:p1", "__choice__:2")

        self.assertEqual({"type": "sync_snapshot"}, relay.command_for_listener("dev"))
        self.assertEqual({
            "type": "send_keys",
            "pane_id": "w1:p1",
            "keys": ["Down", "Enter"],
        }, relay.command_for_listener("dev"))

    def test_listener_uses_authenticated_long_poll_url(self):
        url = push_listener.command_url("wss://relay.example", "dev box")

        self.assertEqual(
            "https://relay.example/command?host=dev%20box&wait=5",
            url,
        )

    def test_listener_builds_idle_event_from_current_herdr_state(self):
        def fake_herdr_json(_herdr, *args):
            if args[:2] == ("pane", "get"):
                return {"pane": {
                    "pane_id": "w9:p1",
                    "workspace_id": "w9",
                    "tab_id": "w9:t2",
                    "cwd": "/work/demo",
                    "agent": "codex",
                    "agent_status": "idle",
                }}
            if args == ("workspace", "list"):
                return {"workspaces": [{"workspace_id": "w9", "label": "demo"}]}
            if args == ("tab", "list"):
                return {"tabs": [{"tab_id": "w9:t2", "label": "api"}]}
            raise AssertionError(args)

        with tempfile.TemporaryDirectory() as temp, patch.object(
            push_listener, "herdr_json", side_effect=fake_herdr_json
        ):
            payload = push_listener.current_agent_payload(
                "herdr", "w9:p1", "dev", pathlib.Path(temp) / ".env"
            )

        self.assertEqual("agent_event", payload["type"])
        self.assertEqual("idle", payload["status"])
        self.assertEqual("demo", payload["workspace_label"])
        self.assertEqual("w9:t2", payload["tab_id"])
        self.assertEqual("api", payload["tab_label"])
        self.assertIsInstance(payload["event_seq"], int)

    def test_listener_snapshot_includes_saved_tab_name(self):
        def fake_herdr_json(_herdr, *args):
            if args == ("pane", "list"):
                return {"panes": [{
                    "pane_id": "w9:p1",
                    "workspace_id": "w9",
                    "tab_id": "w9:t2",
                    "cwd": "/work/demo",
                    "agent": "codex",
                    "agent_status": "working",
                }]}
            if args == ("workspace", "list"):
                return {"workspaces": [{"workspace_id": "w9", "label": "demo"}]}
            if args == ("tab", "list"):
                return {"tabs": [{"tab_id": "w9:t2", "label": "api"}]}
            raise AssertionError(args)

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(push_listener, "herdr_json", side_effect=fake_herdr_json),
            patch.object(push_listener, "push_event") as push_event,
        ):
            push_listener.sync_agents(
                "https://relay.example",
                "token",
                "dev",
                "herdr",
                pathlib.Path(temp) / ".env",
            )

        snapshot = push_event.call_args.args[2]
        self.assertEqual("w9:t2", snapshot["agents"][0]["tab_id"])
        self.assertEqual("api", snapshot["agents"][0]["tab_label"])

    def test_listener_executes_queued_keys_on_local_herdr(self):
        command = {
            "type": "send_keys",
            "pane_id": "w1:p1",
            "keys": ["Down", "Down", "Down", "Enter"],
        }
        with patch.object(push_listener.subprocess, "run") as run:
            push_listener.execute_command(command, "/usr/bin/herdr")

        run.assert_called_once_with(
            [
                "/usr/bin/herdr", "pane", "send-keys", "w1:p1",
                "Down", "Down", "Down", "Enter",
            ],
            check=False,
            timeout=10,
        )

    def test_listener_edge_ip_bypasses_dns_with_curl_resolve(self):
        completed = __import__("subprocess").CompletedProcess(
            args=[], returncode=0,
            stdout='{"type":"send_text","pane_id":"w1:p1","text":"ok"}'
                   "\n__HERDR_STATUS__:200",
            stderr="",
        )
        with patch.object(push_listener.subprocess, "run", return_value=completed) as run:
            command = push_listener.poll_command(
                "wss://latent.ngrok.pro", "token", "dev", "57.182.204.249"
            )

        self.assertEqual("send_text", command["type"])
        self.assertIn(
            "latent.ngrok.pro:443:57.182.204.249",
            run.call_args.args[0],
        )

    def test_listener_ignores_legacy_edge_ip_on_cloudflare(self):
        self.assertEqual(
            "",
            push_listener.edge_ip_for_relay(
                "https://yn-herdr.vault.haus", "57.182.204.249"
            ),
        )

    def test_listener_uses_cloudflare_safe_user_agent(self):
        self.assertEqual(
            {
                "Authorization": "Bearer token",
                "User-Agent": "herdr-push/0.5.2",
            },
            push_listener.relay_headers("token"),
        )

    def test_listener_replays_all_statuses_after_restart_then_only_new_panes(self):
        current = {"w1:p1", "w1:p2"}

        self.assertEqual(
            current, push_listener.panes_to_refresh(None, current)
        )
        self.assertEqual(
            {"w1:p2"}, push_listener.panes_to_refresh({"w1:p1"}, current)
        )


if __name__ == "__main__":
    unittest.main()
