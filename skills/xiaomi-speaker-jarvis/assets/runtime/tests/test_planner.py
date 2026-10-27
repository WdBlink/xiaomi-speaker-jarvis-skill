from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock

from home_jarvis.core import DeviceObservation, HomeAction, ModelPlan, PlanningContext
from home_jarvis.home import Device, Room
from home_jarvis.planner import MiniMaxPlanner, PlannerError


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.body[:size]


class Gateway:
    rooms = {"kitchen": Room("kitchen", "厨房", ("厨房间",))}
    devices = {
        "light.kitchen": Device("light.kitchen", "kitchen", "厨房灯", ("主灯",), frozenset({"on", "off"})),
        "cover.window": Device("cover.window", "kitchen", "窗帘", (), frozenset({"open", "close", "set_position"})),
    }


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.key = root / "minimax.key"
        self.key.write_text("test-secret-key\n", encoding="utf-8")
        self.key.chmod(0o600)
        self.config = root / "planner.json"
        self._write_config()
        self.planner = MiniMaxPlanner(self.config, Gateway())
        self.transport = Mock()
        self.planner._http = self.transport

    def _write_config(self, **changes):
        config = {"endpoint": "https://api.minimaxi.com/v1/chat/completions",
                  "model": "MiniMax-M3", "api_key_file": str(self.key)}
        config.update(changes)
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.config.chmod(0o600)

    @staticmethod
    def completion(content=None, *, finish="stop", message_extra=None):
        message = {"role": "assistant", "content": content}
        if message_extra:
            message.update(message_extra)
        return json.dumps({"choices": [{"finish_reason": finish, "message": message}]}).encode()

    @staticmethod
    def tool(name, arguments, *, call_id="call_1"):
        return {"id": call_id, "type": "function", "function": {
            "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}

    def tool_response(self, name, arguments, **message_extra):
        return FakeResponse(self.completion(finish="tool_calls", message_extra={
            "tool_calls": [self.tool(name, arguments)], **message_extra}))

    def respond(self, content, **kwargs):
        self.transport.open.side_effect = None
        self.transport.open.return_value = FakeResponse(self.completion(content, **kwargs))

    def control(self, arguments):
        self.transport.open.side_effect = None
        self.transport.open.return_value = self.tool_response("control_home", arguments)

    def snapshot(self, local_time="2026-10-27T21:00:00+08:00", *, sun_state="below_horizon", position=35.5):
        return PlanningContext(
            observed_at=datetime.fromisoformat(local_time).timestamp(), local_time=local_time,
            timezone="Asia/Shanghai", room_id="kitchen",
            devices=(DeviceObservation("cover.window", "open", position, 7),), sun_state=sun_state)

    def payloads(self):
        return [json.loads(call.args[0].data) for call in self.transport.open.call_args_list]

    def test_plain_reply_uses_native_tools_and_keeps_user_text(self):
        self.respond("你好，我在。", message_extra={"reasoning_content": "private reasoning"})
        query = Mock()
        result = self.planner.plan("嗯，能帮我解释一下灯的工作原理吗？", query_home=query)
        self.assertEqual(result, ModelPlan(message="你好，我在。"))
        query.assert_not_called()
        args, kwargs = self.transport.open.call_args
        self.assertEqual(args[0].full_url, "https://api.minimaxi.com/v1/chat/completions")
        self.assertEqual(args[0].headers["Authorization"], "Bearer test-secret-key")
        self.assertEqual(kwargs["timeout"], 20)
        payload = self.payloads()[0]
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertIs(payload["reasoning_split"], True)
        self.assertEqual(payload["max_completion_tokens"], 1024)
        self.assertEqual([tool["function"]["name"] for tool in payload["tools"]], ["query_home", "control_home"])
        self.assertEqual(payload["messages"][1]["content"], "嗯，能帮我解释一下灯的工作原理吗？")
        self.assertIn('"aliases":["主灯"]', payload["messages"][0]["content"])
        self.assertIn('"room_aliases":["厨房间"]', payload["messages"][0]["content"])
        self.assertNotIn("private reasoning", repr(result))
        self.assertNotIn("test-secret-key", repr(result))

    def test_control_binds_room_from_catalog_and_ignores_preexecution_content(self):
        self.transport.open.return_value = self.tool_response(
            "control_home", {"actions": [{"entity_id": "light.kitchen", "action": "on"}]},
            content="模型提前声称已经成功")
        query = Mock()
        result = self.planner.plan("嗯，麻烦把厨房的灯都开了吧", query_home=query)
        self.assertEqual(result, ModelPlan("", (HomeAction("kitchen", "light.kitchen", "on"),)))
        query.assert_not_called()
        self.transport.open.assert_called_once()

    def test_query_round_trip_preserves_full_assistant_message_and_real_snapshot(self):
        for local_time, sun_state, position in (
            ("2026-10-27T10:00:00+08:00", "above_horizon", 100),
            ("2026-10-27T21:00:00+08:00", "below_horizon", 1),
        ):
            with self.subTest(local_time=local_time):
                self.transport.reset_mock()
                query_call = {**self.tool("query_home", {}), "index": 0}
                query_response = FakeResponse(self.completion(finish="tool_calls", message_extra={
                    "tool_calls": [query_call], "reasoning_content": "private reasoning",
                    "name": "MiniMax AI", "audio_content": ""}))
                self.transport.open.side_effect = [query_response, FakeResponse(self.completion("建议保留一部分采光。"))]
                context = self.snapshot(local_time, sun_state=sun_state, position=position)
                query = Mock(return_value=context)
                result = self.planner.plan("现在应该关哪个窗帘？", query_home=query)
                self.assertEqual(result, ModelPlan("建议保留一部分采光。", model_calls=2))
                query.assert_called_once_with()
                first, second = self.payloads()
                self.assertEqual([item["role"] for item in second["messages"]], ["system", "user", "assistant", "tool"])
                self.assertEqual(second["messages"][2], json.loads(query_response.body)["choices"][0]["message"])
                tool_result = second["messages"][3]
                self.assertEqual(tool_result["tool_call_id"], "call_1")
                facts = json.loads(tool_result["content"])
                self.assertEqual(facts["snapshot"], json.loads(json.dumps(asdict(context))))
                self.assertEqual({item["entity_id"] for item in facts["catalog"]}, set(Gateway.devices))
                self.assertEqual([tool["function"]["name"] for tool in second["tools"]], ["control_home"])
                self.assertNotIn("private reasoning", repr(result))
                self.assertEqual(first["messages"], second["messages"][:2])

    def test_query_then_one_terminal_control_batch(self):
        self.transport.open.side_effect = [self.tool_response("query_home", {}), self.tool_response(
            "control_home", {"actions": [{"entity_id": "cover.window", "action": "close"}], "reason": "为了隐私"})]
        result = self.planner.plan("你看看现在情况，替我决定然后把合适的窗帘关上", query_home=Mock(return_value=self.snapshot()))
        self.assertEqual(result, ModelPlan("", (HomeAction("kitchen", "cover.window", "close"),), "为了隐私", 2))
        self.assertEqual(self.transport.open.call_count, 2)

    def test_query_is_unavailable_without_callback_and_cannot_repeat(self):
        self.transport.open.return_value = self.tool_response("query_home", {})
        with self.assertRaises(PlannerError):
            self.planner.plan("现在几点？")
        self.assertEqual([tool["function"]["name"] for tool in self.payloads()[0]["tools"]], ["control_home"])
        self.transport.reset_mock()
        query = Mock(return_value=self.snapshot())
        with self.assertRaises(PlannerError):
            self.planner.plan("现在几点？", query_home=query)
        self.assertEqual(self.transport.open.call_count, 2)
        query.assert_called_once()

    def test_query_rejects_parameters_and_invalid_snapshot_without_more_requests(self):
        query = Mock(return_value=self.snapshot())
        for arguments in ({"entity_id": "sensor.private"}, [], None, {"owner_id": "admin"}):
            with self.subTest(arguments=arguments):
                self.transport.open.return_value = self.tool_response("query_home", arguments)
                with self.assertRaises(PlannerError):
                    self.planner.plan("查询", query_home=query)
        query.assert_not_called()
        self.transport.reset_mock()
        self.transport.open.return_value = self.tool_response("query_home", {})
        with self.assertRaises(PlannerError):
            self.planner.plan("查询", query_home=Mock(return_value={"state": "fake"}))
        self.transport.open.assert_called_once()

    def test_query_failure_is_not_retried(self):
        self.transport.open.return_value = self.tool_response("query_home", {})
        query = Mock(side_effect=RuntimeError("cancelled"))
        with self.assertRaises(RuntimeError):
            self.planner.plan("查询", query_home=query)
        self.transport.open.assert_called_once()
        query.assert_called_once()

    def test_all_31_configured_devices_fit_one_control_batch(self):
        gateway = Gateway()
        gateway.devices = {f"light.lamp_{index}": Device(f"light.lamp_{index}", "kitchen", f"灯{index}", (),
                                                       frozenset({"on", "off"})) for index in range(31)}
        self.planner = MiniMaxPlanner(self.config, gateway)
        self.planner._http = self.transport
        actions = [{"entity_id": entity, "action": "off"} for entity in gateway.devices]
        self.control({"actions": actions})
        result = self.planner.plan("把家里所有灯都关掉")
        self.assertEqual([action.entity_id for action in result.actions], list(gateway.devices))
        schema = self.payloads()[0]["tools"][0]["function"]["parameters"]["properties"]["actions"]
        self.assertEqual(schema["maxItems"], 31)
        self.assertGreaterEqual(self.payloads()[0]["max_completion_tokens"], 31 * 128)
        self.transport.open.assert_called_once()

    def test_reason_is_internal_bounded_text(self):
        for reason in ("太阳已在地平线下。", "依" * 200):
            self.control({"reason": reason, "actions": [{"entity_id": "cover.window", "action": "close"}]})
            result = self.planner.plan("选择并关闭合适的窗帘")
            self.assertEqual((result.message, result.reason), ("", reason))
        for reason in (None, 1, [], "", " ", "依" * 201, "依据\x00", "\ud800"):
            with self.subTest(reason=repr(reason)):
                self.control({"reason": reason, "actions": [{"entity_id": "cover.window", "action": "close"}]})
                with self.assertRaises(PlannerError):
                    self.planner.plan("关闭窗帘")

    def test_unknown_and_missing_control_fields_are_rejected(self):
        action = {"entity_id": "cover.window", "action": "close"}
        for arguments in (None, [], {}, {"actions": [action], "message": "已成功"},
                          {"actions": [action], "room_id": "kitchen"}, {"actions": [action], "owner_id": "admin"},
                          {"actions": [{**action, "room_id": "kitchen"}]}, {"actions": [{"action": "close"}]}):
            with self.subTest(arguments=arguments):
                self.control(arguments)
                with self.assertRaises(PlannerError):
                    self.planner.plan("关闭窗帘")

    def test_invalid_entities_actions_and_duplicate_devices_are_rejected(self):
        lamp = {"entity_id": "light.kitchen", "action": "on"}
        for actions in ([], [lamp] * 3, [lamp, lamp], [lamp, {**lamp, "action": "off"}],
                        [{"entity_id": "light.unknown", "action": "on"}], [{**lamp, "action": "open"}],
                        [{**lamp, "action": "delete"}], [{**lamp, "entity_id": []}],
                        [{**lamp, "action": []}], [None]):
            with self.subTest(actions=actions):
                self.control({"actions": actions})
                with self.assertRaises(PlannerError):
                    self.planner.plan("开灯")

    def test_position_requires_an_integer_only_for_set_position(self):
        for position in (0, 50, 100):
            self.control({"actions": [{"entity_id": "cover.window", "action": "set_position", "position": position}]})
            self.assertEqual(self.planner.plan("调整窗帘").actions[0].position, position)
        for position in (None, True, 0.5, -1, 101, "50", []):
            with self.subTest(position=position):
                self.control({"actions": [{"entity_id": "cover.window", "action": "set_position", "position": position}]})
                with self.assertRaises(PlannerError):
                    self.planner.plan("调整窗帘")
        for action in ({"entity_id": "cover.window", "action": "set_position"},
                       {"entity_id": "light.kitchen", "action": "on", "position": None}):
            self.control({"actions": [action]})
            with self.assertRaises(PlannerError):
                self.planner.plan("调整设备")

    def test_unknown_tool_multiple_calls_and_malformed_calls_are_rejected_before_query(self):
        query = Mock(return_value=self.snapshot())
        control = self.tool("control_home", {"actions": [{"entity_id": "light.kitchen", "action": "on"}]})
        for calls in ([self.tool("shell", {})], [control, control], [self.tool("query_home", {}), control],
                      [], None, [{**control, "extra": 1}], [{**control, "id": "bad id"}],
                      [{**control, "index": True}], [{**control, "index": 1}],
                      [{**control, "type": "shell"}], [{**control, "function": {"name": "control_home"}}],
                      [{**control, "function": {**control["function"], "extra": 1}}]):
            with self.subTest(calls=calls):
                self.respond(None, finish="tool_calls", message_extra={"tool_calls": calls})
                with self.assertRaises(PlannerError):
                    self.planner.plan("开灯", query_home=query)
        query.assert_not_called()

    def test_duplicate_json_fields_and_nonfinite_tool_arguments_are_rejected(self):
        for arguments in ('{"actions":[],"actions":[]}',
                          '{"actions":[{"entity_id":"light.kitchen","entity_id":"cover.window","action":"on"}]}',
                          '{"actions":[{"entity_id":"cover.window","action":"set_position","position":NaN}]}',
                          'not json'):
            with self.subTest(arguments=arguments):
                self.control(arguments)
                with self.assertRaises(PlannerError):
                    self.planner.plan("开灯")
        self.transport.open.return_value = FakeResponse(b'{"choices":[],"choices":[]}')
        with self.assertRaises(PlannerError):
            self.planner.plan("你好")

    def test_invalid_completions_and_reply_boundaries_are_rejected(self):
        cases = [(content, "stop", None) for content in (None, 1, "", " ", "好" * 351, "bad\x00", "\ud800", "<think>secret</think>")]
        cases += [("回答", "length", None), ("回答", "stop", {"role": "user"}),
                  ("回答", "stop", {"unknown": 1}), ("回答", "stop", {"reasoning_content": []}),
                  ("回答", "stop", {"name": []}), ("回答", "stop", {"audio_content": {}}),
                  ("回答", "stop", {"tool_calls": [self.tool("query_home", {})]})]
        for content, finish, extra in cases:
            with self.subTest(content=repr(content), finish=finish, extra=extra):
                self.respond(content, finish=finish, message_extra=extra)
                with self.assertRaises(PlannerError):
                    self.planner.plan("回答")
        self.respond("好" * 350)
        self.assertEqual(self.planner.plan("回答").message, "好" * 350)

    def test_old_json_content_is_never_interpreted_as_a_control_call(self):
        content = '{"kind":"home","actions":[{"entity_id":"light.kitchen","action":"on"}]}'
        self.respond(content)
        result = self.planner.plan("解释这段JSON")
        self.assertFalse(result.actions)
        self.assertEqual(result.message, content)

    def test_prompt_expresses_semantics_and_supported_tools_without_regex_scope(self):
        self.respond("我在。")
        self.planner.plan("你好")
        prompt = self.payloads()[0]["messages"][0]["content"]
        for requirement in ("口误纠正", "咨询和明确委托", "不要执行", "无需要求用户重复许可", "客厅的全部灯",
                            "窗帘灯是灯", "仅打开1%", "null为未知", "不能据此推断通风", "不能用用户文本替代工具事实",
                            "不得伪造天气", "不重试失败动作", "真实设备回执", "不能改成立即执行"):
            self.assertIn(requirement, prompt)
        self.assertNotIn("close_scope", prompt)
        self.assertNotIn('"kind"', prompt)

    def test_transport_failure_is_sanitized_and_never_retried(self):
        self.transport.open.side_effect = OSError("test-secret-key from proxy")
        with self.assertRaises(PlannerError) as caught:
            self.planner.plan("开灯")
        self.assertNotIn("test-secret-key", str(caught.exception))
        self.transport.open.assert_called_once()

    def test_redirect_is_not_followed(self):
        self.transport.open.side_effect = HTTPError("https://api.minimaxi.com/v1/chat/completions", 302,
                                                     "redirect", {"Location": "https://evil.invalid"}, None)
        with self.assertRaises(PlannerError) as caught:
            self.planner.plan("开灯")
        self.assertEqual(str(caught.exception), "MiniMax request failed with HTTP 302")
        self.transport.open.assert_called_once()

    def test_m31_uses_low_effort_and_reasoning_split(self):
        self._write_config(model="MiniMax-M3.1-Flash")
        self.planner = MiniMaxPlanner(self.config, Gateway())
        self.planner._http = self.transport
        self.respond("好的。")
        self.planner.plan("你好")
        payload = json.loads(self.transport.open.call_args.args[0].data)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertIs(payload["reasoning_split"], True)

    def test_config_and_key_file_permissions_and_endpoint_are_enforced(self):
        self.config.chmod(0o644)
        with self.assertRaises(PlannerError):
            MiniMaxPlanner(self.config, Gateway())
        self.config.chmod(0o600)
        self._write_config(endpoint="http://api.minimaxi.com/v1/chat/completions")
        with self.assertRaises(PlannerError):
            MiniMaxPlanner(self.config, Gateway())
        self._write_config(endpoint="https://evil.invalid/v1/chat/completions")
        with self.assertRaises(PlannerError):
            MiniMaxPlanner(self.config, Gateway())
        self._write_config()
        self.key.chmod(0o644)
        with self.assertRaises(PlannerError):
            MiniMaxPlanner(self.config, Gateway())

    def test_duplicate_and_unknown_config_fields_are_rejected(self):
        self.config.write_text('{"endpoint":"https://api.minimaxi.com/v1/chat/completions",'
                               '"endpoint":"https://api.minimaxi.com/v1/chat/completions",'
                               '"model":"MiniMax-M3","api_key_file":"' + str(self.key) + '"}', encoding="utf-8")
        self.config.chmod(0o600)
        with self.assertRaises(PlannerError):
            MiniMaxPlanner(self.config, Gateway())
        self._write_config(extra="x")
        with self.assertRaises(PlannerError):
            MiniMaxPlanner(self.config, Gateway())

    def test_key_file_inside_git_worktree_is_rejected(self):
        (Path(self.directory.name) / ".git").mkdir()
        with self.assertRaises(PlannerError):
            MiniMaxPlanner(self.config, Gateway())

    def test_input_is_bounded(self):
        with self.assertRaises(PlannerError):
            self.planner.plan("x" * 4097)
        self.transport.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
