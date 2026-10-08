# -*- coding: utf-8 -*-
"""
Trakt API 封装模块

负责所有与 Trakt 相关的网络请求、OAuth 授权、评分拉取和播放进度拉取逻辑。
__init__.py 只需实例化 TraktHelper 并调用其方法即可，不包含任何 Trakt 业务细节。
"""
import asyncio
import base64
import hashlib
import ipaddress
import math
import random
import secrets
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from app.chain.media import MediaChain
from app.core.config import global_vars, settings
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import RequestUtils


class TraktHelper:
    """Trakt API 封装，提供评分拉取、播放进度拉取和 OAuth 授权能力。

    Args:
        client_id: Trakt Client ID（必填，用于公开接口）
        access_token: 已有的 Trakt Access Token（可选，优先于自动授权）
        username: Trakt 用户名（用于公开评分接口）
        save_data_fn: 持久化回调，签名 ``(key: str, value: Any) -> None``
        get_data_fn: 读取持久化数据回调，签名 ``(key: str) -> Any``
        update_config_fn: 更新插件配置回调，签名 ``(config: dict) -> None``
        send_notification_fn: 发送通知回调（可选），签名 ``(title: str, body: str) -> None``
        manual_mappings: Trakt 条目到豆瓣 subject_id 的手动映射
        redirect_uri: 与 Trakt 应用登记值一致的 HTTPS 回跳地址
    """

    # 协议固定常量，保持类级
    _API_BASE = "https://api.trakt.tv"
    _AUTH_BASE = "https://auth.trakt.tv"
    _API_VERSION = "2"
    _REQUEST_JITTER_RANGE = (0.5, 1.5)
    _PKCE_AUTH_URL = "https://auth.trakt.tv/oauth/authorize"
    _PKCE_LIFETIME_SECONDS = 600

    def __init__(
        self,
        client_id: str,
        access_token: str,
        username: str,
        save_data_fn: Callable[[str, Any], None],
        get_data_fn: Callable[[str], Any],
        update_config_fn: Callable[[Dict[str, Any]], None],
        send_notification_fn: Optional[Callable[[str, str], None]] = None,
        manual_mappings: Optional[Dict[str, str]] = None,
        redirect_uri: str = "",
    ):
        """初始化 Trakt 凭据、授权方式及插件持久化回调。"""
        self._client_id = client_id
        self._access_token = access_token
        self._username = username
        self._redirect_uri = redirect_uri
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
            "User-Agent": f"{settings.USER_AGENT} Plugin/TraktRatingsSync",
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
    def _log_response_failure(action: str, response: Any, oauth: bool = False) -> None:
        """仅记录响应分类，避免错误正文或请求地址泄露认证信息。"""
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", {}) or {}
        content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        body_prefix = str(getattr(response, "text", "") or "")[:256].lstrip().lower()
        is_html = content_type == "text/html" or body_prefix.startswith(("<!doctype html", "<html"))
        if content_type not in ("application/json", "text/html", "text/plain"):
            content_type = "其他" if content_type else "未知"
        detail = f"status={status}, content_type={content_type}, html={is_html}"
        if status == 403:
            if is_html:
                advice = "响应疑似边缘或上游拦截页面，请检查网络出口及上游访问限制"
            else:
                advice = "请检查 Trakt API 应用的 Client ID、启用状态和访问权限"
            if oauth:
                advice += "；403 不等同于 Access Token 过期"
            logger.warning(f"Trakt {action}拒绝访问（403）：{advice}（{detail}）")
        else:
            logger.warning(f"Trakt {action}请求失败（{detail}）")

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
            resp = RequestUtils(timeout=30, headers=self._headers, proxies=settings.PROXY).get_res(url=url)
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
                self._log_response_failure("公开评分接口", resp)
            elif resp.status_code == 404:
                logger.warning("Trakt 用户不存在或未公开评分: %s", self._username)
            else:
                self._log_response_failure("公开评分接口", resp)
        except Exception as e:
            logger.error(f"拉取 Trakt 评分失败：{type(e).__name__}")
        return []

    # ------------------------------------------------------------------
    # 播放进度接口（需要 OAuth Access Token）
    # ------------------------------------------------------------------

    def fetch_playback(self, path: str, access_token: str) -> Optional[List[Dict[str, Any]]]:
        """拉取 Trakt 播放进度列表。

        Args:
            path: 相对路径，例如 ``"/sync/playback/episodes"``
            access_token: 有效的 Trakt Access Token

        Returns:
            播放进度列表，成功无记录时返回空列表，失败时返回 None。
        """
        headers = self._build_headers({"Authorization": f"Bearer {access_token}"})
        url = f"{self._API_BASE}{path}"
        try:
            self._sleep_before_request(f"fetch_playback/{path}")
            resp = RequestUtils(timeout=20, headers=headers, proxies=settings.PROXY).get_res(url=url)
            if resp is None:
                self._log_response_failure("播放进度", resp, oauth=True)
                return None
            if resp.status_code == 204:
                return []
            if resp.status_code != 200:
                if resp.status_code == 401:
                    self._last_oauth_unauthorized = True
                    logger.warning("Trakt Access Token 无效或已过期，无法拉取播放进度（401）")
                else:
                    self._log_response_failure("播放进度", resp, oauth=True)
                return None
            data = resp.json()
            if not isinstance(data, list):
                logger.warning("Trakt 播放进度返回格式异常，期望数组")
                return None
            return data
        except Exception as e:
            logger.error(f"拉取 Trakt 播放进度失败：{type(e).__name__}")
            return None

    def fetch_history(self, media_type: str, access_token: str, limit: int = 20) -> Optional[List[Dict[str, Any]]]:
        """拉取 Trakt 最近观看历史。

        Args:
            media_type: Trakt 历史类型，例如 ``"shows"``。
            access_token: 有效的 Trakt Access Token。
            limit: 返回条数上限。

        Returns:
            最近观看历史列表，成功无记录时返回空列表，失败时返回 None。
        """
        headers = self._build_headers({"Authorization": f"Bearer {access_token}"})
        url = f"{self._API_BASE}/sync/history/{media_type}"
        try:
            self._sleep_before_request(f"fetch_history/{media_type}")
            resp = RequestUtils(timeout=20, headers=headers, proxies=settings.PROXY).get_res(
                url=url,
                params={"limit": max(1, int(limit or 20))},
            )
            if resp is None:
                self._log_response_failure("观看历史", resp, oauth=True)
                return None
            if resp.status_code == 204:
                return []
            if resp.status_code != 200:
                if resp.status_code == 401:
                    self._last_oauth_unauthorized = True
                    logger.warning("Trakt Access Token 无效或已过期，无法拉取观看历史（401）")
                else:
                    self._log_response_failure("观看历史", resp, oauth=True)
                return None
            data = resp.json()
            if not isinstance(data, list):
                logger.warning("Trakt 观看历史返回格式异常，期望数组")
                return None
            return data
        except Exception as e:
            logger.error(f"拉取 Trakt 观看历史失败：{type(e).__name__}")
            return None

    # ------------------------------------------------------------------
    # 逐季完成判断
    # ------------------------------------------------------------------

    def _fetch_show_completion_data(self, show_id: str, path: str, access_token: str = "") -> Any:
        """读取季度元数据或完整观看进度，失败时不将缺失数据视为未观看。"""
        headers = self._build_headers({"Authorization": f"Bearer {access_token}"} if access_token else None)
        params = {"hidden": "true", "specials": "false", "last_activity": "watched"} if path == "progress/watched" else {"extended": "full"}
        try:
            self._sleep_before_request("季度完成状态")
            response = RequestUtils(timeout=20, headers=headers, proxies=settings.PROXY).get_res(
                url=f"{self._API_BASE}/shows/{quote(str(show_id), safe='')}" + (f"/{path}" if path else ""), params=params,
            )
            if response is None or response.status_code != 200:
                if response is not None and response.status_code == 401 and access_token:
                    self._last_oauth_unauthorized = True
                self._log_response_failure("季度完成状态", response, oauth=bool(access_token))
                return None
            data = response.json()
            expected = list if path == "seasons" else dict
            if not isinstance(data, expected):
                logger.warning("Trakt 季度完成状态返回格式异常")
                return None
            return data
        except Exception as error:
            logger.warning(f"Trakt 季度完成状态读取失败：{type(error).__name__}")
            return None

    def fetch_show_progress(self, show_id: str, access_token: str) -> Optional[Dict[str, Any]]:
        """读取整部剧的逐季逐集观看记录，包含隐藏季并排除特别篇。"""
        return self._fetch_show_completion_data(show_id, "progress/watched", access_token)

    def fetch_show_seasons(self, show_id: str) -> Optional[List[Dict[str, Any]]]:
        """读取各季总集数与已播集数，避免把追平更新误判为全季看完。"""
        return self._fetch_show_completion_data(show_id, "seasons")

    def fetch_show_details(self, show_id: str) -> Optional[Dict[str, Any]]:
        """为未保存媒体ID的旧在看缓存补全剧集信息，成功后私下缓存以免重复查询。"""
        cache = dict(self._get_data("trakt_shows") or {})
        if cache.get(show_id):
            return cache[show_id]
        show = self._fetch_show_completion_data(show_id, "")
        if show and (show.get("ids") or {}).get("trakt"):
            cache[show_id] = {"title": show.get("title"), "year": show.get("year"), "ids": show["ids"]}
            self._save_data("trakt_shows", cache)
            return cache[show_id]
        return None

    @staticmethod
    def _episode_is_watched(episode: Dict[str, Any], reset_at: Any) -> bool:
        """有重看起点时只统计此后明确看过的单集，日期不可解析时保守跳过。"""
        if episode.get("completed") is not True:
            return False
        if not reset_at:
            return True
        try:
            return (datetime.fromisoformat(episode["last_watched_at"].replace("Z", "+00:00"))
                    >= datetime.fromisoformat(reset_at.replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    @staticmethod
    def build_season_states(progress: Dict[str, Any], seasons: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按正式季判断是否全部看完，拒绝零集、缺集、重复集及尚未播完的完成声明。"""
        metadata = {item.get("number"): item for item in seasons if isinstance(item, dict)}
        result = []
        for item in progress.get("seasons") or []:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            if type(number) is not int or number <= 0:
                continue
            season = metadata.get(number) or {}
            total, aired, completed = season.get("episode_count"), item.get("aired"), item.get("completed")
            episodes = item.get("episodes") or []
            watched = {episode.get("number") for episode in episodes if isinstance(episode, dict)
                       and type(episode.get("number")) is int
                       and TraktHelper._episode_is_watched(episode, progress.get("reset_at"))}
            valid_counts = all(type(value) is int and value >= 0 for value in (total, aired, completed, season.get("aired_episodes")))
            finished = (valid_counts and total > 0 and aired == total == completed == season["aired_episodes"]
                        and len(episodes) == total and watched == set(range(1, total + 1)))
            result.append({"season": number, "completed": finished, "watched_episodes": len(watched),
                           "total_episodes": total if type(total) is int else None,
                           "season_year": str(season.get("first_aired") or "")[:4] or None})
        return result

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
        season: Optional[int] = None,
        season_year: Optional[str] = None,
    ) -> Dict[str, Any]:
        """通过 MoviePilot 媒体链获取豆瓣 subject_id 和中文标题。"""
        douban_info = None
        media_chain = MediaChain()
        if season is not None:
            try:
                tmdb_info = await media_chain.async_tmdb_info(tmdbid=int(tmdb_id), mtype=mtype) if tmdb_id else {}
                if tmdb_id and not tmdb_info:
                    return {}
                # 核心的按 TMDB ID 桥接目前未透传季号，逐季状态必须显式调用按季匹配。
                return await media_chain.async_match_doubaninfo(
                    name=(tmdb_info or {}).get("name") or title, year=season_year,
                    mtype=mtype, imdbid=imdb_id, season=season,
                ) or {}
            except Exception as error:
                logger.warning(f"豆瓣第{season}季匹配失败：{type(error).__name__}")
                return {}
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
        season: Optional[int] = None,
        season_year: Optional[str] = None,
    ) -> Dict[str, Any]:
        """同步包装异步豆瓣匹配，内部通过 global_vars.loop 执行协程。"""
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._get_douban_info_by_tmdb(tmdb_id, imdb_id, title=title, year=year, mtype=media_type,
                                            season=season, season_year=season_year),
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
        season: Optional[int] = None,
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
        if season is not None:
            specific = [sync_key] + [f"{candidate}:s{season}" for candidate in candidates[1:] if candidate]
            candidates = specific + (candidates if season == 1 else [])
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
                douban_helper.record_unchanged()
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

        if hasattr(douban_helper, "set_target_context"):
            douban_helper.set_target_context(subject_id, "movie.douban.com", display_title, "Trakt")
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
        """同步单季完成状态，兼容旧版单条播放进度调用。

        Args:
            item: 播放进度项（包含 ``progress`` 和对应媒体字段）
            media_key: 媒体字段名（``"movie"`` 或 ``"show"``）
            media_type: 媒体类型
            watching: 在看缓存字典（原地修改）
            douban_helper: DoubanHelper 实例
            private: 是否仅自己可见
        """
        if type(item.get("season")) is int and item["season"] > 0:
            return self._sync_one_season(item, watching, douban_helper, private)
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
        previous = watching.get(key) or {}
        if (previous.get("status") == "在看" and previous.get("douban_id")
                and previous.get("private") == private
                and (not subject_id or subject_id == previous["douban_id"])):
            previous["progress"] = progress
            douban_helper.record_unchanged()
            logger.info("剧集已同步为在看，跳过重复提交: %s", title)
            return True
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

        if hasattr(douban_helper, "set_target_context"):
            douban_helper.set_target_context(subject_id, "movie.douban.com", display_title, "Trakt")
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
                "private": private,
                "sync_time": int(time.time()),
            }
            logger.info("同步未看完到豆瓣在看: %s (%s) -> 在看(progress=%s)", display_title, year, progress)
            return True
        else:
            logger.warning("同步未看完到豆瓣在看失败: %s (%s) subject_id=%s", title, year, subject_id)
            return False

    def _sync_one_season(self, item: Dict[str, Any], watching: Dict[str, Any], douban_helper: Any, private: bool) -> bool:
        """按季同步观看状态，保留已看过和已知评分，只有提交成功才更新同步缓存。"""
        show = item["show"]
        ids = show.get("ids") or {}
        show_id = ids.get("trakt") or ids.get("slug") or ids.get("imdb")
        if not show_id:
            return False
        season = item["season"]
        key = f"{MediaType.TV.value}_{show_id}_s{season}"
        previous = watching.get(key) or {}
        status = "看完" if item.get("completed") or previous.get("status") == "看完" else "在看"
        subject = self._lookup_manual_douban_id(show, MediaType.TV, key, season=season)
        info = {}
        if not subject:
            subject = previous.get("douban_id")
        if not subject:
            info = self._resolve_douban_info(ids.get("tmdb"), ids.get("imdb"), show.get("title"), show.get("year"),
                                             MediaType.TV, season=season, season_year=item.get("season_year"))
            subject = info.get("id")
        if not subject:
            logger.warning(f"Trakt 剧集第{season}季未匹配到豆瓣，保留原有状态：{show.get('title')}")
            return False
        subject = str(subject)
        # 各季不能复用同一豆瓣条目，防止宽泛手动映射或外部匹配错误覆盖其他季。
        if any(str(record.get("douban_id")) == subject and record.get("season") != season
               and str((record.get("show") or {}).get("ids", {}).get("trakt")
                       or (record.get("show") or {}).get("ids", {}).get("slug")
                       or (record.get("show") or {}).get("ids", {}).get("imdb")) == str(show_id)
               for record in watching.values() if record.get("season") is not None):
            logger.warning(f"豆瓣季度条目冲突，跳过第{season}季：{show.get('title')}")
            return False
        actual = ((self._get_data("douban_sync_state") or {}).get("synced") or {}).get(
            f"https://movie.douban.com/j/subject/{subject}/interest", {})
        rated = next((record for record in (self._get_data("finished") or {}).values()
                      if str(record.get("douban_id")) == subject), {})
        if actual.get("interest") == "collect" or rated:
            status = "看完"
        target = "collect" if status == "看完" else "do"
        known_rating = actual.get("rating") or rated.get("douban_rating") or previous.get("douban_rating")
        rating = int(known_rating) if str(known_rating).isdigit() and 1 <= int(known_rating) <= 5 else None
        title = info.get("title") or info.get("alt_title") or previous.get("title") or f"{show.get('title', '未知')} 第{season}季"
        record = {**previous, "douban_id": subject, "title": title, "en_title": show.get("title"), "year": show.get("year"),
                  "season": season, "show": show, "media_type": MediaType.TV.value, "status": status, "private": private,
                  "watched_episodes": item.get("watched_episodes"), "total_episodes": item.get("total_episodes"),
                  "douban_rating": rating, "checked_at": int(time.time())}
        if ((previous.get("status") == status and previous.get("private") == private and previous.get("douban_id") == subject)
                or (actual.get("interest") == target and actual.get("private", "") == ("on" if private else ""))):
            douban_helper.record_unchanged()
            record.setdefault("sync_time", int(time.time()))
            watching[key] = record
            return True
        douban_helper.set_target_context(subject, "movie.douban.com", title, "Trakt")
        if not douban_helper.set_watching_status(subject_id=subject, status=target, private=private, rating=rating):
            return False
        watching[key] = {**record, "sync_time": int(time.time())}
        logger.info(f"Trakt 季度同步成功：{title} → {status}（已看 {item.get('watched_episodes')} / 总集数 {item.get('total_episodes')}）")
        return True

    # ------------------------------------------------------------------
    # OAuth 授权相关
    # ------------------------------------------------------------------

    def reset_authorization(self) -> None:
        """清除 Trakt 授权凭据和待完成请求，保留所有书影音同步记录。"""
        self._access_token = ""
        self._save_data("trakt_token", {})
        self._save_data("trakt_pkce_pending", {})
        self._update_config({"trakt_access_token": "", "trakt_authorization_url": ""})

    @staticmethod
    def _validate_redirect_uri(redirect_uri: str) -> None:
        """要求无查询参数的 HTTPS 回跳地址，避免使用本地或旧版 OOB 地址。"""
        parsed = urlsplit(redirect_uri)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.hostname.lower() == "localhost"):
            raise ValueError("请填写自己域名下的 HTTPS 回跳地址，不含查询参数，并与 Trakt 应用设置完全一致")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return
        if not address.is_global:
            raise ValueError("Trakt 回跳地址不能使用本地或私有 IP")

    def begin_pkce_authorization(self) -> str:
        """生成短期 PKCE 授权链接，并在插件私有数据中保存随机校验信息。"""
        if not self._client_id:
            raise ValueError("请先填写 Trakt Client ID")
        self._validate_redirect_uri(self._redirect_uri)
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        # 新请求只替换待授权校验信息；用户取消或请求过期不应破坏仍可用的令牌。
        self._save_data("trakt_pkce_pending", {})
        self._save_data("trakt_pkce_pending", {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "code_verifier": verifier,
            "state": state,
            "expires_at": int(time.time()) + self._PKCE_LIFETIME_SECONDS,
        })
        authorization_url = self._PKCE_AUTH_URL + "?" + urlencode({
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        self._update_config({"trakt_authorization_url": authorization_url})
        logger.info("Trakt PKCE 授权链接已生成，有效期 10 分钟，请在插件配置页完成授权")
        return authorization_url

    def complete_pkce_authorization(self, callback_url: str) -> bool:
        """校验完整回跳地址和 state，使用一次性 verifier 交换并保存授权凭据。"""
        pending = self._get_data("trakt_pkce_pending") or {}
        if (not pending or pending.get("client_id") != self._client_id
                or pending.get("redirect_uri") != self._redirect_uri
                or int(pending.get("expires_at") or 0) <= int(time.time())):
            self._save_data("trakt_pkce_pending", {})
            self._update_config({"trakt_authorization_url": ""})
            raise ValueError("Trakt 授权请求已过期或应用配置已变化，请重新生成授权链接")
        callback = urlsplit(callback_url)
        expected = urlsplit(self._redirect_uri)
        if ((callback.scheme, callback.netloc, callback.path) != (expected.scheme, expected.netloc, expected.path)
                or callback.fragment):
            raise ValueError("授权回跳地址不匹配，请粘贴完成 Trakt 授权后浏览器地址栏中的完整地址")
        params = parse_qs(callback.query, keep_blank_values=True)
        states = params.get("state", [])
        codes = params.get("code", [])
        if len(states) != 1 or not secrets.compare_digest(states[0], pending.get("state", "")):
            raise ValueError("Trakt 授权 state 校验失败，请使用本次授权链接对应的回跳地址")
        if "error" in params:
            self._save_data("trakt_pkce_pending", {})
            self._update_config({"trakt_authorization_url": ""})
            raise ValueError("Trakt 授权已取消，请重新生成授权链接")
        if len(codes) != 1 or not codes[0]:
            raise ValueError("回跳地址缺少授权码，请完成授权后复制完整地址")
        # 授权码只能使用一次；网络异常也要求重新开始，避免重放已消费的请求。
        self._save_data("trakt_pkce_pending", {})
        self._update_config({"trakt_authorization_url": ""})
        try:
            response = RequestUtils(timeout=10, headers=self._headers, proxies=settings.PROXY).post_res(
                url=f"{self._AUTH_BASE}/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": self._client_id,
                    "redirect_uri": self._redirect_uri,
                    "code": codes[0],
                    "code_verifier": pending["code_verifier"],
                },
            )
            if response is None or response.status_code != 200:
                self._log_response_failure("PKCE 授权", response, oauth=True)
                return False
            return self._persist_token_response(response.json(), auth_mode="pkce", redirect_uri=self._redirect_uri)
        except Exception as error:
            logger.warning("Trakt PKCE 授权失败：%s，请重新生成授权链接", type(error).__name__)
            return False

    def get_access_token(self, force_reauthorize: bool = False) -> Optional[str]:
        """获取有效的 Trakt Access Token。

        优先顺序：
        1. 配置中未过期的 ``access_token``
        2. 持久化缓存中未过期的 token
        3. 使用 Refresh Token 自动续期
        4. 提示在插件配置页完成 PKCE 授权

        Returns:
            有效的 access_token 字符串，无法获取时返回 None。
        """
        token_data = self._get_data("trakt_token") or {}
        if token_data.get("client_id") and token_data["client_id"] != self._client_id:
            self.reset_authorization()
        if not force_reauthorize and self._access_token:
            logger.info("使用配置的 Access Token")
            if not token_data.get("expires_at") or int(token_data["expires_at"]) > int(time.time()):
                return self._access_token

        cached = None if force_reauthorize else self._get_cached_token()
        if cached:
            logger.info("使用缓存的 Access Token")
            return cached

        if not self._client_id:
            return None

        if self._refresh_access_token():
            logger.info("Trakt Refresh Token 续期成功")
            return self._access_token

        logger.warning("Trakt 尚未授权或需要重新授权，请在插件配置页完成 PKCE 授权")
        self._notify("Trakt 需要重新授权", "请打开插件配置页，点击重新授权并完成浏览器授权；已有同步记录会保留。")
        return None

    def _get_cached_token(self) -> Optional[str]:
        """读取持久化缓存中未过期的 Access Token。"""
        now_ts = int(time.time())
        token_data = self._get_data("trakt_token") or {}
        if token_data.get("client_id") and token_data["client_id"] != self._client_id:
            return None
        access_token = token_data.get("access_token")
        expires_at = int(token_data.get("expires_at") or 0)
        if access_token and expires_at > now_ts:
            return access_token
        return None

    def _refresh_access_token(self) -> bool:
        """使用已保存的 Refresh Token 续期 Trakt Access Token。"""
        token_data = self._get_data("trakt_token") or {}
        if token_data.get("client_id") and token_data["client_id"] != self._client_id:
            return False
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            return False
        url = f"{self._AUTH_BASE}/oauth/token"
        payload = {
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "grant_type": "refresh_token",
        }
        redirect_uri = token_data.get("redirect_uri") or self._redirect_uri
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        try:
            resp = RequestUtils(timeout=10, headers=self._headers, proxies=settings.PROXY).post_res(
                url=url,
                json=payload,
            )
            if resp is None or resp.status_code != 200:
                self._log_response_failure("Refresh Token 续期", resp, oauth=True)
                return False
            data = resp.json()
            return self._persist_token_response(data, auth_mode="pkce",
                                                redirect_uri=payload.get("redirect_uri", ""))
        except Exception as e:
            logger.warning(f"Trakt Refresh Token 续期异常：{type(e).__name__}")
            return False

    def _persist_token_response(self, data: Dict[str, Any], auth_mode: str = "pkce", redirect_uri: str = "") -> bool:
        """持久化 Trakt OAuth token 响应。"""
        if not isinstance(data, dict):
            return False
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = int(data.get("expires_in") or 0)
        if not access_token or expires_in <= 0:
            return False
        expires_at = int(time.time()) + expires_in - 60
        token_data = {
            "client_id": self._client_id,
            "auth_mode": auth_mode,
            "redirect_uri": redirect_uri,
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
