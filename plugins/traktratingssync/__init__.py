# -*- coding: utf-8 -*-
"""
Trakt 评分同步到豆瓣插件
从 Trakt 读取用户电影评分，通过 TMDB/IMDB 匹配豆瓣条目，并将评分同步到豆瓣（标记为「看过」并写入评分）。
"""
import asyncio
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
# Trakt 要求：Content-Type、trakt-api-key、trakt-api-version（见 https://trakt.docs.apiary.io）
TRAKT_HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "trakt-api-version": TRAKT_API_VERSION,
}


def _trakt_rating_to_douban(trakt_rating: int) -> int:
    """Trakt 1-10 转为豆瓣 1-5 星"""
    if trakt_rating <= 0:
        return 1
    douban = max(1, min(5, round(trakt_rating / 2)))
    return int(douban)


class TraktRatingsSync(_PluginBase):
    plugin_name = "Trakt 评分同步豆瓣"
    plugin_desc = "从 Trakt 读取用户电影评分，匹配豆瓣条目并同步为「看过」及评分。"
    plugin_icon = "trakt.svg"
    plugin_version = "1.1.0"
    plugin_author = "ColorlessCube"
    author_url = "https://github.com/ColorlessCube"
    plugin_config_prefix = "trakt_ratings_sync_"
    plugin_order = 16
    auth_level = 1

    _enable = False
    _trakt_username = ""
    _trakt_client_id = ""
    _douban_cookie = ""
    _private = True
    _only_movies = True
    _max_sync_count = 0  # 0 表示不限制
    _cron = "0 2 * * *"  # 每天凌晨 2 点

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enable = config.get("enable", False)
        self._trakt_username = (config.get("trakt_username") or "").strip()
        self._trakt_client_id = (config.get("trakt_client_id") or "").strip()
        self._douban_cookie = config.get("douban_cookie", "")
        self._private = config.get("private", True)
        self._only_movies = config.get("only_movies", True)
        self._max_sync_count = int(config.get("max_sync_count") or 0) if config.get("max_sync_count") is not None else 0
        self._cron = config.get("cron", "0 2 * * *") or "0 2 * * *"

    def _fetch_trakt_ratings_movies(self) -> List[Dict[str, Any]]:
        """拉取 Trakt 用户电影评分列表（公开接口，仅需 client_id）。
        API 文档：https://trakt.docs.apiary.io 要求 Header：Content-Type、trakt-api-key、trakt-api-version。
        """
        if not self._trakt_username or not self._trakt_client_id:
            return []
        url = f"{TRAKT_API_BASE}/users/{self._trakt_username}/ratings/movies"
        headers = {
            **TRAKT_HEADERS_BASE,
            "trakt-api-key": self._trakt_client_id,
        }
        try:
            resp = RequestUtils(timeout=30, headers=headers).get_res(url=url)
            if not resp:
                logger.warning("Trakt API 请求失败（网络或超时）")
                return []
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, list):
                    logger.warning("Trakt API 返回格式异常，期望数组")
                    return []
                return data
            if resp.status_code == 429:
                logger.warning("Trakt API 触发频率限制(429)，请稍后再试")
                return []
            if resp.status_code == 403:
                logger.warning("Trakt API 拒绝访问(403)，请检查 Client ID 或该用户评分是否设为私有")
                return []
            if resp.status_code == 404:
                logger.warning("Trakt 用户不存在或未公开评分: %s", self._trakt_username)
                return []
            logger.warning("Trakt API 返回异常: status=%s body=%s", resp.status_code, (resp.text or "")[:200])
        except Exception as e:
            logger.error("拉取 Trakt 评分失败: %s", e, exc_info=True)
        return []

    async def _get_douban_id_by_tmdb(self, tmdb_id: Optional[int], imdb_id: Optional[str],
                                      title: Optional[str] = None, year: Optional[int] = None) -> Optional[str]:
        """根据 TMDB ID（及可选 IMDB/标题/年份）获取豆瓣 subject_id"""
        if tmdb_id:
            try:
                douban_info = await MediaChain().async_get_doubaninfo_by_tmdbid(
                    tmdbid=int(tmdb_id), mtype=MediaType.MOVIE
                )
                if douban_info and douban_info.get("id"):
                    return str(douban_info["id"])
            except Exception as e:
                logger.debug(f"TMDB {tmdb_id} 匹配豆瓣失败: {e}")
        if title or imdb_id:
            try:
                douban_info = await MediaChain().async_match_doubaninfo(
                    name=title or "Unknown",
                    year=str(year) if year else None,
                    mtype=MediaType.MOVIE,
                    imdbid=imdb_id,
                )
                if douban_info and douban_info.get("id"):
                    return str(douban_info["id"])
            except Exception as e:
                logger.debug(f"标题/IMDB 匹配豆瓣失败 {title}: {e}")
        return None

    def _sync_one(self, item: Dict[str, Any], douban_helper: DoubanHelper,
                  synced: Dict[str, Any], wait_retry: Dict[str, Any]) -> bool:
        """同步单条评分到豆瓣（同步上下文，内部用 run_coroutine_threadsafe 调异步匹配）。
        Trakt 返回项结构：{ "rating": 1-10, "rated_at": "...", "movie": { "title", "year", "ids": { "trakt", "slug", "imdb", "tmdb" } } }。
        """
        movie = item.get("movie") if isinstance(item.get("movie"), dict) else {}
        ids = movie.get("ids") if isinstance(movie.get("ids"), dict) else {}
        trakt_rating = item.get("rating")
        if not isinstance(trakt_rating, (int, float)):
            trakt_rating = 0
        trakt_rating = int(trakt_rating)
        douban_rating = _trakt_rating_to_douban(trakt_rating)
        tmdb_id = ids.get("tmdb")
        imdb_id = ids.get("imdb")
        trakt_id = ids.get("trakt") or movie.get("trakt_id")
        slug = ids.get("slug") or ""
        title = movie.get("title", "未知")
        year = movie.get("year")

        if not tmdb_id and not imdb_id:
            logger.warning(f"Trakt 条目无 tmdb/imdb: {title} ({year})")
            return False

        key = str(trakt_id) if trakt_id else slug or f"{title}_{year}"
        if key in synced:
            prev = synced[key]
            if prev.get("trakt_rating") == trakt_rating and prev.get("douban_id"):
                logger.debug(f"已同步过且评分未变，跳过: {title}")
                return True

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._get_douban_id_by_tmdb(
                    int(tmdb_id) if tmdb_id else None,
                    imdb_id,
                    title=title,
                    year=year,
                ),
                global_vars.loop,
            )
            subject_id = future.result(timeout=30)
        except Exception as e:
            logger.warning(f"匹配豆瓣失败 {title} ({year}): {e}")
            if key not in wait_retry:
                wait_retry[key] = {
                    "title": title,
                    "year": year,
                    "trakt_rating": trakt_rating,
                    "tmdb_id": tmdb_id,
                    "imdb_id": imdb_id,
                }
            return False

        if not subject_id:
            logger.warning(f"未找到豆瓣条目: {title} ({year})")
            return False

        ret = douban_helper.set_watching_status(
            subject_id=subject_id,
            status="collect",
            private=self._private,
            rating=douban_rating,
        )
        if ret:
            synced[key] = {
                "douban_id": subject_id,
                "trakt_rating": trakt_rating,
                "title": title,
                "year": year,
            }
            if key in wait_retry:
                del wait_retry[key]
            logger.info(f"同步成功: {title} ({year}) -> 豆瓣 {subject_id} 评分 {douban_rating} 星")
            return True
        else:
            logger.error(f"豆瓣提交失败: {title} ({year}) subject_id={subject_id}")
            if key not in wait_retry:
                wait_retry[key] = {
                    "title": title,
                    "year": year,
                    "trakt_rating": trakt_rating,
                    "subject_id": subject_id,
                }
            return False

    def sync_trakt_ratings_to_douban(self):
        """定时任务入口：拉取 Trakt 评分并同步到豆瓣"""
        if not self._enable:
            logger.debug("Trakt 评分同步插件未启用，跳过")
            return
        if not self._trakt_username or not self._trakt_client_id:
            logger.warning("未配置 Trakt 用户名或 Client ID，跳过同步")
            return

        logger.info("开始执行 Trakt 评分同步到豆瓣...")
        items = self._fetch_trakt_ratings_movies()
        if not items:
            logger.info("未获取到 Trakt 电影评分或接口异常")
            return

        if self._max_sync_count > 0:
            items = items[: self._max_sync_count]
            logger.info("本次最多同步 %d 条，已截断列表", self._max_sync_count)

        synced: Dict[str, Any] = self.get_data("synced") or {}
        wait_retry: Dict[str, Any] = self.get_data("wait") or {}

        try:
            douban_helper = DoubanHelper(user_cookie=self._douban_cookie or None)
        except Exception as e:
            logger.error(f"初始化豆瓣 Helper 失败（请检查 Cookie/CookieCloud）: {e}")
            return

        success_count = 0
        fail_count = 0
        for item in items:
            try:
                if self._sync_one(item, douban_helper, synced, wait_retry):
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                logger.error(f"同步单条失败: {e}", exc_info=True)

        self.save_data("synced", synced)
        self.save_data("wait", wait_retry)
        logger.info(f"Trakt 评分同步完成: 成功 {success_count}, 失败 {fail_count}")

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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "only_movies", "label": "仅同步电影"}}
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
                                            "model": "trakt_username",
                                            "label": "Trakt 用户名",
                                            "placeholder": "例如 ialex-cube",
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
                                            "model": "trakt_client_id",
                                            "label": "Trakt Client ID",
                                            "placeholder": "在 trakt.tv/oauth/applications 创建应用获取",
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
                                            "placeholder": "默认 0 2 * * *（每天凌晨 2 点）",
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
                                            "placeholder": "0 表示不限制，单次最多同步条数",
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
                                            "placeholder": "留空则从 CookieCloud 获取",
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
                                            "text": "从 Trakt 读取公开评分需在 https://trakt.tv/oauth/applications 创建应用并填写 Client ID；"
                                            "豆瓣 Cookie 留空时从 CookieCloud 获取，用于提交「看过」及评分。",
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
            "douban_cookie": "",
            "private": True,
            "only_movies": True,
            "max_sync_count": 0,
            "cron": "0 2 * * *",
        }

    def get_page(self) -> Optional[List[dict]]:
        """插件详情页：说明手动同步方式"""
        return [
            {
                "component": "VCard",
                "content": [
                    {
                        "component": "VCardTitle",
                        "props": {"content": "手动同步"},
                        "content": [],
                    },
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "立即执行一次 Trakt → 豆瓣 同步：请调用插件 API（GET 或 POST）\n"
                                    "路径：/api/v1/plugin/traktratingssync/sync\n"
                                    "或在「插件」→「API」中查看该接口。",
                                },
                                "content": [],
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "content": "立即同步",
                                    "color": "primary",
                                    "event": "runPlugin",
                                    "path": "/sync",
                                },
                                "content": [],
                            },
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
        """手动触发同步（API）"""
        try:
            self.sync_trakt_ratings_to_douban()
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
            logger.warning(f"Trakt 评分同步插件 cron 解析失败，使用默认 0 2 * * *: {e}")
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
                "func": self.sync_trakt_ratings_to_douban,
                "kwargs": {},
            }
        ]
