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
from typing import Any, Dict, List, Optional, Tuple
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
    source: str
    title: str
    file_name: str = ""
    language: str = ""
    score: float = 0
    download_url: str = ""
    file_id: Optional[int] = None
    raw: Optional[dict] = None


class ChineseSubtitle(_PluginBase):
    plugin_name = "中文字幕下载"
    plugin_desc = "媒体整理完成后，自动从 ASSRT、OpenSubtitles、SubDL 搜索并下载中文字幕。"
    plugin_icon = "subtitle.png"
    plugin_version = "1.2.9"
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

    _sources = {
        "assrt": "_assrt_token",
        "opensubtitles": "_opensubtitles_api_key",
        "subdl": "_subdl_api_key",
    }
    _opensubtitles_quota_key = "opensubtitles_download_quota"

    def init_plugin(self, config: dict = None):
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
        return self._enable

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_page(self) -> Optional[List[dict]]:
        return None

    def stop_service(self):
        pass

    def get_service(self) -> List[Dict[str, Any]]:
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
        with type(self)._subtitle_task_lock:
            if not self._enable or not self._scan_enable:
                return
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
        for source in self._enabled_sources():
            try:
                candidates = self._search_source(source, video_path, mediainfo, meta)
                if not candidates:
                    logger.info(f"{source} 未找到匹配中文字幕：{video_path.name}")
                    continue
                for candidate in candidates[: self._max_candidates]:
                    logger.info(f"尝试下载中文字幕候选：{video_path.name} - {candidate.source} - {candidate.title}")
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
            except Exception as err:
                logger.error(f"{source} 中文字幕处理失败：{err}", exc_info=True)
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
            if item.is_file() and item.suffix.lower() in settings.RMT_MEDIAEXT:
                yield item

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
        title = self._first_value(
            self._nfo_first_text(root, "title"),
            self._nfo_first_text(root, "originaltitle"),
            self._nfo_first_text(root, "showtitle"),
        )
        original_title = self._nfo_first_text(root, "originaltitle")
        year = self._nfo_year(root)
        return MediaInfo(
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
            return int(str(value).strip())
        except (TypeError, ValueError):
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
            if getattr(self, token_attr, None):
                sources.append(source)
        return sources

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

    def _search_assrt_by_query(self, video_path: Path, target_title: str, target_year: str,
                               target_resolution: str, query: str) -> List[SubtitleCandidate]:
        seen_ids = set()
        params = {
            "token": self._assrt_token,
            "q": query,
            "cnt": 15,
            "pos": 0,
            "is_file": 0,
        }
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
                    + min(float(item.get("down_count") or 0), 10000) / 100
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
            episode = self._episode(meta)
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
                score_penalty = 100
            else:
                score_penalty = 0
            file_info = files[0]
            score = float(attrs.get("download_count") or 0) + float(attrs.get("ratings") or 0) * 100 - score_penalty
            score += self._release_match_score(video_path.name, file_info.get("file_name"))
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
            episode = self._episode(meta)
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
            score = self._release_match_score(video_path.name, release)
            if item.get("hi"):
                score -= 10
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
            interval_wait = 0
            if self._assrt_interval > 0 and cls._assrt_last_request_time:
                interval_wait = self._assrt_interval - (now - cls._assrt_last_request_time)
            wait_seconds = max(
                cls._assrt_backoff_until - now,
                interval_wait,
            )
            if wait_seconds > 0:
                logger.info(f"ASSRT 请求节流等待 {wait_seconds:.1f} 秒")
                time.sleep(wait_seconds)
            res = RequestUtils(timeout=self._timeout).get_res(url, params=params)
            cls._assrt_last_request_time = time.time()
            if res is not None and res.status_code == 509:
                backoff_seconds = self._assrt_backoff_seconds(res)
                cls._assrt_backoff_until = time.time() + backoff_seconds
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
                target = self._target_subtitle_path(video_path, suffix)
                with zf.open(member) as src:
                    target.write_bytes(src.read())
                return target
        return None

    def _target_subtitle_path(self, video_path: Path, suffix: str) -> Path:
        lang_suffix = self._language_suffix
        if lang_suffix and not lang_suffix.startswith("."):
            lang_suffix = f".{lang_suffix}"
        return video_path.with_name(f"{video_path.stem}{lang_suffix}{suffix}")

    def _has_existing_subtitle(self, video_path: Path) -> bool:
        for suffix in settings.RMT_SUBEXT:
            if video_path.with_suffix(suffix).exists():
                return True
            if self._target_subtitle_path(video_path, suffix).exists():
                return True
        return False

    @staticmethod
    def _looks_chinese(text: str) -> bool:
        return bool(re.search(r"中|简|繁|双语|字幕组|人人|YYeTs|CHS|CHT|Chinese|zh", text, re.IGNORECASE))

    def _assrt_queries(self, video_path: Path, mediainfo: Any, meta: Any) -> List[str]:
        queries = []
        season_episode = ""
        if self._is_tv(mediainfo, meta):
            season_episode = self._season_episode_from_path(video_path) or self._season_episode_from_meta(mediainfo, meta)
        for title in self._title_candidates(video_path, mediainfo, meta) or [video_path.stem]:
            query = f"{title} {season_episode}" if season_episode else title
            query = re.sub(r"\s+", " ", query).strip()
            if query and query not in queries:
                queries.append(query)
        return queries

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
        episode = self._episode(meta)
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
        if query in text or text in query:
            return 100
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
    def _episode(meta: Any) -> Optional[int]:
        return getattr(meta, "begin_episode", None)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
