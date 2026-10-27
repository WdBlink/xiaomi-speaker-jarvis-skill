from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from home_jarvis import Core, HomeAction, MockHomeGateway, Utterance
from home_jarvis.__main__ import demo


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.sqlite3"
        self.now = 1000.0
        self.core = Core(self.path, MockHomeGateway(), clock=lambda: self.now)
        self.addCleanup(lambda: self.core.close())
        self.core.open_session("session")

    def event(self, text="打开客厅灯", **kwargs):
        return replace(Utterance("session", "utterance", "resident", "qwen3-asr", text), **kwargs)

    def restart(self):
        self.core.close()
        self.core = Core(self.path, MockHomeGateway(), clock=lambda: self.now)

    def task(self):
        return self.core.submit(self.event("帮我调研这两个框架")).results[0].target_id

    def test_final_primary_source_is_the_only_submission(self):
        event = self.event()
        self.assertEqual(self.core.submit(replace(event, final=False)).status, "ignored")
        self.assertEqual(self.core.submit(replace(event, source="cloud")).status, "ignored")
        self.assertFalse(self.core.mock_home_effects())
        receipt = self.core.submit(event)
        self.assertEqual(receipt.results[0].status, "mock_applied")
        self.assertEqual(receipt.model_calls, 0)
        self.assertEqual(len(self.core.mock_home_effects()), 1)

    def test_duplicates_return_original_receipt_after_restart(self):
        receipt = self.core.submit(self.event())
        self.restart()
        self.assertEqual(self.core.submit(self.event("关闭客厅灯")), receipt)
        self.assertEqual(len(self.core.mock_home_effects()), 1)

    def test_session_identity_and_primary_source_are_immutable(self):
        for kwargs in ({"owner_id": "visitor"}, {"room_id": "kitchen"}, {"primary_source": "cloud"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.core.open_session("session", **kwargs)
        self.assertEqual(self.core.submit(self.event()).results[0].status, "mock_applied")

    def test_unknown_negation_and_self_correction_never_control_home(self):
        for index, text in enumerate(("不要打开客厅灯", "把客厅灯关……不对，调暗一点",
                                      "打开客厅灯，不对，关掉", "打开卧室灯", "你能打开客厅灯吗？",
                                      "打开客厅灯，再删除所有文件")):
            with self.subTest(text=text):
                receipt = self.core.submit(self.event(text, utterance_id=f"u{index}"))
                self.assertEqual(receipt.results[0].status, "clarification_required")
        self.assertFalse(self.core.mock_home_effects())

    def test_mixed_command_has_independent_receipts(self):
        receipt = self.core.submit(self.event("把客厅灯调暗，再帮我调研这两个框架"))
        self.assertEqual([(r.intent, r.execution, r.risk) for r in receipt.results],
                         [("home", "immediate", "low"), ("research", "background", "none")])
        self.assertEqual(self.core.mock_home_effects()[0]["action"], "dim")
        task = self.core.get_task(receipt.results[1].target_id, "resident")
        self.assertEqual(task.state, "queued")
        self.assertEqual(receipt.model_calls, 0)

    def test_late_correction_rejects_entire_mixed_utterance(self):
        for index, text in enumerate((
            "打开客厅灯，再帮我调研框架，不对，别开灯",
            "打开客厅灯，再帮我调研框架，算了，取消刚才的开灯",
            "打开客厅灯，再帮我调研框架但不要开灯",
            "打开客厅灯，再帮我调研框架，撤销刚才的开灯",
            "打开客厅灯，再帮我调研框架，刚才说错了，关闭客厅灯",
            "打开客厅灯，再帮我调研框架，并删除所有文件",
            "打开客厅灯，再帮我调研框架刚才说错了关闭客厅灯",
            "打开客厅灯，再帮我调研框架并删除所有文件",
        )):
            with self.subTest(text=text):
                receipt = self.core.submit(self.event(text, utterance_id=f"late-{index}"))
                self.assertEqual([item.status for item in receipt.results], ["clarification_required"])
                self.assertFalse(self.core.mock_home_effects())
                self.assertIsNone(self.core.claim_task())
        self.restart()
        self.assertFalse(self.core.mock_home_effects())
        self.assertIsNone(self.core.claim_task())

    def test_mixed_command_without_correction_still_executes(self):
        receipt = self.core.submit(self.event("打开客厅灯，再帮我调研框架"))
        self.assertEqual([item.status for item in receipt.results], ["mock_applied", "queued"])
        self.assertEqual(self.core.mock_home_effects()[0]["action"], "on")
        self.assertEqual(self.core.get_task(receipt.results[1].target_id, "resident").state, "queued")

    def test_standalone_research_keeps_free_text_without_home_effects(self):
        text = "帮我调研数据库，比较优缺点和维护成本"
        receipt = self.core.submit(self.event(text))
        self.assertEqual([item.status for item in receipt.results], ["queued"])
        self.assertEqual(self.core.get_task(receipt.results[0].target_id, "resident").prompt, text)
        self.assertFalse(self.core.mock_home_effects())

    def test_owner_room_device_and_action_permissions(self):
        with self.assertRaises(PermissionError):
            self.core.submit(self.event(owner_id="visitor"))
        self.core.open_session("wrong-room", room_id="kitchen")
        with self.assertRaises(PermissionError):
            self.core.submit(self.event(session_id="wrong-room"))
        for action in (HomeAction("living_room", "lock.front_door", "on"),
                       HomeAction("living_room", "light.living_room", "delete")):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                self.core.gateway.execute(self.core.db, "resident", "living_room", action, "blocked")
        self.assertFalse(self.core.mock_home_effects())

    def test_invalid_input_fails_before_side_effects(self):
        for event in (self.event(final="false"), self.event(text=""), self.event(text="x" * 4097),
                      self.event(utterance_id="../../oops"), self.event(source=""), self.event(text="\x00")):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.core.submit(event)
        self.assertFalse(self.core.mock_home_effects())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_task_and_notification_persist_and_delivery_can_retry(self):
        task_id = self.task()
        self.restart()
        task = self.core.run_mock_worker_once()
        self.assertEqual((task.task_id, task.state), (task_id, "succeeded"))
        self.restart()
        notice = self.core.pending_notifications("resident")[0]
        self.core.notification_failed(notice.notification_id, "resident", "speaker offline")
        self.restart()
        retry = self.core.pending_notifications("resident")[0]
        self.assertEqual((retry.notification_id, retry.attempts, retry.last_error),
                         (notice.notification_id, 1, "speaker offline"))
        self.core.ack_notification(retry.notification_id, "resident")
        self.core.ack_notification(retry.notification_id, "resident")
        self.restart()
        self.assertFalse(self.core.pending_notifications("resident"))
        self.assertTrue(self.core.get_task(task_id, "resident").result.startswith("MOCK"))

    def test_stop_speech_does_not_cancel_or_ack_any_task(self):
        task_id = self.task()
        task = self.core.claim_task()
        self.core.submit(self.event("别说了", utterance_id="stop"))
        self.assertTrue(self.core.is_speech_stopped("session", "resident"))
        self.assertEqual(self.core.get_task(task_id, "resident").state, "running")
        self.assertTrue(self.core.complete_task(task_id, task.claim_token, "done"))
        self.core.stop_speech("session", "resident")
        self.assertEqual(len(self.core.pending_notifications("resident")), 1)

    def test_cancelled_task_cannot_complete_or_publish_success(self):
        task_id = self.task()
        task = self.core.claim_task()
        receipt = self.core.submit(self.event(f"取消任务 {task_id}", utterance_id="cancel"))
        self.assertEqual(receipt.results[0].status, "cancelled")
        self.assertFalse(self.core.complete_task(task_id, task.claim_token, "late success"))
        self.assertFalse(self.core.pending_notifications("resident"))
        self.restart()
        self.assertEqual(self.core.get_task(task_id, "resident").state, "cancelled")

    def test_task_and_notification_access_require_owner(self):
        task_id = self.task()
        with self.assertRaises(PermissionError):
            self.core.cancel_task(task_id, "visitor")
        with self.assertRaises(PermissionError):
            self.core.get_task(task_id, "visitor")
        self.core.run_mock_worker_once()
        notice = self.core.pending_notifications("resident")[0]
        self.assertFalse(self.core.pending_notifications("visitor"))
        with self.assertRaises(PermissionError):
            self.core.ack_notification(notice.notification_id, "visitor")
        with self.assertRaises(PermissionError):
            self.core.stop_speech("session", "visitor")
        self.assertEqual(len(self.core.pending_notifications("resident")), 1)

    def test_expired_worker_fails_without_replay_and_rejects_stale_completion(self):
        task_id = self.task()
        task = self.core.claim_task(lease_seconds=10)
        self.now += 11
        self.restart()
        self.assertIsNone(self.core.claim_task())
        self.assertEqual(self.core.get_task(task_id, "resident").state, "failed")
        self.assertFalse(self.core.complete_task(task_id, task.claim_token, "late success"))
        self.assertEqual(len(self.core.pending_notifications("resident")), 1)
        self.assertIsNone(self.core.claim_task())
        self.assertEqual(len(self.core.pending_notifications("resident")), 1)

    def test_completion_and_outbox_commit_atomically(self):
        task_id = self.task()
        task = self.core.claim_task()
        with patch.object(self.core, "_notify", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.core.complete_task(task_id, task.claim_token, "done")
        self.assertEqual(self.core.get_task(task_id, "resident").state, "running")
        self.assertFalse(self.core.pending_notifications("resident"))
        self.assertTrue(self.core.complete_task(task_id, task.claim_token, "done"))
        self.assertFalse(self.core.complete_task(task_id, task.claim_token, "done"))
        self.assertEqual(len(self.core.pending_notifications("resident")), 1)

    def test_worker_failure_is_a_durable_result(self):
        task_id = self.task()
        task = self.core.claim_task()
        self.assertTrue(self.core.complete_task(task_id, task.claim_token, "failed", error="mock failure"))
        self.restart()
        self.assertEqual(self.core.get_task(task_id, "resident").state, "failed")
        self.assertEqual(self.core.pending_notifications("resident")[0].message, "mock failure")

    def test_mock_gateway_failure_rolls_back_entire_submission(self):
        original = self.core.gateway.execute

        def fail_after_write(*args):
            original(*args)
            raise RuntimeError("injected gateway failure")

        with patch.object(self.core.gateway, "execute", side_effect=fail_after_write):
            with self.assertRaises(RuntimeError):
                self.core.submit(self.event("打开客厅灯，再帮我调研框架"))
        self.assertFalse(self.core.mock_home_effects())
        self.assertIsNone(self.core.claim_task())
        self.assertEqual(self.core.submit(self.event()).status, "committed")
        self.assertEqual(len(self.core.mock_home_effects()), 1)

    def test_concurrent_duplicate_submissions_have_one_effect(self):
        barrier = Barrier(2)

        def submit():
            core = Core(self.path, MockHomeGateway())
            try:
                barrier.wait(timeout=5)
                return core.submit(self.event())
            finally:
                core.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            a, b = list(executor.map(lambda _: submit(), range(2)))
        self.assertEqual(a, b)
        self.assertEqual(len(self.core.mock_home_effects()), 1)

    def test_cancel_completion_race_has_one_terminal_result(self):
        task_id = self.task()
        task = self.core.claim_task()
        barrier = Barrier(2)

        def mutate(cancel):
            core = Core(self.path, MockHomeGateway(), clock=lambda: self.now)
            try:
                barrier.wait(timeout=5)
                if cancel:
                    return core.cancel_task(task_id, "resident")
                return core.complete_task(task_id, task.claim_token, "done")
            finally:
                core.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            cancelled, completed = list(executor.map(mutate, (True, False)))
        state = self.core.get_task(task_id, "resident").state
        self.assertIn(state, {"cancelled", "succeeded"})
        self.assertEqual(completed, state == "succeeded")
        self.assertEqual(cancelled, state)
        self.assertEqual(len(self.core.pending_notifications("resident")), int(completed))

    def test_demo_is_executable_without_external_services(self):
        with patch("socket.socket", side_effect=AssertionError("network call in Mock core")):
            output = demo()
        self.assertTrue(all(output["checks"].values()))
        self.assertEqual(output["mode"], "mock")


if __name__ == "__main__":
    unittest.main()
