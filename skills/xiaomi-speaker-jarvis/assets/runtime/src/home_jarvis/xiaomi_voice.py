"""Bind fresh Xiaomi cloud transcripts to Jarvis."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from concurrent.futures import Future, ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
from threading import Event
import time

from .core import Core, Utterance, route, urgent_route
from .home import configured_gateway
from .planner import MiniMaxPlanner, PlannerError
from .speaker import SpeakerError, XiaomiSpeaker
from .speech import deliver_speech
from .xiaomi_cloud import CloudError, ConversationRecord, XiaomiCloud


SOURCE = "xiaomi-cloud"
MAX_AGE_MS = 60_000


class _CancelledProposal(Exception):
    pass


class _CancellablePlanner:
    def __init__(self, planner, cancelled: Event, valid_until_ms: int, clock):
        self._planner, self._cancelled = planner, cancelled
        self._valid_until_ms, self._clock = valid_until_ms, clock

    def check_current(self):
        if self._cancelled.is_set() or int(self._clock() * 1000) - self._valid_until_ms > 0:
            raise _CancelledProposal()

    @property
    def model(self):
        return self._planner.model

    def plan(self, text, *, query_home=None):
        self.check_current()
        try:
            plan = (self._planner.plan(text, query_home=query_home) if query_home is not None
                    else self._planner.plan(text))
        except Exception as error:
            emit("model_error", reason=str(error) if isinstance(error, PlannerError) else type(error).__name__)
            raise
        self.check_current()
        return plan


def addressed_text(text: str) -> str | None:
    text = text.strip()
    if text.startswith("贾维斯"):
        rest = text[len("贾维斯"):]
    else:
        match = re.match(r"(?i)^jarvis(?=$|[\s，,:：。]|[\u4e00-\u9fff])", text)
        rest = text[match.end():] if match else text
    rest = rest.lstrip(" \t，,:：。")
    return rest or None


class VoiceBridge:
    def __init__(self, core: Core, speaker: XiaomiSpeaker, miot_did: str, *, clock=time.time):
        if str(core.db_path) == ":memory:":
            raise ValueError("The voice bridge requires a persistent database")
        self.core, self.speaker, self.clock = core, speaker, clock
        self.session_id = "xiaomi-cloud:" + miot_did
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-model") if core.planner else None
        self._model_jobs: list[tuple[Future, Event, str | None, int]] = []
        self.ready = False
        self.started_ms = 0
        self.latest_ms = 0
        core.open_session(self.session_id, owner_id="resident", room_id=core.gateway.default_room,
                          primary_source=SOURCE)
        core.db.execute("""CREATE TABLE IF NOT EXISTS voice_seen (
            session_id TEXT NOT NULL, event_id TEXT NOT NULL, time_ms INTEGER NOT NULL,
            disposition TEXT NOT NULL, PRIMARY KEY(session_id, event_id))""")

    @staticmethod
    def event_id(record: ConversationRecord) -> str:
        return hashlib.sha256(record.record_id.encode("utf-8")).hexdigest()

    def baseline(self, records: tuple[ConversationRecord, ...]) -> None:
        self.cancel_pending_models()
        self.ready = False
        with self.core._transaction():
            for record in records:
                self.core.db.execute("INSERT OR IGNORE INTO voice_seen VALUES (?, ?, ?, 'baseline')",
                                     (self.session_id, self.event_id(record), record.time_ms))
        self.started_ms = int(self.clock() * 1000)
        self.latest_ms = self.started_ms
        self.ready = True

    def consume(self, records: tuple[ConversationRecord, ...]) -> list[dict]:
        if not self.ready:
            raise ValueError("A successful cloud baseline is required before voice execution")
        self._expire_model_jobs()
        outputs = []
        accepted = []
        for record in sorted(records, key=lambda item: (item.time_ms, item.record_id)):
            now_ms = int(self.clock() * 1000)
            text = addressed_text(record.text)
            fresh = (self.started_ms < record.time_ms <= now_ms + 5000
                     and record.time_ms >= self.latest_ms and now_ms - record.time_ms <= MAX_AGE_MS)
            if fresh:
                self.latest_ms = record.time_ms
            eligible = fresh and text is not None
            event_id = self.event_id(record)
            with self.core._transaction():
                claimed = self.core.db.execute("INSERT OR IGNORE INTO voice_seen VALUES (?, ?, ?, ?)",
                    (self.session_id, event_id, record.time_ms, "claimed" if eligible else "ignored")).rowcount
            if not claimed or not eligible:
                continue
            accepted.append((record, text, event_id))
        if accepted:
            self._cancel_model_jobs()
        for record, text, event_id in accepted:
            now_ms = int(self.clock() * 1000)
            if (record.time_ms <= self.started_ms or record.time_ms > now_ms + 5000
                    or now_ms - record.time_ms > MAX_AGE_MS):
                continue
            if self._executor is not None:
                # A newer record in the same cloud response supersedes an earlier proposal.
                self._cancel_model_jobs()
            routes = (urgent_route(text) if self._executor is not None
                      else route(text, self.core.gateway.resolve))
            if self._executor is not None and not routes:
                try:
                    native_pause = self.speaker.stop()
                except SpeakerError:
                    native_pause = "uncertain"
                self._schedule_model(Utterance(self.session_id, event_id, "resident", SOURCE, text),
                                     native_pause, record.time_ms)
                continue
            native_pause = None
            if not any(item.intent == "stop_speech" for item in routes):
                try:
                    native_pause = self.speaker.stop()
                except SpeakerError:
                    native_pause = "uncertain"
            receipt = asdict(self.core.submit(Utterance(self.session_id, event_id, "resident", SOURCE, text)))
            if receipt["results"]:
                if native_pause is not None:
                    receipt["native_pause"] = native_pause
                receipt["speech"] = deliver_speech(self.core.db_path, self.speaker, receipt)
            outputs.append(receipt)
        outputs.extend(self._drain_model_jobs())
        return outputs

    def _cancel_model_jobs(self) -> None:
        for future, cancelled, _native_pause, _time_ms in self._model_jobs:
            cancelled.set()
            future.cancel()
        if self._executor is not None:
            self.core.cancel_pending_models(self.session_id, "resident")

    def cancel_pending_models(self) -> None:
        """Cancel voice proposals when cloud continuity or bridge lifetime ends."""
        self._cancel_model_jobs()

    def _expire_model_jobs(self) -> None:
        now_ms = int(self.clock() * 1000)
        if any(not cancelled.is_set() and now_ms - record_ms > MAX_AGE_MS
               for _future, cancelled, _pause, record_ms in self._model_jobs):
            self._cancel_model_jobs()

    def _schedule_model(self, utterance: Utterance, native_pause: str | None, time_ms: int) -> None:
        if self._executor is None:
            raise RuntimeError("Model executor is not configured")
        cancelled = Event()
        planner = _CancellablePlanner(self.core.planner, cancelled, time_ms + MAX_AGE_MS, self.clock)
        future = self._executor.submit(self._submit_in_worker, utterance, planner, cancelled,
                                       time_ms + MAX_AGE_MS)
        self._model_jobs.append((future, cancelled, native_pause, time_ms))

    def _submit_in_worker(self, utterance: Utterance, planner, cancelled: Event, valid_until_ms: int):
        if cancelled.is_set() or int(self.clock() * 1000) - valid_until_ms > 0:
            return None
        worker_core = Core(self.core.db_path, self.core.gateway, self.core.clock, planner=planner)
        try:
            if cancelled.is_set() or int(self.clock() * 1000) - valid_until_ms > 0:
                return None
            return asdict(worker_core.submit(utterance))
        finally:
            worker_core.close()

    def _drain_model_jobs(self) -> list[dict]:
        outputs = []
        remaining = []
        for future, cancelled, native_pause, time_ms in self._model_jobs:
            if not future.done():
                remaining.append((future, cancelled, native_pause, time_ms))
                continue
            if cancelled.is_set() or future.cancelled():
                continue
            try:
                receipt = future.result()
            except Exception:
                outputs.append({"status": "planner_unavailable", "message": "暂时无法处理这条语音请求"})
                continue
            if cancelled.is_set() or not receipt:
                continue
            if receipt.get("results"):
                if native_pause is not None:
                    receipt["native_pause"] = native_pause
                receipt["speech"] = deliver_speech(self.core.db_path, self.speaker, receipt)
            outputs.append(receipt)
        self._model_jobs = remaining
        return outputs

    def close(self) -> None:
        self.cancel_pending_models()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None


def emit(status, **fields):
    print(json.dumps({"status": status, "time_ms": int(time.time() * 1000), **fields}, ensure_ascii=False), flush=True)


def _terminate(_signum, _frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud-config", required=True)
    parser.add_argument("--speaker-config", required=True)
    parser.add_argument("--home-config")
    parser.add_argument("--model-config", required=True)
    parser.add_argument("--quiet-native-replies", action="store_true")
    parser.add_argument("--reply-volume", type=int, default=16)
    parser.add_argument("--db", default=str(Path.home() / ".config/home-jarvis/xiaomi-voice.sqlite3"))
    args = parser.parse_args()
    if not 1 <= args.reply_volume <= 100:
        parser.error("--reply-volume must be between 1 and 100")
    db_path = Path(args.db).expanduser()
    if not db_path.is_absolute() or args.db == ":memory:":
        parser.error("--db must be an absolute persistent path")
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(str(db_path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        parser.error("This voice database already has a running bridge")
    core = None
    bridge = None
    quiet_speaker = None
    previous_sigterm = signal.signal(signal.SIGTERM, _terminate)
    try:
        cloud = XiaomiCloud(args.cloud_config)
        speaker = XiaomiSpeaker(args.speaker_config)
        if cloud.miot_did != speaker.miot_did:
            raise ValueError("Cloud and LAN speaker identities differ")
        gateway = configured_gateway(args.home_config)
        planner = MiniMaxPlanner(args.model_config, gateway)
        if args.quiet_native_replies:
            from .quiet_speaker import QuietSpeaker
            quiet_speaker = QuietSpeaker(speaker, args.cloud_config, reply_volume=args.reply_volume)
            speaker = quiet_speaker
        core = Core(db_path, gateway, planner=planner)
        bridge = VoiceBridge(core, speaker, cloud.miot_did)
        last_error = None
        last_audio_error = None
        while True:
            if quiet_speaker and quiet_speaker.last_error != last_audio_error:
                last_audio_error = quiet_speaker.last_error
                if last_audio_error:
                    emit("audio_control_error", reason=last_audio_error)
            try:
                records = cloud.latest_records()
                if not bridge.ready:
                    bridge.baseline(records)
                    emit("ready", miot_did=cloud.miot_did, source=SOURCE,
                         wake_word="小爱同学", input_mode="all_transcripts", command_prefix=None,
                         model=planner.model if planner else None,
                         audio_mode="quiet_native_replies" if quiet_speaker else "pause_after_transcript",
                         reply_volume=args.reply_volume if quiet_speaker else None)
                else:
                    for receipt in bridge.consume(records):
                        emit("handled", receipt=receipt)
                last_error = None
            except CloudError as error:
                bridge.cancel_pending_models()
                bridge.ready = False
                if quiet_speaker:
                    try:
                        quiet_speaker.stop()
                    except SpeakerError as audio_error:
                        emit("audio_control_error", reason=str(audio_error))
                if str(error) != last_error:
                    emit("cloud_unavailable", reason=str(error))
                    last_error = str(error)
                time.sleep(30)
                cloud = XiaomiCloud(args.cloud_config)
                if cloud.miot_did != speaker.miot_did:
                    raise ValueError("Cloud and LAN speaker identities differ")
                continue
            time.sleep(3 if quiet_speaker else 1)
    except KeyboardInterrupt:
        pass
    except (CloudError, PlannerError, SpeakerError, OSError, ValueError, PermissionError) as error:
        emit("stopped", reason=str(error) if isinstance(error, (CloudError, PlannerError, SpeakerError, ValueError, PermissionError))
             else "Local service storage or connection failed")
        raise SystemExit(1) from None
    finally:
        try:
            if bridge is not None:
                bridge.close()
        finally:
            try:
                if quiet_speaker is not None:
                    quiet_speaker.close()
            except SpeakerError as error:
                emit("audio_restore_failed", reason=str(error))
            finally:
                if core is not None:
                    core.close()
                os.close(lock_fd)
                signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()
