"""Explicit, single-attempt playback requests for a configured Xiaomi S12."""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
import secrets
import stat
from threading import Lock
from typing import Literal


MAX_TEXT_LENGTH = 500
_MAX_CONFIG_BYTES = 4096
_MODEL = "xiaomi.wifispeaker.s12"
_MAC = re.compile(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}")


class SpeakerError(Exception):
    """A sanitized configuration, identity, or uncertain playback failure."""


@dataclass(frozen=True, repr=False)
class _SpeakerConfig:
    localip: str
    did: int
    mac: str
    model: str
    token: str


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SpeakerError("Speaker config contains duplicate fields.")
        result[key] = value
    return result


def _read_config(path: str | os.PathLike[str]) -> _SpeakerConfig:
    try:
        # Do not resolve symlinks. Inspect the opened object before any read.
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()):
                raise SpeakerError("Speaker config must be a current-user-owned regular file with mode 0600.")
            if info.st_size > _MAX_CONFIG_BYTES:
                raise SpeakerError("Speaker config is too large.")
            data = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(data) > _MAX_CONFIG_BYTES:
            raise SpeakerError("Speaker config is too large.")
    except (OSError, TypeError, ValueError):
        raise SpeakerError("Cannot open speaker config as a private regular file; symlinks are not allowed.") from None
    try:
        config = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (UnicodeError, ValueError, RecursionError):
        raise SpeakerError("Speaker config is not valid UTF-8 JSON.") from None
    required = {"localip", "did", "mac", "model", "token"}
    if (not isinstance(config, dict) or not required <= config.keys()
            or config.keys() - required - {"name", "region"}):
        raise SpeakerError("Speaker config requires localip, did, mac, model, and token; only name and region metadata are optional.")
    try:
        if not isinstance(config["localip"], str):
            raise ValueError
        ip = IPv4Address(config["localip"])
        if ip.is_unspecified or ip.is_multicast or int(ip) == 0xffffffff:
            raise ValueError
    except ValueError:
        raise SpeakerError("Speaker localip must be a valid unicast IPv4 address.") from None
    did = config["did"]
    if (type(did) not in (str, int) or not re.fullmatch(r"[1-9][0-9]{0,9}", str(did))
            or int(did) > 0xffffffff):
        raise SpeakerError("Speaker did must be a positive 32-bit decimal device ID.")
    mac = config["mac"]
    if (not isinstance(mac, str) or not _MAC.fullmatch(mac) or int(mac[:2], 16) & 1
            or mac == "00:00:00:00:00:00"):
        raise SpeakerError("Speaker mac must be a valid unicast MAC address.")
    if config["model"] != _MODEL:
        raise SpeakerError("Only xiaomi.wifispeaker.s12 is supported.")
    token = config["token"]
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", token):
        raise SpeakerError("Speaker token must contain exactly 32 hexadecimal characters.")
    return _SpeakerConfig(str(ip), int(did), mac.lower(), _MODEL, token)


class XiaomiSpeaker:
    def __init__(self, config_path: str | os.PathLike[str]):
        self._config = _read_config(config_path)
        self._device = None
        self._lock = Lock()

    @property
    def miot_did(self) -> str:
        return str(self._config.did)

    def speak(self, text: str) -> Literal["accepted"]:
        """Accept a TTS request; this does not confirm audible or completed playback."""
        if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH
                or "\x00" in text):
            raise ValueError("Speaker text must contain 1 to 500 characters, nonblank and without NUL.")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise ValueError("Speaker text must be valid UTF-8 text.") from None
        return self._command("tts", [text])

    def stop(self) -> Literal["accepted"]:
        """Accept a pause request; this does not confirm playback has stopped."""
        return self._command("play_pause", [])

    def _command(self, method: str, params: list) -> Literal["accepted"]:
        with self._lock:
            if self._device is None:
                try:
                    from miio import Device
                except ImportError:
                    raise SpeakerError("Speaker support requires python-miio==0.5.12; install home-jarvis[speaker].") from None
                try:
                    # S12 deduplicates TTS IDs; python-miio wraps IDs at 9999.
                    # Refresh the protocol timestamp even after a long idle.
                    self._device = Device(ip=self._config.localip, token=self._config.token,
                                          model=self._config.model, timeout=3, lazy_discover=False,
                                          start_id=secrets.randbelow(9000) + 1)
                except Exception:
                    raise SpeakerError("Could not initialize the speaker connection; no command was sent.") from None
            try:
                info = self._device.send("miIO.info", [], retry_count=0)
            except Exception:
                raise SpeakerError("Speaker identity check failed; no playback command was sent.") from None
            if (not isinstance(info, dict) or info.get("model") != self._config.model
                    or not isinstance(info.get("mac"), str) or info["mac"].lower() != self._config.mac):
                raise SpeakerError("Speaker identity does not match the configured model and MAC; no playback command was sent.")
            try:
                result = self._device.send(method, params, retry_count=0)
            except Exception:
                raise SpeakerError("Speaker command outcome is unknown; it was not retried.") from None
            if result != ["ok"]:
                raise SpeakerError("Speaker returned an unexpected reply; playback is unconfirmed and was not retried.")
            return "accepted"
