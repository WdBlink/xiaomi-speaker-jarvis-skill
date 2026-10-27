from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "xiaomi-speaker" / "login.py"


class FakeTimeout(Exception):
    pass


class FakeResponse:
    def __init__(self, value=None, *, content=None, status=200, cookies=None):
        self.status_code = status
        self.content = content if content is not None else ("&&&START&&&" + json.dumps(value)).encode()
        self.text = self.content.decode("utf-8", errors="replace")
        self.cookies = cookies or {}


class FakeCookies:
    def __init__(self):
        self.entries = []

    def set(self, name, value, **scope):
        self.entries.append((name, value, scope))


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.headers = {}
        self.cookies = FakeCookies()
        self.trust_env = True
        self.closed = False
        self.calls = []
        self.setup_calls = 0
        self.on_request = None

    def get(self, url, **kwargs):
        self.calls.append((url, deepcopy(kwargs)))
        path = urlsplit(url).path
        if path == "/pass/serviceLogin":
            self.setup_calls += 1
            if self.setup_calls > 1:
                raise AssertionError("QR login must not repeat serviceLogin")
            stage = "setup"
        else:
            stage = {
                "/longPolling/loginUrl": "qr", "/qr.png": "image", "/poll": "poll",
                "/sts": "token", "/admin/v2/device_list": "devices",
            }.get(path)
        if stage is None:
            raise AssertionError("Unexpected mock login endpoint")
        if self.on_request:
            self.on_request(stage)
        response = self.responses[stage]
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        self.now += 1
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class XiaomiLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.directory = self.root / ".config" / "home-jarvis"
        self.directory.mkdir(parents=True, mode=0o700)
        self.did = "111111111"
        self.session_file = self.directory / f"xiaomi-cloud-{self.did}.json"
        self.qr_file = self.directory / "xiaomi-login-qr.png"
        self.old_session = b'{"previous":"session"}\n'
        self.account_id = "333333333"
        self.pass_token = "fixture-pass-token-only"
        self.service_token = "fixture-service-token-only"
        self.ssecurity = "fixture-ssecurity-only"
        self.cloud_device_id = "fixture-cloud-speaker-id"
        self.raw_error = "fixture-private-exception-detail"
        self.sts_location = "https://api2.mina.mi.com/sts?ticket=fixture%2Fpart%2Bkeep&clientSign=already-signed"
        self.responses = {
            "setup": FakeResponse({"sid": "micoapi", "callback": "https://api2.mina.mi.com/sts",
                                   "qs": "%3Fsid%3Dmicoapi%26_json%3Dtrue"}),
            "qr": FakeResponse({"qr": "https://account.xiaomi.com/qr.png",
                                "lp": "https://ak.lp.account.xiaomi.com/poll", "timeout": 30}),
            "image": FakeResponse(content=b"\x89PNG\r\n\x1a\nfixture-image"),
            "poll": FakeResponse({"code": 0, "userId": self.account_id, "passToken": self.pass_token,
                                  "ssecurity": self.ssecurity, "location": self.sts_location}),
            "token": FakeResponse(content=b"ok", cookies={"serviceToken": self.service_token}),
            "devices": FakeResponse({"code": 0, "data": [self.device()]}),
        }
        self.session = FakeSession(self.responses)
        requests = SimpleNamespace(Session=Mock(return_value=self.session),
                                   exceptions=SimpleNamespace(Timeout=FakeTimeout))
        spec = importlib.util.spec_from_file_location("_xiaomi_login_under_test", SCRIPT)
        self.login = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"requests": requests}):
            spec.loader.exec_module(self.login)
        self.clock = FakeClock()
        self.login.time = SimpleNamespace(time=lambda: 1700000000,
                                         monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def device(self, **updates):
        return {"miotDID": self.did, "hardware": "S12", "deviceID": self.cloud_device_id, **updates}

    def preserve_old_session(self):
        self.session_file.write_bytes(self.old_session)
        self.session_file.chmod(0o600)

    def run_login(self):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            self.login.login(self.did, self.directory)

    def run_cli(self, expected_exit):
        with patch.object(self.login.Path, "home", return_value=self.root), \
                patch.object(sys, "argv", [str(SCRIPT), "--did", self.did]), \
                redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            with self.assertRaises(SystemExit) as caught:
                self.login.main()
        self.assertEqual(caught.exception.code, expected_exit)

    def configure_fallback(self, exchange_updates=None):
        exchange = {"code": 0, "userId": self.account_id, "nonce": 9223372036854775806,
                    "ssecurity": self.ssecurity,
                    "location": "https://api2.mina.mi.com/sts?ticket=fixture%2fpart%2Bkeep&auth=a+b"}
        exchange.update(exchange_updates or {})
        fresh = FakeSession({"setup": FakeResponse(exchange), "token": self.responses["token"],
                             "devices": self.responses["devices"]})
        self.responses["token"] = FakeResponse(content=self.raw_error.encode(), status=401)
        self.session.calls.clear()
        self.session.setup_calls = 0
        self.login.requests.Session.reset_mock()
        self.login.requests.Session.side_effect = [self.session, fresh]
        return fresh, exchange

    def assert_clean_failure(self):
        self.assertTrue(self.session.closed)
        self.assertFalse(self.qr_file.exists())
        self.assertEqual(self.session_file.read_bytes(), self.old_session)
        self.assertEqual(list(self.directory.glob(".xiaomi-*")), [])
        self.assert_no_secrets_printed()

    def assert_no_secrets_printed(self):
        text = self.stdout.getvalue() + self.stderr.getvalue()
        for value in (self.pass_token, self.service_token, self.ssecurity, self.account_id,
                      self.cloud_device_id, self.raw_error):
            self.assertFalse(value in text, "Private login detail appeared in output")
        self.assertNotIn("Traceback", text)

    def test_qr_exchange_device_validation_and_minimal_private_save(self):
        self.preserve_old_session()
        observed = []

        def inspect_stage(stage):
            if stage in {"poll", "token", "devices"}:
                self.assertTrue(self.qr_file.exists())
                self.assertEqual(stat.S_IMODE(self.qr_file.stat().st_mode), 0o600)
                self.assertEqual(self.session_file.read_bytes(), self.old_session)
                for path in self.directory.iterdir():
                    self.assertFalse(self.pass_token.encode() in path.read_bytes(), "passToken reached disk")
                observed.append(stage)

        self.session.on_request = inspect_stage
        self.run_login()
        self.assertEqual(observed, ["poll", "token", "devices"])
        saved = json.loads(self.session_file.read_text())
        self.assertEqual(saved, {"userId": self.account_id, "serviceToken": self.service_token,
                                 "deviceId": self.cloud_device_id, "miotDID": self.did, "hardware": "S12"})
        self.assertEqual(stat.S_IMODE(self.session_file.stat().st_mode), 0o600)
        self.assertNotIn(self.pass_token, self.session_file.read_text())
        self.assertNotIn(self.ssecurity, self.session_file.read_text())
        self.assertFalse(self.qr_file.exists())
        self.assertTrue(self.session.closed)
        self.assertFalse(self.session.trust_env)
        self.assertEqual(list(self.directory.glob(".xiaomi-*")), [])
        self.assert_no_secrets_printed()
        statuses = [json.loads(line)["status"] for line in self.stdout.getvalue().splitlines()]
        self.assertEqual(statuses, ["scan_required", "authenticated"])

    def test_requests_use_micoapi_and_do_not_follow_redirects(self):
        self.run_login()
        for url, kwargs in self.session.calls:
            self.assertTrue(url.startswith("https://"))
            self.assertIs(kwargs["allow_redirects"], False)
            self.assertGreater(kwargs["timeout"], 0)
            if urlsplit(url).path == "/pass/serviceLogin":
                self.assertEqual(kwargs["params"], {"sid": "micoapi", "_json": "true"})
        sts_url = next(url for url, _ in self.session.calls if urlsplit(url).path == "/sts")
        self.assertEqual(sts_url, self.sts_location)
        self.assertEqual(self.session.setup_calls, 1)
        _, device_request = self.session.calls[-1]
        self.assertEqual(device_request["cookies"], {"userId": self.account_id, "serviceToken": self.service_token})
        self.assertTrue(all(scope == {"domain": "account.xiaomi.com", "path": "/"}
                            for _, _, scope in self.session.cookies.entries))
        self.assertNotIn("passToken", [name for name, _, _ in self.session.cookies.entries])

    def test_qr_without_pass_token_nonce_or_ssecurity_completes_directly(self):
        self.responses["poll"] = FakeResponse({"userId": self.account_id, "location": self.sts_location})
        self.run_login()
        self.assertEqual(self.session.setup_calls, 1)
        self.assertEqual(json.loads(self.session_file.read_text())["deviceId"], self.cloud_device_id)
        self.assertEqual([urlsplit(url).path for url, _ in self.session.calls], [
            "/pass/serviceLogin", "/longPolling/loginUrl", "/qr.png", "/poll", "/sts", "/admin/v2/device_list",
        ])

    def test_finish_login_reuses_a_confirmed_ticket_without_starting_qr(self):
        authenticated = {"userId": self.account_id, "location": self.sts_location}
        with redirect_stdout(self.stdout):
            result = self.login.finish_login(self.session, authenticated, self.did, self.session_file)
        self.assertIsNone(result)
        self.assertEqual([urlsplit(url).path for url, _ in self.session.calls], ["/sts", "/admin/v2/device_list"])
        self.assertEqual(json.loads(self.session_file.read_text())["userId"], self.account_id)
        self.assertFalse(self.session.closed)
        self.assert_no_secrets_printed()

    def test_sts_401_exchanges_pass_token_once_in_a_clean_session(self):
        self.preserve_old_session()
        self.session.cookies.set("stale", "original-session-only")
        fresh, exchange = self.configure_fallback()

        def inspect_stage(stage):
            self.assertTrue(self.qr_file.exists())
            self.assertEqual(self.session_file.read_bytes(), self.old_session)
            for path in self.directory.iterdir():
                self.assertFalse(self.pass_token.encode() in path.read_bytes(), "passToken reached disk")

        fresh.on_request = inspect_stage
        self.run_login()
        self.assertEqual(self.login.requests.Session.call_count, 2)
        self.assertEqual([urlsplit(url).path for url, _ in fresh.calls], [
            "/pass/serviceLogin", "/sts", "/admin/v2/device_list",
        ])
        self.assertEqual(self.session.setup_calls, 1)
        self.assertEqual(fresh.setup_calls, 1)
        self.assertEqual(fresh.cookies.entries, [])
        self.assertFalse(fresh.trust_env)
        self.assertEqual(fresh.headers["User-Agent"], self.login.UA)
        _, request = fresh.calls[0]
        self.assertEqual(request["params"], {"sid": "micoapi", "_json": "true"})
        self.assertEqual(set(request["cookies"]), {"sdkVersion", "deviceId", "userId", "passToken"})
        self.assertEqual(request["cookies"]["passToken"], self.pass_token)
        self.assertEqual(request["cookies"]["userId"], self.account_id)
        self.assertEqual(request["cookies"]["sdkVersion"], "3.9")
        original_client = next(value for name, value, _ in self.session.cookies.entries if name == "deviceId")
        self.assertNotEqual(request["cookies"]["deviceId"], original_client)
        self.assertEqual(fresh.calls[1][0], exchange["location"] + "&clientSign=cD6rm2qLb6G0OX3CQqVhgs5lAmc%3D")
        self.assertTrue(all(kwargs["allow_redirects"] is False for _, kwargs in fresh.calls))
        self.assertEqual(fresh.calls[-1][1]["cookies"], {"userId": self.account_id, "serviceToken": self.service_token})
        self.assertEqual(json.loads(self.session_file.read_text()), {
            "userId": self.account_id, "serviceToken": self.service_token,
            "deviceId": self.cloud_device_id, "miotDID": self.did, "hardware": "S12",
        })
        self.assertEqual(stat.S_IMODE(self.session_file.stat().st_mode), 0o600)
        self.assertTrue(fresh.closed)
        self.assertTrue(self.session.closed)
        self.assertFalse(self.qr_file.exists())
        self.assert_no_secrets_printed()

    def test_sts_401_without_pass_token_does_not_retry_or_replace_session(self):
        self.responses["poll"] = FakeResponse({"userId": self.account_id, "location": self.sts_location})
        self.responses["token"] = FakeResponse(content=self.raw_error.encode(), status=401)
        self.preserve_old_session()
        self.run_cli(1)
        self.assertEqual(self.login.requests.Session.call_count, 1)
        self.assertEqual(sum(urlsplit(url).path == "/sts" for url, _ in self.session.calls), 1)
        self.assert_clean_failure()

    def test_direct_sts_timeout_does_not_start_a_fallback(self):
        self.responses["token"] = FakeTimeout(self.raw_error)
        self.preserve_old_session()
        self.run_cli(1)
        self.assertEqual(self.login.requests.Session.call_count, 1)
        self.assertEqual(sum(urlsplit(url).path == "/sts" for url, _ in self.session.calls), 1)
        self.assert_clean_failure()

    def test_fallback_rejects_invalid_account_service_and_signing_fields(self):
        variants = [{"code": 70016}, {"userId": "999"}, {"sid": "xiaomiio"},
                    {"callback": "https://account.xiaomi.com/sts"}, {"nonce": 1.5},
                    {"nonce": True}, {"nonce": None}, {"ssecurity": ""},
                    {"location": "https://evil.example/sts"},
                    {"location": "http://api2.mina.mi.com/sts"},
                    {"location": "https://api2.mina.mi.com/sts?clientSign=existing"}]
        for updates in variants:
            with self.subTest(fields=list(updates)):
                self.preserve_old_session()
                fresh, _ = self.configure_fallback(updates)
                self.run_cli(1)
                self.assertEqual([urlsplit(url).path for url, _ in fresh.calls], ["/pass/serviceLogin"])
                self.assertTrue(fresh.closed)
                self.assert_clean_failure()

    def test_fallback_sts_rejection_does_not_retry(self):
        self.preserve_old_session()
        fresh, _ = self.configure_fallback()
        fresh.responses["token"] = FakeResponse(content=self.raw_error.encode(), status=401)
        self.run_cli(1)
        self.assertEqual([urlsplit(url).path for url, _ in fresh.calls], ["/pass/serviceLogin", "/sts"])
        self.assertEqual(self.login.requests.Session.call_count, 2)
        self.assertTrue(fresh.closed)
        error = json.loads(self.stdout.getvalue().splitlines()[-1])
        self.assertEqual(error["stage"], "fallback_sts")
        self.assert_clean_failure()

    def test_fallback_transport_failure_is_sanitized_and_both_sessions_close(self):
        self.preserve_old_session()
        fresh, _ = self.configure_fallback()
        fresh.responses["setup"] = RuntimeError(" ".join((self.raw_error, self.pass_token, self.ssecurity)))
        self.run_cli(1)
        error = json.loads(self.stdout.getvalue().splitlines()[-1])
        self.assertEqual(error["stage"], "service_login_fallback")
        self.assertEqual(error["exception_class"], "RuntimeError")
        self.assertEqual(len(fresh.calls), 1)
        self.assertTrue(fresh.closed)
        self.assert_clean_failure()

    def test_interrupt_during_fallback_closes_both_sessions_and_removes_qr(self):
        self.preserve_old_session()
        fresh, _ = self.configure_fallback()
        fresh.responses["token"] = KeyboardInterrupt()
        self.run_cli(130)
        self.assertTrue(fresh.closed)
        self.assert_clean_failure()

    def test_fallback_still_requires_a_token_and_the_selected_speaker(self):
        variants = [("token", FakeResponse(content=b"ok")),
                    ("devices", FakeResponse({"code": 0, "data": [self.device(miotDID="999")]})),
                    ("devices", FakeResponse({"code": 0, "data": [self.device(hardware="LX04")]}))]
        for stage, response in variants:
            with self.subTest(stage=stage):
                self.preserve_old_session()
                self.responses["token"] = FakeResponse(content=b"ok", cookies={"serviceToken": self.service_token})
                fresh, _ = self.configure_fallback()
                fresh.responses[stage] = response
                self.run_cli(1)
                self.assertTrue(fresh.closed)
                self.assert_clean_failure()

    def test_s12a_and_integer_miot_did_are_supported(self):
        self.responses["devices"] = FakeResponse({"code": 0, "data": [self.device(hardware="S12A", miotDID=int(self.did))]})
        self.run_login()
        self.assertEqual(json.loads(self.session_file.read_text())["hardware"], "S12A")

    def test_nonofficial_nonhttps_and_credential_urls_are_rejected(self):
        for url in ("http://account.xiaomi.com/poll", "https://evil.example/poll",
                    "https://account.xiaomi.com.evil.example/poll", "https://other.lp.account.xiaomi.com/poll",
                    "https://account.xiaomi.com:444/poll", "https://user:password@account.xiaomi.com/poll",
                    "file:///tmp/token", "//account.xiaomi.com/poll"):
            with self.subTest(url=url), self.assertRaises(self.login.LoginError):
                self.login.official(url)
        self.assertEqual(self.session.calls, [])

    def test_actual_long_polling_host_is_allowed(self):
        url = "https://ak.lp.account.xiaomi.com/poll"
        self.assertEqual(self.login.official(url), url)

    def test_wrong_sid_or_callback_fails_before_qr_is_downloaded(self):
        for field, value in (("sid", "xiaomiio"), ("callback", "https://sts.api.io.mi.com/sts")):
            with self.subTest(field=field):
                setup = {"sid": "micoapi", "callback": "https://api2.mina.mi.com/sts", "qs": "fixture"}
                setup[field] = value
                self.responses["setup"] = FakeResponse(setup)
                self.session.setup_calls = 0
                self.session.calls.clear()
                self.preserve_old_session()
                with self.assertRaises(self.login.LoginError):
                    self.run_login()
                self.assertEqual(len(self.session.calls), 1)
                self.assert_clean_failure()

    def test_untrusted_qr_url_is_never_requested(self):
        self.responses["qr"] = FakeResponse({"qr": "https://evil.example/qr.png",
                                             "lp": "https://ak.lp.account.xiaomi.com/poll", "timeout": 30})
        self.preserve_old_session()
        with self.assertRaises(self.login.LoginError):
            self.run_login()
        self.assertEqual(len(self.session.calls), 2)
        self.assert_clean_failure()

    def test_untrusted_poll_url_is_rejected_and_qr_is_removed(self):
        self.responses["qr"] = FakeResponse({"qr": "https://account.xiaomi.com/qr.png",
                                             "lp": "http://ak.lp.account.xiaomi.com/poll", "timeout": 30})
        self.preserve_old_session()
        with self.assertRaises(self.login.LoginError):
            self.run_login()
        self.assertEqual(len(self.session.calls), 3)
        self.assert_clean_failure()

    def test_sts_location_requires_exact_official_service_and_path(self):
        for location in ("https://evil.example/sts", "http://api2.mina.mi.com/sts",
                         "https://account.xiaomi.com/sts", "https://api2.mina.mi.com/other"):
            with self.subTest(location=location):
                self.responses["poll"] = FakeResponse({"userId": self.account_id, "location": location})
                self.session.setup_calls = 0
                self.session.calls.clear()
                self.preserve_old_session()
                with self.assertRaises(self.login.LoginError):
                    self.run_login()
                self.assertFalse(any(urlsplit(url).path == "/sts" for url, _ in self.session.calls))
                self.assert_clean_failure()

    def test_missing_service_token_does_not_replace_existing_session(self):
        self.responses["token"] = FakeResponse(content=b"ok")
        self.preserve_old_session()
        with self.assertRaises(self.login.LoginError):
            self.run_login()
        self.assertFalse(any(urlsplit(url).path == "/admin/v2/device_list" for url, _ in self.session.calls))
        self.assert_clean_failure()

    def test_failed_qr_confirmation_does_not_replace_existing_session(self):
        self.responses["poll"] = FakeResponse({"code": 70016, "userId": self.account_id,
                                              "location": self.sts_location})
        self.preserve_old_session()
        with self.assertRaises(self.login.LoginError):
            self.run_login()
        self.assert_clean_failure()

    def test_wrong_did_hardware_duplicates_and_missing_cloud_id_are_rejected(self):
        variants = {
            "wrong_did": [self.device(miotDID="222222222")],
            "wrong_hardware": [self.device(hardware="LX04")],
            "duplicates": [self.device(), self.device()],
            "missing_cloud_id": [self.device(deviceID="")],
            "no_device": [],
        }
        for label, devices in variants.items():
            with self.subTest(case=label):
                self.responses["devices"] = FakeResponse({"code": 0, "data": devices})
                self.session.setup_calls = 0
                self.preserve_old_session()
                with self.assertRaises(self.login.LoginError):
                    self.run_login()
                self.assert_clean_failure()

    def test_device_list_authentication_failure_is_rejected(self):
        self.responses["devices"] = FakeResponse({"code": 401, "data": [self.device()]})
        self.preserve_old_session()
        with self.assertRaises(self.login.LoginError):
            self.run_login()
        self.assert_clean_failure()

    def test_http_errors_preserve_session_and_remove_qr(self):
        original = self.responses.copy()
        for stage, status in (("setup", 500), ("token", 403), ("devices", 401)):
            with self.subTest(stage=stage, status=status):
                self.responses.clear()
                self.responses.update(original)
                self.responses[stage] = FakeResponse(content=self.raw_error.encode(), status=status)
                self.session.setup_calls = 0
                self.preserve_old_session()
                with self.assertRaises(self.login.LoginError):
                    self.run_login()
                self.assert_clean_failure()
        self.assertEqual(self.login.requests.Session.call_count, 3)

    def test_poll_timeouts_expire_without_exchanging_credentials(self):
        self.responses["poll"] = FakeTimeout(self.raw_error)
        self.preserve_old_session()
        with self.assertRaisesRegex(self.login.LoginError, "expired"):
            self.run_login()
        self.assertEqual(self.session.setup_calls, 1)
        self.assert_clean_failure()

    def test_incomplete_poll_result_never_counts_as_authenticated(self):
        self.responses["poll"] = FakeResponse({"userId": self.account_id})
        self.preserve_old_session()
        with self.assertRaisesRegex(self.login.LoginError, "expired"):
            self.run_login()
        self.assertEqual(self.session.setup_calls, 1)
        self.assert_clean_failure()

    def test_main_sanitizes_transport_exceptions_and_removes_qr(self):
        self.responses["token"] = RuntimeError(" ".join((self.raw_error, self.pass_token, self.service_token)))
        self.preserve_old_session()
        self.run_cli(1)
        error = json.loads(self.stdout.getvalue().splitlines()[-1])
        self.assertEqual(error["status"], "error")
        self.assertEqual(error["stage"], "sts")
        self.assertEqual(error["exception_class"], "RuntimeError")
        self.assert_clean_failure()

    def test_missing_setup_field_has_safe_stage_and_exception_class(self):
        self.responses["setup"] = FakeResponse({"sid": "micoapi", "callback": "https://api2.mina.mi.com/sts"})
        self.preserve_old_session()
        self.run_cli(1)
        error = json.loads(self.stdout.getvalue().splitlines()[-1])
        self.assertEqual(error["stage"], "qr_request")
        self.assertEqual(error["exception_class"], "KeyError")
        self.assert_clean_failure()

    def test_interrupt_removes_qr_and_preserves_existing_session(self):
        self.responses["poll"] = KeyboardInterrupt()
        self.preserve_old_session()
        self.run_cli(130)
        self.assert_clean_failure()

    def test_atomic_save_failure_preserves_old_session_and_cleans_temporary_files(self):
        self.preserve_old_session()
        replace = self.login.os.replace

        def fail_session_replace(source, target):
            if Path(target).resolve() == self.session_file.resolve():
                raise OSError(self.raw_error)
            return replace(source, target)

        with patch.object(self.login.os, "replace", side_effect=fail_session_replace):
            self.run_cli(1)
        self.assert_clean_failure()

    def test_invalid_qr_image_does_not_authenticate(self):
        self.responses["image"] = FakeResponse(content=b"<html>not a QR image</html>")
        self.preserve_old_session()
        with self.assertRaises(self.login.LoginError):
            self.run_login()
        self.assert_clean_failure()

    def test_malformed_or_oversized_responses_are_rejected(self):
        for response in (FakeResponse(content=b"not JSON"), FakeResponse(value=[]),
                         FakeResponse(content=b" " * (1024 * 1024 + 1))):
            self.responses["setup"] = response
            self.session.setup_calls = 0
            self.preserve_old_session()
            with self.assertRaises(self.login.LoginError):
                self.run_login()
            self.assert_clean_failure()

    def test_private_write_refuses_a_git_checkout(self):
        checkout = self.root / "checkout"
        (checkout / ".git").mkdir(parents=True)
        with self.assertRaises(self.login.LoginError):
            self.login.private_write(checkout / "private.json", b"fixture")
        self.assertFalse((checkout / "private.json").exists())

    def test_private_write_refuses_a_symlink_into_a_git_checkout(self):
        checkout = self.root / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / "private").mkdir()
        alias = self.root / "private-alias"
        alias.symlink_to(checkout / "private", target_is_directory=True)
        with self.assertRaises(self.login.LoginError):
            self.login.private_write(alias / "session.json", b"fixture")
        self.assertFalse((checkout / "private/session.json").exists())


if __name__ == "__main__":
    unittest.main()
