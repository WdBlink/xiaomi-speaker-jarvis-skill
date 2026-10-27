from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest

from home_jarvis.core import (Core, DeviceObservation, HomeAction, MockHomeGateway, ModelPlan,
                             PlanningContext, Utterance)


LAMP = HomeAction("living_room", "light.living_room", "on")


class Planner:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.contexts = []

    def plan(self, text, *, query_home=None):
        self.calls.append(text)
        if query_home is not None:
            self.contexts.append(query_home())
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result(text) if callable(self.result) else self.result


class RecordingGateway(MockHomeGateway):
    external = True

    def __init__(self, path):
        self.path = path
        self.calls = []
        self.entities = {f"light.{room}": room for room in
                         ("living_room", "bedroom", "study", "dining_room")}

    def authorize(self, owner_id, room_id, action):
        if (owner_id != "resident" or room_id != "living_room"
                or self.entities.get(action.entity_id) != action.room_id
                or action.action not in ({"close"} if action.entity_id.startswith("cover.") else {"on", "off"})
                or action.position is not None):
            raise PermissionError("private authorization detail must not be exposed")

    def resolve(self, text):
        for name, room in (("客厅灯", "living_room"), ("客厅吸顶灯", "living_room"), ("卧室灯", "bedroom"),
                           ("书房灯", "study"), ("餐厅灯", "dining_room")):
            for verb, action in (("打开", "on"), ("关闭", "off")):
                if text == verb + name:
                    return (HomeAction(room, "light." + room, action),)
        return ()

    def execute(self, owner_id, room_id, action, *, check_current=None):
        if check_current is not None:
            check_current()
        self.authorize(owner_id, room_id, action)
        with sqlite3.connect(self.path) as db:
            states = db.execute("SELECT state FROM home_outbox ORDER BY rowid").fetchall()
            receipt = json.loads(db.execute("SELECT receipt FROM submissions").fetchone()[0])
        if receipt["status"] != "committed" or not any(row[0] == "in_flight" for row in states):
            raise AssertionError("the complete plan and send attempt must be durable before dispatch")
        self.calls.append((action, tuple(row[0] for row in states)))
        return SimpleNamespace(status="applied", message="设备状态回读已确认")


class CorePlannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "planner.sqlite3"
        self.cores = []
        self.addCleanup(lambda: [core.close() for core in self.cores])

    def core(self, planner=None, gateway=None):
        core = Core(self.path, gateway or MockHomeGateway(), planner=planner)
        self.cores.append(core)
        core.open_session("session")
        return core

    def event(self, text="解释一下为什么天空是蓝色的", **kwargs):
        return replace(Utterance("session", "utterance", "resident", "qwen3-asr", text), **kwargs)

    def assert_no_actions(self, core):
        self.assertFalse(core.mock_home_effects())
        self.assertEqual(core.db.execute("SELECT COUNT(*) FROM home_outbox").fetchone()[0], 0)

    def test_planner_requires_persistence_even_with_mock_gateway(self):
        with self.assertRaises(ValueError):
            Core(":memory:", MockHomeGateway(), planner=Planner(ModelPlan("你好")))


    def test_reply_is_durable_and_duplicate_after_restart_does_not_call_model(self):
        planner = Planner(ModelPlan("阳光经过大气散射，蓝光更容易散射。"))
        core = self.core(planner)
        receipt = core.submit(self.event())
        self.assertEqual((receipt.status, receipt.model_calls), ("committed", 1))
        self.assertEqual((receipt.results[0].intent, receipt.results[0].status), ("reply", "answered"))
        self.assertEqual(core.submit(self.event("另一句话")), receipt)
        core.close()
        self.cores.remove(core)
        restarted = self.core(planner)
        self.assertEqual(restarted.submit(self.event()), receipt)
        self.assertEqual(len(planner.calls), 1)
        self.assert_no_actions(restarted)


    def test_without_planner_unknown_and_mock_research_behavior_is_unchanged(self):
        core = self.core()
        receipt = core.submit(self.event())
        self.assertEqual(receipt.results[0].status, "clarification_required")
        self.assertEqual(receipt.model_calls, 0)
        receipt = core.submit(self.event("帮我调研数据库", utterance_id="research"))
        self.assertEqual(receipt.results[0].status, "queued")
        self.assertTrue(core.run_mock_worker_once().result.startswith("MOCK"))


    def test_wrong_source_partial_and_wrong_owner_never_call_planner(self):
        planner = Planner(ModelPlan("你好"))
        core = self.core(planner)
        for event in (self.event(final=False), self.event(source="other-asr")):
            self.assertEqual(core.submit(event).status, "ignored")
        with self.assertRaises(PermissionError):
            core.submit(self.event(owner_id="visitor"))
        self.assertEqual(planner.calls, [])


    def test_network_and_parse_failures_are_sanitized_and_not_retried(self):
        for index, error in enumerate((TimeoutError("secret-token-and-endpoint"),
                                       ValueError("secret-response-body"))):
            with self.subTest(index=index):
                planner = Planner(error)
                core = self.core(planner)
                event = self.event(utterance_id=str(index))
                receipt = core.submit(event)
                self.assertEqual(receipt.status, "model_failed")
                self.assertEqual(receipt.results[0].status, "failed")
                self.assertNotIn("secret", json.dumps(asdict(receipt)))
                self.assertEqual(core.submit(event), receipt)
                self.assertEqual(len(planner.calls), 1)
                self.assert_no_actions(core)


    def test_invalid_plan_and_reply_boundaries_fail_without_actions(self):
        bad = [None, {"message": "hello"}, ModelPlan(""), ModelPlan("   "),
               ModelPlan("x" * 351), ModelPlan("bad\x00text"), ModelPlan("bad\ud800"),
               ModelPlan(1), ModelPlan("ignored", [LAMP]), ModelPlan("ignored", (None,)),
               ModelPlan("ignored", (HomeAction("living_room", "light.living_room", "delete"),)),
               ModelPlan("ignored", (HomeAction("living_room", "light.living_room", "set_position", True),)),
               ModelPlan("ignored", (HomeAction("living_room", "light.living_room", "set_position", 101),)),
               ModelPlan("ignored", (replace(LAMP, position=0),)),
               ModelPlan("ignored", (replace(LAMP, action="set_position"),)),
               ModelPlan("ok", model_calls=True), ModelPlan("ok", model_calls=0),
               ModelPlan("ok", model_calls=3)]
        core = self.core()
        for index, result in enumerate(bad):
            with self.subTest(index=index):
                core.planner = Planner(result)
                receipt = core.submit(self.event("请帮我打开客厅灯", utterance_id=str(index)))
                self.assertEqual(receipt.status, "model_failed")
                self.assert_no_actions(core)
        core.planner = Planner(ModelPlan("好" * 350))
        self.assertEqual(core.submit(self.event(utterance_id="limit")).results[0].message, "好" * 350)


    def test_all_actions_are_authorized_before_any_outbox_or_effect(self):
        gateway = RecordingGateway(self.path)
        planner = Planner(ModelPlan("已经完成所有动作", (LAMP,
            HomeAction("bedroom", "light.not_allowed", "on"))))
        core = self.core(planner, gateway)
        receipt = core.submit(self.event("请帮我打开所有灯"))
        self.assertEqual(receipt.status, "model_failed")
        self.assertEqual(gateway.calls, [])
        self.assert_no_actions(core)
        self.assertNotIn("private authorization", json.dumps(asdict(receipt)))


    def test_model_room_owner_and_unsupported_ability_are_rechecked(self):
        core = self.core()
        plans = (HomeAction("bedroom", LAMP.entity_id, "on"),
                 HomeAction(LAMP.room_id, LAMP.entity_id, "open"))
        for index, action in enumerate(plans):
            core.planner = Planner(ModelPlan("成功", (action,)))
            self.assertEqual(core.submit(self.event("请帮我打开客厅灯", utterance_id=str(index))).status,
                             "model_failed")
        core.open_session("visitor-session", owner_id="visitor")
        core.planner = Planner(ModelPlan("成功", (LAMP,)))
        receipt = core.submit(self.event("请帮我打开客厅灯", session_id="visitor-session", owner_id="visitor"))
        self.assertEqual(receipt.status, "model_failed")
        self.assert_no_actions(core)


    def test_crashed_model_claim_survives_restart_without_replay_or_speakable_result(self):
        planner = Planner(SystemExit("simulated process termination"))
        core = self.core(planner)
        with self.assertRaises(SystemExit):
            core.submit(self.event())
        core.close()
        self.cores.remove(core)
        new_planner = Planner(ModelPlan("should not be called"))
        restarted = self.core(new_planner)
        receipt = restarted.submit(self.event())
        self.assertEqual((receipt.status, receipt.model_calls, receipt.results), ("model_pending", 1, ()))
        self.assertEqual(new_planner.calls, [])
        self.assert_no_actions(restarted)


    def test_cancellation_blocks_late_model_actions_and_cannot_cancel_other_owner(self):
        entered, release = Event(), Event()

        def answer(text):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release model")
            return ModelPlan("完成", (LAMP,))

        planner = Planner(answer)
        core = self.core(planner)

        def submit():
            worker = Core(self.path, MockHomeGateway(), planner=planner)
            try:
                return worker.submit(self.event("请帮我打开客厅灯"))
            finally:
                worker.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(submit)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(PermissionError):
                    core.cancel_pending_models("session", "visitor")
                self.assertEqual(core.cancel_pending_models("session", "resident"), 1)
                self.assertEqual(core.cancel_pending_models("session", "resident"), 0)
            finally:
                release.set()
            receipt = pending.result(timeout=5)
        self.assertEqual((receipt.status, receipt.results), ("model_cancelled", ()))
        self.assertEqual(core.submit(self.event("请帮我打开客厅灯")), receipt)
        self.assertEqual(len(planner.calls), 1)
        self.assert_no_actions(core)


    def test_session_binding_is_revalidated_after_model_call(self):
        def change_session(text):
            with sqlite3.connect(self.path) as db:
                db.execute("UPDATE sessions SET room_id='bedroom' WHERE session_id='session'")
            return ModelPlan("完成", (LAMP,))

        core = self.core(Planner(change_session))
        receipt = core.submit(self.event("请帮我打开客厅灯"))
        self.assertEqual(receipt.status, "model_failed")
        self.assert_no_actions(core)


    def test_only_complete_urgent_commands_bypass_the_planner(self):
        planner = Planner(AssertionError("urgent commands must not call the model"))
        core = self.core()
        research = core.submit(self.event("帮我调研数据库", utterance_id="research"))
        core.planner = planner
        for index, text in enumerate(("别说了", "停止播报。", "取消任务 " + research.results[0].target_id)):
            receipt = core.submit(self.event(text, utterance_id=str(index)))
            self.assertEqual(receipt.model_calls, 0)
        self.assertEqual(planner.calls, [])

    def test_all_other_text_reaches_the_model_unchanged_without_resolving(self):
        planner = Planner(ModelPlan("由模型判断这条请求。"))
        gateway = MockHomeGateway()
        gateway.resolve = lambda _text: self.fail("the agent path must not call the text resolver")
        core = self.core(planner, gateway)
        cases = ("打开客厅灯", "把客厅的灯关掉", "不要开灯", "打开客厅灯，不对，关掉",
                 "请说打开客厅灯", "设置五分钟倒计时", "帮我调研数据库", "查一下北京天气",
                 "打开客厅灯，再停止播报", "你判断一下应该关闭哪个窗帘")
        for index, text in enumerate(cases):
            receipt = core.submit(self.event(text, utterance_id=str(index)))
            self.assertEqual(receipt.results[0].status, "answered")
            self.assertEqual(receipt.results[0].message, "由模型判断这条请求。")
            self.assertEqual(receipt.model_calls, 1)
            self.assert_no_actions(core)
            self.assertIsNone(core.claim_task())
        self.assertEqual(planner.calls, list(cases))

    def test_structured_actions_do_not_need_a_second_language_match(self):
        gateway = RecordingGateway(self.path)
        gateway.resolve = lambda _text: self.fail("no semantic regex after model selection")
        actions = (replace(LAMP, action="off"), HomeAction("bedroom", "light.bedroom", "off"))
        planner = Planner(ModelPlan("模型尚未执行", actions))
        core = self.core(planner, gateway)
        text = "客厅和卧室的灯帮我熄了吧"
        receipt = core.submit(self.event(text))
        self.assertEqual(planner.calls, [text])
        self.assertEqual([item[0] for item in gateway.calls], list(actions))
        self.assertEqual([item.status for item in receipt.results], ["applied", "applied"])

    def context_gateway(self, *, observed_at=1000, error=None):
        gateway = RecordingGateway(self.path)
        gateway.entities.update({"cover.fabric": "living_room", "cover.sheer": "living_room",
                                 "cover.bedroom": "bedroom"})
        context = PlanningContext(observed_at, "2026-10-27T21:50:00+08:00", "Asia/Shanghai",
                                  "living_room", (
                                      DeviceObservation("cover.fabric", "open", 100, 15),
                                      DeviceObservation("cover.sheer", "closed", 0, 15),
                                      DeviceObservation("cover.bedroom", "open", 20, 15),
                                  ), "below_horizon", error)
        def observe(room_id, now):
            self.assertEqual((room_id, now), ("living_room", 1000))
            with sqlite3.connect(self.path) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
            return context
        gateway.query_home = observe
        return gateway, context

    def test_query_is_lazy_and_only_runs_when_planner_calls_the_tool(self):
        gateway, _ = self.context_gateway()
        gateway.query_home = lambda *_args: self.fail("a direct answer need not query home")
        class DirectPlanner:
            def plan(self, text, *, query_home):
                self.query_home = query_home
                return ModelPlan("二加二等于四。")
        planner = DirectPlanner()
        core = self.core(planner, gateway)
        result = core.submit(self.event("二加二等于几"))
        self.assertTrue(callable(planner.query_home))
        self.assertEqual(result.results[0].message, "二加二等于四。")
        self.assert_no_actions(core)

    def test_query_facts_reach_planner_outside_transaction_and_reply_is_preserved(self):
        gateway, context = self.context_gateway()
        answer = "现在已日落，建议关闭客厅布帘保护隐私。"
        planner = Planner(ModelPlan(answer, model_calls=2))
        core = self.core(planner, gateway)
        core.clock = lambda: 1000
        result = core.submit(self.event("根据现在时间判断一下应该关哪个窗帘"))
        self.assertEqual(planner.contexts, [context])
        self.assertEqual(result.results[0].message, answer)
        self.assertEqual(result.model_calls, 2)
        self.assert_no_actions(core)

    def test_unavailable_observation_can_be_explained_without_actions(self):
        gateway, context = self.context_gateway(error="设备状态暂不可用")
        planner = Planner(ModelPlan("暂时读不到设备状态。", model_calls=2))
        core = self.core(planner, gateway)
        core.clock = lambda: 1000
        receipt = core.submit(self.event("根据状态帮我分析一下"))
        self.assertEqual(planner.contexts, [context])
        self.assertEqual(receipt.results[0].status, "answered")
        self.assert_no_actions(core)

    def test_contextual_action_uses_only_real_receipt_and_does_not_replay(self):
        gateway, context = self.context_gateway()
        action = HomeAction("living_room", "cover.fabric", "close")
        planner = Planner(ModelPlan("模型自称已关闭全屋", (action,), "模型自称关闭成功", 2))
        core = self.core(planner, gateway)
        core.clock = lambda: 1000
        event = self.event("根据现在的时间判断后帮我关上需要关闭的窗帘")
        receipt = core.submit(event)
        self.assertEqual([item.status for item in receipt.results], ["applied"])
        self.assertEqual(receipt.results[0].message, "设备状态回读已确认")
        self.assertEqual(receipt.model_calls, 2)
        self.assertNotIn("模型自称", json.dumps(asdict(receipt), ensure_ascii=False))
        self.assertEqual(core.submit(event), receipt)
        self.assertEqual([item[0] for item in gateway.calls], [action])
        self.assertEqual(planner.contexts, [context])

    def test_stale_future_or_nonfinite_context_cannot_dispatch_actions(self):
        for index, observed_at in enumerate((954, 1001, float("nan"), float("inf"))):
            with self.subTest(observed_at=observed_at):
                gateway, _ = self.context_gateway(observed_at=observed_at)
                core = self.core(Planner(ModelPlan("", (LAMP,), model_calls=2)), gateway)
                core.clock = lambda: 1000
                receipt = core.submit(self.event("自己选择并执行", utterance_id=str(index)))
                self.assertEqual(receipt.status, "model_failed")
                self.assertFalse(gateway.calls)
                self.assert_no_actions(core)

    def test_every_dispatch_rechecks_cancellation_and_context_age(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                gateway, _ = self.context_gateway(observed_at=956)
                actions = (LAMP, HomeAction("bedroom", "light.bedroom", "off"))
                planner = Planner(ModelPlan("", actions, model_calls=2))
                clock, stop = [1000], [False]
                def check_current():
                    if stop[0]:
                        raise ValueError("cancelled")
                planner.check_current = check_current
                execute = gateway.execute
                def execute_first(*args, **kwargs):
                    outcome = execute(*args, **kwargs)
                    if cancelled:
                        stop[0] = True
                    else:
                        clock[0] += 2
                    return outcome
                gateway.execute = execute_first
                core = self.core(planner, gateway)
                core.clock = lambda: clock[0]
                event = self.event("调整这两个房间的灯", utterance_id=str(cancelled))
                receipt = core.submit(event)
                self.assertEqual([item.status for item in receipt.results], ["applied", "failed"])
                self.assertEqual(len(gateway.calls), 1)
                self.assertIn("未发送", receipt.results[-1].message)
                self.assertEqual(core.submit(event), receipt)
                self.assertEqual(len(gateway.calls), 1)

    def test_unqueried_actions_expire_before_commit(self):
        clock = [1000]
        def delayed_plan(_text):
            clock[0] += 61
            return ModelPlan("", (LAMP,))
        core = self.core(Planner(delayed_plan))
        core.clock = lambda: clock[0]
        receipt = core.submit(self.event("打开客厅灯"))
        self.assertEqual(receipt.status, "model_failed")
        self.assert_no_actions(core)

    def test_query_checks_cancellation_before_and_after_read(self):
        for cancel_during_query in (False, True):
            with self.subTest(cancel_during_query=cancel_during_query):
                gateway, context = self.context_gateway()
                queried = []
                cancelled = [not cancel_during_query]
                def query(*_args):
                    queried.append(True)
                    cancelled[0] = True
                    return context
                gateway.query_home = query
                planner = Planner(ModelPlan("", (LAMP,)))
                def check_current():
                    if cancelled[0]:
                        raise TimeoutError("private cancellation details")
                planner.check_current = check_current
                core = self.core(planner, gateway)
                core.clock = lambda: 1000
                receipt = core.submit(self.event(utterance_id=str(cancel_during_query)))
                self.assertEqual(receipt.status, "model_failed")
                self.assertEqual(len(queried), int(cancel_during_query))
                self.assertFalse(planner.contexts)
                self.assertNotIn("private", json.dumps(asdict(receipt)))
                self.assert_no_actions(core)

    def test_current_guard_runs_again_in_commit_transaction(self):
        planner = Planner(ModelPlan("", (LAMP,)))
        core = self.core(planner)
        checked_in_transaction = []
        def check_current():
            checked_in_transaction.append(core.db.in_transaction)
            if core.db.in_transaction:
                raise TimeoutError("expired before commit")
        planner.check_current = check_current
        receipt = core.submit(self.event("打开客厅灯"))
        self.assertIn(True, checked_in_transaction)
        self.assertEqual(receipt.status, "model_failed")
        self.assertEqual(len(planner.calls), 1)
        self.assert_no_actions(core)

    def test_complete_batch_can_exceed_four_and_is_durable_before_first_dispatch(self):
        gateway = RecordingGateway(self.path)
        gateway.entities["light.kitchen"] = "kitchen"
        actions = tuple(HomeAction(room, entity, "on") for entity, room in gateway.entities.items())
        planner = Planner(ModelPlan("模型自称全屋已经成功", actions))
        core = self.core(planner, gateway)
        event = self.event("把这些房间的灯都打开")
        receipt = core.submit(event)
        self.assertEqual(len(gateway.calls), 5)
        self.assertEqual(gateway.calls[0][1], ("in_flight",) + ("pending",) * 4)
        self.assertEqual([item.status for item in receipt.results], ["applied"] * 5)
        self.assertTrue(all(item.message == "设备状态回读已确认" for item in receipt.results))
        self.assertNotIn("模型自称", json.dumps(asdict(receipt), ensure_ascii=False))
        self.assertEqual(self.core(planner, gateway).submit(event), receipt)
        self.assertEqual(len(gateway.calls), 5)
        self.assertEqual(len(planner.calls), 1)

    def test_duplicate_and_conflicting_actions_are_rejected(self):
        core = self.core()
        for index, actions in enumerate(((LAMP, LAMP), (LAMP, replace(LAMP, action="off")))):
            core.planner = Planner(ModelPlan("success", actions))
            receipt = core.submit(self.event("打开客厅灯", utterance_id=str(index)))
            self.assertEqual(receipt.status, "model_failed")
            self.assert_no_actions(core)

    def test_execution_error_is_uncertain_without_retry_or_model_success_claim(self):
        gateway = RecordingGateway(self.path)
        execute = gateway.execute
        def interrupted(*args, **kwargs):
            execute(*args, **kwargs)
            raise TimeoutError("private post-write transport error")
        gateway.execute = interrupted
        core = self.core(Planner(ModelPlan("模型自称全部完成", (LAMP,), "关闭成功")), gateway)
        event = self.event("打开客厅灯")
        receipt = core.submit(event)
        self.assertEqual(receipt.results[0].status, "uncertain")
        self.assertNotIn("private", json.dumps(asdict(receipt)))
        self.assertNotIn("模型自称", json.dumps(asdict(receipt), ensure_ascii=False))
        self.assertEqual(core.submit(event), receipt)
        self.assertEqual(len(gateway.calls), 1)

    def test_model_reason_never_overrides_uncertain_speech_receipt(self):
        from home_jarvis.speech import deliver_speech
        gateway = RecordingGateway(self.path)
        execute = gateway.execute
        def uncertain(*args, **kwargs):
            execute(*args, **kwargs)
            return SimpleNamespace(status="uncertain", message="尚未确认目标状态")
        gateway.execute = uncertain
        planner = Planner(ModelPlan("已经关闭", (replace(LAMP, action="off"),), "已经成功关闭"))
        core = self.core(planner, gateway)
        receipt = core.submit(self.event("关闭客厅的灯"))
        spoken = []
        speaker = SimpleNamespace(speak=lambda text: spoken.append(text) or "accepted")
        self.assertEqual(deliver_speech(self.path, speaker, asdict(receipt))["status"], "accepted")
        self.assertEqual(spoken, ["尚未确认目标状态"])

    def test_pending_model_does_not_hold_lock_and_urgent_stop_remains_available(self):
        entered, release = Event(), Event()
        def answer(_text):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release model")
            return ModelPlan("回答")
        planner = Planner(answer)
        core = self.core(planner)
        def submit():
            worker = Core(self.path, MockHomeGateway(), planner=planner)
            try:
                return worker.submit(self.event())
            finally:
                worker.close()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(submit)
            try:
                self.assertTrue(entered.wait(5))
                receipt = core.submit(self.event())
                self.assertEqual((receipt.status, receipt.results), ("model_pending", ()))
                core.db.execute("PRAGMA busy_timeout=100")
                stopped = core.submit(self.event("停止播报", utterance_id="urgent"))
                self.assertEqual(stopped.model_calls, 0)
                self.assertTrue(core.is_speech_stopped("session", "resident"))
                self.assertFalse(core.db.in_transaction)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).results[0].status, "answered")
        self.assertEqual(len(planner.calls), 1)


if __name__ == "__main__":
    unittest.main()
