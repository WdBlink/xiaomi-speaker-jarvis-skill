"""Keep a stock Xiaomi speaker quiet except during an accepted Jarvis reply."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import os
import secrets
import ssl
from threading import Event, RLock, Thread
import time
from typing import Literal
import urllib.error
import urllib.parse
import urllib.request

from .speaker import MAX_TEXT_LENGTH, SpeakerError, XiaomiSpeaker
from .xiaomi_cloud import XiaomiCloud, _NoRedirect, _UA_MINA


_URL = "https://api2.mina.mi.com/remote/ubus"
_HTTP_TIMEOUT = 3
_MAX_RESPONSE = 16384
_SETTLE_TIMEOUT = 3
_IDLE_TIMEOUT = 5
_START_TIMEOUT = 8
_PLAY_TIMEOUT = 120
_ACTIVE_INTERVAL = 1
_IDLE_INTERVAL = 5
_SETTLE_INTERVAL = 0.1
State = Literal["idle", "starting", "playing", "fault", "closed"]


@dataclass(frozen=True)
class _Playback:
    status: int
    volume: int


class _PlayerClient(XiaomiCloud):
    def _ubus(self, method: str, message: dict) -> dict:
        payload = urllib.parse.urlencode({
            "deviceId": self._config.device_id,
            "path": "mediaplayer",
            "method": method,
            "message": json.dumps(message, separators=(",", ":")),
            "requestId": "app_ios_" + secrets.token_hex(15),
        }).encode("ascii")
        request = urllib.request.Request(_URL, data=payload, method="POST", headers={
            "User-Agent": _UA_MINA,
            "Cookie": f"userId={self._config.user_id}; serviceToken={self._config.service_token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        })
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        try:
            with opener.open(request, timeout=_HTTP_TIMEOUT) as response:
                if response.status != 200 or response.geturl() != _URL:
                    raise ValueError
                raw = response.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                raise ValueError
            result = json.loads(raw.decode("utf-8"))
            if (not isinstance(result, dict) or type(result.get("code")) is not int
                    or result["code"] != 0 or not isinstance(result.get("data"), dict)):
                raise ValueError
            data = result["data"]
            if type(data.get("code")) is not int or data["code"] != 0:
                raise ValueError
            return data
        except urllib.error.HTTPError as error:
            error.close()
            raise SpeakerError("Xiaomi playback control failed; its outcome is unconfirmed.") from None
        except Exception:
            raise SpeakerError("Xiaomi playback control failed; its outcome is unconfirmed.") from None

    def get_status(self) -> _Playback:
        data = self._ubus("player_get_play_status", {"media": "app_ios"})
        try:
            info = data.get("info")
            if isinstance(info, str):
                info = json.loads(info)
            if (not isinstance(info, dict) or type(info.get("status")) is not int
                    or info["status"] not in (0, 1) or type(info.get("volume")) is not int
                    or not 0 <= info["volume"] <= 100):
                raise ValueError
            return _Playback(info["status"], info["volume"])
        except Exception:
            raise SpeakerError("Xiaomi playback state is unknown; quiet playback is unconfirmed.") from None

    def set_volume(self, volume: int) -> None:
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ValueError("Playback volume must be an integer from 0 to 100.")
        self._ubus("player_set_volume", {"media": "airplay", "volume": volume})


class QuietSpeaker:
    def __init__(self, speaker: XiaomiSpeaker, cloud_config_path: str | os.PathLike[str],
                 reply_volume: int = 16):
        if type(reply_volume) is not int or not 1 <= reply_volume <= 100:
            raise ValueError("Reply volume must be an integer from 1 to 100.")
        self._speaker = speaker
        self._reply_volume = reply_volume
        self._lock = RLock()
        self._stop_requested = Event()
        self._shutdown = Event()
        self._wake = Event()
        self._state: State = "idle"
        self._started_at: float | None = None
        self._errors: deque[str] = deque(maxlen=20)
        self._last_error: str | None = None
        self._close_error: str | None = None
        self._thread: Thread | None = None
        changed_volume = False
        try:
            self._cloud = _PlayerClient(cloud_config_path)
            if self._cloud.miot_did != self.miot_did:
                raise ValueError
            self._cloud.verify_device()
            changed_volume = True
            self._set_volume(0)
            self._thread = Thread(target=self._watch, name="jarvis-quiet-speaker", daemon=True)
            self._thread.start()
        except BaseException as error:
            self._shutdown.set()
            self._wake.set()
            message = "Quiet speaker initialization failed; no reply was sent."
            with self._lock:
                self._state = "closed"
                if changed_volume:
                    try:
                        self._set_volume(reply_volume)
                    except Exception:
                        message += " Restoring reply volume also failed; device volume is unconfirmed."
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SpeakerError(message) from None

    @property
    def miot_did(self) -> str:
        return self._speaker.miot_did

    @property
    def state(self) -> State:
        return self._state

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._errors)

    def _remember_error(self, message: str) -> None:
        self._last_error = message
        self._errors.append(message)

    def _check_open(self) -> None:
        if self._state == "closed":
            raise SpeakerError("Quiet speaker is closed.")

    def _check_cancelled(self) -> None:
        if self._stop_requested.is_set() or self._shutdown.is_set():
            raise SpeakerError("Quiet speaker reply was cancelled before playback.")

    def _wait_status(self, volume: int, *, idle: bool = False,
                     cancellable: bool = False) -> None:
        deadline = time.monotonic() + (_IDLE_TIMEOUT if idle else _SETTLE_TIMEOUT)
        while True:
            if cancellable:
                self._check_cancelled()
            current = self._cloud.get_status()
            if current.volume == volume and (not idle or current.status == 0):
                return
            if time.monotonic() >= deadline:
                raise SpeakerError("Quiet speaker did not reach the required playback state.")
            if cancellable:
                self._stop_requested.wait(_SETTLE_INTERVAL)
            else:
                time.sleep(_SETTLE_INTERVAL)

    def _set_volume(self, volume: int, *, cancellable: bool = False) -> None:
        self._cloud.set_volume(volume)
        self._wait_status(volume, cancellable=cancellable)

    def _fault(self, message: str) -> None:
        self._state = "fault"
        self._started_at = None
        self._remember_error(message)
        try:
            self._set_volume(0)
        except Exception:
            self._remember_error(message + " Muting also failed; the speaker may be audible.")

    def speak(self, text: str) -> Literal["accepted"]:
        if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH
                or "\x00" in text):
            raise ValueError("Speaker text must contain 1 to 500 characters, nonblank and without NUL.")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise ValueError("Speaker text must be valid UTF-8 text.") from None
        with self._lock:
            self._check_open()
            self._check_cancelled()
            try:
                self._cloud.set_volume(0)
                if self._speaker.stop() != "accepted":
                    raise ValueError
                self._wait_status(0, idle=True, cancellable=True)
                self._set_volume(self._reply_volume, cancellable=True)
                self._check_cancelled()
                if self._speaker.speak(text) != "accepted":
                    raise ValueError
                self._started_at = time.monotonic()
                self._state = "starting"
                self._wake.set()
                return "accepted"
            except Exception:
                self._fault("Quiet speaker could not start the reply; TTS was not retried.")
                raise SpeakerError(self._last_error) from None

    def stop(self) -> Literal["accepted"]:
        self._stop_requested.set()
        try:
            with self._lock:
                self._check_open()
                self._started_at = None
                failed = False
                try:
                    self._cloud.set_volume(0)
                except Exception:
                    failed = True
                try:
                    if self._speaker.stop() != "accepted":
                        failed = True
                except Exception:
                    failed = True
                try:
                    self._wait_status(0)
                except Exception:
                    failed = True
                if failed:
                    self._fault("Quiet speaker stop could not be confirmed.")
                    raise SpeakerError(self._last_error) from None
                self._state = "idle"
                self._wake.set()
                return "accepted"
        finally:
            self._stop_requested.clear()

    def _monitor_once(self) -> None:
        with self._lock:
            if self._shutdown.is_set() or self._state == "closed":
                return
            try:
                current = self._cloud.get_status()
                if self._state in ("idle", "fault"):
                    if current.volume != 0:
                        self._set_volume(0)
                    self._state = "idle"
                    return
                elapsed = time.monotonic() - self._started_at
                if elapsed >= _PLAY_TIMEOUT or (self._state == "starting" and elapsed >= _START_TIMEOUT):
                    raise TimeoutError
                # shortcut: status has no audio source ID; use device-side events if they become available.
                if self._state == "starting" and current.status == 1:
                    self._state = "playing"
                elif self._state == "playing" and current.status == 0:
                    self._set_volume(0)
                    self._started_at = None
                    self._state = "idle"
            except Exception:
                self._fault("Quiet speaker playback monitoring failed or timed out; TTS was not retried.")

    def _watch(self) -> None:
        while not self._shutdown.is_set():
            interval = _ACTIVE_INTERVAL if self._state in ("starting", "playing") else _IDLE_INTERVAL
            self._wake.wait(interval)
            self._wake.clear()
            self._monitor_once()

    def close(self) -> None:
        self._shutdown.set()
        self._stop_requested.set()
        self._wake.set()
        with self._lock:
            if self._state == "closed":
                if self._close_error:
                    raise SpeakerError(self._close_error)
                return
            self._state = "closed"
            self._started_at = None
            try:
                self._set_volume(self._reply_volume)
            except Exception:
                self._close_error = "Quiet speaker closed, but restoring reply volume failed; device volume is unconfirmed."
                self._remember_error(self._close_error)
        if self._thread is not None:
            self._thread.join(timeout=_HTTP_TIMEOUT + 1)
            if self._thread.is_alive():
                self._close_error = "Quiet speaker watcher did not stop; playback control is unconfirmed."
                self._remember_error(self._close_error)
        if self._close_error:
            raise SpeakerError(self._close_error) from None
