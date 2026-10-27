#!/usr/bin/env python3
"""Official Xiaomi QR login for one speaker; never stores the account passToken."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import parse_qs, quote, urlsplit

import requests

ACCOUNT = "https://account.xiaomi.com"
MINA = "https://api2.mina.mi.com"
UA = "MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) Alamofire/6.0.103 MICO/iOSApp/appStore/6.0.103"


class LoginError(Exception):
    def __init__(self, message, *, stage=None, exception_class=None, http_status=None):
        super().__init__(message)
        self.stage = stage
        self.exception_class = exception_class
        self.http_status = http_status


def official(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {
            "account.xiaomi.com", "ak.lp.account.xiaomi.com", "c3.lp.account.xiaomi.com", "api2.mina.mi.com"}
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise LoginError("Login returned an unexpected destination.")
    return url


def get(session, url, **kwargs):
    response = session.get(official(url), timeout=kwargs.pop("timeout", 15),
                           allow_redirects=False, **kwargs)
    if response.status_code not in (200, 302):
        raise LoginError(f"Official login request failed (HTTP {response.status_code}).",
                         http_status=response.status_code)
    if len(response.content) > 1024 * 1024:
        raise LoginError("Official response exceeded the size limit.")
    return response


def data(response):
    try:
        result = json.loads(response.text.removeprefix("&&&START&&&"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except ValueError:
        raise LoginError("Official login response was not valid JSON.") from None


def private_write(path, content):
    path = Path(path).expanduser()
    path = path.parent.resolve() / path.name
    if any((parent / ".git").exists() for parent in (path.parent, *path.parents)):
        raise LoginError("Credentials and QR must be outside Git working trees.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".xiaomi-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sts_location(url):
    location = urlsplit(official(url))
    if location.hostname != "api2.mina.mi.com" or location.path != "/sts" or location.fragment:
        raise LoginError("Unexpected micoapi token endpoint.")
    return url


def finish_login(session, authenticated, did, output):
    """Redeem a QR ticket, with one clean-session fallback for an STS rejection."""
    stage = "qr_result"
    fresh_session = None
    try:
        if (not isinstance(authenticated, dict) or authenticated.get("code", 0) != 0
                or type(authenticated.get("userId")) not in (str, int)
                or not isinstance(authenticated.get("location"), str) or not authenticated["location"]):
            raise LoginError("QR confirmation did not contain the required userId and location.")
        user_id = str(authenticated["userId"])
        if not user_id.isascii() or not user_id.isdecimal() or int(user_id) <= 0:
            raise LoginError("QR confirmation contained an invalid userId.")
        stage = "sts"
        location = sts_location(authenticated["location"])
        try:
            token_response = get(session, location)
        except LoginError as error:
            pass_token = authenticated.get("passToken")
            if error.http_status != 401 or not isinstance(pass_token, str) or not pass_token:
                raise
            stage = "service_login_fallback"
            if len(pass_token) > 8192 or any(ord(char) < 33 or ord(char) > 126 for char in pass_token):
                raise LoginError("QR confirmation contained an invalid passToken.")
            fresh_session = requests.Session()
            fresh_session.trust_env = False
            fresh_session.headers["User-Agent"] = UA
            exchange = data(get(fresh_session, ACCOUNT + "/pass/serviceLogin",
                                params={"sid": "micoapi", "_json": "true"}, cookies={
                                    "sdkVersion": "3.9", "deviceId": secrets.token_hex(8).upper(),
                                    "userId": user_id, "passToken": pass_token}))
            if (exchange.get("code") != 0 or str(exchange.get("userId")) != user_id
                    or exchange.get("sid", "micoapi") != "micoapi"
                    or exchange.get("callback", MINA + "/sts") != MINA + "/sts"):
                raise LoginError("The fallback login did not match the confirmed micoapi account.")
            nonce = exchange.get("nonce")
            security = exchange.get("ssecurity")
            if (type(nonce) not in (str, int) or not str(nonce).isascii()
                    or not str(nonce).removeprefix("-").isdecimal()
                    or not isinstance(security, str) or not security
                    or not isinstance(exchange.get("location"), str) or not exchange["location"]):
                raise LoginError("The fallback login did not contain valid signing fields.")
            stage = "fallback_sts"
            location = sts_location(exchange["location"])
            if "clientSign" in parse_qs(urlsplit(location).query, keep_blank_values=True):
                raise LoginError("The fallback token endpoint was already signed.")
            client_sign = base64.b64encode(hashlib.sha1(f"nonce={nonce}&{security}".encode()).digest()).decode()
            separator = "&" if "?" in location else "?"
            token_response = get(fresh_session, location + separator + "clientSign=" + quote(client_sign, safe=""))
            session = fresh_session
        service_token = token_response.cookies.get("serviceToken")
        if not isinstance(service_token, str) or not service_token:
            raise LoginError("The micoapi service token was not received.")
        stage = "device_list"
        devices = data(get(session, MINA + "/admin/v2/device_list", params={
            "master": 0, "requestId": "app_ios_" + secrets.token_hex(15)},
            cookies={"userId": user_id, "serviceToken": service_token}))
        if (devices.get("code") != 0 or not isinstance(devices.get("data"), list)
                or any(not isinstance(item, dict) for item in devices["data"])):
            raise LoginError("The micoapi session could not read the device list.")
        stage = "device_match"
        matches = [item for item in devices["data"] if str(item.get("miotDID")) == did]
        if len(matches) != 1 or matches[0].get("hardware") not in {"S12", "S12A"}:
            raise LoginError("The selected account did not contain the expected S12 speaker.")
        device = matches[0]
        if not isinstance(device.get("deviceID"), str) or not device["deviceID"]:
            raise LoginError("The selected speaker had no cloud device ID.")
        saved = {"userId": user_id, "serviceToken": service_token, "deviceId": device["deviceID"],
                 "miotDID": did, "hardware": device["hardware"]}
        stage = "save_session"
        private_write(output, (json.dumps(saved) + "\n").encode())
        print(json.dumps({"status": "authenticated", "session_file": str(output),
                          "miotDID": did, "hardware": device["hardware"]}), flush=True)
    except Exception as error:
        message = str(error) if isinstance(error, LoginError) else "Login failed; private details were suppressed."
        raise LoginError(message, stage=stage, exception_class=type(error).__name__) from None
    finally:
        if fresh_session is not None:
            fresh_session.close()


def login(did, directory):
    qr_path = directory / "xiaomi-login-qr.png"
    output = directory / f"xiaomi-cloud-{did}.json"
    session = requests.Session()
    session.trust_env = False
    session.headers["User-Agent"] = UA
    client_id = secrets.token_hex(8).upper()
    session.cookies.set("deviceId", client_id, domain="account.xiaomi.com", path="/")
    session.cookies.set("sdkVersion", "3.9", domain="account.xiaomi.com", path="/")
    stage = "setup"
    try:
        setup = data(get(session, ACCOUNT + "/pass/serviceLogin", params={"sid": "micoapi", "_json": "true"}))
        if setup.get("sid") != "micoapi" or setup.get("callback") != MINA + "/sts":
            raise LoginError("The login service did not match micoapi.")
        stage = "qr_request"
        qr = data(get(session, ACCOUNT + "/longPolling/loginUrl", params={
            "_qrsize": "480", "qs": setup["qs"], "callback": setup["callback"],
            "sid": "micoapi", "serviceParam": setup.get("serviceParam", ""),
            "_hasLogo": "false", "_locale": "zh_CN", "_dc": str(int(time.time() * 1000)),
        }))
        stage = "qr_image"
        image = get(session, qr["qr"]).content
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise LoginError("The QR image was not PNG.")
        stage = "save_qr"
        private_write(qr_path, image)
        print(json.dumps({"status": "scan_required", "qr_file": str(qr_path)}), flush=True)
        stage = "qr_poll"
        deadline = time.monotonic() + min(max(int(qr["timeout"]), 30), 300)
        authenticated = None
        while time.monotonic() < deadline:
            try:
                result = data(get(session, qr["lp"], timeout=10))
            except requests.exceptions.Timeout:
                continue
            if result.get("code", 0) != 0:
                raise LoginError("QR confirmation was not accepted.")
            if result.get("userId") and result.get("location"):
                authenticated = result
                break
            time.sleep(1)
        if not authenticated:
            raise LoginError("QR login expired; a new QR scan is required.")
        finish_login(session, authenticated, did, output)
    except Exception as error:
        if isinstance(error, LoginError) and error.stage:
            raise
        message = str(error) if isinstance(error, LoginError) else "Login failed; private details were suppressed."
        raise LoginError(message, stage=stage, exception_class=type(error).__name__) from None
    finally:
        try:
            session.close()
        finally:
            qr_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--did", required=True)
    args = parser.parse_args()
    if not args.did.isascii() or not args.did.isdecimal() or not 0 < int(args.did) <= 0xffffffff:
        parser.error("--did must be the speaker's positive 32-bit miIO ID")
    try:
        login(args.did, Path.home() / ".config/home-jarvis")
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        message = str(error) if isinstance(error, LoginError) else "Login failed; private details were suppressed."
        stage = error.stage if isinstance(error, LoginError) and error.stage else "startup_or_cleanup"
        exception_class = (error.exception_class if isinstance(error, LoginError) else None) or type(error).__name__
        print(json.dumps({"status": "error", "message": message,
                          "stage": stage, "exception_class": exception_class}), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
