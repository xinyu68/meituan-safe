#!/usr/bin/env python3
"""Safe, agent-oriented wrapper for Meituan restaurant discovery."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "0.7.1"
SKILL_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = SKILL_DIR / ".runtime"
UPSTREAM_DIR = RUNTIME_DIR / "meituan-cli"
UPSTREAM_ENTRY = UPSTREAM_DIR / "dist" / "index.mjs"
HOME_URL = "https://h5.waimai.meituan.com/"
LOGIN_URL = "https://h5.waimai.meituan.com/login?force=true"
BACKGROUND_BROWSER_ARGS = (
    "--start-minimized",
    "--window-position=-32000,-32000",
    "--window-size=1280,900",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)
LOGIN_BROWSER_ARGS = (
    "--start-minimized",
    "--window-size=1280,900",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)
AUTH_COOKIE_NAMES = (
    "wm_order_channel",
    "token",
    "userId",
    "mt_c_token",
    "w_token",
    "openh5_uuid",
    "iuuid",
    "w_visitid",
    "w_utmz",
    "terminal",
    "channelType",
    "channelConfig",
)
WINDOWS_CREDENTIAL_UTF16_LIMIT = 2400
GEOCODE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
PASSPORT_BASE_URL = "https://passport.meituan.com"
PASSPORT_CLIENT_ID = "c6f50b5a1e2f4e2bb00a3e2f58df3ced"
PASSPORT_CSEC_PLATFORM = "7"
PASSPORT_CSEC_VERSION = "1.4.2"
PASSPORT_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
PASSPORT_ENTROPY = b"meituan-safe-passport-v1"
DEAL_AUTH_ERROR_PATTERNS = (
    "token校验异常",
    "token 校验异常",
    "token verification failed",
)
AUTH_ERROR_PATTERNS = (
    "not logged in",
    "session expired",
    "authentication required",
    "invalid cookie",
    "missing cookie",
    "登录已过期",
    "登录失效",
    "token已过期",
)


class MeituanError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        exit_code: int = 1,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.exit_code = exit_code
        self.details = details or {}


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _emit(data: Any, *, next_steps: list[str] | None = None) -> None:
    payload: dict[str, Any] = {
        "ok": True,
        "data": data,
        "meta": {"schema_version": SCHEMA_VERSION},
    }
    if next_steps:
        payload["next"] = next_steps
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _emit_error(error: MeituanError) -> None:
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            **error.details,
        },
        "meta": {"schema_version": SCHEMA_VERSION},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _default_profile_dir() -> Path:
    override = os.environ.get("MEITUAN_SAFE_PROFILE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "meituan-safe" / "chrome-profile"
    return Path.cwd() / ".meituan-profile"


def _login_browser_args() -> list[str]:
    arguments = ["--no-first-run", "--no-default-browser-check"]
    if os.name == "nt":
        arguments.extend(LOGIN_BROWSER_ARGS)
    return arguments


def _compact_auth_cookie(cookies: list[dict[str, Any]]) -> tuple[str, list[str]]:
    by_name = {
        str(cookie.get("name")): str(cookie.get("value") or "")
        for cookie in cookies
        if cookie.get("name") and cookie.get("value") is not None
    }
    if not by_name.get("token") or not by_name.get("userId"):
        raise MeituanError(
            "login_cookie_incomplete",
            "已打开登录页面，但尚未取得完整认证会话",
            retryable=True,
            exit_code=2,
        )
    selected_names = [name for name in AUTH_COOKIE_NAMES if by_name.get(name)]
    cookie_string = "; ".join(f"{name}={by_name[name]}" for name in selected_names)
    if len(cookie_string.encode("utf-16-le")) > WINDOWS_CREDENTIAL_UTF16_LIMIT:
        selected_names = [name for name in ("wm_order_channel", "token", "userId", "openh5_uuid") if by_name.get(name)]
        cookie_string = "; ".join(f"{name}={by_name[name]}" for name in selected_names)
    if len(cookie_string.encode("utf-16-le")) > WINDOWS_CREDENTIAL_UTF16_LIMIT:
        raise MeituanError(
            "credential_too_large",
            "认证信息超过 Windows 凭据库限制，无法安全保存",
            exit_code=2,
        )
    return cookie_string, selected_names


def _windows_snapshot() -> tuple[set[int], int | None]:
    if os.name != "nt":
        return set(), None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        handles: set[int] = set()
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def collect(hwnd: int, _lparam: int) -> bool:
            if user32.IsWindowVisible(hwnd):
                handles.add(int(hwnd))
            return True

        user32.EnumWindows(collect, 0)
        return handles, int(user32.GetForegroundWindow()) or None
    except Exception:
        return set(), None


def _show_new_browser_without_activation(previous_windows: set[int], previous_foreground: int | None) -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        current_windows, _ = _windows_snapshot()
        browser_windows: list[int] = []
        for hwnd in current_windows - previous_windows:
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, len(class_name))
            if class_name.value == "Chrome_WidgetWin_1":
                browser_windows.append(hwnd)

        sw_show_no_activate = 4
        hwnd_not_topmost = wintypes.HWND(-2)
        flags = 0x0001 | 0x0002 | 0x0010 | 0x0040 | 0x0200
        for hwnd in browser_windows:
            user32.ShowWindow(hwnd, sw_show_no_activate)
            user32.SetWindowPos(hwnd, hwnd_not_topmost, 0, 0, 0, 0, flags)
        if previous_foreground and user32.IsWindow(previous_foreground):
            user32.SetForegroundWindow(previous_foreground)
        return len(browser_windows)
    except Exception:
        return 0


def _require_runtime() -> None:
    if not UPSTREAM_ENTRY.is_file():
        raise MeituanError(
            "runtime_missing",
            f"运行环境尚未初始化，请执行：python {SKILL_DIR / 'scripts' / 'bootstrap.py'}",
            exit_code=3,
        )


def _upstream_error(arguments: list[str], message: str) -> MeituanError:
    lowered = message.lower()
    is_deal_request = arguments[:2] == ["deal", "search"]
    if any(value in lowered for value in ("http 403", "http 429", "captcha", "风控")):
        return MeituanError(
            "access_restricted",
            "接口访问受限，不能据此判断登录过期或需要另一种授权",
            details={"action": "investigate_access_or_request", "upstream_message": message[:500]},
        )
    if is_deal_request and any(pattern in lowered for pattern in DEAL_AUTH_ERROR_PATTERNS):
        return MeituanError(
            "deal_auth_rejected",
            "团购接口未接受当前凭证；尚不能区分登录失效、授权范围或请求适配问题",
            exit_code=2,
            details={
                "domain": "in_store_deals",
                "action": "auth-check",
                "login_refresh_recommended": False,
                "upstream_message": message[:500],
            },
        )
    auth_error = any(pattern in lowered for pattern in AUTH_ERROR_PATTERNS)
    missing = any(value in lowered for value in ("not logged in", "missing cookie"))
    if auth_error:
        return MeituanError(
            "not_logged_in" if missing else "auth_rejected",
            "未找到登录凭证" if missing else "接口未接受当前登录凭证，不能仅凭此响应断定过期原因",
            exit_code=2,
            details={"action": "login" if missing else "auth-check", "upstream_message": message[:500]},
        )
    return MeituanError(
        "upstream_error",
        message,
        retryable="non-json response" in lowered,
    )


def _run_mt(arguments: list[str], *, env: dict[str, str] | None = None) -> Any:
    _require_runtime()
    command = ["node", str(UPSTREAM_ENTRY), "--json", *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=UPSTREAM_DIR,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired as exc:
        partial_stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        partial_stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        try:
            envelope = json.loads(partial_stdout.strip())
        except json.JSONDecodeError:
            raise MeituanError(
                "upstream_timeout",
                (partial_stderr.strip() or "美团请求超过 90 秒未完成")[:1000],
                retryable=True,
            ) from exc
        if envelope.get("ok"):
            return envelope.get("data")
        raise _upstream_error(arguments, str(envelope.get("error") or "美团请求失败")) from exc
    raw = completed.stdout.strip()
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        message = completed.stderr.strip() or raw or "上游 CLI 未返回 JSON"
        raise MeituanError("upstream_invalid_output", message[:1000]) from exc
    if not envelope.get("ok"):
        message = str(envelope.get("error") or completed.stderr or "美团请求失败")
        raise _upstream_error(arguments, message)
    return envelope.get("data")


def _clean_location_query(value: str) -> tuple[str, list[str]]:
    location = re.sub(r"\s+", " ", value).strip()
    if not location or len(location) > 120:
        raise MeituanError("invalid_location", "地点必须为 1-120 个字符", exit_code=3)
    variants = [location]
    municipality = next((city for city in ("北京", "上海", "天津", "重庆") if location.startswith(city)), "")
    core = location[len(municipality) :].strip() if municipality else location
    core = re.sub(r"地铁站$", "", core).strip()
    if core and municipality:
        variants.append(f"{core} {municipality}")
    if core and core != location:
        variants.append(core)
    return core or location, list(dict.fromkeys(variants))


def _cache_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / ".cache"
    return root / "meituan-safe" / "cache"


def _passport_auth_path() -> Path:
    suffix = ".bin" if os.name == "nt" else ".json"
    return _cache_dir().parent / f"passport-auth{suffix}"


def _passport_session_path() -> Path:
    suffix = ".bin" if os.name == "nt" else ".json"
    return _cache_dir().parent / f"passport-session{suffix}"


def _dpapi_transform(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        return data
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value: bytes) -> tuple[DataBlob, Any]:
        buffer = ctypes.create_string_buffer(value)
        return DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    input_blob, input_buffer = blob(data)
    entropy_blob, entropy_buffer = blob(PASSPORT_ENTROPY)
    output_blob = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if protect:
        success = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "meituan-safe Passport token",
            ctypes.byref(entropy_blob),
            None,
            None,
            0x1,
            ctypes.byref(output_blob),
        )
    else:
        success = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            0x1,
            ctypes.byref(output_blob),
        )
    del input_buffer, entropy_buffer
    if not success:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.data, output_blob.size)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.data, ctypes.c_void_p))


def _read_private_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        decoded = _dpapi_transform(raw, protect=False)
        payload = json.loads(decoded.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    encoded = _dpapi_transform(raw, protect=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(encoded)
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _delete_private_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _passport_cached_token() -> str | None:
    payload = _read_private_json(_passport_auth_path())
    token = payload.get("token")
    saved_at = payload.get("saved_at")
    if not isinstance(token, str) or not token:
        return None
    try:
        expired = time.time() - float(saved_at) >= PASSPORT_TOKEN_TTL_SECONDS
    except (TypeError, ValueError):
        expired = True
    if expired:
        _delete_private_file(_passport_auth_path())
        return None
    return token


def _passport_request(pathname: str, params: dict[str, str]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{PASSPORT_BASE_URL}{pathname}?{query}",
        headers={
            "Accept": "application/json, */*",
            "Cache-Control": "no-cache",
            "User-Agent": "Mozilla/5.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise MeituanError(
            "passport_unavailable",
            "美团 Passport 授权服务暂时不可用",
            retryable=True,
            exit_code=2,
        ) from exc
    if not isinstance(payload, dict) or payload.get("error"):
        raise MeituanError("passport_failed", "美团 Passport 授权请求失败", exit_code=2)
    code = payload.get("code")
    if code is not None:
        try:
            success_code = int(code) in (0, 200)
        except (TypeError, ValueError):
            success_code = False
        if not success_code:
            raise MeituanError("passport_failed", "美团 Passport 授权请求失败", exit_code=2)
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _trusted_passport_link(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        hostname == "dpurl.cn" or hostname == "meituan.com" or hostname.endswith(".meituan.com")
    )


def _write_passport_qr(auth_link: str) -> Path:
    try:
        import qrcode
    except ImportError as exc:
        raise MeituanError(
            "dependency_missing",
            "缺少二维码依赖，请重新运行 bootstrap.py",
            exit_code=3,
        ) from exc
    output = _cache_dir().parent / "passport-auth-qr.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.stem}.{os.getpid()}.tmp.png")
    try:
        image = qrcode.make(auth_link)
        image.save(temporary)
        temporary.replace(output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return output.resolve()


def _run_deal_login(args: argparse.Namespace) -> dict[str, Any]:
    cached = _passport_cached_token()
    if cached and not args.force:
        return {"authorized": True, "status": "cached", "token_exposed": False}
    if args.force:
        _delete_private_file(_passport_auth_path())
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    challenge = hashlib.sha256(verifier.encode("ascii")).hexdigest()
    data = _passport_request(
        "/api/account/userauth/code",
        {
            "client_id": PASSPORT_CLIENT_ID,
            "code_challenge": challenge,
            "csecplatform": PASSPORT_CSEC_PLATFORM,
            "csecversion": PASSPORT_CSEC_VERSION,
        },
    )
    auth_code = data.get("authCode") or data.get("auth_code")
    auth_link = data.get("shortLink") or data.get("auth_link")
    if not isinstance(auth_code, str) or not auth_code or not isinstance(auth_link, str):
        raise MeituanError("passport_failed", "Passport 未返回有效授权链接", exit_code=2)
    if not _trusted_passport_link(auth_link):
        raise MeituanError("passport_failed", "Passport 返回了非美团 HTTPS 授权链接", exit_code=2)
    _write_private_json(
        _passport_session_path(),
        {
            "auth_code": auth_code,
            "code_verifier": verifier,
            "created_at": time.time(),
        },
    )
    qr_image_path = _write_passport_qr(auth_link)
    return {
        "authorized": False,
        "status": "authorization_required",
        "auth_link": auth_link,
        "qr_image_path": str(qr_image_path),
        "token_exposed": False,
    }


def _run_deal_login_status() -> dict[str, Any]:
    if _passport_cached_token():
        return {"authorized": True, "status": "authorized", "token_exposed": False}
    session = _read_private_json(_passport_session_path())
    auth_code = session.get("auth_code")
    verifier = session.get("code_verifier")
    if not isinstance(auth_code, str) or not isinstance(verifier, str):
        return {"authorized": False, "status": "not_started", "token_exposed": False}
    data = _passport_request(
        "/api/account/userauth/check",
        {
            "client_id": PASSPORT_CLIENT_ID,
            "auth_code": auth_code,
            "code_verifier": verifier,
            "csecplatform": PASSPORT_CSEC_PLATFORM,
            "csecversion": PASSPORT_CSEC_VERSION,
        },
    )
    token = data.get("token") or data.get("accessToken")
    if isinstance(token, str) and token:
        _write_private_json(_passport_auth_path(), {"token": token, "saved_at": time.time()})
        _delete_private_file(_passport_session_path())
        return {"authorized": True, "status": "authorized", "token_exposed": False}
    auth_status = int(data.get("authStatus") or 0)
    if auth_status == 2:
        _delete_private_file(_passport_session_path())
        return {"authorized": False, "status": "cancelled", "token_exposed": False}
    if auth_status == 3:
        _delete_private_file(_passport_session_path())
        return {"authorized": False, "status": "risk_denied", "token_exposed": False}
    if auth_status == 5:
        _delete_private_file(_passport_session_path())
        return {"authorized": False, "status": "expired", "token_exposed": False}
    return {"authorized": False, "status": "pending", "token_exposed": False}


def _run_deal_logout() -> dict[str, Any]:
    _delete_private_file(_passport_auth_path())
    _delete_private_file(_passport_session_path())
    _delete_private_file(_cache_dir().parent / "passport-auth-qr.png")
    return {"authorized": False, "status": "logged_out"}


def _geocode_cache_path() -> Path:
    return _cache_dir() / "geocode.json"


def _load_geocode_cache(location: str, limit: int) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(_geocode_cache_path().read_text(encoding="utf-8"))
        entry = payload.get(location)
        if not isinstance(entry, dict) or time.time() - float(entry.get("stored_at", 0)) > GEOCODE_CACHE_TTL_SECONDS:
            return None
        candidates = entry.get("candidates")
        if not isinstance(candidates, list):
            return None
        return candidates[:limit]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _store_geocode_cache(location: str, candidates: list[dict[str, Any]]) -> None:
    path = _geocode_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload[location] = {"stored_at": time.time(), "candidates": candidates}
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def _geocode(location: str, limit: int = 5) -> list[dict[str, Any]]:
    normalized_location = re.sub(r"\s+", " ", location).strip()
    cached = _load_geocode_cache(normalized_location, limit)
    if cached is not None:
        return cached
    core, variants = _clean_location_query(location)
    collected: dict[str, dict[str, Any]] = {}
    for query in variants:
        params = urllib.parse.urlencode(
            {
                "format": "jsonv2",
                "limit": max(limit * 2, 10),
                "countrycodes": "cn",
                "addressdetails": 1,
                "q": query,
            }
        )
        request = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?{params}",
            headers={"User-Agent": "meituan-safe/0.1 local-skill"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                rows = json.load(response)
        except Exception as exc:
            if not collected:
                raise MeituanError("geocoder_unavailable", f"地点解析服务不可用：{exc}", retryable=True) from exc
            break
        for row in rows:
            key = f"{row.get('osm_type')}:{row.get('osm_id')}"
            address = row.get("address") or {}
            name = str(row.get("name") or "")
            display = str(row.get("display_name") or "")
            category = str(row.get("category") or "")
            place_type = str(row.get("type") or "")
            score = float(row.get("importance") or 0)
            if name == f"地铁{core}站":
                score += 6
            if name == core:
                score += 3
            elif core and core in name:
                score += 1.5
            if core and name in {f"{core}东", f"{core}西", f"地铁{core}东站", f"地铁{core}西站"}:
                score -= 1
            if category == "railway":
                score += 1
            if place_type in {"station", "stop", "subway_entrance"}:
                score += 0.6
            if core and any(core in str(value) for value in address.values()):
                score += 0.8
            if any(city in display for city in ("北京市", "北京")) and "北京" in location:
                score += 0.5
            collected[key] = {
                "name": name or display.split(",", 1)[0],
                "display_name": display,
                "latitude_wgs84": float(row["lat"]),
                "longitude_wgs84": float(row["lon"]),
                "category": category or None,
                "type": place_type or None,
                "score": round(score, 6),
                "source": "OpenStreetMap Nominatim",
            }
        if collected:
            break
    results = sorted(collected.values(), key=lambda item: item["score"], reverse=True)
    for index, item in enumerate(results, 1):
        item["index"] = index
    _store_geocode_cache(normalized_location, results)
    return results[:limit]


def _out_of_china(latitude: float, longitude: float) -> bool:
    return not (72.004 <= longitude <= 137.8347 and 0.8293 <= latitude <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    value = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    value += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    return value


def _transform_lng(x: float, y: float) -> float:
    value = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    value += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    value += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    value += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    return value


def _wgs84_to_gcj02(latitude: float, longitude: float) -> tuple[float, float]:
    if _out_of_china(latitude, longitude):
        return latitude, longitude
    a = 6378245.0
    ee = 0.006693421622965943
    d_lat = _transform_lat(longitude - 105.0, latitude - 35.0)
    d_lng = _transform_lng(longitude - 105.0, latitude - 35.0)
    rad_lat = latitude / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    d_lng = (d_lng * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return latitude + d_lat, longitude + d_lng


def _distance_meters(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    amount = float(match.group(1))
    if "km" in text or "公里" in text or "千米" in text:
        return amount * 1000
    return amount


def _numeric_value(value: Any, *, free_is_zero: bool = False) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip().lower().replace(",", "")
    if not text:
        return None
    if free_is_zero and any(token in text for token in ("免费", "免配送", "免运费")):
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    amount = float(match.group(1))
    if "万" in text:
        amount *= 10_000
    return amount


def _delivery_minutes(value: Any) -> float | None:
    numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", str(value or ""))]
    return max(numbers) if numbers else None


def _first_numeric(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _numeric_value(item.get(key))
        if value is not None:
            return value
    return None


def _deal_party_size(title: Any) -> tuple[int | None, int | None, str | None]:
    text = str(title or "")
    range_match = re.search(
        r"(?<!\d)(\d{1,2})\s*(?:-|—|~|～|至|到)\s*(\d{1,2})\s*(?:人|位)(?:餐|套餐|份|享用)?",
        text,
    )
    if range_match:
        minimum, maximum = int(range_match.group(1)), int(range_match.group(2))
        if 1 <= minimum <= maximum <= 30:
            return minimum, maximum, range_match.group(0)

    exact_match = re.search(r"(?<!\d)(\d{1,2})\s*(?:人|位)(?:餐|套餐|份|享用)", text)
    if exact_match:
        size = int(exact_match.group(1))
        if 1 <= size <= 30:
            return size, size, exact_match.group(0)

    chinese_sizes = {
        "单人": 1,
        "一人": 1,
        "双人": 2,
        "两人": 2,
        "二人": 2,
        "三人": 3,
        "四人": 4,
        "五人": 5,
        "六人": 6,
        "七人": 7,
        "八人": 8,
        "九人": 9,
        "十人": 10,
    }
    for label, size in chinese_sizes.items():
        match = re.search(rf"{label}(?:餐|套餐|份|享用)", text)
        if match:
            return size, size, match.group(0)
    return None, None, None


def _normalize_deal(item: dict[str, Any]) -> dict[str, Any]:
    distance_text = str(item.get("distanceText") or item.get("distance") or "")
    distance = _distance_meters(distance_text)
    rating = _first_numeric(item, ("poiDpFiveScore", "poiScore", "score", "rating"))
    sale_price = _first_numeric(item, ("salePrice", "price", "currentPrice"))
    original_price = _first_numeric(item, ("originalPrice", "marketPrice", "value"))
    official_average_price = _first_numeric(item, ("avgPrice", "averagePrice", "poiAvgPrice", "avgPay"))
    title = item.get("productName") or item.get("title") or item.get("name")
    party_size_min, party_size_max, party_size_text = _deal_party_size(title)
    estimated_average_price = (
        round(sale_price / party_size_min, 2)
        if sale_price is not None and party_size_min is not None
        else None
    )
    average_price = official_average_price if official_average_price is not None else estimated_average_price
    average_price_source = (
        "meituan_official"
        if official_average_price is not None
        else "deal_price_divided_by_min_party_size"
        if estimated_average_price is not None
        else None
    )
    return {
        "product_id": str(item.get("productId") or ""),
        "poi_id": str(item.get("poiId") or ""),
        "restaurant": item.get("poiName") or item.get("shopName") or item.get("merchantName"),
        "title": title,
        "sale_price": sale_price,
        "original_price": original_price,
        "average_price": average_price,
        "average_price_source": average_price_source,
        "official_average_price": official_average_price,
        "estimated_average_price": estimated_average_price,
        "party_size_min": party_size_min,
        "party_size_max": party_size_max,
        "party_size_text": party_size_text,
        "rating": rating,
        "distance": distance_text or None,
        "distance_meters": distance,
        "image": item.get("imageUrl") or item.get("image") or item.get("picture"),
        "sold": item.get("soldText") or item.get("salesText") or item.get("saleCountText"),
        "raw_url": item.get("productUrl") or item.get("url") or item.get("jumpUrl"),
    }


def _run_deals_search(args: argparse.Namespace) -> dict[str, Any]:
    if args.min_rating is not None and not 0 <= args.min_rating <= 5:
        raise MeituanError("invalid_min_rating", "最低评分必须在 0-5 之间", exit_code=3)
    for label, value in (
        ("最低团购价", args.min_price),
        ("最高团购价", args.max_price),
        ("最低人均", args.min_average_price),
        ("最高人均", args.max_average_price),
    ):
        if value is not None and value < 0:
            raise MeituanError("invalid_filter", f"{label}不能为负数", exit_code=3)
    if args.min_price is not None and args.max_price is not None and args.min_price > args.max_price:
        raise MeituanError("invalid_price_range", "最低团购价不能高于最高团购价", exit_code=3)
    if (
        args.min_average_price is not None
        and args.max_average_price is not None
        and args.min_average_price > args.max_average_price
    ):
        raise MeituanError("invalid_average_price_range", "最低人均不能高于最高人均", exit_code=3)

    if args.location:
        candidates = _geocode(args.location, max(args.location_index, 5))
        if not candidates:
            raise MeituanError("location_not_found", f"没有解析到地点：{args.location}", exit_code=3)
        if args.location_index > len(candidates):
            raise MeituanError("invalid_location_index", "地点候选序号超出范围", exit_code=3)
        selected = candidates[args.location_index - 1]
        latitude, longitude = _wgs84_to_gcj02(
            selected["latitude_wgs84"], selected["longitude_wgs84"]
        )
        label = selected["display_name"]
    else:
        if args.lat is None or args.lng is None:
            raise MeituanError("location_required", "提供 --location 或同时提供 --lat/--lng", exit_code=3)
        if not args.city_id:
            raise MeituanError("city_id_required", "直接使用经纬度时还需要 --city-id", exit_code=3)
        latitude, longitude = args.lat, args.lng
        _validate_coordinates(latitude, longitude)
        selected = None
        label = f"coordinates:{latitude},{longitude}"

    command = [
        "deal", "search",
        "--keyword", args.keyword,
        "--lat", str(latitude),
        "--lng", str(longitude),
        "--page", str(args.page),
        "--page-size", str(args.page_size),
    ]
    if args.city_id:
        command.extend(["--city-id", str(args.city_id)])
    else:
        command.extend(["--address", args.location])
    if args.query_id:
        command.extend(["--query-id", args.query_id])
    if args.request_id:
        command.extend(["--request-id", args.request_id])
    auth_source, delegated = _deal_auth_environment(args.auth_source)
    try:
        raw = _run_mt(command, env=delegated)
    except MeituanError as exc:
        exc.details["auth_source"] = auth_source
        if exc.code in ("deal_auth_rejected", "auth_rejected"):
            if auth_source == "passport_pkce":
                _delete_private_file(_passport_auth_path())
                exc.details["action"] = "deal-login"
            else:
                exc.details["action"] = "auth-check"
        raise
    if not isinstance(raw, dict) or not isinstance(raw.get("products"), list):
        raise MeituanError("upstream_invalid_output", "团购接口结果缺少有效商品列表")
    products = raw.get("products", []) if isinstance(raw, dict) else []
    normalized = [_normalize_deal(dict(item)) for item in products if isinstance(item, dict)]
    matched: list[dict[str, Any]] = []
    for item in normalized:
        distance = item["distance_meters"]
        if distance is None or distance > args.radius:
            continue
        if args.min_rating is not None and (item["rating"] is None or item["rating"] < args.min_rating):
            continue
        if args.min_price is not None and (item["sale_price"] is None or item["sale_price"] < args.min_price):
            continue
        if args.max_price is not None and (item["sale_price"] is None or item["sale_price"] > args.max_price):
            continue
        if args.min_average_price is not None and (
            item["average_price"] is None or item["average_price"] < args.min_average_price
        ):
            continue
        if args.max_average_price is not None and (
            item["average_price"] is None or item["average_price"] > args.max_average_price
        ):
            continue
        matched.append(item)
    matched.sort(
        key=lambda item: (
            item["distance_meters"] if item["distance_meters"] is not None else math.inf,
            -(item["rating"] if item["rating"] is not None else 0),
        )
    )
    return {
        "query": {
            "keyword": args.keyword,
            "location": label,
            "radius_meters": args.radius,
            "page": args.page,
            "page_size": args.page_size,
            "filters": {
                "min_rating": args.min_rating,
                "min_price": args.min_price,
                "max_price": args.max_price,
                "min_average_price": args.min_average_price,
                "max_average_price": args.max_average_price,
            },
        },
        "coordinates": {"latitude_gcj02": latitude, "longitude_gcj02": longitude},
        "geocode_selected": selected,
        "city_id": raw.get("cityId") if isinstance(raw, dict) else args.city_id,
        "deals": matched[: args.limit],
        "count": min(len(matched), args.limit),
        "matched_count": len(matched),
        "scanned_count": len(normalized),
        "is_last_page": bool(raw.get("isLastPage", True)) if isinstance(raw, dict) else True,
        "next_page": None if not isinstance(raw, dict) or raw.get("isLastPage", True) else args.page + 1,
        "query_id": str(raw.get("queryId") or "") if isinstance(raw, dict) else "",
        "request_id": str(raw.get("requestId") or "") if isinstance(raw, dict) else "",
        "coverage": "meituan_ranked_deal_results_not_exhaustive",
        "authentication": {"source": auth_source, "api_verified": True, "scope": "deal_search_request"},
    }


def _food_projection_from_deals(
    deal_result: dict[str, Any],
    *,
    min_official_average_price: float | None,
    max_official_average_price: float | None,
    limit: int,
) -> dict[str, Any]:
    """Build a restaurant view without treating package estimates as store averages."""
    restaurants_by_id: dict[str, dict[str, Any]] = {}
    for deal in deal_result.get("deals", []):
        poi_id = str(deal.get("poi_id") or "")
        name = deal.get("restaurant")
        if not poi_id or not name:
            continue
        restaurant = restaurants_by_id.setdefault(
            poi_id,
            {
                "poi_id": poi_id,
                "name": name,
                "rating": deal.get("rating"),
                "distance": deal.get("distance"),
                "distance_meters": deal.get("distance_meters"),
                "official_average_price": deal.get("official_average_price"),
                "image": deal.get("image"),
                "deal_count": 0,
                "representative_deals": [],
                "data_origin": "meituan_deal_catalog_restaurant_projection",
            },
        )
        restaurant["deal_count"] += 1
        restaurant["representative_deals"].append(
            {
                "product_id": deal.get("product_id"),
                "title": deal.get("title"),
                "sale_price": deal.get("sale_price"),
            }
        )
        if restaurant["rating"] is None and deal.get("rating") is not None:
            restaurant["rating"] = deal.get("rating")
        if restaurant["official_average_price"] is None and deal.get("official_average_price") is not None:
            restaurant["official_average_price"] = deal.get("official_average_price")
        current_distance = restaurant.get("distance_meters")
        candidate_distance = deal.get("distance_meters")
        if candidate_distance is not None and (current_distance is None or candidate_distance < current_distance):
            restaurant["distance"] = deal.get("distance")
            restaurant["distance_meters"] = candidate_distance

    restaurants: list[dict[str, Any]] = []
    for restaurant in restaurants_by_id.values():
        official_average = restaurant["official_average_price"]
        if min_official_average_price is not None and (
            official_average is None or official_average < min_official_average_price
        ):
            continue
        if max_official_average_price is not None and (
            official_average is None or official_average > max_official_average_price
        ):
            continue
        restaurants.append(restaurant)
    restaurants.sort(
        key=lambda item: (
            item["distance_meters"] if item["distance_meters"] is not None else math.inf,
            -(item["rating"] if item["rating"] is not None else 0),
        )
    )
    query = dict(deal_result.get("query") or {})
    query["filters"] = {
        "min_rating": query.get("filters", {}).get("min_rating"),
        "min_official_average_price": min_official_average_price,
        "max_official_average_price": max_official_average_price,
    }
    return {
        "query": query,
        "coordinates": deal_result.get("coordinates"),
        "geocode_selected": deal_result.get("geocode_selected"),
        "city_id": deal_result.get("city_id"),
        "restaurants": restaurants[:limit],
        "count": min(len(restaurants), limit),
        "matched_count": len(restaurants),
        "scanned_deal_count": deal_result.get("scanned_count", 0),
        "average_price_semantics": "meituan_official_store_average_only",
        "deal_estimates_excluded_from_store_average": True,
        "data_origin": "restaurant_fields_projected_from_meituan_deal_catalog",
        "coverage": "meituan_ranked_restaurants_with_deals_not_exhaustive",
        "authentication": deal_result.get("authentication"),
    }


def _run_food_search(args: argparse.Namespace) -> dict[str, Any]:
    for label, value in (
        ("最低门店官方人均", args.min_official_average_price),
        ("最高门店官方人均", args.max_official_average_price),
    ):
        if value is not None and value < 0:
            raise MeituanError("invalid_filter", f"{label}不能为负数", exit_code=3)
    if (
        args.min_official_average_price is not None
        and args.max_official_average_price is not None
        and args.min_official_average_price > args.max_official_average_price
    ):
        raise MeituanError("invalid_average_price_range", "最低门店官方人均不能高于最高门店官方人均", exit_code=3)

    deal_args = argparse.Namespace(**vars(args))
    deal_args.min_price = None
    deal_args.max_price = None
    deal_args.min_average_price = None
    deal_args.max_average_price = None
    deal_args.limit = 100
    deal_result = _run_deals_search(deal_args)
    return _food_projection_from_deals(
        deal_result,
        min_official_average_price=args.min_official_average_price,
        max_official_average_price=args.max_official_average_price,
        limit=args.limit,
    )


def _restaurant_metrics(restaurant: dict[str, Any]) -> dict[str, Any]:
    distance = restaurant.get("distance_meters")
    if distance is None:
        distance = _distance_meters(restaurant.get("distance"))
    delivery_fee = restaurant.get("deliveryFee")
    if delivery_fee is None:
        delivery_fee = _numeric_value(restaurant.get("shippingFee"), free_is_zero=True)
    min_order = restaurant.get("minOrderAmount")
    if min_order is None:
        min_order = _numeric_value(restaurant.get("minOrder"))
    promotions = restaurant.get("promotionTags") or []
    if not isinstance(promotions, list):
        promotions = []
    return {
        "rating": _numeric_value(restaurant.get("rating")),
        "distance_meters": distance,
        "delivery_fee": delivery_fee,
        "min_order_amount": min_order,
        "delivery_minutes": _delivery_minutes(restaurant.get("deliveryTime")),
        "monthly_sales": _numeric_value(restaurant.get("monthSales")),
        "promotions": [str(value) for value in promotions if str(value).strip()],
    }


def _recommendation_score(metrics: dict[str, Any]) -> tuple[float, dict[str, float]]:
    rating = metrics.get("rating")
    distance = metrics.get("distance_meters")
    delivery_fee = metrics.get("delivery_fee")
    min_order = metrics.get("min_order_amount")
    eta = metrics.get("delivery_minutes")
    sales = metrics.get("monthly_sales")
    breakdown = {
        "rating": (rating / 5 * 45) if rating is not None else 0,
        "distance": max(0, 20 - min(distance, 4000) / 200) if distance is not None else 0,
        "delivery_fee": max(0, 12 - min(delivery_fee or 0, 12)) if delivery_fee is not None else 0,
        "delivery_time": max(0, 12 - max((eta or 20) - 20, 0) * 0.4) if eta is not None else 0,
        "sales": min(math.log10((sales or 0) + 1) * 3, 8) if sales is not None else 0,
        "promotions": min(len(metrics.get("promotions") or []) * 1.5, 3),
        "min_order": max(0, 5 - min(min_order or 0, 50) / 10) if min_order is not None else 0,
    }
    return round(sum(breakdown.values()), 2), {key: round(value, 2) for key, value in breakdown.items()}


def _enrich_restaurant(restaurant: dict[str, Any]) -> dict[str, Any]:
    item = dict(restaurant)
    metrics = _restaurant_metrics(item)
    score, breakdown = _recommendation_score(metrics)
    item["metrics"] = metrics
    item["recommendation_score"] = score
    item["recommendation_breakdown"] = breakdown
    return item


def _restaurant_matches(metrics: dict[str, Any], args: argparse.Namespace) -> bool:
    checks = (
        ("rating", args.min_rating, lambda actual, expected: actual >= expected),
        ("delivery_fee", args.max_delivery_fee, lambda actual, expected: actual <= expected),
        ("min_order_amount", args.max_min_order, lambda actual, expected: actual <= expected),
        ("delivery_minutes", args.max_delivery_minutes, lambda actual, expected: actual <= expected),
        ("monthly_sales", args.min_monthly_sales, lambda actual, expected: actual >= expected),
    )
    for key, expected, predicate in checks:
        if expected is None:
            continue
        actual = metrics.get(key)
        if actual is None or not predicate(actual, expected):
            return False
    return not args.require_promotion or bool(metrics.get("promotions"))


def _restaurant_sort_key(item: dict[str, Any], sort_by: str) -> Any:
    metrics = item["metrics"]
    if sort_by == "rating":
        return (-(metrics.get("rating") or -1), metrics.get("distance_meters") or math.inf)
    if sort_by == "delivery-fee":
        return (metrics.get("delivery_fee") if metrics.get("delivery_fee") is not None else math.inf)
    if sort_by == "min-order":
        return (metrics.get("min_order_amount") if metrics.get("min_order_amount") is not None else math.inf)
    if sort_by == "delivery-time":
        return (metrics.get("delivery_minutes") if metrics.get("delivery_minutes") is not None else math.inf)
    if sort_by == "sales":
        sales = metrics.get("monthly_sales")
        return -sales if sales is not None else math.inf
    if sort_by == "recommended":
        return -item["recommendation_score"]
    return (metrics.get("distance_meters") if metrics.get("distance_meters") is not None else math.inf)


def _validate_coordinates(latitude: float, longitude: float) -> None:
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise MeituanError("invalid_coordinates", "经纬度超出有效范围", exit_code=3)


async def _run_login(args: argparse.Namespace) -> dict[str, Any]:
    _require_runtime()
    try:
        from playwright.async_api import Error as PlaywrightError, async_playwright
    except ImportError as exc:
        raise MeituanError("dependency_missing", "缺少 Playwright，请先运行 bootstrap.py", exit_code=3) from exc

    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    previous_windows, previous_foreground = _windows_snapshot()
    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {
            **_login_mobile_options(playwright),
            "user_data_dir": str(profile_dir),
            "headless": False,
            "locale": "zh-CN",
            "args": _login_browser_args(),
        }
        if args.channel != "chromium":
            launch_options["channel"] = args.channel
        context = await playwright.chromium.launch_persistent_context(**launch_options)
        try:
            if args.force:
                await context.clear_cookies()
            page = context.pages[-1] if context.pages else await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(2_000)
            demoted_windows = _show_new_browser_without_activation(previous_windows, previous_foreground)
            initial_cookies = await context.cookies([HOME_URL, "https://i.waimai.meituan.com/"])
            initial_names = {str(cookie.get("name")) for cookie in initial_cookies}
            already_authenticated = "token" in initial_names and ("userId" in initial_names or "uuid" in initial_names)
            login_entry_visible = await page.evaluate(
                """() => {
                    const text = document.body?.innerText || '';
                    const input = document.querySelector(
                        'input[placeholder*="手机号"], input[type="tel"], input[autocomplete="tel"]'
                    );
                    return Boolean(input) || /发送验证码|第三方登录|验证码登录/.test(text);
                }"""
            )
            if not already_authenticated and not login_entry_visible:
                raise MeituanError(
                    "login_entry_missing",
                    "官方登录页未渲染登录入口，请检查网络或稍后重试",
                    retryable=True,
                    exit_code=2,
                    details={"url": page.url, "title": await page.title()},
                )
            print(
                "请在打开的美团官方登录页中使用手机号验证码或页面提供的方式登录；程序不会要求你复制 Cookie。",
                file=sys.stderr,
            )
            deadline = time.monotonic() + args.timeout
            cookie_string = ""
            while time.monotonic() < deadline:
                cookies = await context.cookies([HOME_URL, "https://i.waimai.meituan.com/"])
                names = {str(cookie.get("name")) for cookie in cookies}
                if "token" in names and "userId" in names:
                    cookie_string, delegated_cookie_names = _compact_auth_cookie(cookies)
                    break
                await page.wait_for_timeout(1_500)
            if not cookie_string:
                raise MeituanError("login_timeout", f"{args.timeout} 秒内未检测到登录成功", exit_code=2)
            delegated = os.environ.copy()
            delegated["MEITUAN_SAFE_COOKIE"] = cookie_string
            _run_mt(["auth", "login"], env=delegated)
            status = _run_mt(["auth", "whoami"])
            return {
                "logged_in": bool(status.get("loggedIn")),
                "auth_mode": status.get("authMode"),
                "browser_mode": "mobile",
                "browser_focus_mode": "visible_no_activate" if os.name == "nt" else "visible",
                "browser_windows_adjusted": demoted_windows,
                "delegated_cookie_count": len(delegated_cookie_names),
                "forced": bool(args.force),
                "verification_scope": "stored_credentials_only",
                "api_verified": False,
            }
        finally:
            try:
                await context.close()
            except PlaywrightError as exc:
                # Closing an already-closed browser must not replace the login result.
                if "Target page, context or browser has been closed" not in str(exc):
                    raise


def _run_status() -> dict[str, Any]:
    deal_authorized = _passport_cached_token() is not None
    deal_status = {
        "verification_scope": "stored_credentials_only",
        "in_store_deal_auth": "stored_unverified" if deal_authorized else "not_stored",
        "in_store_deal_auth_mode": "passport_pkce",
        "api_verified": False,
        "note": "状态仅表示本地凭证存在；无 Passport 时可尝试共享登录，使用 auth-check 对照验证",
    }
    try:
        status = _run_mt(["auth", "whoami"])
    except MeituanError as exc:
        if exc.code == "not_logged_in":
            return {"logged_in": False, "auth_mode": None, "shared_auth_state": "not_stored", **deal_status}
        raise
    return {
        "logged_in": bool(status.get("loggedIn")),
        "auth_mode": status.get("authMode"),
        "shared_auth_state": "stored_unverified" if status.get("loggedIn") else "not_stored",
        **deal_status,
    }


def _run_nearby_search(args: argparse.Namespace) -> dict[str, Any]:
    if args.min_rating is not None and not 0 <= args.min_rating <= 5:
        raise MeituanError("invalid_min_rating", "最低评分必须在 0-5 之间", exit_code=3)
    for field, value in (
        ("最高配送费", args.max_delivery_fee),
        ("最高起送价", args.max_min_order),
        ("最长配送时间", args.max_delivery_minutes),
        ("最低月售", args.min_monthly_sales),
    ):
        if value is not None and value < 0:
            raise MeituanError("invalid_filter", f"{field}不能为负数", exit_code=3)
    if args.location:
        candidates = _geocode(args.location, max(args.location_index, 5))
        if not candidates:
            raise MeituanError("location_not_found", f"没有解析到地点：{args.location}", exit_code=3)
        if args.location_index > len(candidates):
            raise MeituanError("invalid_location_index", "地点候选序号超出范围", exit_code=3)
        selected = candidates[args.location_index - 1]
        wgs_lat = selected["latitude_wgs84"]
        wgs_lng = selected["longitude_wgs84"]
        latitude, longitude = _wgs84_to_gcj02(wgs_lat, wgs_lng)
        label = selected["display_name"]
    else:
        if args.lat is None or args.lng is None:
            raise MeituanError("location_required", "提供 --location 或同时提供 --lat/--lng", exit_code=3)
        latitude, longitude = args.lat, args.lng
        _validate_coordinates(latitude, longitude)
        selected = None
        candidates = []
        label = f"coordinates:{latitude},{longitude}"

    raw = _run_mt(
        [
            "waimai",
            "search-at",
            args.keyword,
            "--label",
            label,
            "--lat",
            str(latitude),
            "--lng",
            str(longitude),
            "--page-start",
            str(args.page_start),
            "--pages",
            str(args.pages),
            "--limit",
            str(args.page_size),
            "--sort",
            str(args.upstream_sort),
        ]
    )
    restaurants: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page_items = raw if isinstance(raw, list) else []
    for restaurant in page_items:
        restaurant_id = str(restaurant.get("id") or "")
        if restaurant_id and restaurant_id in seen_ids:
            continue
        if restaurant_id:
            seen_ids.add(restaurant_id)
        restaurants.append(restaurant)
    matched: list[dict[str, Any]] = []
    unknown_distance = 0
    for restaurant in restaurants:
        distance = _distance_meters(restaurant.get("distance"))
        if distance is None:
            unknown_distance += 1
            continue
        if distance <= args.radius:
            item = dict(restaurant)
            item["distance_meters"] = round(distance, 1)
            enriched = _enrich_restaurant(item)
            if _restaurant_matches(enriched["metrics"], args):
                matched.append(enriched)
    matched.sort(key=lambda item: _restaurant_sort_key(item, args.sort_by))
    matched = matched[: args.limit]
    return {
        "query": {
            "keyword": args.keyword,
            "radius_meters": args.radius,
            "location": label,
            "pages_requested": args.pages,
            "page_start": args.page_start,
            "page_size": args.page_size,
            "sort_by": args.sort_by,
            "filters": {
                "min_rating": args.min_rating,
                "max_delivery_fee": args.max_delivery_fee,
                "max_min_order": args.max_min_order,
                "max_delivery_minutes": args.max_delivery_minutes,
                "min_monthly_sales": args.min_monthly_sales,
                "require_promotion": args.require_promotion,
            },
        },
        "coordinates": {"latitude_gcj02": latitude, "longitude_gcj02": longitude},
        "geocode_selected": selected,
        "geocode_alternatives": candidates[1:3] if candidates else [],
        "restaurants": matched,
        "count": len(matched),
        "scanned_count": len(restaurants),
        "unknown_distance_count": unknown_distance,
        "coverage": "meituan_ranked_results_not_exhaustive",
        "score_model": "local_heuristic_v1",
        "performance": {"upstream_processes": 1, "browser_sessions": 1},
    }


def _run_menu_search(args: argparse.Namespace) -> dict[str, Any]:
    if args.min_price is not None and args.min_price < 0:
        raise MeituanError("invalid_min_price", "最低价格不能为负数", exit_code=3)
    if args.max_price is not None and args.max_price < 0:
        raise MeituanError("invalid_max_price", "最高价格不能为负数", exit_code=3)
    if args.min_price is not None and args.max_price is not None and args.min_price > args.max_price:
        raise MeituanError("invalid_price_range", "最低价格不能高于最高价格", exit_code=3)
    if args.min_monthly_sales is not None and args.min_monthly_sales < 0:
        raise MeituanError("invalid_min_monthly_sales", "最低月售不能为负数", exit_code=3)
    raw = _run_mt(["waimai", "menu", args.restaurant_id])
    items = raw.get("items", []) if isinstance(raw, dict) else []
    keyword = args.keyword.casefold().strip()
    matched: list[dict[str, Any]] = []
    for raw_item in items:
        item = dict(raw_item)
        searchable = " ".join(
            str(item.get(key) or "") for key in ("name", "description", "tag", "unit")
        ).casefold()
        if keyword and keyword not in searchable:
            continue
        price = _numeric_value(item.get("min_price"))
        sales = _numeric_value(item.get("month_saled"))
        skus = item.get("skus") or []
        available = not skus or any(sku.get("stock") is None or sku.get("stock", 0) > 0 for sku in skus)
        if args.min_price is not None and (price is None or price < args.min_price):
            continue
        if args.max_price is not None and (price is None or price > args.max_price):
            continue
        if args.min_monthly_sales is not None and (sales is None or sales < args.min_monthly_sales):
            continue
        if args.in_stock and not available:
            continue
        item["metrics"] = {"price": price, "monthly_sales": sales, "available": available}
        matched.append(item)
    if args.sort_by == "price":
        matched.sort(key=lambda item: item["metrics"]["price"] if item["metrics"]["price"] is not None else math.inf)
    elif args.sort_by == "sales":
        matched.sort(
            key=lambda item: (
                -item["metrics"]["monthly_sales"]
                if item["metrics"]["monthly_sales"] is not None
                else math.inf
            )
        )
    else:
        matched.sort(key=lambda item: str(item.get("name") or "").casefold())
    return {
        "restaurant_id": args.restaurant_id,
        "query": {
            "keyword": args.keyword,
            "min_price": args.min_price,
            "max_price": args.max_price,
            "min_monthly_sales": args.min_monthly_sales,
            "in_stock": args.in_stock,
            "sort_by": args.sort_by,
        },
        "items": matched[: args.limit],
        "count": min(len(matched), args.limit),
        "matched_count": len(matched),
        "scanned_count": len(items),
    }


def _run_compare(ids: list[str]) -> dict[str, Any]:
    raw = _run_mt(["waimai", "compare", *ids])
    rows = raw if isinstance(raw, list) else []
    restaurants = [_enrich_restaurant(row) for row in rows if isinstance(row, dict)]
    restaurants.sort(key=lambda item: -item["recommendation_score"])
    return {
        "restaurants": restaurants,
        "best_restaurant_id": restaurants[0].get("id") if restaurants else None,
        "score_model": "local_heuristic_v1",
        "requested_count": len(ids),
        "resolved_count": len(restaurants),
    }


def _capabilities() -> dict[str, Any]:
    return {
        "read": [
            "status",
            "auth-check",
            "geocode",
            "addresses",
            "nearby-search",
            "food-search",
            "deals-search",
            "restaurant",
            "compare",
            "menu",
            "menus",
            "menu-search",
            "order-status",
            "order-detail",
        ],
        "interactive": ["login", "deal-login", "deal-login-status"],
        "unsupported": ["coupon_claiming", "cart_mutation", "order_submission", "payment", "captcha_bypass"],
        "location": {"geocoder": "OpenStreetMap Nominatim", "meituan_coordinates": "GCJ-02"},
        "notes": {
            "promotions": "returned_when_exposed_by_search_response",
            "deals": "read_only_ranked_results_not_exhaustive",
            "food_search": "restaurant_view_distinct_from_deals;official_store_average_only",
            "deal_average_price": "official_or_conservative_package_size_estimate;filter_fails_closed_when_missing",
            "deal_auth": "auto:cached_passport_else_shared;explicit_shared_comparison_supported;token_never_printed",
            "orders": "read_only_and_requires_exact_order_id",
            "recommendation_score": "local_heuristic_not_meituan_ranking",
        },
    }


def _schemas() -> dict[str, Any]:
    return {
        "auth-check": {
            "since": "0.7.0",
            "params": {
                "location": {"type": "string", "required": True},
                "keyword": {"type": "string", "default": "美食"},
            },
            "semantics": "read-only;one search per endpoint using shared credentials;no Passport fallback or login",
        },
        "nearby-search": {
            "since": "0.1.0",
            "params": {
                "keyword": {"type": "string", "required": True},
                "location": {"type": "string", "required_unless": "lat,lng"},
                "radius": {"type": "integer", "default": 1000, "min": 100, "max": 20000},
                "limit": {"type": "integer", "default": 20, "max": 100},
                "pages": {"type": "integer", "default": 1, "min": 1, "max": 10, "since": "0.2.0"},
                "page_start": {"type": "integer", "default": 0, "min": 0, "max": 100, "since": "0.2.0"},
                "page_size": {"type": "integer", "default": 20, "min": 1, "max": 100, "since": "0.2.0"},
                "sort_by": {
                    "type": "string",
                    "default": "distance",
                    "enum": ["distance", "rating", "delivery-fee", "min-order", "delivery-time", "sales", "recommended"],
                    "since": "0.2.0",
                },
                "min_rating": {"type": "number", "min": 0, "max": 5, "since": "0.2.0"},
                "max_delivery_fee": {"type": "number", "min": 0, "since": "0.2.0"},
                "max_min_order": {"type": "number", "min": 0, "since": "0.2.0"},
                "max_delivery_minutes": {"type": "number", "min": 0, "since": "0.2.0"},
                "min_monthly_sales": {"type": "number", "min": 0, "since": "0.2.0"},
                "require_promotion": {"type": "boolean", "default": False, "since": "0.2.0"},
            },
        },
        "deals-search": {
            "since": "0.4.0",
            "params": {
                "auth_source": {"type": "string", "default": "auto", "enum": ["auto", "shared", "passport"], "since": "0.7.0"},
                "keyword": {"type": "string", "required": True},
                "location": {"type": "string", "required_unless": "lat,lng,city_id"},
                "radius": {"type": "integer", "default": 1000, "min": 100, "max": 20000},
                "min_rating": {"type": "number", "min": 0, "max": 5},
                "min_price": {"type": "number", "min": 0},
                "max_price": {"type": "number", "min": 0},
                "min_average_price": {"type": "number", "min": 0},
                "max_average_price": {"type": "number", "min": 0},
                "page": {"type": "integer", "default": 1, "min": 1, "max": 100},
                "page_size": {"type": "integer", "default": 10, "min": 1, "max": 10},
                "query_id": {"type": "string", "required_for_continuation": True},
                "request_id": {"type": "string", "required_for_continuation": True},
                "limit": {"type": "integer", "default": 20, "min": 1, "max": 100},
            },
        },
        "food-search": {
            "since": "0.5.0",
            "params": {
                "auth_source": {"type": "string", "default": "auto", "enum": ["auto", "shared", "passport"], "since": "0.7.0"},
                "keyword": {"type": "string", "required": True},
                "location": {"type": "string", "required_unless": "lat,lng,city_id"},
                "radius": {"type": "integer", "default": 1000, "min": 100, "max": 20000},
                "min_rating": {"type": "number", "min": 0, "max": 5},
                "min_official_average_price": {"type": "number", "min": 0},
                "max_official_average_price": {"type": "number", "min": 0},
                "page": {"type": "integer", "default": 1, "min": 1, "max": 100},
                "page_size": {"type": "integer", "default": 10, "min": 1, "max": 10},
                "query_id": {"type": "string", "required_for_continuation": True},
                "request_id": {"type": "string", "required_for_continuation": True},
                "limit": {"type": "integer", "default": 20, "min": 1, "max": 100},
            },
            "semantics": "returns unique restaurant cards;never substitutes package-derived estimates for official store average",
        },
        "menu-search": {
            "since": "0.2.0",
            "params": {
                "restaurant_id": {"type": "string", "required": True},
                "keyword": {"type": "string", "default": ""},
                "min_price": {"type": "number", "min": 0},
                "max_price": {"type": "number", "min": 0},
                "min_monthly_sales": {"type": "number", "min": 0},
                "in_stock": {"type": "boolean", "default": False},
                "sort_by": {"type": "string", "default": "price", "enum": ["price", "sales", "name"]},
                "limit": {"type": "integer", "default": 50, "max": 200},
            },
        },
        "order-status": {"since": "0.2.0", "params": {"order_id": {"type": "string", "required": True}}},
        "order-detail": {"since": "0.2.0", "params": {"order_id": {"type": "string", "required": True}}},
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全查询美团附近餐厅与菜单")
    parser.add_argument("--profile-dir", default=str(_default_profile_dir()))
    parser.add_argument("--channel", choices=("chrome", "msedge", "chromium"), default="chrome")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="列出当前能力和边界")
    schema = commands.add_parser("schema", help="输出稳定参数契约")
    schema.add_argument(
        "--method",
        dest="schema_method",
        choices=("auth-check", "nearby-search", "food-search", "deals-search", "menu-search", "order-status", "order-detail"),
    )
    login = commands.add_parser("login", help="显示浏览器并等待用户登录")
    login.add_argument("--timeout", type=int, default=600)
    login.add_argument("--force", action="store_true", help="清除专用浏览器 Profile 中的旧 Cookie 后重新登录")
    deal_login = commands.add_parser("deal-login", help="发起到店团购 Passport 授权")
    deal_login.add_argument("--force", action="store_true", help="忽略仍在有效期内的团购授权并重新授权")
    commands.add_parser("deal-login-status", help="单次查询到店团购授权结果")
    commands.add_parser("deal-logout", help="清除本机保存的到店团购授权")
    commands.add_parser("status", help="检查委托登录状态")
    auth_check = commands.add_parser("auth-check", help="用同一份共享登录凭证对照验证外卖与团购，不重新授权")
    auth_check.add_argument("--location", required=True)
    auth_check.add_argument("--keyword", default="美食")
    geocode = commands.add_parser("geocode", help="解析地点候选")
    geocode.add_argument("--location", required=True)
    geocode.add_argument("--limit", type=int, default=5, choices=range(1, 11), metavar="1-10")
    commands.add_parser("addresses", help="列出账号内的配送地址")
    nearby = commands.add_parser("nearby-search", help="按地点和半径搜索餐厅")
    nearby.add_argument("--location")
    nearby.add_argument("--location-index", type=int, default=1)
    nearby.add_argument("--lat", type=float)
    nearby.add_argument("--lng", type=float)
    nearby.add_argument("--keyword", required=True)
    nearby.add_argument("--radius", type=int, default=1000, choices=range(100, 20001), metavar="100-20000")
    nearby.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    nearby.add_argument("--pages", type=int, default=1, choices=range(1, 11), metavar="1-10")
    nearby.add_argument("--page-start", type=int, default=0, choices=range(0, 101), metavar="0-100")
    nearby.add_argument("--page-size", type=int, default=20, choices=range(1, 101), metavar="1-100")
    nearby.add_argument("--upstream-sort", type=int, default=0, choices=range(0, 11), metavar="0-10")
    nearby.add_argument(
        "--sort-by",
        choices=("distance", "rating", "delivery-fee", "min-order", "delivery-time", "sales", "recommended"),
        default="distance",
    )
    nearby.add_argument("--min-rating", type=float)
    nearby.add_argument("--max-delivery-fee", type=float)
    nearby.add_argument("--max-min-order", type=float)
    nearby.add_argument("--max-delivery-minutes", type=float)
    nearby.add_argument("--min-monthly-sales", type=float)
    nearby.add_argument("--require-promotion", action="store_true")
    deals = commands.add_parser("deals-search", help="按地点搜索到店团购套餐")
    deals.add_argument("--auth-source", choices=("auto", "shared", "passport"), default="auto")
    deals.add_argument("--location")
    deals.add_argument("--location-index", type=int, default=1)
    deals.add_argument("--lat", type=float)
    deals.add_argument("--lng", type=float)
    deals.add_argument("--city-id")
    deals.add_argument("--keyword", required=True)
    deals.add_argument("--radius", type=int, default=1000, choices=range(100, 20001), metavar="100-20000")
    deals.add_argument("--min-rating", type=float)
    deals.add_argument("--min-price", type=float)
    deals.add_argument("--max-price", type=float)
    deals.add_argument("--min-average-price", type=float)
    deals.add_argument("--max-average-price", type=float)
    deals.add_argument("--page", type=int, default=1, choices=range(1, 101), metavar="1-100")
    deals.add_argument("--page-size", type=int, default=10, choices=range(1, 11), metavar="1-10")
    deals.add_argument("--query-id", default="")
    deals.add_argument("--request-id", default="")
    deals.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    food = commands.add_parser("food-search", help="按地点搜索到店美食门店（与团购套餐分开）")
    food.add_argument("--auth-source", choices=("auto", "shared", "passport"), default="auto")
    food.add_argument("--location")
    food.add_argument("--location-index", type=int, default=1)
    food.add_argument("--lat", type=float)
    food.add_argument("--lng", type=float)
    food.add_argument("--city-id")
    food.add_argument("--keyword", required=True)
    food.add_argument("--radius", type=int, default=1000, choices=range(100, 20001), metavar="100-20000")
    food.add_argument("--min-rating", type=float)
    food.add_argument("--min-official-average-price", type=float)
    food.add_argument("--max-official-average-price", type=float)
    food.add_argument("--page", type=int, default=1, choices=range(1, 101), metavar="1-100")
    food.add_argument("--page-size", type=int, default=10, choices=range(1, 11), metavar="1-10")
    food.add_argument("--query-id", default="")
    food.add_argument("--request-id", default="")
    food.add_argument("--limit", type=int, default=20, choices=range(1, 101), metavar="1-100")
    restaurant = commands.add_parser("restaurant", help="读取餐厅详情")
    restaurant.add_argument("--id", required=True)
    compare = commands.add_parser("compare", help="比较 2-5 家餐厅")
    compare.add_argument("--ids", required=True, nargs="+", metavar="RESTAURANT_ID")
    menu = commands.add_parser("menu", help="读取餐厅菜单")
    menu.add_argument("--restaurant-id", required=True)
    menus = commands.add_parser("menus", help="批量读取 1-10 家餐厅菜单")
    menus.add_argument("--restaurant-ids", required=True, nargs="+", metavar="RESTAURANT_ID")
    menu_search = commands.add_parser("menu-search", help="按关键词、价格和销量筛选菜品")
    menu_search.add_argument("--restaurant-id", required=True)
    menu_search.add_argument("--keyword", default="")
    menu_search.add_argument("--min-price", type=float)
    menu_search.add_argument("--max-price", type=float)
    menu_search.add_argument("--min-monthly-sales", type=float)
    menu_search.add_argument("--in-stock", action="store_true")
    menu_search.add_argument("--sort-by", choices=("price", "sales", "name"), default="price")
    menu_search.add_argument("--limit", type=int, default=50, choices=range(1, 201), metavar="1-200")
    order_status = commands.add_parser("order-status", help="只读查询订单状态")
    order_status.add_argument("--order-id", required=True)
    order_detail = commands.add_parser("order-detail", help="只读查询订单详情")
    order_detail.add_argument("--order-id", required=True)
    return parser


def _dispatch(args: argparse.Namespace) -> tuple[Any, list[str] | None]:
    if args.command == "capabilities":
        return _capabilities(), ["meituan_cli.py schema"]
    if args.command == "schema":
        schemas = _schemas()
        return schemas.get(args.schema_method) if args.schema_method else schemas, None
    if args.command == "login":
        return asyncio.run(_run_login(args)), ["meituan_cli.py status"]
    if args.command == "deal-login":
        return _run_deal_login(args), ["展示 qr_image_path，让用户用美团 App 扫码授权，然后运行 deal-login-status"]
    if args.command == "deal-login-status":
        return _run_deal_login_status(), None
    if args.command == "deal-logout":
        return _run_deal_logout(), None
    if args.command == "status":
        return _run_status(), None
    if args.command == "auth-check":
        return _run_auth_check(args), None
    if args.command == "geocode":
        return {"query": args.location, "candidates": _geocode(args.location, args.limit)}, None
    if args.command == "addresses":
        return _run_mt(["waimai", "address", "list"]), None
    if args.command == "nearby-search":
        return _run_nearby_search(args), None
    if args.command == "food-search":
        return _run_food_search(args), None
    if args.command == "deals-search":
        return _run_deals_search(args), None
    if args.command == "restaurant":
        return _run_mt(["waimai", "detail", args.id]), None
    if args.command == "compare":
        if not 2 <= len(args.ids) <= 5:
            raise MeituanError("invalid_restaurant_count", "比较餐厅数量必须为 2-5 家", exit_code=3)
        return _run_compare(args.ids), None
    if args.command == "menu":
        return _run_mt(["waimai", "menu", args.restaurant_id]), None
    if args.command == "menus":
        if not 1 <= len(args.restaurant_ids) <= 10:
            raise MeituanError("invalid_restaurant_count", "批量菜单数量必须为 1-10 家", exit_code=3)
        return _run_mt(["waimai", "menus", *args.restaurant_ids]), None
    if args.command == "menu-search":
        return _run_menu_search(args), None
    if args.command == "order-status":
        return _run_mt(["waimai", "order", "status", args.order_id]), None
    if args.command == "order-detail":
        return _run_mt(["waimai", "order", "detail", args.order_id]), None
    raise MeituanError("unknown_command", f"未知命令：{args.command}", exit_code=3)


def _shared_auth_environment() -> dict[str, str]:
    delegated = os.environ.copy()
    delegated.pop("MEITUAN_SAFE_DEAL_TOKEN", None)
    return delegated


def _deal_auth_environment(source: str) -> tuple[str, dict[str, str]]:
    delegated = _shared_auth_environment()
    if source not in ("auto", "shared", "passport"):
        raise MeituanError("invalid_auth_source", "未知认证来源", exit_code=3)
    token = _passport_cached_token() if source != "shared" else None
    if token:
        delegated["MEITUAN_SAFE_DEAL_TOKEN"] = token
        return "passport_pkce", delegated
    if source == "passport":
        raise MeituanError(
            "deal_login_required", "指定的 Passport 授权未保存，请执行 deal-login 或改用 shared",
            exit_code=2, details={"action": "deal-login", "auth_source": "passport_pkce"},
        )
    return "shared", delegated


def _auth_check_failure(error: MeituanError) -> dict[str, Any]:
    state = {
        "not_logged_in": "missing_credentials",
        "auth_rejected": "credential_rejected",
        "deal_auth_rejected": "credential_rejected",
        "access_restricted": "access_restricted",
    }.get(error.code, "unavailable")
    # Do not include raw auth responses or credential previews in diagnostics.
    return {"state": state, "api_verified": False, "error_code": error.code}


def _run_auth_check(args: argparse.Namespace) -> dict[str, Any]:
    delegated = _shared_auth_environment()
    stored = _run_mt(["auth", "whoami"], env=delegated)
    checks: dict[str, Any] = {}
    if not isinstance(stored, dict) or not isinstance(stored.get("loggedIn"), bool):
        raise MeituanError("upstream_invalid_output", "无法识别本地凭证状态")
    if not stored["loggedIn"]:
        checks = {name: {"state": "missing_credentials", "api_verified": False} for name in ("waimai", "deals")}
    else:
        candidates = _geocode(args.location)
        if not candidates:
            raise MeituanError("location_not_found", f"没有解析到地点：{args.location}", exit_code=3)
        selected = candidates[0]
        lat, lng = _wgs84_to_gcj02(selected["latitude_wgs84"], selected["longitude_wgs84"])
        commands = {
            "waimai": ["waimai", "search-at", args.keyword, "--label", args.location,
                       "--lat", str(lat), "--lng", str(lng), "--pages", "1", "--limit", "1"],
            "deals": ["deal", "search", "--keyword", args.keyword, "--address", args.location,
                      "--lat", str(lat), "--lng", str(lng), "--page", "1", "--page-size", "1"],
        }
        for name, command in commands.items():
            try:
                raw = _run_mt(command, env=delegated)
                items = raw if name == "waimai" else (raw.get("products") if isinstance(raw, dict) else None)
                if not isinstance(items, list):
                    raise MeituanError("upstream_invalid_output", "无法识别查询结果")
                checks[name] = {"state": "verified", "api_verified": True, "returned_count": len(items)}
            except MeituanError as exc:
                checks[name] = _auth_check_failure(exc)
    states = {name: check["state"] for name, check in checks.items()}
    verified = all(check["api_verified"] for check in checks.values())
    if verified:
        action = "none"
    elif any(state in ("access_restricted", "unavailable") for state in states.values()):
        action = "investigate_access_or_request"
    elif states["waimai"] in ("missing_credentials", "credential_rejected"):
        action = "login_then_recheck"
    else:
        action = "offer_passport_authorization"
    return {
        "auth_source": "shared", "verification_scope": "live_read_only_searches",
        "shared_login_verified": verified, "checks": checks, "next_action": action,
        "separate_login_required": None,
        "note": "诊断完成不等于登录验证通过；凭证被拒绝不证明过期或必须分别登录；不自动重试或重新授权",
    }


def _login_mobile_options(playwright: Any) -> dict[str, Any]:
    # Render the official H5 login as a mobile page without requiring DevTools.
    device = playwright.devices["Pixel 7"]
    return {
        key: device[key]
        for key in ("user_agent", "viewport", "device_scale_factor", "is_mobile", "has_touch")
    }


def main() -> None:
    _configure_stdio()
    args = _parser().parse_args()
    try:
        data, next_steps = _dispatch(args)
    except MeituanError as exc:
        _emit_error(exc)
        raise SystemExit(exc.exit_code) from None
    except KeyboardInterrupt:
        error = MeituanError("cancelled", "操作已取消", exit_code=1)
        _emit_error(error)
        raise SystemExit(1) from None
    except Exception as exc:
        error = MeituanError("unexpected_error", str(exc))
        _emit_error(error)
        raise SystemExit(1) from None
    _emit(data, next_steps=next_steps)


if __name__ == "__main__":
    main()
