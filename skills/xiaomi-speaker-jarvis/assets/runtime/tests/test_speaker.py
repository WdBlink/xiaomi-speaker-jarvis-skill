from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from home_jarvis.speaker import MAX_TEXT_LENGTH, SpeakerError, XiaomiSpeaker


class SpeakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "speaker.json"
        self.config = {
            "localip": "192.0.2.10", "did": "111111111", "mac": "02:00:00:00:00:01",
            "model": "xiaomi.wifispeaker.s12", "token": "0111111111abcdef" * 2,
        }
        self.save_config()
        self.info = {"model": self.config["model"], "mac": self.config["mac"]}
        self.device = Mock()
        self.factory = Mock(return_value=self.device)
        self.miio = patch.dict(sys.modules, {"miio": SimpleNamespace(Device=self.factory)})
        self.miio.start()
        self.addCleanup(self.miio.stop)

    def save_config(self):
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def assert_safe_error(self, function, *args):
        try:
            function(*args)
        except SpeakerError as error:
            rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            for secret in (self.config["token"], self.config["localip"], "sensitive-original-error"):
                self.assertNotIn(secret, rendered)
            return str(error)
        self.fail("SpeakerError was not raised")

    def test_tts_is_a_single_request_after_identity_check(self):
        self.device.send.side_effect = [self.info, ["ok"]]
        speaker = XiaomiSpeaker(self.path)
        self.factory.assert_not_called()
        self.assertEqual(speaker.speak("这是本地播报。"), "accepted")
        self.assertEqual(self.device.send.call_args_list, [
            call("miIO.info", [], retry_count=0), call("tts", ["这是本地播报。"], retry_count=0),
        ])
        self.assertEqual(self.factory.call_args.kwargs["token"], self.config["token"])
        self.assertIs(self.factory.call_args.kwargs.get("lazy_discover"), False)
        self.assertGreaterEqual(self.factory.call_args.kwargs["start_id"], 1)
        self.assertLess(self.factory.call_args.kwargs["start_id"], 9999)
        self.assertNotIn(self.config["token"], repr(speaker._config))

    def test_stop_uses_pause_without_tts_and_requires_ok(self):
        for reply in (["ok"], ["error"], None):
            with self.subTest(reply=reply):
                self.device.reset_mock()
                self.device.send.side_effect = [self.info, reply]
                speaker = XiaomiSpeaker(self.path)
                if reply == ["ok"]:
                    self.assertEqual(speaker.stop(), "accepted")
                else:
                    self.assert_safe_error(speaker.stop)
                self.assertEqual(self.device.send.call_args_list, [
                    call("miIO.info", [], retry_count=0), call("play_pause", [], retry_count=0),
                ])

    def test_device_is_reused_and_identity_is_rechecked_for_each_command(self):
        self.device.send.side_effect = [self.info, ["ok"], self.info, ["ok"], self.info, ["ok"]]
        speaker = XiaomiSpeaker(self.path)
        speaker.speak("第一句")
        speaker.speak("第二句")
        speaker.stop()
        self.factory.assert_called_once()
        self.assertEqual(self.device.send.call_args_list[::2], [call("miIO.info", [], retry_count=0)] * 3)

    def test_cached_protocol_refreshes_handshake_after_idle_without_replaying_tts(self):
        self.miio.stop()
        try:
            from miio import Device
            from miio.miioprotocol import MiIOProtocol
            from miio.protocol import Message
        except ImportError:
            self.skipTest("Optional python-miio is not installed")

        now = datetime(2026, 1, 1)
        events, requests = [], []
        fail_tts = False
        token = bytes.fromhex(self.config["token"])
        info = self.info

        def handshake(_address):
            events.append("handshake")
            return SimpleNamespace(
                header=SimpleNamespace(value=SimpleNamespace(device_id=b"\x00\x00\x00\x01", ts=now)),
                checksum=b"\x00" * 16,
            )

        class Socket:
            def settimeout(self, _timeout):
                pass

            def sendto(self, payload, _address):
                self.packet = Message.parse(payload, token=token)
                request = self.packet.data.value
                events.append(request["method"])
                requests.append(request)

            def recvfrom(self, _size):
                request = self.packet.data.value
                if self.packet.header.value.ts != now + timedelta(seconds=1):
                    raise TimeoutError("synthetic stale timestamp")
                if fail_tts and request["method"] == "tts":
                    raise TimeoutError("synthetic uncertain TTS")
                reply = Message.build({
                    "data": {"value": {"id": request["id"], "result": info if request["method"] == "miIO.info" else ["ok"]}},
                    "header": {"value": {"length": 0, "unknown": 0, "device_id": b"\x00\x00\x00\x01", "ts": now}},
                    "checksum": 0,
                }, token=token)
                return reply, ("192.0.2.10", 54321)

        with patch("miio.Device", wraps=Device) as factory, \
                patch.object(MiIOProtocol, "discover", side_effect=handshake), \
                patch("miio.miioprotocol.socket.socket", side_effect=lambda *_args: Socket()), \
                patch("miio.miioprotocol._LOGGER.error"):
            speaker = XiaomiSpeaker(self.path)
            self.assertEqual(speaker.speak("首次"), "accepted")
            now += timedelta(hours=24)
            self.assertEqual(speaker.speak("空闲后"), "accepted")
            fail_tts = True
            self.assertIn("unknown", self.assert_safe_error(speaker.speak, "超时"))
        factory.assert_called_once()
        self.assertEqual(events, ["handshake", "miIO.info", "handshake", "tts"] * 3)
        self.assertEqual([r["params"] for r in requests if r["method"] == "tts"],
                         [["首次"], ["空闲后"], ["超时"]])
        self.assertEqual(len({r["id"] for r in requests}), 6)

    def test_mismatched_or_missing_identity_prevents_playback(self):
        for info in ({**self.info, "model": "xiaomi.wifispeaker.v2"},
                     {**self.info, "mac": "02:00:00:00:00:02"}, {}, None, [self.info]):
            with self.subTest(info=info):
                self.device.reset_mock()
                self.device.send.side_effect = [info]
                self.assert_safe_error(XiaomiSpeaker(self.path).speak, "不能播放")
                self.device.send.assert_called_once_with("miIO.info", [], retry_count=0)

    def test_mac_comparison_is_case_insensitive(self):
        self.device.send.side_effect = [{**self.info, "mac": self.info["mac"].lower()}, ["ok"]]
        self.assertEqual(XiaomiSpeaker(self.path).speak("测试"), "accepted")

    def test_identity_error_is_sanitized_and_never_sends_tts(self):
        self.device.send.side_effect = TimeoutError("sensitive-original-error " + self.config["token"])
        self.assert_safe_error(XiaomiSpeaker(self.path).speak, "不能播放")
        self.device.send.assert_called_once_with("miIO.info", [], retry_count=0)

    def test_tts_and_pause_timeouts_are_uncertain_and_never_retried(self):
        for method, params in (("tts", ["测试"]), ("play_pause", [])):
            with self.subTest(method=method):
                self.device.reset_mock()
                self.device.send.side_effect = [self.info, TimeoutError(
                    "sensitive-original-error " + self.config["token"] + self.config["localip"])]
                speaker = XiaomiSpeaker(self.path)
                message = self.assert_safe_error(speaker.speak, "测试") if method == "tts" else self.assert_safe_error(speaker.stop)
                self.assertIn("unknown", message)
                self.assertEqual(self.device.send.call_args_list, [
                    call("miIO.info", [], retry_count=0), call(method, params, retry_count=0),
                ])

    def test_only_exact_ok_list_is_accepted(self):
        for reply in (None, "ok", {"result": ["ok"]}, ["OK"], ["ok", "ok"], [], True,
                      {"error": self.config["token"]}):
            with self.subTest(reply=reply):
                self.device.reset_mock()
                self.device.send.side_effect = [self.info, reply]
                self.assert_safe_error(XiaomiSpeaker(self.path).speak, "测试")
                self.assertEqual(self.device.send.call_count, 2)

    def test_invalid_text_does_not_initialize_connection(self):
        speaker = XiaomiSpeaker(self.path)
        for text in ("", " \n\t", None, 12, "a\0b", "a" * (MAX_TEXT_LENGTH + 1), "\ud800"):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                speaker.speak(text)
        self.factory.assert_not_called()

    def test_maximum_text_length_is_accepted(self):
        text = "中" * MAX_TEXT_LENGTH
        self.device.send.side_effect = [self.info, ["ok"]]
        self.assertEqual(XiaomiSpeaker(self.path).speak(text), "accepted")
        self.device.send.assert_called_with("tts", [text], retry_count=0)

    def test_private_regular_file_is_required(self):
        for mode in (0o644, 0o640, 0o604, 0o400, 0o700):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                self.assert_safe_error(XiaomiSpeaker, self.path)
        self.assert_safe_error(XiaomiSpeaker, self.path.parent)
        self.assert_safe_error(XiaomiSpeaker, self.path.with_name("missing"))
        self.factory.assert_not_called()

    def test_symbolic_link_is_rejected(self):
        link = self.path.with_name("link")
        link.symlink_to(self.path)
        self.assert_safe_error(XiaomiSpeaker, link)
        self.factory.assert_not_called()

    def test_wrong_owner_is_rejected(self):
        info = self.path.stat()
        with patch("home_jarvis.speaker.os.fstat", return_value=SimpleNamespace(
                st_mode=info.st_mode, st_uid=os.getuid() + 1, st_size=info.st_size)):
            self.assert_safe_error(XiaomiSpeaker, self.path)

    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.path.with_name("fifo")
        os.mkfifo(fifo, 0o600)
        code = """from home_jarvis.speaker import XiaomiSpeaker, SpeakerError
import sys
try:
    XiaomiSpeaker(sys.argv[1])
except SpeakerError:
    sys.exit(0)
sys.exit(1)
"""
        result = subprocess.run([sys.executable, "-c", code, str(fifo)], capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_oversized_and_malformed_json_are_rejected(self):
        for data in (b" " * 4097, b"\xff", b"{", b"[]", b"null", b'{"token":"a","token":"b"}',
                     b"[" * 1500 + b"]" * 1500):
            with self.subTest(data=data[:20]):
                self.path.write_bytes(data)
                self.assert_safe_error(XiaomiSpeaker, self.path)
        self.factory.assert_not_called()

    def test_missing_unknown_and_invalid_fields_are_rejected(self):
        original = self.config.copy()
        invalid = {
            "localip": (None, 1234, "localhost", "192.0.2.999", "::1", "0.0.0.0", "224.0.0.1"),
            "did": (None, True, 0, -1, "0", "1.0", " 12", 0x100000000),
            "mac": (None, "not-a-mac", "02-00-00-00-00-01", "01:00:00:00:00:01", "00:00:00:00:00:00"),
            "model": (None, "xiaomi.wifispeaker.v2"), "token": (None, "", "g" * 32, "a" * 31),
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.config = {**original, field: value}
                    self.save_config()
                    with self.assertRaises(SpeakerError):
                        XiaomiSpeaker(self.path)
        for config in ({k: v for k, v in original.items() if k != "token"}, {**original, "extra": "value"}):
            self.config = config
            self.save_config()
            with self.assertRaises(SpeakerError):
                XiaomiSpeaker(self.path)
        self.factory.assert_not_called()

    def test_integer_device_id_is_accepted(self):
        self.config["did"] = 111111111
        self.save_config()
        XiaomiSpeaker(self.path)

    def test_known_metadata_is_accepted_without_affecting_connection(self):
        self.config.update(name="Mi AI Speaker", region="cn")
        self.save_config()
        self.device.send.side_effect = [self.info, ["ok"]]
        self.assertEqual(XiaomiSpeaker(self.path).speak("测试"), "accepted")
        self.assertEqual(self.factory.call_args.kwargs["ip"], self.config["localip"])
        self.assertEqual(self.factory.call_args.kwargs["model"], self.config["model"])
        self.assertNotIn("name", self.factory.call_args.kwargs)
        self.assertNotIn("region", self.factory.call_args.kwargs)
        self.assertEqual(self.device.send.call_args_list, [
            call("miIO.info", [], retry_count=0), call("tts", ["测试"], retry_count=0),
        ])

    def test_optional_dependency_is_only_required_when_sending(self):
        with patch.dict(sys.modules, {"miio": None}):
            speaker = XiaomiSpeaker(self.path)
            self.assertIn("python-miio==0.5.12", self.assert_safe_error(speaker.speak, "测试"))

    def test_connection_initialization_error_is_sanitized(self):
        self.factory.side_effect = RuntimeError("sensitive-original-error " + self.config["token"])
        self.assert_safe_error(XiaomiSpeaker(self.path).speak, "测试")
        self.device.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
