import json
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
import urllib.error
import urllib.parse
from unittest.mock import Mock, patch

from home_jarvis.quiet_speaker import QuietSpeaker, _Playback, _PlayerClient
from home_jarvis.speaker import SpeakerError
from home_jarvis.xiaomi_cloud import CloudError


class FakePlayer:
    miot_did = "111111111"

    def __init__(self):
        self.events = []
        self.volume = 16
        self.status = 0
        self.status_error = False
        self.volume_error = False

    def verify_device(self):
        self.events.append("verify")

    def set_volume(self, volume):
        self.events.append(("volume", volume))
        if self.volume_error:
            raise RuntimeError("secret-from-device")
        self.volume = volume

    def get_status(self):
        self.events.append(("status", self.status, self.volume))
        if self.status_error:
            raise RuntimeError("secret-from-device")
        return _Playback(self.status, self.volume)


class FakeSpeaker:
    miot_did = "111111111"

    def __init__(self, player):
        self.player = player
        self.tts_error = False
        self.stop_error = False

    def stop(self):
        self.player.events.append("pause")
        if self.stop_error:
            raise RuntimeError("secret-from-device")
        self.player.status = 0
        return "accepted"

    def speak(self, text):
        self.player.events.append(("tts", text))
        if self.tts_error:
            raise RuntimeError("secret-from-device")
        return "accepted"


class QuietSpeakerTests(unittest.TestCase):
    def setUp(self):
        self.player = FakePlayer()
        self.local = FakeSpeaker(self.player)
        self.now = 100.0
        self.thread = Mock()
        self.thread.is_alive.return_value = False
        for target, values in (
            ("home_jarvis.quiet_speaker._PlayerClient", {"return_value": self.player}),
            ("home_jarvis.quiet_speaker.Thread", {"return_value": self.thread}),
            ("home_jarvis.quiet_speaker.time.monotonic", {"side_effect": lambda: self.now}),
            ("home_jarvis.quiet_speaker.time.sleep", {"side_effect": self.advance}),
        ):
            patched = patch(target, **values)
            patched.start()
            self.addCleanup(patched.stop)

    def advance(self, seconds):
        self.now += seconds

    def build(self, **kwargs):
        return QuietSpeaker(self.local, "unused-test-config", **kwargs)

    def assert_safe_error(self, function, *args):
        try:
            function(*args)
        except SpeakerError as error:
            rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            self.assertNotIn("secret-from-device", rendered)
            return str(error)
        self.fail("SpeakerError was not raised")

    def test_startup_verifies_identity_and_zero_readback_before_watcher(self):
        speaker = self.build()
        self.assertEqual(speaker.miot_did, self.local.miot_did)
        self.assertEqual(self.player.events, ["verify", ("volume", 0), ("status", 0, 0)])
        self.assertEqual(speaker.state, "idle")
        self.thread.start.assert_called_once()

    def test_identity_mismatch_or_cloud_verification_failure_never_changes_volume(self):
        self.player.miot_did = "different"
        self.assert_safe_error(self.build)
        self.assertEqual(self.player.events, [])
        self.player.miot_did = self.local.miot_did
        self.player.verify_device = Mock(side_effect=RuntimeError("secret-from-device"))
        self.assert_safe_error(self.build)
        self.assertEqual(self.player.events, [])

    def test_startup_failure_restores_reply_volume_without_returned_adapter(self):
        self.thread.start.side_effect = RuntimeError("secret-from-device")
        self.assert_safe_error(self.build)
        self.assertEqual(self.player.volume, 16)
        self.assertEqual(self.player.events[-2:], [("volume", 16), ("status", 0, 16)])

    def test_startup_failed_readback_attempts_restoration_and_reports_failure(self):
        self.player.status_error = True
        error = self.assert_safe_error(self.build)
        self.assertEqual(self.player.volume, 16)
        self.assertIn("Restoring reply volume also failed", error)

    def test_initialization_interrupt_restores_volume_and_preserves_interrupt(self):
        original_status = self.player.get_status
        for error in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(error).__name__):
                self.player.get_status = Mock(side_effect=[error, _Playback(0, 16)])
                with self.assertRaises(type(error)):
                    self.build()
                self.assertEqual(self.player.volume, 16)
                self.assertEqual(self.player.events[-1], ("volume", 16))
        self.player.get_status = original_status

    def test_initialization_waits_for_asynchronous_zero_readback(self):
        self.player.get_status = Mock(side_effect=[_Playback(0, 16), _Playback(0, 0)])
        speaker = self.build()
        self.assertEqual(speaker.state, "idle")
        self.assertEqual(self.player.get_status.call_count, 2)
        self.assertGreater(self.now, 100)

    def test_reply_stops_old_audio_at_zero_before_restoring_volume_and_tts(self):
        speaker = self.build()
        self.player.events.clear()
        self.player.status = 1
        self.assertEqual(speaker.speak("回答"), "accepted")
        self.assertEqual(self.player.events, [
            ("volume", 0), "pause", ("status", 0, 0),
            ("volume", 16), ("status", 0, 16), ("tts", "回答"),
        ])
        self.assertEqual(speaker.state, "starting")
        self.assertEqual(self.player.volume, 16)

    def test_initial_idle_is_not_completion_then_playing_to_idle_remutes(self):
        speaker = self.build()
        speaker.speak("回答")
        speaker._monitor_once()
        self.assertEqual((speaker.state, self.player.volume), ("starting", 16))
        self.player.status = 1
        speaker._monitor_once()
        self.assertEqual(speaker.state, "playing")
        self.player.status = 0
        speaker._monitor_once()
        self.assertEqual((speaker.state, self.player.volume), ("idle", 0))

    def test_startup_and_total_play_deadlines_fail_closed_without_tts_replay(self):
        for playing, timeout in ((False, 8), (True, 120)):
            with self.subTest(playing=playing):
                self.player.events.clear()
                speaker = self.build()
                speaker.speak("回答")
                if playing:
                    self.player.status = 1
                    speaker._monitor_once()
                self.advance(timeout)
                speaker._monitor_once()
                self.assertEqual((speaker.state, self.player.volume), ("fault", 0))
                self.assertIn("timed out", speaker.last_error)
                self.assertEqual(self.player.events.count(("tts", "回答")), 1)
                speaker.close()

    def test_failed_tts_is_not_retried_and_remutes(self):
        speaker = self.build()
        self.local.tts_error = True
        self.assert_safe_error(speaker.speak, "回答")
        self.assertEqual((speaker.state, self.player.volume), ("fault", 0))
        self.assertEqual(self.player.events.count(("tts", "回答")), 1)
        self.assertTrue(speaker.errors)

    def test_failed_monitor_attempts_zero_and_retains_error_after_recovery(self):
        speaker = self.build()
        speaker.speak("回答")
        self.player.status_error = True
        speaker._monitor_once()
        self.assertEqual((speaker.state, self.player.volume), ("fault", 0))
        self.assertIn("may be audible", speaker.last_error)
        self.assertNotIn("secret-from-device", repr(speaker.errors))
        old_error = speaker.last_error
        self.player.status_error = False
        speaker._monitor_once()
        self.assertEqual(speaker.state, "idle")
        self.assertEqual(speaker.last_error, old_error)

    def test_unknown_player_state_faults_and_attempts_mute(self):
        speaker = self.build()
        speaker.speak("回答")
        self.player.get_status = Mock(side_effect=[
            SpeakerError("Xiaomi playback state is unknown"), _Playback(0, 0),
        ])
        speaker._monitor_once()
        self.assertEqual((speaker.state, self.player.volume), ("fault", 0))
        self.assertTrue(speaker.last_error)

    def test_real_background_watcher_remutes_after_observed_playback(self):
        playing_seen, remuted = threading.Event(), threading.Event()
        original_status, original_volume = self.player.get_status, self.player.set_volume

        def status():
            result = original_status()
            if result.status == 1:
                playing_seen.set()
            return result

        def volume(value):
            original_volume(value)
            if value == 0 and playing_seen.is_set():
                remuted.set()

        self.player.get_status, self.player.set_volume = status, volume
        with patch("home_jarvis.quiet_speaker.Thread", threading.Thread), \
                patch("home_jarvis.quiet_speaker._ACTIVE_INTERVAL", 0.01):
            speaker = self.build()
            self.addCleanup(speaker.close)
            speaker.speak("异步回答")
            self.player.status = 1
            self.assertTrue(playing_seen.wait(2))
            self.player.status = 0
            self.assertTrue(remuted.wait(2))
            speaker.close()
            self.assertEqual(speaker.state, "closed")
            self.assertEqual(self.player.volume, 16)
            self.assertFalse(speaker._thread.is_alive())

    def test_idle_reasserts_zero_if_physical_volume_was_changed(self):
        speaker = self.build()
        self.player.volume = 30
        self.player.status = 1
        speaker._monitor_once()
        self.assertEqual((speaker.state, self.player.volume), ("idle", 0))

    def test_stop_cancels_active_turn_and_tries_pause_even_if_cloud_fails(self):
        speaker = self.build()
        speaker.speak("回答")
        self.assertEqual(speaker.stop(), "accepted")
        self.assertEqual((speaker.state, self.player.volume), ("idle", 0))
        self.player.volume_error = True
        self.player.events.clear()
        self.assert_safe_error(speaker.stop)
        self.assertIn("pause", self.player.events)
        self.assertEqual(speaker.state, "fault")

    def test_concurrent_stop_cancels_preparing_reply_before_tts(self):
        speaker = self.build()
        entered, release = threading.Event(), threading.Event()
        original_pause = self.local.stop

        def blocked_pause():
            entered.set()
            if not release.wait(2):
                raise AssertionError("test pause not released")
            return original_pause()

        self.local.stop = blocked_pause
        results = []

        def speak():
            try:
                speaker.speak("不得播报")
            except SpeakerError:
                results.append("cancelled")

        reply = threading.Thread(target=speak)
        reply.start()
        self.assertTrue(entered.wait(2))
        stopping = threading.Thread(target=lambda: results.append(speaker.stop()))
        stopping.start()
        self.assertTrue(speaker._stop_requested.wait(2))
        release.set()
        reply.join(2)
        stopping.join(2)
        self.assertFalse(reply.is_alive())
        self.assertFalse(stopping.is_alive())
        self.assertCountEqual(results, ["cancelled", "accepted"])
        self.assertNotIn(("tts", "不得播报"), self.player.events)
        self.assertEqual((speaker.state, self.player.volume), ("idle", 0))

    def test_watcher_holds_no_stale_turn_after_stop_and_new_reply(self):
        speaker = self.build()
        speaker.speak("第一句")
        self.player.status = 1
        speaker._monitor_once()
        speaker.stop()
        speaker.speak("第二句")
        speaker._monitor_once()
        self.assertEqual((speaker.state, self.player.volume), ("starting", 16))

    def test_close_restores_volume_and_stale_watcher_cannot_remute(self):
        speaker = self.build(reply_volume=22)
        speaker.speak("回答")
        speaker.close()
        self.assertEqual((speaker.state, self.player.volume), ("closed", 22))
        before = list(self.player.events)
        speaker._monitor_once()
        speaker.close()
        self.assertEqual(self.player.events, before)
        self.assert_safe_error(speaker.speak, "不得播报")
        self.thread.join.assert_called_once()

    def test_close_failure_is_observable_and_not_reported_as_success_on_retry(self):
        speaker = self.build()
        self.player.volume_error = True
        self.assertIn("restoring", self.assert_safe_error(speaker.close))
        self.assertEqual(speaker.state, "closed")
        self.assertIn("restoring", speaker.last_error)
        self.assert_safe_error(speaker.close)

    def test_invalid_text_or_reply_volume_has_no_device_effect(self):
        for value in (0, 101, True, 1.0, "16"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.build(reply_volume=value)
        speaker = self.build()
        self.player.events.clear()
        for text in (None, "", " \n", "a\x00b", "\ud800", "中" * 501):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                speaker.speak(text)
        self.assertEqual(self.player.events, [])


class PlayerClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "cloud.json"
        self.path.write_text(json.dumps({
            "userId": "test-user", "serviceToken": "test-private-token",
            "deviceId": "test-cloud-device", "miotDID": "111111111", "hardware": "S12A",
        }))
        self.path.chmod(0o600)
        self.client = _PlayerClient(self.path)

    def fake_response(self, body, *, url="https://api2.mina.mi.com/remote/ubus", status=200):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = status
        response.geturl.return_value = url
        response.read.return_value = json.dumps(body).encode() if not isinstance(body, bytes) else body
        opener = Mock()
        opener.open.return_value = response
        patched = patch("home_jarvis.quiet_speaker.urllib.request.build_opener", return_value=opener)
        factory = patched.start()
        self.addCleanup(patched.stop)
        return factory, opener

    def test_fixed_official_endpoint_airplay_volume_and_no_proxy_redirect(self):
        factory, opener = self.fake_response({"code": 0, "data": {"code": 0}})
        self.client.set_volume(0)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api2.mina.mi.com/remote/ubus")
        self.assertEqual(request.method, "POST")
        params = urllib.parse.parse_qs(request.data.decode())
        self.assertEqual(params["path"], ["mediaplayer"])
        self.assertEqual(params["method"], ["player_set_volume"])
        self.assertEqual(json.loads(params["message"][0]), {"media": "airplay", "volume": 0})
        self.assertEqual(params["deviceId"], ["test-cloud-device"])
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsNone(factory.call_args.args[1].redirect_request(None, None, 302, "", {}, "https://evil.test"))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)

    def test_real_status_shape_and_unknown_states(self):
        for info in ({"status": 1, "volume": 16, "loop_type": 1},
                     json.dumps({"status": 0, "volume": 0, "loop_type": 1})):
            _, opener = self.fake_response({"code": 0, "data": {"code": 0, "info": info}})
            self.assertIsInstance(self.client.get_status(), _Playback)
            payload = urllib.parse.parse_qs(opener.open.call_args.args[0].data.decode())
            self.assertEqual(json.loads(payload["message"][0]), {"media": "app_ios"})
        for info in ({"status": 2, "volume": 16}, {"status": True, "volume": 16},
                     {"status": 0, "volume": -1}, {"status": 0, "volume": "0"}, {}, "not-json"):
            with self.subTest(info=info):
                self.fake_response({"code": 0, "data": {"code": 0, "info": info}})
                with self.assertRaises(SpeakerError):
                    self.client.get_status()

    def test_bad_responses_and_redirects_are_rejected(self):
        for body in ({"code": 1}, {"code": 0, "data": {"code": -1}},
                     {"code": False, "data": {"code": 0}}, {"code": 0, "data": {}},
                     b"x" * 16385, b"not-json"):
            with self.subTest(body=str(body)[:50]):
                self.fake_response(body)
                with self.assertRaises(SpeakerError):
                    self.client.set_volume(0)
        for url, status in (("https://evil.test", 200), ("https://api2.mina.mi.com/remote/ubus", 302)):
            self.fake_response({"code": 0, "data": {"code": 0}}, url=url, status=status)
            with self.assertRaises(SpeakerError):
                self.client.set_volume(0)

    def test_http_auth_timeout_errors_never_leak_credentials(self):
        for error in (TimeoutError("test-private-token"), urllib.error.HTTPError(
                "https://api2.mina.mi.com/remote/ubus?test-private-token", 401,
                "test-private-token", {}, None)):
            _, opener = self.fake_response({})
            opener.open.side_effect = error
            try:
                self.client.set_volume(0)
            except SpeakerError as safe:
                rendered = "".join(traceback.format_exception(type(safe), safe, safe.__traceback__))
                self.assertNotIn("test-private-token", rendered)
            else:
                self.fail("SpeakerError was not raised")
            self.assertEqual(opener.open.call_count, 1)

    def test_existing_secure_config_reader_is_used(self):
        self.path.chmod(0o644)
        with self.assertRaises(CloudError):
            _PlayerClient(self.path)
        self.path.chmod(0o600)
        link = self.path.with_name("link")
        link.symlink_to(self.path)
        with self.assertRaises(CloudError):
            _PlayerClient(link)


if __name__ == "__main__":
    unittest.main()
