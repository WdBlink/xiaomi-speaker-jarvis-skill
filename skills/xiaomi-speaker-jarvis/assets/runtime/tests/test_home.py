from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

from home_jarvis import Core, HomeAction, HomeAssistantGateway, Utterance, configured_gateway



class FakeHA:
    def __init__(self):
        self.states = {
            "switch.ceiling": {"entity_id": "switch.ceiling", "state": "off", "attributes": {}},
            "switch.bedroom": {"entity_id": "switch.bedroom", "state": "off", "attributes": {}},
            "light.desk": {"entity_id": "light.desk", "state": "off", "attributes": {}},
            "cover.curtain": {"entity_id": "cover.curtain", "state": "closed",
                              "attributes": {"supported_features": 15, "current_position": 0}},
        }
        self.calls = []
        self.reads = []
        self.get_status = 200
        self.post_status = 200
        self.after_status = 200
        self.get_delay = self.post_delay = 0
        self.update_state = True
        self.redirect = None
        self.on_post = None
        self.post_body = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if fixture.redirect:
                    self.send_header("Location", fixture.redirect)
                self.end_headers()
                try:
                    self.wfile.write(json.dumps(data).encode())
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                fixture.reads.append((self.path, self.headers.get("Authorization")))
                time.sleep(fixture.get_delay)
                status = fixture.after_status if fixture.calls else fixture.get_status
                entity = self.path.removeprefix("/api/states/")
                if status == 200 and entity not in fixture.states:
                    status = 404
                self.reply(status, deepcopy(fixture.states.get(entity, {})))

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.calls.append((self.path, data, self.headers.get("Authorization")))
                if fixture.on_post:
                    fixture.on_post()
                if fixture.post_status == 200 and fixture.update_state:
                    state = fixture.states[data["entity_id"]]
                    service = self.path.rsplit("/", 1)[-1]
                    if service in {"turn_on", "turn_off"}:
                        state["state"] = service.removeprefix("turn_")
                    else:
                        position = {"open_cover": 100, "close_cover": 0}.get(
                            service, data.get("position", state["attributes"]["current_position"]))
                        state["attributes"]["current_position"] = position
                        state["state"] = "open" if position else "closed"
                time.sleep(fixture.post_delay)
                self.reply(fixture.post_status, fixture.post_body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.ha = FakeHA()
        self.addCleanup(self.ha.close)
        self.token = self.directory / "ha-token"
        self.token.write_text("test-bearer-token\n")
        self.token.chmod(0o600)
        self.config = {
            "schema_version": 1, "base_url": self.ha.url, "token_file": str(self.token),
            "owners": ["resident"], "default_room": "living_room",
            "rooms": [{"id": "living_room", "name": "客厅", "aliases": ["起居室"]},
                      {"id": "bedroom", "name": "卧室"}],
            "devices": [
                {"entity_id": "switch.ceiling", "room_id": "living_room", "name": "顶灯", "aliases": ["灯"],
                 "actions": ["on", "off"]},
                {"entity_id": "switch.bedroom", "room_id": "bedroom", "name": "顶灯", "aliases": ["灯"],
                 "actions": ["on", "off"]},
                {"entity_id": "light.desk", "room_id": "living_room", "name": "台灯", "actions": ["on", "off"]},
                {"entity_id": "cover.curtain", "room_id": "living_room", "name": "客厅窗帘", "aliases": ["窗帘"],
                 "actions": ["open", "close", "stop", "set_position"]},
            ],
        }
        self.config_file = self.directory / "home.json"
        self.save_config()
        self.gateway = self.make_gateway()
        self.db_path = self.directory / "home.sqlite3"
        self.core = Core(self.db_path, self.gateway)
        self.addCleanup(lambda: self.core.close())
        self.core.open_session("session")

    def save_config(self):
        self.config_file.write_text(json.dumps(self.config, ensure_ascii=False))

    def make_gateway(self):
        return HomeAssistantGateway(self.config_file, timeout=0.15, verification_timeout=0.1, poll_interval=0.01)

    def restart(self):
        self.core.close()
        self.core = Core(self.db_path, self.make_gateway())

    def event(self, text="打开客厅灯", utterance_id="turn", **kwargs):
        return replace(Utterance("session", utterance_id, "resident", "qwen3-asr", text), **kwargs)

    def submit(self, text="打开客厅灯", utterance_id="turn"):
        return self.core.submit(self.event(text, utterance_id))

    def configure_layered_curtains(self):
        self.config["rooms"][1] = {"id": "bedroom", "name": "主卧", "aliases": ["卧室"]}
        covers = [
            ("cover.living_sheer", "living_room", "客厅纱帘", []),
            ("cover.living_blackout", "living_room", "客厅遮阳帘", ["客厅布帘"]),
            ("cover.bedroom_sheer", "bedroom", "主卧纱帘", []),
            ("cover.bedroom_blackout", "bedroom", "主卧遮阳帘", ["主卧布帘"]),
            ("cover.garage", "living_room", "车库门", []),
        ]
        self.config["devices"] = self.config["devices"][:3] + [
            {"entity_id": entity, "room_id": room, "name": name, "aliases": aliases,
             "actions": ["open", "close", "stop", "set_position"]}
            for entity, room, name, aliases in covers
        ] + [
            {"entity_id": "light.curtain", "room_id": "living_room", "name": "客厅窗帘灯", "actions": ["on", "off"]},
            {"entity_id": "switch.socket", "room_id": "living_room", "name": "插座", "actions": ["on", "off"]},
        ]
        for entity, *_ in covers:
            self.ha.states[entity] = {"entity_id": entity, "state": "open",
                                      "attributes": {"supported_features": 15, "current_position": 100}}
        for entity in ("light.curtain", "switch.socket"):
            self.ha.states[entity] = {"entity_id": entity, "state": "off", "attributes": {}}
        self.save_config()
        self.gateway = self.make_gateway()
        self.core.gateway = self.gateway

    def test_real_request_and_state_confirmation_are_durable(self):
        def check_committed():
            with sqlite3.connect(self.db_path) as db:
                self.assertEqual(db.execute("SELECT state FROM home_outbox").fetchone()[0], "in_flight")
                receipt = json.loads(db.execute("SELECT receipt FROM submissions").fetchone()[0])
                self.assertEqual(receipt["results"][0]["status"], "uncertain")
                db.execute("BEGIN IMMEDIATE")
                db.rollback()

        self.ha.on_post = check_committed
        receipt = self.submit()
        self.assertEqual(receipt.results[0].status, "applied")
        self.assertEqual(self.ha.calls, [("/api/services/switch/turn_on", {"entity_id": "switch.ceiling"},
                                          "Bearer test-bearer-token")])
        self.assertGreaterEqual(len(self.ha.reads), 2)
        self.assertFalse(self.core.mock_home_effects())
        self.restart()
        self.assertEqual(self.submit("关闭客厅灯"), receipt)
        self.assertEqual(len(self.ha.calls), 1)
        self.assertNotIn("test-bearer-token", self.db_path.read_bytes().decode(errors="ignore"))

    def test_supported_switch_light_and_cover_commands(self):
        commands = [
            ("打开客厅灯", "switch/turn_on", "switch.ceiling", None),
            ("请把起居室的顶灯关掉", "switch/turn_off", "switch.ceiling", None),
            ("打开卧室灯", "switch/turn_on", "switch.bedroom", None),
            ("打开台灯", "light/turn_on", "light.desk", None),
            ("关闭客厅台灯", "light/turn_off", "light.desk", None),
            ("打开客厅窗帘", "cover/open_cover", "cover.curtain", None),
            ("把客厅窗帘开到50%", "cover/set_cover_position", "cover.curtain", 50),
            ("停止客厅窗帘", "cover/stop_cover", "cover.curtain", None),
            ("把窗帘调到0%", "cover/set_cover_position", "cover.curtain", 0),
            ("把窗帘设置到100%", "cover/set_cover_position", "cover.curtain", 100),
            ("关闭客厅窗帘", "cover/close_cover", "cover.curtain", None),
        ]
        for index, (text, service, entity, position) in enumerate(commands):
            with self.subTest(text=text):
                self.assertEqual(self.submit(text, str(index)).results[0].status, "applied")
                expected = {"entity_id": entity}
                if position is not None:
                    expected["position"] = position
                self.assertEqual(self.ha.calls[-1][:2], ("/api/services/" + service, expected))

    def test_ambiguous_malformed_and_unsupported_commands_never_send(self):
        for index, text in enumerate((
            "打开灯", "打开顶灯", "打开所有灯", "打开厨房灯", "不要打开客厅灯", "你能打开客厅灯吗？",
            "打开客厅灯，不对，关闭", "把客厅灯调暗", "调暗台灯", "停止客厅灯", "把客厅灯开到50%",
            "把窗帘开到101%", "把窗帘开到-1%", "把窗帘开到50.5%", "把窗帘开到百分之五十",
            "打开 switch.ceiling", "调用 switch/turn_on", "打开客厅灯，再删除文件",
            "打开客厅灯，再帮我调研框架但不要开灯", "打开客厅灯，再帮我调研框架，不对，取消",
            "打开客厅灯，再打开顶灯", "打开客厅灯，再把卧室灯调暗",
        )):
            with self.subTest(text=text):
                receipt = self.submit(text, str(index))
                self.assertEqual([result.status for result in receipt.results], ["clarification_required"])
        self.assertFalse(self.ha.calls)
        self.assertFalse(self.ha.reads)
        self.assertIsNone(self.core.claim_task())

    def test_same_room_alias_collision_requires_clarification(self):
        self.config["devices"][2]["aliases"] = ["灯"]
        self.save_config()
        gateway = self.make_gateway()
        self.assertEqual(gateway.resolve("打开客厅灯"), ())
        self.assertEqual(gateway.resolve("打开客厅顶灯")[0].entity_id, "switch.ceiling")

    def test_natural_phrases_bind_exact_devices_and_room_categories(self):
        self.configure_layered_curtains()
        curtains = {"cover.living_sheer", "cover.living_blackout"}
        lights = {"switch.ceiling", "light.desk", "light.curtain"}
        cases = (
            ("关闭客厅的窗帘", curtains, "close", None),
            ("请帮我把起居室里的窗帘都给关上", curtains, "close", None),
            ("麻烦你关闭一下客厅全部窗帘", curtains, "close", None),
            ("客厅的所有窗帘拉上", curtains, "close", None),
            ("把客厅窗帘关一下", curtains, "close", None),
            ("把客厅的纱帘给关上", {"cover.living_sheer"}, "close", None),
            ("关客厅的布帘", {"cover.living_blackout"}, "close", None),
            ("起居室里的纱帘开一下", {"cover.living_sheer"}, "open", None),
            ("开启卧室的遮阳帘", {"cover.bedroom_blackout"}, "open", None),
            ("客厅窗帘灯关一下", {"light.curtain"}, "off", None),
            ("打开起居室的所有灯", lights, "on", None),
            ("开启客厅灯光", lights, "on", None),
            ("帮我把客厅的窗帘都调到50%", curtains, "set_position", 50),
        )
        for text, entities, action, position in cases:
            with self.subTest(text=text):
                actual = self.gateway.resolve(text)
                self.assertEqual({item.entity_id for item in actual}, entities)
                self.assertTrue(all(item.action == action and item.position == position for item in actual))
        self.assertFalse(self.ha.reads)
        self.assertFalse(self.ha.calls)

    def test_natural_phrases_keep_negation_unknown_rooms_and_unscoped_categories_unresolved(self):
        self.configure_layered_curtains()
        cases = (
            "关闭窗帘", "关闭所有窗帘", "关闭纱帘", "关闭布帘", "关闭所有灯", "关闭厨房的窗帘",
            "不要关闭客厅的窗帘", "请别把客厅的窗帘关上", "客厅窗帘不要关", "如果关闭客厅窗帘",
            "假如把客厅窗帘拉上", "请说关闭客厅窗帘", "‘关闭客厅窗帘’", "你能关闭客厅窗帘吗？",
            "稍后关闭客厅窗帘", "客厅窗帘晚点关一下", "关闭客厅窗帘，不对，打开",
            "关闭客厅窗帘，再删除文件", "关闭客厅窗帘和未知设备", "客厅窗帘关闭后再打开",
        )
        for index, text in enumerate(cases):
            with self.subTest(text=text):
                receipt = self.submit(text, str(index))
                self.assertEqual([item.status for item in receipt.results], ["clarification_required"])
        self.assertFalse(self.ha.reads)
        self.assertFalse(self.ha.calls)
        self.assertEqual(self.core.db.execute("SELECT count(*) FROM home_outbox").fetchone()[0], 0)

    def test_curtain_groups_reject_unsupported_members_and_exact_alias_collisions(self):
        self.configure_layered_curtains()
        blackout = next(item for item in self.config["devices"] if item["entity_id"] == "cover.living_blackout")
        blackout["actions"].remove("close")
        self.save_config()
        self.core.gateway = self.make_gateway()
        self.assertEqual(self.submit("关闭客厅窗帘").results[0].status, "clarification_required")
        self.assertEqual(self.core.gateway.resolve("关闭客厅纱帘")[0].entity_id, "cover.living_sheer")
        blackout["actions"].append("close")
        for device in self.config["devices"]:
            if device["entity_id"] in {"cover.living_sheer", "cover.living_blackout"}:
                device["aliases"].extend(["客厅东窗", "客厅窗帘"])
        self.save_config()
        self.core.gateway = self.make_gateway()
        for index, text in enumerate(("关闭客厅的东窗", "关闭客厅窗帘")):
            self.assertEqual(self.submit(text, str(index)).results[0].status, "clarification_required")
        self.assertFalse(self.ha.reads)
        self.assertFalse(self.ha.calls)

    def test_room_alias_collision_cannot_expand_curtains_across_rooms(self):
        self.configure_layered_curtains()
        self.config["rooms"][1]["aliases"].append("起居室")
        self.save_config()
        self.core.gateway = self.make_gateway()
        self.assertEqual(self.submit("关闭起居室窗帘").results[0].status, "clarification_required")
        self.assertFalse(self.ha.reads)
        self.assertFalse(self.ha.calls)

    def test_entire_curtain_group_is_authorized_before_persistence_or_dispatch(self):
        self.configure_layered_curtains()
        authorize = self.gateway.authorize
        authorized = []

        def deny_second(owner, room, action):
            authorize(owner, room, action)
            authorized.append(action.entity_id)
            if len(authorized) == 2:
                raise PermissionError("second curtain is not authorized")

        with patch.object(self.gateway, "authorize", side_effect=deny_second), self.assertRaises(PermissionError):
            self.submit("关闭客厅的窗帘")
        self.assertEqual(set(authorized), {"cover.living_sheer", "cover.living_blackout"})
        self.assertEqual(self.core.db.execute("SELECT count(*) FROM home_outbox").fetchone()[0], 0)
        self.assertFalse(self.ha.reads)
        self.assertFalse(self.ha.calls)

    def test_curtain_group_is_durable_before_dispatch_and_never_replayed(self):
        self.configure_layered_curtains()
        snapshots = []

        def check_group_committed():
            with closing(sqlite3.connect(self.db_path)) as db:
                snapshots.append(tuple(row[0] for row in db.execute("SELECT state FROM home_outbox ORDER BY rowid")))
                receipt = json.loads(db.execute("SELECT receipt FROM submissions").fetchone()[0])
                self.assertEqual(len(receipt["results"]), 2)

        self.ha.on_post = check_group_committed
        receipt = self.submit("关闭客厅的窗帘")
        self.assertEqual([item.status for item in receipt.results], ["applied", "applied"])
        self.assertEqual(snapshots, [("in_flight", "pending"), ("applied", "in_flight")])
        self.assertEqual({call[1]["entity_id"] for call in self.ha.calls},
                         {"cover.living_sheer", "cover.living_blackout"})
        self.restart()
        self.assertEqual(self.submit("关闭客厅的窗帘"), receipt)
        self.assertEqual(len(self.ha.calls), 2)

    def test_owner_room_entity_action_and_position_are_authorized(self):
        action = HomeAction("living_room", "switch.ceiling", "on")
        for owner, room, candidate in (
            ("visitor", "living_room", action), ("resident", "missing", action),
            ("resident", "living_room", replace(action, room_id="bedroom")),
            ("resident", "living_room", replace(action, entity_id="switch.not_allowed")),
            ("resident", "living_room", replace(action, entity_id="lock.front_door")),
            ("resident", "living_room", replace(action, action="dim")),
            ("resident", "living_room", replace(action, action="toggle")),
            ("resident", "living_room", replace(action, position=1)),
            ("resident", "living_room", HomeAction("living_room", "cover.curtain", "set_position", True)),
            ("resident", "living_room", HomeAction("living_room", "cover.curtain", "set_position", 101)),
        ):
            with self.subTest(action=candidate), self.assertRaises(PermissionError):
                self.gateway.execute(owner, room, candidate)
        self.core.open_session("visitor", owner_id="visitor")
        with self.assertRaises(PermissionError):
            self.core.submit(self.event(session_id="visitor", owner_id="visitor"))
        self.assertFalse(self.ha.reads)
        self.assertFalse(self.ha.calls)

    def test_entire_mixed_command_is_authorized_before_any_effect(self):
        with self.assertRaises(PermissionError):
            self.submit("打开客厅灯，再帮我调研框架，再取消任务 missing")
        self.assertFalse(self.ha.reads)
        self.assertIsNone(self.core.claim_task())
        self.assertEqual(self.core.db.execute("SELECT count(*) FROM home_outbox").fetchone()[0], 0)

    def test_mixed_home_task_stop_and_notification_semantics(self):
        receipt = self.submit("打开客厅灯，再帮我调研框架")
        self.assertEqual([r.status for r in receipt.results], ["applied", "queued"])
        task_id = receipt.results[1].target_id
        self.submit("别说了", "stop")
        self.assertTrue(self.core.is_speech_stopped("session", "resident"))
        task = self.core.run_mock_worker_once()
        self.assertEqual(task.task_id, task_id)
        self.assertEqual(task.state, "succeeded")
        self.restart()
        self.assertEqual(len(self.core.pending_notifications("resident")), 1)
        self.assertEqual(self.submit(), receipt)
        self.assertEqual(len(self.ha.calls), 1)

    def test_preflight_http_auth_missing_unavailable_and_timeout_fail_without_write(self):
        for index, status in enumerate((401, 403, 404, 500)):
            self.ha.get_status = status
            result = self.submit(utterance_id=str(index)).results[0]
            self.assertEqual(result.status, "failed")
            self.assertIn(str(status), result.message)
        self.ha.get_status = 200
        self.ha.states["switch.ceiling"]["state"] = "unavailable"
        self.assertEqual(self.submit(utterance_id="unavailable").results[0].status, "failed")
        self.ha.states["switch.ceiling"]["state"] = "off"
        self.ha.get_delay = 0.3
        self.assertEqual(self.submit(utterance_id="timeout").results[0].status, "failed")
        self.assertFalse(self.ha.calls)

    def test_post_auth_rejection_is_not_success_and_is_not_retried(self):
        self.ha.post_status = 401
        receipt = self.submit()
        self.assertEqual(receipt.results[0].status, "failed")
        self.assertIn("401", receipt.results[0].message)
        self.restart()
        self.ha.post_status = 200
        self.assertEqual(self.submit(), receipt)
        self.assertEqual(len(self.ha.calls), 1)

    def test_post_500_timeout_and_failed_verification_are_uncertain(self):
        self.ha.post_status = 500
        self.assertEqual(self.submit(utterance_id="server-error").results[0].status, "uncertain")
        self.ha.post_status = 200
        self.ha.post_delay = 0.3
        receipt = self.submit(utterance_id="timeout")
        self.assertEqual(receipt.results[0].status, "uncertain")
        self.assertEqual(self.ha.states["switch.ceiling"]["state"], "on")
        self.restart()
        self.assertEqual(self.submit(utterance_id="timeout"), receipt)
        self.ha.post_delay = 0
        self.ha.after_status = 503
        # A fresh action still checks availability before sending.
        self.ha.calls.clear()
        self.assertEqual(self.submit(utterance_id="verify-failed").results[0].status, "uncertain")
        self.assertEqual(len(self.ha.calls), 1)

    def test_state_mismatch_does_not_claim_success(self):
        self.ha.update_state = False
        result = self.submit().results[0]
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(len(self.ha.calls), 1)
        self.assertGreater(len(self.ha.reads), 2)

    def test_connection_failure_is_durable_and_not_reported_as_mock(self):
        self.gateway.base_url = "http://127.0.0.1:0"
        receipt = self.submit()
        self.assertEqual(receipt.results[0].status, "failed")
        self.restart()
        self.assertEqual(self.submit(), receipt)
        self.assertFalse(self.ha.calls)

    def test_invalid_state_response_is_not_accepted(self):
        self.ha.states["switch.ceiling"]["entity_id"] = "switch.other"
        self.assertEqual(self.submit().results[0].status, "failed")
        self.assertFalse(self.ha.calls)

    def test_cover_requires_live_capability_and_target_position(self):
        self.ha.states["cover.curtain"]["attributes"]["supported_features"] = 3
        self.assertEqual(self.submit("把窗帘开到50%", "unsupported").results[0].status, "failed")
        self.assertFalse(self.ha.calls)
        self.ha.states["cover.curtain"]["attributes"]["supported_features"] = 15
        self.ha.states["cover.curtain"].update(state="open")
        self.ha.states["cover.curtain"]["attributes"]["current_position"] = 40
        self.ha.update_state = False
        self.assertEqual(self.submit("打开窗帘", "not-fully-open").results[0].status, "uncertain")

    def test_redirects_never_forward_authorization(self):
        other = FakeHA()
        self.addCleanup(other.close)
        self.ha.redirect = other.url + "/api/states/switch.ceiling"
        self.ha.get_status = 302
        self.assertEqual(self.submit(utterance_id="get-redirect").results[0].status, "failed")
        self.ha.get_status = 200
        self.ha.post_status = 307
        self.assertEqual(self.submit(utterance_id="post-redirect").results[0].status, "uncertain")
        self.assertFalse(other.reads)
        self.assertFalse(other.calls)

    def test_crash_before_dispatch_preserves_pending_without_replay(self):
        with patch.object(self.core, "_dispatch_home", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.submit()
        self.restart()
        self.assertEqual(self.submit().results[0].status, "pending")
        self.assertFalse(self.ha.calls)
        self.assertFalse(self.ha.reads)

    def test_crash_after_write_preserves_uncertainty_without_replay(self):
        original = self.gateway.execute

        def crash(*args):
            original(*args)
            raise SystemExit("crash after HA applied")

        with patch.object(self.gateway, "execute", side_effect=crash), self.assertRaises(SystemExit):
            self.submit()
        self.restart()
        self.assertEqual(self.submit().results[0].status, "uncertain")
        self.assertEqual(self.core.db.execute("SELECT state FROM home_outbox").fetchone()[0], "in_flight")
        self.assertEqual(len(self.ha.calls), 1)

    def test_receipt_write_failure_cannot_erase_the_started_attempt(self):
        original = self.core._home_result

        def fail_terminal(effect, state, message):
            if state == "applied":
                raise sqlite3.OperationalError("disk failure")
            return original(effect, state, message)

        with patch.object(self.core, "_home_result", side_effect=fail_terminal), self.assertRaises(sqlite3.OperationalError):
            self.submit()
        self.restart()
        self.assertEqual(self.submit().results[0].status, "uncertain")
        self.assertEqual(len(self.ha.calls), 1)

    def test_concurrent_duplicate_does_not_send_again_while_request_runs(self):
        started, finish = Event(), Event()

        def on_post():
            started.set()
            finish.wait(timeout=2)

        self.ha.on_post = on_post

        def submit_first():
            core = Core(self.db_path, HomeAssistantGateway(self.config_file, timeout=2))
            try:
                return core.submit(self.event())
            finally:
                core.close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(submit_first)
            try:
                self.assertTrue(started.wait(timeout=2))
                self.assertEqual(self.submit().results[0].status, "uncertain")
            finally:
                finish.set()
            receipt = first.result(timeout=3)
        self.assertEqual(receipt.results[0].status, "applied")
        self.assertEqual(self.submit(), receipt)
        self.assertEqual(len(self.ha.calls), 1)

    def test_configuration_and_secret_boundaries_fail_closed(self):
        with self.assertRaises(ValueError):
            Core(":memory:", self.gateway)
        original = deepcopy(self.config)
        variants = []
        for key, value in (("schema_version", True), ("owners", []), ("rooms", []), ("devices", []),
                           ("default_room", "missing"), ("base_url", "http://user:secret@localhost"),
                           ("base_url", self.ha.url + "/api"), ("base_url", self.ha.url + "?x=1"),
                           ("token_file", "relative-token")):
            variants.append(dict(original, **{key: value}))
        for key, value in (("actions", ["dim"]), ("actions", ["toggle"]), ("entity_id", "lock.front"),
                           ("entity_id", "switch.ceiling/../../"), ("room_id", "missing"), ("name", "灯，再开门")):
            variant = deepcopy(original)
            variant["devices"][0][key] = value
            variants.append(variant)
        for variant in variants:
            self.config = variant
            self.save_config()
            with self.subTest(config=variant), self.assertRaises(ValueError):
                self.make_gateway()
        self.config = original
        self.save_config()
        self.token.chmod(0o644)
        with self.assertRaises(ValueError):
            self.make_gateway()
        self.token.chmod(0o600)
        (self.directory / ".git").mkdir()
        with self.assertRaises(ValueError):
            self.make_gateway()
        (self.directory / ".git").rmdir()
        self.token.write_text("test-bearer-token\nother-header: value")
        with self.assertRaises(ValueError):
            self.make_gateway()
        self.assertFalse(self.ha.reads)

    def test_fifo_token_is_rejected_without_blocking_startup(self):
        fifo = self.directory / "token-fifo"
        os.mkfifo(fifo, 0o600)
        self.config["token_file"] = str(fifo)
        self.save_config()
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                   JARVIS_HOME_CONFIG=str(self.config_file))
        result = subprocess.run(
            [sys.executable, "-c", "from home_jarvis import configured_gateway; configured_gateway()"],
            env=env, capture_output=True, text=True, timeout=2,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular file with mode 0600", result.stderr)
        self.assertFalse(self.ha.reads)
        self.assertFalse(self.ha.calls)

    def test_config_loader_uses_explicit_then_environment_then_default(self):
        with patch.dict(os.environ, {"JARVIS_HOME_CONFIG": str(self.config_file)}):
            self.assertIsInstance(configured_gateway(), HomeAssistantGateway)
            with self.assertRaises(FileNotFoundError):
                configured_gateway(self.directory / "missing")
        with patch.dict(os.environ, {"JARVIS_HOME_CONFIG": str(self.directory / "missing")}):
            with self.assertRaises(FileNotFoundError):
                configured_gateway()
            self.assertIsInstance(configured_gateway(self.config_file), HomeAssistantGateway)
        with patch.dict(os.environ, {}, clear=True), patch("home_jarvis.home.DEFAULT_HOME_CONFIG", self.config_file):
            self.assertIsInstance(configured_gateway(), HomeAssistantGateway)

    def test_cli_real_default_and_explicit_mock(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                   JARVIS_HOME_CONFIG=str(self.config_file))
        command = [sys.executable, "-m", "home_jarvis", "打开客厅灯", "--db", str(self.directory / "cli.sqlite3"),
                   "--utterance-id", "cli-turn"]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["results"][0]["status"], "applied")
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout)["results"][0]["status"], "applied")
        self.assertEqual(len(self.ha.calls), 1)
        env["JARVIS_HOME_CONFIG"] = str(self.directory / "missing")
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("mock_applied", result.stdout)
        result = subprocess.run([sys.executable, "-m", "home_jarvis", "--home-config", str(self.config_file)],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"mode": "mock"', result.stdout)
        result = subprocess.run(command + ["--mock", "--session", "mock"], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout)["results"][0]["status"], "mock_applied")
        self.assertEqual(len(self.ha.calls), 1)



if __name__ == "__main__":
    unittest.main()
