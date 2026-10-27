import argparse
from contextlib import redirect_stdout
import io
import json
import signal
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from home_jarvis.core import Core, HomeAction, MockHomeGateway, ModelPlan, PlanningContext
from home_jarvis.planner import PlannerError
from home_jarvis.speaker import SpeakerError
from home_jarvis.xiaomi_cloud import ConversationRecord
from home_jarvis.xiaomi_voice import VoiceBridge, addressed_text, main


class VoiceBridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.gateway = MockHomeGateway()
        self.gateway.default_room = "living_room"
        self.core = Core(Path(self.directory.name) / "voice.sqlite3", self.gateway)
        self.addCleanup(self.core.close)
        self.speaker = Mock()
        self.speaker.speak.return_value = self.speaker.stop.return_value = "accepted"
        self.now = 1000.0
        self.bridge = VoiceBridge(self.core, self.speaker, "111111111", clock=lambda: self.now)
        self.addCleanup(self.bridge.close)

    def record(self, identifier="1", text="打开客厅灯", timestamp=1001000):
        return ConversationRecord("111111111:" + identifier, timestamp, text)

    def ready(self):
        self.bridge.baseline(())
        self.now = 1002.0

    def use_planner(self, planner):
        self.bridge.close()
        self.core.planner = planner
        self.bridge = VoiceBridge(self.core, self.speaker, "111111111", clock=lambda: self.now)
        self.addCleanup(self.bridge.close)

    def drain_until(self, predicate):
        output = []
        for _ in range(200):
            output.extend(self.bridge.consume(()))
            if predicate(output):
                return output
            time.sleep(0.005)
        self.fail("asynchronous model result did not complete")

    def test_plain_commands_and_legacy_prefixes_are_accepted(self):
        self.assertEqual(addressed_text("打开客厅灯"), "打开客厅灯")
        self.assertEqual(addressed_text("让贾维斯打开客厅灯"), "让贾维斯打开客厅灯")
        self.assertEqual(addressed_text("不要贾维斯打开客厅灯"), "不要贾维斯打开客厅灯")
        self.assertEqual(addressed_text("小爱同学，打开客厅灯"), "小爱同学，打开客厅灯")
        self.assertEqual(addressed_text("让小爱同学打开客厅灯"), "让小爱同学打开客厅灯")
        for standalone in ["", "   ", "贾维斯", "贾维斯，", "Jarvis", "Jarvis:"]:
            self.assertIsNone(addressed_text(standalone))
        self.assertEqual(addressed_text(" 贾维斯，打开客厅灯。"), "打开客厅灯。")
        self.assertEqual(addressed_text("JARVIS: 打开客厅灯"), "打开客厅灯")
        self.assertEqual(addressed_text("贾维斯不要打开客厅灯"), "不要打开客厅灯")

    def test_requires_successful_baseline(self):
        with self.assertRaises(ValueError):
            self.bridge.consume((self.record(),))
        self.speaker.stop.assert_not_called()

    def test_baseline_and_restart_do_not_execute_history(self):
        old = self.record(timestamp=999000)
        self.bridge.baseline((old,))
        self.now = 1002.0
        self.assertEqual(self.bridge.consume((old,)), [])
        new_bridge = VoiceBridge(self.core, self.speaker, "111111111", clock=lambda: self.now)
        downtime = self.record("downtime", timestamp=1001000)
        new_bridge.baseline((downtime,))
        self.assertEqual(new_bridge.consume((downtime, old)), [])
        self.assertEqual(self.core.mock_home_effects(), ())
        self.speaker.speak.assert_not_called()

    def test_live_final_routes_to_core_and_lan_output_once(self):
        self.ready()
        result = self.bridge.consume((self.record(),))[0]
        self.assertEqual(result["results"][0]["status"], "mock_applied")
        self.assertEqual(result["speech"]["status"], "accepted")
        self.assertEqual(self.bridge.consume((self.record(),)), [])
        self.assertEqual(len(self.core.mock_home_effects()), 1)
        self.speaker.stop.assert_called_once()
        self.speaker.speak.assert_called_once_with("MOCK light.living_room: on")
        row = self.core.db.execute("SELECT primary_source FROM sessions").fetchone()
        self.assertEqual(row[0], "xiaomi-cloud")

    def test_room_curtain_group_executes_both_layers_and_speaks_once(self):
        self.gateway.resolve = Mock(return_value=(
            HomeAction("living_room", "cover.sheer", "close"),
            HomeAction("living_room", "cover.fabric", "close"),
        ))
        self.gateway.authorize = Mock()
        self.ready()
        record = self.record(text="关闭客厅的窗帘")
        result = self.bridge.consume((record,))[0]
        self.assertEqual([item["target_id"] for item in result["results"]],
                         ["cover.sheer", "cover.fabric"])
        self.assertEqual(len(self.core.mock_home_effects()), 2)
        self.assertEqual(result["model_calls"], 0)
        self.speaker.speak.assert_called_once_with("MOCK cover.sheer: close。MOCK cover.fabric: close")
        self.assertEqual(self.bridge.consume((record,)), [])
        self.assertEqual(len(self.core.mock_home_effects()), 2)
        self.assertEqual(self.speaker.speak.call_count, 1)

    def test_native_reply_is_paused_before_home_action(self):
        self.ready()
        order = []
        submit = self.core.submit

        def observed_submit(event):
            order.append("home")
            return submit(event)

        self.speaker.stop.side_effect = lambda: order.append("pause") or "accepted"
        self.speaker.speak.side_effect = lambda text: order.append("speak") or "accepted"
        with patch.object(self.core, "submit", side_effect=observed_submit):
            self.bridge.consume((self.record(),))
        self.assertEqual(order, ["pause", "home", "speak"])

    def test_revision_is_not_new_command_but_same_text_new_id_is(self):
        self.ready()
        self.bridge.consume((self.record(),))
        revised = self.record(text="贾维斯，关闭客厅灯", timestamp=1001100)
        self.bridge.consume((revised,))
        self.bridge.consume((self.record("second", timestamp=1001200),))
        self.assertEqual(len(self.core.mock_home_effects()), 2)
        self.assertEqual([row["action"] for row in self.core.mock_home_effects()], ["on", "on"])

    def test_ordinary_query_is_clarified_and_negation_does_not_control_home(self):
        self.ready()
        ordinary = self.bridge.consume((self.record(text="现在几点了"),))[0]
        self.assertEqual(ordinary["results"][0]["intent"], "clarify")
        self.assertEqual(ordinary["results"][0]["status"], "clarification_required")
        self.assertEqual(self.core.mock_home_effects(), ())
        self.bridge.consume((self.record("negative", "不要打开客厅灯", 1001100),))
        self.assertEqual(self.core.mock_home_effects(), ())
        self.assertEqual(self.speaker.speak.call_count, 2)

    def test_stale_future_and_late_records_are_not_executed(self):
        self.ready()
        self.bridge.consume((self.record("newer", timestamp=1001500),))
        self.bridge.consume((self.record("late", timestamp=1001400), self.record("future", timestamp=1100000)))
        self.now = 1100.0
        self.bridge.consume((self.record("expired", timestamp=1003000),))
        self.assertEqual(len(self.core.mock_home_effects()), 1)

    def test_same_timestamp_distinct_records_and_sorting(self):
        self.ready()
        self.bridge.consume((self.record("b", "关闭客厅灯", 1001100), self.record("a")))
        self.bridge.consume((self.record("c", timestamp=1001100),))
        self.assertEqual([row["action"] for row in self.core.mock_home_effects()], ["on", "off", "on"])

    def test_slow_first_action_does_not_execute_an_expired_second_record(self):
        self.ready()
        submit = self.core.submit

        def slow_submit(event):
            receipt = submit(event)
            self.now += 61
            return receipt

        with patch.object(self.core, "submit", side_effect=slow_submit):
            self.bridge.consume((self.record(), self.record("second", "贾维斯关闭客厅灯", 1001100)))
        self.assertEqual([row["action"] for row in self.core.mock_home_effects()], ["on"])

    def test_stop_does_not_start_tts_or_pause_twice(self):
        self.ready()
        self.bridge.consume((self.record(text="贾维斯别说了"),))
        self.bridge.consume((self.record(text="贾维斯别说了"),))
        self.speaker.stop.assert_called_once()
        self.speaker.speak.assert_not_called()

    def test_audio_failure_preserves_home_receipt(self):
        self.ready()
        self.speaker.stop.side_effect = SpeakerError("uncertain")
        self.speaker.speak.side_effect = SpeakerError("uncertain")
        result = self.bridge.consume((self.record(),))[0]
        self.assertEqual(result["results"][0]["status"], "mock_applied")
        self.assertEqual(result["speech"]["status"], "uncertain")
        self.bridge.consume((self.record(),))
        self.speaker.speak.assert_called_once()

    def test_interrupted_submission_is_not_replayed(self):
        self.ready()
        with patch.object(self.core, "submit", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.bridge.consume((self.record(),))
        self.assertEqual(self.bridge.consume((self.record(),)), [])
        self.speaker.speak.assert_not_called()

    def test_recovery_from_cloud_failure_rebaselines(self):
        self.ready()
        self.bridge.ready = False
        self.now = 1003.0
        self.bridge.baseline((self.record(),))
        self.assertEqual(self.bridge.consume((self.record(),)), [])
        self.assertEqual(self.core.mock_home_effects(), ())

    def test_model_reply_completes_once_on_main_thread(self):
        planner = Mock(model="MiniMax-M3", plan=Mock(return_value=ModelPlan("请说清楚要控制哪盏灯。")))
        self.use_planner(planner)
        self.ready()
        self.assertEqual(self.bridge.consume((self.record(text="你能帮我看看吗"),)), [])
        outputs = self.drain_until(lambda items: bool(items))
        self.assertEqual(outputs[0]["results"][0]["status"], "answered")
        self.assertEqual(outputs[0]["speech"]["status"], "accepted")
        planner.plan.assert_called_once_with("你能帮我看看吗")
        self.speaker.speak.assert_called_once_with("请说清楚要控制哪盏灯")
        self.assertEqual(self.bridge.consume((self.record(text="你能帮我看看吗"),)), [])
        self.speaker.speak.assert_called_once()

    def test_contextual_advice_reaches_model_and_speaker_without_home_actions(self):
        context = PlanningContext(time.time(), "2026-10-27T22:00:00+08:00", "Asia/Shanghai",
                                  "living_room", sun_state="below_horizon")
        self.gateway.query_home = Mock(return_value=context)
        observed = []
        def answer(_text, *, query_home):
            observed.append(query_home())
            return ModelPlan("已日落，建议关闭客厅布帘保护隐私。", model_calls=2)
        planner = Mock(model="MiniMax-M3", plan=Mock(side_effect=answer))
        self.use_planner(planner)
        self.ready()
        text = "根据现在的时间判断应该把哪个窗帘关上"
        self.bridge.consume((self.record(text=text),))
        outputs = self.drain_until(lambda items: bool(items))
        self.assertEqual(outputs[0]["results"][0]["status"], "answered")
        planner.plan.assert_called_once()
        self.assertEqual(planner.plan.call_args.args, (text,))
        self.assertEqual(observed, [context])
        self.assertEqual(outputs[0]["model_calls"], 2)
        self.speaker.speak.assert_called_once_with("已日落，建议关闭客厅布帘保护隐私")
        self.assertFalse(self.core.mock_home_effects())
        self.assertEqual(self.bridge.consume((self.record(text=text),)), [])
        self.speaker.speak.assert_called_once()

    def test_exact_home_command_is_understood_by_model_without_resolver(self):
        action = HomeAction("living_room", "light.living_room", "off")
        planner = Mock(model="MiniMax-M3", plan=Mock(return_value=ModelPlan("", (action,))))
        self.use_planner(planner)
        self.gateway.resolve = Mock(side_effect=AssertionError("voice must not use the text resolver"))
        self.ready()
        self.bridge.consume((self.record(text="关闭客厅灯"),))
        outputs = self.drain_until(lambda items: bool(items))
        planner.plan.assert_called_once_with("关闭客厅灯")
        self.assertEqual(outputs[0]["results"][0]["status"], "mock_applied")
        self.gateway.resolve.assert_not_called()

    def test_stop_cancels_slow_model_and_main_thread_stays_responsive(self):
        started, release = Event(), Event()

        class SlowPlanner:
            model = "MiniMax-M3"

            def plan(self, _text):
                started.set()
                release.wait(2)
                return ModelPlan("旧提案", (HomeAction("living_room", "light.living_room", "on"),))

        self.use_planner(SlowPlanner())
        self.ready()
        self.assertEqual(self.bridge.consume((self.record(text="请帮我打开客厅灯"),)), [])
        self.assertTrue(started.wait(1))

        stop_output = self.bridge.consume((self.record("stop", "别说了", 1001100),))
        self.assertTrue(any(item["results"][0]["intent"] == "stop_speech" for item in stop_output))
        self.assertFalse(release.is_set(), "stop handling must not wait for the model")
        release.set()
        self.drain_until(lambda _items: not self.bridge._model_jobs)
        self.assertEqual(self.core.mock_home_effects(), ())
        self.speaker.speak.assert_not_called()
        self.assertEqual(self.speaker.stop.call_count, 2)

    def test_baseline_duplicate_and_standalone_prefix_do_not_call_model(self):
        planner = Mock(model="MiniMax-M3", plan=Mock(return_value=ModelPlan("答复。")))
        self.use_planner(planner)
        old = self.record("old", "旧历史问题", 999000)
        self.bridge.baseline((old,))
        self.now = 1002.0
        self.assertEqual(self.bridge.consume((old, self.record("prefix", "贾维斯", 1001000))), [])
        self.assertEqual(self.bridge.consume((self.record("one", "你能帮我看看吗", 1001100),)), [])
        self.drain_until(lambda _items: not self.bridge._model_jobs)
        self.assertEqual(self.bridge.consume((self.record("one", "你能帮我看看吗", 1001100),)), [])
        planner.plan.assert_called_once()

    def test_rebaseline_cancels_inflight_model_proposal(self):
        started, release = Event(), Event()

        class SlowPlanner:
            model = "MiniMax-M3"

            def plan(self, _text):
                started.set()
                release.wait(2)
                return ModelPlan("迟到回复", (HomeAction("living_room", "light.living_room", "on"),))

        self.use_planner(SlowPlanner())
        self.ready()
        self.bridge.consume((self.record(text="请帮我打开客厅灯"),))
        self.assertTrue(started.wait(1))
        try:
            self.bridge.baseline(())
            release.set()
            self.drain_until(lambda _items: not self.bridge._model_jobs)
        finally:
            release.set()
        self.assertEqual(self.core.mock_home_effects(), ())
        self.speaker.speak.assert_not_called()

    def test_model_result_expiring_after_queue_or_request_is_cancelled(self):
        started, release = Event(), Event()

        class SlowPlanner:
            model = "MiniMax-M3"

            def plan(self, _text):
                started.set()
                release.wait(2)
                return ModelPlan("过期提案", (HomeAction("living_room", "light.living_room", "on"),))

        self.use_planner(SlowPlanner())
        self.ready()
        self.bridge.consume((self.record(text="请帮我打开客厅灯"),))
        self.assertTrue(started.wait(1))
        try:
            self.now = 1062.0
            future = self.bridge._model_jobs[0][0]
            release.set()
            future.result(timeout=1)
            self.assertEqual(self.bridge.consume(()), [])
            self.drain_until(lambda _items: not self.bridge._model_jobs)
        finally:
            release.set()
        self.assertEqual(self.core.mock_home_effects(), ())
        self.speaker.speak.assert_not_called()

    def test_expired_queued_proposal_is_skipped_before_planning(self):
        started, release = Event(), Event()

        class SlowFirstPlanner:
            model = "MiniMax-M3"

            def __init__(self):
                self.calls = 0

            def plan(self, _text):
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    release.wait(2)
                return ModelPlan("迟到的动作", (HomeAction("living_room", "light.living_room", "on"),))

        planner = SlowFirstPlanner()
        self.use_planner(planner)
        self.ready()
        self.bridge.consume((self.record("first", "请帮我打开客厅灯"),))
        self.assertTrue(started.wait(1))
        self.bridge.consume((self.record("second", "请帮我打开客厅灯", 1001100),))
        self.now = 1062.0
        try:
            release.set()
            self.drain_until(lambda _items: not self.bridge._model_jobs)
        finally:
            release.set()
        self.assertEqual(planner.calls, 1)
        self.assertEqual(self.core.mock_home_effects(), ())
        self.speaker.speak.assert_not_called()

    def test_close_cancels_pending_claim_and_suppresses_late_speech(self):
        started, release = Event(), Event()

        class SlowPlanner:
            model = "MiniMax-M3"

            def plan(self, _text):
                started.set()
                release.wait(2)
                return ModelPlan("不应播报的旧答案。")

        self.use_planner(SlowPlanner())
        self.ready()
        self.bridge.consume((self.record(text="你能帮我看看吗"),))
        self.assertTrue(started.wait(1))
        try:
            self.bridge.close()
            release.set()
            self.drain_until(lambda _items: not self.bridge._model_jobs)
        finally:
            release.set()
        self.assertEqual(self.core.mock_home_effects(), ())
        self.speaker.speak.assert_not_called()

    def test_expiry_is_rechecked_after_waiting_for_final_sqlite_lock(self):
        started, release, routed = Event(), Event(), Event()

        class SlowPlanner:
            model = "MiniMax-M3"

            def plan(self, _text):
                started.set()
                release.wait(2)
                return ModelPlan("打开客厅灯", (HomeAction("living_room", "light.living_room", "on"),))

        self.use_planner(SlowPlanner())
        self.ready()
        self.bridge.consume((self.record(text="请帮我打开客厅灯"),))
        self.assertTrue(started.wait(1))
        lock_db = sqlite3.connect(self.core.db_path, isolation_level=None, timeout=2)
        try:
            lock_db.execute("BEGIN IMMEDIATE")
            original = Core._model_routes

            def observed_routes(plan, text):
                routes = original(plan, text)
                routed.set()
                return routes

            with patch.object(Core, "_model_routes", staticmethod(observed_routes)):
                release.set()
                self.assertTrue(routed.wait(1))
                self.now = 1062.0
                lock_db.execute("ROLLBACK")
                future = self.bridge._model_jobs[0][0]
                future.result(timeout=2)
        finally:
            release.set()
            if lock_db.in_transaction:
                lock_db.execute("ROLLBACK")
            lock_db.close()
        self.assertEqual(self.bridge.consume(()), [])
        self.assertEqual(self.core.mock_home_effects(), ())
        self.speaker.speak.assert_not_called()

    def test_model_errors_log_only_sanitized_planner_diagnostics(self):
        from home_jarvis.xiaomi_voice import _CancellablePlanner
        for error, reason in ((PlannerError("MiniMax response has an unsupported completion shape"),
                               "MiniMax response has an unsupported completion shape"),
                              (ValueError("private token must not be logged"), "ValueError")):
            output = io.StringIO()
            planner = _CancellablePlanner(Mock(plan=Mock(side_effect=error)), Event(), 2000, lambda: 1)
            with redirect_stdout(output), self.assertRaises(type(error)):
                planner.plan("关闭客厅的灯")
            diagnostic = json.loads(output.getvalue())
            self.assertEqual((diagnostic["status"], diagnostic["reason"]), ("model_error", reason))
            self.assertNotIn("private", output.getvalue())

    def test_planner_startup_error_is_reported_without_traceback_or_secret(self):
        args = SimpleNamespace(cloud_config="cloud", speaker_config="speaker", home_config=None,
                               db=str(Path(self.directory.name) / "startup.sqlite3"),
                               model_config="model", quiet_native_replies=False, reply_volume=16)
        cloud = Mock(miot_did="111111111")
        speaker = Mock(miot_did="111111111")
        output = io.StringIO()
        with patch.object(argparse.ArgumentParser, "parse_args", return_value=args), \
                patch("home_jarvis.xiaomi_voice.XiaomiCloud", return_value=cloud), \
                patch("home_jarvis.xiaomi_voice.XiaomiSpeaker", return_value=speaker), \
                patch("home_jarvis.xiaomi_voice.MiniMaxPlanner",
                      side_effect=PlannerError("private config rejected")), \
                redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_context:
                main()
        self.assertEqual(exit_context.exception.code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["reason"], "private config rejected")
        self.assertNotIn("Traceback", output.getvalue())

    def test_quiet_mode_mutes_on_cloud_failure_and_restores_on_exit(self):
        from home_jarvis.xiaomi_cloud import CloudError
        args = SimpleNamespace(cloud_config="cloud", speaker_config="speaker", home_config=None,
                               db=str(Path(self.directory.name) / "quiet.sqlite3"),
                               model_config=None, quiet_native_replies=True, reply_volume=16)
        cloud = Mock(miot_did="111111111")
        cloud.latest_records.side_effect = [(), CloudError("Xiaomi cloud request was rejected")]
        speaker = Mock(miot_did="111111111")
        quiet = Mock(miot_did="111111111", last_error=None)
        output = io.StringIO()
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(argparse.ArgumentParser, "parse_args", return_value=args), \
                patch("home_jarvis.xiaomi_voice.XiaomiCloud", return_value=cloud), \
                patch("home_jarvis.xiaomi_voice.XiaomiSpeaker", return_value=speaker), \
                patch("home_jarvis.xiaomi_voice.configured_gateway", return_value=self.gateway), \
                patch("home_jarvis.xiaomi_voice.MiniMaxPlanner", return_value=Mock(model="MiniMax-M3")), \
                patch("home_jarvis.quiet_speaker.QuietSpeaker", return_value=quiet) as factory, \
                patch("home_jarvis.xiaomi_voice.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleep, \
                redirect_stdout(output):
            main()
        factory.assert_called_once_with(speaker, "cloud", reply_volume=16)
        quiet.stop.assert_called_once()
        quiet.close.assert_called_once()
        self.assertEqual(sleep.call_args_list[0].args, (3,))
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        ready = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(ready["audio_mode"], "quiet_native_replies")
        self.assertEqual(ready["reply_volume"], 16)

    def test_sigterm_restores_quiet_speaker_and_previous_signal_handler(self):
        args = SimpleNamespace(cloud_config="cloud", speaker_config="speaker", home_config=None,
                               db=str(Path(self.directory.name) / "signal.sqlite3"),
                               model_config=None, quiet_native_replies=True, reply_volume=16)
        cloud = Mock(miot_did="111111111")
        cloud.latest_records.side_effect = lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        speaker = Mock(miot_did="111111111")
        quiet = Mock(miot_did="111111111", last_error=None)
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(argparse.ArgumentParser, "parse_args", return_value=args), \
                patch("home_jarvis.xiaomi_voice.XiaomiCloud", return_value=cloud), \
                patch("home_jarvis.xiaomi_voice.XiaomiSpeaker", return_value=speaker), \
                patch("home_jarvis.xiaomi_voice.configured_gateway", return_value=self.gateway), \
                patch("home_jarvis.xiaomi_voice.MiniMaxPlanner", return_value=Mock(model="MiniMax-M3")), \
                patch("home_jarvis.quiet_speaker.QuietSpeaker", return_value=quiet):
            main()
        quiet.close.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


if __name__ == "__main__":
    unittest.main()
