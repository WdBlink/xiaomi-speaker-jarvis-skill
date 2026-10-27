from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from home_jarvis.core import HomeAction
from home_jarvis.home import HomeAssistantGateway, HomeRequestError


class ContextHA:
    def __init__(self):
        self.config = {"time_zone": "Asia/Shanghai", "internal_url": "private-address",
                       "latitude": "private-location"}
        self.states = [
            {"entity_id": entity, "state": state,
             "attributes": {"current_position": position, "supported_features": 15,
                            "private_data": "private-attribute"}}
            for entity, state, position in (
                ("cover.living_sheer", "open", 100), ("cover.living_blackout", "open", 100),
                ("cover.bedroom_sheer", "closed", 0), ("cover.bedroom_blackout", "open", 1),
                ("cover.garage", "open", 100), ("cover.no_close", "open", 100),
            )
        ] + [
            {"entity_id": "light.curtain", "state": "on", "attributes": {}},
            {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {"elevation": 20}},
            {"entity_id": "sensor.private", "state": "private-sensor", "attributes": {}},
        ]
        self.reads = []
        self.fail_path = None

    def request(self, path):
        self.reads.append(path)
        if path == self.fail_path:
            raise HomeRequestError("private-failure-details")
        if path == "/api/config":
            return deepcopy(self.config)
        if path == "/api/states":
            return deepcopy(self.states)
        raise AssertionError("planning must only read config and the state list")

    def state(self, entity):
        return next(item for item in self.states if item["entity_id"] == entity)


class HomeContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        token = root / "token"
        token.write_text("fixture-only-token")
        token.chmod(0o600)
        config = {
            "schema_version": 1, "base_url": "http://127.0.0.1:1", "token_file": str(token),
            "owners": ["resident"], "default_room": "living_room",
            "rooms": [{"id": "living_room", "name": "客厅", "aliases": ["起居室"]},
                      {"id": "bedroom", "name": "主卧", "aliases": ["卧室"]}],
            "devices": [
                {"entity_id": entity, "room_id": room, "name": name,
                 "actions": ["open", "close", "stop", "set_position"]}
                for entity, room, name in (
                    ("cover.living_sheer", "living_room", "客厅纱帘"),
                    ("cover.living_blackout", "living_room", "客厅遮阳帘"),
                    ("cover.bedroom_sheer", "bedroom", "主卧纱帘"),
                    ("cover.bedroom_blackout", "bedroom", "主卧布帘"),
                    ("cover.garage", "living_room", "车库门"),
                )
            ] + [
                {"entity_id": "cover.no_close", "room_id": "living_room", "name": "备用窗帘",
                 "actions": ["open"]},
                {"entity_id": "light.curtain", "room_id": "living_room", "name": "客厅窗帘灯",
                 "actions": ["on", "off"]},
            ],
        }
        config_file = root / "home.json"
        config_file.write_text(json.dumps(config, ensure_ascii=False))
        self.gateway = HomeAssistantGateway(config_file)
        self.ha = ContextHA()
        requests = patch.object(self.gateway, "_request", side_effect=self.ha.request)
        requests.start()
        self.addCleanup(requests.stop)
        self.now = datetime(2026, 10, 27, 1, tzinfo=timezone.utc).timestamp()

    def context(self, room_id="living_room", now=None):
        return self.gateway.query_home(room_id, self.now if now is None else now)

    def observation(self, entity, context=None):
        return next(item for item in (context or self.context()).devices if item.entity_id == entity)

    def test_live_snapshot_uses_two_reads_and_only_allowlisted_facts(self):
        context = self.context()
        self.assertEqual(self.ha.reads, ["/api/config", "/api/states"])
        self.assertEqual(context.observed_at, self.now)
        self.assertEqual(context.local_time, "2026-10-27T09:00:00+08:00")
        self.assertEqual((context.timezone, context.room_id, context.sun_state),
                         ("Asia/Shanghai", "living_room", "above_horizon"))
        self.assertIsNone(context.observation_error)
        self.assertEqual({item.entity_id for item in context.devices}, set(self.gateway.devices))
        rendered = json.dumps(asdict(context))
        for secret in ("private-address", "private-location", "private-attribute", "private-sensor",
                       "fixture-only-token", "sensor.private"):
            self.assertNotIn(secret, rendered)

    def test_snapshot_refreshes_time_sun_and_state_without_a_time_based_policy(self):
        day = self.context()
        self.ha.state("sun.sun")["state"] = "below_horizon"
        night = self.context(now=self.now + 12 * 3600)
        self.assertEqual(night.local_time, "2026-10-27T21:00:00+08:00")
        self.assertEqual(night.sun_state, "below_horizon")
        self.assertEqual(night.devices, day.devices)
        self.ha.state("cover.living_sheer").update(state="closed")
        self.ha.state("cover.living_sheer")["attributes"]["current_position"] = 0
        updated = self.observation("cover.living_sheer")
        self.assertEqual((updated.state, updated.position), ("closed", 0))
        self.assertEqual(self.ha.reads, ["/api/config", "/api/states"] * 3)

    def test_source_room_is_context_not_a_filter_on_visible_devices(self):
        living = self.context()
        bedroom = self.context(room_id="bedroom")
        self.assertEqual(bedroom.room_id, "bedroom")
        self.assertEqual(living.devices, bedroom.devices)

    def test_ha_failure_returns_unknown_facts_without_private_details(self):
        for path in ("/api/config", "/api/states"):
            with self.subTest(path=path):
                self.ha.fail_path = path
                context = self.context()
                self.assertTrue(context.observation_error)
                self.assertTrue(all(item.state == "unknown" for item in context.devices))
                self.assertIsNone(context.sun_state)
                self.assertNotIn("private-failure-details", json.dumps(asdict(context)))
                expected_zone = "UTC" if path == "/api/config" else "Asia/Shanghai"
                self.assertEqual(context.timezone, expected_zone)

    def test_invalid_ha_config_or_state_list_reports_observation_failure(self):
        for config in (None, {}, {"time_zone": []}, {"time_zone": "No/Such_Zone"},
                       {"time_zone": "/etc/passwd"}):
            with self.subTest(config=config):
                self.ha.config = config
                context = self.context()
                self.assertEqual(context.timezone, "UTC")
                self.assertEqual(context.local_time, "2026-10-27T01:00:00+00:00")
                self.assertTrue(context.observation_error)
        self.ha.config = {"time_zone": "Asia/Shanghai"}
        self.ha.states = {"entity_id": "cover.living_sheer", "state": "open"}
        self.assertTrue(self.context().observation_error)

    def test_invalid_or_missing_observation_is_unknown(self):
        entity = "cover.living_sheer"
        original = deepcopy(self.ha.state(entity))
        invalid = [None, {"entity_id": entity}]
        invalid += [{**original, "state": value} for value in (None, [], "unknown", "unavailable", "on")]
        invalid += [{**original, "attributes": value} for value in (None, [], "bad")]
        invalid += [{**original, "attributes": {**original["attributes"], "current_position": value}}
                    for value in (True, -1, 101, float("nan"), float("inf"), "100", [])]
        invalid += [{**original, "attributes": {**original["attributes"], "supported_features": value}}
                    for value in (True, -1, "15", [])]
        rest = [item for item in self.ha.states if item["entity_id"] != entity]
        for item in invalid:
            with self.subTest(item=item):
                self.ha.states = rest + ([] if item is None else [item])
                observation = self.observation(entity)
                self.assertEqual((observation.state, observation.position, observation.supported_features),
                                 ("unknown", None, 0))

    def test_moving_closed_and_capability_facts_are_preserved_for_the_agent(self):
        entity = "cover.living_sheer"
        for state in ("opening", "closing", "closed"):
            self.ha.state(entity)["state"] = state
            self.assertEqual(self.observation(entity).state, state)
        self.ha.state(entity)["attributes"]["supported_features"] = 1
        self.assertEqual(self.observation(entity).supported_features, 1)
        del self.ha.state(entity)["attributes"]["supported_features"]
        self.assertEqual(self.observation(entity).supported_features, 0)

    def test_duplicate_entity_and_invalid_sun_observations_are_unknown(self):
        entity = "cover.living_sheer"
        self.ha.states.append(deepcopy(self.ha.state(entity)))
        self.assertEqual(self.observation(entity).state, "unknown")
        for state in ([], "unknown", "not_a_sun_state"):
            self.ha.state("sun.sun")["state"] = state
            self.assertIsNone(self.context().sun_state)
        self.ha.state("sun.sun")["state"] = "above_horizon"
        self.ha.states.append(deepcopy(self.ha.state("sun.sun")))
        self.assertIsNone(self.context().sun_state)

    def test_query_does_not_change_the_explicit_text_command_parser(self):
        before = self.gateway.resolve("关闭客厅纱帘")
        self.assertTrue(before)
        self.context()
        self.assertEqual(self.gateway.resolve("关闭客厅纱帘"), before)
        self.assertEqual(self.gateway.resolve("关闭窗帘"), ())

    def test_cancellation_after_state_precheck_prevents_post(self):
        action = HomeAction("living_room", "cover.living_sheer", "close")
        with patch.object(self.gateway, "_state", return_value=self.ha.state(action.entity_id)) as state:
            def cancelled():
                state.assert_called_once_with(action.entity_id)
                raise RuntimeError("private-cancellation-details")

            outcome = self.gateway.execute("resident", "living_room", action, check_current=cancelled)
        self.assertEqual(outcome.status, "failed")
        self.assertIn("未发送", outcome.message)
        self.assertNotIn("private-cancellation-details", outcome.message)
        self.assertEqual(self.ha.reads, [])


if __name__ == "__main__":
    unittest.main()
