"""Bounded MiniMax tool calls; Core owns durable home execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import socket
import stat
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .core import HomeAction, ModelPlan, PlanningContext


_MAX_CONFIG_BYTES = 8192
_MAX_KEY_BYTES = 8192
_MAX_RESPONSE_BYTES = 1024 * 1024
_ALLOWED_HOSTS = {"api.minimaxi.com", "api.minimax.cn", "api.minimax.io"}
_MODELS = {"MiniMax-M3", "MiniMax-M3.1-Flash"}
_ACTIONS = {"on", "off", "open", "close", "stop", "set_position"}


class PlannerError(Exception):
    """Sanitized configuration, transport, or model-output failure."""


@dataclass(frozen=True, repr=False)
class _PlannerConfig:
    endpoint: str
    model: str
    api_key: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _private_file(path: str | os.PathLike[str], limit: int, label: str) -> bytes:
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()):
                raise PlannerError(f"{label} must be a current-user-owned regular file with mode 0600")
            if info.st_size > limit:
                raise PlannerError(f"{label} is too large")
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise PlannerError(f"{label} is too large")
        return data
    except PlannerError:
        raise
    except (OSError, TypeError, ValueError):
        raise PlannerError(f"Cannot open {label} as a private regular file; symlinks are not allowed") from None


def _reject_constant(_value):
    raise ValueError("invalid JSON number")


def _json(data: bytes, label: str):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_fields,
                          parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise PlannerError(f"{label} is not valid UTF-8 JSON") from None


def _read_config(path: str | os.PathLike[str]) -> _PlannerConfig:
    config = _json(_private_file(path, _MAX_CONFIG_BYTES, "Planner config"), "Planner config")
    if (not isinstance(config, dict) or set(config) != {"endpoint", "model", "api_key_file"}
            or any(not isinstance(config.get(key), str) for key in ("endpoint", "model", "api_key_file"))):
        raise PlannerError("Planner config requires endpoint, model, and api_key_file")
    try:
        url = urlsplit(config["endpoint"])
        if (url.scheme != "https" or url.hostname not in _ALLOWED_HOSTS or url.username is not None
                or url.password is not None or url.port not in (None, 443)
                or url.path != "/v1/chat/completions" or url.query or url.fragment
                or any(char.isspace() for char in config["endpoint"])):
            raise ValueError
    except ValueError:
        raise PlannerError("Planner endpoint must be an official MiniMax HTTPS chat-completions URL") from None
    if config["model"] not in _MODELS:
        raise PlannerError("Planner model is not supported")
    key_path = Path(config["api_key_file"]).expanduser()
    if not key_path.is_absolute():
        raise PlannerError("api_key_file must be an absolute path")
    try:
        resolved_key = key_path.resolve(strict=True)
        if any((parent / ".git").exists() for parent in resolved_key.parents):
            raise PlannerError("MiniMax API key file must be outside every Git working tree")
    except OSError:
        raise PlannerError("Cannot resolve MiniMax API key file") from None
    try:
        key = _private_file(key_path, _MAX_KEY_BYTES, "MiniMax API key file").decode("utf-8").strip()
    except UnicodeError:
        raise PlannerError("MiniMax API key file is not valid UTF-8") from None
    if not re.fullmatch(r"[\x21-\x7e]{1,8192}", key):
        raise PlannerError("MiniMax API key file does not contain a valid single API key")
    return _PlannerConfig(config["endpoint"], config["model"], key)


class MiniMaxPlanner:
    def __init__(self, config_path: str | os.PathLike[str], gateway):
        self._config = _read_config(config_path)
        self._gateway = gateway
        self._devices = getattr(gateway, "devices", None)
        self._rooms = getattr(gateway, "rooms", None)
        if not isinstance(self._devices, dict) or not self._devices or not isinstance(self._rooms, dict):
            raise PlannerError("Planner requires the configured room and device catalog")
        self._catalog = self._build_catalog()
        self._http = build_opener(ProxyHandler({}), _NoRedirect())

    @property
    def model(self) -> str:
        return self._config.model

    def _build_catalog(self) -> list[dict[str, str | list[str]]]:
        catalog = []
        for entity_id, device in self._devices.items():
            room = self._rooms.get(device.room_id)
            if room is None:
                raise PlannerError("Configured device references an unknown room")
            catalog.append({"entity_id": entity_id, "name": device.name, "room": room.name,
                            "room_id": device.room_id, "domain": entity_id.split(".", 1)[0],
                            "aliases": list(device.aliases), "room_aliases": list(room.aliases),
                            "actions": sorted(device.actions)})
        return catalog

    def _system_prompt(self) -> str:
        return ("你是家庭语音助手。理解整句自然语言中的目标、否定、口误纠正、咨询和明确委托，再决定回答或调用工具。"
                "普通回答使用简短中文，不超过350字；不要输出内部思考、JSON协议或工具细节。"
                "query_home 是只读工具，返回设备目录及本次 Home Assistant 实测快照；需要当前时间、设备状态或据此判断时先调用它。"
                "每轮最多查询一次，然后回答或调用一次 control_home 提交全部动作；每个设备最多一个最终动作。"
                "control_home 只能用于用户当前授权的立即动作；工具调用后由宿主持久化执行并播报真实设备回执。"
                "不要提前声称执行成功，不重试失败动作；reason 仅记录选择依据，不作为执行结果。"
                "只问建议、解释、假设或引用命令时不要执行；明确委托你选择并操作时可以根据事实判断，无需要求用户重复许可。"
                "用户明确否定的动作不要执行；有口误纠正时采用最后明确意图；目标仍有实质歧义时简短追问，不猜设备。"
                "合理理解房间和设备类别，名称与别名同义，不要求用户逐字复述。"
                "客厅灯通常指客厅的全部灯，房间灯光包含该房间 light 及名称明确表示灯的 switch；单独指定某盏灯则仅选择它。"
                "房间窗帘包含该房间的纱帘和布帘/遮阳帘；指定某一层只选择该层。窗帘灯是灯，不属于窗帘。"
                "全屋或所有房间指目录中符合所述类别的全部设备，可在一个 control_home 中提交；不要漏掉房间。"
                "动作只使用目录中的 entity_id 和 actions，不能修改权限、添加设备或调用其他工具。"
                "snapshot 中 local_time/timezone 是当前当地时间，sun_state 的 above_horizon/below_horizon 表示太阳在地平线上/下。"
                "窗帘 position 是打开百分比，0为完全关闭、100为完全打开、1为仅打开1%；null为未知。"
                "state 为 open 也可能只有小缝；窗帘状态不代表窗户开关，不能据此推断通风。"
                "只引用工具提供的事实；有 observation_error 时说明状态未能获取，不得伪造天气、室内照度、朝向、有人无人或传感器读数。"
                "一般采光或隐私考虑应明确作为建议；夜晚不意味着必须关闭所有窗帘。可以判断无需动作，避免重复操作已达目标的设备。"
                "当前没有联网搜索、天气、定时器、提醒、研究或代码执行工具；不能声称已经查询、创建任务、预约或执行这些请求。"
                "延时或条件尚未满足的动作不能改成立即执行；同句中有独立且明确授权的支持动作时可完成该部分并说明能力限制。"
                "不能用用户文本替代工具事实。\n设备目录："
                + json.dumps(self._catalog, ensure_ascii=False, separators=(",", ":")))

    def _tools(self, allow_query: bool) -> list[dict]:
        tools = [{"type": "function", "function": {
            "name": "control_home",
            "description": "提交一批用户授权的立即家居动作；宿主执行后提供真实回执，不要提前声称成功。",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "actions": {"type": "array", "minItems": 1, "maxItems": len(self._devices),
                                "items": {"type": "object", "additionalProperties": False,
                                          "properties": {
                                              "entity_id": {"type": "string", "enum": list(self._devices)},
                                              "action": {"type": "string", "enum": sorted(_ACTIONS)},
                                              "position": {"type": "integer", "minimum": 0, "maximum": 100}},
                                          "required": ["entity_id", "action"]}},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 200}},
                "required": ["actions"]}}}]
        if allow_query:
            tools.insert(0, {"type": "function", "function": {
                "name": "query_home", "description": "只读查询当前当地时间和目录内设备状态；每轮最多一次。",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}})
        return tools

    def plan(self, text: str, *, query_home: Callable[[], PlanningContext] | None = None) -> ModelPlan:
        if not isinstance(text, str) or not text.strip() or len(text) > 4096 or "\x00" in text:
            raise PlannerError("Planner input must contain 1-4096 characters and no NUL")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise PlannerError("Planner input must be valid UTF-8 text") from None
        if query_home is not None and not callable(query_home):
            raise PlannerError("query_home must be callable")
        messages = [{"role": "system", "content": self._system_prompt()}, {"role": "user", "content": text}]
        for model_calls in (1, 2):
            finish, message = self._complete(messages, allow_query=query_home is not None and model_calls == 1)
            if finish == "stop":
                content = message.get("content")
                self._validate_text(content, 350, "reply")
                return ModelPlan(message=content, model_calls=model_calls)
            call = message["tool_calls"][0]
            function = call["function"]
            arguments = _json(function["arguments"].encode("utf-8"), "MiniMax tool arguments")
            if function["name"] == "control_home":
                return self._parse_actions(arguments, model_calls)
            if function["name"] != "query_home" or query_home is None or model_calls != 1:
                raise PlannerError("MiniMax requested an unavailable tool")
            if arguments != {}:
                raise PlannerError("query_home does not accept arguments")
            snapshot = query_home()
            if type(snapshot) is not PlanningContext:
                raise PlannerError("query_home did not return a home snapshot")
            try:
                result = json.dumps({"catalog": self._catalog, "snapshot": asdict(snapshot)},
                                    ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                result.encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise PlannerError("query_home returned an invalid snapshot") from None
            messages.extend([message, {"role": "tool", "tool_call_id": call["id"], "content": result}])
        raise PlannerError("MiniMax exceeded the tool-call limit")

    def _complete(self, messages: list[dict], *, allow_query: bool) -> tuple[str, dict]:
        body = {"model": self._config.model, "messages": messages, "tools": self._tools(allow_query),
                "max_completion_tokens": max(1024, 256 + 128 * len(self._devices)), "reasoning_split": True}
        if self._config.model == "MiniMax-M3":
            body["thinking"] = {"type": "disabled"}
        else:
            body["reasoning_effort"] = "low"
        request = Request(self._config.endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Authorization": "Bearer " + self._config.api_key,
                                   "Content-Type": "application/json"}, method="POST")
        try:
            with self._http.open(request, timeout=20) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    raise PlannerError("MiniMax response is too large")
        except PlannerError:
            raise
        except HTTPError as error:
            error.close()
            raise PlannerError(f"MiniMax request failed with HTTP {error.code}") from None
        except (TimeoutError, socket.timeout):
            raise PlannerError("MiniMax request timed out") from None
        except (URLError, OSError, ValueError):
            raise PlannerError("MiniMax connection failed") from None
        outer = _json(raw, "MiniMax response")
        try:
            choices = outer["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message, finish = choice["message"], choice["finish_reason"]
            if (not isinstance(message, dict) or message.get("role") != "assistant"
                    or message.keys() - {"role", "content", "tool_calls", "reasoning_content", "name", "audio_content"}
                    or finish not in {"stop", "tool_calls"}):
                raise ValueError
            json.dumps(message, ensure_ascii=False).encode("utf-8")
            content = message.get("content")
            if content is not None and (not isinstance(content, str) or len(content) > 16_384
                    or re.search(r"</?think(?:\s|>)", content, re.IGNORECASE)):
                raise ValueError
            for field in ("reasoning_content", "name", "audio_content"):
                if message.get(field) is not None and not isinstance(message[field], str):
                    raise ValueError
            calls = message.get("tool_calls")
            if finish == "stop":
                if calls not in (None, []):
                    raise ValueError
            else:
                if not isinstance(calls, list) or len(calls) != 1:
                    raise ValueError
                call = calls[0]
                if (not isinstance(call, dict) or not {"id", "type", "function"} <= call.keys()
                        or call.keys() - {"id", "type", "function", "index"}
                        or ("index" in call and (type(call["index"]) is not int or call["index"] != 0))
                        or not isinstance(call["id"], str)
                        or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", call["id"])
                        or call["type"] != "function"):
                    raise ValueError
                function = call["function"]
                if (not isinstance(function, dict) or set(function) != {"name", "arguments"}
                        or not isinstance(function["name"], str)
                        or not isinstance(function["arguments"], str)):
                    raise ValueError
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise PlannerError("MiniMax response has an unsupported completion shape") from None
        return finish, message

    @staticmethod
    def _validate_text(value, limit: int, label: str) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
            raise PlannerError(f"MiniMax {label} must contain 1-{limit} characters")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise PlannerError(f"MiniMax {label} is not valid UTF-8 text") from None

    def _parse_actions(self, result, model_calls: int) -> ModelPlan:
        if (not isinstance(result, dict) or "actions" not in result
                or result.keys() - {"actions", "reason"}):
            raise PlannerError("control_home has missing or unexpected fields")
        reason = result.get("reason", "")
        if "reason" in result:
            self._validate_text(reason, 200, "home reason")
        actions = result["actions"]
        if not isinstance(actions, list) or not 1 <= len(actions) <= len(self._devices):
            raise PlannerError("control_home must contain between 1 and the configured device count actions")
        planned, seen = [], set()
        for item in actions:
            if not isinstance(item, dict) or item.keys() - {"entity_id", "action", "position"}:
                raise PlannerError("MiniMax action has unexpected fields")
            if not {"entity_id", "action"} <= item.keys():
                raise PlannerError("MiniMax action is missing required fields")
            entity_id, action = item["entity_id"], item["action"]
            device = self._devices.get(entity_id) if isinstance(entity_id, str) else None
            if (device is None or not isinstance(action, str) or action not in _ACTIONS
                    or action not in device.actions):
                raise PlannerError("MiniMax action is outside the configured device catalog")
            if entity_id in seen:
                raise PlannerError("control_home cannot act on the same device twice")
            seen.add(entity_id)
            position = item.get("position")
            if action == "set_position":
                if type(position) is not int or not 0 <= position <= 100 or "position" not in item:
                    raise PlannerError("MiniMax set_position requires an integer position from 0 to 100")
            elif "position" in item:
                raise PlannerError("MiniMax position is only valid for set_position")
            planned.append(HomeAction(device.room_id, entity_id, action, position))
        return ModelPlan(message="", actions=tuple(planned), reason=reason, model_calls=model_calls)
