"""Read-only access to Xiaomi MiNA conversation records for one speaker."""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import ssl
import stat
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_HOST = "api2.mina.mi.com"
_BASE_URL = f"https://{_HOST}"
_DEVICE_LIST_PATH = "/admin/v2/device_list"
_CONVERSATION_HOST = "userprofile.mina.mi.com"
_CONVERSATION_BASE_URL = f"https://{_CONVERSATION_HOST}"
_CONVERSATION_PATH = "/device_profile/v2/conversation"
_MAX_CONFIG_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_HTTP_TIMEOUT_SECONDS = 10
_MAX_RECORD_TEXT = 2048
_UA_MINA = (
    "MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) "
    "Alamofire/6.0.103 MICO/iOSApp/appStore/6.0.103"
)
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")


class CloudError(Exception):
    """A safe, non-sensitive error from Xiaomi cloud access."""


@dataclass(frozen=True)
class ConversationRecord:
    record_id: str
    time_ms: int
    text: str


@dataclass(frozen=True, repr=False)
class _Config:
    user_id: str
    service_token: str
    device_id: str
    miot_did: str
    hardware: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class XiaomiCloud:
    """Read one configured Xiaomi speaker's MiNA records.

    The config is deliberately supplied out of band (for example, by a QR
    login flow). This class never logs in, refreshes tokens, or selects a
    different device.
    """

    def __init__(self, config_path: str | os.PathLike[str]):
        self._config = self._read_config(Path(config_path))
        self._verified = False

    @staticmethod
    def _read_config(path: Path) -> _Config:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise CloudError("Xiaomi cloud config must be a regular non-symlink file")
            flags = (os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != os.getuid()
                    or stat.S_IMODE(opened.st_mode) != 0o600
                ):
                    raise CloudError("Xiaomi cloud config must be owned by this user with mode 0600")
                raw = stream.read(_MAX_CONFIG_BYTES + 1)
        except CloudError:
            raise
        except (OSError, OverflowError):
            raise CloudError("Unable to read Xiaomi cloud config securely") from None

        if len(raw) > _MAX_CONFIG_BYTES:
            raise CloudError("Xiaomi cloud config exceeds the size limit")
        try:
            values = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise CloudError("Xiaomi cloud config is not valid JSON") from None
        if not isinstance(values, dict) or set(values) != {
            "userId", "serviceToken", "deviceId", "miotDID", "hardware"
        }:
            raise CloudError("Xiaomi cloud config has an invalid shape")

        user_id = _safe_identifier(values["userId"])
        device_id = _safe_identifier(values["deviceId"])
        miot_did = _safe_identifier(values["miotDID"])
        hardware = _safe_identifier(values["hardware"])
        token = values["serviceToken"]
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise CloudError("Xiaomi cloud config has an invalid shape")
        return _Config(user_id, token, device_id, miot_did, hardware)

    def verify_device(self) -> None:
        """Confirm the configured cloud device identity exactly once per call."""
        self._verified = False
        result = self._request("GET", _DEVICE_LIST_PATH)
        _check_outer_result(result)
        rows = result.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CloudError("Xiaomi device list has an invalid shape")
        matching_did = [
            row for row in rows
            if str(row.get("miotDID")) == self._config.miot_did
        ]
        matching_hardware = [row for row in rows if row.get("hardware") == self._config.hardware]
        if len(matching_did) != 1:
            raise CloudError("Configured Xiaomi speaker was not uniquely found")
        row = matching_did[0]
        if (
            row.get("deviceID") != self._config.device_id
            or row.get("hardware") != self._config.hardware
        ):
            raise CloudError("Xiaomi device identity does not match the configured target")
        if len(matching_hardware) != 1 or matching_hardware[0] is not row:
            raise CloudError("Xiaomi cloud cannot uniquely bind this hardware model to the target")
        self._verified = True

    @property
    def miot_did(self) -> str:
        """The public MiOT DID used to bind cloud records to the local speaker."""
        return self._config.miot_did

    def latest_records(self) -> tuple[ConversationRecord, ...]:
        """Return validated user-query records for the uniquely matched speaker."""
        if not self._verified:
            self.verify_device()
        result = self._request(
            "GET",
            _CONVERSATION_PATH,
            {
                "source": "dialogu",
                "hardware": self._config.hardware,
                "timestamp": str(int(time.time() * 1000)),
                "limit": "2",
            },
            include_device_id=True,
        )
        _check_outer_result(result)
        raw_data = result.get("data")
        if not isinstance(raw_data, str):
            raise CloudError("Xiaomi conversation response has an invalid shape")
        try:
            data = json.loads(raw_data)
        except (ValueError, TypeError):
            raise CloudError("Xiaomi conversation response has an invalid shape") from None
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise CloudError("Xiaomi conversation response has an invalid shape")

        records: list[ConversationRecord] = []
        for item in data["records"]:
            record = self._record_from_conversation(item)
            if record is not None:
                records.append(record)
        return tuple(records)

    def _record_from_conversation(self, item: Any) -> ConversationRecord | None:
        if not isinstance(item, dict) or item.get("illegalContent") not in (None, False, 0):
            return None
        request_id = item.get("requestId")
        raw_time = item.get("time")
        query = item.get("query")
        if not isinstance(request_id, str) or not _RECORD_ID_RE.fullmatch(request_id):
            return None
        if isinstance(raw_time, bool) or not isinstance(raw_time, int):
            return None
        if raw_time < 1 or raw_time > int(time.time() * 1000) + 300_000:
            return None
        if not isinstance(query, str):
            return None
        query = " ".join(query.split())
        if not query:
            return None
        if len(query) > _MAX_RECORD_TEXT or any(ord(char) < 32 for char in query):
            return None
        return ConversationRecord(f"{self._config.miot_did}:{request_id}", raw_time, query)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        *,
        include_device_id: bool = False,
    ) -> dict[str, Any]:
        if method != "GET" or path not in (_DEVICE_LIST_PATH, _CONVERSATION_PATH):
            raise CloudError("Unsupported Xiaomi cloud endpoint")
        is_conversation = path == _CONVERSATION_PATH
        base_url = _CONVERSATION_BASE_URL if is_conversation else _BASE_URL
        query = dict(params or {})
        if is_conversation != include_device_id:
            raise CloudError("Unsupported Xiaomi cloud request")
        if not is_conversation:
            query["master"] = "0"
            query["requestId"] = "app_ios_" + "".join(
                secrets.choice(string.ascii_letters + string.digits) for _ in range(30)
            )
        url = base_url + path + "?" + urllib.parse.urlencode(query)
        headers = {
            "User-Agent": _UA_MINA,
            "Cookie": f"userId={self._config.user_id}; serviceToken={self._config.service_token}",
            "Accept": "application/json",
        }
        if include_device_id:
            headers["Cookie"] += f"; deviceId={self._config.device_id}"
        req = urllib.request.Request(url, headers=headers, method=method)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        try:
            with opener.open(req, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                if response.geturl() != url or response.status != 200:
                    raise CloudError("Xiaomi cloud request failed")
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (401, 403):
                raise CloudError("Xiaomi cloud authentication rejected") from None
            raise CloudError("Xiaomi cloud request failed") from None
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError):
            raise CloudError("Xiaomi cloud request failed") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise CloudError("Xiaomi cloud response exceeds the size limit")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise CloudError("Xiaomi cloud response is not valid JSON") from None
        if not isinstance(parsed, dict):
            raise CloudError("Xiaomi cloud response has an invalid shape")
        if parsed.get("code") != 0:
            message = parsed.get("message", "")
            if isinstance(message, str) and "auth" in message.lower():
                raise CloudError("Xiaomi cloud authentication rejected")
            raise CloudError("Xiaomi cloud request was rejected")
        return parsed


def _safe_identifier(value: Any) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise CloudError("Xiaomi cloud config has an invalid shape")
    result = str(value)
    if not _ID_RE.fullmatch(result):
        raise CloudError("Xiaomi cloud config has an invalid shape")
    return result


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _check_outer_result(result: dict[str, Any]) -> None:
    if result.get("code") != 0:
        message = result.get("message", "")
        if isinstance(message, str) and "auth" in message.lower():
            raise CloudError("Xiaomi cloud authentication rejected")
        raise CloudError("Xiaomi cloud request was rejected")
