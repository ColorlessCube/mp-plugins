# -*- coding: utf-8 -*-
import io
import re
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from apscheduler.triggers.cron import CronTrigger

from app.core.context import MediaInfo
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType
from app.utils.http import RequestUtils


@dataclass
class SubtitleCandidate:
    """
    字幕候选项，保存搜索源返回的可下载字幕元数据。
    """

    source: str
    title: str
    file_name: str = ""
    language: str = ""
    score: float = 0
    download_url: str = ""
    file_id: Optional[int] = None
    raw: Optional[dict] = None


class ChineseSubtitle(_PluginBase):
    """
    中文字幕下载插件，负责在媒体整理完成或目录扫描时补齐外挂中文字幕。
    """

    plugin_name = "中文字幕下载"
    plugin_desc = "媒体整理完成后，自动从 ASSRT、OpenSubtitles、SubDL 搜索并下载中文字幕。"
    plugin_icon = "subtitle.png"
    plugin_version = "1.2.21"
    plugin_author = "Codex"
    plugin_config_prefix = "chinese_subtitle_"
    plugin_order = 30
    auth_level = 1

    _enable: bool = False
    _overwrite: bool = False
    _notify: bool = False
    _language_suffix: str = ".zh-CN"
    _timeout: int = 20
    _source_order: str = "assrt,opensubtitles,subdl"
    _max_candidates: int = 5
    _scan_enable: bool = False
    _scan_system_library_dirs: bool = True
    _scan_cron: str = "0 4 * * *"
    _scan_dirs: str = ""
    _scan_limit: int = 50

    _assrt_token: str = ""
    _assrt_interval: int = 3
    _opensubtitles_api_key: str = ""
    _opensubtitles_username: str = ""
    _opensubtitles_password: str = ""
    _opensubtitles_languages: str = "zh-cn,zh-tw,ze"
    _opensubtitles_daily_limit: int = 5
    _subdl_api_key: str = ""
    _subdl_languages: str = "ZH,ZH_CN,ZH_TW"

    _opensubtitles_token: str = ""
    _opensubtitles_token_time: float = 0
    _assrt_last_request_time: float = 0
    _assrt_backoff_until: float = 0
    _assrt_request_lock = threading.Lock()
    _subtitle_task_lock = threading.RLock()
    _scan_task_lock = threading.Lock()
    _assrt_season_cache: Dict[str, List[str]] = {}
    _scan_active: bool = False
    _scan_disabled_sources: Set[str] = set()

    _sources = {
        "assrt": "_assrt_token",
        "opensubtitles": "_opensubtitles_api_key",
        "subdl": "_subdl_api_key",
    }
    _assrt_backoff_key = "assrt_backoff_until"
    _opensubtitles_quota_key = "opensubtitles_download_quota"
    _source_miss_cache_key = "source_miss_cache"
    _source_miss_cache_ttl = 24 * 3600
    _source_miss_cache_limit = 2000

    def init_plugin(self, config: dict = None):
        """初始化插件配置。"""
        config = config or {}
        self._enable = config.get("enable", False)
        self._overwrite = config.get("overwrite", False)
        self._notify = config.get("notify", False)
        self._language_suffix = (config.get("language_suffix") or ".zh-CN").strip()
        self._timeout = self._config_int(config.get("timeout"), 20, minimum=1)
        self._source_order = (config.get("source_order") or "assrt,opensubtitles,subdl").strip()
        self._max_candidates = self._config_int(config.get("max_candidates"), 5, minimum=1)
        self._scan_enable = config.get("scan_enable", False)
        self._scan_system_library_dirs = config.get("scan_system_library_dirs", True)
        self._scan_cron = (config.get("scan_cron") or "0 4 * * *").strip()
        self._scan_dirs = (config.get("scan_dirs") or "").strip()
        self._scan_limit = self._config_int(config.get("scan_limit"), 50, minimum=1)
        self._assrt_token = (config.get("assrt_token") or "").strip()
        self._assrt_interval = self._config_int(config.get("assrt_interval"), 3, minimum=0)
        self._opensubtitles_api_key = (config.get("opensubtitles_api_key") or "").strip()
        self._opensubtitles_username = (config.get("opensubtitles_username") or "").strip()
        self._opensubtitles_password = (config.get("opensubtitles_password") or "").strip()
        self._opensubtitles_languages = (config.get("opensubtitles_languages") or "zh-cn,zh-tw,ze").strip()
        self._opensubtitles_daily_limit = self._config_int(config.get("opensubtitles_daily_limit"), 5, minimum=0)
        self._subdl_api_key = (config.get("subdl_api_key") or "").strip()
        self._subdl_languages = (config.get("subdl_languages") or "ZH,ZH_CN,ZH_TW").strip()

    @staticmethod
    def _first_value(*values: Any) -> Optional[Any]:
        for value in values:
            if value not in (None, ""):
                return value
        return None

    @staticmethod
    def _config_int(value: Any, default: int, minimum: Optional[int] = None) -> int:
        try:
            result = int(value) if value not in (None, "") else default
        except (TypeError, ValueError):
            result = default
        if minimum is not None:
            result = max(minimum, result)
        return result

    def get_state(self) -> bool:
        """获取插件启用状态。"""
        return self._enable

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """获取插件命令列表。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """获取插件 API 列表。"""
        return []

    def get_page(self) -> Optional[List[dict]]:
        """获取插件详情页配置。"""
        return None

    def stop_service(self):
        """停止插件服务。"""
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """获取定时扫描服务配置。"""
        if not self._enable or not self._scan_enable:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._scan_cron or "0 4 * * *")
        except Exception as err:
            logger.warn(f"中文字幕目录扫描 cron 解析失败，使用默认 0 4 * * *：{err}")
            trigger = CronTrigger.from_crontab("0 4 * * *")
        return [{
            "id": "chinese_subtitle_scan",
            "name": "中文字幕目录扫描",
            "trigger": trigger,
            "func": self.scan_library,
            "kwargs": {
                "coalesce": True,
                "max_instances": 1,
            },
        }]

    @eventmanager.register(EventType.TransferComplete)
    def transfer_complete(self, event: Event):
        """处理媒体整理完成事件。"""
        with type(self)._subtitle_task_lock:
            if not self._enable:
                return
            event_data = event.event_data or {}
            fileitem = event_data.get("fileitem")
            mediainfo = event_data.get("mediainfo")
            meta = event_data.get("meta")
            transferinfo = event_data.get("transferinfo")

            target_item = getattr(transferinfo, "target_item", None) if transferinfo else None
            video_path = Path((target_item or fileitem).path) if (target_item or fileitem) else None
            storage = getattr(target_item or fileitem, "storage", "local") if (target_item or fileitem) else "local"
            self._process_video(video_path=video_path, mediainfo=mediainfo, meta=meta, storage=storage)

    def scan_library(self):
        """扫描媒体库中缺少中文字幕的视频。"""
        cls = type(self)
        if not cls._scan_task_lock.acquire(blocking=False):
            logger.warn("中文字幕目录扫描仍在运行，跳过本次重复触发")
            return
        try:
            with cls._subtitle_task_lock:
                if not self._enable or not self._scan_enable:
                    return
                cls._scan_active = True
                cls._scan_disabled_sources = set()
                try:
                    self._scan_library_locked()
                finally:
                    cls._scan_active = False
                    cls._scan_disabled_sources = set()
        finally:
            cls._scan_task_lock.release()

    def _scan_library_locked(self):
        scan_dirs = self._scan_directories()
        if not scan_dirs:
            logger.warn("中文字幕目录扫描未配置扫描目录")
            return
        attempted = 0
        downloaded = 0
        missing = 0
        logger.info(f"开始扫描缺失中文字幕视频，目录数：{len(scan_dirs)}")
        for scan_dir in scan_dirs:
            if not scan_dir.exists() or not scan_dir.is_dir():
                logger.warn(f"中文字幕扫描目录不存在或不是目录：{scan_dir}")
                continue
            for video_path in self._iter_video_files(scan_dir):
                if attempted >= self._scan_limit:
                    logger.info(f"中文字幕目录扫描达到单次尝试上限：{self._scan_limit}")
                    return
                if self._has_existing_subtitle(video_path):
                    continue
                missing += 1
                attempted += 1
                mediainfo = self._mediainfo_from_local_nfo(video_path)
                if self._process_video(video_path=video_path, mediainfo=mediainfo, meta=None, storage="local"):
                    downloaded += 1
        logger.info(f"中文字幕目录扫描完成，发现缺字幕视频 {missing} 个，尝试 {attempted} 个，成功下载 {downloaded} 个")

    def _process_video(self, video_path: Optional[Path], mediainfo: Any = None,
                       meta: Any = None, storage: str = "local") -> bool:
        if not video_path:
            return False
        if storage and storage != "local":
            logger.info(f"中文字幕下载暂不处理非本地存储：{storage}:{video_path}")
            return False
        if video_path.suffix.lower() not in settings.RMT_MEDIAEXT:
            return False
        if not video_path.exists():
            logger.warn(f"中文字幕下载跳过，视频文件不存在：{video_path}")
            return False
        if not self._overwrite and self._has_existing_subtitle(video_path):
            logger.info(f"中文字幕已存在，跳过：{video_path}")
            return False

        logger.info(f"开始搜索中文字幕：{video_path.name}")
        all_candidates = []
        for source in self._enabled_sources():
            try:
                if self._source_miss_cache_hit(source, video_path, mediainfo, meta):
                    logger.info(f"{source} 最近未命中过，跳过重复搜索：{video_path.name}")
                    continue
                if not self._source_search_available(source):
                    continue
                if source == "assrt":
                    saved = self._download_assrt_season_episode(video_path, mediainfo, meta)
                    if saved:
                        self._clear_source_miss(source, video_path, mediainfo, meta)
                        logger.info(f"中文字幕下载完成：{saved}")
                        if self._notify:
                            self.systemmessage.put(
                                title="中文字幕下载完成",
                                message=f"{video_path.name}\nASSRT: 整季字幕包",
                                role="plugin",
                            )
                        return True
                    if not self._source_search_available(source):
                        continue
                candidates = self._search_source(source, video_path, mediainfo, meta)
                if not candidates:
                    logger.info(f"{source} 未找到匹配中文字幕：{video_path.name}")
                    if self._source_search_available(source):
                        self._record_source_miss(source, video_path, mediainfo, meta)
                    continue
                self._clear_source_miss(source, video_path, mediainfo, meta)
                all_candidates.extend(candidates[: self._max_candidates])
            except Exception as err:
                logger.error(f"{source} 中文字幕处理失败：{err}", exc_info=True)
        for candidate in self._rank_candidates(all_candidates):
            logger.info(
                f"尝试下载中文字幕候选：{video_path.name} - {candidate.source} - "
                f"{candidate.title} - score={candidate.score:.1f}"
            )
            saved = self._download_candidate(candidate, video_path)
            if saved:
                logger.info(f"中文字幕下载完成：{saved}")
                if self._notify:
                    self.systemmessage.put(
                        title="中文字幕下载完成",
                        message=f"{video_path.name}\n{candidate.source}: {candidate.title}",
                        role="plugin",
                    )
                return True
        logger.warn(f"未能下载到中文字幕：{video_path.name}")
        return False

    def _scan_directories(self) -> List[Path]:
        paths = []
        if self._scan_system_library_dirs:
            for directory in DirectoryHelper().get_local_library_dirs():
                if directory.library_path:
                    paths.append(Path(directory.library_path))
        for line in self._scan_dirs.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            paths.append(Path(line))
        return self._unique_child_paths(paths)

    @staticmethod
    def _unique_child_paths(paths: List[Path]) -> List[Path]:
        unique_paths = []
        for path in paths:
            try:
                normalized = path.resolve()
            except Exception:
                normalized = path
            if any(normalized == existed or normalized.is_relative_to(existed) for existed in unique_paths):
                continue
            unique_paths = [existed for existed in unique_paths if not existed.is_relative_to(normalized)]
            unique_paths.append(normalized)
        return unique_paths

    @staticmethod
    def _iter_video_files(scan_dir: Path):
        for item in scan_dir.rglob("*"):
            if (
                    item.is_file()
                    and item.suffix.lower() in settings.RMT_MEDIAEXT
                    and not ChineseSubtitle._is_bluray_stream_segment(item)
            ):
                yield item

    @staticmethod
    def _is_bluray_stream_segment(video_path: Path) -> bool:
        parts = [part.lower() for part in video_path.parts]
        return (
                video_path.suffix.lower() == ".m2ts"
                and video_path.stem.isdigit()
                and "bdmv" in parts
                and "stream" in parts
        )

    def _mediainfo_from_local_nfo(self, video_path: Path) -> Optional[MediaInfo]:
        fallback = None
        for nfo_path in self._nfo_candidates(video_path):
            try:
                mediainfo = self._parse_nfo_mediainfo(nfo_path)
            except Exception as err:
                logger.warn(f"中文字幕扫描解析 NFO 失败：{nfo_path} - {err}")
                continue
            if not mediainfo:
                continue
            if mediainfo.imdb_id:
                self._log_nfo_mediainfo(video_path, mediainfo)
                return mediainfo
            if mediainfo.tmdb_id and not fallback:
                fallback = mediainfo
        if fallback:
            self._log_nfo_mediainfo(video_path, fallback)
        return fallback

    @staticmethod
    def _nfo_candidates(video_path: Path) -> List[Path]:
        preferred = [video_path.with_suffix(".nfo"), video_path.parent / "movie.nfo", video_path.parent / "tvshow.nfo"]
        candidates = [path for path in preferred if path.exists()]
        candidates.extend(path for path in sorted(video_path.parent.glob("*.nfo")) if path not in candidates)
        return candidates

    def _parse_nfo_mediainfo(self, nfo_path: Path) -> Optional[MediaInfo]:
        root = ET.parse(nfo_path).getroot()
        root_tag = self._strip_xml_namespace(root.tag).lower()
        media_type = self._nfo_media_type(root_tag)
        imdb_id = self._nfo_id(root, "imdb")
        tmdb_id = self._safe_int(self._nfo_id(root, "tmdb"))
        tvdb_id = self._safe_int(self._nfo_id(root, "tvdb"))
        if root_tag == "episodedetails":
            title = self._first_value(
                self._nfo_first_text(root, "showtitle"),
                self._nfo_first_text(root, "originaltitle"),
                self._nfo_first_text(root, "title"),
            )
        else:
            title = self._first_value(
                self._nfo_first_text(root, "title"),
                self._nfo_first_text(root, "originaltitle"),
                self._nfo_first_text(root, "showtitle"),
            )
        original_title = self._nfo_first_text(root, "originaltitle")
        year = self._nfo_year(root)
        mediainfo = MediaInfo(
            source="nfo",
            type=media_type,
            title=title,
            en_title=self._nfo_english_title(root),
            original_title=original_title,
            year=year,
            season=self._safe_int(self._nfo_first_text(root, "season")),
            tmdb_id=tmdb_id,
            imdb_id=imdb_id.strip() if imdb_id else None,
            tvdb_id=tvdb_id,
        )
        mediainfo.episode = self._safe_int(self._nfo_first_text(root, "episode"))
        return mediainfo

    @staticmethod
    def _nfo_media_type(root_tag: str) -> Optional[MediaType]:
        if root_tag == "movie":
            return MediaType.MOVIE
        if root_tag in {"tvshow", "episodedetails"}:
            return MediaType.TV
        return None

    def _nfo_year(self, root: ET.Element) -> Optional[str]:
        year = self._nfo_first_text(root, "year")
        if year:
            return year
        date_text = self._nfo_first_text(root, "premiered") or self._nfo_first_text(root, "aired")
        match = re.search(r"\b(19\d{2}|20\d{2})\b", date_text or "")
        return match.group(1) if match else None

    @classmethod
    def _nfo_id(cls, root: ET.Element, id_type: str) -> Optional[str]:
        value = cls._nfo_first_text(root, f"{id_type}id") or cls._nfo_uniqueid(root, id_type)
        if value:
            return value
        if id_type == "imdb":
            for value in cls._nfo_texts(root, "uniqueid"):
                if re.fullmatch(r"tt\d+", value, re.IGNORECASE):
                    return value
        return None

    @classmethod
    def _nfo_english_title(cls, root: ET.Element) -> Optional[str]:
        for tag_name in ("originaltitle", "sorttitle", "title", "showtitle"):
            for value in cls._nfo_texts(root, tag_name):
                if cls._looks_english_title(value):
                    return value
        return None

    @staticmethod
    def _looks_english_title(value: Optional[str]) -> bool:
        return bool(value and re.search(r"[A-Za-z]", value) and not re.search(r"[\u4e00-\u9fff]", value))

    @classmethod
    def _nfo_first_text(cls, root: ET.Element, tag_name: str) -> Optional[str]:
        return next(iter(cls._nfo_texts(root, tag_name)), None)

    @classmethod
    def _nfo_texts(cls, root: ET.Element, tag_name: str) -> List[str]:
        values = []
        for element in root.findall(".//"):
            if cls._strip_xml_namespace(element.tag).lower() != tag_name:
                continue
            value = (element.text or "").strip()
            if value:
                values.append(value)
        return values

    @classmethod
    def _nfo_uniqueid(cls, root: ET.Element, id_type: str) -> Optional[str]:
        for element in root.findall(".//"):
            if cls._strip_xml_namespace(element.tag).lower() != "uniqueid":
                continue
            if (element.attrib.get("type") or "").lower() != id_type:
                continue
            value = (element.text or "").strip()
            if value:
                return value
        return None

    @staticmethod
    def _strip_xml_namespace(tag_name: str) -> str:
        return tag_name.rsplit("}", 1)[-1]

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            text = str(value).strip()
            if re.fullmatch(r"\d+\.0+", text):
                text = text.split(".", 1)[0]
            return int(text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _unique_texts(values: List[str]) -> List[str]:
        unique_values = []
        for value in values:
            value = (value or "").strip()
            if value not in unique_values:
                unique_values.append(value)
        return unique_values

    @staticmethod
    def _number_to_chinese(number: int) -> str:
        digits = "零一二三四五六七八九"
        if number <= 0:
            return str(number)
        if number < 10:
            return digits[number]
        if number < 20:
            return f"十{digits[number % 10]}" if number % 10 else "十"
        if number < 100:
            ten, one = divmod(number, 10)
            return f"{digits[ten]}十{digits[one] if one else ''}"
        return str(number)

    @staticmethod
    def _chinese_number_to_int(text: str) -> Optional[int]:
        digit_map = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                     "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        text = (text or "").strip()
        if not text:
            return None
        if text in digit_map:
            return digit_map[text]
        if "十" in text:
            left, _, right = text.partition("十")
            tens = digit_map.get(left, 1) if left else 1
            ones = digit_map.get(right, 0) if right else 0
            return tens * 10 + ones
        return None

    @staticmethod
    def _log_nfo_mediainfo(video_path: Path, mediainfo: MediaInfo):
        logger.info(
            f"中文字幕扫描从 NFO 读取媒体ID：{video_path.name} "
            f"imdb={mediainfo.imdb_id or '-'} tmdb={mediainfo.tmdb_id or '-'}"
        )

    def _enabled_sources(self) -> List[str]:
        sources = []
        for source in re.split(r"[,，\s]+", self._source_order.lower()):
            token_attr = self._sources.get(source)
            if not token_attr or source in sources:
                continue
            if source in type(self)._scan_disabled_sources:
                continue
            if getattr(self, token_attr, None):
                sources.append(source)
        return sources

    def _source_search_available(self, source: str) -> bool:
        if source in type(self)._scan_disabled_sources:
            return False
        if source == "assrt":
            backoff_wait = self._assrt_backoff_remaining()
            if backoff_wait > 0:
                self._disable_source_for_scan("assrt", f"流控冷却期未结束，剩余 {backoff_wait:.1f} 秒")
                return False
        if source == "opensubtitles" and self._opensubtitles_download_quota_exhausted():
            return False
        return True

    def _disable_source_for_scan(self, source: str, reason: str):
        cls = type(self)
        if not cls._scan_active or source in cls._scan_disabled_sources:
            return
        cls._scan_disabled_sources.add(source)
        logger.warn(f"{source} 本轮目录扫描已暂停：{reason}")

    def _assrt_backoff_remaining(self) -> float:
        now = time.time()
        persisted_until = 0
        try:
            persisted_until = float(self.get_data(self._assrt_backoff_key) or 0)
        except (TypeError, ValueError):
            persisted_until = 0
        backoff_until = max(type(self)._assrt_backoff_until, persisted_until)
        if backoff_until <= now:
            if persisted_until > 0:
                self.save_data(self._assrt_backoff_key, 0)
            return 0
        type(self)._assrt_backoff_until = backoff_until
        return backoff_until - now

    def _set_assrt_backoff(self, backoff_seconds: int):
        backoff_until = time.time() + backoff_seconds
        type(self)._assrt_backoff_until = backoff_until
        self.save_data(self._assrt_backoff_key, backoff_until)

    def _source_miss_cache_hit(self, source: str, video_path: Path, mediainfo: Any, meta: Any) -> bool:
        cache = self._source_miss_cache()
        key = self._source_miss_cache_entry_key(source, video_path, mediainfo, meta)
        return key in cache and cache[key] > time.time()

    def _record_source_miss(self, source: str, video_path: Path, mediainfo: Any, meta: Any):
        cache = self._source_miss_cache()
        key = self._source_miss_cache_entry_key(source, video_path, mediainfo, meta)
        cache[key] = time.time() + self._source_miss_cache_ttl
        if len(cache) > self._source_miss_cache_limit:
            cache = dict(sorted(cache.items(), key=lambda item: item[1])[-self._source_miss_cache_limit:])
        self.save_data(self._source_miss_cache_key, cache)

    def _clear_source_miss(self, source: str, video_path: Path, mediainfo: Any, meta: Any):
        cache = self._source_miss_cache()
        key = self._source_miss_cache_entry_key(source, video_path, mediainfo, meta)
        if key in cache:
            cache.pop(key, None)
            self.save_data(self._source_miss_cache_key, cache)

    def _source_miss_cache(self) -> Dict[str, float]:
        now = time.time()
        cache = self.get_data(self._source_miss_cache_key) or {}
        if not isinstance(cache, dict):
            return {}
        cleaned = {}
        for key, expires_at in cache.items():
            try:
                expires_value = float(expires_at)
            except (TypeError, ValueError):
                continue
            if expires_value > now:
                cleaned[str(key)] = expires_value
        if len(cleaned) != len(cache):
            self.save_data(self._source_miss_cache_key, cleaned)
        return cleaned

    def _source_miss_cache_entry_key(self, source: str, video_path: Path, mediainfo: Any, meta: Any) -> str:
        try:
            path_key = str(video_path.resolve())
        except Exception:
            path_key = str(video_path)
        season_episode = self._season_episode_from_path(video_path) or self._season_episode_from_meta(mediainfo, meta)
        title = self._target_title(video_path, mediainfo, meta)
        return "|".join([source, path_key, title or "", season_episode or ""])

    def _search_source(self, source: str, video_path: Path, mediainfo: Any, meta: Any) -> List[SubtitleCandidate]:
        if source == "assrt":
            return self._search_assrt(video_path, mediainfo, meta)
        if source == "opensubtitles":
            return self._search_opensubtitles(video_path, mediainfo, meta)
        if source == "subdl":
            return self._search_subdl(video_path, mediainfo, meta)
        return []

    def _search_assrt(self, video_path: Path, mediainfo: Any, meta: Any) -> List[SubtitleCandidate]:
        target_title = self._target_title(video_path, mediainfo, meta)
        target_year = self._target_year(video_path, mediainfo, meta)
        target_resolution = self._target_resolution(video_path)
        for query in self._assrt_queries(video_path, mediainfo, meta):
            if not self._source_search_available("assrt"):
                break
            candidates = self._search_assrt_by_query(
                video_path=video_path,
                target_title=target_title,
                target_year=target_year,
                target_resolution=target_resolution,
                query=query,
            )
            if candidates:
                return candidates
        return []

    def _download_assrt_season_episode(self, video_path: Path, mediainfo: Any, meta: Any) -> Optional[Path]:
        if not self._is_tv(mediainfo, meta):
            return None
        season, episode = self._season_episode_numbers(video_path, mediainfo, meta)
        if not season or not episode:
            return None
        cache_key = self._assrt_season_cache_key(video_path, mediainfo, meta, season)
        cached_urls = type(self)._assrt_season_cache.get(cache_key)
        if cached_urls is None:
            cached_urls = self._assrt_season_file_urls(video_path, mediainfo, meta, season)
            type(self)._assrt_season_cache[cache_key] = cached_urls
        if not cached_urls:
            return None
        urls = self._assrt_episode_urls(cached_urls, season, episode)
        if not urls:
            logger.info(f"ASSRT 整季缓存未找到当前集字幕：{video_path.name}")
            return None
        for url in urls:
            logger.info(f"开始下载 ASSRT 整季匹配字幕：{video_path.name}，地址：{self._safe_url_for_log(url)}")
            saved = self._download_url(url, video_path)
            if saved:
                return saved
        return None

    def _assrt_season_file_urls(self, video_path: Path, mediainfo: Any, meta: Any, season: int) -> List[str]:
        target_title = self._target_title(video_path, mediainfo, meta)
        target_year = self._target_year(video_path, mediainfo, meta)
        target_resolution = self._target_resolution(video_path)
        _, episode = self._season_episode_numbers(video_path, mediainfo, meta)
        for query in self._assrt_season_queries(video_path, mediainfo, meta, season):
            if not self._source_search_available("assrt"):
                break
            candidates = self._search_assrt_by_query(
                video_path=video_path,
                target_title=target_title,
                target_year=target_year,
                target_resolution=target_resolution,
                query=query,
                filelist=True,
            )
            for candidate in candidates[: self._max_candidates]:
                if episode and not self._assrt_candidate_filelist_has_episode(candidate, season, episode):
                    continue
                urls = self._assrt_candidate_file_urls(candidate)
                if self._assrt_season_package_matches(urls, season, episode):
                    logger.info(f"ASSRT 通过整季查询 `{query}` 找到整季候选：{candidate.title}")
                    return urls
        return []

    def _assrt_candidate_file_urls(self, candidate: SubtitleCandidate) -> List[str]:
        sub_id = (candidate.raw or {}).get("id")
        if not sub_id:
            return []
        res = self._assrt_get_res(
            "https://api.assrt.net/v1/sub/detail",
            params={"token": self._assrt_token, "id": sub_id},
        )
        if not res or res.status_code != 200:
            status_code = res.status_code if res is not None else "无响应"
            logger.warn(f"ASSRT 整季字幕详情获取失败，ID：{sub_id}，状态：{status_code}")
            return []
        data = res.json()
        details = ((data.get("sub") or {}).get("subs") or []) if data.get("status") == 0 else []
        if not details:
            return []
        detail = details[0]
        file_urls = [f.get("url") for f in detail.get("filelist") or [] if f.get("url")]
        supported_urls = [url for url in file_urls if not self._unsupported_subtitle_url_suffix(url)]
        if not file_urls and detail.get("url") and not self._unsupported_subtitle_url_suffix(detail.get("url")):
            supported_urls.append(detail.get("url"))
        return supported_urls

    def _search_assrt_by_query(self, video_path: Path, target_title: str, target_year: str,
                               target_resolution: str, query: str, filelist: bool = False) -> List[SubtitleCandidate]:
        seen_ids = set()
        params = {
            "token": self._assrt_token,
            "q": query,
            "cnt": 15,
            "pos": 0,
            "is_file": 0,
        }
        if filelist:
            params["filelist"] = 1
        res = self._assrt_get_res("https://api.assrt.net/v1/sub/search", params=params)
        if not res or res.status_code != 200:
            if res is not None:
                logger.warn(f"ASSRT 搜索失败，状态码：{res.status_code}")
            return []
        data = res.json()
        subs = ((data.get("sub") or {}).get("subs") or []) if data.get("status") == 0 else []
        if isinstance(subs, dict):
            subs = []
        candidates = []
        for item in subs:
            sub_id = item.get("id")
            if sub_id in seen_ids:
                continue
            lang_desc = ((item.get("lang") or {}).get("desc") or "")
            text = f"{item.get('native_name') or ''} {item.get('release_site') or ''} {lang_desc}"
            if not self._looks_chinese(text):
                continue
            if self._is_low_quality_subtitle_metadata(text):
                continue
            match_text = f"{item.get('native_name') or ''} {item.get('videoname') or ''}"
            score_filter = self._assrt_candidate_score(
                target_title=target_title,
                target_year=target_year,
                target_resolution=target_resolution,
                query=query,
                match_text=match_text,
            )
            if score_filter is None:
                continue
            seen_ids.add(sub_id)
            score = (
                    score_filter
                    + float(item.get("vote_score") or 0) * 5
                    + min(float(item.get("down_count") or 0), 5000) / 100
            )
            candidates.append(SubtitleCandidate(
                source="ASSRT",
                title=item.get("native_name") or str(sub_id),
                language=lang_desc,
                score=score,
                raw=item,
            ))
        if candidates:
            logger.info(f"ASSRT 通过片名 `{query}` 找到 {len(candidates)} 条候选：{video_path.name}")
        else:
            logger.info(f"ASSRT 通过片名 `{query}` 未找到匹配中文字幕：{video_path.name}")
        return sorted(candidates, key=lambda x: x.score, reverse=True)

    def _search_opensubtitles(self, video_path: Path, mediainfo: Any, meta: Any) -> List[SubtitleCandidate]:
        if self._opensubtitles_download_quota_exhausted():
            return []
        headers = self._opensubtitles_headers()
        params = {
            "languages": self._opensubtitles_languages,
            "order_by": "download_count",
            "order_direction": "desc",
        }
        if not self._add_media_ids(params, mediainfo, strip_imdb_tt=True):
            params["query"] = self._target_title(video_path, mediainfo, meta)
        if self._is_tv(mediainfo, meta):
            season = self._season(mediainfo, meta)
            episode = self._episode(mediainfo, meta)
            if season:
                params["season_number"] = season
            if episode:
                params["episode_number"] = episode

        search_params_list = [params]
        movie_hash = self._opensubtitles_hash(video_path)
        if movie_hash:
            hash_params = params.copy()
            hash_params["moviehash"] = movie_hash
            search_params_list.insert(0, hash_params)

        for search_params in search_params_list:
            res = RequestUtils(headers=headers, timeout=self._timeout).get_res(
                "https://api.opensubtitles.com/api/v1/subtitles", params=search_params
            )
            if not res:
                logger.warn(f"OpenSubtitles 搜索无响应：{video_path.name} 参数={self._opensubtitles_log_params(search_params)}")
                continue
            if res.status_code != 200:
                logger.warn(
                    f"OpenSubtitles 搜索失败，状态码：{res.status_code} "
                    f"参数={self._opensubtitles_log_params(search_params)} "
                    f"返回={self._response_summary(res)}"
                )
                continue

            candidates = self._opensubtitles_candidates(res.json(), video_path)
            if candidates:
                return candidates
            if "moviehash" in search_params and len(search_params_list) > 1:
                logger.info(f"OpenSubtitles moviehash 未命中，改用媒体ID/标题重试：{video_path.name}")
        return []

    def _opensubtitles_candidates(self, data: dict, video_path: Path) -> List[SubtitleCandidate]:
        candidates = []
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            files = attrs.get("files") or []
            if not files:
                continue
            if attrs.get("ai_translated") or attrs.get("machine_translated"):
                continue
            if attrs.get("hearing_impaired"):
                continue
            file_info = files[0]
            title_text = " ".join(str(value or "") for value in (
                attrs.get("release"),
                attrs.get("feature_details", {}).get("title"),
                file_info.get("file_name"),
            ))
            if self._is_low_quality_subtitle_metadata(title_text):
                continue
            score = self._release_match_score(video_path.name, file_info.get("file_name"))
            score += min(float(attrs.get("download_count") or 0), 5000) / 100
            score += float(attrs.get("ratings") or 0) * 10
            candidates.append(SubtitleCandidate(
                source="OpenSubtitles",
                title=attrs.get("release") or attrs.get("feature_details", {}).get("title") or item.get("id"),
                file_name=file_info.get("file_name") or "",
                language=attrs.get("language") or "",
                score=score,
                file_id=file_info.get("file_id"),
                raw=item,
            ))
        return sorted(candidates, key=lambda x: x.score, reverse=True)

    def _search_subdl(self, video_path: Path, mediainfo: Any, meta: Any) -> List[SubtitleCandidate]:
        params = {
            "api_key": self._subdl_api_key,
            "file_name": video_path.name,
            "languages": self._subdl_languages,
            "subs_per_page": 30,
            "releases": 1,
            "comment": 1,
        }
        self._add_media_ids(params, mediainfo, strip_imdb_tt=False)
        if self._is_tv(mediainfo, meta):
            params["type"] = "tv"
            season = self._season(mediainfo, meta)
            episode = self._episode(mediainfo, meta)
            if season:
                params["season_number"] = season
            if episode:
                params["episode_number"] = episode
        else:
            params["type"] = "movie"
        if getattr(mediainfo, "year", None):
            params["year"] = getattr(mediainfo, "year")

        res = RequestUtils(timeout=self._timeout).get_res("https://api.subdl.com/api/v1/subtitles", params=params)
        if not res or res.status_code != 200:
            return []
        data = res.json()
        if not data.get("status"):
            return []
        candidates = []
        for item in data.get("subtitles") or []:
            link = item.get("url") or item.get("download") or item.get("link")
            if not link:
                continue
            download_url = link if str(link).startswith("http") else urljoin("https://dl.subdl.com", str(link))
            release = item.get("release_name") or item.get("name") or item.get("subtitle_name") or ""
            if item.get("hi") or self._is_low_quality_subtitle_metadata(release):
                continue
            score = self._release_match_score(video_path.name, release)
            candidates.append(SubtitleCandidate(
                source="SubDL",
                title=release or download_url,
                file_name=release,
                language=item.get("language") or item.get("lang") or "",
                score=score,
                download_url=download_url,
                raw=item,
            ))
        return sorted(candidates, key=lambda x: x.score, reverse=True)

    def _download_candidate(self, candidate: SubtitleCandidate, video_path: Path) -> Optional[Path]:
        if candidate.source == "ASSRT":
            return self._download_assrt(candidate, video_path)
        if candidate.source == "OpenSubtitles":
            return self._download_opensubtitles(candidate, video_path)
        if candidate.download_url:
            return self._download_url(candidate.download_url, video_path)
        return None

    def _download_assrt(self, candidate: SubtitleCandidate, video_path: Path) -> Optional[Path]:
        sub_id = (candidate.raw or {}).get("id")
        if not sub_id:
            return None
        res = self._assrt_get_res(
            "https://api.assrt.net/v1/sub/detail",
            params={"token": self._assrt_token, "id": sub_id},
        )
        if not res or res.status_code != 200:
            status_code = res.status_code if res is not None else "无响应"
            logger.warn(f"ASSRT 字幕详情获取失败，ID：{sub_id}，状态：{status_code}")
            return None
        data = res.json()
        details = ((data.get("sub") or {}).get("subs") or []) if data.get("status") == 0 else []
        if not details:
            logger.info(f"ASSRT 字幕详情无可下载文件，ID：{sub_id}")
            return None
        detail = details[0]
        file_urls = [f.get("url") for f in detail.get("filelist") or [] if f.get("url")]
        supported_file_urls = [url for url in file_urls if not self._unsupported_subtitle_url_suffix(url)]
        if file_urls and not supported_file_urls:
            logger.info(f"ASSRT 字幕详情仅包含不支持的字幕文件，跳过候选，ID：{sub_id}")
            return None
        urls = supported_file_urls
        if not file_urls and detail.get("url"):
            urls.append(detail.get("url"))
        if not urls:
            logger.info(f"ASSRT 字幕详情未返回下载地址，ID：{sub_id}")
            return None
        for url in urls:
            if self._unsupported_subtitle_url_suffix(url):
                logger.info(f"跳过不支持的 ASSRT 字幕文件格式，ID：{sub_id}，地址：{self._safe_url_for_log(url)}")
                continue
            logger.info(f"开始下载 ASSRT 字幕文件，ID：{sub_id}，地址：{self._safe_url_for_log(url)}")
            saved = self._download_url(url, video_path)
            if saved:
                return saved
            logger.info(f"ASSRT 字幕文件下载未成功，ID：{sub_id}，地址：{self._safe_url_for_log(url)}")
        return None

    def _download_opensubtitles(self, candidate: SubtitleCandidate, video_path: Path) -> Optional[Path]:
        if not candidate.file_id:
            return None
        if not self._consume_opensubtitles_download_quota():
            return None
        token = self._get_opensubtitles_token()
        if not token:
            logger.warn("OpenSubtitles 未配置用户名/密码或登录失败，无法下载字幕")
            self._rollback_opensubtitles_download_quota()
            return None
        headers = self._opensubtitles_headers(token=token)
        res = RequestUtils(headers=headers, timeout=self._timeout).post_res(
            "https://api.opensubtitles.com/api/v1/download",
            json={"file_id": candidate.file_id},
        )
        if not res or res.status_code != 200:
            logger.warn(
                f"OpenSubtitles 下载链接获取失败，状态码：{getattr(res, 'status_code', '无响应')} "
                f"file_id={candidate.file_id} 返回={self._response_summary(res) if res else ''}"
            )
            self._rollback_opensubtitles_download_quota()
            return None
        link = (res.json() or {}).get("link")
        if not link:
            logger.warn(f"OpenSubtitles 下载链接为空，file_id={candidate.file_id}")
            self._rollback_opensubtitles_download_quota()
            return None
        return self._download_url(link, video_path)

    def _consume_opensubtitles_download_quota(self) -> bool:
        if self._opensubtitles_download_quota_exhausted():
            return False
        if self._opensubtitles_daily_limit <= 0:
            return True
        quota = self._opensubtitles_download_quota()
        quota["count"] += 1
        self.save_data(self._opensubtitles_quota_key, quota)
        logger.info(f"OpenSubtitles 今日下载额度：{quota['count']}/{self._opensubtitles_daily_limit}")
        return True

    def _opensubtitles_download_quota_exhausted(self) -> bool:
        if self._opensubtitles_daily_limit <= 0:
            return False
        quota = self._opensubtitles_download_quota()
        if quota["count"] < self._opensubtitles_daily_limit:
            return False
        logger.warn(
            f"OpenSubtitles 今日下载额度已用完：{quota['count']}/{self._opensubtitles_daily_limit}，跳过 OpenSubtitles"
        )
        return True

    def _rollback_opensubtitles_download_quota(self):
        if self._opensubtitles_daily_limit <= 0:
            return
        quota = self._opensubtitles_download_quota()
        if quota["count"] > 0:
            quota["count"] -= 1
            self.save_data(self._opensubtitles_quota_key, quota)

    def _opensubtitles_download_quota(self) -> dict:
        today = self._today_key()
        quota = self.get_data(self._opensubtitles_quota_key) or {}
        if quota.get("date") != today:
            return {"date": today, "count": 0}
        try:
            count = int(quota.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        return {"date": today, "count": max(0, count)}

    @staticmethod
    def _today_key() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _assrt_get_res(self, url: str, params: dict):
        cls = type(self)
        with cls._assrt_request_lock:
            now = time.time()
            backoff_wait = self._assrt_backoff_remaining()
            if backoff_wait > 0:
                logger.info(f"ASSRT 处于流控冷却期，跳过请求，剩余 {backoff_wait:.1f} 秒")
                return None
            interval_wait = 0
            if self._assrt_interval > 0 and cls._assrt_last_request_time:
                interval_wait = self._assrt_interval - (now - cls._assrt_last_request_time)
            if interval_wait > 0:
                logger.info(f"ASSRT 请求节流等待 {interval_wait:.1f} 秒")
                time.sleep(interval_wait)
                if "assrt" in cls._scan_disabled_sources:
                    return None
                backoff_wait = self._assrt_backoff_remaining()
                if backoff_wait > 0:
                    logger.info(f"ASSRT 处于流控冷却期，跳过请求，剩余 {backoff_wait:.1f} 秒")
                    return None
            res = RequestUtils(timeout=self._timeout).get_res(url, params=params)
            cls._assrt_last_request_time = time.time()
            if res is not None and res.status_code == 509:
                backoff_seconds = self._assrt_backoff_seconds(res)
                self._set_assrt_backoff(backoff_seconds)
                self._disable_source_for_scan("assrt", f"触发 509 流控，冷却 {backoff_seconds} 秒")
                logger.warn(f"ASSRT 触发流控 509，暂停请求 {backoff_seconds} 秒")
            return res

    def _download_url(self, url: str, video_path: Path) -> Optional[Path]:
        if self._unsupported_subtitle_url_suffix(url):
            logger.info(f"跳过不支持的字幕文件格式，地址：{self._safe_url_for_log(url)}")
            return None
        res = RequestUtils(timeout=self._timeout).get_res(url)
        if not res or res.status_code != 200 or not res.content:
            status_code = res.status_code if res is not None else "无响应"
            logger.warn(f"字幕文件下载失败，状态：{status_code}，地址：{self._safe_url_for_log(url)}")
            return None
        content = res.content
        content_type = (res.headers.get("Content-Type") or "").lower()
        if zipfile.is_zipfile(io.BytesIO(content)) or "zip" in content_type or url.lower().split("?")[0].endswith(".zip"):
            return self._save_from_zip(content, video_path)
        suffix = self._guess_subtitle_suffix(url, content)
        if suffix not in settings.RMT_SUBEXT:
            return None
        if not self._valid_chinese_subtitle_content(content, suffix):
            logger.warn(f"字幕文件内容校验失败，地址：{self._safe_url_for_log(url)}")
            return None
        target = self._target_subtitle_path(video_path, suffix)
        target.write_bytes(content)
        return target

    @staticmethod
    def _safe_url_for_log(url: str) -> str:
        parsed = urlparse(url or "")
        if not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    @staticmethod
    def _unsupported_subtitle_url_suffix(url: str) -> bool:
        url_suffix = Path(urlparse(url or "").path).suffix.lower()
        return bool(url_suffix and url_suffix != ".zip" and url_suffix not in settings.RMT_SUBEXT)

    def _assrt_backoff_seconds(self, res) -> int:
        retry_after = (res.headers.get("Retry-After") or "").strip() if getattr(res, "headers", None) else ""
        if retry_after.isdigit():
            return max(10, min(int(retry_after), 300))
        return max(30, min(self._assrt_interval * 10, 300))

    def _save_from_zip(self, content: bytes, video_path: Path) -> Optional[Path]:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            members = [m for m in zf.infolist() if Path(m.filename).suffix.lower() in settings.RMT_SUBEXT]
            if not members:
                return None
            scored = sorted(
                members,
                key=lambda m: self._release_match_score(video_path.name, Path(m.filename).name),
                reverse=True,
            )
            for member in scored:
                suffix = Path(member.filename).suffix.lower()
                with zf.open(member) as src:
                    content = src.read()
                if not self._valid_chinese_subtitle_content(content, suffix):
                    logger.info(f"跳过压缩包内无效字幕文件：{member.filename}")
                    continue
                target = self._target_subtitle_path(video_path, suffix)
                target.write_bytes(content)
                return target
        return None

    def _target_subtitle_path(self, video_path: Path, suffix: str) -> Path:
        lang_suffix = self._language_suffix
        if lang_suffix and not lang_suffix.startswith("."):
            lang_suffix = f".{lang_suffix}"
        return video_path.with_name(f"{video_path.stem}{lang_suffix}{suffix}")

    def _has_existing_subtitle(self, video_path: Path) -> bool:
        subtitle_path = self._existing_subtitle_path(video_path)
        if not subtitle_path:
            return False
        if self._valid_existing_subtitle(subtitle_path):
            return True
        logger.warn(f"已有中文字幕无效，重新搜索：{subtitle_path}")
        return False

    def _existing_subtitle_path(self, video_path: Path) -> Optional[Path]:
        for suffix in settings.RMT_SUBEXT:
            for subtitle_path in (video_path.with_suffix(suffix), self._target_subtitle_path(video_path, suffix)):
                if subtitle_path.exists():
                    return subtitle_path
        return None

    def _valid_existing_subtitle(self, subtitle_path: Path) -> bool:
        suffix = subtitle_path.suffix.lower()
        if suffix not in settings.RMT_SUBEXT:
            return False
        try:
            return self._valid_chinese_subtitle_content(subtitle_path.read_bytes(), suffix)
        except Exception as err:
            logger.warn(f"读取已有中文字幕失败：{subtitle_path} - {err}")
            return False

    @staticmethod
    def _looks_chinese(text: str) -> bool:
        return bool(re.search(r"中|简|繁|双语|字幕组|人人|YYeTs|CHS|CHT|Chinese|zh", text, re.IGNORECASE))

    @staticmethod
    def _is_low_quality_subtitle_metadata(text: str) -> bool:
        text = text or ""
        return bool(re.search(
            r"机翻|机器翻译|自动翻译|听障|听写|ai\s*translated|machine\s*translated|"
            r"auto\s*translated|hearing\s*impaired|\bSDH\b|\bHI\b",
            text,
            re.IGNORECASE,
        ))

    @staticmethod
    def _rank_candidates(candidates: List[SubtitleCandidate]) -> List[SubtitleCandidate]:
        unique_candidates = []
        seen_keys = set()
        for candidate in candidates:
            key = (
                candidate.source,
                candidate.file_id,
                candidate.download_url,
                candidate.title,
                candidate.file_name,
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unique_candidates.append(candidate)
        return sorted(unique_candidates, key=lambda x: x.score, reverse=True)

    def _assrt_queries(self, video_path: Path, mediainfo: Any, meta: Any) -> List[str]:
        queries = []
        episode_texts = [""]
        if self._is_tv(mediainfo, meta):
            season, episode = self._season_episode_numbers(video_path, mediainfo, meta)
            episode_texts = self._assrt_episode_query_texts(season, episode)
        for title in self._assrt_query_titles(video_path, mediainfo, meta):
            for episode_text in episode_texts:
                query = f"{title} {episode_text}" if episode_text else title
                query = re.sub(r"\s+", " ", query).strip()
                if query and query not in queries:
                    queries.append(query)
        return queries[:8] if self._is_tv(mediainfo, meta) else queries

    def _assrt_season_queries(self, video_path: Path, mediainfo: Any, meta: Any, season: int) -> List[str]:
        queries = []
        season_texts = [f"S{int(season):02d}", "全季", ""]
        if int(season) > 1:
            season_texts.insert(1, f"第{int(season)}季")
        for title in self._assrt_query_titles(video_path, mediainfo, meta):
            for season_text in season_texts:
                query = re.sub(r"\s+", " ", f"{title} {season_text}").strip()
                if query and query not in queries:
                    queries.append(query)
        return queries[:6]

    def _assrt_episode_query_texts(self, season: Optional[int], episode: Optional[int]) -> List[str]:
        texts = []
        if season and episode:
            texts.append(f"S{int(season):02d}E{int(episode):02d}")
        if episode:
            episode_number = int(episode)
            texts.extend([
                f"E{episode_number:02d}",
                f"第{episode_number}集",
            ])
        texts.append("")
        return self._unique_texts(texts)

    def _assrt_query_titles(self, video_path: Path, mediainfo: Any, meta: Any) -> List[str]:
        titles = self._title_candidates(video_path, mediainfo, meta)
        chinese_titles = [title for title in titles if re.search(r"[\u4e00-\u9fff]", title)]
        other_titles = [title for title in titles if title not in chinese_titles]
        prioritized_titles = self._unique_texts([*chinese_titles[:2], *other_titles[:1]])
        return prioritized_titles or [video_path.stem]

    def _season_episode_numbers(self, video_path: Path, mediainfo: Any, meta: Any) -> Tuple[Optional[int], Optional[int]]:
        match = re.search(r"S(\d{1,2})E(\d{1,3})", video_path.stem, re.IGNORECASE)
        if match:
            return int(match.group(1)), int(match.group(2))
        season = self._season(mediainfo, meta)
        episode = self._episode(mediainfo, meta)
        return int(season) if season else None, int(episode) if episode else None

    def _assrt_season_cache_key(self, video_path: Path, mediainfo: Any, meta: Any, season: int) -> str:
        title = next(iter(self._title_candidates(video_path, mediainfo, meta)), "")
        return f"{video_path.parent.resolve()}|{title.lower()}|S{int(season):02d}"

    def _assrt_season_package_matches(self, urls: List[str], season: int, episode: Optional[int] = None) -> bool:
        if not urls:
            return False
        file_names = [self._url_file_name(url) for url in urls]
        if episode and any(self._assrt_file_name_matches_episode(name, season, episode) for name in file_names):
            return True
        season_pattern = re.compile(rf"\bS0?{int(season)}E\d{{1,3}}\b", re.IGNORECASE)
        numbered_count = sum(1 for name in file_names if season_pattern.search(name) or self._chinese_episode_number(name))
        return numbered_count >= 2

    def _assrt_episode_urls(self, urls: List[str], season: int, episode: int) -> List[str]:
        return [url for url in urls if self._assrt_file_name_matches_episode(self._url_file_name(url), season, episode)]

    def _assrt_candidate_filelist_has_episode(self, candidate: SubtitleCandidate, season: int, episode: int) -> bool:
        filelist = (candidate.raw or {}).get("filelist") or []
        if not filelist:
            return True
        names = [item.get("f") or item.get("filename") or "" for item in filelist]
        return any(self._assrt_file_name_matches_episode(name, season, episode) for name in names)

    def _assrt_file_name_matches_episode(self, file_name: str, season: int, episode: int) -> bool:
        text = self._normalize_episode_file_name(file_name)
        if re.search(rf"\bS0?{int(season)}E0?{int(episode)}(?:\b|E)", text, re.IGNORECASE):
            return True
        if re.search(rf"\bEP?0?{int(episode)}\b", text, re.IGNORECASE):
            return True
        chinese_episode = self._chinese_episode_number(text)
        return chinese_episode == int(episode)

    @staticmethod
    def _normalize_episode_file_name(file_name: str) -> str:
        text = Path(file_name or "").name
        text = re.sub(r"[_\-.]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _chinese_episode_number(cls, text: str) -> Optional[int]:
        match = re.search(r"第\s*(\d{1,4}|[零〇一二两三四五六七八九十百千]+)\s*[集话話]", text or "")
        if not match:
            return None
        value = match.group(1)
        if value.isdigit():
            return int(value)
        return cls._chinese_number_to_int(value)

    @staticmethod
    def _url_file_name(url: str) -> str:
        return Path(urlparse(url or "").path).name

    def _assrt_candidate_score(self, target_title: str, target_year: str, target_resolution: str,
                               query: str, match_text: str) -> Optional[float]:
        title_score = max(
            self._release_match_score(target_title, match_text),
            self._text_match_score(target_title, match_text),
            self._text_match_score(query, match_text),
        )
        if title_score < 45:
            return None

        candidate_years = set(re.findall(r"\b(19\d{2}|20\d{2})\b", match_text))
        if target_year and candidate_years and not self._year_matches(target_year, candidate_years):
            return None

        candidate_resolutions = {
            resolution.lower()
            for resolution in re.findall(r"\b(720p|1080p|2160p|4k|8k)\b", match_text, re.IGNORECASE)
        }
        resolution_score = 0
        normalized_resolution = self._normalize_resolution(target_resolution)
        if normalized_resolution and candidate_resolutions:
            if normalized_resolution in {self._normalize_resolution(res) for res in candidate_resolutions}:
                resolution_score = 20

        year_score = 20 if target_year and self._year_matches(target_year, candidate_years) else 0
        return title_score + year_score + resolution_score

    @staticmethod
    def _clean_query_title(text: str) -> str:
        text = re.sub(r"\[[^\]]+]", " ", text or "")
        text = re.sub(r"\([^)]*\d{4}[^)]*\)", " ", text)
        text = re.sub(r"\b(720p|1080p|2160p|4k|8k|web[-_. ]?dl|webrip|bluray|bdrip|hdtv|hdr|dv|remux)\b",
                      " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\b(x264|x265|h264|h265|hevc|avc|aac|dts|ddp?5?\\.?1|atmos)\b",
                      " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*-\s*$", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip(" -._")

    def _title_candidates(self, video_path: Path, mediainfo: Any, meta: Any) -> List[str]:
        if self._is_tv(mediainfo, meta):
            return self._tv_title_candidates(video_path, mediainfo, meta)
        titles = []
        for value in (
                getattr(mediainfo, "en_title", None),
                getattr(mediainfo, "original_title", None),
                getattr(meta, "en_name", None),
                getattr(mediainfo, "title", None),
                getattr(meta, "cn_name", None),
                self._clean_query_title(video_path.parent.name),
                self._clean_query_title(video_path.stem),
        ):
            value = (value or "").strip()
            if value and value not in titles:
                titles.append(value)
        return titles

    def _tv_title_candidates(self, video_path: Path, mediainfo: Any, meta: Any) -> List[str]:
        titles = []
        path_title = self._series_title_from_path(video_path)
        file_title = self._series_title_from_filename(video_path)
        metadata_values = (
            getattr(meta, "en_name", None),
            getattr(meta, "cn_name", None),
        )
        if not path_title and not file_title:
            metadata_values = (
                getattr(mediainfo, "en_title", None),
                getattr(mediainfo, "original_title", None),
                getattr(meta, "en_name", None),
                getattr(mediainfo, "title", None),
                getattr(meta, "cn_name", None),
            )
        for value in (path_title, *metadata_values, file_title):
            title = self._clean_query_title(value or "")
            if not title or self._is_generic_tv_title(title):
                continue
            if title not in titles:
                titles.append(title)
        return titles

    def _series_title_from_path(self, video_path: Path) -> str:
        parent_name = video_path.parent.name
        if self._is_generic_tv_title(parent_name) and video_path.parent.parent != video_path.parent:
            grandparent_title = self._clean_query_title(video_path.parent.parent.name)
            if self._is_generic_tv_title(grandparent_title):
                return ""
            return grandparent_title
        return self._clean_query_title(parent_name)

    def _series_title_from_filename(self, video_path: Path) -> str:
        title = re.split(r"\s+-\s+S\d{1,2}E\d{1,3}\b", video_path.stem, maxsplit=1, flags=re.IGNORECASE)[0]
        if title == video_path.stem:
            title = re.split(r"\bS\d{1,2}E\d{1,3}\b", video_path.stem, maxsplit=1, flags=re.IGNORECASE)[0]
        return self._clean_query_title(title)

    @staticmethod
    def _is_generic_tv_title(title: str) -> bool:
        title = re.sub(r"\s+", " ", (title or "").strip(" -._")).lower()
        if not title:
            return True
        return bool(re.fullmatch(
            r"(tv|shows?|series|电视剧|剧集|国产剧|欧美剧|日韩剧|动漫|动画|"
            r"(season|series)\s*\d+|s\d{1,2}|第\s*\d+\s*[季部]|第\s*[一二三四五六七八九十百]+\s*[季部]|特别篇|specials?)",
            title,
            re.IGNORECASE,
        ))

    def _target_title(self, video_path: Path, mediainfo: Any, meta: Any) -> str:
        return self._first_value(*self._title_candidates(video_path, mediainfo, meta), video_path.stem)

    @staticmethod
    def _target_year(video_path: Path, mediainfo: Any, meta: Any) -> str:
        for value in (
                getattr(mediainfo, "year", None),
                getattr(meta, "year", None),
        ):
            if value:
                return str(value)
        match = re.search(r"\b(19\d{2}|20\d{2})\b", f"{video_path.parent.name} {video_path.stem}")
        return match.group(1) if match else ""

    @staticmethod
    def _target_resolution(video_path: Path) -> str:
        match = re.search(r"\b(720p|1080p|2160p|4k|8k)\b", video_path.stem, re.IGNORECASE)
        return match.group(1) if match else ""

    @staticmethod
    def _normalize_resolution(resolution: str) -> str:
        resolution = (resolution or "").lower()
        if resolution == "4k":
            return "2160p"
        return resolution

    @staticmethod
    def _year_matches(target_year: str, candidate_years: set) -> bool:
        try:
            target = int(target_year)
        except (TypeError, ValueError):
            return False
        for year in candidate_years:
            try:
                if abs(int(year) - target) <= 1:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    @staticmethod
    def _season_episode_from_path(video_path: Path) -> str:
        match = re.search(r"S(\d{1,2})E(\d{1,3})", video_path.stem, re.IGNORECASE)
        if not match:
            return ""
        return f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}"

    def _season_episode_from_meta(self, mediainfo: Any, meta: Any) -> str:
        season = self._season(mediainfo, meta)
        episode = self._episode(mediainfo, meta)
        if not season or not episode:
            return ""
        return f"S{int(season):02d}E{int(episode):02d}"

    @staticmethod
    def _release_match_score(video_name: str, release_name: Optional[str]) -> float:
        if not release_name:
            return 0
        video_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", video_name.lower()))
        release_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", release_name.lower()))
        if not video_tokens or not release_tokens:
            return 0
        return len(video_tokens & release_tokens) / len(video_tokens) * 100

    @staticmethod
    def _text_match_score(query: str, text: str) -> float:
        query = re.sub(r"\s+", " ", (query or "").lower()).strip()
        text = re.sub(r"\s+", " ", (text or "").lower()).strip()
        if not query or not text:
            return 0
        if query == text:
            return 100
        if query in text or text in query:
            shorter, longer = sorted((query, text), key=len)
            containment_score = len(shorter) / len(longer) * 100
            return min(95, containment_score)
        return SequenceMatcher(None, query, text).ratio() * 100

    @staticmethod
    def _opensubtitles_log_params(params: dict) -> dict:
        return {key: value for key, value in params.items() if key in {
            "languages", "imdb_id", "tmdb_id", "query", "season_number", "episode_number", "moviehash"
        }}

    @staticmethod
    def _response_summary(res) -> str:
        try:
            data = res.json() or {}
            message = data.get("message") or data.get("error") or data.get("errors") or data
            return str(message)[:300]
        except Exception:
            try:
                return (res.text or "")[:300]
            except Exception:
                return ""

    @staticmethod
    def _guess_subtitle_suffix(url: str, content: bytes) -> str:
        suffix = Path(url.split("?", 1)[0]).suffix.lower()
        if suffix in settings.RMT_SUBEXT:
            return suffix
        head = content[:256].lower()
        if b"[script info]" in head or b"[events]" in head:
            return ".ass"
        return ".srt"

    @classmethod
    def _valid_subtitle_content(cls, content: bytes, suffix: str) -> bool:
        if not content or len(content.strip()) < 20:
            return False
        text = cls._decode_subtitle_text(content[:200000])
        if not text:
            return False
        head = text[:500].lower()
        if re.search(r"<\s*(html|body|script)\b|<!doctype\s+html", head):
            return False
        if suffix in {".ass", ".ssa"}:
            return bool(
                re.search(r"\[(script info|events)]", text, re.IGNORECASE)
                and re.search(r"(?m)^\s*(Dialogue|Format)\s*:", text)
            )
        return bool(re.search(
            r"(?m)^\s*\d+\s*\r?\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*"
            r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}",
            text,
        ))

    @classmethod
    def _valid_chinese_subtitle_content(cls, content: bytes, suffix: str) -> bool:
        if not cls._valid_subtitle_content(content, suffix):
            return False
        text = cls._decode_subtitle_text(content[:200000])
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

    @staticmethod
    def _decode_subtitle_text(content: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-16", "gb18030", "big5", "latin-1"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return ""

    def _opensubtitles_headers(self, token: str = "") -> dict:
        headers = {
            "Api-Key": self._opensubtitles_api_key,
            "User-Agent": "MoviePilot ChineseSubtitle v1.0.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _get_opensubtitles_token(self) -> str:
        if self._opensubtitles_token and time.time() - self._opensubtitles_token_time < 11 * 3600:
            return self._opensubtitles_token
        if not self._opensubtitles_username or not self._opensubtitles_password:
            return ""
        res = RequestUtils(headers=self._opensubtitles_headers(), timeout=self._timeout).post_res(
            "https://api.opensubtitles.com/api/v1/login",
            json={"username": self._opensubtitles_username, "password": self._opensubtitles_password},
        )
        if not res or res.status_code != 200:
            return ""
        token = (res.json() or {}).get("token") or ""
        if token:
            self._opensubtitles_token = token
            self._opensubtitles_token_time = time.time()
        return token

    @staticmethod
    def _opensubtitles_hash(video_path: Path) -> str:
        size = video_path.stat().st_size
        if size < 131072:
            return ""
        hash_value = size
        with video_path.open("rb") as fp:
            for chunk in (fp.read(65536),):
                hash_value += sum(int.from_bytes(chunk[i:i + 8], "little") for i in range(0, len(chunk), 8))
            fp.seek(max(0, size - 65536))
            chunk = fp.read(65536)
            hash_value += sum(int.from_bytes(chunk[i:i + 8], "little") for i in range(0, len(chunk), 8))
        return f"{hash_value & 0xFFFFFFFFFFFFFFFF:016x}"

    def _add_media_ids(self, params: dict, mediainfo: Any, strip_imdb_tt: bool = False) -> bool:
        imdb_id = self._imdb_id(mediainfo) if strip_imdb_tt else getattr(mediainfo, "imdb_id", None)
        if imdb_id:
            params["imdb_id"] = str(imdb_id)
            return True
        tmdb_id = getattr(mediainfo, "tmdb_id", None)
        if tmdb_id:
            params["tmdb_id"] = tmdb_id
            return True
        return False

    @staticmethod
    def _imdb_id(mediainfo: Any) -> Optional[str]:
        imdb_id = getattr(mediainfo, "imdb_id", None)
        if not imdb_id:
            return None
        imdb_id = str(imdb_id).lower().replace("tt", "")
        return str(int(imdb_id)) if imdb_id.isdigit() else None

    @staticmethod
    def _is_tv(mediainfo: Any, meta: Any) -> bool:
        media_type = getattr(mediainfo, "type", None) or getattr(meta, "type", None)
        return media_type == MediaType.TV or str(media_type).endswith("TV") or str(media_type) == "电视剧"

    @staticmethod
    def _season(mediainfo: Any, meta: Any) -> Optional[int]:
        return getattr(meta, "begin_season", None) or getattr(mediainfo, "season", None)

    @staticmethod
    def _episode(mediainfo: Any, meta: Any) -> Optional[int]:
        return getattr(meta, "begin_episode", None) or getattr(mediainfo, "episode", None)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """获取插件配置表单。"""
        def field(model: str, label: str, **props) -> dict:
            return {"component": "VTextField", "props": {"model": model, "label": label, **props}}

        def switch(model: str, label: str) -> dict:
            return {"component": "VSwitch", "props": {"model": model, "label": label}}

        def textarea(model: str, label: str, **props) -> dict:
            return {"component": "VTextarea", "props": {"model": model, "label": label, **props}}

        def col(component: dict, cols: int = 12, md: Optional[int] = None) -> dict:
            props = {"cols": cols}
            if md:
                props["md"] = md
            return {"component": "VCol", "props": props, "content": [component]}

        def row(*cols: dict) -> dict:
            return {"component": "VRow", "content": list(cols)}

        form = [{
            "component": "VForm",
            "content": [
                row(
                    col(switch("enable", "启用插件"), md=3),
                    col(switch("overwrite", "覆盖已有字幕"), md=3),
                    col(switch("notify", "下载成功通知"), md=3),
                    col(field("language_suffix", "字幕文件语言后缀"), md=3),
                ),
                row(
                    col(field("source_order", "字幕源顺序", placeholder="assrt,opensubtitles,subdl"), md=6),
                    col(field("max_candidates", "每源尝试数量", type="number"), md=3),
                    col(field("timeout", "请求超时秒数", type="number"), md=3),
                ),
                row(
                    col(switch("scan_enable", "启用目录扫描"), md=3),
                    col(switch("scan_system_library_dirs", "扫描系统媒体库目录"), md=3),
                    col(field("scan_cron", "扫描 Cron", placeholder="0 4 * * *"), md=3),
                    col(field("scan_limit", "单次最多尝试视频数", type="number"), md=3),
                    col(textarea(
                        "scan_dirs", "追加扫描目录", rows=4,
                        placeholder="每行一个 MoviePilot 可访问的本地目录。开启“扫描系统媒体库目录”时，这里可留空。",
                    )),
                ),
                row(
                    col(field("assrt_token", "ASSRT Token", placeholder="assrt.net 用户面板中的 API Token"), md=8),
                    col(field("assrt_interval", "ASSRT 请求间隔秒数", type="number"), md=4),
                ),
                row(
                    col(field("opensubtitles_api_key", "OpenSubtitles Api-Key"), md=4),
                    col(field("opensubtitles_username", "OpenSubtitles 用户名"), md=4),
                    col(field("opensubtitles_password", "OpenSubtitles 密码", type="password"), md=4),
                    col(field("opensubtitles_languages", "OpenSubtitles 语言", placeholder="zh-cn,zh-tw,ze"), md=8),
                    col(field("opensubtitles_daily_limit", "OpenSubtitles 每日下载上限", type="number"), md=4),
                ),
                row(
                    col(field("subdl_api_key", "SubDL API Key"), md=6),
                    col(field("subdl_languages", "SubDL 语言", placeholder="ZH,ZH_CN,ZH_TW"), md=6),
                ),
            ],
        }]
        return form, self._default_config()

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        return {
            "enable": False,
            "overwrite": False,
            "notify": False,
            "language_suffix": ".zh-CN",
            "source_order": "assrt,opensubtitles,subdl",
            "max_candidates": 5,
            "timeout": 20,
            "scan_enable": False,
            "scan_system_library_dirs": True,
            "scan_cron": "0 4 * * *",
            "scan_dirs": "",
            "scan_limit": 50,
            "assrt_token": "",
            "assrt_interval": 3,
            "opensubtitles_api_key": "",
            "opensubtitles_username": "",
            "opensubtitles_password": "",
            "opensubtitles_languages": "zh-cn,zh-tw,ze",
            "opensubtitles_daily_limit": 5,
            "subdl_api_key": "",
            "subdl_languages": "ZH,ZH_CN,ZH_TW",
        }
