# -*- coding: utf-8 -*-
"""
网易云音乐 API Helper
用于查询用户最近听过的歌曲、喜欢的歌曲等信息。
Cookie 失效时通过注入的 notify_fn 通知用户。

直接运行本文件可快速测试：
    python netease_helper.py
或传入 Cookie 字符串：
    NETEASE_COOKIE="MUSIC_U=xxx; __csrf=yyy" python netease_helper.py
"""
import json
import random
import re
import shlex
import string
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import base64

from app.log import logger


class NeteaseHelper:
    """网易云音乐 API 封装类。

    Args:
        cookies: 网易云音乐登录 Cookie 字典（需包含 MUSIC_U）；
                 也可传入 Cookie 字符串，会自动解析。
        notify_fn: Cookie 失效等异常时的通知回调，签名 ``(title: str, body: str) -> None``
    """

    # ---------- Weapi 加密固定常量（与官方 JS 一致，不应随实例变化）----------
    _NONCE = b"0CoJUm6Qyw8W8jud"
    _PUB_KEY = "010001"
    _MODULUS = (
        "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
        "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
        "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
        "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b"
        "3ece0462db0a22b8e7"
    )
    _SECRET_KEY_CHARSET = string.digits + string.ascii_letters
    _REQUEST_JITTER_RANGE = (0.8, 2.0)

    def __init__(
            self,
            cookies: Optional[Any] = None,
            notify_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._notify = notify_fn or (lambda title, body: None)
        parsed_headers: Dict[str, str] = {}
        self._csrf_token = ""
        self._cookie_parse_warnings: List[str] = []

        # 支持字符串或字典两种形式的 Cookie
        # 使用手动 split 解析而非 SimpleCookie，后者对浏览器复制的 Cookie 字符串
        # 解析不可靠（遇到值含 = 等特殊字符时会静默跳过整个字段）
        if isinstance(cookies, str):
            parsed = self._parse_input(cookies)
            self.cookies = parsed.get("cookies", {})
            parsed_headers = parsed.get("headers", {})
            self._csrf_token = parsed.get("csrf_token", "")
            self._cookie_parse_warnings = parsed.get("warnings", [])
        elif isinstance(cookies, dict):
            self.cookies = dict(cookies)
        else:
            self.cookies = {}

        self._base_url = "https://music.163.com"
        self._api_url = f"{self._base_url}/weapi"

        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://music.163.com/",
        }
        if parsed_headers:
            self._headers.update(parsed_headers)

        if not self.cookies.get("MUSIC_U"):
            logger.warning("未提供网易云音乐 Cookie（MUSIC_U），某些接口可能无法使用")
        if self._cookie_parse_warnings:
            for warning in self._cookie_parse_warnings:
                logger.warning("网易云 Cookie 解析警告: %s", warning)
        if self._csrf_token:
            logger.debug("网易云将优先使用 URL 中的 csrf_token，值长度 %d", len(self._csrf_token))

    # ------------------------------------------------------------------
    # 加密工具
    # ------------------------------------------------------------------

    def _aes_encrypt(self, text: str, key: bytes) -> str:
        """AES CBC 加密，返回 base64 编码结果"""
        cipher = AES.new(key, AES.MODE_CBC, b"0102030405060708")
        encrypted = cipher.encrypt(pad(text.encode("utf-8"), AES.block_size))
        return base64.b64encode(encrypted).decode("utf-8")

    def _rsa_encrypt(self, text: str) -> str:
        """RSA 加密（网易云音乐简化版）：反转→大数幂模→十六进制，左补零至 256 字符"""
        reversed_text = text[::-1]
        hex_text = reversed_text.encode("utf-8").hex()
        result = pow(int(hex_text, 16), int(self._PUB_KEY, 16), int(self._MODULUS, 16))
        return format(result, "x").zfill(256)

    def _generate_secret_key(self, length: int = 16) -> str:
        """随机生成 length 位 secret key（字母+数字，与官方 JS 一致）"""
        return "".join(random.choices(self._SECRET_KEY_CHARSET, k=length))

    def _encrypt_params(self, params: Dict[str, Any]) -> Dict[str, str]:
        """Weapi 双重 AES + RSA 加密请求参数"""
        secret_key = self._generate_secret_key(16)
        secret_key_bytes = secret_key.encode("utf-8")
        text = json.dumps(params, separators=(",", ":"))
        first = self._aes_encrypt(text, self._NONCE)
        second = self._aes_encrypt(first, secret_key_bytes)
        return {"params": second, "encSecKey": self._rsa_encrypt(secret_key)}

    def _sleep_before_request(self, endpoint: str) -> None:
        """请求前随机等待，降低批量任务的固定节奏。"""
        delay = random.uniform(*self._REQUEST_JITTER_RANGE)
        logger.debug("网易云请求前随机等待 %.2f 秒: %s", delay, endpoint)
        time.sleep(delay)

    @staticmethod
    def _cookie_string_to_dict(cookie_string: str) -> Dict[str, str]:
        """将 Cookie 字符串转为字典。"""
        cookies: Dict[str, str] = {}
        for part in cookie_string.split(";"):
            part = part.strip()
            if "=" in part:
                key, _, value = part.partition("=")
                cookies[key.strip()] = value.strip()
        return cookies

    @staticmethod
    def _extract_cookie_values(cookie_string: str, key: str) -> List[str]:
        """从 Cookie 字符串中提取指定 key 的所有值，保留重复项。"""
        values: List[str] = []
        for part in cookie_string.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            if name.strip() == key:
                values.append(value.strip())
        return values

    @classmethod
    def _parse_input(cls, raw_value: str) -> Dict[str, Any]:
        """兼容纯 Cookie 字符串或完整 curl，提取网易云 Cookie 和关键请求头。"""
        text = (raw_value or "").strip()
        if not text:
            return {"cookies": {}, "headers": {}, "csrf_token": "", "warnings": []}
        if not text.lower().startswith("curl "):
            return {"cookies": cls._cookie_string_to_dict(text), "headers": {}, "csrf_token": "", "warnings": []}

        normalized = re.sub(r"\\\n\s*", " ", text).strip()
        try:
            parts = shlex.split(normalized)
        except Exception:
            logger.warning("解析网易云 cURL 失败，回退为原始 Cookie 字符串")
            return {"cookies": cls._cookie_string_to_dict(text), "headers": {}, "csrf_token": "", "warnings": []}

        cookie_string = ""
        headers: Dict[str, str] = {}
        csrf_token = ""
        warnings: List[str] = []
        allow_headers = {
            "Accept",
            "Accept-Language",
            "Content-Type",
            "Origin",
            "Referer",
            "User-Agent",
        }
        for part in parts:
            if part.startswith("http://") or part.startswith("https://"):
                parsed_url = urlparse(part)
                qs = parse_qs(parsed_url.query)
                csrf_token = (qs.get("csrf_token") or [""])[0].strip()
                if csrf_token:
                    break
        for i, part in enumerate(parts):
            if part in ("-b", "--cookie") and i + 1 < len(parts):
                cookie_string = parts[i + 1].strip()
                continue
            if part in ("-H", "--header") and i + 1 < len(parts):
                header = parts[i + 1]
                if header.lower().startswith("cookie:"):
                    cookie_string = header.split(":", 1)[1].strip()
                    continue
                if ":" in header:
                    key, value = header.split(":", 1)
                    key = key.strip()
                    value = value.strip()
                    if key in allow_headers:
                        headers[key] = value

        if cookie_string:
            csrf_values = cls._extract_cookie_values(cookie_string, "__csrf")
            if len(csrf_values) > 1:
                unique_values = list(dict.fromkeys(csrf_values))
                warnings.append(f"检测到重复 __csrf，值为 {unique_values}")
            if csrf_token and csrf_values and csrf_token != csrf_values[-1]:
                warnings.append(
                    f"URL 中 csrf_token={csrf_token} 与 Cookie 中最终 __csrf={csrf_values[-1]} 不一致，将优先使用 URL 参数"
                )
            logger.debug("已从网易云 cURL 中提取 Cookie、请求头和 csrf_token")
            return {
                "cookies": cls._cookie_string_to_dict(cookie_string),
                "headers": headers,
                "csrf_token": csrf_token,
                "warnings": warnings,
            }

        logger.warning("未能从网易云 cURL 中提取 Cookie")
        return {"cookies": {}, "headers": headers, "csrf_token": csrf_token, "warnings": warnings}

    def _auth_context(self) -> str:
        """返回当前网易云鉴权上下文摘要。"""
        return (
            f"cookie_count={len(self.cookies)}, "
            f"has_music_u={bool(self.cookies.get('MUSIC_U'))}, "
            f"has_csrf={bool(self.cookies.get('__csrf') or self._csrf_token)}, "
            f"csrf_source={'url' if self._csrf_token else 'cookie'}, "
            f"user_agent={self._headers.get('User-Agent', '')[:120]}"
        )

    def _notify_auth_failure(self, message: str, detail: str) -> None:
        """统一记录并通知网易云鉴权失败。"""
        logger.error("%s 详情: %s", message, detail)
        self._notify("网易云 Cookie 已失效", f"{message}\n{detail}")

    # ------------------------------------------------------------------
    # 网络请求
    # ------------------------------------------------------------------

    def _request(
            self,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            method: str = "POST",
    ) -> Optional[Dict[str, Any]]:
        """发送请求到网易云音乐 Weapi，自动加密参数。

        Args:
            endpoint: API 路径（不含 base URL），例如 ``"nuser/account/get"``
            params: 业务参数（加密前）
            method: HTTP 方法，默认 POST

        Returns:
            响应 JSON（code==200 时），失败返回 None
        """
        url = f"{self._api_url}/{endpoint}"
        request_params = dict(params or {})
        request_params["csrf_token"] = self._csrf_token or self.cookies.get("__csrf", "")

        try:
            self._sleep_before_request(endpoint)
            if method.upper() == "POST":
                encrypted = self._encrypt_params(request_params)
                response = requests.post(
                    url=url,
                    data=encrypted,
                    headers=self._headers,
                    cookies=self.cookies,
                    timeout=15,
                )
            else:
                response = requests.get(
                    url=url,
                    params=request_params,
                    headers=self._headers,
                    cookies=self.cookies,
                    timeout=15,
                )

            response.raise_for_status()
            data = response.json()

            if data.get("code") == 200:
                return data

            code = data.get("code")
            # 301 / 401 通常表示未登录或 Cookie 失效
            if code in (301, 401):
                msg = "网易云音乐 Cookie 已失效或填写错误，请重新从浏览器复制 MUSIC_U / __csrf，或直接粘贴包含 Cookie 的完整 cURL。"
                detail = (
                    f"endpoint={endpoint}, code={code}, api_msg={data.get('msg', data.get('message', ''))}, "
                    f"{self._auth_context()}"
                )
                self._notify_auth_failure(msg, detail)
                return None

            logger.warning(
                "网易云音乐 API 返回错误: endpoint=%s, code=%s, msg=%s",
                endpoint, code, data.get("msg", data.get("message", "")),
            )
            return None

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403):
                msg = "网易云音乐请求被拒绝，Cookie 可能已失效、填写错误或被风控，请重新复制 Cookie 或完整 cURL。"
                detail = (
                    f"endpoint={endpoint}, status={e.response.status_code}, body={(e.response.text or '')[:200]}, "
                    f"{self._auth_context()}"
                )
                self._notify_auth_failure(msg, detail)
            else:
                logger.error("网易云音乐 API HTTP 错误: %s", e)
        except requests.RequestException as e:
            logger.error("网易云音乐 API 请求异常: %s", e)
        except Exception as e:
            logger.error("网易云音乐 API 处理异常: %s", e, exc_info=True)
        return None

    # ------------------------------------------------------------------
    # 用户相关接口
    # ------------------------------------------------------------------

    def get_user_info(self, uid: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """获取用户信息（不传 uid 则获取当前登录用户）"""
        if uid:
            result = self._request("v1/user/detail", {"uid": uid})
        else:
            result = self._request("nuser/account/get", {})
        if result:
            return result.get("profile") or result.get("account")
        return None

    def get_user_playlist(self, uid: int, limit: int = 30, offset: int = 0) -> List[Dict[str, Any]]:
        """获取用户歌单列表"""
        result = self._request("user/playlist", {"uid": uid, "limit": limit, "offset": offset})
        return result.get("playlist", []) if result else []

    def get_liked_songs(self, uid: int) -> List[int]:
        """获取用户喜欢的音乐 ID 列表"""
        result = self._request("song/like/get", {"uid": uid})
        return result.get("ids", []) if result else []

    # ------------------------------------------------------------------
    # 歌曲相关接口
    # ------------------------------------------------------------------

    def get_song_detail(self, song_ids: List[int]) -> List[Dict[str, Any]]:
        """获取歌曲详细信息（最多 1000 首）"""
        song_ids = song_ids[:1000]
        params: Dict[str, Any] = {
            "c": json.dumps([{"id": sid} for sid in song_ids], separators=(",", ":")),
            "ids": json.dumps(song_ids, separators=(",", ":")),
        }
        result = self._request("v3/song/detail", params)
        if result:
            return [self._format_song(song) for song in result.get("songs", [])]
        return []

    @staticmethod
    def _format_song(song: Dict[str, Any]) -> Dict[str, Any]:
        """将原始歌曲数据格式化为统一结构"""
        return {
            "id": song.get("id"),
            "name": song.get("name"),
            "artists": [artist.get("name") for artist in song.get("ar", [])],
            "album": song.get("al", {}).get("name"),
            "duration": song.get("dt", 0) // 1000,
            "album_pic": song.get("al", {}).get("picUrl"),
        }

    # ------------------------------------------------------------------
    # 播放记录相关接口
    # ------------------------------------------------------------------

    def get_current_uid(self) -> Optional[int]:
        """获取当前登录用户的 uid（通过 nuser/account/get）"""
        result = self._request("nuser/account/get", {})
        if result:
            uid = (result.get("account") or {}).get("id")
            if uid:
                return int(uid)
            logger.warning(
                "网易云 nuser/account/get 返回成功但未包含 account.id: keys=%s, %s",
                list(result.keys())[:20],
                self._auth_context(),
            )
        else:
            logger.warning("网易云 nuser/account/get 未返回有效数据: %s", self._auth_context())
        return None

    def get_recent_played(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取当前登录用户最近一周的听歌排行（需要有效 MUSIC_U Cookie）。

        使用 v1/play/record?type=1（最近一周），按播放次数降序。

        Args:
            limit: 返回数量限制

        Returns:
            播放记录列表，每项含 id / name / artists / album / play_count / score
        """
        if not self.cookies.get("MUSIC_U"):
            msg = "获取最近播放记录需要登录，请提供有效的网易云 Cookie 或完整 cURL。"
            detail = self._auth_context()
            self._notify_auth_failure(msg, detail)
            return []

        uid = self.get_current_uid()
        if not uid:
            msg = "无法获取当前用户 uid，Cookie 可能已失效、填写错误或缺少 __csrf。"
            detail = self._auth_context()
            self._notify_auth_failure(msg, detail)
            return []

        records = self.get_user_record(uid, record_type=1)
        return records[:limit]

    def get_user_record(self, uid: int, record_type: int = 1) -> List[Dict[str, Any]]:
        """获取用户听歌排行榜（v1/play/record）。

        Args:
            uid: 用户 ID
            record_type: 1 为最近一周，0 为所有时间

        Returns:
            听歌记录列表
        """
        result = self._request("v1/play/record", {"uid": uid, "type": record_type})
        if not result:
            return []
        key = "weekData" if record_type == 1 else "allData"
        return [self._format_play_record(r) for r in result.get(key, [])]

    @staticmethod
    def _format_play_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """将 v1/play/record 返回的单条记录格式化为统一结构"""
        song = record.get("song", {})
        return {
            "id": song.get("id"),
            "name": song.get("name"),
            "artists": [artist.get("name") for artist in song.get("ar", [])],
            "album": song.get("al", {}).get("name"),
            "play_count": record.get("playCount", 0),
            "score": record.get("score", 0),
        }

    # ------------------------------------------------------------------
    # 歌单相关接口
    # ------------------------------------------------------------------

    def get_playlist_detail(self, playlist_id: int) -> Optional[Dict[str, Any]]:
        """获取歌单详情（含全部歌曲）"""
        result = self._request("v3/playlist/detail", {"id": playlist_id, "n": 100000})
        if not result:
            return None
        playlist = result.get("playlist", {})
        return {
            "id": playlist.get("id"),
            "name": playlist.get("name"),
            "description": playlist.get("description"),
            "cover_img_url": playlist.get("coverImgUrl"),
            "track_count": playlist.get("trackCount", 0),
            "play_count": playlist.get("playCount", 0),
            "create_time": playlist.get("createTime"),
            "update_time": playlist.get("updateTime"),
            "creator": {
                "user_id": playlist.get("creator", {}).get("userId"),
                "nickname": playlist.get("creator", {}).get("nickname"),
            },
            "tracks": [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "artists": [a.get("name") for a in t.get("ar", [])],
                    "album": t.get("al", {}).get("name"),
                }
                for t in playlist.get("tracks", [])
            ],
        }

    # ------------------------------------------------------------------
    # 搜索接口
    # ------------------------------------------------------------------

    def search(
            self,
            keyword: str,
            search_type: int = 1,
            limit: int = 30,
            offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """搜索音乐（1: 单曲, 10: 专辑, 100: 歌手, 1000: 歌单, 1002: 用户）"""
        result = self._request(
            "cloudsearch/get/web",
            {"s": keyword, "type": search_type, "limit": limit, "offset": offset},
        )
        if not result:
            return []
        result_key = {1: "songs", 10: "albums", 100: "artists", 1000: "playlists", 1002: "userprofiles"}.get(
            search_type, "songs"
        )
        return result.get("result", {}).get(result_key, [])

    # ------------------------------------------------------------------
    # 整合接口
    # ------------------------------------------------------------------

    def get_favorite_songs_with_details(self, uid: int, limit: int = 100) -> List[Dict[str, Any]]:
        """获取用户喜欢的音乐并返回详细信息"""
        liked_ids = self.get_liked_songs(uid)
        if not liked_ids:
            logger.info("用户 %s 没有喜欢的歌曲", uid)
            return []
        return self.get_song_detail(liked_ids[:limit])

    def get_recent_albums(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取最近一周听歌记录并按专辑聚合去重。

        聚合逻辑：
        - 拉取最近一周播放记录（v1/play/record?type=1）
        - 以专辑名为 key 聚合，累计播放次数，收集曲目列表（去重）
        - 按累计播放次数降序，最多返回 limit 张专辑

        Args:
            limit: 返回专辑数量上限

        Returns:
            专辑列表，每项含 album / artist / song_count / total_play_count / songs
        """
        records = self.get_recent_played(limit=1000)
        if not records:
            return []

        album_map: Dict[str, Dict[str, Any]] = {}
        for rec in records:
            album_name = rec.get("album") or rec.get("name") or "未知专辑"
            artists = rec.get("artists") or []
            artist = artists[0] if artists else ""
            song_name = rec.get("name") or ""
            play_count = int(rec.get("play_count") or 0)

            if album_name not in album_map:
                album_map[album_name] = {
                    "album": album_name,
                    "artist": artist,
                    "song_count": 0,
                    "total_play_count": 0,
                    "songs": [],
                }
            entry = album_map[album_name]
            entry["total_play_count"] += play_count
            if song_name and song_name not in entry["songs"]:
                entry["songs"].append(song_name)
                entry["song_count"] += 1

        albums = sorted(album_map.values(), key=lambda x: x["total_play_count"], reverse=True)
        return albums[:limit]


# ---------------------------------------------------------------------------
# 本地测试入口（直接 python netease_helper.py 运行）
# ---------------------------------------------------------------------------

def _print_sep(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main() -> None:
    """交互式测试 NeteaseHelper 各步骤，方便诊断 Cookie 问题。"""
    import os
    import sys
    import logging

    # 用标准 logging 替换 app.log，使其可独立运行
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)-8s %(name)s  %(message)s",
        stream=sys.stdout,
    )

    # ------------------------------------------------------------------ #
    # 1. 读取 Cookie
    # ------------------------------------------------------------------ #
    _print_sep("Step 1 · 读取 Cookie")

    cookie_str = os.environ.get("NETEASE_COOKIE", "").strip()
    if not cookie_str:
        print("未通过环境变量 NETEASE_COOKIE 传入 Cookie，请手动粘贴（回车两次结束）：")
        lines = []
        while True:
            line = input()
            if line == "":
                break
            lines.append(line)
        cookie_str = " ".join(lines).strip()

    if not cookie_str:
        print("[ERROR] 未提供任何 Cookie，退出。")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 2. 解析 Cookie
    # ------------------------------------------------------------------ #
    _print_sep("Step 2 · 解析 Cookie（手动 split）")

    parsed: Dict[str, str] = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            parsed[key.strip()] = value.strip()

    print(f"共解析到 {len(parsed)} 个 Cookie 字段：")
    for k, v in parsed.items():
        display_v = v[:20] + "..." if len(v) > 20 else v
        print(f"  {k:30s} = {display_v}")

    if "MUSIC_U" not in parsed:
        print("\n[ERROR] Cookie 中不含 MUSIC_U，请重新从浏览器复制完整 Cookie。")
        sys.exit(1)
    else:
        print(f"\n[OK] MUSIC_U 已找到，长度 {len(parsed['MUSIC_U'])} 字符")

    # ------------------------------------------------------------------ #
    # 3. 初始化 NeteaseHelper
    # ------------------------------------------------------------------ #
    _print_sep("Step 3 · 初始化 NeteaseHelper")

    def _notify(title: str, body: str) -> None:
        print(f"[NOTIFY] {title}: {body}")

    helper = NeteaseHelper(cookies=parsed, notify_fn=_notify)
    print(f"MUSIC_U in helper.cookies: {bool(helper.cookies.get('MUSIC_U'))}")
    print(f"__csrf  in helper.cookies: {helper.cookies.get('__csrf', '(未找到)')}")

    # ------------------------------------------------------------------ #
    # 4. 获取当前登录用户 uid
    # ------------------------------------------------------------------ #
    _print_sep("Step 4 · 获取当前用户 uid（nuser/account/get）")

    uid = helper.get_current_uid()
    if not uid:
        print("[ERROR] 获取 uid 失败，Cookie 可能已失效或被风控，退出。")
        sys.exit(1)
    print(f"[OK] uid = {uid}")

    # ------------------------------------------------------------------ #
    # 5. 拉取最近一周播放记录（原始）
    # ------------------------------------------------------------------ #
    _print_sep("Step 5 · 拉取最近一周听歌排行（v1/play/record?type=1）")

    raw_result = helper._request("v1/play/record", {"uid": uid, "type": 1})
    if raw_result is None:
        print("[ERROR] v1/play/record 接口返回 None，可能被风控或需要登录。")
        sys.exit(1)

    week_data = raw_result.get("weekData") or []
    print(f"[OK] weekData 条数：{len(week_data)}")
    if not week_data:
        print("[WARN] weekData 为空，可能近一周没有播放记录，尝试拉取全部时间（type=0）...")
        raw_all = helper._request("v1/play/record", {"uid": uid, "type": 0})
        all_data = (raw_all or {}).get("allData") or []
        print(f"      allData 条数：{len(all_data)}")
        if not all_data:
            print("[ERROR] allData 也为空，账号可能关闭了听歌记录权限。请到网易云 App：")
            print("        设置 → 隐私 → 允许他人查看我的播放记录 → 开启")
            sys.exit(1)
        week_data = all_data  # 降级使用全部记录继续测试

    # 打印前 5 条原始记录
    print("\n前 5 条原始记录示例：")
    for i, rec in enumerate(week_data[:5], 1):
        song = rec.get("song", {})
        artists = [a.get("name") for a in song.get("ar", [])]
        album = song.get("al", {}).get("name", "-")
        print(f"  {i}. {song.get('name', '-')} - {'/'.join(artists)} "
              f"| 专辑: {album} | 播放次数: {rec.get('playCount', 0)}")

    # ------------------------------------------------------------------ #
    # 6. 通过 get_recent_played 整合接口获取
    # ------------------------------------------------------------------ #
    _print_sep("Step 6 · 通过 get_recent_played() 获取格式化记录")

    records = helper.get_recent_played(limit=100)
    print(f"[OK] 格式化记录数：{len(records)}")
    for i, r in enumerate(records[:5], 1):
        print(f"  {i}. {r.get('name', '-')} | 专辑: {r.get('album', '-')} "
              f"| 播放次数: {r.get('play_count', 0)}")

    # ------------------------------------------------------------------ #
    # 7. 按专辑聚合
    # ------------------------------------------------------------------ #
    _print_sep("Step 7 · 专辑聚合（get_recent_albums）")

    albums = helper.get_recent_albums(limit=20)
    print(f"[OK] 聚合专辑数：{len(albums)}")
    for i, alb in enumerate(albums[:10], 1):
        print(f"  {i:2d}. 《{alb['album']}》 - {alb['artist']} "
              f"| 曲目: {alb['song_count']} 首 | 累计播放: {alb['total_play_count']} 次")

    _print_sep("全部测试通过 ✅")


if __name__ == "__main__":
    main()
