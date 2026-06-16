# -*- coding: utf-8 -*-
"""
豆瓣书影音同步插件

插件定义、配置读取和任务调度入口。
所有业务逻辑分别由对应 helper 实现：
  - TraktHelper      → Trakt API 封装（评分拉取、播放进度、OAuth 授权）
  - DoubanHelper     → 豆瓣 Cookie 操作（标记看过/在看、写入评分）
  - WereadHelper     → 微信读书 Skill API（书架、阅读进度）
  - NeteaseOpenApiHelper → 网易云音乐开放平台 API（最近播放记录，按专辑聚合）
  - XiaoyuzhouHelper → 小宇宙 FM API（播客听取历史）
"""
import hashlib
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse, urlunparse

from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType
from app.utils.http import RequestUtils
from .douban_helper import DoubanHelper
from .netease_openapi_helper import NeteaseOpenApiHelper
from .trakt_helper import TraktHelper
from .weread_helper import WereadHelper
from .xiaoyuzhou_helper import XiaoyuzhouHelper


class TraktRatingsSync(_PluginBase):
    """豆瓣书影音同步插件入口，负责配置、调度和多平台同步编排。"""

    plugin_name = "豆瓣书影音同步"
    plugin_desc = "聚合多平台记录同步到豆瓣：Trakt 电影 →「看过」及评分，Trakt 剧集播放进度 →「在看」，微信读书书架 → 阅读记录，网易云音乐 → 「听过」专辑，小宇宙播客 → 「听过」。"
    plugin_icon = "trakt.png"
    plugin_version = "3.14.27"
    plugin_author = "ColorlessCube"
    author_url = "https://github.com/ColorlessCube"
    plugin_config_prefix = "trakt_ratings_sync_"
    plugin_order = 16
    auth_level = 1

    _enable: bool = False
    _trakt_username: str = ""
    _trakt_client_id: str = ""
    _trakt_client_secret: str = ""
    _trakt_access_token: str = ""
    _trakt_manual_mappings: str = ""
    _douban_cookie: str = ""
    _weread_api_key: str = ""
    _weread_limit: int = 20
    _netease_app_id: str = ""
    _netease_app_secret: str = ""
    _netease_private_key: str = ""
    _netease_access_token: str = ""
    _netease_refresh_token: str = ""
    _netease_token_expires_at: int = 0
    _netease_anonymous_access_token: str = ""
    _netease_device_id: str = ""
    _netease_qr_key: str = ""
    _netease_qr_url: str = ""
    _netease_limit: int = 20
    _xiaoyuzhou_cookie: str = ""
    _xiaoyuzhou_limit: int = 20
    _private: bool = True
    _sync_type: str = "all"   # all | movies | shows
    _max_sync_count: int = 0  # 0 = 不限制
    _trakt_history_limit: int = 20
    _trakt_history_days: int = 30
    _cron: str = "0 2 * * *"
    _bark_webhook_url: str = ""
    _weread_auth_notify_cooldown: int = 6 * 60 * 60
    _netease_auth_notify_cooldown: int = 10 * 60
    _netease_auth_wait_seconds: int = 5 * 60
    _netease_auth_poll_interval: int = 5

    # helper 实例（延迟初始化）
    _douban_helper: Optional[DoubanHelper] = None
    _trakt_helper: Optional[TraktHelper] = None
    _weread_helper: Optional[WereadHelper] = None
    _netease_openapi_helper: Optional[NeteaseOpenApiHelper] = None
    _xiaoyuzhou_helper: Optional[XiaoyuzhouHelper] = None

    # ------------------------------------------------------------------
    # 插件生命周期
    # ------------------------------------------------------------------

    def init_plugin(self, config: dict = None):
        """初始化插件配置，并重置依赖配置的 Helper 实例。"""
        config = config or {}
        self._enable = config.get("enable", False)
        self._trakt_username = (config.get("trakt_username") or "").strip()
        self._trakt_client_id = (config.get("trakt_client_id") or "").strip()
        self._trakt_client_secret = (config.get("trakt_client_secret") or "").strip()
        self._trakt_access_token = (config.get("trakt_access_token") or "").strip()
        self._trakt_manual_mappings = (config.get("trakt_manual_mappings") or "").strip()
        self._douban_cookie = (config.get("douban_cookie") or "").strip()
        self._weread_api_key = (config.get("weread_api_key") or "").strip()
        self._weread_limit = int(config.get("weread_limit") or 20)
        self._netease_app_id = (config.get("netease_app_id") or "").strip()
        self._netease_app_secret = (config.get("netease_app_secret") or "").strip()
        self._netease_private_key = (config.get("netease_private_key") or "").strip()
        self._netease_access_token = (config.get("netease_access_token") or "").strip()
        self._netease_refresh_token = (config.get("netease_refresh_token") or "").strip()
        self._netease_token_expires_at = int(config.get("netease_token_expires_at") or 0)
        self._netease_anonymous_access_token = (config.get("netease_anonymous_access_token") or "").strip()
        self._netease_device_id = NeteaseOpenApiHelper.normalize_device_id(
            config.get("netease_device_id") or "",
            self._netease_app_id,
        )
        self._netease_qr_key = (config.get("netease_qr_key") or "").strip()
        self._netease_qr_url = (config.get("netease_qr_url") or "").strip()
        self._netease_limit = int(config.get("netease_limit") or 20)
        self._xiaoyuzhou_cookie = (config.get("xiaoyuzhou_cookie") or "").strip()
        self._xiaoyuzhou_limit = int(config.get("xiaoyuzhou_limit") or 20)
        self._private = config.get("private", True)
        self._sync_type = config.get("sync_type", "all") or "all"
        self._max_sync_count = int(config.get("max_sync_count") or 0)
        self._trakt_history_limit = int(config.get("trakt_history_limit") or 20)
        self._trakt_history_days = int(config.get("trakt_history_days") or 30)
        self._cron = config.get("cron", "0 2 * * *") or "0 2 * * *"
        self._bark_webhook_url = (config.get("bark_webhook_url") or "").strip()

        # 重置 helper，下次 run() 时重新初始化
        self._douban_helper = None
        self._trakt_helper = None
        self._weread_helper = None
        self._netease_openapi_helper = None
        self._xiaoyuzhou_helper = None

    def run(self):
        """定时/手动触发入口：依次执行 Trakt 同步、微信读书同步、网易云音乐同步、小宇宙播客同步。"""
        if not self._enable:
            logger.debug("豆瓣书影音同步插件未启用，跳过")
            return

        # 初始化豆瓣 helper（注入通知回调）
        try:
            self._douban_helper = DoubanHelper(
                user_cookie=self._douban_cookie or None,
                notify_fn=self._send_bark_notification,
            )
        except Exception as e:
            logger.error("初始化豆瓣 Helper 失败: %s", e)
            return

        # 同步 Trakt 最近观看记录（有 client_id 就尝试，token 可在内部通过设备码获取）
        if self._trakt_client_id:
            try:
                self._sync_trakt()
            except Exception as e:
                logger.error("同步 Trakt 评分失败: %s", e, exc_info=True)

        # 同步微信读书最近阅读记录
        if self._weread_api_key:
            try:
                self._sync_weread()
            except Exception as e:
                logger.error("同步微信读书记录失败: %s", e, exc_info=True)

        # 同步网易云音乐最近听歌专辑到豆瓣「听过」
        if self._has_netease_source():
            try:
                self._sync_netease()
            except Exception as e:
                logger.error("同步网易云音乐记录失败: %s", e, exc_info=True)

        # 同步小宇宙播客最近听取记录到豆瓣「听过」
        if self._xiaoyuzhou_cookie:
            try:
                self._sync_xiaoyuzhou()
            except Exception as e:
                logger.error("同步小宇宙播客记录失败: %s", e, exc_info=True)

        logger.info("豆瓣书影音同步完成")

    # ------------------------------------------------------------------
    # 同步调度方法（内部）
    # ------------------------------------------------------------------

    def _sync_trakt(self) -> None:
        """同步 Trakt 最近观看记录，持久化并打印日志摘要。"""
        if not self._trakt_client_id:
            logger.debug("未配置 Trakt Client ID，跳过 Trakt 同步")
            return

        # 初始化 Trakt helper
        self._trakt_helper = TraktHelper(
            client_id=self._trakt_client_id,
            client_secret=self._trakt_client_secret,
            access_token=self._trakt_access_token,
            username=self._trakt_username,
            save_data_fn=self.save_data,
            get_data_fn=self.get_data,
            update_config_fn=self._merge_update_config,
            send_notification_fn=self._send_bark_notification,
            manual_mappings=self._parse_trakt_manual_mappings(),
        )

        # 同步 Trakt 评分 → 豆瓣看过
        try:
            self._sync_ratings()
        except Exception as e:
            logger.error("同步 Trakt 评分到豆瓣失败: %s", e, exc_info=True)

        # 同步 Trakt 播放进度 → 豆瓣在看
        try:
            self._sync_progress()
        except Exception as e:
            logger.error("同步 Trakt 观看进度到豆瓣失败: %s", e, exc_info=True)

    def _sync_ratings(self) -> None:
        """从 Trakt 拉取评分并批量同步到豆瓣「看过」。"""
        all_items: List[Dict[str, Any]] = []

        if self._sync_type in ("all", "movies"):
            movies = self._trakt_helper.fetch_ratings("movies")
            if movies:
                for item in movies:
                    item["_media_type"] = MediaType.MOVIE
                all_items.extend(movies)
                logger.info("获取到 %d 条电影评分", len(movies))

        if self._sync_type in ("all", "shows"):
            shows = self._trakt_helper.fetch_ratings("shows")
            if shows:
                for item in shows:
                    item["_media_type"] = MediaType.TV
                all_items.extend(shows)
                logger.info("获取到 %d 条电视剧评分", len(shows))

        if not all_items:
            logger.info("未获取到 Trakt 评分或接口异常")
            return

        # 按评分时间倒序，优先同步最近评分；按最大数量截断
        all_items.sort(key=lambda x: (x.get("rated_at") or "")[:19], reverse=True)
        if self._max_sync_count > 0:
            all_items = all_items[: self._max_sync_count]
            logger.info("本次最多同步 %d 条，已按最近评分取前 N 条", self._max_sync_count)

        finished: Dict[str, Any] = self.get_data("finished") or {}
        wait_retry: Dict[str, Any] = self.get_data("wait") or {}

        success_count = 0
        fail_count = 0
        for item in all_items:
            media_type = item.pop("_media_type", MediaType.MOVIE)
            try:
                if self._trakt_helper.sync_one_rate(
                    item, finished, wait_retry, media_type,
                    self._douban_helper, self._private
                ):
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                logger.error("同步单条失败: %s", e, exc_info=True)

        self.save_data("finished", finished)
        self.save_data("wait", wait_retry)
        logger.info("Trakt 评分同步完成: 成功 %d，失败 %d", success_count, fail_count)

    def _sync_progress(self) -> None:
        """从 Trakt 播放进度（未看完列表）同步豆瓣「在看」。"""
        if self._sync_type == "movies":
            logger.info("同步类型为仅电影，跳过 Trakt 剧集在看同步")
            return

        access_token = self._trakt_helper.get_access_token()
        if not access_token:
            logger.debug("未获取到 Trakt Access Token，跳过未看完列表同步")
            return

        episodes, recent_shows = self._fetch_trakt_progress_sources(access_token)
        if self._trakt_helper.has_oauth_unauthorized():
            logger.warning("Trakt OAuth Token 已失效，尝试刷新或重新授权后重试剧集同步")
            access_token = self._trakt_helper.get_access_token(force_reauthorize=True)
            if not access_token:
                logger.warning("Trakt 重新授权未完成，跳过本次剧集同步")
                return
            self._trakt_access_token = access_token
            self._trakt_helper.reset_oauth_unauthorized()
            episodes, recent_shows = self._fetch_trakt_progress_sources(access_token)

        # 豆瓣无法将电影设置为在看，仅同步剧集
        logger.info("获取到 %d 条 Trakt 剧集播放进度", len(episodes))

        watching: Dict[str, Any] = self.get_data("watching") or {}
        success_count = 0

        for e in episodes:
            if not e.get("show"):
                continue
            try:
                if self._trakt_helper.sync_one_progress(
                    {"progress": e.get("progress"), "show": e.get("show")},
                    "show",
                    MediaType.TV,
                    watching,
                    self._douban_helper,
                    self._private,
                ):
                    success_count += 1
            except Exception as ex:
                logger.error("同步播放进度失败: %s", ex, exc_info=True)

        logger.info("获取到 %d 个最近在看剧集", len(recent_shows))

        for history_item in recent_shows:
            show = history_item.get("show")
            if not show:
                continue
            try:
                if self._trakt_helper.sync_one_progress(
                    {"progress": "history", "show": show},
                    "show",
                    MediaType.TV,
                    watching,
                    self._douban_helper,
                    self._private,
                ):
                    success_count += 1
            except Exception as ex:
                logger.error("同步观看历史失败: %s", ex, exc_info=True)

        if not episodes and not recent_shows:
            logger.info("Trakt 剧集播放进度和最近观看历史均为空，无需同步在看")
            self.save_data("watching", {})
            return

        self.save_data("watching", watching)
        logger.info("Trakt 剧集在看同步完成: 成功 %d 条", success_count)

    def _fetch_trakt_progress_sources(
        self,
        access_token: str,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """拉取 Trakt 剧集播放进度和观看历史来源。"""
        self._trakt_helper.reset_oauth_unauthorized()
        episodes = self._trakt_helper.fetch_playback("/sync/playback/episodes", access_token)
        history_items = self._trakt_helper.fetch_history(
            media_type="shows",
            access_token=access_token,
            limit=self._trakt_history_limit,
        )
        recent_shows = self._extract_recent_history_shows(history_items)
        logger.info(
            "获取到 %d 条 Trakt 剧集观看历史，提取 %d 个最近在看剧集",
            len(history_items),
            len(recent_shows),
        )
        return episodes, recent_shows

    def _extract_recent_history_shows(self, history_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """从 Trakt 剧集观看历史中按剧集去重，并过滤过旧记录。"""
        result: List[Dict[str, Any]] = []
        seen = set()
        now = datetime.now(timezone.utc)

        for item in history_items or []:
            show = item.get("show") if isinstance(item, dict) else None
            if not show:
                continue
            watched_at = item.get("watched_at")
            if watched_at and self._trakt_history_days > 0:
                try:
                    watched_time = datetime.fromisoformat(watched_at.replace("Z", "+00:00"))
                    if (now - watched_time.astimezone(timezone.utc)).days > self._trakt_history_days:
                        continue
                except Exception as err:
                    logger.debug("解析 Trakt watched_at 失败: %s %s", watched_at, err)

            ids = show.get("ids") if isinstance(show.get("ids"), dict) else {}
            show_key = ids.get("trakt") or ids.get("slug") or f"{show.get('title')}_{show.get('year')}"
            if not show_key or show_key in seen:
                continue
            seen.add(show_key)
            result.append(item)
        return result

    def _parse_trakt_manual_mappings(self) -> Dict[str, str]:
        """解析 Trakt 条目到豆瓣 subject_id 的手动映射配置。"""
        mappings: Dict[str, str] = {}
        for line in (self._trakt_manual_mappings or "").splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if "=" in text:
                key, value = text.split("=", 1)
            elif ":" in text:
                key, value = text.rsplit(":", 1)
            else:
                logger.warning("Trakt 手动映射格式无效，已跳过: %s", text)
                continue
            key = key.strip().lower()
            value = value.strip()
            if key and value.isdigit():
                mappings[key] = value
            else:
                logger.warning("Trakt 手动映射内容无效，已跳过: %s", text)
        return mappings

    def _sync_weread(self) -> None:
        """同步微信读书最近阅读记录到豆瓣「在读/读过」，并持久化打印日志摘要。

        流程：
        1. 拉取微信读书最近阅读记录（含进度/状态）
        2. 持久化书单供详情页展示
        3. 对每本书：
           a. 先查 weread_book_id → douban_subject_id 缓存映射，命中则跳过搜索
           b. 缓存未命中时，调用 get_book_subject_id(title, author) 搜索豆瓣
              （内部按「书名+作者 → 纯书名 → 书名截断」逐级 fallback）
           c. 搜索成功后将映射写入缓存，下次直接复用
        4. 「读完」→ 豆瓣「读过」(collect)；「在读」→ 豆瓣「在读」(do)；其余跳过
        5. 已同步过（相同 subject_id + 状态未变）则跳过，避免重复提交
        """
        if not self._weread_api_key:
            logger.debug("未配置微信读书 Skill API Key，跳过同步")
            return

        if not self._weread_helper:
            self._weread_helper = WereadHelper(
                api_key=self._weread_api_key,
                notify_fn=self._send_weread_auth_notification,
            )

        logger.info("开始同步微信读书最近阅读记录...")
        books = self._weread_helper.get_recent_books(
            limit=self._weread_limit,
            include_progress=True,
        )

        if not books:
            logger.info("微信读书未获取到最近阅读记录（API Key 可能已失效或书架为空）")
            return

        # 持久化书单（供详情页展示）
        self.save_data("weread_books", books)

        logger.info("微信读书拉取完成，共 %d 本，开始同步到豆瓣：", len(books))
        for i, book in enumerate(books, 1):
            time_str = WereadHelper.format_reading_time(book.get("reading_time", 0))
            progress = book.get("reading_progress", 0)
            status = book.get("status", "")
            logger.info(
                "  %2d. 【%s】%s - %s | 进度 %s%% | 累计 %s",
                i, status, book.get("title", ""), book.get("author", ""), progress, time_str,
            )

        # 已同步缓存（key = 豆瓣 subject_id，value 含已同步的 status，避免重复提交）
        synced: Dict[str, Any] = self.get_data("weread_synced") or {}

        # weread_book_id → douban_subject_id 映射缓存（避免重复搜索豆瓣）
        # 结构：{ weread_book_id: { "subject_id": str, "douban_title": str } }
        book_id_map: Dict[str, Any] = self.get_data("weread_book_id_map") or {}

        success_count = 0
        skip_count = 0
        fail_count = 0

        for book in books:
            title = (book.get("title") or "").strip()
            author = (book.get("author") or "").strip()
            weread_book_id = (book.get("book_id") or "").strip()
            weread_status = book.get("status") or ""  # "读完" / "在读" / ""

            if not title:
                continue

            # 仅同步「读完」和「在读」，其余状态（未读等）跳过
            if weread_status == "读完":
                douban_status = "collect"
            elif weread_status == "在读":
                douban_status = "do"
            else:
                logger.debug("跳过非在读/读完书目: %s (status=%s)", title, weread_status)
                continue

            # ── 方案 2：先查 weread_book_id 缓存映射 ──────────────────────
            subject_id: Optional[str] = None
            douban_title: Optional[str] = None

            if weread_book_id and weread_book_id in book_id_map:
                cached_map = book_id_map[weread_book_id]
                subject_id = cached_map.get("subject_id")
                douban_title = cached_map.get("douban_title")
                logger.debug(
                    "命中 book_id 缓存: %s → 豆瓣 %s (id=%s)",
                    title, douban_title, subject_id,
                )

            # ── 方案 3：缓存未命中，执行 fallback 搜索 ────────────────────
            if not subject_id:
                try:
                    douban_title, subject_id = self._douban_helper.get_book_subject_id(
                        title=title, author=author or None
                    )
                except Exception as e:
                    logger.warning("豆瓣图书搜索异常 [%s]: %s", title, e)
                    fail_count += 1
                    continue

                if not subject_id:
                    logger.debug("豆瓣未找到图书条目（含 fallback）: %s", title)
                    fail_count += 1
                    continue

                # 搜索成功，写入 book_id 映射缓存
                if weread_book_id:
                    book_id_map[weread_book_id] = {
                        "subject_id": subject_id,
                        "douban_title": douban_title or title,
                        "weread_title": title,
                        "author": author,
                    }
                    logger.debug(
                        "新增 book_id 缓存: %s (weread=%s) → 豆瓣 %s (id=%s)",
                        title, weread_book_id, douban_title, subject_id,
                    )

            # ── 已同步且状态未变则跳过 ────────────────────────────────────
            cached = synced.get(subject_id) or {}
            if cached.get("douban_status") == douban_status:
                logger.debug(
                    "豆瓣图书已同步过且状态未变，跳过: %s (id=%s, status=%s)",
                    douban_title or title, subject_id, douban_status,
                )
                skip_count += 1
                continue

            # ── 提交到豆瓣 ────────────────────────────────────────────────
            ok = self._douban_helper.set_book_status(
                subject_id=subject_id,
                status=douban_status,
                private=self._private,
                rating=None,
            )
            if ok:
                synced[subject_id] = {
                    "douban_id": subject_id,
                    "douban_title": douban_title or title,
                    "douban_status": douban_status,
                    "weread_book_id": weread_book_id,
                    "weread_title": title,
                    "author": author,
                    "weread_status": weread_status,
                    "reading_progress": book.get("reading_progress", 0),
                    "sync_time": int(time.time()),
                }
                logger.info(
                    "微信读书 → 豆瓣 %s: %s (id=%s)",
                    "读过" if douban_status == "collect" else "在读",
                    douban_title or title, subject_id,
                )
                success_count += 1
            else:
                logger.warning("豆瓣图书提交失败: %s (id=%s)", title, subject_id)
                fail_count += 1

        # 持久化两份缓存
        self.save_data("weread_synced", synced)
        self.save_data("weread_book_id_map", book_id_map)
        logger.info(
            "微信读书同步完成: 成功 %d，跳过 %d（已同步），失败/未匹配 %d",
            success_count, skip_count, fail_count,
        )

    def _sync_netease(self) -> None:
        """同步网易云音乐最近一周听歌专辑到豆瓣「听过」。

        流程：
        1. 拉取最近一周播放记录，按专辑聚合
        2. 对每张专辑：
           a. 先查「专辑名+艺术家」→ douban_subject_id 缓存映射，命中则跳过搜索
           b. 缓存未命中时，调用 get_music_subject_id(title, artist) 搜索豆瓣
              （内部按「专辑+艺术家 → 纯专辑名」逐级 fallback）
           c. 搜索成功后将映射写入缓存，下次直接复用
        3. 调用 set_music_status("collect") 标记为「听过」
        4. 结果持久化到插件数据 "netease_albums"
        """
        if not self._has_netease_source():
            logger.debug("未配置网易云音乐开放平台 AppID/PrivateKey，跳过同步")
            return

        netease_helper = self._get_netease_openapi_helper()
        logger.info("开始同步网易云音乐最近听歌记录到豆瓣（官方开放平台）...")

        if not netease_helper:
            logger.warning("网易云音乐 Helper 初始化失败，跳过同步")
            return

        albums = netease_helper.get_recent_albums(limit=self._netease_limit)
        self._persist_netease_openapi_state(netease_helper)
        token_state = netease_helper.get_token_state()
        logger.info(
            f"网易云开放平台 Token 诊断: "
            f"refresh_attempts={token_state.get('refresh_attempts')}, "
            f"refresh_successes={token_state.get('refresh_successes')}, "
            f"refresh_failures={token_state.get('refresh_failures')}, "
            f"auth_required_count={token_state.get('auth_required_count')}, "
            f"last_refresh_code={token_state.get('last_refresh_code')}, "
            f"last_refresh_message={token_state.get('last_refresh_message')}"
        )
        if not albums:
            logger.info("网易云音乐未获取到最近专辑记录（凭据可能失效或暂无听歌记录）")
            return

        # 已同步缓存（key = 豆瓣 subject_id，避免重复提交）
        synced: Dict[str, Any] = self.get_data("netease_albums") or {}

        # 「专辑名+艺术家」→ douban_subject_id 映射缓存（避免重复搜索豆瓣）
        # 结构：{ "专辑名\tartist": { "subject_id": str, "douban_title": str } }
        album_map: Dict[str, Any] = self.get_data("netease_album_map") or {}

        success_count = 0
        skip_count = 0
        fail_count = 0

        for album_info in albums:
            album_name = album_info.get("album") or ""
            artist = album_info.get("artist") or ""
            if not album_name:
                continue

            # ── 方案 2：先查专辑缓存映射 ──────────────────────────────────
            # 用 tab 分隔专辑名和艺术家作为缓存 key，避免拼接歧义
            cache_key = f"{album_name}\t{artist}"
            subject_id: Optional[str] = None
            douban_title: Optional[str] = None

            if cache_key in album_map:
                cached_map = album_map[cache_key]
                subject_id = cached_map.get("subject_id")
                douban_title = cached_map.get("douban_title")
                logger.debug(
                    "命中专辑缓存: %s - %s → 豆瓣 %s (id=%s)",
                    artist, album_name, douban_title, subject_id,
                )

            # ── 方案 3：缓存未命中，执行 fallback 搜索 ────────────────────
            if not subject_id:
                try:
                    douban_title, subject_id = self._douban_helper.get_music_subject_id(
                        title=album_name, artist=artist or None
                    )
                except Exception as e:
                    logger.warning("豆瓣音乐搜索异常 [%s - %s]: %s", artist, album_name, e)
                    fail_count += 1
                    continue

                if not subject_id:
                    logger.debug("豆瓣未找到音乐条目（含 fallback）: %s - %s", artist, album_name)
                    fail_count += 1
                    continue

                # 搜索成功，写入专辑缓存
                album_map[cache_key] = {
                    "subject_id": subject_id,
                    "douban_title": douban_title or album_name,
                    "album": album_name,
                    "artist": artist,
                }
                logger.debug(
                    "新增专辑缓存: %s - %s → 豆瓣 %s (id=%s)",
                    artist, album_name, douban_title, subject_id,
                )

            # ── 已同步过则跳过 ────────────────────────────────────────────
            if subject_id in synced:
                logger.debug("豆瓣音乐已同步过，跳过: %s (id=%s)", douban_title or album_name, subject_id)
                skip_count += 1
                continue

            # ── 提交「听过」状态 ──────────────────────────────────────────
            ok = self._douban_helper.set_music_status(
                subject_id=subject_id,
                status="collect",
                private=self._private,
                rating=None,
            )
            if ok:
                synced[subject_id] = {
                    "douban_id": subject_id,
                    "douban_title": douban_title or album_name,
                    "album": album_name,
                    "artist": artist,
                    "song_count": album_info.get("song_count", 0),
                    "total_play_count": album_info.get("total_play_count", 0),
                    "songs": album_info.get("songs", []),
                    "sync_time": int(time.time()),
                }
                logger.info(
                    "网易云 → 豆瓣 听过: %s - %s (id=%s, 累计播放 %d 次)",
                    artist, album_name, subject_id, album_info.get("total_play_count", 0)
                )
                success_count += 1
            else:
                logger.warning("豆瓣音乐提交失败: %s - %s (id=%s)", artist, album_name, subject_id)
                fail_count += 1

        # 持久化两份缓存
        self.save_data("netease_albums", synced)
        self.save_data("netease_album_map", album_map)
        logger.info(
            "网易云音乐同步完成: 成功 %d，跳过 %d（已同步），失败/未匹配 %d",
            success_count, skip_count, fail_count,
        )

    def _sync_xiaoyuzhou(self) -> None:
        """同步小宇宙播客最近听取记录到豆瓣。

        流程：
        1. 拉取最近听取的播客单集（含播放进度 is_finished 字段）
        2. 按播客去重，取该播客最后一集的 is_finished 判断整体状态：
           - is_finished = True  → 豆瓣「听过」(collect)
           - is_finished = False 且有进度 → 豆瓣「在听」(do)
           - 无进度记录（listen_pct = 0）→ 出现在历史则认为「听过」(collect)
        3. 对每个播客：
           a. 先查「播客名」→ douban_subject_id 缓存映射，命中则跳过搜索
           b. 缓存未命中时，调用 get_podcast_subject_id(title) 搜索豆瓣
           c. 搜索成功后将映射写入缓存，下次直接复用
        4. 根据状态调用 set_podcast_status()，已同步且状态未变则跳过
        5. 结果持久化到插件数据 "xiaoyuzhou_episodes"
        """
        if not self._xiaoyuzhou_cookie:
            logger.debug("未配置小宇宙 Cookie，跳过同步")
            return

        if not self._xiaoyuzhou_helper:
            self._xiaoyuzhou_helper = XiaoyuzhouHelper(
                access_token=self._xiaoyuzhou_cookie,
                notify_fn=self._send_bark_notification,
            )

        logger.info("开始同步小宇宙播客最近听取记录到豆瓣...")

        episodes = self._xiaoyuzhou_helper.get_recent_episodes(limit=self._xiaoyuzhou_limit)
        self._persist_xiaoyuzhou_cookie_if_updated()
        if not episodes:
            logger.info("小宇宙未获取到最近听取记录（Cookie 可能已失效或暂无听取记录）")
            return

        # 持久化播客列表（供详情页展示）
        self.save_data("xiaoyuzhou_episodes", episodes)

        # 已同步缓存（key = 豆瓣 subject_id）
        # 结构：{ subject_id: { ..., "status": "collect" | "do" } }
        synced: Dict[str, Any] = self.get_data("xiaoyuzhou_podcasts") or {}

        # 「播客名」→ douban_subject_id 映射缓存（避免重复搜索豆瓣）
        # 结构：{ "播客名": { "subject_id": str, "douban_title": str } }
        podcast_map: Dict[str, Any] = self.get_data("xiaoyuzhou_podcast_map") or {}

        success_count = 0
        skip_count = 0
        fail_count = 0

        # ── 按播客去重，同时确定最佳状态 ──────────────────────────────────
        # 对同一播客，取所有单集中 is_finished=True 最优先；其次取 listen_pct 最大的
        seen_podcasts: Dict[str, Dict[str, Any]] = {}
        for ep in episodes:
            podcast_id = ep.get("podcast_id", "")
            podcast_name = ep.get("podcast_name", "")
            if not (podcast_id and podcast_name):
                continue
            if podcast_id not in seen_podcasts:
                seen_podcasts[podcast_id] = ep
            else:
                existing = seen_podcasts[podcast_id]
                # 已听完优先级最高，无需替换
                if existing.get("is_finished"):
                    continue
                # 替换为进度更高的那条
                if ep.get("is_finished") or ep.get("listen_pct", 0) > existing.get("listen_pct", 0):
                    seen_podcasts[podcast_id] = ep

        logger.info(
            "小宇宙拉取到 %d 条单集，去重后 %d 个播客，开始匹配豆瓣播客条目",
            len(episodes), len(seen_podcasts),
        )

        for podcast_id, ep_info in seen_podcasts.items():
            podcast_name = ep_info.get("podcast_name", "")
            if not podcast_name:
                continue

            # ── 确定要同步到豆瓣的状态 ────────────────────────────────────
            # is_finished=True 或 listen_pct=0（无进度记录）→ collect（听过）
            # is_finished=False 且有进度 → do（在听）
            listen_pct = ep_info.get("listen_pct", 0.0)
            is_finished = ep_info.get("is_finished", False)
            if is_finished or listen_pct == 0.0:
                target_status = "collect"
            else:
                target_status = "do"

            # ── 先查播客缓存映射 ───────────────────────────────────────────
            subject_id: Optional[str] = None
            douban_title: Optional[str] = None

            if podcast_name in podcast_map:
                cached_map = podcast_map[podcast_name]
                subject_id = cached_map.get("subject_id")
                douban_title = cached_map.get("douban_title")
                logger.debug(
                    "命中播客缓存: %s → 豆瓣 %s (id=%s)",
                    podcast_name, douban_title, subject_id,
                )

            # ── 缓存未命中，执行搜索 ─────────────────────────────────────
            if not subject_id:
                try:
                    douban_title, subject_id = self._douban_helper.get_podcast_subject_id(
                        title=podcast_name
                    )
                except Exception as e:
                    logger.warning("豆瓣播客搜索异常 [%s]: %s", podcast_name, e)
                    fail_count += 1
                    continue

                if not subject_id:
                    logger.warning(
                        "豆瓣未找到播客条目: %s；代表单集=%s；目标状态=%s；播放进度=%.0f%%",
                        podcast_name,
                        ep_info.get("title", ""),
                        "听过" if target_status == "collect" else "在听",
                        listen_pct * 100,
                    )
                    fail_count += 1
                    continue

                # 搜索成功，写入播客缓存
                podcast_map[podcast_name] = {
                    "subject_id": subject_id,
                    "douban_title": douban_title or podcast_name,
                    "podcast_name": podcast_name,
                }
                logger.debug(
                    "新增播客缓存: %s → 豆瓣 %s (id=%s)",
                    podcast_name, douban_title, subject_id,
                )

            # ── 已同步且状态相同则跳过 ────────────────────────────────────
            if subject_id in synced:
                cached_status = synced[subject_id].get("status", "collect")
                if cached_status == target_status:
                    logger.debug(
                        "豆瓣播客已同步（%s），状态无变化，跳过: %s (id=%s)",
                        target_status, douban_title or podcast_name, subject_id,
                    )
                    skip_count += 1
                    continue
                # 状态有变化（例如从「在听」升级为「听过」），继续提交
                logger.info(
                    "播客状态变更 %s → %s，重新提交: %s (id=%s)",
                    cached_status, target_status, douban_title or podcast_name, subject_id,
                )

            # ── 提交豆瓣状态 ─────────────────────────────────────────────
            ok = self._douban_helper.set_podcast_status(
                subject_id=subject_id,
                status=target_status,
                private=self._private,
                rating=None,
            )
            status_label = "听过" if target_status == "collect" else "在听"
            if ok:
                synced[subject_id] = {
                    "douban_id": subject_id,
                    "douban_title": douban_title or podcast_name,
                    "podcast_name": podcast_name,
                    "podcast_id": podcast_id,
                    "status": target_status,
                    "listen_pct": listen_pct,
                    "sync_time": int(time.time()),
                }
                logger.info(
                    "小宇宙 → 豆瓣 %s: %s (id=%s)",
                    status_label, douban_title or podcast_name, subject_id,
                )
                success_count += 1
            else:
                logger.warning("豆瓣播客提交失败: %s (id=%s)", podcast_name, subject_id)
                fail_count += 1

        # 持久化两份缓存
        self.save_data("xiaoyuzhou_podcasts", synced)
        self.save_data("xiaoyuzhou_podcast_map", podcast_map)
        logger.info(
            "小宇宙播客同步完成: 成功 %d，跳过 %d（已同步且状态无变化），失败/未匹配 %d",
            success_count, skip_count, fail_count,
        )

# ------------------------------------------------------------------
# 辅助工具
# ------------------------------------------------------------------

    def _has_netease_source(self) -> bool:
        """判断网易云音乐是否存在可用数据源配置。"""
        return bool(self._netease_app_id and self._netease_private_key)

    def _get_netease_openapi_helper(self) -> Optional[NeteaseOpenApiHelper]:
        """创建或复用网易云开放平台 Helper。"""
        if not (self._netease_app_id and self._netease_private_key):
            return None
        if not self._netease_openapi_helper:
            self._netease_openapi_helper = NeteaseOpenApiHelper(
                app_id=self._netease_app_id,
                private_key=self._netease_private_key,
                app_secret=self._netease_app_secret,
                access_token=self._netease_access_token,
                refresh_token=self._netease_refresh_token,
                token_expires_at=self._netease_token_expires_at,
                anonymous_access_token=self._netease_anonymous_access_token,
                device_id=self._netease_device_id,
                notify_fn=self._send_bark_notification,
                auth_required_fn=self._send_netease_auth_notification,
            )
        return self._netease_openapi_helper

    def _persist_netease_openapi_state(self, helper: NeteaseOpenApiHelper) -> None:
        """持久化网易云开放平台 Helper 的非空状态。"""
        state = helper.get_token_values()
        patch: Dict[str, Any] = {
            "netease_device_id": state.get("device_id") or self._netease_device_id,
            "netease_token_expires_at": state.get("token_expires_at") or self._netease_token_expires_at,
        }
        if state.get("anonymous_access_token"):
            patch["netease_anonymous_access_token"] = state["anonymous_access_token"]
        if state.get("access_token"):
            patch["netease_access_token"] = state["access_token"]
        if state.get("refresh_token"):
            patch["netease_refresh_token"] = state["refresh_token"]
        self._merge_update_config(patch)

    def _merge_update_config(self, patch: Dict[str, Any]) -> None:
        """将 patch 合并到当前配置后调用 update_config（避免覆盖其他字段）。"""
        current = {
            "enable": self._enable,
            "trakt_username": self._trakt_username,
            "trakt_client_id": self._trakt_client_id,
            "trakt_client_secret": self._trakt_client_secret,
            "trakt_access_token": self._trakt_access_token,
            "trakt_manual_mappings": self._trakt_manual_mappings,
            "douban_cookie": self._douban_cookie,
            "weread_api_key": self._weread_api_key,
            "weread_limit": self._weread_limit,
            "netease_app_id": self._netease_app_id,
            "netease_app_secret": self._netease_app_secret,
            "netease_private_key": self._netease_private_key,
            "netease_access_token": self._netease_access_token,
            "netease_refresh_token": self._netease_refresh_token,
            "netease_token_expires_at": self._netease_token_expires_at,
            "netease_anonymous_access_token": self._netease_anonymous_access_token,
            "netease_device_id": self._netease_device_id,
            "netease_qr_key": self._netease_qr_key,
            "netease_qr_url": self._netease_qr_url,
            "netease_limit": self._netease_limit,
            "xiaoyuzhou_cookie": self._xiaoyuzhou_cookie,
            "xiaoyuzhou_limit": self._xiaoyuzhou_limit,
            "private": self._private,
            "sync_type": self._sync_type,
            "max_sync_count": self._max_sync_count,
            "trakt_history_limit": self._trakt_history_limit,
            "trakt_history_days": self._trakt_history_days,
            "cron": self._cron,
            "bark_webhook_url": self._bark_webhook_url,
        }
        current.update(patch)
        self._trakt_access_token = current.get("trakt_access_token") or ""
        self._trakt_manual_mappings = current.get("trakt_manual_mappings") or ""
        self._netease_access_token = current.get("netease_access_token") or ""
        self._netease_refresh_token = current.get("netease_refresh_token") or ""
        self._netease_token_expires_at = int(current.get("netease_token_expires_at") or 0)
        self._netease_anonymous_access_token = current.get("netease_anonymous_access_token") or ""
        self._netease_device_id = current.get("netease_device_id") or self._netease_device_id
        self._netease_qr_key = current.get("netease_qr_key") or ""
        self._netease_qr_url = current.get("netease_qr_url") or ""
        self._netease_app_secret = current.get("netease_app_secret") or ""
        self.update_config(current)

    def _persist_xiaoyuzhou_cookie_if_updated(self) -> None:
        """持久化小宇宙自动刷新后的 Cookie。"""
        if not self._xiaoyuzhou_helper:
            return
        refreshed_cookie = self._xiaoyuzhou_helper.get_updated_cookie_string()
        if not refreshed_cookie or refreshed_cookie == self._xiaoyuzhou_cookie:
            return
        self._xiaoyuzhou_cookie = refreshed_cookie
        self._merge_update_config({"xiaoyuzhou_cookie": refreshed_cookie})
        logger.info("小宇宙 Token 自动刷新结果已写回插件配置")

    def _send_bark_notification(self, title: str, content: str, link_url: str = "") -> bool:
        """发送 Bark 推送通知（POST JSON 方式）。

        参考: https://github.com/Finb/Bark
        """
        if not self._bark_webhook_url:
            logger.debug("未配置 Bark Webhook URL，跳过通知发送")
            return False
        try:
            title = (title or "").strip()
            content = (content or "").strip()
            if not title and not content:
                logger.warning("Bark 通知标题和正文均为空，跳过发送")
                return False
            if not title:
                title = self.plugin_name
            if not content:
                content = title

            url, payload = self._build_bark_request(self._bark_webhook_url, title, content, link_url)
            logger.debug("发送 Bark 通知: %s", title)
            resp = RequestUtils(
                timeout=10, headers={"Content-Type": "application/json"}
            ).post_res(url=url, json=payload)
            if resp and resp.status_code == 200:
                logger.info("✅ Bark 通知发送成功: %s", title)
                return True
            logger.warning(
                "❌ Bark 通知发送失败: HTTP %s %s",
                getattr(resp, "status_code", "None"),
                (getattr(resp, "text", "") or "")[:200],
            )
            return False
        except Exception as e:
            logger.error("❌ Bark 通知发送异常: %s", e)
            return False

    @staticmethod
    def _build_bark_request(
            webhook_url: str,
            title: str,
            content: str,
            link_url: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        """构造 Bark 请求 URL 和 JSON 参数。"""
        url = webhook_url.strip().rstrip("/")
        link_url = (link_url or "").strip()
        parsed = urlparse(url)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments and segments[-1] != "push":
            device_key = segments[-1]
            body = content if len(content) <= 1800 else f"{content[:1800]}..."
            query_params = {"group": "豆瓣书影音同步", "sound": "bell"}
            if link_url:
                query_params["url"] = link_url
            path_segments = segments[:-1] + [
                quote(device_key, safe=""),
                quote(title, safe=""),
                quote(body, safe=""),
            ]
            path_url = urlunparse(
                parsed._replace(
                    path="/" + "/".join(path_segments),
                    params="",
                    query=urlencode(query_params),
                    fragment="",
                )
            )
            return path_url, {}
        payload = {
            "title": title,
            "body": content,
            "group": "豆瓣书影音同步",
            "sound": "bell",
        }
        if link_url:
            payload["url"] = link_url
        return url, payload

    def _send_netease_auth_notification(self) -> bool:
        """发送网易云重新扫码认证通知，并把短期二维码链接写回配置。"""
        helper = self._netease_openapi_helper or self._get_netease_openapi_helper()
        if not helper:
            return False

        fingerprint_source = f"{self._netease_app_id}:{self._netease_device_id}:{self._netease_refresh_token[:16]}"
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
        state = self.get_data("netease_auth_notify_state") or {}
        now = int(time.time())
        try:
            last_notified_at = int(state.get("last_notified_at") or 0)
        except (TypeError, ValueError):
            last_notified_at = 0
        if (
            state.get("fingerprint") == fingerprint
            and now - last_notified_at < self._netease_auth_notify_cooldown
        ):
            logger.warning(
                "网易云鉴权失败通知仍在冷却期内，跳过 Bark 推送: cooldown=%ss",
                self._netease_auth_notify_cooldown,
            )
            return False

        qrcode = helper.get_login_qrcode()
        self._persist_netease_openapi_state(helper)
        if not qrcode:
            self.save_data("netease_auth_notify_state", {
                "fingerprint": fingerprint,
                "last_notified_at": now,
                "title": "网易云音乐授权已失效",
            })
            return self._send_bark_notification(
                "网易云音乐授权已失效",
                "自动生成认证链接失败，请打开豆瓣书影音同步插件页面重新生成网易云二维码。",
            )

        patch = {
            "netease_qr_key": qrcode.get("uniKey") or "",
            "netease_qr_url": qrcode.get("qrCodeUrl") or "",
        }
        self._merge_update_config(patch)
        auth_url = patch["netease_qr_url"]
        self.save_data("netease_auth_notify_state", {
            "fingerprint": fingerprint,
            "last_notified_at": now,
            "title": "网易云音乐授权已失效",
            "qr_key": patch["netease_qr_key"],
        })
        content = (
            "网易云开放平台 Token 已失效，点击通知打开认证链接并在 5 分钟内完成授权。"
            "插件会在本次任务中自动轮询授权结果。"
        )
        sent = self._send_bark_notification("网易云音乐需要重新授权", content, auth_url)
        if not sent:
            return False
        return self._wait_netease_auth_completion(helper, patch["netease_qr_key"]) or sent

    def _wait_netease_auth_completion(self, helper: NeteaseOpenApiHelper, qr_key: str) -> bool:
        """等待网易云扫码授权完成，并把新 Token 写回插件配置。"""
        if not qr_key:
            return False
        deadline = time.time() + self._netease_auth_wait_seconds
        while time.time() < deadline:
            time.sleep(self._netease_auth_poll_interval)
            status_data = helper.poll_login_qrcode(qr_key)
            self._persist_netease_openapi_state(helper)
            if not status_data:
                continue
            status_code = status_data.get("status")
            status_message = status_data.get("msg") or ""
            if status_code == 803:
                self._merge_update_config({"netease_qr_key": "", "netease_qr_url": ""})
                self._send_bark_notification("网易云音乐授权成功", "网易云开放平台授权已完成，Token 已写回插件配置。")
                logger.info("网易云音乐扫码授权成功，新 Token 已写回插件配置")
                return True
            if status_code == 800:
                self._merge_update_config({"netease_qr_key": "", "netease_qr_url": ""})
                logger.warning("网易云音乐认证二维码已过期: %s", status_message or "二维码不存在或过期")
                return False
        logger.warning("网易云音乐扫码授权等待超时，请稍后重新触发同步生成新的认证链接")
        return False

    def _send_weread_auth_notification(self, title: str, content: str) -> bool:
        """发送微信读书鉴权失败通知，并按凭据指纹做持久化冷却。"""
        auth_value = self._weread_api_key
        fingerprint = hashlib.sha256(auth_value.encode("utf-8")).hexdigest()[:16]
        state = self.get_data("weread_auth_notify_state") or {}
        now = int(time.time())
        try:
            last_notified_at = int(state.get("last_notified_at") or 0)
        except (TypeError, ValueError):
            last_notified_at = 0

        if (
            state.get("fingerprint") == fingerprint
            and now - last_notified_at < self._weread_auth_notify_cooldown
        ):
            logger.warning(
                "微信读书鉴权失败通知仍在冷却期内，跳过 Bark 推送: cooldown=%ss",
                self._weread_auth_notify_cooldown,
            )
            return False

        self.save_data("weread_auth_notify_state", {
            "fingerprint": fingerprint,
            "last_notified_at": now,
            "title": title,
        })
        return self._send_bark_notification(title, content)

    # ------------------------------------------------------------------
    # 插件接口
    # ------------------------------------------------------------------

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enable

    def stop_service(self):
        """停止插件服务。"""
        pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件注册命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件暴露给 MoviePilot 的 API 定义。"""
        return [
            {
                "path": "/sync",
                "endpoint": self._api_sync,
                "methods": ["GET", "POST"],
                "summary": "手动执行同步",
                "description": "立即执行一次 Trakt 评分同步到豆瓣",
            },
            {
                "path": "/traktratingssync/netease/qrcode",
                "endpoint": self._api_netease_qrcode,
                "methods": ["GET", "POST"],
                "summary": "生成网易云官方登录二维码",
                "description": "通过网易云开放平台生成 App 扫码登录二维码",
            },
            {
                "path": "/traktratingssync/netease/qrcode/status",
                "endpoint": self._api_netease_qrcode_status,
                "methods": ["GET", "POST"],
                "summary": "轮询网易云官方登录二维码",
                "description": "轮询上一次生成的网易云登录二维码状态，成功后写回 Token",
            },
            {
                "path": "/traktratingssync/netease/test",
                "endpoint": self._api_netease_test,
                "methods": ["GET", "POST"],
                "summary": "测试网易云官方最近播放专辑",
                "description": "拉取网易云开放平台最近播放专辑，用于验证官方授权是否可用",
            }
        ]

    def _api_sync(self) -> Dict[str, Any]:
        """手动触发同步（API 端点）。"""
        try:
            self.run()
            return {"success": True, "message": "同步任务已执行"}
        except Exception as e:
            logger.error("手动同步失败: %s", e, exc_info=True)
            return {"success": False, "message": str(e)}

    def _api_netease_qrcode(self) -> Dict[str, Any]:
        """生成网易云官方二维码登录链接。"""
        helper = self._get_netease_openapi_helper()
        if not helper:
            return {"success": False, "message": "请先配置网易云开放平台 AppID 和 PrivateKey"}

        qrcode = helper.get_login_qrcode()
        self._persist_netease_openapi_state(helper)
        if not qrcode:
            message = helper.get_last_error() or "生成网易云登录二维码失败，请检查 AppID/PrivateKey 和开放平台权限"
            return {"success": False, "message": message}

        patch = {
            "netease_qr_key": qrcode.get("uniKey") or "",
            "netease_qr_url": qrcode.get("qrCodeUrl") or "",
        }
        self._merge_update_config(patch)
        return {
            "success": True,
            "message": "请在 5 分钟内使用网易云音乐 App 扫码授权",
            "qrCodeUrl": qrcode.get("qrCodeUrl"),
            "uniKey": qrcode.get("uniKey"),
        }

    def _api_netease_qrcode_status(self) -> Dict[str, Any]:
        """轮询网易云官方二维码登录状态。"""
        helper = self._get_netease_openapi_helper()
        if not helper:
            return {"success": False, "message": "请先配置网易云开放平台 AppID 和 PrivateKey"}
        if not self._netease_qr_key:
            return {"success": False, "message": "请先生成网易云登录二维码"}

        status_data = helper.poll_login_qrcode(self._netease_qr_key)
        self._persist_netease_openapi_state(helper)
        if not status_data:
            return {"success": False, "message": "轮询网易云登录二维码失败"}

        status_code = status_data.get("status")
        status_message = status_data.get("msg") or ""
        if status_code == 803:
            self._merge_update_config({"netease_qr_key": "", "netease_qr_url": ""})
            return {"success": True, "status": status_code, "message": "网易云扫码授权成功"}
        if status_code == 800:
            self._merge_update_config({"netease_qr_key": "", "netease_qr_url": ""})
            return {"success": False, "status": status_code, "message": status_message or "二维码已过期，请重新生成"}
        return {"success": False, "status": status_code, "message": status_message or "等待扫码"}

    def _api_netease_test(self) -> Dict[str, Any]:
        """测试网易云开放平台最近播放专辑接口。"""
        helper = self._get_netease_openapi_helper()
        if not helper:
            return {"success": False, "message": "请先配置网易云开放平台 AppID 和 PrivateKey"}
        albums = helper.get_recent_albums(limit=min(self._netease_limit, 10))
        self._persist_netease_openapi_state(helper)
        return {
            "success": bool(albums),
            "message": f"获取到 {len(albums)} 张最近播放专辑" if albums else "未获取到最近播放专辑",
            "albums": albums[:10],
            "token_state": helper.get_token_state(),
        }

    def get_service(self) -> List[Dict[str, Any]]:
        """返回定时同步服务定义。"""
        if not self._enable:
            return []
        try:
            from apscheduler.triggers.cron import CronTrigger
            cron = (self._cron or "").strip() or "0 2 * * *"
            trigger = CronTrigger.from_crontab(cron)
        except Exception as e:
            logger.warning("Trakt 评分同步插件 cron 解析失败，使用默认 0 2 * * *: %s", e)
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
                "name": "豆瓣书影音同步",
                "trigger": trigger,
                "func": self.run,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页表单结构和默认配置。"""
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "trakt_manual_mappings",
                                            "label": "Trakt → 豆瓣手动映射（可选）",
                                            "placeholder": "每行一条，例如：imdb:tt1234567=12345678 或 movie:294048=12345678",
                                            "rows": 2,
                                            "auto-grow": True,
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
                                            "model": "trakt_history_limit",
                                            "label": "Trakt 剧集观看历史数量",
                                            "placeholder": "20",
                                            "type": "number",
                                            "hint": "用于将最近看过单集的剧集同步为豆瓣在看",
                                            "persistent-hint": True,
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
                                            "model": "trakt_history_days",
                                            "label": "Trakt 剧集观看历史天数",
                                            "placeholder": "30",
                                            "type": "number",
                                            "hint": "只处理最近 N 天观看过单集的剧集，0 表示不限制",
                                            "persistent-hint": True,
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
                                            "placeholder": "可直接填豆瓣 Cookie，或粘贴包含 Cookie 的完整豆瓣 cURL",
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 10},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "weread_api_key",
                                            "label": "微信读书 Skill API Key",
                                            "placeholder": "wrk-...",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "weread_limit",
                                            "label": "微信读书同步数量",
                                            "placeholder": "20",
                                            "type": "number",
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
                                            "model": "netease_app_id",
                                            "label": "网易云开放平台 AppID",
                                            "placeholder": "b3010d...",
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
                                            "model": "netease_app_secret",
                                            "label": "网易云开放平台 AppSecret",
                                            "type": "password",
                                            "placeholder": "用于后续 Token 刷新",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "netease_private_key",
                                            "label": "网易云开放平台 PrivateKey",
                                            "placeholder": "粘贴开放平台 RSA PrivateKey，不需要依赖 CLI",
                                            "rows": 2,
                                            "auto-grow": True,
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
                                            "model": "netease_device_id",
                                            "label": "网易云开放平台 Device ID",
                                            "placeholder": "留空自动生成，仅允许字母数字",
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
                                            "model": "netease_access_token",
                                            "label": "网易云 Access Token（自动填充）",
                                            "type": "password",
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
                                            "model": "netease_refresh_token",
                                            "label": "网易云 Refresh Token（自动填充）",
                                            "type": "password",
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
                                            "model": "netease_token_expires_at",
                                            "label": "网易云 Token 过期时间戳",
                                            "readonly": True,
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
                                            "model": "netease_limit",
                                            "label": "网易云同步专辑数",
                                            "placeholder": "20",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 10},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "xiaoyuzhou_cookie",
                                            "label": "小宇宙 FM Token（可选）",
                                            "placeholder": "可直接填 x-jike-access-token，或粘贴包含该字段的完整小宇宙 cURL",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "xiaoyuzhou_limit",
                                            "label": "小宇宙同步播客数",
                                            "placeholder": "20",
                                            "type": "number",
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
                                            "text": (
                                                "📌 使用说明：\n"
                                                "1. 在 https://trakt.tv/oauth/applications 创建应用获取 Client ID\n"
                                                "2. 填写 Client Secret 启用自动授权（首次同步时会通过 Bark 发送授权链接，系统阻塞等待10分钟）\n"
                                                "3. 授权成功后，Access Token 会自动回填到配置中\n"
                                                "4. Trakt 自动匹配失败的条目可在手动映射中填写 imdb/tmdb/trakt 与豆瓣 subject_id\n"
                                                "5. 豆瓣支持两种填写方式：直接填 Cookie，或粘贴包含 Cookie 的完整 cURL；失效时会通过 Bark 推送通知提醒更新\n"
                                                "6. 微信读书填写 Skill API Key（wrk-...），不再支持旧版阅读页 cURL\n"
                                                "7. 支持同步电影和电视剧评分，以及未看完列表为「在看」\n"
                                                "8. 网易云优先使用开放平台 AppID/PrivateKey + 官方扫码授权；Token 失效时会通过 Bark 推送短期认证链接\n"
                                                "9. 网易云会将最近播放专辑同步到豆瓣音乐「听过」\n"
                                                "10. 小宇宙支持两种填写方式：直接填 x-jike-access-token，或粘贴包含该字段的完整小宇宙 cURL"
                                            ),
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
            "trakt_manual_mappings": "",
            "douban_cookie": "",
            "weread_api_key": "",
            "weread_limit": 20,
            "netease_app_id": "",
            "netease_app_secret": "",
            "netease_private_key": "",
            "netease_access_token": "",
            "netease_refresh_token": "",
            "netease_token_expires_at": 0,
            "netease_anonymous_access_token": "",
            "netease_device_id": "",
            "netease_qr_key": "",
            "netease_qr_url": "",
            "netease_limit": 20,
            "xiaoyuzhou_cookie": "",
            "xiaoyuzhou_limit": 20,
            "private": True,
            "sync_type": "all",
            "max_sync_count": 0,
            "trakt_history_limit": 20,
            "trakt_history_days": 30,
            "cron": "0 2 * * *",
            "bark_webhook_url": "",
        }

    def get_page(self) -> Optional[List[dict]]:
        """插件详情页：展示 Trakt 同步历史（看完/在看）、微信读书最近阅读、网易云音乐同步记录、小宇宙播客同步记录。"""
        finished = self.get_data("finished") or {}
        watching = self.get_data("watching") or {}

        # 兼容旧数据：synced → finished
        if not finished:
            synced = self.get_data("synced") or {}
            if synced:
                logger.info("检测到旧数据格式，迁移 synced -> finished")
                finished = synced
                self.save_data("finished", finished)

        # 合并看完和在看（同一 douban_id 优先显示看完）
        all_items: Dict[str, Any] = {}
        for item in finished.values():
            douban_id = item.get("douban_id")
            if douban_id:
                all_items[douban_id] = item
        for item in watching.values():
            douban_id = item.get("douban_id")
            if douban_id and douban_id not in all_items:
                all_items[douban_id] = item

        history_list = sorted(all_items.values(), key=lambda x: x.get("sync_time", 0), reverse=True)[:100]

        # 微信读书区块
        weread_books: List[Dict[str, Any]] = self.get_data("weread_books") or []
        weread_section: List[dict] = []
        if weread_books:
            weread_section = [
                {
                    "component": "VRow",
                    "props": {"class": "mt-4"},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-h6 mb-2"},
                                    "text": "📚 微信读书 · 书籍",
                                }
                            ],
                        }
                    ],
                },
                {
                    "component": "VTable",
                    "props": {"hover": True, "fixedHeader": True},
                    "content": [
                        {
                            "component": "thead",
                            "content": [
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "书名"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "作者"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "状态"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "进度"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "累计阅读"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "读完时间"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "链接"},
                            ],
                        },
                        {
                            "component": "tbody",
                            "content": [
                                {
                                    "component": "tr",
                                    "props": {"key": f"weread_{idx}"},
                                    "content": [
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": book.get("title", ""),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": book.get("author", "-"),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "content": [
                                                {
                                                    "component": "VChip",
                                                    "props": {
                                                        "size": "small",
                                                        "color": (
                                                            "success" if book.get("status") == "读完"
                                                            else "primary" if book.get("status") == "在读"
                                                            else "default"
                                                        ),
                                                        "variant": "flat",
                                                    },
                                                    "text": book.get("status", "在读"),
                                                }
                                            ],
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": f"{book.get('reading_progress', 0)}%",
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": WereadHelper.format_reading_time(book.get("reading_time", 0)),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": book.get("finished_date") or "-",
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
                                                        "href": book.get("weread_url", ""),
                                                        "target": "_blank",
                                                    },
                                                    "text": "🔗 阅读",
                                                }
                                            ] if book.get("weread_url") else [],
                                        },
                                    ],
                                }
                                for idx, book in enumerate(weread_books)
                            ],
                        },
                    ],
                },
            ]

        # 网易云音乐同步记录区块
        netease_synced: Dict[str, Any] = self.get_data("netease_albums") or {}
        netease_list = sorted(
            netease_synced.values(), key=lambda x: x.get("sync_time", 0), reverse=True
        )[:100]
        netease_section: List[dict] = []
        if netease_list:
            netease_section = [
                {
                    "component": "VRow",
                    "props": {"class": "mt-4"},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-h6 mb-2"},
                                    "text": "🎵 网易云音乐 · 音乐",
                                }
                            ],
                        }
                    ],
                },
                {
                    "component": "VTable",
                    "props": {"hover": True, "fixedHeader": True},
                    "content": [
                        {
                            "component": "thead",
                            "content": [
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "专辑"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "艺术家"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "曲目数"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "累计播放"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "同步时间"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "豆瓣"},
                            ],
                        },
                        {
                            "component": "tbody",
                            "content": [
                                {
                                    "component": "tr",
                                    "props": {"key": f"netease_{idx}"},
                                    "content": [
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": rec.get("douban_title") or rec.get("album", "-"),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": rec.get("artist", "-"),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": str(rec.get("song_count", "-")),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": str(rec.get("total_play_count", "-")),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": (
                                                datetime.fromtimestamp(rec["sync_time"]).strftime("%Y-%m-%d %H:%M")
                                                if rec.get("sync_time") else "-"
                                            ),
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
                                                        "href": f"https://music.douban.com/subject/{rec.get('douban_id', '')}/",
                                                        "target": "_blank",
                                                    },
                                                    "text": f"🔗 {rec.get('douban_id', '')}",
                                                }
                                            ] if rec.get("douban_id") else [],
                                            "text": "-" if not rec.get("douban_id") else None,
                                        },
                                    ],
                                }
                                for idx, rec in enumerate(netease_list)
                            ],
                        },
                    ],
                },
            ]

        # 小宇宙播客同步记录区块
        xiaoyuzhou_episodes: List[Dict[str, Any]] = self.get_data("xiaoyuzhou_episodes") or []
        xiaoyuzhou_section: List[dict] = []
        if xiaoyuzhou_episodes:
            xiaoyuzhou_section = [
                {
                    "component": "VRow",
                    "props": {"class": "mt-4"},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-h6 mb-2"},
                                    "text": "🎙️ 小宇宙 FM · 播客",
                                }
                            ],
                        }
                    ],
                },
                {
                    "component": "VTable",
                    "props": {"hover": True, "fixedHeader": True},
                    "content": [
                        {
                            "component": "thead",
                            "content": [
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "单集"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "播客"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "时长"},
                                {"component": "th", "props": {"class": "text-start ps-4"}, "text": "播放状态"},
                            ],
                        },
                        {
                            "component": "tbody",
                            "content": [
                                {
                                    "component": "tr",
                                    "props": {"key": f"xiaoyuzhou_{idx}"},
                                    "content": [
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": ep.get("title", ""),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": ep.get("podcast_name", "-"),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "text": XiaoyuzhouHelper.format_duration(ep.get("duration", 0)),
                                        },
                                        {
                                            "component": "td",
                                            "props": {"class": "text-start ps-4"},
                                            "content": [
                                                {
                                                    "component": "VChip",
                                                    "props": {
                                                        "size": "small",
                                                        "color": (
                                                            "success" if ep.get("is_finished")
                                                            else "primary" if ep.get("listen_pct", 0) > 0
                                                            else "default"
                                                        ),
                                                        "variant": "flat",
                                                    },
                                                    "text": (
                                                        "已听完" if ep.get("is_finished")
                                                        else f"听了 {ep.get('listen_pct', 0)*100:.0f}%" if ep.get("listen_pct", 0) > 0
                                                        else "无记录"
                                                    ),
                                                }
                                            ],
                                        },
                                    ],
                                }
                                for idx, ep in enumerate(xiaoyuzhou_episodes)
                            ],
                        },
                    ],
                },
            ]

        if not history_list:
            return [
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "暂无 Trakt 同步历史记录",
                    },
                }
            ] + weread_section + netease_section + xiaoyuzhou_section

        # Trakt 同步历史区块（含标题）
        trakt_section = [
            {
                "component": "VRow",
                "props": {"class": "mt-4"},
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "text-h6 mb-2"},
                                "text": "🎬 Trakt · 视频",
                            }
                        ],
                    }
                ],
            },
            {
                "component": "VTable",
                "props": {"hover": True, "fixedHeader": True},
                "content": [
                    {
                        "component": "thead",
                        "content": [
                            {"component": "th", "props": {"class": "text-start ps-4"}, "text": "标题"},
                            {"component": "th", "props": {"class": "text-start ps-4"}, "text": "年份"},
                            {"component": "th", "props": {"class": "text-start ps-4"}, "text": "类型"},
                            {"component": "th", "props": {"class": "text-start ps-4"}, "text": "状态"},
                            {"component": "th", "props": {"class": "text-start ps-4"}, "text": "Trakt 评分"},
                            {"component": "th", "props": {"class": "text-start ps-4"}, "text": "豆瓣评分"},
                            {"component": "th", "props": {"class": "text-start ps-4"}, "text": "同步时间"},
                            {"component": "th", "props": {"class": "text-start ps-4"}, "text": "豆瓣 ID"},
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
                                        "text": item.get("title", "未知"),
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": str(item.get("year", "-")),
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": item.get("media_type") or "-",
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "content": [
                                            {
                                                "component": "VChip",
                                                "props": {
                                                    "size": "small",
                                                    "color": "success" if item.get("status") == "看完" else "primary",
                                                    "variant": "flat",
                                                },
                                                "text": item.get("status", "在看"),
                                            }
                                        ],
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": str(item.get("trakt_rating", "-")) if item.get("status") == "看完" else "-",
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": str(item.get("douban_rating", "-")) if item.get("status") == "看完" else "-",
                                    },
                                    {
                                        "component": "td",
                                        "props": {"class": "text-start ps-4"},
                                        "text": (
                                            datetime.fromtimestamp(item["sync_time"]).strftime("%Y-%m-%d %H:%M")
                                            if item.get("sync_time") else "-"
                                        ),
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
                                        ] if item.get("douban_id") else [],
                                        "text": "-" if not item.get("douban_id") else None,
                                    },
                                ],
                            }
                            for idx, item in enumerate(history_list)
                        ],
                    },
                ],
            }
        ]
        return trakt_section + weread_section + netease_section + xiaoyuzhou_section
