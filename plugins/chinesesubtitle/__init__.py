# -*- coding: utf-8 -*-
import hashlib
import io
import queue
import re
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

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
    plugin_version = "1.2.35"
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
    _prefer_bilingual: bool = True
    _upgrade_existing_to_bilingual: bool = True
    _max_candidates: int = 5
    _scan_enable: bool = False
    _scan_system_library_dirs: bool = True
    _scan_cron: str = "0 4 * * *"
    _scan_dirs: str = ""
    _scan_limit: int = 50
    _scan_miss_ttl_hours: int = 24

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
    _nfo_mediainfo_cache: Dict[str, Optional[MediaInfo]] = {}
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
    _season_source_miss_cache_key = "season_source_miss_cache"
    _season_source_miss_cache_limit = 2000
    _season_source_miss_threshold = 3
    _video_scan_miss_cache_key = "video_scan_miss_cache"
    _video_scan_miss_cache_limit = 5000
    _scan_cursor_cache_key = "scan_cursor_cache"
    _scan_cursor_cache_limit = 200
    _nfo_mediainfo_cache_limit = 1000
    _download_failed_url_cache_key = "download_failed_url_cache"
    _download_failed_url_cache_ttl = 24 * 3600
    _download_failed_url_cache_limit = 5000
    _assrt_candidate_url_attempt_limit = 2
    _subtitle_download_max_bytes = 8 * 1024 * 1024
    _subtitle_download_deadline_extra_seconds = 2
    _bilingual_preference_score = 80
    _candidate_download_score_floor = 70
    _non_bilingual_keep_margin = 20
    _release_feature_match_score = 15
    _release_feature_mismatch_penalty = 12
    _release_group_match_score = 15
    _release_group_mismatch_penalty = 5
    _subtitle_coverage_min_duration_seconds = 10 * 60
    _subtitle_coverage_min_ratio = 0.65
    _subtitle_coverage_end_min_ratio = 0.75
    _subtitle_coverage_start_max_ratio = 0.25
    _subtitle_coverage_start_max_seconds = 10 * 60

    def init_plugin(self, config: dict = None):
        """初始化插件配置。"""
        config = config or {}
        self._enable = config.get("enable", False)
        self._overwrite = config.get("overwrite", False)
        self._notify = config.get("notify", False)
        self._language_suffix = (config.get("language_suffix") or ".zh-CN").strip()
        self._timeout = self._config_int(config.get("timeout"), 20, minimum=1)
        self._source_order = (config.get("source_order") or "assrt,opensubtitles,subdl").strip()
        self._prefer_bilingual = config.get("prefer_bilingual", True)
        self._upgrade_existing_to_bilingual = config.get("upgrade_existing_to_bilingual", True)
        self._max_candidates = self._config_int(config.get("max_candidates"), 5, minimum=1)
        self._scan_enable = config.get("scan_enable", False)
        self._scan_system_library_dirs = config.get("scan_system_library_dirs", True)
        self._scan_cron = (config.get("scan_cron") or "0 4 * * *").strip()
        self._scan_dirs = (config.get("scan_dirs") or "").strip()
        self._scan_limit = self._config_int(config.get("scan_limit"), 50, minimum=1)
        self._scan_miss_ttl_hours = self._config_int(config.get("scan_miss_ttl_hours"), 24, minimum=0)
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
        cached_skipped = 0
        duplicate_skipped = 0
        seen_video_keys = set()
        limit_reached = False
        logger.info(f"开始扫描缺失中文字幕视频，目录数：{len(scan_dirs)}")
        for scan_dir in scan_dirs:
            if not scan_dir.exists() or not scan_dir.is_dir():
                logger.warn(f"中文字幕扫描目录不存在或不是目录：{scan_dir}")
                continue
            last_scanned_key = ""
            for video_path in self._scan_video_files_from_cursor(scan_dir):
                if attempted >= self._scan_limit:
                    logger.info(f"中文字幕目录扫描达到单次尝试上限：{self._scan_limit}")
                    limit_reached = True
                    break
                last_scanned_key = self._scan_cursor_file_key(video_path)
                video_key = self._video_identity_key(video_path)
                if video_key and video_key in seen_video_keys:
                    duplicate_skipped += 1
                    logger.info(f"中文字幕目录扫描跳过重复视频：{video_path.name}")
                    continue
                if video_key:
                    seen_video_keys.add(video_key)
                existing_subtitle_checked = False
                if not self._overwrite:
                    existing_subtitle_checked = True
                    if self._has_existing_subtitle(video_path):
                        continue
                missing += 1
                if self._video_scan_miss_cache_hit(video_path):
                    cached_skipped += 1
                    logger.info(f"中文字幕目录扫描跳过近期未命中视频：{video_path.name}")
                    continue
                attempted += 1
                mediainfo = self._mediainfo_from_local_nfo(video_path)
                if self._process_video(
                        video_path=video_path,
                        mediainfo=mediainfo,
                        meta=None,
                        storage="local",
                        existing_subtitle_checked=existing_subtitle_checked,
                ):
                    self._clear_video_scan_miss(video_path)
                    downloaded += 1
                else:
                    self._record_video_scan_miss(video_path)
            if last_scanned_key:
                self._record_scan_cursor(scan_dir, last_scanned_key)
            if limit_reached:
                break
        logger.info(
            f"中文字幕目录扫描完成，发现缺字幕视频 {missing} 个，"
            f"缓存跳过 {cached_skipped} 个，重复跳过 {duplicate_skipped} 个，"
            f"尝试 {attempted} 个，成功下载 {downloaded} 个"
        )

    def _process_video(self, video_path: Optional[Path], mediainfo: Any = None,
                       meta: Any = None, storage: str = "local",
                       existing_subtitle_checked: bool = False) -> bool:
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
        if not self._overwrite and not existing_subtitle_checked and self._has_existing_subtitle(video_path):
            logger.info(f"中文字幕已存在，跳过：{video_path}")
            return False

        logger.info(f"开始搜索中文字幕：{video_path.name}")
        all_candidates = []
        for source in self._enabled_sources():
            try:
                if self._source_miss_cache_hit(source, video_path, mediainfo, meta):
                    logger.info(f"{source} 最近未命中过，跳过重复搜索：{video_path.name}")
                    continue
                if self._season_source_miss_cache_hit(source, video_path, mediainfo, meta):
                    logger.info(f"{source} 本季近期连续未命中，跳过重复搜索：{video_path.name}")
                    continue
                if not self._source_search_available(source):
                    continue
                if source == "assrt":
                    saved = self._download_assrt_season_episode(video_path, mediainfo, meta)
                    if saved:
                        self._clear_source_miss(source, video_path, mediainfo, meta)
                        self._clear_season_source_miss(source, video_path, mediainfo, meta)
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
                        self._record_season_source_miss(source, video_path, mediainfo, meta)
                    continue
                self._clear_source_miss(source, video_path, mediainfo, meta)
                self._clear_season_source_miss(source, video_path, mediainfo, meta)
                all_candidates.extend(candidates[: self._max_candidates])
            except Exception as err:
                logger.error(f"{source} 中文字幕处理失败：{err}", exc_info=True)
        for candidate in self._downloadable_candidates(all_candidates):
            effective_score = self._candidate_sort_score(candidate)
            logger.info(
                f"尝试下载中文字幕候选：{video_path.name} - {candidate.source} - "
                f"{candidate.title} - score={effective_score:.1f}"
            )
            saved = self._download_candidate(candidate, video_path, mediainfo, meta)
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

    def _scan_video_files_from_cursor(self, scan_dir: Path) -> List[Path]:
        files = self._scan_video_files(scan_dir)
        if not files:
            return []
        cursor = self._scan_cursor_cache().get(self._scan_cursor_cache_entry_key(scan_dir), "")
        if not cursor:
            return files
        file_keys = [self._scan_cursor_file_key(video_path) for video_path in files]
        if cursor not in file_keys:
            return files
        cursor_index = file_keys.index(cursor)
        return files[cursor_index + 1:] + files[:cursor_index + 1]

    def _scan_video_files(self, scan_dir: Path) -> List[Path]:
        return sorted(self._iter_video_files(scan_dir), key=self._scan_cursor_file_key)

    def _record_scan_cursor(self, scan_dir: Path, file_key: str):
        directory_key = self._scan_cursor_cache_entry_key(scan_dir)
        if not directory_key or not file_key:
            return
        cache = self._scan_cursor_cache()
        cache[directory_key] = file_key
        if len(cache) > self._scan_cursor_cache_limit:
            cache = dict(list(cache.items())[-self._scan_cursor_cache_limit:])
        self.save_data(self._scan_cursor_cache_key, cache)

    def _scan_cursor_cache(self) -> Dict[str, str]:
        cache = self.get_data(self._scan_cursor_cache_key) or {}
        if not isinstance(cache, dict):
            return {}
        cleaned = {
            str(directory_key): str(file_key)
            for directory_key, file_key in cache.items()
            if directory_key and file_key
        }
        if len(cleaned) != len(cache):
            self.save_data(self._scan_cursor_cache_key, cleaned)
        return cleaned

    @staticmethod
    def _scan_cursor_cache_entry_key(scan_dir: Path) -> str:
        try:
            return str(scan_dir.resolve())
        except Exception:
            return str(scan_dir)

    @staticmethod
    def _scan_cursor_file_key(video_path: Path) -> str:
        try:
            return str(video_path.resolve())
        except Exception:
            return str(video_path)

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
                mediainfo = self._cached_nfo_mediainfo(nfo_path)
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

    def _cached_nfo_mediainfo(self, nfo_path: Path) -> Optional[MediaInfo]:
        cache_key = self._nfo_mediainfo_cache_key(nfo_path)
        if cache_key and cache_key in type(self)._nfo_mediainfo_cache:
            return type(self)._nfo_mediainfo_cache[cache_key]
        mediainfo = self._parse_nfo_mediainfo(nfo_path)
        if cache_key:
            cache = type(self)._nfo_mediainfo_cache
            cache[cache_key] = mediainfo
            if len(cache) > self._nfo_mediainfo_cache_limit:
                type(self)._nfo_mediainfo_cache = dict(list(cache.items())[-self._nfo_mediainfo_cache_limit:])
        return mediainfo

    @staticmethod
    def _nfo_mediainfo_cache_key(nfo_path: Path) -> str:
        try:
            stat = nfo_path.stat()
        except OSError:
            return ""
        try:
            path_key = str(nfo_path.resolve())
        except Exception:
            path_key = str(nfo_path)
        return f"{path_key}|{stat.st_size}|{stat.st_mtime_ns}"

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
            runtime=self._safe_int(self._nfo_first_text(root, "runtime")),
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
        for source in self._configured_sources():
            token_attr = self._sources.get(source)
            if source in type(self)._scan_disabled_sources:
                continue
            if getattr(self, token_attr, None):
                sources.append(source)
        return sources

    def _configured_sources(self) -> List[str]:
        sources = []
        for source in re.split(r"[,，\s]+", self._source_order.lower()):
            token_attr = self._sources.get(source)
            if not token_attr or source in sources:
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
            self._disable_source_for_scan("opensubtitles", "今日下载额度已用完")
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

    def _season_source_miss_cache_hit(self, source: str, video_path: Path, mediainfo: Any, meta: Any) -> bool:
        if not type(self)._scan_active:
            return False
        key = self._season_source_miss_cache_entry_key(source, video_path, mediainfo, meta)
        if not key:
            return False
        entry = self._season_source_miss_cache().get(key) or {}
        return int(entry.get("count") or 0) >= self._season_source_miss_threshold

    def _record_season_source_miss(self, source: str, video_path: Path, mediainfo: Any, meta: Any):
        if not type(self)._scan_active:
            return
        ttl = self._scan_miss_ttl_seconds()
        key = self._season_source_miss_cache_entry_key(source, video_path, mediainfo, meta)
        if ttl <= 0 or not key:
            return
        cache = self._season_source_miss_cache()
        entry = cache.get(key) or {}
        cache[key] = {
            "count": int(entry.get("count") or 0) + 1,
            "expires": time.time() + ttl,
        }
        if len(cache) > self._season_source_miss_cache_limit:
            cache = dict(sorted(
                cache.items(),
                key=lambda item: float((item[1] or {}).get("expires") or 0),
            )[-self._season_source_miss_cache_limit:])
        self.save_data(self._season_source_miss_cache_key, cache)

    def _clear_season_source_miss(self, source: str, video_path: Path, mediainfo: Any, meta: Any):
        key = self._season_source_miss_cache_entry_key(source, video_path, mediainfo, meta)
        if not key:
            return
        cache = self._season_source_miss_cache()
        if key in cache:
            cache.pop(key, None)
            self.save_data(self._season_source_miss_cache_key, cache)

    def _season_source_miss_cache(self) -> Dict[str, dict]:
        ttl = self._scan_miss_ttl_seconds()
        if ttl <= 0:
            return {}
        now = time.time()
        cache = self.get_data(self._season_source_miss_cache_key) or {}
        if not isinstance(cache, dict):
            return {}
        cleaned = {}
        for key, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            try:
                count = int(entry.get("count") or 0)
                expires_value = float(entry.get("expires") or 0)
            except (TypeError, ValueError):
                continue
            if count > 0 and expires_value > now:
                cleaned[str(key)] = {
                    "count": count,
                    "expires": expires_value,
                }
        if len(cleaned) != len(cache):
            self.save_data(self._season_source_miss_cache_key, cleaned)
        return cleaned

    def _season_source_miss_cache_entry_key(self, source: str, video_path: Path, mediainfo: Any, meta: Any) -> str:
        season, _ = self._season_episode_numbers(video_path, mediainfo, meta)
        if not season:
            return ""
        if not self._is_tv(mediainfo, meta) and not self._season_episode_from_path(video_path):
            return ""
        series_title = self._season_source_miss_series_title(video_path, mediainfo, meta)
        if not series_title:
            return ""
        imdb_id = str(getattr(mediainfo, "imdb_id", None) or "")
        tmdb_id = str(getattr(mediainfo, "tmdb_id", None) or "")
        option_key = self._season_source_miss_option_key(source)
        return "|".join([
            source,
            series_title.lower(),
            f"S{int(season):02d}",
            imdb_id,
            tmdb_id,
            option_key,
        ])

    def _season_source_miss_series_title(self, video_path: Path, mediainfo: Any, meta: Any) -> str:
        for title in (
                self._series_title_from_filename(video_path),
                self._series_title_from_path(video_path),
                self._target_title(video_path, mediainfo, meta),
        ):
            title = self._clean_query_title(title or "")
            if title and not self._is_generic_tv_title(title):
                return title
        return ""

    def _season_source_miss_option_key(self, source: str) -> str:
        source_languages = {
            "opensubtitles": self._opensubtitles_languages,
            "subdl": self._subdl_languages,
        }.get(source, "")
        return "|".join([
            self._language_suffix or "",
            str(bool(self._prefer_bilingual)),
            str(bool(self._upgrade_existing_to_bilingual)),
            source_languages or "",
        ])

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

    def _video_scan_miss_cache_hit(self, video_path: Path) -> bool:
        cache = self._video_scan_miss_cache()
        key = self._video_scan_miss_cache_entry_key(video_path)
        return bool(key and key in cache and cache[key] > time.time())

    def _record_video_scan_miss(self, video_path: Path):
        ttl = self._scan_miss_ttl_seconds()
        key = self._video_scan_miss_cache_entry_key(video_path)
        if ttl <= 0 or not key:
            return
        cache = self._video_scan_miss_cache()
        cache[key] = time.time() + ttl
        if len(cache) > self._video_scan_miss_cache_limit:
            cache = dict(sorted(cache.items(), key=lambda item: item[1])[-self._video_scan_miss_cache_limit:])
        self.save_data(self._video_scan_miss_cache_key, cache)

    def _clear_video_scan_miss(self, video_path: Path):
        key = self._video_scan_miss_cache_entry_key(video_path)
        if not key:
            return
        cache = self._video_scan_miss_cache()
        if key in cache:
            cache.pop(key, None)
            self.save_data(self._video_scan_miss_cache_key, cache)

    def _video_scan_miss_cache(self) -> Dict[str, float]:
        ttl = self._scan_miss_ttl_seconds()
        if ttl <= 0:
            return {}
        now = time.time()
        cache = self.get_data(self._video_scan_miss_cache_key) or {}
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
            self.save_data(self._video_scan_miss_cache_key, cleaned)
        return cleaned

    def _video_scan_miss_cache_entry_key(self, video_path: Path) -> str:
        identity_key = self._video_identity_key(video_path)
        if not identity_key:
            return ""
        option_key = "|".join([
            self._language_suffix or "",
            str(bool(self._prefer_bilingual)),
            str(bool(self._upgrade_existing_to_bilingual)),
            ",".join(self._configured_sources()),
            str(self._scan_miss_ttl_hours),
        ])
        return "|".join([identity_key, option_key])

    @staticmethod
    def _video_identity_key(video_path: Path) -> str:
        try:
            stat = video_path.stat()
        except OSError:
            return ""
        try:
            path_key = str(video_path.resolve())
        except Exception:
            path_key = str(video_path)
        return "|".join([path_key, str(stat.st_size), str(stat.st_mtime_ns)])

    def _scan_miss_ttl_seconds(self) -> int:
        return max(0, int(self._scan_miss_ttl_hours or 0)) * 3600

    def _download_failed_url_cache_hit(self, url: str) -> bool:
        cache = self._download_failed_url_cache()
        key = self._download_failed_url_cache_entry_key(url)
        return bool(key and key in cache and cache[key] > time.time())

    def _record_download_failed_url(self, url: str):
        key = self._download_failed_url_cache_entry_key(url)
        if not key:
            return
        cache = self._download_failed_url_cache()
        cache[key] = time.time() + self._download_failed_url_cache_ttl
        if len(cache) > self._download_failed_url_cache_limit:
            cache = dict(sorted(cache.items(), key=lambda item: item[1])[-self._download_failed_url_cache_limit:])
        self.save_data(self._download_failed_url_cache_key, cache)

    def _clear_download_failed_url(self, url: str):
        key = self._download_failed_url_cache_entry_key(url)
        if not key:
            return
        cache = self._download_failed_url_cache()
        if key in cache:
            cache.pop(key, None)
            self.save_data(self._download_failed_url_cache_key, cache)

    def _download_failed_url_cache(self) -> Dict[str, float]:
        now = time.time()
        cache = self.get_data(self._download_failed_url_cache_key) or {}
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
            self.save_data(self._download_failed_url_cache_key, cleaned)
        return cleaned

    @staticmethod
    def _download_failed_url_cache_entry_key(url: str) -> str:
        if not url:
            return ""
        return hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()

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
        target_release_text = video_path.name
        for query in self._assrt_queries(video_path, mediainfo, meta):
            if not self._source_search_available("assrt"):
                break
            candidates = self._search_assrt_by_query(
                video_path=video_path,
                target_title=target_title,
                target_year=target_year,
                target_resolution=target_resolution,
                target_release_text=target_release_text,
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
            saved = self._download_url(url, video_path, mediainfo, meta)
            if saved:
                return saved
        return None

    def _assrt_season_file_urls(self, video_path: Path, mediainfo: Any, meta: Any, season: int) -> List[str]:
        target_title = self._target_title(video_path, mediainfo, meta)
        target_year = self._target_year(video_path, mediainfo, meta)
        target_resolution = self._target_resolution(video_path)
        target_release_text = video_path.name
        _, episode = self._season_episode_numbers(video_path, mediainfo, meta)
        for query in self._assrt_season_queries(video_path, mediainfo, meta, season):
            if not self._source_search_available("assrt"):
                break
            candidates = self._search_assrt_by_query(
                video_path=video_path,
                target_title=target_title,
                target_year=target_year,
                target_resolution=target_resolution,
                target_release_text=target_release_text,
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
        supported_urls = self._assrt_supported_file_urls(detail.get("filelist") or [])
        if not file_urls and detail.get("url") and not self._unsupported_subtitle_url_suffix(detail.get("url")):
            supported_urls.append(detail.get("url"))
        return supported_urls

    def _search_assrt_by_query(self, video_path: Path, target_title: str, target_year: str,
                               target_resolution: str, target_release_text: str,
                               query: str, filelist: bool = False) -> List[SubtitleCandidate]:
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
                target_release_text=target_release_text,
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
        return sorted(candidates, key=self._candidate_sort_score, reverse=True)

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
            score += self._release_feature_score(video_path.name, title_text)
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
        return sorted(candidates, key=self._candidate_sort_score, reverse=True)

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
            score += self._release_feature_score(video_path.name, release)
            candidates.append(SubtitleCandidate(
                source="SubDL",
                title=release or download_url,
                file_name=release,
                language=item.get("language") or item.get("lang") or "",
                score=score,
                download_url=download_url,
                raw=item,
            ))
        return sorted(candidates, key=self._candidate_sort_score, reverse=True)

    def _download_candidate(self, candidate: SubtitleCandidate, video_path: Path,
                            mediainfo: Any = None, meta: Any = None) -> Optional[Path]:
        if candidate.source == "ASSRT":
            return self._download_assrt(candidate, video_path, mediainfo, meta)
        if candidate.source == "OpenSubtitles":
            return self._download_opensubtitles(candidate, video_path, mediainfo, meta)
        if candidate.download_url:
            return self._download_url(candidate.download_url, video_path, mediainfo, meta)
        return None

    def _download_assrt(self, candidate: SubtitleCandidate, video_path: Path,
                        mediainfo: Any = None, meta: Any = None) -> Optional[Path]:
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
        file_entries = detail.get("filelist") or []
        file_urls = [f.get("url") for f in file_entries if f.get("url")]
        supported_file_urls = self._assrt_supported_file_urls(file_entries)
        if file_urls and not supported_file_urls:
            logger.info(f"ASSRT 字幕详情仅包含不支持的字幕文件，跳过候选，ID：{sub_id}")
            return None
        urls = supported_file_urls
        if not file_urls and detail.get("url"):
            urls.append(detail.get("url"))
        urls = self._unique_urls(urls)
        if not urls:
            logger.info(f"ASSRT 字幕详情未返回下载地址，ID：{sub_id}")
            return None
        attempted = 0
        for url in urls:
            if self._unsupported_subtitle_url_suffix(url):
                logger.info(f"跳过不支持的 ASSRT 字幕文件格式，ID：{sub_id}，地址：{self._safe_url_for_log(url)}")
                continue
            if self._download_failed_url_cache_hit(url):
                logger.info(f"跳过近期下载失败的 ASSRT 字幕地址，ID：{sub_id}，地址：{self._safe_url_for_log(url)}")
                continue
            if attempted >= self._assrt_candidate_url_attempt_limit:
                logger.info(f"ASSRT 候选下载地址达到尝试上限，跳过剩余地址，ID：{sub_id}")
                break
            attempted += 1
            logger.info(f"开始下载 ASSRT 字幕文件，ID：{sub_id}，地址：{self._safe_url_for_log(url)}")
            saved = self._download_url(url, video_path, mediainfo, meta)
            if saved:
                return saved
            logger.info(f"ASSRT 字幕文件下载未成功，ID：{sub_id}，地址：{self._safe_url_for_log(url)}")
        return None

    def _assrt_supported_file_urls(self, file_entries: List[dict]) -> List[str]:
        urls = []
        for entry in file_entries:
            url = entry.get("url")
            if not url:
                continue
            file_name = entry.get("f") or entry.get("filename") or entry.get("name") or ""
            if file_name and self._unsupported_subtitle_file_reference(file_name):
                continue
            if self._unsupported_subtitle_url_suffix(url):
                continue
            urls.append(url)
        return self._unique_urls(urls)

    @staticmethod
    def _unique_urls(urls: List[str]) -> List[str]:
        unique_urls = []
        seen_urls = set()
        for url in urls:
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            unique_urls.append(url)
        return unique_urls

    def _download_opensubtitles(self, candidate: SubtitleCandidate, video_path: Path,
                                mediainfo: Any = None, meta: Any = None) -> Optional[Path]:
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
        return self._download_url(link, video_path, mediainfo, meta)

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
        if type(self)._scan_active and "opensubtitles" in type(self)._scan_disabled_sources:
            return True
        quota = self._opensubtitles_download_quota()
        if quota["count"] < self._opensubtitles_daily_limit:
            return False
        logger.warn(
            f"OpenSubtitles 今日下载额度已用完：{quota['count']}/{self._opensubtitles_daily_limit}，跳过 OpenSubtitles"
        )
        self._disable_source_for_scan("opensubtitles", "今日下载额度已用完")
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

    def _download_url(self, url: str, video_path: Path, mediainfo: Any = None, meta: Any = None) -> Optional[Path]:
        if self._unsupported_subtitle_url_suffix(url):
            logger.info(f"跳过不支持的字幕文件格式，地址：{self._safe_url_for_log(url)}")
            return None
        if self._download_failed_url_cache_hit(url):
            logger.info(f"跳过近期下载失败的字幕地址：{self._safe_url_for_log(url)}")
            return None
        response = self._download_response(url)
        if not response or response.get("status_code") != 200:
            status_code = response.get("status_code") if response else "无响应"
            logger.warn(f"字幕文件下载失败，状态：{status_code}，地址：{self._safe_url_for_log(url)}")
            self._record_download_failed_url(url)
            return None
        content = response.get("content") or b""
        if not content:
            logger.warn(f"字幕文件下载内容为空或超限，地址：{self._safe_url_for_log(url)}")
            self._record_download_failed_url(url)
            return None
        content_type = (response.get("headers", {}).get("Content-Type") or "").lower()
        if zipfile.is_zipfile(io.BytesIO(content)) or "zip" in content_type or url.lower().split("?")[0].endswith(".zip"):
            saved = self._save_from_zip(content, video_path, mediainfo, meta)
            if saved:
                self._clear_download_failed_url(url)
            else:
                self._record_download_failed_url(url)
            return saved
        suffix = self._guess_subtitle_suffix(url, content)
        if suffix not in settings.RMT_SUBEXT:
            self._record_download_failed_url(url)
            return None
        if not self._valid_chinese_subtitle_content(content, suffix):
            logger.warn(f"字幕文件内容校验失败，地址：{self._safe_url_for_log(url)}")
            self._record_download_failed_url(url)
            return None
        if not self._subtitle_timeline_coverage_ok(content, suffix, video_path, mediainfo, meta):
            logger.warn(f"字幕文件时间轴覆盖不足，地址：{self._safe_url_for_log(url)}")
            self._record_download_failed_url(url)
            return None
        target = self._target_subtitle_path(video_path, suffix)
        target.write_bytes(content)
        self._clear_download_failed_url(url)
        return target

    def _download_timeout(self) -> int:
        return max(1, min(int(self._timeout or 20), 15))

    def _download_deadline_seconds(self) -> int:
        return self._download_timeout() + self._subtitle_download_deadline_extra_seconds

    def _download_response(self, url: str) -> Optional[dict]:
        result_queue = queue.Queue(maxsize=1)

        def fetch_response():
            res = None
            try:
                res = RequestUtils(timeout=self._download_timeout()).get_res(url, stream=True)
                if res is None:
                    result_queue.put({"status_code": None, "headers": {}, "content": b""})
                    return
                result_queue.put({
                    "status_code": res.status_code,
                    "headers": dict(getattr(res, "headers", {}) or {}),
                    "content": self._response_content(res),
                })
            except Exception as err:
                logger.warn(f"字幕文件下载请求异常：{err}，地址：{self._safe_url_for_log(url)}")
                result_queue.put({"status_code": None, "headers": {}, "content": b""})
            finally:
                self._close_response(res)

        download_thread = threading.Thread(target=fetch_response, daemon=True)
        download_thread.start()
        download_thread.join(timeout=self._download_deadline_seconds())
        if download_thread.is_alive():
            logger.warn(
                f"字幕文件下载超过总耗时限制 {self._download_deadline_seconds()} 秒，"
                f"跳过地址：{self._safe_url_for_log(url)}"
            )
            return None
        try:
            return result_queue.get_nowait()
        except queue.Empty:
            return None

    def _response_content(self, res) -> bytes:
        if not hasattr(res, "iter_content"):
            content = getattr(res, "content", b"") or b""
            return b"" if len(content) > self._subtitle_download_max_bytes else content
        chunks = []
        total_size = 0
        try:
            for chunk in res.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total_size += len(chunk)
                if total_size > self._subtitle_download_max_bytes:
                    return b""
                chunks.append(chunk)
        except Exception as err:
            logger.warn(f"读取字幕下载响应失败：{err}")
            return b""
        return b"".join(chunks)

    @staticmethod
    def _close_response(res):
        try:
            if res is not None and hasattr(res, "close"):
                res.close()
        except Exception as err:
            logger.debug(f"关闭字幕下载响应失败：{err}")

    @staticmethod
    def _safe_url_for_log(url: str) -> str:
        parsed = urlparse(url or "")
        if not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    @staticmethod
    def _unsupported_subtitle_url_suffix(url: str) -> bool:
        url_suffix = Path(unquote(urlparse(url or "").path)).suffix.lower()
        return bool(url_suffix and url_suffix != ".zip" and url_suffix not in settings.RMT_SUBEXT)

    @staticmethod
    def _unsupported_subtitle_file_reference(text: str) -> bool:
        file_suffix = Path(unquote(urlparse(text or "").path or text or "")).suffix.lower()
        return bool(file_suffix and file_suffix != ".zip" and file_suffix not in settings.RMT_SUBEXT)

    def _assrt_backoff_seconds(self, res) -> int:
        retry_after = (res.headers.get("Retry-After") or "").strip() if getattr(res, "headers", None) else ""
        if retry_after.isdigit():
            return max(10, min(int(retry_after), 300))
        return max(30, min(self._assrt_interval * 10, 300))

    def _save_from_zip(self, content: bytes, video_path: Path,
                       mediainfo: Any = None, meta: Any = None) -> Optional[Path]:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            members = [m for m in zf.infolist() if Path(m.filename).suffix.lower() in settings.RMT_SUBEXT]
            if not members:
                return None
            scored = sorted(
                members,
                key=lambda m: (
                    self._release_match_score(video_path.name, Path(m.filename).name)
                    + self._release_feature_score(video_path.name, Path(m.filename).name)
                    + self._bilingual_sort_bonus(Path(m.filename).name)
                ),
                reverse=True,
            )
            for member in scored:
                suffix = Path(member.filename).suffix.lower()
                with zf.open(member) as src:
                    content = src.read()
                if not self._valid_chinese_subtitle_content(content, suffix):
                    logger.info(f"跳过压缩包内无效字幕文件：{member.filename}")
                    continue
                if not self._subtitle_timeline_coverage_ok(content, suffix, video_path, mediainfo, meta):
                    logger.info(f"跳过压缩包内时间轴覆盖不足字幕文件：{member.filename}")
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
        if self._valid_existing_subtitle(subtitle_path, video_path):
            if self._should_upgrade_existing_subtitle(subtitle_path):
                logger.info(f"已有中文字幕非双语，继续搜索中英双语字幕：{subtitle_path}")
                return False
            return True
        logger.warn(f"已有中文字幕无效，重新搜索：{subtitle_path}")
        return False

    def _existing_subtitle_path(self, video_path: Path) -> Optional[Path]:
        for suffix in settings.RMT_SUBEXT:
            for subtitle_path in (video_path.with_suffix(suffix), self._target_subtitle_path(video_path, suffix)):
                if subtitle_path.exists():
                    return subtitle_path
        return None

    def _valid_existing_subtitle(self, subtitle_path: Path, video_path: Path) -> bool:
        suffix = subtitle_path.suffix.lower()
        if suffix not in settings.RMT_SUBEXT:
            return False
        try:
            content = subtitle_path.read_bytes()
            return (
                    self._valid_chinese_subtitle_content(content, suffix)
                    and self._subtitle_timeline_coverage_ok(content, suffix, video_path)
            )
        except Exception as err:
            logger.warn(f"读取已有中文字幕失败：{subtitle_path} - {err}")
            return False

    def _should_upgrade_existing_subtitle(self, subtitle_path: Path) -> bool:
        if not self._prefer_bilingual or not self._upgrade_existing_to_bilingual:
            return False
        if self._looks_chinese_english_bilingual(subtitle_path.name):
            return False
        try:
            return not self._bilingual_subtitle_content(subtitle_path.read_bytes())
        except Exception as err:
            logger.warn(f"检查已有字幕双语内容失败：{subtitle_path} - {err}")
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

    def _rank_candidates(self, candidates: List[SubtitleCandidate]) -> List[SubtitleCandidate]:
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
        return sorted(unique_candidates, key=self._candidate_sort_score, reverse=True)

    def _downloadable_candidates(self, candidates: List[SubtitleCandidate]) -> List[SubtitleCandidate]:
        ranked_candidates = self._rank_candidates(candidates)
        if not ranked_candidates:
            return []
        top_score = self._candidate_sort_score(ranked_candidates[0])
        bilingual_scores = [
            self._candidate_sort_score(candidate)
            for candidate in ranked_candidates
            if self._looks_chinese_english_bilingual(self._candidate_bilingual_text(candidate))
        ]
        best_bilingual_score = max(bilingual_scores) if bilingual_scores else 0
        downloadable = []
        for candidate in ranked_candidates:
            if not self._candidate_download_filter(candidate, top_score, best_bilingual_score):
                continue
            downloadable.append(candidate)
            if len(downloadable) >= self._max_candidates:
                break
        return downloadable

    def _candidate_download_filter(self, candidate: SubtitleCandidate, top_score: float,
                                   best_bilingual_score: float = 0) -> bool:
        score = self._candidate_sort_score(candidate)
        if score < self._candidate_download_score_floor and score < top_score:
            return False
        if not self._prefer_bilingual or not best_bilingual_score:
            return True
        if self._looks_chinese_english_bilingual(self._candidate_bilingual_text(candidate)):
            return True
        return score >= best_bilingual_score + self._non_bilingual_keep_margin

    def _candidate_sort_score(self, candidate: SubtitleCandidate) -> float:
        return candidate.score + self._bilingual_sort_bonus(self._candidate_bilingual_text(candidate))

    @staticmethod
    def _candidate_bilingual_text(candidate: SubtitleCandidate) -> str:
        raw = candidate.raw if isinstance(candidate.raw, dict) else {}
        raw_attrs = raw.get("attributes") or {}
        raw_file = next(iter(raw_attrs.get("files") or []), {}) if isinstance(raw_attrs.get("files"), list) else {}
        return " ".join(str(value or "") for value in (
            candidate.title,
            candidate.file_name,
            candidate.language,
            raw.get("native_name"),
            raw.get("videoname"),
            raw.get("release_name"),
            raw.get("subtitle_name"),
            raw.get("name"),
            raw.get("comment"),
            raw_attrs.get("release"),
            raw_attrs.get("language"),
            raw_file.get("file_name"),
        ))

    @classmethod
    def _bilingual_preference_bonus(cls, text: str) -> int:
        return cls._bilingual_preference_score if cls._looks_chinese_english_bilingual(text) else 0

    def _bilingual_sort_bonus(self, text: str) -> int:
        if not self._prefer_bilingual:
            return 0
        return self._bilingual_preference_bonus(text)

    @staticmethod
    def _looks_chinese_english_bilingual(text: str) -> bool:
        text = text or ""
        return bool(re.search(
            r"中英|英中|中\s*[/&+._ -]\s*英|英\s*[/&+._ -]\s*中|"
            r"简英|繁英|简体.*英|繁体.*英|双语|bilingual|dual\s*sub|"
            r"chs\s*[/&+._ -]?\s*(eng|en)|cht\s*[/&+._ -]?\s*(eng|en)|"
            r"chi(nese)?\s*[/&+._ -]\s*eng(lish)?|"
            r"zh\s*[/&+._ -]\s*en|cn\s*[/&+._ -]\s*en",
            text,
            re.IGNORECASE,
        ))

    def _assrt_queries(self, video_path: Path, mediainfo: Any, meta: Any) -> List[str]:
        queries = []
        episode_texts = [""]
        if self._is_tv(mediainfo, meta):
            season, episode = self._season_episode_numbers(video_path, mediainfo, meta)
            if not season or not episode:
                return []
            episode_texts = self._assrt_episode_query_texts(season, episode)
        for title in self._assrt_query_titles(video_path, mediainfo, meta):
            for episode_text in episode_texts:
                query = f"{title} {episode_text}" if episode_text else title
                query = re.sub(r"\s+", " ", query).strip()
                if query and query not in queries:
                    queries.append(query)
        return queries[:3] if self._is_tv(mediainfo, meta) else queries

    def _assrt_season_queries(self, video_path: Path, mediainfo: Any, meta: Any, season: int) -> List[str]:
        queries = []
        season_texts = [f"S{int(season):02d}"]
        if int(season) > 1:
            season_texts.insert(1, f"第{int(season)}季")
        for title in self._assrt_query_titles(video_path, mediainfo, meta):
            for season_text in season_texts:
                query = re.sub(r"\s+", " ", f"{title} {season_text}").strip()
                if query and query not in queries:
                    queries.append(query)
        return queries[:3]

    def _assrt_episode_query_texts(self, season: Optional[int], episode: Optional[int]) -> List[str]:
        texts = []
        if season and episode:
            texts.append(f"S{int(season):02d}E{int(episode):02d}")
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
                               target_release_text: str, query: str, match_text: str) -> Optional[float]:
        if self._short_chinese_title_embedded_in_long_phrase(target_title, match_text):
            return None
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
        release_feature_score = self._release_feature_score(target_release_text, match_text)
        return title_score + year_score + resolution_score + release_feature_score

    @classmethod
    def _short_chinese_title_embedded_in_long_phrase(cls, target_title: str, match_text: str) -> bool:
        title = cls._compact_chinese(cls._clean_query_title(target_title))
        if not 2 <= len(title) <= 4:
            return False
        for chunk in re.findall(r"[\u4e00-\u9fff]+", match_text or ""):
            compact_chunk = cls._compact_chinese(chunk)
            if compact_chunk == title:
                return False
            if title in compact_chunk and not compact_chunk.startswith(title) and not compact_chunk.endswith(title):
                return True
        return False

    @staticmethod
    def _compact_chinese(text: str) -> str:
        return "".join(re.findall(r"[\u4e00-\u9fff]", text or ""))

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

    def _release_feature_score(self, video_name: str, release_name: Optional[str]) -> float:
        if not release_name:
            return 0
        video_features = self._release_features(video_name)
        release_features = self._release_features(release_name)
        score = 0
        for feature_name in ("resolution", "source", "hdr", "codec", "audio", "fps"):
            video_values = video_features.get(feature_name) or set()
            release_values = release_features.get(feature_name) or set()
            if not video_values or not release_values:
                continue
            if video_values & release_values:
                score += self._release_feature_match_score
            else:
                score -= self._release_feature_mismatch_penalty
        video_groups = video_features.get("group") or set()
        release_groups = release_features.get("group") or set()
        if video_groups and release_groups:
            if video_groups & release_groups:
                score += self._release_group_match_score
            else:
                score -= self._release_group_mismatch_penalty
        return score

    @classmethod
    def _release_features(cls, text: str) -> Dict[str, Set[str]]:
        normalized = cls._normalized_release_text(text)
        features: Dict[str, Set[str]] = {
            "resolution": set(),
            "source": set(),
            "hdr": set(),
            "codec": set(),
            "audio": set(),
            "fps": set(),
            "group": set(),
        }
        for resolution in re.findall(r"\b(720p|1080p|2160p|4k|8k)\b", normalized, re.IGNORECASE):
            features["resolution"].add(cls._normalize_resolution(resolution))
        source_patterns = {
            "webdl": r"\bweb\s*dl\b",
            "webrip": r"\bweb\s*rip\b",
            "bluray": r"\bblu\s*ray\b|\bbluray\b|\bbd\s*rip\b|\bbr\s*rip\b",
            "hdtv": r"\bhdtv\b",
            "remux": r"\bremux\b",
        }
        for source, pattern in source_patterns.items():
            if re.search(pattern, normalized, re.IGNORECASE):
                features["source"].add(source)
        hdr_patterns = {
            "hdr": r"\bhdr(?:10(?:plus|\+)?)?\b",
            "dv": r"\bdv\b|\bdolby\s*vision\b",
        }
        for hdr, pattern in hdr_patterns.items():
            if re.search(pattern, normalized, re.IGNORECASE):
                features["hdr"].add(hdr)
        codec_patterns = {
            "x264": r"\bx264\b|\bh\.?264\b|\bavc\b",
            "x265": r"\bx265\b|\bh\.?265\b|\bhevc\b",
        }
        for codec, pattern in codec_patterns.items():
            if re.search(pattern, normalized, re.IGNORECASE):
                features["codec"].add(codec)
        audio_patterns = {
            "aac": r"\baac\b",
            "ac3": r"\bac3\b|\beac3\b|\bddp?\s*5?\.?1?\b",
            "dts": r"\bdts\b",
            "truehd": r"\btruehd\b",
            "atmos": r"\batmos\b",
        }
        for audio, pattern in audio_patterns.items():
            if re.search(pattern, normalized, re.IGNORECASE):
                features["audio"].add(audio)
        for fps in re.findall(r"\b(23\.976|24|25|29\.970|30|50|60)\s*fps\b", normalized, re.IGNORECASE):
            features["fps"].add(fps)
        release_group = cls._release_group(text)
        if release_group:
            features["group"].add(release_group)
        return features

    @staticmethod
    def _normalized_release_text(text: str) -> str:
        text = Path(text or "").name
        text = re.sub(r"[_+.]+", " ", text)
        text = re.sub(r"[-]+", " ", text)
        return re.sub(r"\s+", " ", text).strip().lower()

    @staticmethod
    def _release_group(text: str) -> str:
        stem = Path(text or "").name
        stem = re.sub(r"\.(mkv|mp4|avi|mov|wmv|m2ts|ts|srt|ass|ssa|zip)$", "", stem, flags=re.IGNORECASE)
        match = re.search(r"[-. ]([A-Za-z0-9]{2,20})$", stem)
        if not match:
            return ""
        group = match.group(1).lower()
        if group in {
            "webdl", "webrip", "bluray", "bdrip", "brrip", "hdtv", "remux",
            "x264", "x265", "h264", "h265", "hevc", "aac", "dts", "truehd",
        }:
            return ""
        return group

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

    def _subtitle_timeline_coverage_ok(self, content: bytes, suffix: str, video_path: Path,
                                       mediainfo: Any = None, meta: Any = None) -> bool:
        duration_seconds = self._media_duration_seconds(video_path, mediainfo, meta)
        if not duration_seconds or duration_seconds < self._subtitle_coverage_min_duration_seconds:
            return True
        timeline_range = self._subtitle_timeline_range(content, suffix)
        if not timeline_range:
            logger.info(f"字幕时间轴解析失败：{video_path.name}")
            return False
        first_start, last_end, cue_count = timeline_range
        start_limit = min(
            duration_seconds * self._subtitle_coverage_start_max_ratio,
            self._subtitle_coverage_start_max_seconds,
        )
        coverage_seconds = max(0, last_end - first_start)
        if (
                first_start > start_limit
                or last_end < duration_seconds * self._subtitle_coverage_end_min_ratio
                or coverage_seconds < duration_seconds * self._subtitle_coverage_min_ratio
        ):
            logger.info(
                f"字幕时间轴覆盖不足：{video_path.name} "
                f"start={first_start:.1f}s end={last_end:.1f}s "
                f"duration={duration_seconds:.1f}s cues={cue_count}"
            )
            return False
        return True

    def _media_duration_seconds(self, video_path: Optional[Path], mediainfo: Any = None,
                                meta: Any = None) -> Optional[float]:
        for container in (mediainfo, meta):
            for attr_name in ("runtime", "run_time", "duration_minutes"):
                seconds = self._duration_value_seconds(getattr(container, attr_name, None), default_unit="minutes")
                if seconds:
                    return seconds
            for attr_name in ("duration_seconds", "duration", "video_duration"):
                seconds = self._duration_value_seconds(getattr(container, attr_name, None), default_unit="seconds")
                if seconds:
                    return seconds
        if video_path:
            nfo_mediainfo = self._mediainfo_from_local_nfo(video_path)
            if nfo_mediainfo and nfo_mediainfo is not mediainfo:
                return self._duration_value_seconds(getattr(nfo_mediainfo, "runtime", None), default_unit="minutes")
        return None

    @classmethod
    def _duration_value_seconds(cls, value: Any, default_unit: str = "seconds") -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            text = value.strip().lower()
            timestamp_seconds = cls._subtitle_timestamp_seconds(text)
            if timestamp_seconds is not None and ":" in text:
                return timestamp_seconds
            match = re.search(r"\d+(?:\.\d+)?", text)
            if not match:
                return None
            amount = float(match.group())
            if re.search(r"毫秒|milliseconds?|ms\b", text):
                return amount / 1000
            if re.search(r"小时|hours?|hrs?|h\b", text):
                return amount * 3600
            if re.search(r"分钟|分|minutes?|mins?|m\b", text):
                return amount * 60
            if re.search(r"秒|seconds?|secs?|s\b", text):
                return amount
        else:
            try:
                amount = float(value)
            except (TypeError, ValueError):
                return None
        if amount <= 0:
            return None
        if default_unit == "minutes" and amount < 1000:
            return amount * 60
        if default_unit == "milliseconds" or amount > 360000:
            return amount / 1000
        return amount

    @classmethod
    def _subtitle_timeline_range(cls, content: bytes, suffix: str) -> Optional[Tuple[float, float, int]]:
        text = cls._decode_subtitle_text(content)
        if not text:
            return None
        if suffix in {".ass", ".ssa"}:
            return cls._ass_subtitle_timeline_range(text)
        return cls._srt_subtitle_timeline_range(text)

    @classmethod
    def _srt_subtitle_timeline_range(cls, text: str) -> Optional[Tuple[float, float, int]]:
        ranges = []
        for match in re.finditer(
                r"(?m)(\d+:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{1,3})",
                text,
        ):
            start = cls._subtitle_timestamp_seconds(match.group(1))
            end = cls._subtitle_timestamp_seconds(match.group(2))
            if start is None or end is None or end <= start:
                continue
            ranges.append((start, end))
        return cls._timeline_bounds(ranges)

    @classmethod
    def _ass_subtitle_timeline_range(cls, text: str) -> Optional[Tuple[float, float, int]]:
        ranges = []
        format_fields = []
        for line in text.splitlines():
            key, separator, value = line.partition(":")
            if not separator:
                continue
            key = key.strip().lower()
            value = value.strip()
            if key == "format":
                fields = [field.strip().lower() for field in value.split(",")]
                if "start" in fields and "end" in fields:
                    format_fields = fields
                continue
            if key != "dialogue":
                continue
            start_index = format_fields.index("start") if "start" in format_fields else 1
            end_index = format_fields.index("end") if "end" in format_fields else 2
            maxsplit = max(len(format_fields) - 1, start_index, end_index)
            parts = value.split(",", maxsplit)
            if len(parts) <= max(start_index, end_index):
                continue
            start = cls._subtitle_timestamp_seconds(parts[start_index].strip())
            end = cls._subtitle_timestamp_seconds(parts[end_index].strip())
            if start is None or end is None or end <= start:
                continue
            ranges.append((start, end))
        return cls._timeline_bounds(ranges)

    @staticmethod
    def _timeline_bounds(ranges: List[Tuple[float, float]]) -> Optional[Tuple[float, float, int]]:
        if not ranges:
            return None
        return min(start for start, _ in ranges), max(end for _, end in ranges), len(ranges)

    @staticmethod
    def _subtitle_timestamp_seconds(value: str) -> Optional[float]:
        match = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,2}):(\d{2})(?:[,.](\d{1,3}))?\s*", value or "")
        if not match:
            return None
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        fraction_text = match.group(4) or ""
        fraction = int(fraction_text) / (10 ** len(fraction_text)) if fraction_text else 0
        return hours * 3600 + minutes * 60 + seconds + fraction

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

    @classmethod
    def _bilingual_subtitle_content(cls, content: bytes) -> bool:
        text = cls._decode_subtitle_text(content[:200000])
        if not text or not re.search(r"[\u4e00-\u9fff]", text):
            return False
        dialogue_lines = [
            line for line in text.splitlines()
            if re.search(r"[\u4e00-\u9fff]", line) and re.search(r"[A-Za-z]{2,}", line)
        ]
        return len(dialogue_lines) >= 2 or cls._looks_chinese_english_bilingual(text[:2000])

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
                    col(field("source_order", "字幕源顺序", placeholder="assrt,opensubtitles,subdl"), md=4),
                    col(field("max_candidates", "每源尝试数量", type="number"), md=2),
                    col(field("timeout", "请求超时秒数", type="number"), md=2),
                    col(switch("prefer_bilingual", "优先中英双语"), md=2),
                    col(switch("upgrade_existing_to_bilingual", "纯中文字幕升级双语"), md=2),
                ),
                row(
                    col(switch("scan_enable", "启用目录扫描"), md=2),
                    col(switch("scan_system_library_dirs", "扫描系统媒体库目录"), md=3),
                    col(field("scan_cron", "扫描 Cron", placeholder="0 4 * * *"), md=3),
                    col(field("scan_limit", "单次最多尝试视频数", type="number"), md=2),
                    col(field("scan_miss_ttl_hours", "未命中冷却小时", type="number"), md=2),
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
            "prefer_bilingual": True,
            "upgrade_existing_to_bilingual": True,
            "max_candidates": 5,
            "timeout": 20,
            "scan_enable": False,
            "scan_system_library_dirs": True,
            "scan_cron": "0 4 * * *",
            "scan_dirs": "",
            "scan_limit": 50,
            "scan_miss_ttl_hours": 24,
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
