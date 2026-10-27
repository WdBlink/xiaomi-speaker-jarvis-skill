"""Durable home actions; research tasks and mock workers are experimental demos."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import math
import os
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Callable, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from .home import HomeAssistantGateway


@dataclass(frozen=True)
class Utterance:
    session_id: str
    utterance_id: str
    owner_id: str
    source: str
    text: str
    final: bool = True


@dataclass(frozen=True)
class HomeAction:
    room_id: str
    entity_id: str
    action: Literal["on", "off", "dim", "open", "close", "stop", "set_position"]
    position: int | None = None


@dataclass(frozen=True)
class DeviceObservation:
    entity_id: str
    state: str
    position: int | float | None
    supported_features: int


@dataclass(frozen=True)
class PlanningContext:
    observed_at: float
    local_time: str
    timezone: str
    room_id: str
    devices: tuple[DeviceObservation, ...] = ()
    sun_state: str | None = None
    observation_error: str | None = None


@dataclass(frozen=True)
class ModelPlan:
    message: str
    actions: tuple[HomeAction, ...] = ()
    reason: str = ""
    model_calls: int = 1


@dataclass(frozen=True)
class ActionReceipt:
    intent: str
    execution: str
    risk: str
    status: str
    message: str
    target_id: str | None = None


@dataclass(frozen=True)
class Receipt:
    session_id: str
    utterance_id: str
    status: str
    results: tuple[ActionReceipt, ...] = ()
    model_calls: int = 0


@dataclass(frozen=True)
class Task:
    task_id: str
    owner_id: str
    room_id: str
    prompt: str
    state: str
    result: str | None
    claim_token: str | None
    lease_until: float | None


@dataclass(frozen=True)
class Notification:
    notification_id: str
    task_id: str
    owner_id: str
    room_id: str
    message: str
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class Route:
    intent: str
    execution: str
    risk: str
    text: str
    action: HomeAction | None = None
    task_id: str | None = None


HOME_PHRASES = {
    "打开客厅灯": "on", "请打开客厅灯": "on", "把客厅灯打开": "on",
    "关闭客厅灯": "off", "关掉客厅灯": "off", "把客厅灯关掉": "off",
    "把客厅灯调暗": "dim", "调暗客厅灯": "dim",
}


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        raise ValueError("IDs must contain 1-128 letters, digits, '.', '_', ':' or '-'")


def _text(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\x00" in value:
        raise ValueError("text must contain 1-4096 characters and no NUL")


def route(text: str, resolve: Callable[[str], tuple[HomeAction, ...]]) -> tuple[Route, ...]:
    _text(text)
    parts = re.split(r"[，,]再", text.strip().rstrip("。.!！"))
    routes = []
    for part in parts:
        part = part.strip()
        actions = resolve(part)
        if actions:
            routes.extend(Route("home", "immediate", "low", part, action) for action in actions)
        elif part in {"别说了", "停止播报"}:
            routes.append(Route("stop_speech", "immediate", "none", part))
        elif re.fullmatch(r"取消任务 [A-Za-z0-9._:-]{1,128}", part):
            routes.append(Route("cancel_task", "immediate", "low", part, task_id=part[5:]))
        # shortcut: finite mixed research phrases, validate a planner before allowing free-text mixed commands.
        elif part in {"帮我调研这两个框架", "调研这两个框架", "帮我调研框架", "调研框架"} or (
            len(parts) == 1 and re.fullmatch(r"(?:帮我)?调研\S.{0,2047}", part)
        ):
            routes.append(Route("research", "background", "none", part))
        else:
            return (Route("clarify", "none", "none", text),)
    return tuple(routes)


def urgent_route(text: str) -> tuple[Route, ...]:
    text = text.strip().rstrip("。.!！")
    if text in {"别说了", "停止播报"}:
        return (Route("stop_speech", "immediate", "none", text),)
    if re.fullmatch(r"取消任务 [A-Za-z0-9._:-]{1,128}", text):
        return (Route("cancel_task", "immediate", "low", text, task_id=text[5:]),)
    return ()


class MockHomeGateway:
    """Only database effects are implemented; this class never calls Home Assistant."""

    external = False

    def resolve(self, text: str) -> tuple[HomeAction, ...]:
        if text in HOME_PHRASES:
            return (HomeAction("living_room", "light.living_room", HOME_PHRASES[text]),)
        return ()

    def authorize(self, owner_id: str, room_id: str, action: HomeAction) -> None:
        if (owner_id, room_id, action.room_id, action.entity_id) != (
            "resident", "living_room", "living_room", "light.living_room"
        ) or action.action not in {"on", "off", "dim"} or action.position is not None:
            raise PermissionError("home action is not in the complete-action allowlist")

    def execute(self, db: sqlite3.Connection, owner_id: str, room_id: str,
                action: HomeAction, effect_id: str) -> None:
        self.authorize(owner_id, room_id, action)
        db.execute("INSERT INTO home_effects VALUES (?, ?, ?)",
                   (effect_id, action.entity_id, action.action))


class Core:
    def __init__(self, db_path: str | os.PathLike[str], gateway: MockHomeGateway | HomeAssistantGateway,
                 clock: Callable[[], float] = time.time, *, planner=None):
        if (gateway.external or planner is not None) and str(db_path) == ":memory:":
            raise ValueError("real home actions and model requests require a persistent database")
        self.db_path = db_path
        if str(db_path) != ":memory:":
            try:
                fd = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self.db = sqlite3.connect(db_path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.gateway = gateway
        self.clock = clock
        self.planner = planner
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                room_id TEXT NOT NULL, primary_source TEXT NOT NULL,
                speech_stopped INTEGER NOT NULL DEFAULT 0 CHECK (speech_stopped IN (0, 1))
            );
            CREATE TABLE IF NOT EXISTS submissions (
                session_id TEXT NOT NULL REFERENCES sessions(session_id),
                utterance_id TEXT NOT NULL, receipt TEXT NOT NULL,
                PRIMARY KEY (session_id, utterance_id)
            );
            CREATE TABLE IF NOT EXISTS home_effects (
                effect_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, action TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS home_outbox (
                effect_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL, utterance_id TEXT NOT NULL, result_index INTEGER NOT NULL,
                owner_id TEXT NOT NULL, room_id TEXT NOT NULL, action TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'in_flight', 'applied', 'failed', 'uncertain')),
                message TEXT NOT NULL,
                UNIQUE(session_id, utterance_id, result_index),
                FOREIGN KEY (session_id, utterance_id) REFERENCES submissions(session_id, utterance_id)
                    DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, room_id TEXT NOT NULL,
                prompt TEXT NOT NULL, state TEXT NOT NULL
                    CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
                result TEXT, claim_token TEXT, lease_until REAL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                notification_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
                owner_id TEXT NOT NULL, room_id TEXT NOT NULL, message TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)),
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
            );
        """)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def open_session(self, session_id: str, owner_id: str = "resident",
                     room_id: str = "living_room", primary_source: str = "qwen3-asr") -> None:
        for value in (session_id, owner_id, room_id, primary_source):
            _identifier(value)
        with self._transaction():
            self.db.execute("INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?, 0)",
                            (session_id, owner_id, room_id, primary_source))
            existing = self.db.execute("SELECT * FROM sessions WHERE session_id = ?",
                                       (session_id,)).fetchone()
            if tuple(existing)[1:4] != (owner_id, room_id, primary_source):
                raise ValueError("session identity and primary ASR cannot change")

    def _session(self, session_id: str, owner_id: str) -> sqlite3.Row:
        _identifier(session_id)
        _identifier(owner_id)
        session = self.db.execute("SELECT * FROM sessions WHERE session_id = ?",
                                  (session_id,)).fetchone()
        if session is None or session["owner_id"] != owner_id:
            raise PermissionError("session is not owned by this caller")
        return session

    def submit(self, event: Utterance) -> Receipt:
        if not isinstance(event, Utterance) or type(event.final) is not bool:
            raise ValueError("expected an Utterance with a boolean final flag")
        for value in (event.session_id, event.utterance_id, event.owner_id, event.source):
            _identifier(value)
        _text(event.text)
        effects = []
        model_claim = None
        with self._transaction():
            session = self._session(event.session_id, event.owner_id)
            if not event.final or event.source != session["primary_source"]:
                return Receipt(event.session_id, event.utterance_id, "ignored")
            existing = self.db.execute(
                "SELECT receipt FROM submissions WHERE session_id = ? AND utterance_id = ?",
                (event.session_id, event.utterance_id)).fetchone()
            if existing:
                return self._receipt(existing["receipt"])
            routes = (urgent_route(event.text) if self.planner is not None
                      else route(event.text, self.gateway.resolve))
            if self.planner is not None and not routes:
                receipt = Receipt(event.session_id, event.utterance_id, "model_pending", model_calls=1)
                model_claim = json.dumps(asdict(receipt), ensure_ascii=False)
            else:
                self._authorize_routes(routes, event, session)
                receipt, effects = self._prepare_routes(routes, event, session)
            self.db.execute("INSERT INTO submissions VALUES (?, ?, ?)",
                            (event.session_id, event.utterance_id,
                             json.dumps(asdict(receipt), ensure_ascii=False)))
        if model_claim is not None:
            return self._submit_model(event, session, model_claim)
        for effect_id in effects:
            self._dispatch_home(effect_id)
        return self._receipt(self.db.execute(
            "SELECT receipt FROM submissions WHERE session_id = ? AND utterance_id = ?",
            (event.session_id, event.utterance_id)).fetchone()["receipt"])

    def _authorize_routes(self, routes, event, session) -> None:
        for item in routes:
            if item.action:
                self.gateway.authorize(event.owner_id, session["room_id"], item.action)
            if item.task_id:
                self._task(item.task_id, event.owner_id)

    def _prepare_routes(self, routes, event, session, model_calls=0) -> tuple[Receipt, list[str]]:
        results, effects = [], []
        for index, item in enumerate(routes):
            if item.action and self.gateway.external:
                effect_id = str(uuid4())
                message = "家居动作已保存，尚未发出；重复输入不会自动重放"
                self.db.execute("INSERT INTO home_outbox VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                                (effect_id, event.session_id, event.utterance_id, index, event.owner_id,
                                 session["room_id"], json.dumps(asdict(item.action)), message))
                effects.append(effect_id)
                results.append(ActionReceipt(item.intent, item.execution, item.risk, "pending",
                                             message, item.action.entity_id))
            else:
                results.append(self._execute(item, event, session))
        return Receipt(event.session_id, event.utterance_id, "committed", tuple(results), model_calls), effects

    @staticmethod
    def _model_routes(plan: ModelPlan, text: str) -> tuple[Route, ...]:
        if (type(plan) is not ModelPlan or type(plan.actions) is not tuple
                or type(plan.model_calls) is not int or not 1 <= plan.model_calls <= 2):
            raise ValueError("invalid model plan")
        if not isinstance(plan.reason, str) or len(plan.reason) > 200 or "\x00" in plan.reason:
            raise ValueError("invalid model reason")
        plan.reason.encode("utf-8")
        if not plan.actions:
            _text(plan.message)
            if len(plan.message) > 350:
                raise ValueError("model reply is too long")
            plan.message.encode("utf-8")
            return (Route("reply", "immediate", "none", plan.message),)
        for action in plan.actions:
            if type(action) is not HomeAction:
                raise ValueError("invalid model action")
            _identifier(action.room_id)
            _identifier(action.entity_id)
            if (type(action.action) is not str or action.action not in
                    {"on", "off", "dim", "open", "close", "stop", "set_position"}
                    or (action.position is not None and
                        (type(action.position) is not int or not 0 <= action.position <= 100))
                    or (action.action == "set_position") != (action.position is not None)):
                raise ValueError("invalid model action parameters")
        if len({action.entity_id for action in plan.actions}) != len(plan.actions):
            raise ValueError("a model plan cannot act on the same device twice")
        return tuple(Route("home", "immediate", "low", text, action) for action in plan.actions)

    def _submit_model(self, event: Utterance, claimed_session: sqlite3.Row, claim: str) -> Receipt:
        context = None
        started_at = self.clock()
        model_calls = 1

        def check_current():
            guard = getattr(self.planner, "check_current", None)
            if guard is not None:
                guard()
            if not 0 <= self.clock() - started_at <= 60:
                raise ValueError("model request expired")
            if context is not None and (type(context.observed_at) not in (int, float)
                    or not math.isfinite(context.observed_at)
                    or not 0 <= self.clock() - context.observed_at <= 45):
                raise ValueError("home observation expired")

        def query_home():
            nonlocal context
            check_current()
            context = self.gateway.query_home(claimed_session["room_id"], self.clock())
            check_current()
            return context

        try:
            check_current()
            plan = (self.planner.plan(event.text, query_home=query_home)
                    if callable(getattr(self.gateway, "query_home", None))
                    else self.planner.plan(event.text))
            routes = self._model_routes(plan, event.text)
            model_calls = plan.model_calls
        except Exception:
            routes = None
        effects = []
        with self._transaction():
            stored = self.db.execute("SELECT receipt FROM submissions WHERE session_id=? AND utterance_id=?",
                                     (event.session_id, event.utterance_id)).fetchone()
            if stored is None:
                raise RuntimeError("model claim no longer exists")
            if stored["receipt"] != claim:
                return self._receipt(stored["receipt"])
            try:
                session = self._session(event.session_id, event.owner_id)
                if (routes is None or tuple(session)[1:4] != tuple(claimed_session)[1:4]
                        or event.source != session["primary_source"]):
                    raise ValueError("model request could not be completed")
                self._authorize_routes(routes, event, session)
                check_current()
            except Exception:
                receipt = Receipt(event.session_id, event.utterance_id, "model_failed", (
                    ActionReceipt("reply", "none", "none", "failed",
                                  "模型请求未能安全完成，未执行家居动作。"),), model_calls)
            else:
                receipt, effects = self._prepare_routes(routes, event, session, model_calls=model_calls)
            self.db.execute("UPDATE submissions SET receipt=? WHERE session_id=? AND utterance_id=?",
                            (json.dumps(asdict(receipt), ensure_ascii=False), event.session_id, event.utterance_id))
        for effect_id in effects:
            self._dispatch_home(effect_id, check_current=check_current)
        return self._receipt(self.db.execute(
            "SELECT receipt FROM submissions WHERE session_id=? AND utterance_id=?",
            (event.session_id, event.utterance_id)).fetchone()["receipt"])

    def cancel_pending_models(self, session_id: str, owner_id: str) -> int:
        """Invalidate outstanding proposals; their eventual replies cannot execute actions."""
        with self._transaction():
            self._session(session_id, owner_id)
            pending = self.db.execute("SELECT utterance_id, receipt FROM submissions WHERE session_id=?",
                                      (session_id,)).fetchall()
            cancelled = 0
            for row in pending:
                receipt = self._receipt(row["receipt"])
                if receipt.status != "model_pending":
                    continue
                receipt = Receipt(session_id, row["utterance_id"], "model_cancelled", model_calls=1)
                self.db.execute("UPDATE submissions SET receipt=? WHERE session_id=? AND utterance_id=?",
                                (json.dumps(asdict(receipt)), session_id, row["utterance_id"]))
                cancelled += 1
            return cancelled

    @staticmethod
    def _receipt(raw: str) -> Receipt:
        data = json.loads(raw)
        data["results"] = tuple(ActionReceipt(**item) for item in data["results"])
        return Receipt(**data)

    def _home_result(self, effect: sqlite3.Row, state: str, message: str) -> None:
        self.db.execute("UPDATE home_outbox SET state = ?, message = ? WHERE effect_id = ?",
                        (state, message, effect["effect_id"]))
        raw = self.db.execute("SELECT receipt FROM submissions WHERE session_id = ? AND utterance_id = ?",
                              (effect["session_id"], effect["utterance_id"])).fetchone()["receipt"]
        data = json.loads(raw)
        result = data["results"][effect["result_index"]]
        result.update(status="uncertain" if state == "in_flight" else state, message=message)
        self.db.execute("UPDATE submissions SET receipt = ? WHERE session_id = ? AND utterance_id = ?",
                        (json.dumps(data, ensure_ascii=False), effect["session_id"], effect["utterance_id"]))

    def _dispatch_home(self, effect_id: str, *, check_current=None) -> None:
        with self._transaction():
            effect = self.db.execute("SELECT * FROM home_outbox WHERE effect_id = ?", (effect_id,)).fetchone()
            if effect["state"] != "pending":
                return
            if check_current is not None:
                try:
                    check_current()
                except Exception:
                    self._home_result(effect, "failed", "本次判断已过期或取消，未发送该设备动作")
                    return
            self._home_result(effect, "in_flight", "HA 请求已开始，结果尚未确认；重复输入不会自动重放")
        try:
            kwargs = {"check_current": check_current} if check_current is not None else {}
            outcome = self.gateway.execute(effect["owner_id"], effect["room_id"],
                                           HomeAction(**json.loads(effect["action"])), **kwargs)
            state, message = outcome.status, outcome.message
        except Exception:
            # An exception after a write may hide a successful physical effect.
            state, message = "uncertain", "HA 执行中断，结果不确定，不会自动重放"
        with self._transaction():
            self._home_result(effect, state, message)

    def _execute(self, item: Route, event: Utterance, session: sqlite3.Row) -> ActionReceipt:
        target = None
        if item.action:
            target = item.action.entity_id
            self.gateway.execute(self.db, event.owner_id, session["room_id"], item.action, str(uuid4()))
            status, message = "mock_applied", f"MOCK {target}: {item.action.action}"
        elif item.intent == "research":
            target = str(uuid4())
            self.db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, 'queued', NULL, NULL, NULL)",
                            (target, event.owner_id, session["room_id"], item.text))
            status, message = "queued", "已保存模拟调研任务"
        elif item.intent == "stop_speech":
            self.db.execute("UPDATE sessions SET speech_stopped = 1 WHERE session_id = ?",
                            (event.session_id,))
            status, message = "stop_requested", "已请求停止播报，后台任务继续"
        elif item.intent == "cancel_task":
            target = item.task_id
            status = self._cancel_task(target, event.owner_id)
            message = "任务已取消" if status == "cancelled" else "任务已结束，状态未改变"
        elif item.intent == "reply":
            status, message = "answered", item.text
        elif item.intent == "unsupported":
            status = "unsupported"
            message = "目前尚未接入实时查询、定时提醒或调研和编码执行器；本次未创建任务或执行家居动作。"
        else:
            status, message = "clarification_required", "请明确房间、设备名称和支持的动作，未执行动作"
        return ActionReceipt(item.intent, item.execution, item.risk, status, message, target)

    def _task(self, task_id: str, owner_id: str) -> sqlite3.Row:
        _identifier(task_id)
        _identifier(owner_id)
        task = self.db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None or task["owner_id"] != owner_id:
            raise PermissionError("task is not owned by this caller")
        return task

    def get_task(self, task_id: str, owner_id: str) -> Task:
        return Task(**dict(self._task(task_id, owner_id)))

    def _notify(self, task: sqlite3.Row, message: str) -> None:
        self.db.execute(
            "INSERT INTO notifications (notification_id, task_id, owner_id, room_id, message) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), task["task_id"], task["owner_id"], task["room_id"], message))

    def claim_task(self, lease_seconds: float = 60) -> Task | None:
        if not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive and finite")
        with self._transaction():
            now = self.clock()
            expired = self.db.execute(
                "SELECT * FROM tasks WHERE state = 'running' AND lease_until <= ?", (now,)).fetchall()
            for task in expired:
                message = "Worker lease expired; no automatic replay"
                self.db.execute("UPDATE tasks SET state = 'failed', result = ?, claim_token = NULL, "
                                "lease_until = NULL WHERE task_id = ?", (message, task["task_id"]))
                self._notify(task, message)
            task = self.db.execute("SELECT * FROM tasks WHERE state = 'queued' ORDER BY rowid LIMIT 1").fetchone()
            if task is None:
                return None
            token = str(uuid4())
            self.db.execute("UPDATE tasks SET state = 'running', claim_token = ?, lease_until = ? "
                            "WHERE task_id = ?", (token, now + lease_seconds, task["task_id"]))
            return self.get_task(task["task_id"], task["owner_id"])

    def complete_task(self, task_id: str, claim_token: str, result: str,
                      error: str | None = None) -> bool:
        _identifier(task_id)
        _identifier(claim_token)
        _text(result)
        if error is not None:
            _text(error)
        with self._transaction():
            task = self.db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if (task is None or task["state"] != "running" or task["claim_token"] != claim_token
                    or task["lease_until"] <= self.clock()):
                return False
            message = error if error is not None else result
            self.db.execute("UPDATE tasks SET state = ?, result = ?, claim_token = NULL, "
                            "lease_until = NULL WHERE task_id = ?",
                            ("failed" if error is not None else "succeeded", message, task_id))
            self._notify(task, message)
            return True

    def run_mock_worker_once(self) -> Task | None:
        task = self.claim_task()
        if task is None:
            return None
        self.complete_task(task.task_id, task.claim_token, f"MOCK 调研结果：{task.prompt}")
        return self.get_task(task.task_id, task.owner_id)

    def _cancel_task(self, task_id: str, owner_id: str) -> str:
        task = self._task(task_id, owner_id)
        if task["state"] in {"queued", "running"}:
            self.db.execute("UPDATE tasks SET state = 'cancelled', result = 'Cancelled by owner', "
                            "claim_token = NULL, lease_until = NULL WHERE task_id = ?", (task_id,))
            return "cancelled"
        return task["state"]

    def cancel_task(self, task_id: str, owner_id: str) -> str:
        with self._transaction():
            return self._cancel_task(task_id, owner_id)

    def stop_speech(self, session_id: str, owner_id: str) -> None:
        with self._transaction():
            self._session(session_id, owner_id)
            self.db.execute("UPDATE sessions SET speech_stopped = 1 WHERE session_id = ?", (session_id,))

    def is_speech_stopped(self, session_id: str, owner_id: str) -> bool:
        return bool(self._session(session_id, owner_id)["speech_stopped"])

    def pending_notifications(self, owner_id: str) -> tuple[Notification, ...]:
        _identifier(owner_id)
        # shortcut: one dispatcher, add delivery leases before parallel consumers.
        return tuple(Notification(**dict(row)) for row in self.db.execute(
            "SELECT notification_id, task_id, owner_id, room_id, message, attempts, last_error "
            "FROM notifications WHERE owner_id = ? AND delivered = 0 ORDER BY rowid", (owner_id,)))

    def _notification(self, notification_id: str, owner_id: str) -> sqlite3.Row:
        _identifier(notification_id)
        _identifier(owner_id)
        row = self.db.execute("SELECT * FROM notifications WHERE notification_id = ?", (notification_id,)).fetchone()
        if row is None or row["owner_id"] != owner_id:
            raise PermissionError("notification is not owned by this caller")
        return row

    def ack_notification(self, notification_id: str, owner_id: str) -> None:
        with self._transaction():
            self._notification(notification_id, owner_id)
            self.db.execute("UPDATE notifications SET delivered = 1 WHERE notification_id = ?", (notification_id,))

    def notification_failed(self, notification_id: str, owner_id: str, error: str) -> None:
        _text(error)
        with self._transaction():
            self._notification(notification_id, owner_id)
            self.db.execute("UPDATE notifications SET attempts = attempts + 1, last_error = ? "
                            "WHERE notification_id = ? AND delivered = 0", (error, notification_id))

    def mock_home_effects(self) -> tuple[dict, ...]:
        return tuple(dict(row) for row in self.db.execute("SELECT * FROM home_effects ORDER BY rowid"))
