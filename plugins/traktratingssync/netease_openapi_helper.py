# -*- coding: utf-8 -*-
"""
网易云音乐开放平台 API Helper。

用于通过官方开放平台 RSA 签名、匿名登录、二维码登录和最近播放专辑接口
获取用户音乐记录，避免依赖网页 Cookie。
"""
import base64
import hashlib
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from app.log import logger
from app.utils.http import RequestUtils


class NeteaseOpenApiHelper:
    """网易云音乐开放平台 API 封装类。

    Args:
        app_id: 网易云音乐开放平台 AppID。
        app_secret: 网易云音乐开放平台 AppSecret，预留给 Token 刷新接口。
        private_key: 网易云音乐开放平台 RSA 私钥。
        access_token: 用户扫码授权后的 Access Token。
        refresh_token: 用户扫码授权后的 Refresh Token。
        token_expires_at: 用户 Access Token 过期时间戳。
        anonymous_access_token: 匿名登录 Access Token，用于二维码轮询。
        device_id: 开放平台设备 ID，需稳定且仅含字母数字。
        notify_fn: 鉴权异常时的通知回调。
        auth_required_fn: 需要用户重新扫码认证时的回调。
    """

    _BASE_URL = "https://openapi.music.163.com"
    _CLI_VERSION = "0.1.5"
    _REQUEST_JITTER_RANGE = (0.8, 2.0)
    _TOKEN_REFRESH_MARGIN_SECONDS = 3600

    def __init__(
            self,
            app_id: str,
            private_key: str,
            app_secret: str = "",
            access_token: str = "",
            refresh_token: str = "",
            token_expires_at: int = 0,
            anonymous_access_token: str = "",
            device_id: str = "",
            notify_fn: Optional[Callable[[str, str], None]] = None,
            auth_required_fn: Optional[Callable[[], None]] = None,
    ):
        self._app_id = (app_id or "").strip()
        self._app_secret = (app_secret or "").strip()
        self._private_key = (private_key or "").strip()
        self._access_token = (access_token or "").strip()
        self._refresh_token = (refresh_token or "").strip()
        self._token_expires_at = int(token_expires_at or 0)
        self._anonymous_access_token = (anonymous_access_token or "").strip()
        self._device_id = self.normalize_device_id(device_id, self._app_id)
        self._notify = notify_fn or (lambda title, body: None)
        self._auth_required = auth_required_fn or (lambda: None)
        self._headers = {
            "User-Agent": f"ncm-cli/{self._CLI_VERSION} MoviePilot TraktRatingsSync/3.14",
            "Accept": "application/json",
        }
        self._last_error = ""
        self._refresh_attempts = 0
        self._refresh_successes = 0
        self._refresh_failures = 0
        self._auth_required_count = 0
        self._last_refresh_code: Any = None
        self._last_refresh_message = ""
        self._manifest: Dict[str, Any] = {}

    @staticmethod
    def normalize_device_id(device_id: str, app_id: str = "") -> str:
        """规范化网易云开放平台设备 ID，保证稳定且符合官方字符约束。"""
        clean_id = re.sub(r"[^0-9A-Za-z]", "", device_id or "")
        if clean_id:
            return clean_id[:64]
        digest = hashlib.sha256(f"moviepilot:{app_id or 'netease'}".encode("utf-8")).hexdigest()
        return f"MP{digest[:30]}"

    @staticmethod
    def _format_private_key(private_key: str) -> str:
        key = (private_key or "").strip()
        if "BEGIN" in key and "PRIVATE KEY" in key:
            return key
        compact_key = "".join(key.split())
        lines = [compact_key[i:i + 64] for i in range(0, len(compact_key), 64)]
        return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----"

    @staticmethod
    def _format_parameters(params: Dict[str, Any]) -> str:
        filtered_params = {}
        for key, value in params.items():
            if key == "sign" or value == "" or value is None or isinstance(value, bytes):
                continue
            filtered_params[key] = value

        pairs = []
        for key, value in sorted(filtered_params.items(), key=lambda item: item[0]):
            if isinstance(value, bool):
                value = str(value).lower()
            pairs.append(f"{key}={value}")
        return "&".join(pairs)

    def _sign(self, params: Dict[str, Any]) -> Optional[str]:
        if not self._private_key:
            self._last_error = "网易云开放平台未配置 PrivateKey"
            logger.error(self._last_error)
            return None
        try:
            content = self._format_parameters(params)
            private_key = RSA.import_key(self._format_private_key(self._private_key))
            digest = SHA256.new(content.encode("utf-8"))
            signature = pkcs1_15.new(private_key).sign(digest)
            self._last_error = ""
            return base64.b64encode(signature).decode("utf-8")
        except Exception as e:
            self._last_error = self._format_private_key_error(e)
            logger.error("网易云开放平台签名失败: %s", self._last_error)
            return None

    @staticmethod
    def _format_private_key_error(error: Exception) -> str:
        """将私钥导入异常转换为不包含敏感值的用户提示。"""
        error_text = str(error)
        if "base64" in error_text.lower():
            return "网易云开放平台 PrivateKey 格式无效，请从开放平台重新复制完整私钥或粘贴带 BEGIN/END 的 PEM 内容"
        return "网易云开放平台 PrivateKey 无法导入，请确认复制的是 RSA 私钥而不是 PubKey 或 AppSecret"

    @staticmethod
    def _resolve_token_expires_at(expires_value: Any) -> int:
        """将网易云返回的秒数或时间戳统一转换为本地过期时间戳。"""
        expires_seconds = int(expires_value or 0)
        if not expires_seconds:
            return 0
        now_ts = int(time.time())
        if expires_seconds > now_ts:
            return expires_seconds
        return now_ts + expires_seconds

    def _device_json(self) -> str:
        device = {
            "clientIp": "127.0.0.1",
            "deviceType": "openapi",
            "os": "ncmcli",
            "appVer": self._CLI_VERSION,
            "channel": "ncmcli",
            "model": "MoviePilot_cli",
            "deviceId": self._device_id,
            "brand": "ncmcli",
            "osVer": "15.3",
        }
        return json.dumps(device, ensure_ascii=False, separators=(",", ":"))

    def _build_params(self, biz_content: Dict[str, Any], access_token: str = "") -> Optional[Dict[str, Any]]:
        if not self._app_id:
            self._last_error = "网易云开放平台未配置 AppID"
            logger.error(self._last_error)
            return None

        params: Dict[str, Any] = {
            "appId": self._app_id,
            "signType": "RSA_SHA256",
            "timestamp": int(time.time() * 1000),
            "device": self._device_json(),
            "bizContent": json.dumps(biz_content or {}, ensure_ascii=False, separators=(",", ":")),
        }
        if access_token:
            params["accessToken"] = access_token

        sign = self._sign(params)
        if not sign:
            return None
        params["sign"] = sign
        return params

    def _request(
            self,
            endpoint: str,
            biz_content: Dict[str, Any],
            access_token: str = "",
            method: str = "GET",
    ) -> Optional[Dict[str, Any]]:
        params = self._build_params(biz_content=biz_content, access_token=access_token)
        if not params:
            return None

        time.sleep(self._random_delay(endpoint))
        url = f"{self._BASE_URL}{endpoint}"
        request_utils = RequestUtils(headers=self._headers, timeout=15)
        if method.upper() == "POST":
            response = request_utils.post_json(url=url, data={}, params=params)
        else:
            response = request_utils.get_json(url=url, params=params)
        if not response:
            self._last_error = f"网易云开放平台请求失败: {endpoint}"
            logger.error(self._last_error)
            return None

        if response.get("code") == 200 or (endpoint == "/openapi/v1/ncm/cli/manifest" and response.get("manifests")):
            self._last_error = ""
            return response

        self._last_error = response.get("message", "") or f"网易云开放平台返回异常: {response.get('code')}"
        logger.warning(
            "网易云开放平台返回异常: endpoint=%s, code=%s, message=%s",
            endpoint, response.get("code"), response.get("message", ""),
        )
        return response

    def _random_delay(self, endpoint: str) -> float:
        import random

        delay = random.uniform(*self._REQUEST_JITTER_RANGE)
        logger.debug("网易云开放平台请求前随机等待 %.2f 秒: %s", delay, endpoint)
        return delay

    def login_anonymous(self) -> Optional[Dict[str, Any]]:
        """获取匿名 Access Token，用于二维码轮询。"""
        response = self._request(
            endpoint="/openapi/music/basic/oauth2/login/anonymous",
            biz_content={"clientId": self._app_id},
            method="POST",
        )
        data = (response or {}).get("data") or {}
        token = (data.get("accessToken") or "").strip()
        if not token:
            logger.warning("网易云开放平台匿名登录未返回 accessToken")
            return None
        self._anonymous_access_token = token
        return data

    def get_login_qrcode(self) -> Optional[Dict[str, Any]]:
        """生成网易云 App 扫码登录二维码。"""
        anonymous_token = self._anonymous_access_token
        if not anonymous_token:
            anonymous = self.login_anonymous()
            anonymous_token = (anonymous or {}).get("accessToken") or ""
        if not anonymous_token:
            return None

        response = self._request(
            endpoint="/openapi/music/basic/user/oauth2/qrcodekey/get/v2",
            biz_content={"type": 2, "expiredKey": "300"},
            access_token=anonymous_token,
            method="GET",
        )
        data = (response or {}).get("data") or {}
        if not data.get("qrCodeUrl") or not data.get("uniKey"):
            logger.warning("网易云开放平台生成二维码失败: %s", (response or {}).get("message", ""))
            return None
        return data

    def poll_login_qrcode(self, uni_key: str) -> Optional[Dict[str, Any]]:
        """轮询网易云二维码状态，扫码成功时返回用户 Token 信息。"""
        if not uni_key:
            logger.warning("网易云二维码轮询缺少 uniKey")
            return None

        anonymous_token = self._anonymous_access_token
        if not anonymous_token:
            anonymous = self.login_anonymous()
            anonymous_token = (anonymous or {}).get("accessToken") or ""
        if not anonymous_token:
            return None

        response = self._request(
            endpoint="/openapi/music/basic/oauth2/device/login/qrcode/get",
            biz_content={"key": uni_key, "clientId": self._app_id},
            access_token=anonymous_token,
            method="GET",
        )
        data = (response or {}).get("data") or {}
        token_data = data.get("accessToken") or {}
        if data.get("status") == 803 and token_data.get("accessToken") not in ("", "null", None):
            self._access_token = token_data.get("accessToken") or ""
            self._refresh_token = token_data.get("refreshToken") or ""
            self._token_expires_at = self._resolve_token_expires_at(token_data.get("expireTime"))
        return data

    def refresh_access_token(self) -> bool:
        """使用 Refresh Token 刷新网易云开放平台 Access Token。"""
        self._refresh_attempts += 1
        if not self._refresh_token:
            self._last_error = "网易云开放平台缺少 Refresh Token，请重新扫码登录"
            self._mark_refresh_failure("missing_refresh_token", self._last_error)
            logger.warning(self._last_error)
            return False
        if not self._app_secret:
            self._last_error = "网易云开放平台缺少 AppSecret，无法刷新 Access Token"
            self._mark_refresh_failure("missing_app_secret", self._last_error)
            logger.warning(self._last_error)
            return False

        logger.info(
            f"网易云开放平台开始刷新 Access Token: "
            f"has_access_token={bool(self._access_token)}, "
            f"has_refresh_token={bool(self._refresh_token)}, "
            f"has_app_secret={bool(self._app_secret)}"
        )
        response = self._request(
            endpoint="/openapi/music/basic/user/oauth2/token/refresh/v2",
            biz_content={
                "clientId": self._app_id,
                "clientSecret": self._app_secret,
                "refreshToken": self._refresh_token,
            },
            access_token=self._access_token,
            method="POST",
        )
        data = (response or {}).get("data") or {}
        access_token = (data.get("accessToken") or "").strip()
        refresh_token = (data.get("refreshToken") or "").strip()
        if (response or {}).get("code") != 200 or not access_token:
            code = (response or {}).get("code")
            if code in (1407, 1408):
                self._last_error = "网易云开放平台授权已失效，请重新扫码登录"
            else:
                self._last_error = (response or {}).get("message") or "网易云开放平台刷新 Access Token 失败"
            self._mark_refresh_failure(code, self._last_error)
            logger.warning("网易云开放平台刷新 Access Token 失败: code=%s, message=%s", code, self._last_error)
            return False

        self._access_token = access_token
        self._refresh_token = refresh_token or self._refresh_token
        self._token_expires_at = self._resolve_token_expires_at(
            data.get("expiresTime") or data.get("expireIn") or data.get("expireTime")
        )
        self._last_error = ""
        self._refresh_successes += 1
        self._last_refresh_code = 200
        self._last_refresh_message = ""
        logger.info("网易云开放平台 Access Token 刷新成功，expires_at=%s", self._token_expires_at)
        return True

    def _mark_refresh_failure(self, code: Any, message: str) -> None:
        self._refresh_failures += 1
        self._last_refresh_code = code
        self._last_refresh_message = message or ""

    def _trigger_auth_required(self, reason: str) -> None:
        self._auth_required_count += 1
        logger.warning(
            f"网易云开放平台触发重新授权: reason={reason}, "
            f"refresh_attempts={self._refresh_attempts}, "
            f"refresh_successes={self._refresh_successes}, "
            f"refresh_failures={self._refresh_failures}, "
            f"last_refresh_code={self._last_refresh_code}, "
            f"last_refresh_message={self._last_refresh_message}"
        )
        self._auth_required()

    def _ensure_access_token(self) -> bool:
        """在调用实名接口前确保 Access Token 存在且未临近过期。"""
        if not self._access_token:
            if self.refresh_access_token():
                return True
            self._last_error = self._last_error or "网易云开放平台未登录，请先完成扫码授权"
            logger.warning(self._last_error)
            self._trigger_auth_required("missing_access_token")
            return False

        now_ts = int(time.time())
        if self._token_expires_at and self._token_expires_at <= now_ts + self._TOKEN_REFRESH_MARGIN_SECONDS:
            if self.refresh_access_token():
                return True
            if self._token_expires_at > now_ts:
                logger.warning("网易云开放平台 Access Token 预刷新失败，将继续使用尚未过期的现有 Token")
                return True
            self._last_error = self._last_error or "网易云开放平台 Token 已过期，Refresh Token 无法续期"
            logger.warning(self._last_error)
            self._trigger_auth_required("expired_access_token")
            return False

        return True

    def _request_with_access_token(
            self,
            endpoint: str,
            biz_content: Dict[str, Any],
            method: str = "GET",
    ) -> Optional[Dict[str, Any]]:
        """调用实名接口，遇到 Token 过期码时刷新后重试一次。"""
        if not self._ensure_access_token():
            return None

        response = self._request(
            endpoint=endpoint,
            biz_content=biz_content,
            access_token=self._access_token,
            method=method,
        )
        if (response or {}).get("code") != 1406:
            return response

        logger.warning("网易云开放平台接口提示 Access Token 过期，尝试使用 Refresh Token 续期")
        if not self.refresh_access_token():
            self._last_error = self._last_error or "网易云开放平台 Token 已过期，Refresh Token 无法续期"
            logger.warning(self._last_error)
            self._trigger_auth_required("auth_response_1406")
            return response

        return self._request(
            endpoint=endpoint,
            biz_content=biz_content,
            access_token=self._access_token,
            method=method,
        )

    def get_recent_albums(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取最近播放专辑列表，并转换为插件统一专辑结构。"""
        response = self._request_with_access_token(
            endpoint="/openapi/music/basic/album/play/record/list",
            biz_content={"limit": limit},
            method="GET",
        )
        data = (response or {}).get("data") or {}
        records = data.get("records") or []
        return self._format_album_records(records=records, limit=limit)

    def get_favorite_songs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取红心歌单歌曲列表，并转换为插件可展示的歌曲结构。"""
        playlist = self._request_with_access_token(
            endpoint="/openapi/music/basic/playlist/star/get/v2",
            biz_content={"trialScene": "cli"},
            method="POST",
        )
        playlist_id = (((playlist or {}).get("data") or {}).get("id") or "").strip()
        if not playlist_id:
            self._last_error = (playlist or {}).get("message") or "网易云红心歌单接口未返回 playlistId"
            logger.warning(self._last_error)
            return []

        response = self._request_with_access_token(
            endpoint="/openapi/music/basic/playlist/song/list/get/v3",
            biz_content={
                "playlistId": playlist_id,
                "limit": min(max(int(limit or 100), 1), 500),
                "offset": 0,
                "qualityFlag": False,
                "trialScene": "cli",
            },
            method="GET",
        )
        data = (response or {}).get("data") or []
        songs = data if isinstance(data, list) else data.get("list") or data.get("songs") or []
        return self._format_song_records(records=songs, limit=limit)

    def load_manifest(self) -> Dict[str, Any]:
        """按官方 CLI 协议加载动态命令清单，用于诊断开放平台能力。"""
        response = self._request_with_access_token(
            endpoint="/openapi/v1/ncm/cli/manifest",
            biz_content={"cliVersion": self._CLI_VERSION, "cachedVersion": "{}"},
            method="POST",
        )
        self._manifest = (response or {}).get("manifests") or {}
        return self._manifest

    def get_token_state(self) -> Dict[str, Any]:
        """返回当前网易云开放平台 Token 状态摘要，不包含敏感值。"""
        return {
            "has_access_token": bool(self._access_token),
            "has_refresh_token": bool(self._refresh_token),
            "has_anonymous_access_token": bool(self._anonymous_access_token),
            "has_app_secret": bool(self._app_secret),
            "token_expires_at": self._token_expires_at,
            "device_id": self._device_id,
            "manifest_count": len(self._manifest),
            "refresh_attempts": self._refresh_attempts,
            "refresh_successes": self._refresh_successes,
            "refresh_failures": self._refresh_failures,
            "auth_required_count": self._auth_required_count,
            "last_refresh_code": self._last_refresh_code,
            "last_refresh_message": self._last_refresh_message,
        }

    def get_last_error(self) -> str:
        """返回最近一次开放平台失败原因，不包含敏感值。"""
        return self._last_error

    def get_token_values(self) -> Dict[str, Any]:
        """返回需要持久化的网易云开放平台 Token 值。"""
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "token_expires_at": self._token_expires_at,
            "anonymous_access_token": self._anonymous_access_token,
            "device_id": self._device_id,
        }

    @staticmethod
    def _format_album_records(records: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        album_map: Dict[str, Dict[str, Any]] = {}
        for item in records:
            record = item.get("record") or {}
            album_name = record.get("name") or ""
            artists = record.get("artists") or []
            artist = (artists[0] or {}).get("name", "") if artists else ""
            if not album_name:
                continue

            album_id = record.get("id") or ""
            cache_key = album_id or f"{album_name}\t{artist}"
            play_time = int(item.get("playTime") or 0)
            if cache_key not in album_map:
                album_map[cache_key] = {
                    "album": album_name,
                    "artist": artist,
                    "song_count": 0,
                    "total_play_count": 0,
                    "songs": [],
                    "netease_album_id": album_id,
                    "cover_img_url": record.get("coverImgUrl") or "",
                    "play_time": play_time,
                }
            entry = album_map[cache_key]
            entry["total_play_count"] += 1
            entry["play_time"] = max(int(entry.get("play_time") or 0), play_time)

        albums = sorted(album_map.values(), key=lambda x: x.get("play_time", 0), reverse=True)
        return albums[:limit]

    @staticmethod
    def _format_song_records(records: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        songs: List[Dict[str, Any]] = []
        for item in records[:limit]:
            artists = item.get("artists") or item.get("fullArtists") or []
            album = item.get("album") or {}
            ext_map = item.get("extMap") or {}
            songs.append({
                "song": item.get("name") or "",
                "artists": [artist.get("name", "") for artist in artists if artist.get("name")],
                "album": album.get("name") or "",
                "netease_song_id": item.get("id") or "",
                "netease_original_song_id": item.get("originalId") or "",
                "netease_album_id": album.get("id") or "",
                "netease_original_album_id": album.get("originalId") or "",
                "duration": item.get("duration") or 0,
                "liked": bool(item.get("liked")),
                "visible": bool(item.get("visible")),
                "cover_img_url": item.get("coverImgUrl") or "",
                "add_time": int(ext_map.get("addTime") or 0),
            })
        return songs
