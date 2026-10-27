import argparse
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from .core import Core, MockHomeGateway, Utterance
from .home import configured_gateway
from .speaker import SpeakerError, XiaomiSpeaker
from .speech import deliver_speech



def demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="home-jarvis-demo-") as directory:
        path = Path(directory) / "demo.sqlite3"
        core = Core(path, MockHomeGateway())
        try:
            core.open_session("demo")
            event = Utterance("demo", "turn-1", "resident", "qwen3-asr",
                              "把客厅灯调暗，再帮我调研这两个框架")
            receipt = core.submit(event)
            assert core.submit(event) == receipt
            core.stop_speech("demo", "resident")
            task = core.run_mock_worker_once()
            assert task and task.state == "succeeded"
        finally:
            core.close()
        core = Core(path, MockHomeGateway())
        try:
            notifications = core.pending_notifications("resident")
            assert len(core.mock_home_effects()) == 1 and len(notifications) == 1
            core.notification_failed(notifications[0].notification_id, "resident", "speaker offline")
            assert len(core.pending_notifications("resident")) == 1
            core.ack_notification(notifications[0].notification_id, "resident")
            assert not core.pending_notifications("resident")
            return {"mode": "mock", "receipt": asdict(receipt), "task": asdict(task),
                    "home_effects": core.mock_home_effects(), "checks": {
                        "duplicate_suppressed": True, "persisted_after_restart": True,
                        "stop_speech_kept_task_running": True, "outbox_retry_and_ack": True}}
        finally:
            core.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Home Jarvis allowlisted Home Assistant control; isolated Mock demo without text")
    parser.add_argument("text", nargs="?", help="明确文本指令；省略时运行隔离 demo")
    parser.add_argument("--db", default="home-jarvis.sqlite3")
    parser.add_argument("--session", default="local-cli")
    parser.add_argument("--utterance-id", default=None, help="重试必须复用同一 ID")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--home-config", help="私有 HA JSON 配置；默认 JARVIS_HOME_CONFIG 或 ~/.config/home-jarvis/home.json")
    modes.add_argument("--mock", action="store_true", help="显式使用离线 Mock，不访问 HA")
    parser.add_argument("--room", help="会话来源房间 ID；默认配置的 default_room")
    parser.add_argument("--speaker-config", help="用指定的私有 S12 配置播报回执；只确认设备接收，不代表播放完成")
    args = parser.parse_args()
    if args.speaker_config is not None and args.db == ":memory:":
        parser.error("--speaker-config requires a persistent --db")
    if args.text is None:
        if args.speaker_config is not None:
            parser.error("--speaker-config requires a text command")
        if args.home_config is not None:
            parser.error("--home-config requires a text command; omit it to run the isolated Mock demo")
        output = demo()
    else:
        try:
            speaker = XiaomiSpeaker(args.speaker_config) if args.speaker_config else None
            gateway = MockHomeGateway() if args.mock else configured_gateway(args.home_config)
            core = Core(args.db, gateway)
        except (OSError, ValueError, SpeakerError) as error:
            parser.error(str(error))
        try:
            room = args.room or (gateway.default_room if gateway.external else "living_room")
            core.open_session(args.session, room_id=room)
            output = asdict(core.submit(Utterance(args.session, args.utterance_id or str(uuid4()),
                                                "resident", "qwen3-asr", args.text)))
        except (ValueError, PermissionError) as error:
            parser.error(str(error))
        finally:
            core.close()
        if speaker is not None and output["results"]:
            stopping = any(item["intent"] == "stop_speech" for item in output["results"])
            output["speech"] = deliver_speech(args.db, speaker, output)
            if stopping:
                for item in output["results"]:
                    if item["intent"] == "stop_speech":
                        item["status"] = "stop_requested" if output["speech"]["status"] == "accepted" else "uncertain"
                        item["message"] = "暂停请求已接收，尚未确认播放已停止" if item["status"] == "stop_requested" else "暂停请求结果未确认"
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
