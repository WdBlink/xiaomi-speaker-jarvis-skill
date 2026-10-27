import json
from pathlib import Path
import tempfile
import time
import traceback
import unittest
import urllib.parse
from unittest.mock import patch

from home_jarvis.xiaomi_cloud import CloudError, ConversationRecord, XiaomiCloud


USER_ID = "222222222"
SERVICE_TOKEN = "test-secret-token"
DEVICE_ID = "fixture-cloud-speaker-id"
MIOT_DID = "111111111"
HARDWARE = "S12A"


class XiaomiCloudTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "xiaomi-cloud.json"
        self.write_config()

    def write_config(self):
        self.path.write_text(json.dumps({
            "userId": USER_ID,
            "serviceToken": SERVICE_TOKEN,
            "deviceId": DEVICE_ID,
            "miotDID": MIOT_DID,
            "hardware": HARDWARE,
        }), encoding="utf-8")
        self.path.chmod(0o600)

    def device_list(self, *rows):
        return {"code": 0, "data": list(rows or ({
            "miotDID": MIOT_DID, "deviceID": DEVICE_ID, "hardware": HARDWARE,
        },))}

    @staticmethod
    def record(request_id="request-1", query="几点了？", when=None, **overrides):
        result = {
            "requestId": request_id,
            "time": int(time.time() * 1000) if when is None else when,
            "query": query,
            "answers": [{"text": "这是答案，不是用户输入"}],
            "bitSet": 0,
            "illegalContent": False,
        }
        result.update(overrides)
        return result

    @staticmethod
    def conversation_result(*records):
        return {
            "code": 0,
            "message": "success",
            "data": json.dumps({
                "bitSet": 0,
                "nextEndTime": int(time.time() * 1000),
                "records": list(records),
            }),
        }

    def fake_requests(self, responses):
        calls = []

        def request(_client, method, path, params=None, *, include_device_id=False):
            calls.append((method, path, params, include_device_id))
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        mock = patch.object(XiaomiCloud, "_request", request)
        mock.start()
        self.addCleanup(mock.stop)
        return calls

    def assert_safe_error(self, function, *args):
        try:
            function(*args)
        except CloudError as error:
            rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            self.assertNotIn(SERVICE_TOKEN, rendered)
            self.assertNotIn(USER_ID, rendered)
            return error
        self.fail("CloudError was not raised")

    def test_conversation_shape_returns_query_and_stable_id(self):
        calls = self.fake_requests([
            self.device_list(),
            self.conversation_result(self.record()),
        ])
        client = XiaomiCloud(self.path)
        records = client.latest_records()

        self.assertEqual(client.miot_did, MIOT_DID)
        self.assertNotIn(SERVICE_TOKEN, repr(client._config))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].record_id, f"{MIOT_DID}:request-1")
        self.assertEqual(records[0].text, "几点了？")
        self.assertEqual(records[0].time_ms, self.fake_requests_time(records[0].time_ms))
        self.assertEqual(calls[0][:2], ("GET", "/admin/v2/device_list"))
        self.assertEqual(calls[1][:2], ("GET", "/device_profile/v2/conversation"))
        self.assertEqual(calls[1][2]["source"], "dialogu")
        self.assertEqual(calls[1][2]["hardware"], HARDWARE)
        self.assertEqual(calls[1][2]["limit"], "2")
        self.assertTrue(calls[1][2]["timestamp"].isdecimal())
        self.assertTrue(calls[1][3])

    @staticmethod
    def fake_requests_time(value):
        if value < 1 or value > int(time.time() * 1000) + 300_000:
            raise AssertionError("record timestamp outside expected range")
        return value

    def test_device_cookie_and_exact_request_parameters(self):
        import urllib.error

        responses = [
            self.device_list(),
            self.conversation_result(self.record()),
        ]
        requests = []

        class FakeResponse:
            status = 200

            def __init__(self, request, response):
                self.request = request
                self.response = response

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return self.request.full_url

            def read(self, _limit):
                return json.dumps(self.response).encode("utf-8")

        class FakeOpener:
            def open(self, request, timeout):
                requests.append((request, timeout))
                return FakeResponse(request, responses.pop(0))

        with patch("home_jarvis.xiaomi_cloud.urllib.request.build_opener",
                   return_value=FakeOpener()):
            XiaomiCloud(self.path).latest_records()

        self.assertEqual(len(requests), 2)
        device_request, conversation_request = [item[0] for item in requests]
        self.assertEqual(device_request.full_url.split("?", 1)[0],
                         "https://api2.mina.mi.com/admin/v2/device_list")
        device_query = urllib.parse.parse_qs(urllib.parse.urlsplit(device_request.full_url).query)
        self.assertEqual(device_query["master"], ["0"])
        self.assertTrue(device_query["requestId"][0].startswith("app_ios_"))
        self.assertEqual(device_request.get_header("Cookie"),
                         f"userId={USER_ID}; serviceToken={SERVICE_TOKEN}")

        self.assertEqual(conversation_request.full_url.split("?", 1)[0],
                         "https://userprofile.mina.mi.com/device_profile/v2/conversation")
        conversation_query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(conversation_request.full_url).query)
        self.assertEqual(conversation_query["source"], ["dialogu"])
        self.assertEqual(conversation_query["hardware"], [HARDWARE])
        self.assertEqual(conversation_query["limit"], ["2"])
        self.assertTrue(conversation_query["timestamp"][0].isdecimal())
        self.assertEqual(conversation_request.get_header("Cookie"),
                         f"userId={USER_ID}; serviceToken={SERVICE_TOKEN}; deviceId={DEVICE_ID}")
        for request, timeout in requests:
            self.assertEqual(request.get_header("User-agent"),
                             "MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) "
                             "Alamofire/6.0.103 MICO/iOSApp/appStore/6.0.103")
            self.assertEqual(timeout, 10)

    def test_empty_records_and_historical_invalid_entries_are_filtered(self):
        invalid = [
            self.record(request_id="bad/id"),
            self.record(when="1700000000000"),
            self.record(query="  \n "),
            self.record(illegalContent=True),
            self.record(request_id="content-flagged", illegalContent=1),
            {"answers": [{"text": "answer-only"}], "requestId": "answer", "time": int(time.time() * 1000)},
        ]
        calls = self.fake_requests([
            self.device_list(),
            self.conversation_result(*invalid, self.record(request_id="valid", query="问天气")),
        ])
        records = XiaomiCloud(self.path).latest_records()
        self.assertEqual([record.text for record in records], ["问天气"])
        self.assertEqual([record.record_id for record in records], [f"{MIOT_DID}:valid"])
        self.assertEqual(len(calls), 2)

        self.fake_requests([self.device_list(), self.conversation_result()])
        self.assertEqual(XiaomiCloud(self.path).latest_records(), ())

    def test_numeric_device_list_did_matches_login_saved_string(self):
        self.fake_requests([
            self.device_list({"miotDID": int(MIOT_DID), "deviceID": DEVICE_ID, "hardware": HARDWARE}),
            self.conversation_result(),
        ])
        self.assertEqual(XiaomiCloud(self.path).latest_records(), ())

    def test_same_hardware_must_be_unique(self):
        calls = self.fake_requests([self.device_list(
            {"miotDID": MIOT_DID, "deviceID": DEVICE_ID, "hardware": HARDWARE},
            {"miotDID": "other-did", "deviceID": "other-device", "hardware": HARDWARE},
        )])
        with self.assertRaises(CloudError):
            XiaomiCloud(self.path).latest_records()
        self.assertEqual(len(calls), 1)

    def test_target_identity_mismatch_prevents_conversation_request(self):
        rows = (
            {"miotDID": "other", "deviceID": DEVICE_ID, "hardware": HARDWARE},
            {"miotDID": MIOT_DID, "deviceID": "other", "hardware": HARDWARE},
            {"miotDID": MIOT_DID, "deviceID": DEVICE_ID, "hardware": "other"},
        )
        for row in rows:
            with self.subTest(row=row):
                calls = self.fake_requests([self.device_list(row)])
                with self.assertRaises(CloudError):
                    XiaomiCloud(self.path).latest_records()
                self.assertEqual(len(calls), 1)

    def test_malformed_conversation_response_fails_closed(self):
        for response in (
            {"code": 0, "message": "success", "data": "not-json"},
            {"code": 0, "message": "success", "data": json.dumps({"records": {}})},
        ):
            with self.subTest(response=response):
                self.fake_requests([self.device_list(), response])
                with self.assertRaises(CloudError):
                    XiaomiCloud(self.path).latest_records()

    def test_expired_auth_is_sanitized(self):
        import urllib.error

        class FakeOpener:
            def open(self, *_args, **_kwargs):
                raise urllib.error.HTTPError(
                    "https://api2.mina.mi.com/admin/v2/device_list", 401,
                    "Unauthorized", {}, None)

        with patch("home_jarvis.xiaomi_cloud.urllib.request.build_opener",
                   return_value=FakeOpener()):
            error = self.assert_safe_error(
                XiaomiCloud(self.path)._request, "GET", "/admin/v2/device_list")
        self.assertIn("authentication rejected", str(error))

    def test_http_errors_do_not_include_credentials_or_remote_body(self):
        import urllib.error

        class FakeOpener:
            def open(self, *_args, **_kwargs):
                raise urllib.error.HTTPError(
                    "https://userprofile.mina.mi.com/device_profile/v2/conversation", 401,
                    "Unauthorized " + SERVICE_TOKEN, {}, None)

        with patch("home_jarvis.xiaomi_cloud.urllib.request.build_opener",
                   return_value=FakeOpener()):
            self.assert_safe_error(
                lambda: XiaomiCloud(self.path)._request(
                    "GET", "/device_profile/v2/conversation",
                    {"source": "dialogu", "hardware": HARDWARE,
                     "timestamp": "1", "limit": "2"},
                    include_device_id=True))

    def test_rejects_symlink_and_insecure_permissions(self):
        link = self.path.with_name("link.json")
        link.symlink_to(self.path)
        with self.assertRaises(CloudError):
            XiaomiCloud(link)

        self.path.chmod(0o644)
        with self.assertRaises(CloudError):
            XiaomiCloud(self.path)

    def test_rejects_duplicate_config_fields(self):
        self.path.write_text(
            json.dumps({
                "userId": USER_ID,
                "serviceToken": SERVICE_TOKEN,
                "deviceId": DEVICE_ID,
                "miotDID": MIOT_DID,
                "hardware": HARDWARE,
            }).replace('"userId": "222222222"', '"userId": "222222222", "userId": "654321"'),
            encoding="utf-8",
        )
        self.path.chmod(0o600)
        with self.assertRaises(CloudError):
            XiaomiCloud(self.path)


if __name__ == "__main__":
    unittest.main()
