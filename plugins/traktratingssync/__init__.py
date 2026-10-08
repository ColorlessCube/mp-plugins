# -*- coding: utf-8 -*-
"""
豆瓣书影音同步插件

插件定义、配置读取和任务调度入口。
所有业务逻辑分别由对应 helper 实现：
  - TraktHelper      → Trakt API 封装（评分拉取、播放进度、OAuth 授权）
  - DoubanHelper     → 豆瓣 Cookie 操作（标记看过/在看、写入评分）
  - WereadHelper     → 微信读书 Skill API（书架、阅读进度）
  - NeteaseHelper     → 网易云音乐 Cookie API（最近播放记录，按专辑聚合）
  - XiaoyuzhouHelper → 小宇宙 FM API（播客听取历史）
"""
import hashlib
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse, urlunparse

from fastapi import Depends, HTTPException, Request
from starlette.responses import Response as HttpResponse

from app.core.security import verify_resource_token
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import Response as ApiResponse, TokenPayload
from app.schemas.types import MediaType
from app.utils.http import RequestUtils
from .douban_helper import DoubanHelper
from .netease_helper import NeteaseHelper
from .trakt_helper import TraktHelper
from .weread_helper import WereadHelper
from .xiaoyuzhou_helper import XiaoyuzhouHelper


class TraktRatingsSync(_PluginBase):
    """豆瓣书影音同步插件入口，负责配置、调度和多平台同步编排。"""

    plugin_name = "豆瓣书影音同步"
    plugin_desc = "聚合多平台记录同步到豆瓣：Trakt 电影 →「看过」及评分，Trakt 剧集播放进度 →「在看」，微信读书书架 → 阅读记录，网易云音乐 → 「听过」专辑，小宇宙播客 → 「听过」。"
    plugin_icon = "trakt.png"
    plugin_version = "3.17.0"
    plugin_author = "ColorlessCube"
    author_url = "https://github.com/ColorlessCube"
    plugin_config_prefix = "trakt_ratings_sync_"
    plugin_order = 16
    auth_level = 1

    _enable: bool = False
    _trakt_username: str = ""
    _trakt_client_id: str = ""
    _trakt_access_token: str = ""
    _trakt_redirect_uri: str = ""
    _trakt_authorization_url: str = ""
    _trakt_auth_message: str = ""
    _trakt_manual_mappings: str = ""
    _douban_cookie: str = ""
    _douban_write_limit: int = 10
    _douban_write_interval: int = 10
    _run_lock = Lock()
    _weread_api_key: str = ""
    _weread_limit: int = 20
    _netease_cookie: str = ""
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
    _notification_mode: str = "changes"
    _notification_channel: str = "moviepilot"
    _NOTIFY_COOLDOWN = 6 * 60 * 60
    _NOTIFY_RETRY_INTERVAL = 5 * 60
    _trakt_callback_path = "/api/v1/plugin/TraktRatingsSync/oauth/callback"

    # helper 实例（延迟初始化）
    _douban_helper: Optional[DoubanHelper] = None
    _trakt_helper: Optional[TraktHelper] = None
    _weread_helper: Optional[WereadHelper] = None
    _netease_helper: Optional[NeteaseHelper] = None
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
        self._trakt_access_token = (config.get("trakt_access_token") or (self.get_data("trakt_token") or {}).get("access_token") or "").strip()
        self._trakt_redirect_uri = (config.get("trakt_redirect_uri") or "").strip()
        auth_status = self.get_data("trakt_auth_status") or {}
        self._trakt_authorization_url = auth_status.get("url") or config.get("trakt_authorization_url") or ""
        self._trakt_auth_message = auth_status.get("message") or config.get("trakt_auth_message") or ""
        token_data = dict(self.get_data("trakt_token") or {})
        if self._trakt_access_token and not token_data.get("access_token"):
            token_data["access_token"] = self._trakt_access_token
            self.save_data("trakt_token", token_data)
        self._trakt_manual_mappings = (config.get("trakt_manual_mappings") or "").strip()
        self._douban_cookie = (config.get("douban_cookie") or "").strip()
        self._douban_write_limit = max(1, int(config.get("douban_write_limit") or 10))
        self._douban_write_interval = max(5, int(config.get("douban_write_interval") or 10))
        self._weread_api_key = (config.get("weread_api_key") or "").strip()
        self._weread_limit = int(config.get("weread_limit") or 20)
        self._netease_cookie = (config.get("netease_cookie") or "").strip()
        self._netease_limit = int(config.get("netease_limit") or 20)
        self._xiaoyuzhou_cookie = (config.get("xiaoyuzhou_cookie") or "").strip()
        self._xiaoyuzhou_limit = int(config.get("xiaoyuzhou_limit") or 20)
        self._private = config.get("private", True)
        self._sync_type = config.get("sync_type", "all") or "all"
        self._max_sync_count = int(config.get("max_sync_count") or 0)
        self._trakt_history_limit = int(config.get("trakt_history_limit") or 20)
        history_days = config.get("trakt_history_days", 30)
        self._trakt_history_days = int(history_days) if history_days not in (None, "") else 30
        self._cron = config.get("cron", "0 2 * * *") or "0 2 * * *"
        self._bark_webhook_url = (config.get("bark_webhook_url") or "").strip()
        self._notification_mode = config.get("notification_mode") or "changes"
        self._notification_channel = config.get("notification_channel") or ("bark" if self._bark_webhook_url else "moviepilot")
        self._source_issue_this_run = set()
        self._source_fetch_ok = set()

        # 重置 helper，下次 run() 时重新初始化
        self._douban_helper = None
        self._trakt_helper = None
        self._weread_helper = None
        self._netease_helper = None
        self._xiaoyuzhou_helper = None

        self._init_trakt_authorization(config)
        # 清理旧配置同时迁移短期授权显示信息，现有令牌和业务设置继续保留。
        self._merge_update_config({})

    def _create_trakt_helper(self) -> TraktHelper:
        """以当前配置创建 Trakt Helper，统一授权和同步入口的凭据来源。"""
        return TraktHelper(
            client_id=self._trakt_client_id,
            access_token=self._trakt_access_token,
            username=self._trakt_username,
            save_data_fn=self.save_data,
            get_data_fn=self.get_data,
            update_config_fn=self._merge_update_config,
            send_notification_fn=lambda title, body: self._notify_issue("Trakt", title, body),
            manual_mappings=self._parse_trakt_manual_mappings(),
            redirect_uri=self._trakt_redirect_uri,
        )

    def _init_trakt_authorization(self, config: Dict[str, Any]) -> None:
        """更换应用时清除对应令牌；加载配置不再执行任何一次性授权操作。"""
        bound_client = self.get_data("trakt_auth_client_id")
        token_client = (self.get_data("trakt_token") or {}).get("client_id")
        previous_client = bound_client if bound_client is not None else token_client
        pending = self.get_data("trakt_pkce_pending") or {}
        if previous_client is not None and previous_client != self._trakt_client_id:
            self._create_trakt_helper().reset_authorization()
            self._trakt_auth_message = "Trakt 应用已更换，请重新授权；同步记录已保留"
        elif pending and (pending.get("redirect_uri") != self._trakt_redirect_uri
                          or int(pending.get("expires_at") or 0) <= int(time.time())):
            self.save_data("trakt_pkce_pending", {})
            self._trakt_authorization_url = ""
            self._trakt_auth_message = "授权请求已过期或回跳地址已变化，请重新生成授权链接"
        self.save_data("trakt_auth_client_id", self._trakt_client_id)

    def run(self) -> None:
        """串行执行同步，阻止定时与手动入口同时写入豆瓣。"""
        if not self._enable:
            return
        if not self._run_lock.acquire(blocking=False):
            logger.warning("豆瓣同步任务正在运行，跳过本次重复触发")
            return
        started_at = int(time.time())
        self.save_data("last_run", {"started_at": started_at, "status": "running"})
        try:
            self._run_sync()
        except Exception:
            self.save_data("last_run", {"started_at": started_at, "finished_at": int(time.time()), "status": "failed"})
            raise
        finally:
            self._run_lock.release()

    def _run_sync(self) -> None:
        """定时/手动触发入口：依次执行 Trakt 同步、微信读书同步、网易云音乐同步、小宇宙播客同步。"""
        if not self._enable:
            logger.debug("豆瓣书影音同步插件未启用，跳过")
            return

        logger.info("开始豆瓣书影音同步（统一写入上限 %d，间隔 %d–%d 秒）", self._douban_write_limit,
                    self._douban_write_interval, self._douban_write_interval * 2)
        self._source_issue_this_run = set()
        self._source_fetch_ok = set()
        # 各来源共享请求预算与待处理队列，验证后不再进入下一个平台。
        try:
            self._douban_helper = DoubanHelper(
                user_cookie=self._douban_cookie or None,
                notify_fn=lambda title, body: self._notify_issue("豆瓣", title, body),
                save_data_fn=self.save_data,
                get_data_fn=self.get_data,
                write_limit=self._douban_write_limit,
                write_interval=self._douban_write_interval,
            )
        except Exception as e:
            logger.error(f"初始化豆瓣 Helper 失败：{type(e).__name__}")
            last_run = self.get_data("last_run") or {}
            self.save_data("last_run", {**last_run, "status": "failed", "finished_at": int(time.time())})
            self._notify_issue("豆瓣", "豆瓣同步初始化失败", "请检查豆瓣配置及插件日志；本轮未执行写入。")
            return

        sources = (
            ("Trakt", self._trakt_client_id, self._sync_trakt),
            ("微信读书", self._weread_api_key, self._sync_weread),
            ("网易云音乐", self._has_netease_source(), self._sync_netease),
            ("小宇宙", self._xiaoyuzhou_cookie, self._sync_xiaoyuzhou),
        )
        for source_name, enabled, sync in sources:
            if self._douban_helper.requests_paused:
                break
            if not enabled:
                continue
            try:
                sync()
            except Exception as e:
                logger.error(f"{source_name}同步异常：{type(e).__name__}")
                self._notify_issue(source_name, f"{source_name}同步异常", "本轮未能完成该平台处理，其他平台继续执行；请查看插件日志。")
            if source_name in self._source_fetch_ok and source_name not in self._source_issue_this_run:
                self._resolve_issue(source_name)
        self._douban_helper.flush_pending()
        summary = self._douban_helper.get_sync_summary()
        logger.info("豆瓣写入汇总: 新增成功 %d，状态未变跳过 %d，提交失败 %d，待处理 %d，暂停 %s",
                    summary["written"], summary["skipped"], summary["failed"], summary["pending"], summary["paused"])
        last_run = self.get_data("last_run") or {}
        last_run.update({**summary, "finished_at": int(time.time()),
                         "status": "paused" if summary["paused"] else "partial" if summary["failed"] or self._source_issue_this_run else "completed",
                         "source_errors": sorted(self._source_issue_this_run)})
        self.save_data("last_run", last_run)
        if not summary["paused"] and getattr(self._douban_helper, "is_authenticated", False):
            self._resolve_issue("豆瓣")
        if summary["written"] and self._notification_mode == "changes":
            self._send_notification("豆瓣同步完成", f"成功写入 {summary['written']} 条，跳过未变化 {summary['skipped']} 条，"
                                    f"待处理 {summary['pending']} 条，提交失败 {summary['failed']} 条。详情见插件页面。")
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
        self._trakt_helper = self._create_trakt_helper()

        # 同步 Trakt 评分 → 豆瓣看过
        try:
            self._sync_ratings()
        except Exception as e:
            logger.error("同步 Trakt 评分到豆瓣失败: %s", e, exc_info=True)
            self._notify_issue("Trakt", "Trakt同步异常", "本轮部分影视记录未完成，请查看插件日志。")

        if self._douban_helper.requests_paused:
            return
        # 同步 Trakt 播放进度 → 豆瓣在看
        try:
            self._sync_progress()
        except Exception as e:
            logger.error("同步 Trakt 观看进度到豆瓣失败: %s", e, exc_info=True)
            self._notify_issue("Trakt", "Trakt同步异常", "本轮部分影视记录未完成，请查看插件日志。")

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
            if self._douban_helper.requests_paused:
                break
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
        logger.info("Trakt 评分同步完成: 已处理 %d，未完成 %d（包括延期；实际写入见豆瓣汇总）", success_count, fail_count)

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

        if episodes is None or recent_shows is None:
            logger.warning("Trakt 播放进度或观看历史拉取失败，保留已有在看记录，跳过本次剧集同步")
            self._notify_issue("Trakt", "Trakt记录读取未完成", "请检查网络及Trakt应用状态；原有同步记录保留。")
            return

        self._mark_source_success("Trakt")
        # 豆瓣无法将电影设置为在看，仅同步剧集
        logger.info("获取到 %d 条 Trakt 剧集播放进度", len(episodes))

        watching: Dict[str, Any] = self.get_data("watching") or {}
        success_count = 0

        candidates: Dict[str, dict] = {}
        for e in episodes:
            show = e.get("show") or {}
            ids = show.get("ids") or {}
            key = ids.get("trakt") or ids.get("tmdb") or ids.get("imdb") or ids.get("slug")
            progress = e.get("progress")
            if not key or not isinstance(progress, (int, float)) or not 10 <= progress < 100:
                continue
            key = str(key)
            if key not in candidates or progress > candidates[key]["progress"]:
                candidates[key] = {"progress": progress, "show": show}
        for history_item in recent_shows:
            show = history_item.get("show") or {}
            ids = show.get("ids") or {}
            key = ids.get("trakt") or ids.get("tmdb") or ids.get("imdb") or ids.get("slug")
            if key:
                candidates.setdefault(str(key), {"progress": "history", "show": show})
        logger.info("Trakt 在看记录去重后共 %d 个剧集", len(candidates))
        for item in candidates.values():
            if self._douban_helper.requests_paused:
                break
            try:
                if self._trakt_helper.sync_one_progress(
                    item,
                    "show",
                    MediaType.TV,
                    watching,
                    self._douban_helper,
                    self._private,
                ):
                    success_count += 1
            except Exception as ex:
                logger.error("同步播放进度失败: %s", ex, exc_info=True)

        if not episodes and not recent_shows:
            logger.info("Trakt 剧集播放进度和最近观看历史均为空，无需同步在看")
            self.save_data("watching", {})
            return

        self.save_data("watching", watching)
        logger.info("Trakt 剧集在看同步完成: 已处理 %d 个剧集（实际写入见豆瓣汇总）", success_count)

    def _fetch_trakt_progress_sources(
        self,
        access_token: str,
    ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]]]:
        """拉取 Trakt 剧集来源，使用 None 区分请求失败与成功空列表。"""
        self._trakt_helper.reset_oauth_unauthorized()
        episodes = self._trakt_helper.fetch_playback("/sync/playback/episodes", access_token)
        history_items = self._trakt_helper.fetch_history(
            media_type="shows",
            access_token=access_token,
            limit=self._trakt_history_limit,
        )
        if history_items is None:
            return episodes, None
        recent_shows = self._extract_recent_history_shows(history_items)
        logger.info(f"获取到 {len(history_items)} 条 Trakt 剧集观看历史，提取 {len(recent_shows)} 个最近在看剧集")
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

        self._mark_source_success("微信读书")
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
            if self._douban_helper.requests_paused:
                break
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
                self._douban_helper.record_unchanged()
                continue

            # ── 提交到豆瓣 ────────────────────────────────────────────────
            if hasattr(self._douban_helper, "set_target_context"):
                self._douban_helper.set_target_context(subject_id, "book.douban.com", douban_title or title, "微信读书")
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
                logger.info("豆瓣图书尚未完成（含额度延期），已保留待处理: %s", title)
                fail_count += 1

        # 持久化两份缓存
        self.save_data("weread_synced", synced)
        self.save_data("weread_book_id_map", book_id_map)
        logger.info(
            "微信读书同步完成: 成功 %d，跳过 %d（已同步），未完成/未匹配 %d（含额度延期；实际失败见写入汇总）",
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
            logger.debug("未配置网易云音乐 Cookie，跳过同步")
            return

        netease_helper = self._get_netease_helper()
        logger.info("开始同步网易云音乐最近听歌记录到豆瓣（Cookie）...")

        if not netease_helper:
            logger.warning("网易云音乐 Helper 初始化失败，跳过同步")
            return

        albums = netease_helper.get_recent_albums(limit=self._netease_limit)
        if not albums:
            logger.info("网易云音乐未获取到最近专辑记录（Cookie 可能失效或暂无听歌记录）")
            return

        self._mark_source_success("网易云音乐")
        # 已同步缓存（key = 豆瓣 subject_id，避免重复提交）
        synced: Dict[str, Any] = self.get_data("netease_albums") or {}

        # 「专辑名+艺术家」→ douban_subject_id 映射缓存（避免重复搜索豆瓣）
        # 结构：{ "专辑名\tartist": { "subject_id": str, "douban_title": str } }
        album_map: Dict[str, Any] = self.get_data("netease_album_map") or {}

        success_count = 0
        skip_count = 0
        fail_count = 0

        for album_info in albums:
            if self._douban_helper.requests_paused:
                break
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
                self._douban_helper.record_unchanged()
                continue

            # ── 提交「听过」状态 ──────────────────────────────────────────
            if hasattr(self._douban_helper, "set_target_context"):
                self._douban_helper.set_target_context(subject_id, "music.douban.com", douban_title or album_name, "网易云音乐")
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
                logger.info(f"豆瓣音乐尚未完成（含额度延期），已保留待处理：{artist} - {album_name}")
                fail_count += 1

        # 持久化两份缓存
        self.save_data("netease_albums", synced)
        self.save_data("netease_album_map", album_map)
        logger.info(
            "网易云音乐同步完成: 成功 %d，跳过 %d（已同步），未完成/未匹配 %d（含额度延期；实际失败见写入汇总）",
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
                notify_fn=lambda title, body: self._notify_issue("小宇宙", title, body),
            )

        logger.info("开始同步小宇宙播客最近听取记录到豆瓣...")

        episodes = self._xiaoyuzhou_helper.get_recent_episodes(limit=self._xiaoyuzhou_limit)
        self._persist_xiaoyuzhou_cookie_if_updated()
        if not episodes:
            logger.info("小宇宙未获取到最近听取记录（Cookie 可能已失效或暂无听取记录）")
            return

        self._mark_source_success("小宇宙")
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
            if self._douban_helper.requests_paused:
                break
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
                    self._douban_helper.record_unchanged()
                    continue
                # 状态有变化（例如从「在听」升级为「听过」），继续提交
                logger.info(
                    "播客状态变更 %s → %s，重新提交: %s (id=%s)",
                    cached_status, target_status, douban_title or podcast_name, subject_id,
                )

            # ── 提交豆瓣状态 ─────────────────────────────────────────────
            if hasattr(self._douban_helper, "set_target_context"):
                self._douban_helper.set_target_context(subject_id, "www.douban.com", douban_title or podcast_name, "小宇宙")
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
                logger.info("豆瓣播客尚未完成（含额度延期），已保留待处理: %s", podcast_name)
                fail_count += 1

        # 持久化两份缓存
        self.save_data("xiaoyuzhou_podcasts", synced)
        self.save_data("xiaoyuzhou_podcast_map", podcast_map)
        logger.info(
            "小宇宙播客同步完成: 成功 %d，跳过 %d（已同步且状态无变化），未完成/未匹配 %d（含额度延期；实际失败见写入汇总）",
            success_count, skip_count, fail_count,
        )

# ------------------------------------------------------------------
# 辅助工具
# ------------------------------------------------------------------

    def _has_netease_source(self) -> bool:
        """判断网易云音乐是否存在可用数据源配置。"""
        return bool(self._netease_cookie)

    def _get_netease_helper(self) -> Optional[NeteaseHelper]:
        """创建或复用网易云 Cookie Helper。"""
        if not self._netease_cookie:
            return None
        if not self._netease_helper:
            self._netease_helper = NeteaseHelper(
                cookies=self._netease_cookie,
                notify_fn=self._send_netease_cookie_auth_notification,
            )
        return self._netease_helper

    def _merge_update_config(self, patch: Dict[str, Any]) -> None:
        """将 patch 合并到当前配置后调用 update_config（避免覆盖其他字段）。"""
        current = {
            "enable": self._enable,
            "trakt_username": self._trakt_username,
            "trakt_client_id": self._trakt_client_id,
            "trakt_redirect_uri": self._trakt_redirect_uri,
            "trakt_manual_mappings": self._trakt_manual_mappings,
            "douban_cookie": self._douban_cookie,
            "douban_write_limit": self._douban_write_limit,
            "douban_write_interval": self._douban_write_interval,
            "weread_api_key": self._weread_api_key,
            "weread_limit": self._weread_limit,
            "netease_cookie": self._netease_cookie,
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
        self._trakt_authorization_url = patch.get("trakt_authorization_url", self._trakt_authorization_url)
        self._trakt_auth_message = patch.get("trakt_auth_message", self._trakt_auth_message)
        self.save_data("trakt_auth_status", {"url": self._trakt_authorization_url, "message": self._trakt_auth_message})
        current["notification_mode"] = self._notification_mode
        current["notification_channel"] = self._notification_channel
        current.update({key: value for key, value in patch.items() if key in current})
        self._trakt_access_token = patch.get("trakt_access_token", self._trakt_access_token) or ""
        self._trakt_manual_mappings = current.get("trakt_manual_mappings") or ""
        self._netease_cookie = current.get("netease_cookie") or ""
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
                "",
            )
            return False
        except Exception as e:
            logger.error(f"Bark 通知发送异常：{type(e).__name__}")
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

    def _send_notification(self, title: str, content: str) -> bool:
        """按单一通道投递通知，默认只发送数量和必要操作。"""
        if self._notification_mode == "off":
            return False
        if self._notification_channel == "bark":
            return self._send_bark_notification(title, content)
        try:
            self.post_message(title=title, text=content)
            return True
        except Exception as error:
            logger.warning(f"MoviePilot 通知投递异常：{type(error).__name__}")
            return False

    def _mark_source_success(self, source: str) -> None:
        """只有读取到有效数据时才确认来源恢复，保留独立测试调用的兼容性。"""
        self._source_fetch_ok = getattr(self, "_source_fetch_ok", set())
        self._source_fetch_ok.add(source)

    def _notify_issue(self, source: str, title: str, content: str) -> bool:
        """按平台持久化异常，通知成功后才进入冷却，失败保留后续重试。"""
        credentials = {"Trakt": self._trakt_client_id, "豆瓣": self._douban_cookie,
                       "微信读书": self._weread_api_key, "网易云音乐": self._netease_cookie,
                       "小宇宙": self._xiaoyuzhou_cookie}
        fingerprint = hashlib.sha256((credentials.get(source) or "").encode()).hexdigest()[:16]
        issues = dict(self.get_data("notification_issues") or {})
        previous = issues.get(source) or {}
        now = int(time.time())
        self._source_issue_this_run = getattr(self, "_source_issue_this_run", set())
        self._source_issue_this_run.add(source)
        event_fingerprint = hashlib.sha256((title + (content if source == "豆瓣" else "")).encode()).hexdigest()[:16]
        same = (previous.get("fingerprint") == fingerprint and previous.get("event_fingerprint") == event_fingerprint
                and previous.get("active"))
        state = dict(previous) if same else {"fingerprint": fingerprint, "last_success_at": 0, "last_attempt_at": 0}
        state.update({"active": True, "title": title, "event_fingerprint": event_fingerprint, "updated_at": now, "recovery_pending": False})
        issues[source] = state
        self.save_data("notification_issues", issues)
        if self._notification_mode == "off":
            return False
        if now - state.get("last_success_at", 0) < self._NOTIFY_COOLDOWN:
            return False
        if now - state.get("last_attempt_at", 0) < self._NOTIFY_RETRY_INTERVAL:
            return False
        actions = {"Trakt": "请确认Trakt后台应用仍有效，再在插件配置页检查Client ID和授权状态。",
                   "微信读书": "请在插件配置页更新微信读书 API Key。",
                   "网易云音乐": "请在正常浏览器登录网易云，再更新插件 Cookie。",
                   "小宇宙": "请在插件配置页更新小宇宙认证信息。"}
        body = content if source == "豆瓣" else actions.get(source, "请查看插件详情与日志。")
        state["last_attempt_at"] = now
        delivered = self._send_notification(title, body)
        if delivered:
            state["last_success_at"] = now
        state["delivery_pending"] = not delivered
        self.save_data("notification_issues", issues)
        return delivered

    def _resolve_issue(self, source: str) -> None:
        """确认有效来源数据后清除异常，只为已投递的故障发送一次恢复消息。"""
        issues = dict(self.get_data("notification_issues") or {})
        state = dict(issues.get(source) or {})
        if not state.get("active") and not state.get("recovery_pending"):
            return
        now = int(time.time())
        recovery_pending = False
        if state.get("last_success_at") and self._notification_mode != "off":
            if state.get("recovery_pending") and now - state.get("recovery_attempt_at", 0) < self._NOTIFY_RETRY_INTERVAL:
                recovery_pending = True
            else:
                state["recovery_attempt_at"] = now
                recovery_pending = not self._send_notification(f"{source}同步已恢复", "本次检查通过，后续将按原定时任务继续处理。")
        state.update({"active": False, "delivery_pending": False, "recovery_pending": recovery_pending, "resolved_at": now})
        issues[source] = state
        self.save_data("notification_issues", issues)

    def _send_netease_cookie_auth_notification(self, title: str, content: str) -> bool:
        """将网易云授权异常交给统一通知规则。"""
        return self._notify_issue("网易云音乐", title, content)

    def _send_weread_auth_notification(self, title: str, content: str) -> bool:
        """将微信读书授权异常交给统一通知规则。"""
        return self._notify_issue("微信读书", title, content)

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
            {"path": "/oauth/start", "endpoint": self._api_trakt_start, "methods": ["POST"],
             "summary": "生成 Trakt PKCE 授权链接", "response_model": ApiResponse,
             "allow_anonymous": True, "dependencies": [Depends(self._verify_trakt_callback_admin)]},
            {"path": "/douban/resume", "endpoint": self._api_douban_resume, "methods": ["POST"],
             "summary": "完成验证后恢复豆瓣同步", "response_model": ApiResponse,
             "allow_anonymous": True, "dependencies": [Depends(self._verify_trakt_callback_admin)]},
            {
                "path": "/sync",
                "endpoint": self._api_sync,
                "methods": ["GET", "POST"],
                "summary": "手动执行同步",
                "description": "立即执行一次 Trakt 评分同步到豆瓣",
                "response_model": ApiResponse,
            },
            {
                "path": "/oauth/callback",
                "endpoint": self._api_trakt_callback,
                "methods": ["GET"],
                "summary": "接收 Trakt PKCE 授权结果",
                "description": "使用 MoviePilot 管理员资源 Cookie 和一次性 PKCE 请求完成授权，不触发同步",
                "response_model": ApiResponse,
                # 浏览器回跳无法携带 API key；改用已有资源 Cookie 依赖，仍要求管理员身份。
                "allow_anonymous": True,
                "dependencies": [Depends(self._verify_trakt_callback_admin)],
            },
        ]

    @staticmethod
    def _verify_trakt_callback_admin(payload: TokenPayload = Depends(verify_resource_token)) -> TokenPayload:
        """通过现有资源 Cookie 依赖验证回跳浏览器，限制为 MoviePilot 管理员。"""
        if not payload.super_user:
            raise HTTPException(status_code=403, detail="请在同一浏览器登录 MoviePilot 管理员后完成 Trakt 授权")
        return payload

    def _api_trakt_callback(self, request: Request, response: HttpResponse) -> ApiResponse:
        """接收浏览器回跳并交换令牌，复用 Helper 的一次性 state 和 PKCE 校验。"""
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        if urlparse(self._trakt_redirect_uri).path != self._trakt_callback_path:
            return ApiResponse(success=False, message="请将 Trakt 和插件的回跳地址设为插件真实的 OAuth 回跳接口，再重新生成授权链接")
        try:
            # 代理后的 Request URL 可能是内网 HTTP 地址；令牌交换必须使用登记的 HTTPS 地址。
            callback_url = f"{self._trakt_redirect_uri}?{request.url.query}"
            authorized = self._create_trakt_helper().complete_pkce_authorization(callback_url)
            message = "Trakt 授权成功，令牌已自动保存；请返回 MoviePilot 插件配置页确认" if authorized else "Trakt 令牌交换失败，请查看插件日志并重新生成授权链接"
        except ValueError as error:
            authorized = False
            message = str(error)
        except Exception as error:
            authorized = False
            message = "Trakt 授权处理失败，请重新生成授权链接"
            logger.warning(f"Trakt 回跳处理异常：{type(error).__name__}")
        self._merge_update_config({"trakt_auth_message": message})
        if authorized:
            logger.info("Trakt PKCE 回跳授权成功，令牌已保存")
        else:
            logger.warning(f"Trakt PKCE 回跳未完成：{message}")
        return ApiResponse(success=authorized, message=message)

    def _api_trakt_start(self, response: HttpResponse = None) -> ApiResponse:
        """使用已经保存的应用配置生成授权链接，不启动同步或设备码轮询。"""
        if response is not None:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        if not self._run_lock.acquire(blocking=False):
            return ApiResponse(success=False, message="同步任务正在运行，请结束后再重新授权")
        try:
            if urlparse(self._trakt_redirect_uri).path != self._trakt_callback_path:
                return ApiResponse(success=False, message="请先保存正确的插件 HTTPS 回跳地址，再生成授权链接")
            try:
                self._create_trakt_helper().begin_pkce_authorization()
            except ValueError as error:
                return ApiResponse(success=False, message=str(error))
            self._merge_update_config({"trakt_auth_message": "链接已生成，有效期10分钟；请点击打开授权页面"})
            return ApiResponse(success=True, message="链接已生成，请点击打开授权页面；现有令牌和同步记录已保留",
                               data={"authorization_url": self._trakt_authorization_url})
        finally:
            self._run_lock.release()

    def _api_douban_resume(self) -> ApiResponse:
        """完成浏览器验证后解除人工暂停，保留待处理队列和服务端冷却。"""
        if not self._run_lock.acquire(blocking=False):
            return ApiResponse(success=False, message="同步任务正在运行，请稍后恢复")
        try:
            state = dict(self.get_data("douban_sync_state") or {})
            if state.get("blocked_until", 0) > time.time():
                return ApiResponse(success=False, message="豆瓣仍在冷却期，冷却结束后由定时任务继续处理")
            state.pop("requires_verification", None)
            state.pop("reason", None)
            self.save_data("douban_sync_state", state)
            return ApiResponse(success=True, message="已解除人工暂停，待处理记录保留；下次定时任务继续处理")
        finally:
            self._run_lock.release()

    def _api_sync(self) -> ApiResponse:
        """手动触发同步（API 端点）。"""
        try:
            self.run()
            return ApiResponse(success=True, message="同步任务已执行")
        except Exception as e:
            logger.error("手动同步失败: %s", e, exc_info=True)
            return ApiResponse(success=False, message="同步执行异常，请查看插件日志")

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

    @staticmethod
    def _action_button(label: str, path: str, disabled: bool = False) -> dict:
        """构建由管理员资源 Cookie 鉴权的原生插件操作按钮。"""
        return {"component": "VBtn", "text": label, "props": {"color": "primary", "variant": "tonal", "disabled": disabled},
                "events": {"click": {"api": f"plugin/TraktRatingsSync/{path}", "method": "post"}}}

    @staticmethod
    def _fold(title: str, content: List[dict]) -> dict:
        """使用原生折叠面板减少默认页面长度，保留键盘可操作的标题。"""
        return {"component": "VExpansionPanels", "props": {"variant": "accordion", "class": "my-3"}, "content": [
            {"component": "VExpansionPanel", "content": [
                {"component": "VExpansionPanelTitle", "text": title},
                {"component": "VExpansionPanelText", "content": content},
            ]},
        ]}

    @staticmethod
    def _config_action_button(label: str, path: str, message_model: str, show: Optional[str] = None) -> dict:
        """使用宿主表单的onClick契约调用同源管理员接口，并立即更新显示状态。"""
        update = ("model._ui_trakt_url = result.data.authorization_url;" if path == "oauth/start"
                  else "model._ui_douban_paused = false;")
        handler = f"""async function(event) {{
            if (model._ui_busy) return;
            model._ui_busy = true;
            try {{
                const response = await fetch('/api/v1/plugin/TraktRatingsSync/{path}', {{method: 'POST', credentials: 'same-origin'}});
                const result = await response.json();
                model.{message_model} = result.message || (result.detail ? '操作未完成，请确认已登录MoviePilot管理员' : '操作未完成');
                if (response.ok && result.success) {{ {update} }}
            }} catch(error) {{
                model.{message_model} = '请求未完成，请检查网络后重试';
            }} finally {{ model._ui_busy = false; }}
        }}"""
        props = {"color": "primary", "variant": "tonal", "disabled": "{{ _ui_busy }}", "onClick": handler}
        if show:
            props["show"] = show
        return {"component": "VBtn", "text": label, "props": props}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回精简配置，只支持 PKCE；授权和恢复同步通过按钮执行。"""
        def field(model: str, label: str, **props) -> dict:
            return {"component": "VTextField", "props": {"model": model, "label": label, **props}}

        def row(*components: dict) -> dict:
            return {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [item]} for item in components]}

        def select(model: str, label: str, options: List[Tuple[str, str]]) -> dict:
            return {"component": "VSelect", "props": {"model": model, "label": label, "items": [{"title": title, "value": value} for title, value in options]}}

        def heading(title: str) -> dict:
            return {"component": "div", "text": title, "props": {"class": "text-subtitle-1 font-weight-medium mt-4 mb-2"}}

        def credential(model: str, label: str, hint: str, **props) -> dict:
            return field(model, label, type="password", autocomplete="off", hint=hint, **{"persistent-hint": True}, **props)

        token = self.get_data("trakt_token") or {}
        pending = self.get_data("trakt_pkce_pending") or {}
        url = self._trakt_authorization_url if pending.get("expires_at", 0) > time.time() else ""
        authorized = bool(token.get("access_token") or self._trakt_access_token)
        auth_text = self._trakt_auth_message if url else "已保存 Trakt 授权，令牌会自动续期" if authorized else self._trakt_auth_message or "尚未授权：保存 Client ID 与回跳地址后，点击生成授权链接"
        if pending and not url and not authorized:
            auth_text = "上次授权链接已过期，请重新生成并在10分钟内完成授权"
        trakt = [
            row(field("trakt_username", "Trakt 用户名", hint="用于读取公开评分，仍需保留"), field("trakt_client_id", "Trakt Client ID")),
            field("trakt_redirect_uri", "Trakt HTTPS 回跳地址", hint=f"填写 MoviePilot 的 HTTPS 域名 + {self._trakt_callback_path}，并与 Trakt 后台一致", **{"persistent-hint": True}),
            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "{{ _ui_trakt_message }}"}},
            {"component": "div", "props": {"class": "d-flex flex-wrap ga-3 my-3"}, "content": [self._config_action_button("重新授权" if authorized else "生成授权链接", "oauth/start", "_ui_trakt_message")]},
            {"component": "div", "props": {"class": "text-caption text-medium-emphasis"}, "text": "按钮使用已保存的配置；修改应用信息后请先保存。链接会立即显示，请在10分钟内完成授权。"},
            {"component": "VBtn", "text": "打开 Trakt 授权页面", "props": {"href": "{{ _ui_trakt_url }}", "show": "{{ !!_ui_trakt_url }}", "target": "_blank", "rel": "noreferrer", "color": "primary", "class": "my-3"}},
        ]
        state = self.get_data("douban_sync_state") or {}
        douban = [credential("douban_cookie", "豆瓣 Cookie", "支持 Cookie 字符串或含 Cookie 的完整 cURL")]
        if state.get("requires_verification"):
            douban.extend([
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "{{ _ui_douban_message }}"}},
                self._config_action_button("已完成验证，恢复同步", "douban/resume", "_ui_douban_message", "{{ _ui_douban_paused }}"),
            ])
        advanced = [
            heading("豆瓣请求节制"), row(field("douban_write_limit", "所有平台每轮写入额度", type="number", min=1, hint="默认10次，失败请求也占额度；超额记录自动延期"),
                                         field("douban_write_interval", "写入最小间隔（秒）", type="number", min=5, hint="默认10秒，实际间隔10–20秒")),
            heading("Trakt 读取范围"), row(select("sync_type", "Trakt 影视同步范围", [("电影和剧集", "all"), ("仅电影", "movies"), ("仅剧集", "shows")]),
                                         field("max_sync_count", "Trakt 最近评分读取上限", type="number", min=0, hint="0表示不限制；不影响其他平台及豆瓣写入额度")),
            row(field("trakt_history_limit", "剧集观看历史读取条数", type="number", min=1), field("trakt_history_days", "剧集历史范围（天）", type="number", min=0, hint="0表示不限天数")),
            {"component": "VTextarea", "props": {"model": "trakt_manual_mappings", "label": "Trakt → 豆瓣手动映射", "rows": 2, "placeholder": "imdb:tt1234567=12345678", "auto-grow": True}},
            heading("各平台读取数量"), row(field("weread_limit", "微信读书读取本数", type="number", min=1), field("netease_limit", "网易云读取专辑数", type="number", min=1)),
            field("xiaoyuzhou_limit", "小宇宙读取单集数", type="number", min=1),
        ]
        form = [{"component": "VForm", "content": [
            heading("基础设置"), row({"component": "VSwitch", "props": {"model": "enable", "label": "启用插件"}},
                                       {"component": "VSwitch", "props": {"model": "private", "label": "豆瓣记录仅自己可见"}}),
            field("cron", "定时同步", hint="五段cron表达式，例如0 10 * * *代表每天上午10点", **{"persistent-hint": True}),
            heading("豆瓣"), *douban,
            self._fold("Trakt", trakt),
            self._fold("微信读书", [credential("weread_api_key", "微信读书 API Key", "仅支持现有 Skill API Key")]),
            self._fold("网易云音乐", [credential("netease_cookie", "网易云 Cookie", "支持 Cookie 字符串或完整 cURL")]),
            self._fold("小宇宙", [credential("xiaoyuzhou_cookie", "小宇宙认证信息", "支持 Token、含刷新令牌的 Cookie 或完整 cURL")]),
            self._fold("通知", [select("notification_mode", "通知内容", [("有新增时汇总，并提醒异常", "changes"), ("仅异常和恢复", "errors"), ("关闭推送", "off")]),
                                select("notification_channel", "通知通道", [("MoviePilot 已配置通道", "moviepilot"), ("Bark", "bark")]),
                                credential("bark_webhook_url", "Bark 地址", "优先填写服务器/设备Key，不必附带标题和正文", show="{{ notification_channel === 'bark' }}")]),
            self._fold("高级设置", advanced),
        ]}]
        return form, {"enable": False, "private": True, "cron": "0 2 * * *", "douban_cookie": "",
                      "trakt_username": "", "trakt_client_id": "", "trakt_redirect_uri": "", "trakt_manual_mappings": "",
                      "weread_api_key": "", "netease_cookie": "", "xiaoyuzhou_cookie": "",
                      "weread_limit": 20, "netease_limit": 20, "xiaoyuzhou_limit": 20,
                      "sync_type": "all", "max_sync_count": 0, "trakt_history_limit": 20, "trakt_history_days": 30,
                      "douban_write_limit": 10, "douban_write_interval": 10,
                      "notification_mode": "changes", "notification_channel": "moviepilot", "bark_webhook_url": "",
                      "_ui_busy": False, "_ui_trakt_url": url, "_ui_trakt_message": auth_text,
                      "_ui_douban_paused": bool(state.get("requires_verification")), "_ui_douban_message": state.get("reason") or ""}

    def get_page(self) -> Optional[List[dict]]:
        """展示本轮真实写入汇总、异常和待处理明细，来源状态单独标识。"""
        def timestamp(value: Any) -> str:
            return datetime.fromtimestamp(value).strftime("%m-%d %H:%M") if value else "—"

        def table(headers: List[str], rows: List[List[Any]]) -> dict:
            return {"component": "VTable", "props": {"density": "compact", "hover": True, "class": "text-no-wrap", "style": "max-height:420px;overflow:auto"}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [{"component": "th", "text": label} for label in headers]}]},
                {"component": "tbody", "content": [{"component": "tr", "content": [value if isinstance(value, dict) else {"component": "td", "text": str(value)} for value in row]} for row in rows]},
            ]}

        def link(subject: str, host: str) -> dict:
            path = "podcast" if host == "www.douban.com" else "subject"
            return {"component": "td", "content": [{"component": "VBtn", "text": "豆瓣条目", "props": {"href": f"https://{host}/{path}/{subject}/", "target": "_blank", "rel": "noreferrer", "variant": "text", "size": "small"}}]} if subject else {"component": "td", "text": "—"}

        state = self.get_data("douban_sync_state") or {}
        pending = state.get("pending") or {}
        actual = state.get("synced") or {}
        targets = state.get("targets") or {}

        def target_url(subject: str, host: str) -> str:
            path = "ilmen/thing" if host == "www.douban.com" else "subject"
            return f"https://{host}/j/{path}/{subject}/interest"

        def sync_status(subject: str, host: str, expected: str = "", legacy: bool = False) -> str:
            if not subject:
                return "未匹配"
            url = target_url(subject, host)
            if url in pending:
                return "待重试" if pending[url].get("last_error") else "待处理"
            saved = actual.get(url) or {}
            if saved:
                return "已同步" if not expected or saved.get("interest") == expected else "来源状态已变化，待处理"
            return "已同步（历史记录）" if legacy else "尚未同步"

        run = self.get_data("last_run") or {}
        status_labels = {"running": "运行中", "completed": "本轮完成", "partial": "部分完成", "paused": "已暂停", "failed": "执行异常"}
        running = self._run_lock.locked()
        next_run = "未启用"
        services = self.get_service()
        if services:
            fire_time = services[0]["trigger"].get_next_fire_time(None, datetime.now().astimezone())
            next_run = fire_time.strftime("%m-%d %H:%M") if fire_time else "无后续时间"
        status = "运行中" if running else status_labels.get(run.get("status"), "等待定时同步")
        page = [{"component": "div", "props": {"class": "text-h6 mb-2"}, "text": "同步概览"},
                {"component": "VAlert", "props": {"type": "warning" if run.get("status") in ("partial", "paused", "failed") else "info", "variant": "tonal",
                 "text": f"{status} · 最近执行 {timestamp(run.get('started_at'))} · 完成 {timestamp(run.get('finished_at'))} · 下次执行 {next_run}"}},
                {"component": "div", "props": {"class": "d-flex flex-wrap ga-2 my-3"}, "content": [
                    {"component": "VChip", "props": {"variant": "tonal"}, "text": f"{label} {count}"} for label, count in (
                        ("本轮写入", run.get("written", 0)), ("未变化跳过", run.get("skipped", 0)),
                        ("提交失败", run.get("failed", 0)), ("待处理", len(pending)),
                    )]},
        ]
        issues = self.get_data("notification_issues") or {}
        for source, issue in issues.items():
            if issue.get("active"):
                page.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "my-2", "text": f"{source}：{issue.get('title', '异常')}"}})
        paused = bool(state.get("requires_verification") or state.get("blocked_until", 0) > time.time())
        if paused:
            recovery = "请先在浏览器完成验证，再更新Cookie或点击恢复同步" if state.get("requires_verification") else f"冷却至 {timestamp(state.get('blocked_until'))}，随后按定时任务继续"
            page.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "text": f"豆瓣已暂停：{state.get('reason', '')}。{recovery}"}})
            if state.get("requires_verification"):
                page.append(self._action_button("已完成验证，恢复同步", "douban/resume", running))
        if pending:
            rows = []
            for url, entry in list(pending.items())[:20]:
                context = targets.get(url) or {}
                subject = url.split('/')[-2]
                reason = entry.get("last_error") or (state.get("reason") if paused else "等待后续写入额度")
                rows.append([context.get("title") or f"豆瓣条目 {subject}", context.get("source") or "历史待处理", reason, link(subject, entry.get("host", "www.douban.com"))])
            page.append(self._fold(f"待处理记录 · {len(pending)} 条（最近20条）", [table(["名称", "来源", "原因", "链接"], rows)]))

        successful_times = state.get("target_success_at") or {}
        if successful_times:
            rows = []
            labels = {"collect": "已看/读/听过", "do": "正在看/读/听"}
            for url, when in sorted(successful_times.items(), key=lambda item: item[1], reverse=True)[:20]:
                context = targets.get(url) or {}
                subject = url.split('/')[-2]
                host = urlparse(url).hostname or "www.douban.com"
                rows.append([context.get("title") or f"豆瓣条目 {subject}", context.get("source") or "队列补交",
                             labels.get((actual.get(url) or {}).get("interest"), "已提交"), timestamp(when), link(subject, host)])
            page.append(self._fold("最近成功写入 · 最近20条（包含队列补交）", [table(["名称", "来源", "豆瓣状态", "成功时间", "链接"], rows)]))

        finished = self.get_data("finished") or self.get_data("synced") or {}
        watching = self.get_data("watching") or {}
        video = {str(item.get("douban_id")): item for item in [*watching.values(), *finished.values()] if item.get("douban_id")}
        video_rows = [[item.get("title", "未知"), item.get("status", "在看"), sync_status(str(item.get("douban_id")), "movie.douban.com", legacy=True), timestamp(item.get("sync_time")), link(str(item.get("douban_id")), "movie.douban.com")]
                      for item in sorted(video.values(), key=lambda item: item.get("sync_time", 0), reverse=True)[:20]]
        if video:
            page.append(self._fold(f"Trakt · {len(video)} 条历史记录", [table(["标题", "豆瓣目标状态", "同步结果", "同步时间", "链接"], video_rows)]))

        books = self.get_data("weread_books") or []
        book_maps = self.get_data("weread_book_id_map") or {}
        book_synced = self.get_data("weread_synced") or {}
        book_rows = []
        for book in books[:20]:
            subject = str((book_maps.get(str(book.get("book_id"))) or {}).get("subject_id") or "")
            expected = "collect" if book.get("status") == "读完" else "do"
            legacy = (book_synced.get(subject) or {}).get("douban_status") == expected
            result = sync_status(subject, "book.douban.com", expected, legacy) if book.get("status") in ("读完", "在读") else "不需同步"
            book_rows.append([book.get("title", "未知"), book.get("status") or "未读", f"{book.get('reading_progress', 0)}%", result, link(subject, "book.douban.com")])
        if books:
            page.append(self._fold(f"微信读书 · {len(books)} 本来源记录", [table(["书名", "来源阅读状态", "进度", "豆瓣同步结果", "链接"], book_rows)]))

        albums = self.get_data("netease_albums") or {}
        album_rows = [[item.get("douban_title") or item.get("album", "未知"), item.get("artist", "—"), sync_status(str(item.get("douban_id")), "music.douban.com", legacy=True), timestamp(item.get("sync_time")), link(str(item.get("douban_id")), "music.douban.com")]
                      for item in sorted(albums.values(), key=lambda item: item.get("sync_time", 0), reverse=True)[:20]]
        if albums:
            page.append(self._fold(f"网易云音乐 · {len(albums)} 条历史记录", [table(["专辑", "艺术家", "豆瓣同步结果", "同步时间", "链接"], album_rows)]))

        episodes = self.get_data("xiaoyuzhou_episodes") or []
        podcasts = {}
        for ep in episodes:
            key = str(ep.get("podcast_id") or ep.get("podcast_name") or "")
            previous = podcasts.get(key)
            if not previous or (ep.get("is_finished", False), ep.get("listen_pct", 0)) > (previous.get("is_finished", False), previous.get("listen_pct", 0)):
                podcasts[key] = ep
        podcast_map = self.get_data("xiaoyuzhou_podcast_map") or {}
        podcast_synced = self.get_data("xiaoyuzhou_podcasts") or {}
        podcast_rows = []
        for ep in list(podcasts.values())[:20]:
            name = ep.get("podcast_name", "未知")
            subject = str((podcast_map.get(name) or {}).get("subject_id") or "")
            expected = "collect" if ep.get("is_finished") or not ep.get("listen_pct") else "do"
            legacy = (podcast_synced.get(subject) or {}).get("status") == expected
            podcast_rows.append([name, "听完" if ep.get("is_finished") else f"进度 {ep.get('listen_pct', 0) * 100:.0f}%", sync_status(subject, "www.douban.com", expected, legacy), link(subject, "www.douban.com")])
        if podcasts:
            page.append(self._fold(f"小宇宙 · {len(podcasts)} 个播客（来源 {len(episodes)} 条单集）", [table(["播客", "来源收听状态", "豆瓣同步结果", "链接"], podcast_rows)]))
        if not any((video, books, albums, episodes, pending)):
            page.append({"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": "暂无同步记录，执行一次同步后会在这里显示最近结果。"}})
        return page
