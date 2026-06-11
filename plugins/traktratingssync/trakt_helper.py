# -*- coding: utf-8 -*-
"""
Trakt API 封装模块

负责所有与 Trakt 相关的网络请求、OAuth 授权、评分拉取和播放进度拉取逻辑。
__init__.py 只需实例化 TraktHelper 并调用其方法即可，不包含任何 Trakt 业务细节。
"""
import asyncio
import math
import random
import time
from typing import Any, Callable, Dict, List, Optional

from app.chain.media import MediaChain
from app.core.config import global_vars
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import RequestUtils


class TraktHelper:
    """Trakt API 封装，提供评分拉取、播放进度拉取和 OAuth 授权能力。

    Args:
        client_id: Trakt Client ID（必填，用于公开接口）
        client_secret: Trakt Client Secret（可选，用于设备码授权）
        access_token: 已有的 Trakt Access Token（可选，优先于自动授权）
        username: Trakt 用户名（用于公开评分接口）
        save_data_fn: 持久化回调，签名 ``(key: str, value: Any) -> None``
        get_data_fn: 读取持久化数据回调，签名 ``(key: str) -> Any``
        update_config_fn: 更新插件配置回调，签名 ``(config: dict) -> None``
        send_notification_fn: 发送通知回调（可选），签名 ``(title: str, body: str) -> None``
        manual_mappings: Trakt 条目到豆瓣 subject_id 的手动映射
    """

    # 协议固定常量，保持类级
    _API_BASE = "https://api.trakt.tv"
    _API_VERSION = "2"
    _REQUEST_JITTER_RANGE = (0.5, 1.5)

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        access_token: str,
        username: str,
        save_data_fn: Callable[[str, Any], None],
        get_data_fn: Callable[[str], Any],
        update_config_fn: Callable[[Dict[str, Any]], None],
        send_notification_fn: Optional[Callable[[str, str], None]] = None,
        manual_mappings: Optional[Dict[str, str]] = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._username = username
        self._save_data = save_data_fn
        self._get_data = get_data_fn
        self._update_config = update_config_fn
        self._notify = send_notification_fn or (lambda title, body: None)
        self._manual_mappings = {
            str(key).strip().lower(): str(value).strip()
            for key, value in (manual_mappings or {}).items()
            if str(key).strip() and str(value).strip()
        }
        self._last_oauth_unauthorized = False

        # 实例级基础请求头（含 api-key，避免每处重复构建）
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "trakt-api-version": self._API_VERSION,
            "trakt-api-key": self._client_id,
        }

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _build_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构建请求头，可附加额外字段（如 Authorization）。

        Args:
            extra: 需要附加或覆盖的额外请求头字段

        Returns:
            合并后的请求头字典（不修改 ``self._headers``）。
        """
        headers = dict(self._headers)
        if extra:
            headers.update(extra)
        return headers

    def _sleep_before_request(self, action: str) -> None:
        """请求前随机等待，降低周期任务的固定节奏。"""
        delay = random.uniform(*self._REQUEST_JITTER_RANGE)
        logger.debug("Trakt %s 前随机等待 %.2f 秒", action, delay)
        time.sleep(delay)

    @staticmethod
    def _trakt_rating_to_douban(trakt_rating: int) -> int:
        """Trakt 1-10 评分转为豆瓣 1-5 星。"""
        if trakt_rating <= 0:
            return 1
        return int(math.ceil(trakt_rating / 2))

    def reset_oauth_unauthorized(self) -> None:
        """重置最近一次 OAuth 请求 401 标记。"""
        self._last_oauth_unauthorized = False

    def has_oauth_unauthorized(self) -> bool:
        """判断最近一次 OAuth 接口请求是否返回 401。"""
        return self._last_oauth_unauthorized

    # ------------------------------------------------------------------
    # 公开评分接口（仅需 client_id）
    # ------------------------------------------------------------------

    def fetch_ratings(self, media_type: str) -> List[Dict[str, Any]]:
        """拉取 Trakt 用户评分列表。

        Args:
            media_type: ``"movies"`` 或 ``"shows"``

        Returns:
            Trakt 返回的评分项列表，失败时返回空列表。
        """
        if not self._username or not self._client_id:
            return []

        if media_type == "movies":
            url = f"{self._API_BASE}/users/{self._username}/ratings/movies"
        elif media_type == "shows":
            url = f"{self._API_BASE}/users/{self._username}/ratings/shows"
        else:
            logger.warning("fetch_ratings: 未知 media_type=%s", media_type)
            return []

        try:
            self._sleep_before_request(f"fetch_ratings/{media_type}")
            resp = RequestUtils(timeout=30, headers=self._headers).get_res(url=url)
            if resp is None:
                logger.warning("Trakt API 请求失败（网络或超时）")
                return []
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, list):
                    logger.warning("Trakt API 返回格式异常，期望数组")
                    return []
                return data
            if resp.status_code == 429:
                logger.warning("Trakt API 触发频率限制（429），请稍后再试")
            elif resp.status_code == 403:
                logger.warning("Trakt API 拒绝访问（403），请检查 Client ID 或该用户评分是否设为私有")
            elif resp.status_code == 404:
                logger.warning("Trakt 用户不存在或未公开评分: %s", self._username)
            else:
                logger.warning("Trakt API 返回异常: status=%s body=%s",
                               resp.status_code, (resp.text or "")[:200])
        except Exception as e:
            logger.error("拉取 Trakt 评分失败: %s", e, exc_info=True)
        return []

    # ------------------------------------------------------------------
    # 播放进度接口（需要 OAuth Access Token）
    # ------------------------------------------------------------------

    def fetch_playback(self, path: str, access_token: str) -> List[Dict[str, Any]]:
        """拉取 Trakt 播放进度列表。

        Args:
            path: 相对路径，例如 ``"/sync/playback/episodes"``
            access_token: 有效的 Trakt Access Token

        Returns:
            播放进度列表，失败时返回空列表。
        """
        headers = self._build_headers({"Authorization": f"Bearer {access_token}"})
        url = f"{self._API_BASE}{path}"
        try:
            self._sleep_before_request(f"fetch_playback/{path}")
            resp = RequestUtils(timeout=20, headers=headers).get_res(url=url)
            if resp is None:
                logger.debug("Trakt 播放进度请求失败: %s", path)
                return []
            if resp.status_code == 204:
                return []
            if resp.status_code != 200:
                if resp.status_code == 401:
                    self._last_oauth_unauthorized = True
                    logger.warning("Trakt Access Token 无效或已过期，无法拉取播放进度: %s", path)
                else:
                    logger.warning("Trakt 播放进度返回异常 %s: %s %s",
                                   path, resp.status_code, (resp.text or "")[:200])
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error("拉取 Trakt 播放进度失败 %s: %s", path, e, exc_info=True)
            return []

    def fetch_history(self, media_type: str, access_token: str, limit: int = 20) -> List[Dict[str, Any]]:
        """拉取 Trakt 最近观看历史。

        Args:
            media_type: Trakt 历史类型，例如 ``"shows"``。
            access_token: 有效的 Trakt Access Token。
            limit: 返回条数上限。

        Returns:
            最近观看历史列表，失败时返回空列表。
        """
        headers = self._build_headers({"Authorization": f"Bearer {access_token}"})
        url = f"{self._API_BASE}/sync/history/{media_type}"
        try:
            self._sleep_before_request(f"fetch_history/{media_type}")
            resp = RequestUtils(timeout=20, headers=headers).get_res(
                url=url,
                params={"limit": max(1, int(limit or 20))},
            )
            if resp is None:
                logger.debug("Trakt 观看历史请求失败: %s", media_type)
                return []
            if resp.status_code == 204:
                return []
            if resp.status_code != 200:
                if resp.status_code == 401:
                    self._last_oauth_unauthorized = True
                    logger.warning("Trakt Access Token 无效或已过期，无法拉取观看历史: %s", media_type)
                else:
                    logger.warning(
                        "Trakt 观看历史返回异常 %s: %s %s",
                        media_type,
                        resp.status_code,
                        (resp.text or "")[:200],
                    )
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error("拉取 Trakt 观看历史失败 %s: %s", media_type, e, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # 豆瓣信息匹配（MoviePilot 映射桥接）
    # ------------------------------------------------------------------

    async def _get_douban_info_by_tmdb(
        self,
        tmdb_id: Optional[int],
        imdb_id: Optional[str],
        title: Optional[str] = None,
        year: Optional[int] = None,
        mtype: MediaType = MediaType.MOVIE,
    ) -> Dict[str, Any]:
        """通过 MoviePilot 媒体链获取豆瓣 subject_id 和中文标题。"""
        douban_info = None
        media_chain = MediaChain()
        if tmdb_id:
            try:
                douban_info = await media_chain.async_get_doubaninfo_by_tmdbid(
                    tmdbid=int(tmdb_id), mtype=mtype
                )
                if douban_info and douban_info.get("id"):
                    logger.debug("MoviePilot 映射豆瓣信息 (TMDB %s): %s", tmdb_id, douban_info)
                    return douban_info
            except Exception as e:
                logger.debug("MoviePilot TMDB %s 映射豆瓣失败: %s", tmdb_id, e)
            return {}

        if title or imdb_id:
            try:
                douban_info = await media_chain.async_match_doubaninfo(
                    name=title or "Unknown",
                    year=str(year) if year else None,
                    mtype=mtype,
                    imdbid=imdb_id,
                )
                if douban_info and douban_info.get("id"):
                    logger.debug("MoviePilot 兜底映射豆瓣信息 (%s): %s", imdb_id or title, douban_info)
                    return douban_info
            except Exception as e:
                logger.debug("MoviePilot IMDb/标题兜底映射豆瓣失败 %s: %s", title, e)
        return douban_info or {}

    def _resolve_douban_info(
        self,
        tmdb_id: Optional[int],
        imdb_id: Optional[str],
        title: str,
        year: Optional[int],
        media_type: MediaType,
    ) -> Dict[str, Any]:
        """同步包装异步豆瓣匹配，内部通过 global_vars.loop 执行协程。"""
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._get_douban_info_by_tmdb(tmdb_id, imdb_id, title=title, year=year, mtype=media_type),
                global_vars.loop,
            )
            return future.result(timeout=30) or {}
        except Exception as e:
            logger.warning("匹配豆瓣失败 %s (%s): %s", title, year, e)
            return {}

    def _lookup_manual_douban_id(
        self,
        media: Dict[str, Any],
        media_type: MediaType,
        sync_key: str,
    ) -> Optional[str]:
        """根据配置的手动映射查找豆瓣 subject_id。"""
        if not self._manual_mappings:
            return None
        ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
        title = media.get("title", "未知")
        year = media.get("year")
        trakt_id = ids.get("trakt") or media.get("trakt_id")
        tmdb_id = ids.get("tmdb")
        imdb_id = ids.get("imdb")
        slug = ids.get("slug") or ""
        media_name = "movie" if media_type == MediaType.MOVIE else "show"
        candidates = [
            sync_key,
            f"{media_name}:{trakt_id}" if trakt_id else "",
            f"trakt:{trakt_id}" if trakt_id else "",
            f"tmdb:{tmdb_id}" if tmdb_id else "",
            f"imdb:{imdb_id}" if imdb_id else "",
            f"slug:{slug}" if slug else "",
            f"{title} ({year})" if year else "",
            f"{title} {year}" if year else "",
            title,
        ]
        for candidate in candidates:
            subject_id = self._manual_mappings.get(str(candidate).strip().lower())
            if subject_id:
                logger.info("命中 Trakt 手动映射: %s -> 豆瓣 %s", candidate, subject_id)
                return subject_id
        return None

    # ------------------------------------------------------------------
    # 评分同步（单条）
    # ------------------------------------------------------------------

    def sync_one_rate(
        self,
        item: Dict[str, Any],
        finished: Dict[str, Any],
        wait_retry: Dict[str, Any],
        media_type: MediaType,
        douban_helper: Any,
        private: bool,
    ) -> bool:
        """同步单条 Trakt 评分到豆瓣。

        Args:
            item: Trakt 评分项（``movie`` / ``show`` + ``rating`` + ``rated_at``）
            finished: 已同步缓存字典（原地修改）
            wait_retry: 待重试缓存字典（原地修改）
            media_type: 媒体类型
            douban_helper: DoubanHelper 实例
            private: 是否仅自己可见

        Returns:
            同步成功返回 True，否则返回 False。
        """
        media_key = "movie" if media_type == MediaType.MOVIE else "show"
        media = item.get(media_key) if isinstance(item.get(media_key), dict) else {}

        ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
        trakt_rating = item.get("rating")
        if not isinstance(trakt_rating, (int, float)):
            trakt_rating = 0
        trakt_rating = int(trakt_rating)
        douban_rating = self._trakt_rating_to_douban(trakt_rating)

        tmdb_id = ids.get("tmdb")
        imdb_id = ids.get("imdb")
        trakt_id = ids.get("trakt") or media.get("trakt_id")
        slug = ids.get("slug") or ""
        title = media.get("title", "未知")
        year = media.get("year")

        if not tmdb_id and not imdb_id:
            logger.warning("Trakt 条目无 tmdb/imdb: %s (%s)", title, year)
            return False

        key = f"{media_type}_{str(trakt_id) if trakt_id else slug or f'{title}_{year}'}"
        if key in finished:
            prev = finished[key]
            if prev.get("trakt_rating") == trakt_rating and prev.get("douban_id"):
                logger.debug("已同步过且评分未变，跳过: %s", title)
                return True

        subject_id = self._lookup_manual_douban_id(media, media_type, key)
        douban_info: Dict[str, Any] = {}
        if not subject_id:
            douban_info = self._resolve_douban_info(
                int(tmdb_id) if tmdb_id else None, imdb_id, title, year, media_type
            )
            subject_id = douban_info.get("id")
        if not subject_id:
            logger.warning(
                "Trakt 条目未匹配到豆瓣信息: %s (%s), tmdb=%s, imdb=%s, trakt=%s, rating=%s",
                title, year, tmdb_id, imdb_id, trakt_id or slug, trakt_rating,
            )
            if key not in wait_retry:
                wait_retry[key] = {
                    "title": title, "year": year,
                    "trakt_rating": trakt_rating,
                    "douban_rating": douban_rating,
                    "tmdb_id": tmdb_id, "imdb_id": imdb_id,
                    "media_type": media_type.value,
                }
            return False

        display_title = douban_info.get("alt_title", title)

        ret = douban_helper.set_watching_status(
            subject_id=subject_id,
            status="collect",
            private=private,
            rating=douban_rating,
        )
        if ret:
            finished[key] = {
                "douban_id": subject_id,
                "trakt_rating": trakt_rating,
                "douban_rating": douban_rating,
                "title": display_title,
                "en_title": title,
                "year": year,
                "media_type": media_type.value,
                "status": "看完",
                "sync_time": int(time.time()),
            }
            wait_retry.pop(key, None)
            logger.info("同步成功: %s (%s) -> 豆瓣 %s 评分 %s 星", display_title, year, subject_id, douban_rating)
            return True
        else:
            logger.error("豆瓣提交失败: %s (%s) subject_id=%s", title, year, subject_id)
            if key not in wait_retry:
                wait_retry[key] = {
                    "title": title, "year": year,
                    "trakt_rating": trakt_rating,
                    "douban_rating": douban_rating,
                    "subject_id": subject_id,
                    "media_type": media_type.value,
                }
            return False

    # ------------------------------------------------------------------
    # 播放进度同步（单条）
    # ------------------------------------------------------------------

    def sync_one_progress(
        self,
        item: Dict[str, Any],
        media_key: str,
        media_type: MediaType,
        watching: Dict[str, Any],
        douban_helper: Any,
        private: bool,
    ) -> bool:
        """同步单条播放进度到豆瓣「在看」。

        Args:
            item: 播放进度项（包含 ``progress`` 和对应媒体字段）
            media_key: 媒体字段名（``"movie"`` 或 ``"show"``）
            media_type: 媒体类型
            watching: 在看缓存字典（原地修改）
            douban_helper: DoubanHelper 实例
            private: 是否仅自己可见
        """
        progress = item.get("progress")
        if isinstance(progress, (int, float)) and progress < 10:
            title_temp = (item.get(media_key) or {}).get("title", "未知")
            logger.debug("进度低于 10%%，跳过: %s progress=%s", title_temp, progress)
            return False
        if isinstance(progress, (int, float)) and progress >= 100:
            return False

        media = item.get(media_key) or {}
        ids = media.get("ids") or {}
        tmdb_id = ids.get("tmdb")
        imdb_id = ids.get("imdb")
        trakt_id = ids.get("trakt") or media.get("trakt_id")
        slug = ids.get("slug") or ""
        title = media.get("title", "未知")
        year = media.get("year")

        if not tmdb_id and not imdb_id:
            logger.debug("Trakt 播放进度条目无 tmdb/imdb，跳过: %s (%s)", title, year)
            return False

        key = f"{media_type.value}_{str(trakt_id) if trakt_id else slug or f'{title}_{year}'}"

        subject_id = self._lookup_manual_douban_id(media, media_type, key)
        douban_info: Dict[str, Any] = {}
        if not subject_id:
            douban_info = self._resolve_douban_info(
                int(tmdb_id) if tmdb_id else None, imdb_id, title, year, media_type
            )
            subject_id = douban_info.get("id")
        if not subject_id:
            logger.debug("匹配豆瓣未看完条目失败 %s (%s)", title, year)
            return False

        display_title = (
            douban_info.get("title")
            or douban_info.get("cn_name")
            or douban_info.get("name")
            or title
        )

        if douban_helper.set_watching_status(
            subject_id=subject_id,
            status="do",
            private=private,
            rating=None,
        ):
            watching[key] = {
                "douban_id": subject_id,
                "title": display_title,
                "en_title": title,
                "year": year,
                "media_type": media_type.value,
                "progress": progress,
                "status": "在看",
                "sync_time": int(time.time()),
            }
            logger.info("同步未看完到豆瓣在看: %s (%s) -> 在看(progress=%s)", display_title, year, progress)
            return True
        else:
            logger.warning("同步未看完到豆瓣在看失败: %s (%s) subject_id=%s", title, year, subject_id)
            return False

    # ------------------------------------------------------------------
    # OAuth 授权相关
    # ------------------------------------------------------------------

    def get_access_token(self, force_reauthorize: bool = False) -> Optional[str]:
        """获取有效的 Trakt Access Token。

        优先顺序：
        1. 构造时传入的 ``access_token``（由用户在配置中填写）
        2. 持久化缓存中未过期的 token
        3. 启动设备码授权流程（阻塞等待最多 10 分钟）

        Returns:
            有效的 access_token 字符串，无法获取时返回 None。
        """
        if not force_reauthorize and self._access_token:
            logger.info("使用配置的 Access Token")
            return self._access_token

        cached = None if force_reauthorize else self._get_cached_token()
        if cached:
            logger.info("使用缓存的 Access Token")
            return cached

        if not self._client_id or not self._client_secret:
            logger.debug("未配置 Trakt Client Secret，无法自动获取 Access Token")
            return None

        if force_reauthorize and self._refresh_access_token():
            logger.info("Trakt Refresh Token 续期成功")
            return self._access_token

        logger.info("开始 Trakt 设备码授权流程...")
        return self._create_device_code_and_wait()

    def _get_cached_token(self) -> Optional[str]:
        """读取持久化缓存中未过期的 Access Token。"""
        now_ts = int(time.time())
        token_data = self._get_data("trakt_token") or {}
        access_token = token_data.get("access_token")
        expires_at = int(token_data.get("expires_at") or 0)
        if access_token and expires_at > now_ts:
            return access_token
        return None

    def _refresh_access_token(self) -> bool:
        """使用已保存的 Refresh Token 续期 Trakt Access Token。"""
        token_data = self._get_data("trakt_token") or {}
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            return False
        url = f"{self._API_BASE}/oauth/token"
        try:
            resp = RequestUtils(timeout=10, headers=self._headers).post_res(
                url=url,
                json={
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                    "grant_type": "refresh_token",
                },
            )
            if resp is None or resp.status_code != 200:
                logger.warning(
                    "Trakt Refresh Token 续期失败: %s %s",
                    getattr(resp, "status_code", None),
                    getattr(resp, "text", "")[:200],
                )
                return False
            data = resp.json()
            return self._persist_token_response(data)
        except Exception as e:
            logger.warning("Trakt Refresh Token 续期异常: %s", e)
            return False

    def _persist_token_response(self, data: Dict[str, Any]) -> bool:
        """持久化 Trakt OAuth token 响应。"""
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = int(data.get("expires_in") or 0)
        if not access_token or expires_in <= 0:
            return False
        expires_at = int(time.time()) + expires_in - 60
        token_data = {
            "access_token": access_token,
            "expires_at": expires_at,
        }
        if refresh_token:
            token_data["refresh_token"] = refresh_token
        self._access_token = access_token
        self._save_data("trakt_token", token_data)
        self._update_config({"trakt_access_token": access_token})
        logger.info("✅ Access Token 已保存（有效期约 %d 小时）", expires_in // 3600)
        return True

    def _create_device_code_and_wait(self) -> Optional[str]:
        """创建 Trakt 设备码并阻塞等待用户授权（最多 10 分钟）。

        Returns:
            授权成功后的 access_token，失败返回 None。
        """
        url = f"{self._API_BASE}/oauth/device/code"
        try:
            resp = RequestUtils(timeout=10, headers=self._headers).post_res(
                url=url,
                json={"client_id": self._client_id},
            )
            if resp is None or resp.status_code != 200:
                logger.warning(
                    "获取 Trakt 设备码失败: %s %s",
                    getattr(resp, "status_code", None),
                    getattr(resp, "text", "")[:200],
                )
                return None

            data = resp.json()
            device_code = data.get("device_code")
            user_code = data.get("user_code")
            verification_url = data.get("verification_url")
            interval = int(data.get("interval") or 5)

            if not device_code or not user_code or not verification_url:
                logger.warning("Trakt 设备码返回内容不完整: %s", data)
                return None

            msg = (
                f"豆瓣书影音同步 - Trakt 需要授权。\n\n"
                f"请在浏览器打开: {verification_url}\n"
                f"并输入授权码: {user_code}\n\n"
                f"系统将等待 10 分钟，请在此时间内完成授权。"
            )
            self._notify("豆瓣书影音同步 - Trakt 授权", msg)
            logger.info("Trakt 设备码已生成: %s", user_code)
            logger.info("授权链接: %s", verification_url)
            logger.info("系统将阻塞等待授权，最多等待 10 分钟...")

            max_wait_seconds = 600
            start_time = time.time()
            attempt = 0

            while time.time() - start_time < max_wait_seconds:
                attempt += 1
                elapsed = int(time.time() - start_time)
                logger.info("第 %d 次尝试获取 token（已等待 %d 秒）...", attempt, elapsed)

                access_token = self._exchange_device_token(device_code)
                if access_token:
                    logger.info("✅ 授权成功！用时 %d 秒", elapsed)
                    self._notify(
                        "豆瓣书影音同步 - Trakt 授权成功",
                        f"Trakt 授权已完成，用时 {elapsed} 秒。\n未看完列表同步功能已启用。",
                    )
                    return access_token

                time.sleep(interval)

            logger.warning("❌ Trakt 授权超时（等待了 10 分钟）")
            self._notify(
                "豆瓣书影音同步 - Trakt 授权超时",
                "等待授权超时（10分钟）。请重新运行同步任务或手动配置 Access Token。",
            )
            return None

        except Exception as e:
            logger.error("Trakt 设备码授权流程异常: %s", e, exc_info=True)
            return None

    def _exchange_device_token(self, device_code: str) -> Optional[str]:
        """使用设备码轮询交换 Trakt Access Token。

        Args:
            device_code: 从设备码接口获取的 device_code

        Returns:
            成功时返回 access_token，等待中或失败时返回 None。
        """
        url = f"{self._API_BASE}/oauth/device/token"
        try:
            resp = RequestUtils(timeout=10, headers=self._headers).post_res(
                url=url,
                json={
                    "code": device_code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            if resp is None:
                return None

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception as e:
                    logger.debug("解析 Trakt Access Token 响应失败: %s", e)
                    return None

                if self._persist_token_response(data):
                    return self._access_token
                return None

            if resp.status_code == 400:
                try:
                    err = (resp.json().get("error") or "").lower()
                except Exception:
                    err = ""
                if err not in ("authorization_pending", "slow_down"):
                    logger.debug("Trakt 授权错误: %s", err)
                return None

            return None

        except Exception as e:
            logger.debug("交换 token 异常: %s", e)
            return None
