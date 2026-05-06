# -*- coding: utf-8 -*-
import io
import re
import time
import zipfile
from difflib import SequenceMatcher
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from apscheduler.triggers.cron import CronTrigger

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
    plugin_version = "1.2.2"
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
    _subdl_api_key: str = ""
    _subdl_languages: str = "ZH,ZH_CN,ZH_TW"

    _opensubtitles_token: str = ""
    _opensubtitles_token_time: float = 0
    _assrt_last_request_time: float = 0

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enable = config.get("enable", False)
        self._overwrite = config.get("overwrite", False)
        self._notify = config.get("notify", False)
        self._language_suffix = (config.get("language_suffix") or ".zh-CN").strip()
        self._timeout = int(config.get("timeout") or 20)
        self._source_order = (config.get("source_order") or "assrt,opensubtitles,subdl").strip()
        self._max_candidates = int(config.get("max_candidates") or 5)
        self._scan_enable = config.get("scan_enable", False)
        self._scan_system_library_dirs = config.get("scan_system_library_dirs", True)
        self._scan_cron = (config.get("scan_cron") or "0 4 * * *").strip()
        self._scan_dirs = (config.get("scan_dirs") or "").strip()
        self._scan_limit = int(config.get("scan_limit") or 50)
        self._assrt_token = (config.get("assrt_token") or "").strip()
        self._assrt_interval = max(0, int(config.get("assrt_interval") or 3))
        self._opensubtitles_api_key = (config.get("opensubtitles_api_key") or "").strip()
        self._opensubtitles_username = (config.get("opensubtitles_username") or "").strip()
        self._opensubtitles_password = (config.get("opensubtitles_password") or "").strip()
        self._opensubtitles_languages = (config.get("opensubtitles_languages") or "zh-cn,zh-tw,ze").strip()
        self._subdl_api_key = (config.get("subdl_api_key") or "").strip()
        self._subdl_languages = (config.get("subdl_languages") or "ZH,ZH_CN,ZH_TW").strip()

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
            "kwargs": {},
        }]

    @eventmanager.register(EventType.TransferComplete)
    def transfer_complete(self, event: Event):
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
                if self._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local"):
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

    def _enabled_sources(self) -> List[str]:
        sources = []
        for source in re.split(r"[,，\s]+", self._source_order.lower()):
            if source in {"assrt", "opensubtitles", "subdl"} and source not in sources:
                if source == "assrt" and not self._assrt_token:
                    continue
                if source == "opensubtitles" and not self._opensubtitles_api_key:
                    continue
                if source == "subdl" and not self._subdl_api_key:
                    continue
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
        seen_ids = set()
        target_title = self._target_title(video_path, mediainfo, meta)
        target_year = self._target_year(video_path, mediainfo, meta)
        target_resolution = self._target_resolution(video_path)
        query = self._assrt_query(video_path, mediainfo, meta)
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
        return sorted(candidates, key=lambda x: x.score, reverse=True)

    def _search_opensubtitles(self, video_path: Path, mediainfo: Any, meta: Any) -> List[SubtitleCandidate]:
        headers = self._opensubtitles_headers()
        params = {
            "languages": self._opensubtitles_languages,
            "order_by": "download_count",
            "order_direction": "desc",
        }
        imdb_id = self._imdb_id(mediainfo)
        if imdb_id:
            params["imdb_id"] = imdb_id
        elif getattr(mediainfo, "tmdb_id", None):
            params["tmdb_id"] = getattr(mediainfo, "tmdb_id")
        else:
            params["query"] = self._query_title(video_path, mediainfo, meta)
        if self._is_tv(mediainfo, meta):
            season = self._season(mediainfo, meta)
            episode = self._episode(meta)
            if season:
                params["season_number"] = season
            if episode:
                params["episode_number"] = episode
        movie_hash = self._opensubtitles_hash(video_path)
        if movie_hash:
            params["moviehash"] = movie_hash

        res = RequestUtils(headers=headers, timeout=self._timeout).get_res(
            "https://api.opensubtitles.com/api/v1/subtitles", params=params
        )
        if not res or res.status_code != 200:
            return []
        data = res.json()
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
        imdb_id = getattr(mediainfo, "imdb_id", None)
        if imdb_id:
            params["imdb_id"] = str(imdb_id)
        if getattr(mediainfo, "tmdb_id", None):
            params["tmdb_id"] = getattr(mediainfo, "tmdb_id")
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
        urls = [f.get("url") for f in detail.get("filelist") or [] if f.get("url")]
        if detail.get("url"):
            urls.append(detail.get("url"))
        if not urls:
            logger.info(f"ASSRT 字幕详情未返回下载地址，ID：{sub_id}")
            return None
        for url in urls:
            logger.info(f"开始下载 ASSRT 字幕文件，ID：{sub_id}，地址：{self._safe_url_for_log(url)}")
            saved = self._download_url(url, video_path)
            if saved:
                return saved
            logger.info(f"ASSRT 字幕文件下载未成功，ID：{sub_id}，地址：{self._safe_url_for_log(url)}")
        return None

    def _download_opensubtitles(self, candidate: SubtitleCandidate, video_path: Path) -> Optional[Path]:
        if not candidate.file_id:
            return None
        token = self._get_opensubtitles_token()
        if not token:
            logger.warn("OpenSubtitles 未配置用户名/密码或登录失败，无法下载字幕")
            return None
        headers = self._opensubtitles_headers(token=token)
        res = RequestUtils(headers=headers, timeout=self._timeout).post_res(
            "https://api.opensubtitles.com/api/v1/download",
            json={"file_id": candidate.file_id},
        )
        if not res or res.status_code != 200:
            return None
        link = (res.json() or {}).get("link")
        if not link:
            return None
        return self._download_url(link, video_path)

    def _assrt_get_res(self, url: str, params: dict):
        if self._assrt_interval > 0 and self._assrt_last_request_time:
            wait_seconds = self._assrt_interval - (time.time() - self._assrt_last_request_time)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        res = RequestUtils(timeout=self._timeout).get_res(url, params=params)
        self._assrt_last_request_time = time.time()
        return res

    def _download_url(self, url: str, video_path: Path) -> Optional[Path]:
        url_suffix = Path(urlparse(url or "").path).suffix.lower()
        if url_suffix and url_suffix != ".zip" and url_suffix not in settings.RMT_SUBEXT:
            logger.info(f"跳过不支持的字幕文件格式：{url_suffix}，地址：{self._safe_url_for_log(url)}")
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

    def _assrt_query(self, video_path: Path, mediainfo: Any, meta: Any) -> str:
        query = self._target_title(video_path, mediainfo, meta)
        if self._is_tv(mediainfo, meta):
            season_episode = self._season_episode_from_path(video_path) or self._season_episode_from_meta(mediainfo, meta)
            if season_episode:
                return f"{query} {season_episode}"
        return query

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

    def _target_title(self, video_path: Path, mediainfo: Any, meta: Any) -> str:
        for value in (
                getattr(mediainfo, "en_title", None),
                getattr(mediainfo, "original_title", None),
                getattr(mediainfo, "title", None),
                getattr(meta, "en_name", None),
                getattr(meta, "cn_name", None),
                self._clean_query_title(video_path.parent.name),
                self._clean_query_title(video_path.stem),
        ):
            value = (value or "").strip()
            if value:
                return value
        return video_path.stem

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

    @staticmethod
    def _query_title(video_path: Path, mediainfo: Any, meta: Any) -> str:
        return (
                getattr(mediainfo, "en_title", None)
                or getattr(mediainfo, "title", None)
                or getattr(meta, "en_name", None)
                or getattr(meta, "cn_name", None)
                or video_path.stem
        )

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "enable", "label": "启用插件"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "overwrite", "label": "覆盖已有字幕"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "notify", "label": "下载成功通知"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "language_suffix", "label": "字幕文件语言后缀"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "source_order", "label": "字幕源顺序", "placeholder": "assrt,opensubtitles,subdl"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "max_candidates", "label": "每源尝试数量", "type": "number"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "timeout", "label": "请求超时秒数", "type": "number"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "scan_enable", "label": "启用目录扫描"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "scan_system_library_dirs", "label": "扫描系统媒体库目录"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "scan_cron", "label": "扫描 Cron", "placeholder": "0 4 * * *"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VTextField", "props": {"model": "scan_limit", "label": "单次最多尝试视频数", "type": "number"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextarea", "props": {"model": "scan_dirs", "label": "追加扫描目录", "placeholder": "每行一个 MoviePilot 可访问的本地目录。开启“扫描系统媒体库目录”时，这里可留空。", "rows": 4}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                                {"component": "VTextField", "props": {"model": "assrt_token", "label": "ASSRT Token", "placeholder": "assrt.net 用户面板中的 API Token"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "assrt_interval", "label": "ASSRT 请求间隔秒数", "type": "number"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "opensubtitles_api_key", "label": "OpenSubtitles Api-Key"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "opensubtitles_username", "label": "OpenSubtitles 用户名"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField", "props": {"model": "opensubtitles_password", "label": "OpenSubtitles 密码", "type": "password"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextField", "props": {"model": "opensubtitles_languages", "label": "OpenSubtitles 语言", "placeholder": "zh-cn,zh-tw,ze"}}
                            ]},
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "subdl_api_key", "label": "SubDL API Key"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField", "props": {"model": "subdl_languages", "label": "SubDL 语言", "placeholder": "ZH,ZH_CN,ZH_TW"}}
                            ]},
                        ],
                    },
                ],
            }
        ], {
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
            "subdl_api_key": "",
            "subdl_languages": "ZH,ZH_CN,ZH_TW",
        }
