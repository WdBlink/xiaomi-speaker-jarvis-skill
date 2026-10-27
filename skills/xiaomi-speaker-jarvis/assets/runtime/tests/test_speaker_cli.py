from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from home_jarvis.__main__ import main
from home_jarvis.speech import deliver_speech
from home_jarvis.core import Core, MockHomeGateway
from home_jarvis.speaker import SpeakerError


class SpeakerCLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "jarvis.sqlite3"
        Core(self.db, MockHomeGateway()).close()

    def run_cli(self, text, speaker):
        output = io.StringIO()
        with patch("sys.argv", ["home-jarvis", text, "--mock", "--db", str(self.db),
                                "--speaker-config", "private.json", "--utterance-id", "test-utterance"]), \
                patch("home_jarvis.__main__.XiaomiSpeaker", return_value=speaker), redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def test_clarification_is_spoken_without_claiming_playback_complete(self):
        speaker = Mock()
        speaker.speak.return_value = "accepted"
        result = self.run_cli("测试语音输出", speaker)
        speaker.speak.assert_called_once_with("请明确房间、设备名称和支持的动作，未执行动作")
        self.assertEqual(result["speech"], {"status": "accepted", "operation": "tts"})
        self.assertEqual(result["results"][0]["status"], "clarification_required")

    def test_speaker_failure_preserves_the_home_action_result(self):
        speaker = Mock()
        speaker.speak.side_effect = SpeakerError("播报结果未确认")
        result = self.run_cli("打开客厅灯", speaker)
        self.assertEqual(result["results"][0]["status"], "mock_applied")
        self.assertEqual(result["speech"]["status"], "uncertain")
        speaker.speak.assert_called_once()
        core = Core(self.db, MockHomeGateway())
        try:
            self.assertEqual(len(core.mock_home_effects()), 1)
        finally:
            core.close()

    def test_stop_sends_pause_and_does_not_start_another_tts(self):
        speaker = Mock()
        speaker.stop.return_value = "accepted"
        result = self.run_cli("别说了", speaker)
        speaker.stop.assert_called_once_with()
        speaker.speak.assert_not_called()
        self.assertEqual(result["results"][0]["status"], "stop_requested")
        self.assertIn("尚未确认", result["results"][0]["message"])

    def test_failed_stop_is_not_reported_as_stopped(self):
        speaker = Mock()
        speaker.stop.side_effect = SpeakerError("暂停结果未确认")
        result = self.run_cli("停止播报", speaker)
        self.assertEqual(result["results"][0]["status"], "uncertain")
        speaker.speak.assert_not_called()

    def test_bad_speaker_configuration_prevents_home_side_effects(self):
        with patch("sys.argv", ["home-jarvis", "打开客厅灯", "--speaker-config", "private.json"]), \
                patch("home_jarvis.__main__.XiaomiSpeaker", side_effect=SpeakerError("配置无效")), \
                patch("home_jarvis.__main__.Core") as core, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main()
        core.assert_not_called()

    def test_demo_cannot_be_given_a_real_speaker(self):
        with patch("sys.argv", ["home-jarvis", "--speaker-config", "private.json"]), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main()

    def test_speaker_rejects_nonpersistent_database_before_sending(self):
        with patch("sys.argv", ["home-jarvis", "测试", "--mock", "--db", ":memory:",
                                "--speaker-config", "private.json"]), \
                patch("home_jarvis.__main__.XiaomiSpeaker") as speaker, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main()
        speaker.assert_not_called()

    def test_inflight_home_receipt_does_not_claim_final_speech(self):
        receipt = {"session_id": "s", "utterance_id": "u", "results": [{"intent": "home", "message": "临时回执"}]}
        speaker = Mock()
        speaker.speak.return_value = "accepted"
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO home_outbox VALUES ('effect', 's', 'u', 0, 'resident', 'room', '{}', 'in_flight', '进行中')")
        result = deliver_speech(self.db, speaker, receipt)
        self.assertEqual(result["status"], "deferred")
        speaker.speak.assert_not_called()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cli_speech").fetchone()[0], 0)
            db.execute("UPDATE home_outbox SET state='applied' WHERE effect_id='effect'")
        receipt["results"][0]["message"] = "完成回执"
        self.assertEqual(deliver_speech(self.db, speaker, receipt)["status"], "accepted")
        speaker.speak.assert_called_once_with("完成回执")

    def test_same_utterance_does_not_repeat_tts_or_home_action(self):
        speaker = Mock()
        speaker.speak.return_value = "accepted"
        self.run_cli("打开客厅灯", speaker)
        result = self.run_cli("打开客厅灯", speaker)
        speaker.speak.assert_called_once()
        self.assertTrue(result["speech"]["replayed"])
        core = Core(self.db, MockHomeGateway())
        try:
            self.assertEqual(len(core.mock_home_effects()), 1)
        finally:
            core.close()

    def test_pending_model_does_not_reserve_the_final_speech(self):
        pending = {"session_id": "s", "utterance_id": "u", "status": "model_pending", "results": []}
        final = {**pending, "status": "committed",
                 "results": [{"intent": "reply", "message": "四。"}]}
        speaker = Mock()
        speaker.speak.return_value = "accepted"
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO submissions VALUES (?, ?, ?)", ("s", "u", json.dumps(pending)))
        self.assertEqual(deliver_speech(self.db, speaker, final)["status"], "deferred")
        speaker.speak.assert_not_called()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cli_speech").fetchone()[0], 0)
            db.execute("UPDATE submissions SET receipt=?", (json.dumps(final),))
        self.assertEqual(deliver_speech(self.db, speaker, pending)["status"], "accepted")
        speaker.speak.assert_called_once_with("四")

    def test_cancelled_model_or_oversized_reply_does_not_reserve_speech(self):
        speaker = Mock()
        cancelled = {"session_id": "s", "utterance_id": "u", "status": "model_cancelled", "results": []}
        self.assertEqual(deliver_speech(self.db, speaker, cancelled)["status"], "suppressed")
        oversized = {**cancelled, "status": "committed", "results": [{"intent": "reply", "message": "字" * 501}]}
        self.assertEqual(deliver_speech(self.db, speaker, oversized)["status"], "rejected")
        speaker.speak.assert_not_called()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cli_speech").fetchone()[0], 0)

    def test_stale_caller_receipt_cannot_replace_persisted_final_speech(self):
        stale = {"session_id": "s", "utterance_id": "u", "results": [{"intent": "home", "message": "旧的执行中回执"}]}
        final = {**stale, "results": [{"intent": "home", "message": "最终完成回执"}]}
        speaker = Mock()
        speaker.speak.return_value = "accepted"
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO submissions VALUES (?, ?, ?)", ("s", "u", json.dumps(final)))
            db.execute("INSERT INTO home_outbox VALUES ('effect', 's', 'u', 0, 'resident', 'room', '{}', 'applied', '已完成')")
        self.assertEqual(deliver_speech(self.db, speaker, stale)["status"], "accepted")
        self.assertTrue(deliver_speech(self.db, speaker, final)["replayed"])
        speaker.speak.assert_called_once_with("最终完成回执")

    def test_large_home_batch_summarizes_persisted_outcomes_without_repeating_speech(self):
        cases = (
            (["applied"] * 31, "本次31个家居动作，31个已确认完成。"),
            (["applied"] * 23 + ["failed"] * 4 + ["uncertain"] * 4,
             "本次31个家居动作，23个已确认完成，4个失败，4个结果不确定。不会自动重试。"),
            (["failed"] * 31, "本次31个家居动作，31个失败。不会自动重试。"),
            (["uncertain"] * 31, "本次31个家居动作，31个结果不确定。不会自动重试。"),
        )
        messages = {"applied": "HA 已确认客厅灯的目标状态，实测设备已经完成动作",
                    "failed": "HA 状态检查失败，未发送该设备的控制请求",
                    "uncertain": "HA 已接收控制请求，但尚未确认目标状态，不会自动重放"}
        for index, (statuses, expected) in enumerate(cases):
            with self.subTest(statuses=statuses):
                receipt = {"session_id": "s", "utterance_id": str(index), "status": "committed",
                           "results": [{"intent": "home", "status": status, "target_id": f"light.lamp_{i}",
                                        "message": messages[status]} for i, status in enumerate(statuses)]}
                saved = json.dumps(receipt)
                with sqlite3.connect(self.db) as db:
                    db.execute("INSERT INTO submissions VALUES (?, ?, ?)", ("s", str(index), saved))
                stale = {**receipt, "results": [{"intent": "home", "status": "applied", "message": "错误的全部成功"}]}
                speaker = Mock()
                speaker.speak.return_value = "accepted"
                result = deliver_speech(self.db, speaker, stale)
                self.assertEqual(result, {"status": "accepted", "operation": "tts"})
                self.assertTrue(deliver_speech(self.db, speaker, receipt)["replayed"])
                speaker.speak.assert_called_once_with(expected)
                with sqlite3.connect(self.db) as db:
                    self.assertEqual(db.execute("SELECT receipt FROM submissions WHERE utterance_id=?",
                                                (str(index),)).fetchone()[0], saved)

    def test_short_home_batch_keeps_detailed_outcome_messages(self):
        receipt = {"session_id": "s", "utterance_id": "u", "results": [
            {"intent": "home", "status": "applied", "message": "客厅灯已确认关闭。"},
            {"intent": "home", "status": "uncertain", "message": "卧室灯状态尚未确认。"}]}
        speaker = Mock()
        speaker.speak.return_value = "accepted"
        self.assertEqual(deliver_speech(self.db, speaker, receipt)["status"], "accepted")
        speaker.speak.assert_called_once_with("客厅灯已确认关闭。卧室灯状态尚未确认")

    def test_same_stop_utterance_does_not_repeat_pause(self):
        speaker = Mock()
        speaker.stop.return_value = "accepted"
        self.run_cli("别说了", speaker)
        self.run_cli("别说了", speaker)
        speaker.stop.assert_called_once()

    def test_concurrent_delivery_has_only_one_sender(self):
        receipt = {"session_id": "s", "utterance_id": "u", "results": [{"intent": "clarify", "message": "测试"}]}
        speaker = Mock()
        speaker.speak.return_value = "accepted"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: deliver_speech(self.db, speaker, receipt), range(2)))
        speaker.speak.assert_called_once_with("测试")
        self.assertEqual(sum(bool(item.get("replayed")) for item in results), 1)

    def test_interruption_after_claim_does_not_resend(self):
        receipt = {"session_id": "s", "utterance_id": "u", "results": [{"intent": "clarify", "message": "测试"}]}
        speaker = Mock()
        speaker.speak.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            deliver_speech(self.db, speaker, receipt)
        result = deliver_speech(self.db, speaker, receipt)
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(result["replayed"])
        speaker.speak.assert_called_once()


if __name__ == "__main__":
    unittest.main()
