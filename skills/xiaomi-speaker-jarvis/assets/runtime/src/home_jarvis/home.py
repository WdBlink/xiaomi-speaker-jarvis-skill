"""Allowlisted Home Assistant REST actions. No discovery or credential creation."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import DeviceObservation, HomeAction, PlanningContext, _identifier


DEFAULT_HOME_CONFIG = Path("~/.config/home-jarvis/home.json")
SERVICES = {
    "switch": {"on": "turn_on", "off": "turn_off"},
    "light": {"on": "turn_on", "off": "turn_off"},
    "cover": {"open": "open_cover", "close": "close_cover",
              "stop": "stop_cover", "set_position": "set_cover_position"},
}
COVER_FEATURES = {"open": 1, "close": 2, "set_position": 4, "stop": 8}


@dataclass(frozen=True)
class Room:
    id: str
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Device:
    entity_id: str
    room_id: str
    name: str
    aliases: tuple[str, ...]
    actions: frozenset[str]


@dataclass(frozen=True)
class HomeOutcome:
    status: str
    message: str


class HomeRequestError(Exception):
    def __init__(self, code: str, http_status: int | None = None):
        super().__init__(code)
        self.http_status = http_status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("home config has missing or unknown fields")


def _names(value) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() or item != item.strip() or len(item) > 80
        or re.search(r"[\x00-\x1f，,。.!！?？%]", item) for item in value
    ):
        raise ValueError("room and device names must be nonempty text without command punctuation")
    return tuple(value)


def _token(path: Path) -> str:
    path = path.expanduser().resolve()
    if any((parent / ".git").exists() for parent in path.parents):
        raise ValueError("token_file must be outside every Git working tree")
    # Inspect the opened object before reading, even if the path became a FIFO or symlink.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()):
            raise ValueError("token_file must be a current-user-owned regular file with mode 0600")
        token = handle.read(8193).strip()
    if not re.fullmatch(r"[A-Za-z0-9._~-]{1,8192}", token):
        raise ValueError("token_file does not contain a valid single bearer token")
    return token


class HomeAssistantGateway:
    external = True

    def __init__(self, config_path: str | os.PathLike[str], *, timeout: float = 5,
                 verification_timeout: float = 10, poll_interval: float = 0.25):
        for value in (timeout, verification_timeout, poll_interval):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("HTTP and verification timeouts must be positive and finite")
        self.timeout, self.verification_timeout, self.poll_interval = timeout, verification_timeout, poll_interval
        try:
            with Path(config_path).expanduser().open(encoding="utf-8") as handle:
                config = json.load(handle)
        except json.JSONDecodeError:
            raise ValueError("home config is not valid JSON") from None
        _fields(config, {"schema_version", "base_url", "token_file", "owners", "default_room", "rooms", "devices"})
        if type(config["schema_version"]) is not int or config["schema_version"] != 1:
            raise ValueError("home config schema_version must be 1")
        if not isinstance(config["base_url"], str):
            raise ValueError("base_url must be an HTTP or HTTPS origin")
        url = urlsplit(config["base_url"])
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or url.password is not None or url.path not in {"", "/"} or url.query or url.fragment
                or re.search(r"[\s\\]", config["base_url"])):
            raise ValueError("base_url must be an HTTP or HTTPS origin without credentials, path or query")
        url.port
        self.base_url = config["base_url"].rstrip("/")
        if not isinstance(config["owners"], list) or not config["owners"]:
            raise ValueError("owners must be a nonempty list")
        for owner in config["owners"]:
            _identifier(owner)
        self.owners = frozenset(config["owners"])
        self.rooms: dict[str, Room] = {}
        if not isinstance(config["rooms"], list) or not config["rooms"]:
            raise ValueError("rooms must be a nonempty list")
        for item in config["rooms"]:
            _fields(item, {"id", "name"}, {"aliases"})
            _identifier(item["id"])
            name = _names([item["name"]])[0]
            if item["id"] in self.rooms:
                raise ValueError("room IDs must be unique")
            self.rooms[item["id"]] = Room(item["id"], name, _names(item.get("aliases", [])))
        _identifier(config["default_room"])
        self.default_room = config["default_room"]
        if self.default_room not in self.rooms:
            raise ValueError("default_room must name a configured room")
        self.devices: dict[str, Device] = {}
        self.targets: dict[str, set[str]] = {}
        self.groups: dict[str, set[str]] = {}
        if not isinstance(config["devices"], list) or not config["devices"]:
            raise ValueError("devices must be a nonempty list")
        for item in config["devices"]:
            _fields(item, {"entity_id", "room_id", "name", "actions"}, {"aliases"})
            entity = item["entity_id"]
            if not isinstance(entity, str) or not re.fullmatch(r"(?:switch|light|cover)\.[a-z0-9_]+", entity):
                raise ValueError("only explicit switch, light and cover entity IDs are allowed")
            if entity in self.devices:
                raise ValueError("entity IDs must be unique")
            _identifier(item["room_id"])
            if item["room_id"] not in self.rooms:
                raise ValueError("device room_id must name a configured room")
            actions = item["actions"]
            if (not isinstance(actions, list) or not actions or any(not isinstance(a, str) for a in actions)
                    or not set(actions) <= SERVICES[entity.split(".")[0]].keys()):
                raise ValueError("device actions must be supported by its domain; dim is not supported")
            device = Device(entity, item["room_id"], _names([item["name"]])[0],
                            _names(item.get("aliases", [])), frozenset(actions))
            self.devices[entity] = device
            room = self.rooms[device.room_id]
            names = {device.name, *device.aliases}
            prefixes = sorted((room.name, *room.aliases), key=len, reverse=True)
            local_names = set()
            for name in names:
                prefix = next((prefix for prefix in prefixes if name.startswith(prefix)), "")
                local = name[len(prefix):] if prefix else name
                if prefix:
                    local = re.sub(r"^(?:里的|的)", "", local)
                if local:
                    local_names.add(local)
            names |= {prefix + separator + name for prefix in prefixes
                      for name in local_names for separator in ("", "的", "里的")}
            for name in names:
                self.targets.setdefault(name, set()).add(entity)
            domain = entity.split(".")[0]
            categories = ()
            if domain == "cover" and any("帘" in name for name in (device.name, *device.aliases)):
                categories = ("窗帘", "所有窗帘", "全部窗帘")
            elif domain == "light" or (domain == "switch" and any(
                    "灯" in name for name in (device.name, *device.aliases))):
                categories = ("所有灯", "全部灯", "灯光")
            for prefix in prefixes:
                for separator in ("", "的", "里的"):
                    for category in categories:
                        self.groups.setdefault(prefix + separator + category, set()).add(entity)
        if not isinstance(config["token_file"], str) or not Path(config["token_file"]).expanduser().is_absolute():
            raise ValueError("token_file must be an absolute path")
        self._token = _token(Path(config["token_file"]))
        self._http = build_opener(ProxyHandler({}), _NoRedirect())

    def resolve(self, text: str) -> tuple[HomeAction, ...]:
        text = text.strip().rstrip("。.!！")
        text = re.sub(r"^(?:(?:请|麻烦)(?:你)?)?(?:帮我|替我|给我)?", "", text, count=1)
        position = None
        match = re.fullmatch(r"(?:把)?(.+?)(?:都)?(?:给)?(?:开到|调到|设置到)(\d{1,3})%", text)
        if match:
            target, position = match[1], int(match[2])
            verb = "set_position"
            if position > 100:
                return ()
        else:
            verbs = r"打开|开启|关闭|关掉|关上|停止|拉开|拉上|开|关"
            match = re.fullmatch(rf"({verbs})(?:一下)?(.+?)(?:一下)?", text)
            if match:
                verb, target = match[1], match[2]
            else:
                match = re.fullmatch(rf"(?:把)?(.+?)(?:都)?(?:给)?({verbs})(?:一下)?", text)
                if not match:
                    return ()
                target, verb = match[1], match[2]
        if target in self.targets:
            matches = self.targets[target]
            if len(matches) != 1:
                return ()
        else:
            matches = self.groups.get(target, set())
            if not matches or len({self.devices[entity].room_id for entity in matches}) != 1:
                return ()
        verb = {"开": "打开", "拉开": "打开", "关": "关闭", "关上": "关闭", "拉上": "关闭"}.get(verb, verb)
        actions = []
        for entity in sorted(matches):
            device = self.devices[entity]
            cover = entity.startswith("cover.")
            action = {"打开": "open" if cover else "on", "开启": "open" if cover else "on",
                      "关闭": "close" if cover else "off", "关掉": "close" if cover else "off",
                      "停止": "stop", "set_position": "set_position"}[verb]
            if action not in device.actions:
                return ()
            actions.append(HomeAction(device.room_id, entity, action, position))
        return tuple(actions)

    def authorize(self, owner_id: str, room_id: str, action: HomeAction) -> None:
        device = self.devices.get(action.entity_id)
        if (owner_id not in self.owners or room_id not in self.rooms or device is None
                or device.room_id != action.room_id or action.action not in device.actions
                or (action.action == "set_position" and (type(action.position) is not int or not 0 <= action.position <= 100))
                or (action.action != "set_position" and action.position is not None)):
            raise PermissionError("home action is not in the complete-action allowlist")

    def query_home(self, room_id: str, now: float) -> PlanningContext:
        timezone = "UTC"
        observations = tuple(DeviceObservation(entity, "unknown", None, 0) for entity in self.devices)
        sun_state = None
        error = None
        try:
            config = self._request("/api/config")
            if not isinstance(config, dict) or not isinstance(config.get("time_zone"), str):
                raise HomeRequestError("invalid_timezone")
            ZoneInfo(config["time_zone"])
            timezone = config["time_zone"]
            states = self._request("/api/states")
            if not isinstance(states, list):
                raise HomeRequestError("invalid_states")
            selected, duplicates = {}, set()
            for item in states:
                if not isinstance(item, dict):
                    continue
                entity = item.get("entity_id")
                if not isinstance(entity, str) or (entity not in self.devices and entity != "sun.sun"):
                    continue
                if entity in selected:
                    duplicates.add(entity)
                selected[entity] = item
            observed = []
            for entity in self.devices:
                item = selected.get(entity, {})
                state, attributes = item.get("state"), item.get("attributes")
                known = {"open", "closed", "opening", "closing"} if entity.startswith("cover.") else {"on", "off"}
                if entity in duplicates or not isinstance(state, str) or state not in known or not isinstance(attributes, dict):
                    observed.append(DeviceObservation(entity, "unknown", None, 0))
                    continue
                position = attributes.get("current_position")
                features = attributes.get("supported_features", 0)
                if (type(features) is not int or features < 0
                        or (position is not None and (type(position) not in (int, float)
                            or not math.isfinite(position) or not 0 <= position <= 100))):
                    observed.append(DeviceObservation(entity, "unknown", None, 0))
                    continue
                observed.append(DeviceObservation(entity, state, position, features))
            observations = tuple(observed)
            sun = selected.get("sun.sun", {}).get("state")
            if "sun.sun" not in duplicates and isinstance(sun, str) and sun in {"above_horizon", "below_horizon"}:
                sun_state = sun
        except (HomeRequestError, ZoneInfoNotFoundError, ValueError):
            error = "无法读取 Home Assistant 的时间配置和设备状态"
        local_time = datetime.fromtimestamp(now, ZoneInfo(timezone)).isoformat(timespec="seconds")
        return PlanningContext(now, local_time, timezone, room_id, observations, sun_state,
                               observation_error=error)

    def _request(self, path: str, payload: dict | None = None, *, timeout: float | None = None):
        request = Request(self.base_url + path,
                          data=None if payload is None else json.dumps(payload).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with self._http.open(request, timeout=timeout or self.timeout) as response:
                data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise HomeRequestError("response_too_large")
                return json.loads(data)
        except HTTPError as error:
            error.close()
            raise HomeRequestError(f"HTTP {error.code}", error.code) from None
        except (TimeoutError, socket.timeout):
            raise HomeRequestError("request_timeout") from None
        except (URLError, OSError):
            raise HomeRequestError("connection_failed") from None
        except (ValueError, UnicodeError):
            raise HomeRequestError("invalid_response") from None

    def _state(self, entity_id: str, *, timeout: float | None = None) -> dict:
        data = self._request("/api/states/" + entity_id, timeout=timeout)
        if (not isinstance(data, dict) or data.get("entity_id") != entity_id
                or not isinstance(data.get("state"), str) or not isinstance(data.get("attributes"), dict)):
            raise HomeRequestError("invalid_entity_state")
        if data["state"] in {"unknown", "unavailable"}:
            raise HomeRequestError("entity_unavailable")
        return data

    @staticmethod
    def _confirmed(action: HomeAction, state: dict) -> bool:
        value = state["state"]
        if action.action in {"on", "off"}:
            return value == action.action
        if value not in {"open", "closed"}:
            return False
        if action.action == "stop":
            return True
        position = state["attributes"].get("current_position")
        if action.action == "set_position":
            return type(position) in (int, float) and abs(position - action.position) <= 1
        expected = 100 if action.action == "open" else 0
        return value == ("open" if expected else "closed") and (position is None or position == expected)

    def execute(self, owner_id: str, room_id: str, action: HomeAction, *,
                check_current: Callable[[], None] | None = None) -> HomeOutcome:
        self.authorize(owner_id, room_id, action)
        try:
            before = self._state(action.entity_id)
            if action.entity_id.startswith("cover."):
                features = before["attributes"].get("supported_features", 0)
                if type(features) is not int or not features & COVER_FEATURES[action.action]:
                    return HomeOutcome("failed", "HA 当前未声明支持此窗帘动作，未发送控制请求")
        except HomeRequestError as error:
            return HomeOutcome("failed", f"HA 状态检查失败（{error}），未发送控制请求")
        domain = action.entity_id.split(".")[0]
        payload = {"entity_id": action.entity_id}
        if action.position is not None:
            payload["position"] = action.position
        if check_current is not None:
            try:
                check_current()
            except Exception:
                return HomeOutcome("failed", "本次指令已取消或过期，未发送控制请求")
        try:
            self._request(f"/api/services/{domain}/{SERVICES[domain][action.action]}", payload)
        except HomeRequestError as error:
            rejected = error.http_status in {400, 401, 403, 404, 405, 422}
            return HomeOutcome("failed" if rejected else "uncertain",
                               f"HA 控制请求{'被拒绝' if rejected else '结果不确定'}（{error}），不会自动重放")
        deadline = time.monotonic() + self.verification_timeout
        while True:
            try:
                state = self._state(action.entity_id, timeout=min(self.timeout, max(0.001, deadline - time.monotonic())))
            except HomeRequestError as error:
                return HomeOutcome("uncertain", f"已发送 HA 控制请求，状态确认失败（{error}），不会自动重放")
            if self._confirmed(action, state):
                return HomeOutcome("applied", f"HA 已确认 {self.devices[action.entity_id].name} 的目标状态")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return HomeOutcome("uncertain", "HA 已接收控制请求，但尚未确认目标状态，不会自动重放")
            time.sleep(min(self.poll_interval, remaining))


def configured_gateway(config_path: str | os.PathLike[str] | None = None) -> HomeAssistantGateway:
    return HomeAssistantGateway(config_path if config_path is not None else
                                os.environ.get("JARVIS_HOME_CONFIG", DEFAULT_HOME_CONFIG))
