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
        private_key: 网易云音乐开放平台 RSA 私钥。
        access_token: 用户扫码授权后的 Access Token。
        refresh_token: 用户扫码授权后的 Refresh Token。
        token_expires_at: 用户 Access Token 过期时间戳。
        anonymous_access_token: 匿名登录 Access Token，用于二维码轮询。
        device_id: 开放平台设备 ID，需稳定且仅含字母数字。
        notify_fn: 鉴权异常时的通知回调。
    """

    _BASE_URL = "https://openapi.music.163.com"
    _REQUEST_JITTER_RANGE = (0.8, 2.0)

    def __init__(
            self,
            app_id: str,
            private_key: str,
            access_token: str = "",
            refresh_token: str = "",
            token_expires_at: int = 0,
            anonymous_access_token: str = "",
            device_id: str = "",
            notify_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._app_id = (app_id or "").strip()
        self._private_key = (private_key or "").strip()
        self._access_token = (access_token or "").strip()
        self._refresh_token = (refresh_token or "").strip()
        self._token_expires_at = int(token_expires_at or 0)
        self._anonymous_access_token = (anonymous_access_token or "").strip()
        self._device_id = self.normalize_device_id(device_id, self._app_id)
        self._notify = notify_fn or (lambda title, body: None)
        self._headers = {
            "User-Agent": "MoviePilot TraktRatingsSync/3.14",
            "Accept": "application/json",
        }

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
            logger.error("网易云开放平台未配置 PrivateKey")
            return None
        try:
            content = self._format_parameters(params)
            private_key = RSA.import_key(self._format_private_key(self._private_key))
            digest = SHA256.new(content.encode("utf-8"))
            signature = pkcs1_15.new(private_key).sign(digest)
            return base64.b64encode(signature).decode("utf-8")
        except Exception as e:
            logger.error("网易云开放平台签名失败: %s", e)
            return None

    def _device_json(self) -> str:
        device = {
            "clientIp": "192.168.0.1",
            "deviceType": "andrcar",
            "os": "andrcar",
            "appVer": "1.0.0",
            "channel": "moviepilot",
            "model": "MoviePilot",
            "deviceId": self._device_id,
            "brand": "MoviePilot",
            "osVer": "1.0.0",
        }
        return json.dumps(device, ensure_ascii=False, separators=(",", ":"))

    def _build_params(self, biz_content: Dict[str, Any], access_token: str = "") -> Optional[Dict[str, Any]]:
        if not self._app_id:
            logger.error("网易云开放平台未配置 AppID")
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

    def _request(self, endpoint: str, biz_content: Dict[str, Any], access_token: str = "") -> Optional[Dict[str, Any]]:
        params = self._build_params(biz_content=biz_content, access_token=access_token)
        if not params:
            return None

        time.sleep(self._random_delay(endpoint))
        url = f"{self._BASE_URL}{endpoint}"
        response = RequestUtils(headers=self._headers, timeout=15).get_json(url=url, params=params)
        if not response:
            logger.error("网易云开放平台请求失败: endpoint=%s", endpoint)
            return None

        if response.get("code") == 200:
            return response

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
        )
        data = (response or {}).get("data") or {}
        token_data = data.get("accessToken") or {}
        if data.get("status") == 803 and token_data.get("accessToken") not in ("", "null", None):
            self._access_token = token_data.get("accessToken") or ""
            self._refresh_token = token_data.get("refreshToken") or ""
            expires_in = int(token_data.get("expireTime") or 0)
            self._token_expires_at = int(time.time()) + expires_in if expires_in else 0
        return data

    def get_recent_albums(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取最近播放专辑列表，并转换为插件统一专辑结构。"""
        if not self._access_token:
            self._notify(
                "网易云开放平台未登录",
                "请先通过插件的网易云官方二维码登录接口完成扫码授权。",
            )
            return []
        if self._token_expires_at and self._token_expires_at <= int(time.time()):
            self._notify(
                "网易云开放平台 Token 已过期",
                "当前版本尚未配置 refresh 所需的 clientSecret，请重新生成二维码扫码登录。",
            )
            return []

        response = self._request(
            endpoint="/openapi/music/basic/album/play/record/list",
            biz_content={"limit": limit},
            access_token=self._access_token,
        )
        data = (response or {}).get("data") or {}
        records = data.get("records") or []
        return self._format_album_records(records=records, limit=limit)

    def get_token_state(self) -> Dict[str, Any]:
        """返回当前网易云开放平台 Token 状态摘要，不包含敏感值。"""
        return {
            "has_access_token": bool(self._access_token),
            "has_refresh_token": bool(self._refresh_token),
            "has_anonymous_access_token": bool(self._anonymous_access_token),
            "token_expires_at": self._token_expires_at,
            "device_id": self._device_id,
        }

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
