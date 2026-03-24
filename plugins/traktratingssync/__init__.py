# -*- coding: utf-8 -*-
"""
Trakt 评分同步到豆瓣插件
从 Trakt 读取用户电影/电视剧评分,通过 TMDB/IMDB 匹配豆瓣条目,并将评分同步到豆瓣(标记为「看过」并写入评分)。
支持同步类型选择:全部、仅电影、仅电视剧。
可选:基于 Trakt 播放进度(/sync/playback)同步「尚未看完」的视频为豆瓣「在看」。
"""
import asyncio
import math
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.chain.media import MediaChain
from app.core.config import global_vars
from app.log import logger
from app.plugins import _PluginBase
from .douban_helper import DoubanHelper
from app.schemas.types import MediaType
from app.utils.http import RequestUtils

TRAKT_API_BASE = "https://api.trakt.tv"
TRAKT_API_VERSION = "2"
# Trakt 要求:Content-Type、trakt-api-key、trakt-api-version(见 https://trakt.docs.apiary.io)
TRAKT_HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "trakt-api-version": TRAKT_API_VERSION,
}


def _trakt_rating_to_douban(trakt_rating: int) -> int:
    """Trakt 1-10 转为豆瓣 1-5 星"""
    if trakt_rating <= 0:
        return 1
    douban = math.ceil(trakt_rating / 2)
    return int(douban)


class TraktRatingsSync(_PluginBase):
    plugin_name = "Trakt 评分同步豆瓣"
    plugin_desc = "从 Trakt 读取用户电影/电视剧评分,匹配豆瓣条目并同步为「看过」及评分;可选把 Trakt 中尚未看完的视频同步为豆瓣「在看」。"
    plugin_icon = "trakt.png"
    plugin_version = "3.3.0"
    plugin_author = "ColorlessCube"
    author_url = "https://github.com/ColorlessCube"
    plugin_config_prefix = "trakt_ratings_sync_"
    plugin_order = 16
    auth_level = 1

    _enable = False
    _trakt_username = ""
    _trakt_client_id = ""
    _trakt_client_secret = ""
    _trakt_access_token = ""
    _douban_cookie = ""
    _private = True
    _sync_type = "all"  # all: 全部,movies: 仅电影,shows: 仅电视剧
    _max_sync_count = 0  # 0 表示不限制
    _cron = "0 2 * * *"  # 每天凌晨 2 点
    _bark_webhook_url = ""  # Bark Webhook URL,用于发送通知
    _douban_helper = None

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enable = config.get("enable", False)
        self._trakt_username = (config.get("trakt_username") or "").strip()
        self._trakt_client_id = (config.get("trakt_client_id") or "").strip()
        # 可选:Trakt OAuth Client Secret + Access Token,用于读取播放进度(未看完列表)
        self._trakt_client_secret = (config.get("trakt_client_secret") or "").strip()
        self._trakt_access_token = (config.get("trakt_access_token") or "").strip()
        self._douban_cookie = (config.get("douban_cookie") or "").strip()
        self._private = config.get("private", True)
        sync_type = config.get("sync_type", "all") or "all"
        self._sync_type = sync_type
        self._max_sync_count = int(config.get("max_sync_count") or 0) if config.get("max_sync_count") is not None else 0
        self._cron = config.get("cron", "0 2 * * *") or "0 2 * * *"
        self._bark_webhook_url = (config.get("bark_webhook_url") or "").strip()
        self._douban_helper = None

    def run(self):
        """定时任务入口:拉取 Trakt 评分并同步到豆瓣"""
        if not self._enable:
            logger.debug("Trakt 同步插件未启用,跳过")
            return
        if not self._trakt_username or not self._trakt_client_id:
            logger.warning("未配置 Trakt 用户名或 Client ID,跳过同步")
            return

        logger.info("开始执行 Trakt 同步到豆瓣...")

        try:
            self._douban_helper = DoubanHelper(user_cookie=self._douban_cookie or None)
        except Exception as e:
            logger.error(f"初始化豆瓣 Helper 失败: {e}")
            return

        if not self._douban_helper.is_authenticated:
            msg = "豆瓣 Cookie 认证失败，请重新录入"
            logger.error(msg)
            self._send_bark_notification("豆瓣 Cookie 已失效", msg)
            return

        try:
            self.sync_ratings()
        except Exception as e:
            logger.error("同步 Trakt 评分到豆瓣失败: %s", e, exc_info=True)

        # 同步 Trakt 播放进度中「尚未看完」的视频为豆瓣「在看」
        try:
            self.sync_progress()
        except Exception as e:
            logger.error("同步 Trakt 观看进度到豆瓣失败: %s", e, exc_info=True)

        logger.info("Trakt 同步到豆瓣完成")

    def _fetch_trakt_ratings(self, media_type) -> List[Dict[str, Any]]:
        """拉取 Trakt 用户电影评分列表(公开接口,仅需 client_id)。
        API 文档:https://trakt.docs.apiary.io 要求 Header:Content-Type、trakt-api-key、trakt-api-version。
        """
        if not self._trakt_username or not self._trakt_client_id:
            return []

        if media_type == "movies":
            url = f"{TRAKT_API_BASE}/users/{self._trakt_username}/ratings/movies"
        elif media_type == "shows":
            url = f"{TRAKT_API_BASE}/users/{self._trakt_username}/ratings/shows"
        else:
            return []

        headers = {
            **TRAKT_HEADERS_BASE,
            "trakt-api-key": self._trakt_client_id,
        }
        try:
            resp = RequestUtils(timeout=30, headers=headers).get_res(url=url)
            if not resp:
                logger.warning("Trakt API 请求失败(网络或超时)")
                return []
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, list):
                    logger.warning("Trakt API 返回格式异常,期望数组")
                    return []
                return data
            if resp.status_code == 429:
                logger.warning("Trakt API 触发频率限制(429),请稍后再试")
                return []
            if resp.status_code == 403:
                logger.warning("Trakt API 拒绝访问(403),请检查 Client ID 或该用户评分是否设为私有")
                return []
            if resp.status_code == 404:
                logger.warning("Trakt 用户不存在或未公开评分: %s", self._trakt_username)
                return []
            logger.warning("Trakt API 返回异常: status=%s body=%s", resp.status_code, (resp.text or "")[:200])
        except Exception as e:
            logger.error("拉取 Trakt 评分失败: %s", e, exc_info=True)
        return []

    async def _get_douban_info_by_tmdb(self, tmdb_id: Optional[int], imdb_id: Optional[str],
                                       title: Optional[str] = None, year: Optional[int] = None,
                                       mtype: MediaType = MediaType.MOVIE) -> Dict[str, Any]:
        """根据 TMDB ID(及可选 IMDB/标题/年份)获取豆瓣 subject_id 和中文标题

        Returns:
            Dict[str, Any]
        """
        douban_info = None
        if tmdb_id:
            try:
                douban_info = await MediaChain().async_get_doubaninfo_by_tmdbid(
                    tmdbid=int(tmdb_id), mtype=mtype
                )
                if douban_info and douban_info.get("id"):
                    logger.debug(f"豆瓣信息 (TMDB {tmdb_id}): {douban_info}")
                    return douban_info
            except Exception as e:
                logger.debug(f"TMDB {tmdb_id} 匹配豆瓣失败: {e}")
        if title or imdb_id:
            try:
                douban_info = await MediaChain().async_match_doubaninfo(
                    name=title or "Unknown",
                    year=str(year) if year else None,
                    mtype=mtype,
                    imdbid=imdb_id,
                )
                if douban_info and douban_info.get("id"):
                    logger.debug(f"豆瓣信息 (标题匹配 {title}): {douban_info}")
                    return douban_info
            except Exception as e:
                logger.debug(f"标题/IMDB 匹配豆瓣失败 {title}: {e}")
        return douban_info

    def _sync_one_rate(self, item: Dict[str, Any],
                       finished: Dict[str, Any], wait_retry: Dict[str, Any],
                       media_type: MediaType = MediaType.MOVIE) -> bool:
        """同步单条评分到豆瓣(同步上下文,内部用 run_coroutine_threadsafe 调异步匹配)。
        Trakt 返回项结构: { "rating": 1-10, "rated_at": "...", "movie/show": { "title", "year", "ids": { "trakt", "slug", "imdb", "tmdb" } } }。
        """
        if media_type == MediaType.MOVIE:
            media = item.get("movie") if isinstance(item.get("movie"), dict) else {}
        else:
            media = item.get("show") if isinstance(item.get("show"), dict) else {}

        ids = media.get("ids") if isinstance(media.get("ids"), dict) else {}
        trakt_rating = item.get("rating")
        if not isinstance(trakt_rating, (int, float)):
            trakt_rating = 0
        trakt_rating = int(trakt_rating)
        douban_rating = _trakt_rating_to_douban(trakt_rating)
        tmdb_id = ids.get("tmdb")
        imdb_id = ids.get("imdb")
        trakt_id = ids.get("trakt") or media.get("trakt_id")
        slug = ids.get("slug") or ""
        title = media.get("title", "未知")
        year = media.get("year")

        if not tmdb_id and not imdb_id:
            logger.warning(f"Trakt 条目无 tmdb/imdb: {title} ({year})")
            return False

        key = f"{media_type}_{str(trakt_id) if trakt_id else slug or f'{title}_{year}'}"
        if key in finished:
            prev = finished[key]
            if prev.get("trakt_rating") == trakt_rating and prev.get("douban_id"):
                logger.debug(f"已同步过且评分未变,跳过: {title}")
                return True

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._get_douban_info_by_tmdb(
                    int(tmdb_id) if tmdb_id else None,
                    imdb_id,
                    title=title,
                    year=year,
                    mtype=media_type,
                ),
                global_vars.loop,
            )
            result = future.result(timeout=30)
            douban_info = result if result else {}
        except Exception as e:
            logger.warning(f"匹配豆瓣失败 {title} ({year}): {e}")
            if key not in wait_retry:
                wait_retry[key] = {
                    "title": title,
                    "year": year,
                    "trakt_rating": trakt_rating,
                    "douban_rating": _trakt_rating_to_douban(trakt_rating),
                    "tmdb_id": tmdb_id,
                    "imdb_id": imdb_id,
                    "media_type": media_type.value,
                }
            return False
        subject_id = douban_info.get("id", None)
        if not subject_id:
            logger.warning(f"未找到豆瓣条目: {title} ({year})")
            return False

        display_title = douban_info.get("alt_title", title)

        ret = self._douban_helper.set_watching_status(
            subject_id=subject_id,
            status="collect",
            private=self._private,
            rating=douban_rating,
        )
        if ret:
            finished[key] = {
                "douban_id": subject_id,
                "trakt_rating": trakt_rating,
                "douban_rating": _trakt_rating_to_douban(trakt_rating),
                "title": display_title,
                "en_title": title,
                "year": year,
                "media_type": media_type.value,
                "status": "看完",  # 标记状态
                "sync_time": int(time.time()),  # 同步时间戳
            }
            if key in wait_retry:
                del wait_retry[key]
            logger.info(f"同步成功: {display_title} ({year}) -> 豆瓣 {subject_id} 评分 {douban_rating} 星")
            return True
        else:
            logger.error(f"豆瓣提交失败: {title} ({year}) subject_id={subject_id}")
            if key not in wait_retry:
                wait_retry[key] = {
                    "title": title,
                    "year": year,
                    "trakt_rating": trakt_rating,
                    "douban_rating": _trakt_rating_to_douban(trakt_rating),
                    "subject_id": subject_id,
                    "media_type": media_type.value,
                }
            return False

    def _sync_one_progress(self, item: Dict[str, Any], media_key: str, media_type: MediaType,
                           watching: Dict[str, Any]) -> None:
        """同步单条播放进度到豆瓣「在看」，并缓存数据

        Args:
            item: 播放进度项
            media_key: 媒体键名 (movie/show)
            media_type: 媒体类型
            watching: 在看缓存字典
        """
        progress = item.get("progress")
        # 修改进度判断:progress >= 10 才认为是正在观看
        if isinstance(progress, (int, float)) and progress < 10:
            title_temp = (item.get(media_key) or {}).get("title", "未知")
            logger.debug("进度低于 10 %,跳过: %s progress=%s", title_temp, progress)
            return
        if isinstance(progress, (int, float)) and progress >= 100:
            return
        media = item.get(media_key) or {}
        ids = media.get("ids") or {}
        tmdb_id = ids.get("tmdb")
        imdb_id = ids.get("imdb")
        trakt_id = ids.get("trakt") or media.get("trakt_id")
        slug = ids.get("slug") or ""
        title = media.get("title", "未知")
        year = media.get("year")

        if not tmdb_id and not imdb_id:
            logger.debug("Trakt 播放进度条目无 tmdb/imdb,跳过: %s (%s)", title, year)
            return

        # 生成唯一 key
        key = f"{media_type.value}_{str(trakt_id) if trakt_id else slug or f'{title}_{year}'}"

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._get_douban_info_by_tmdb(
                    int(tmdb_id) if tmdb_id else None,
                    imdb_id,
                    title=title,
                    year=year,
                    mtype=media_type,
                ),
                global_vars.loop,
            )
            result = future.result(timeout=30)
            douban_info = result if result else {}
        except Exception as e:
            logger.debug("匹配豆瓣未看完条目失败 %s (%s): %s", title, year, e)
            return

        subject_id = douban_info.get("id", None)
        if not subject_id:
            logger.debug("未找到豆瓣未看完条目: %s (%s)", title, year)
            return

        # 优先使用中文标题
        display_title = (
                douban_info.get("title") or
                douban_info.get("cn_name") or
                douban_info.get("name") or
                title
        )

        if self._douban_helper.set_watching_status(
                subject_id=subject_id,
                status="do",
                private=self._private,
                rating=None,
        ):
            # 缓存在看数据
            watching[key] = {
                "douban_id": subject_id,
                "title": display_title,
                "en_title": title,
                "year": year,
                "media_type": media_type.value,
                "progress": progress,
                "status": "在看",  # 标记状态
                "sync_time": int(time.time()),  # 同步时间戳
            }
            logger.info("同步未看完到豆瓣在看: %s (%s) -> 在看(progress=%s)", display_title, year, progress)
        else:
            logger.warning("同步未看完到豆瓣在看失败: %s (%s) subject_id=%s", title, year, subject_id)

    def sync_progress(self) -> None:
        """基于 Trakt 播放进度(未看完列表)同步豆瓣「在看」。

        使用 /sync/playback/movies 与 /sync/playback/episodes,需要 OAuth Access Token。
        仅对 progress < 100% 的条目,在豆瓣标记为在看(interest=do,不写评分)。
        """
        # 优先使用配置中的 Access Token,其次使用设备授权流程获得并缓存的 Token
        access_token = self._get_trakt_access_token_for_playback()
        if not access_token:
            logger.debug("未获取到 Trakt Access Token,跳过未看完列表同步")
            return

        headers = {
            **TRAKT_HEADERS_BASE,
            "trakt-api-key": self._trakt_client_id,
            "Authorization": f"Bearer {access_token}",
        }

        def _fetch_playback(path: str) -> List[Dict[str, Any]]:
            url = f"{TRAKT_API_BASE}{path}"
            try:
                resp = RequestUtils(timeout=20, headers=headers).get_res(url=url)
                if not resp:
                    logger.debug("Trakt 播放进度请求失败: %s", path)
                    return []
                if resp.status_code == 204:
                    return []
                if resp.status_code != 200:
                    logger.debug("Trakt 播放进度返回异常 %s: %s %s", path, resp.status_code, (resp.text or "")[:200])
                    return []
                data = resp.json()
                return data if isinstance(data, list) else []
            except Exception as e:
                logger.error("拉取 Trakt 播放进度失败 %s: %s", path, e, exc_info=True)
                return []

        # 豆瓣无法将电影设置为在看状态，因此只同步剧集
        # movies = _fetch_playback("/sync/playback/movies")
        episodes = _fetch_playback("/sync/playback/episodes")

        if not episodes:
            logger.debug("Trakt 播放进度列表为空,无未看完的条目")
            # 清空在看缓存
            self.save_data("watching", {})
            return

        # 获取在看缓存
        watching: Dict[str, Any] = self.get_data("watching") or {}

        success_count = 0
        for e in episodes or []:
            # episodes 里按整部剧标记在看,使用 show 信息
            if not e.get("show"):
                continue
            # 保留 progress, 但用 show 做匹配
            try:
                self._sync_one_progress(
                    {"progress": e.get("progress"), "show": e.get("show")},
                    "show",
                    MediaType.TV,
                    watching
                )
                success_count += 1
            except Exception as ex:
                logger.error(f"同步播放进度失败: {ex}", exc_info=True)

        # 保存在看缓存
        self.save_data("watching", watching)
        logger.info(f"Trakt 播放进度同步完成: 成功 {success_count} 条")

    def sync_ratings(self):
        all_items = []
        # 根据配置决定同步类型
        if self._sync_type in ["all", "movies"]:
            movies = self._fetch_trakt_ratings("movies")
            if movies:
                for item in movies:
                    item["_media_type"] = MediaType.MOVIE
                all_items.extend(movies)
                logger.info(f"获取到 {len(movies)} 条电影评分")

        if self._sync_type in ["all", "shows"]:
            shows = self._fetch_trakt_ratings("shows")
            if shows:
                for item in shows:
                    item["_media_type"] = MediaType.TV
                all_items.extend(shows)
                logger.info(f"获取到 {len(shows)} 条电视剧评分")

        if not all_items:
            logger.info("未获取到 Trakt 评分或接口异常")
            return

        # 按评分时间倒序,优先同步最近评分的;再按最大数量截断
        def _rated_at_sort_key(x: Dict[str, Any]) -> str:
            return (x.get("rated_at") or "")[:19]

        all_items.sort(key=_rated_at_sort_key, reverse=True)
        if self._max_sync_count > 0:
            all_items = all_items[: self._max_sync_count]
            logger.info("本次最多同步 %d 条,已按最近评分取前 N 条", self._max_sync_count)

        finished: Dict[str, Any] = self.get_data("finished") or {}
        wait_retry: Dict[str, Any] = self.get_data("wait") or {}

        success_count = 0
        fail_count = 0
        for item in all_items:
            media_type = item.pop("_media_type", MediaType.MOVIE)
            try:
                if self._sync_one_rate(item, finished, wait_retry, media_type=media_type):
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                logger.error(f"同步单条失败: {e}", exc_info=True)

        self.save_data("finished", finished)
        self.save_data("wait", wait_retry)
        logger.info(f"Trakt 评分同步完成: 成功 {success_count}, 失败 {fail_count}")

    # ***************************************************************** Trakt 鉴权逻辑 *****************************************************************

    def _get_cached_token(self) -> Optional[str]:
        """获取缓存的 Trakt Access Token(如果未过期)"""
        now_ts = int(time.time())
        token_data = self.get_data("trakt_token") or {}
        access_token = token_data.get("access_token")
        expires_at = int(token_data.get("expires_at") or 0)
        if access_token and expires_at > now_ts:
            return access_token
        return None

    def _create_device_code_and_wait(self) -> Optional[str]:
        """创建 Trakt 设备码并阻塞等待用户授权（最多10分钟）

        Returns:
            Optional[str]: 获取到的 access_token，失败返回 None
        """
        url = f"{TRAKT_API_BASE}/oauth/device/code"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "trakt-api-version": TRAKT_API_VERSION,
            "trakt-api-key": self._trakt_client_id,
        }
        try:
            resp = RequestUtils(timeout=10, headers=headers).post_res(
                url=url,
                json={"client_id": self._trakt_client_id},
            )
            if not resp or resp.status_code != 200:
                logger.warning("获取 Trakt 设备码失败: %s %s",
                               getattr(resp, "status_code", None),
                               getattr(resp, "text", "")[:200])
                return None

            data = resp.json()
            device_code = data.get("device_code")
            user_code = data.get("user_code")
            verification_url = data.get("verification_url")
            interval = int(data.get("interval") or 5)
            expires_in = int(data.get("expires_in") or 600)

            if not device_code or not user_code or not verification_url:
                logger.warning("Trakt 设备码返回内容不完整: %s", data)
                return None

            # 发送授权通知
            msg = (
                f"Trakt 评分同步豆瓣需要授权。\n\n"
                f"请在浏览器打开: {verification_url}\n"
                f"并输入授权码: {user_code}\n\n"
                f"系统将等待 10 分钟，请在此时间内完成授权。"
            )
            self._send_bark_notification("Trakt 评分同步豆瓣 授权", msg)
            logger.info(f"Trakt 设备码已生成: {user_code}")
            logger.info(f"授权链接: {verification_url}")
            logger.info("系统将阻塞等待授权，最多等待 10 分钟...")

            # 阻塞轮询，最多等待 10 分钟
            max_wait_seconds = 600  # 10 分钟
            start_time = time.time()
            attempt = 0

            while time.time() - start_time < max_wait_seconds:
                attempt += 1
                elapsed = int(time.time() - start_time)
                logger.info(f"第 {attempt} 次尝试获取 token (已等待 {elapsed} 秒)...")

                # 尝试交换 token
                access_token = self._exchange_device_token(device_code)
                if access_token:
                    logger.info(f"✅ 授权成功! 用时 {elapsed} 秒")
                    self._send_bark_notification(
                        "Trakt 授权成功",
                        f"Trakt 授权已完成，用时 {elapsed} 秒。\n未看完列表同步功能已启用。"
                    )
                    return access_token

                # 等待一段时间再试
                time.sleep(interval)

            # 超时
            logger.warning("❌ Trakt 授权超时（等待了 10 分钟）")
            self._send_bark_notification(
                "Trakt 授权超时",
                "等待授权超时（10分钟）。请重新运行同步任务或手动配置 Access Token。"
            )
            return None

        except Exception as e:
            logger.error("Trakt 设备码授权流程异常: %s", e, exc_info=True)
            return None

    def _exchange_device_token(self, device_code: str) -> Optional[str]:
        """使用设备码交换 Trakt Access Token

        Args:
            device_code: 设备码

        Returns:
            Optional[str]: access_token，失败或等待中返回 None
        """
        url = f"{TRAKT_API_BASE}/oauth/device/token"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "trakt-api-version": TRAKT_API_VERSION,
            "trakt-api-key": self._trakt_client_id,
        }
        try:
            resp = RequestUtils(timeout=10, headers=headers).post_res(
                url=url,
                json={
                    "code": device_code,
                    "client_id": self._trakt_client_id,
                    "client_secret": self._trakt_client_secret,
                },
            )
            if not resp:
                return None

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception as e:
                    logger.debug("解析 Trakt Access Token 响应失败: %s", e)
                    return None

                access_token = data.get("access_token")
                expires_in = int(data.get("expires_in") or 0)

                if access_token and expires_in > 0:
                    # 保存 token 到插件数据
                    expires_at = int(time.time()) + expires_in - 60
                    self.save_data("trakt_token", {
                        "access_token": access_token,
                        "expires_at": expires_at,
                    })

                    current_config = {
                        "enable": self._enable,
                        "trakt_username": self._trakt_username,
                        "trakt_client_id": self._trakt_client_id,
                        "trakt_client_secret": self._trakt_client_secret,
                        "trakt_access_token": access_token,  # ✅ 只更新这个字段
                        "douban_cookie": self._douban_cookie,
                        "private": self._private,
                        "sync_type": self._sync_type,
                        "max_sync_count": self._max_sync_count,
                        "cron": self._cron,
                        "bark_webhook_url": self._bark_webhook_url,
                    }
                    self.update_config(current_config)

                    logger.info(f"✅ Access Token 已保存到配置（有效期约 {expires_in // 3600} 小时）")

                    return access_token

                return None

            if resp.status_code == 400:
                # 授权等待中或失败
                try:
                    err = (resp.json().get("error") or "").lower()
                except Exception:
                    err = ""

                if err in ("authorization_pending", "slow_down"):
                    # 等待用户授权
                    return None
                else:
                    # 授权失败
                    logger.debug(f"Trakt 授权错误: {err}")
                    return None

            return None
        except Exception as e:
            logger.debug(f"交换 token 异常: {e}")
            return None

    def _get_trakt_access_token_for_playback(self) -> Optional[str]:
        """获取用于 /sync/playback 的 Trakt Access Token（阻塞式设备授权）

        优先顺序:
        1. 配置中的 trakt_access_token（始终视为有效，由用户自行维护）
        2. 插件数据中缓存的 trakt_token（包含 access_token 与 expires_at）
        3. 若无有效 token，启动设备码授权流程（阻塞等待10分钟）
        """
        # 1. 配置中显式提供的 Access Token，直接使用
        if self._trakt_access_token:
            logger.info("使用配置的 Access Token")
            return self._trakt_access_token

        # 2. 检查已缓存的 token
        cached_token = self._get_cached_token()
        if cached_token:
            logger.info("使用缓存的 Access Token")
            return cached_token

        # 3. 启动设备码授权流程（阻塞等待）
        if not self._trakt_client_id or not self._trakt_client_secret:
            logger.debug("未配置 Trakt Client Secret，无法自动获取 Access Token")
            return None

        logger.info("开始 Trakt 设备码授权流程...")
        return self._create_device_code_and_wait()

    # ***************************************************************** Bark 通知 *****************************************************************

    def _send_bark_notification(self, title: str, content: str) -> bool:
        """发送 Bark 通知（POST JSON 方式）

        参考: https://github.com/Finb/Bark

        Args:
            title: 通知标题
            content: 通知内容

        Returns:
            bool: 发送是否成功
        """
        if not self._bark_webhook_url:
            logger.debug("未配置 Bark Webhook URL，跳过通知发送")
            return False

        try:
            url = self._bark_webhook_url.rstrip('/')
            payload = {
                "title": title,
                "body": content,
                "group": "Trakt同步",
                "sound": "bell"
            }

            logger.debug(f"发送 Bark 通知: {title}")
            resp = RequestUtils(timeout=10, headers={"Content-Type": "application/json"}).post_res(
                url=url,
                json=payload
            )

            if resp and resp.status_code == 200:
                logger.info(f"✅ Bark 通知发送成功: {title}")
                return True
            else:
                logger.warning(f"❌ Bark 通知发送失败: HTTP {getattr(resp, 'status_code', 'None')}")
                return False
        except Exception as e:
            logger.error(f"❌ Bark 通知发送异常: {e}")
            return False

    # ***************************************************************** 插件配置 *****************************************************************

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enable", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "private", "label": "豆瓣仅自己可见"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "sync_type",
                                            "label": "同步类型",
                                            "items": [
                                                {"title": "全部(电影 + 电视剧)", "value": "all"},
                                                {"title": "仅电影", "value": "movies"},
                                                {"title": "仅电视剧", "value": "shows"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "trakt_username",
                                            "label": "Trakt 用户名",
                                            "placeholder": "例如 ialex-cube",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "trakt_client_id",
                                            "label": "Trakt Client ID",
                                            "placeholder": "在 trakt.tv/oauth/applications 创建应用获取(公开评分)",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "trakt_client_secret",
                                            "label": "Trakt Client Secret(可选)",
                                            "placeholder": "用于设备码授权自动获取 Access Token,请勿泄露",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "trakt_access_token",
                                            "label": "Trakt Access Token（可选，自动获取后会自动填充）",
                                            "placeholder": "留空将自动通过设备码授权获取（阻塞等待10分钟）",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "定时执行 cron",
                                            "placeholder": "默认 0 2 * * *(每天凌晨 2 点)",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_sync_count",
                                            "label": "最大同步数量",
                                            "placeholder": "0 表示不限制,单次最多同步条数",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "douban_cookie",
                                            "label": "豆瓣 Cookie",
                                            "placeholder": "从浏览器复制豆瓣 Cookie 粘贴到此处",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "bark_webhook_url",
                                            "label": "Bark Webhook URL",
                                            "placeholder": "https://api.day.app/your_key/your_message",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "📌 使用说明：\n"
                                                    "1. 在 https://trakt.tv/oauth/applications 创建应用获取 Client ID\n"
                                                    "2. 填写 Client Secret 启用自动授权（首次同步时会通过 Bark 发送授权链接，系统阻塞等待10分钟）\n"
                                                    "3. 授权成功后，Access Token 会自动回填到配置中\n"
                                                    "4. 豆瓣 Cookie 需从浏览器手动复制，失效时会通过 Bark 推送通知提醒更新\n"
                                                    "5. 支持同步电影和电视剧评分，以及未看完列表为「在看」",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enable": False,
            "trakt_username": "",
            "trakt_client_id": "",
            "trakt_client_secret": "",
            "trakt_access_token": "",
            "douban_cookie": "",
            "private": True,
            "sync_type": "all",
            "max_sync_count": 0,
            "cron": "0 2 * * *",
            "bark_webhook_url": "",
        }

    def get_page(self) -> Optional[List[dict]]:
        """获取插件详情页面,显示同步历史记录（包括看完和在看）"""
        finished = self.get_data("finished") or {}
        watching = self.get_data("watching") or {}

        # 兼容旧数据：如果 finished 为空但 synced 有数据，则迁移
        if not finished:
            synced = self.get_data("synced") or {}
            if synced:
                logger.info("检测到旧数据格式，迁移 synced -> finished")
                finished = synced
                self.save_data("finished", finished)
                # 可选：删除旧数据
                # self.save_data("synced", {})

        # 合并看完和在看的数据
        all_items = {}

        # 添加看完的记录
        for key, item in finished.items():
            douban_id = item.get("douban_id")
            if douban_id:
                all_items[douban_id] = item

        # 添加在看的记录（如果同一个 douban_id 既有看完又有在看，优先显示看完）
        for key, item in watching.items():
            douban_id = item.get("douban_id")
            if douban_id and douban_id not in all_items:
                all_items[douban_id] = item

        history_list = list(all_items.values())

        # 按同步时间倒序排列（最新的在前）
        history_list.sort(key=lambda x: x.get("sync_time", 0), reverse=True)

        # 限制显示最近 100 条
        history_list = history_list[:100]

        if not history_list:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无同步历史记录",
                    },
                }
            ]

        return [
            {
                "component": "VTable",
                "props": {
                    "hover": True,
                    "fixedHeader": True,
                },
                "content": [
                    {
                        "component": "thead",
                        "content": [
                            {
                                "component": "th",
                                "props": {"class": "text-start ps-4"},
                                "text": "标题",
                            },
                            {
                                "component": "th",
                                "props": {"class": "text-start ps-4"},
                                "text": "年份",
                            },
                            {
                                "component": "th",
                                "props": {"class": "text-start ps-4"},
                                "text": "类型",
                            },
                            {
                                "component": "th",
                                "props": {"class": "text-start ps-4"},
                                "text": "状态",
                            },
                            {
                                "component": "th",
                                "props": {"class": "text-start ps-4"},
                                "text": "Trakt 评分",
                            },
                            {
                                "component": "th",
                                "props": {"class": "text-start ps-4"},
                                "text": "豆瓣评分",
                            },
                            {
                                "component": "th",
                                "props": {"class": "text-start ps-4"},
                                "text": "同步时间",
                            },
                            {
                                "component": "th",
                                "props": {"class": "text-start ps-4"},
                                "text": "豆瓣 ID",
                            },
                        ],
                    },
                    {
                        "component": "tbody",
                        "content": [
                            {
                                "component": "tr",
                                "props": {"key": f"history_{idx}"},
                                "content": [
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": f"{item.get('title', '未知')}",
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": str(item.get('year', '-')),
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": item.get('media_type') if item.get('media_type') else "-",
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "content": [
                                            {
                                                "component": "VChip",
                                                "props": {
                                                    "size": "small",
                                                    "color": "success" if item.get('status') == "看完" else "primary",
                                                    "variant": "flat",
                                                },
                                                "text": item.get('status', '在看'),
                                            }
                                        ],
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": str(item.get('trakt_rating', '-')) if item.get(
                                            'status') == "看完" else "-",
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": str(item.get('douban_rating', '-')) if item.get(
                                            'status') == "看完" else "-",
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": datetime.fromtimestamp(item.get('sync_time', 0)).strftime(
                                            '%Y-%m-%d %H:%M') if item.get('sync_time') else "-",
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "content": [
                                            {
                                                "component": "VBtn",
                                                "props": {
                                                    "variant": "text",
                                                    "color": "primary",
                                                    "size": "small",
                                                    "href": f"https://movie.douban.com/subject/{item.get('douban_id', '')}/",
                                                    "target": "_blank",
                                                },
                                                "text": f"🔗 {item.get('douban_id', '')}",
                                            }
                                        ] if item.get('douban_id') else [],
                                        "text": "-" if not item.get('douban_id') else None,
                                    },
                                ],
                            }
                            for idx, item in enumerate(history_list)
                        ],
                    },
                ],
            }
        ]

    def get_state(self) -> bool:
        return self._enable

    def stop_service(self):
        pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/sync",
                "endpoint": self._api_sync,
                "methods": ["GET", "POST"],
                "summary": "手动执行同步",
                "description": "立即执行一次 Trakt 评分同步到豆瓣",
            }
        ]

    def _api_sync(self) -> Dict[str, Any]:
        """手动触发同步(API)"""
        try:
            self.run()
            return {"success": True, "message": "同步任务已执行"}
        except Exception as e:
            logger.error(f"手动同步失败: {e}", exc_info=True)
            return {"success": False, "message": str(e)}

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enable:
            return []
        try:
            from apscheduler.triggers.cron import CronTrigger
            cron = (self._cron or "").strip() or "0 2 * * *"
            trigger = CronTrigger.from_crontab(cron)
        except Exception as e:
            logger.warning(f"Trakt 评分同步插件 cron 解析失败,使用默认 0 2 * * *: {e}")
            try:
                from apscheduler.triggers.cron import CronTrigger
                trigger = CronTrigger.from_crontab("0 2 * * *")
            except Exception:
                trigger = None
        if trigger is None:
            return []
        return [
            {
                "id": "trakt_ratings_sync",
                "name": "Trakt 评分同步豆瓣",
                "trigger": trigger,
                "func": self.run,
                "kwargs": {},
            }
        ]
