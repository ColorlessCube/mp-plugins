import importlib.util
import io
import sys
import time
import types
import zipfile
from enum import Enum
from pathlib import Path


class _MediaType(Enum):
    """测试用媒体类型。"""

    MOVIE = "电影"
    TV = "电视剧"


class _EventType(Enum):
    """测试用事件类型。"""

    TransferComplete = "transfer.complete"


class _Logger:
    """测试用日志桩。"""

    def debug(self, *_args, **_kwargs):
        """记录 debug 日志。"""

    def info(self, *_args, **_kwargs):
        """记录 info 日志。"""

    def warn(self, *_args, **_kwargs):
        """记录 warn 日志。"""

    def warning(self, *_args, **_kwargs):
        """记录 warning 日志。"""

    def error(self, *_args, **_kwargs):
        """记录 error 日志。"""


class _EventManager:
    """测试用事件管理器。"""

    @staticmethod
    def register(_event_type):
        """返回原函数的注册装饰器。"""

        def decorator(func):
            """保持被装饰函数不变。"""
            return func

        return decorator


class _PluginBase:
    """测试用插件基类。"""

    def __init__(self):
        """初始化消息与持久化数据桩。"""
        self._data = {}
        self.systemmessage = types.SimpleNamespace(put=lambda **_kwargs: None)

    def get_data(self, key):
        """读取测试持久化数据。"""
        return self._data.get(key)

    def save_data(self, key, value):
        """保存测试持久化数据。"""
        self._data[key] = value


def _load_plugin_module(monkeypatch):
    """加载中文字幕插件并替换 MoviePilot 边界依赖。"""
    settings = types.SimpleNamespace(
        RMT_MEDIAEXT=[".mkv", ".mp4"],
        RMT_SUBEXT=[".srt", ".ass", ".ssa"],
    )
    monkeypatch.setitem(sys.modules, "app.core.context", types.SimpleNamespace(MediaInfo=types.SimpleNamespace))
    monkeypatch.setitem(sys.modules, "app.core.config", types.SimpleNamespace(settings=settings))
    monkeypatch.setitem(
        sys.modules,
        "app.core.event",
        types.SimpleNamespace(Event=object, eventmanager=_EventManager()),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.helper.directory",
        types.SimpleNamespace(DirectoryHelper=lambda: types.SimpleNamespace(get_local_library_dirs=lambda: [])),
    )
    monkeypatch.setitem(sys.modules, "app.log", types.SimpleNamespace(logger=_Logger()))
    monkeypatch.setitem(sys.modules, "app.plugins", types.SimpleNamespace(_PluginBase=_PluginBase))
    monkeypatch.setitem(
        sys.modules,
        "app.schemas.types",
        types.SimpleNamespace(EventType=_EventType, MediaType=_MediaType),
    )
    monkeypatch.setitem(sys.modules, "app.utils.http", types.SimpleNamespace(RequestUtils=object))

    plugin_path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "chinesesubtitle"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("chinesesubtitle_quality_test", plugin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin(module):
    """构造启用状态的测试插件。"""
    plugin = module.ChineseSubtitle()
    plugin.init_plugin({
        "enable": True,
        "assrt_token": "assrt-token",
        "opensubtitles_api_key": "opensubtitles-key",
        "subdl_api_key": "subdl-key",
        "max_candidates": 5,
    })
    return plugin


def test_process_video_ranks_candidates_across_sources(monkeypatch, tmp_path):
    """跨源候选应统一排序后再尝试下载。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.1080p.WEB-DL-GRP.mkv"
    video_path.write_bytes(b"video")
    attempted = []

    def search_source(source, *_args):
        """按源返回不同分数的候选。"""
        if source == "assrt":
            return [module.SubtitleCandidate(source="ASSRT", title="low", score=60)]
        if source == "opensubtitles":
            return [module.SubtitleCandidate(source="OpenSubtitles", title="high", score=140)]
        return []

    def download_candidate(candidate, _video_path):
        """记录实际尝试的候选。"""
        attempted.append(candidate.title)
        if candidate.title == "high":
            return _video_path.with_name(f"{_video_path.stem}.zh-CN.srt")
        return None

    monkeypatch.setattr(plugin, "_enabled_sources", lambda: ["assrt", "opensubtitles"])
    monkeypatch.setattr(plugin, "_download_assrt_season_episode", lambda *_args: None)
    monkeypatch.setattr(plugin, "_search_source", search_source)
    monkeypatch.setattr(plugin, "_download_candidate", download_candidate)
    monkeypatch.setattr(plugin, "_has_existing_subtitle", lambda _video_path: False)

    assert plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")
    assert attempted == ["high"]


def test_process_video_prefers_bilingual_candidate(monkeypatch, tmp_path):
    """中英双语候选应在质量接近时优先尝试。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.1080p.WEB-DL-GRP.mkv"
    video_path.write_bytes(b"video")
    attempted = []

    def search_source(source, *_args):
        """返回普通中文字幕和中英双语候选。"""
        if source == "assrt":
            return [module.SubtitleCandidate(source="ASSRT", title="Movie.2024.简体中文", score=140)]
        if source == "opensubtitles":
            return [module.SubtitleCandidate(source="OpenSubtitles", title="Movie.2024.中英双语", score=90)]
        return []

    def download_candidate(candidate, _video_path):
        """记录实际尝试的候选。"""
        attempted.append(candidate.title)
        return _video_path.with_name(f"{_video_path.stem}.zh-CN.srt")

    monkeypatch.setattr(plugin, "_enabled_sources", lambda: ["assrt", "opensubtitles"])
    monkeypatch.setattr(plugin, "_download_assrt_season_episode", lambda *_args: None)
    monkeypatch.setattr(plugin, "_search_source", search_source)
    monkeypatch.setattr(plugin, "_download_candidate", download_candidate)
    monkeypatch.setattr(plugin, "_has_existing_subtitle", lambda _video_path: False)

    assert plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")
    assert attempted == ["Movie.2024.中英双语"]


def test_process_video_prunes_low_value_download_candidates(monkeypatch, tmp_path):
    """下载阶段应只尝试排序后的高价值候选，避免低分候选拖慢扫描。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    plugin._max_candidates = 2
    video_path = tmp_path / "Movie.2024.1080p.WEB-DL-GRP.mkv"
    video_path.write_bytes(b"video")
    attempted = []

    def search_source(source, *_args):
        """返回多个候选用于验证最终下载尝试数。"""
        if source == "assrt":
            return [
                module.SubtitleCandidate(source="ASSRT", title="best", score=120),
                module.SubtitleCandidate(source="ASSRT", title="second", score=100),
                module.SubtitleCandidate(source="ASSRT", title="low", score=40),
            ]
        if source == "subdl":
            return [module.SubtitleCandidate(source="SubDL", title="third", score=90)]
        return []

    def download_candidate(candidate, _video_path):
        """记录实际尝试的候选。"""
        attempted.append(candidate.title)
        return None

    monkeypatch.setattr(plugin, "_enabled_sources", lambda: ["assrt", "subdl"])
    monkeypatch.setattr(plugin, "_download_assrt_season_episode", lambda *_args: None)
    monkeypatch.setattr(plugin, "_search_source", search_source)
    monkeypatch.setattr(plugin, "_download_candidate", download_candidate)
    monkeypatch.setattr(plugin, "_has_existing_subtitle", lambda _video_path: False)

    assert not plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")
    assert attempted == ["best", "second"]


def test_downloadable_candidates_prunes_non_bilingual_when_bilingual_available(monkeypatch):
    """已有双语候选时，低于双语质量线的普通候选不应进入下载尝试。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    candidates = [
        module.SubtitleCandidate(source="ASSRT", title="Movie.2024.中英双语", score=80),
        module.SubtitleCandidate(source="ASSRT", title="Movie.2024.简体中文", score=130),
        module.SubtitleCandidate(source="SubDL", title="Movie.2024.高分纯中文", score=190),
    ]

    downloadable = plugin._downloadable_candidates(candidates)

    assert [candidate.title for candidate in downloadable] == [
        "Movie.2024.高分纯中文",
        "Movie.2024.中英双语",
    ]


def test_process_video_reuses_scan_existing_subtitle_check(monkeypatch, tmp_path):
    """扫描阶段已检查已有字幕时处理函数不应重复读取字幕。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.mkv"
    video_path.write_bytes(b"video")

    def fail_existing_subtitle(_video_path):
        """重复检查已有字幕时抛错。"""
        raise AssertionError("existing subtitle should not be checked twice")

    monkeypatch.setattr(plugin, "_has_existing_subtitle", fail_existing_subtitle)
    monkeypatch.setattr(plugin, "_enabled_sources", lambda: ["subdl"])
    monkeypatch.setattr(plugin, "_search_source", lambda *_args: [])

    assert not plugin._process_video(
        video_path=video_path,
        mediainfo=None,
        meta=None,
        storage="local",
        existing_subtitle_checked=True,
    )


def test_opensubtitles_candidates_prefer_matching_release_features(monkeypatch, tmp_path):
    """候选排序应优先匹配片源、编码、音频和发布组的字幕。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.1080p.WEB-DL.x265.DDP5.1-GRP.mkv"
    data = {
        "data": [
            {
                "id": "mismatch",
                "attributes": {
                    "release": "Movie.2024.1080p.BluRay.x264.DTS-OTHER",
                    "language": "zh-cn",
                    "download_count": 5000,
                    "ratings": 10,
                    "files": [{"file_id": 1, "file_name": "Movie.2024.1080p.BluRay.x264.DTS-OTHER.srt"}],
                },
            },
            {
                "id": "match",
                "attributes": {
                    "release": "Movie.2024.1080p.WEB-DL.x265.DDP5.1-GRP",
                    "language": "zh-cn",
                    "download_count": 1,
                    "ratings": 1,
                    "files": [{"file_id": 2, "file_name": "Movie.2024.1080p.WEB-DL.x265.DDP5.1-GRP.srt"}],
                },
            },
        ]
    }

    candidates = plugin._opensubtitles_candidates(data, video_path)

    assert [candidate.file_id for candidate in candidates] == [2, 1]


def test_opensubtitles_candidates_filters_machine_and_hearing_impaired(monkeypatch, tmp_path):
    """OpenSubtitles 应排除机翻和听障字幕。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.1080p.WEB-DL-GRP.mkv"
    data = {
        "data": [
            {
                "id": "machine",
                "attributes": {
                    "machine_translated": True,
                    "download_count": 5000,
                    "ratings": 10,
                    "files": [{"file_id": 1, "file_name": video_path.name}],
                },
            },
            {
                "id": "hearing",
                "attributes": {
                    "hearing_impaired": True,
                    "download_count": 5000,
                    "ratings": 10,
                    "files": [{"file_id": 2, "file_name": video_path.name}],
                },
            },
            {
                "id": "human",
                "attributes": {
                    "release": "Movie.2024.1080p.WEB-DL-GRP",
                    "language": "zh-cn",
                    "download_count": 10,
                    "ratings": 8,
                    "files": [{"file_id": 3, "file_name": video_path.name}],
                },
            },
        ]
    }

    candidates = plugin._opensubtitles_candidates(data, video_path)

    assert [candidate.title for candidate in candidates] == ["Movie.2024.1080p.WEB-DL-GRP"]
    assert candidates[0].file_id == 3


def test_valid_subtitle_content_rejects_html_and_accepts_subtitles(monkeypatch):
    """字幕内容校验应拒绝 HTML 错误页并接受常见字幕格式。"""
    module = _load_plugin_module(monkeypatch)

    assert not module.ChineseSubtitle._valid_subtitle_content(
        b"<!doctype html><html><body>not found</body></html>",
        ".srt",
    )
    assert module.ChineseSubtitle._valid_subtitle_content(
        b"1\n00:00:01,000 --> 00:00:02,000\nhello\n",
        ".srt",
    )
    assert module.ChineseSubtitle._valid_subtitle_content(
        b"[Script Info]\nTitle: demo\n[Events]\nFormat: Layer, Start, End, Text\n",
        ".ass",
    )


def test_invalid_existing_subtitle_does_not_skip_search(monkeypatch, tmp_path):
    """已有字幕无效时应继续搜索并允许新字幕覆盖目标文件。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")
    subtitle_path = tmp_path / "Movie.2026.zh-CN.srt"
    subtitle_path.write_text("<html>bad gateway</html>", encoding="utf-8")
    searched_sources = []

    def search_source(source, *_args):
        """返回可下载候选并记录搜索发生。"""
        searched_sources.append(source)
        return [module.SubtitleCandidate(source="SubDL", title="good", score=100, download_url="https://example/sub.srt")]

    def download_candidate(_candidate, _video_path):
        """模拟下载到目标字幕文件。"""
        subtitle_path.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
        return subtitle_path

    monkeypatch.setattr(plugin, "_enabled_sources", lambda: ["subdl"])
    monkeypatch.setattr(plugin, "_search_source", search_source)
    monkeypatch.setattr(plugin, "_download_candidate", download_candidate)

    assert plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")
    assert searched_sources == ["subdl"]


def test_bilingual_existing_subtitle_still_skips_search(monkeypatch, tmp_path):
    """已有中英双语字幕有效时仍应跳过。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")
    subtitle_path = tmp_path / "Movie.2026.zh-CN.srt"
    subtitle_path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n你好 hello\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n世界 world\n",
        encoding="utf-8",
    )

    def fail_enabled_sources():
        """已有有效中文字幕时不应进入搜索源。"""
        raise AssertionError("search should be skipped")

    monkeypatch.setattr(plugin, "_enabled_sources", fail_enabled_sources)

    assert not plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")


def test_pure_chinese_existing_subtitle_searches_for_bilingual_upgrade(monkeypatch, tmp_path):
    """偏好双语时已有纯中文字幕不应阻止继续搜索。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")
    subtitle_path = tmp_path / "Movie.2026.zh-CN.srt"
    subtitle_path.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
    searched_sources = []

    def search_source(source, *_args):
        """记录搜索源并返回双语候选。"""
        searched_sources.append(source)
        return [module.SubtitleCandidate(source="SubDL", title="Movie.2026.中英双语", score=100)]

    def download_candidate(_candidate, _video_path):
        """模拟双语字幕覆盖保存。"""
        subtitle_path.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n你好 hello\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n世界 world\n",
            encoding="utf-8",
        )
        return subtitle_path

    monkeypatch.setattr(plugin, "_enabled_sources", lambda: ["subdl"])
    monkeypatch.setattr(plugin, "_search_source", search_source)
    monkeypatch.setattr(plugin, "_download_candidate", download_candidate)

    assert plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")
    assert searched_sources == ["subdl"]


def test_non_chinese_existing_subtitle_does_not_skip_search(monkeypatch, tmp_path):
    """已有非中文字幕时不应阻止重新搜索。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")
    subtitle_path = tmp_path / "Movie.2026.zh-CN.srt"
    subtitle_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
    searched_sources = []

    def search_source(source, *_args):
        """记录搜索源并返回中文候选。"""
        searched_sources.append(source)
        return [module.SubtitleCandidate(source="SubDL", title="good", score=100, download_url="https://example/sub.srt")]

    def download_candidate(_candidate, _video_path):
        """模拟下载到目标字幕文件。"""
        subtitle_path.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")
        return subtitle_path

    monkeypatch.setattr(plugin, "_enabled_sources", lambda: ["subdl"])
    monkeypatch.setattr(plugin, "_search_source", search_source)
    monkeypatch.setattr(plugin, "_download_candidate", download_candidate)

    assert plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")
    assert searched_sources == ["subdl"]


def test_nfo_episode_is_used_when_filename_has_no_sxxexx(monkeypatch, tmp_path):
    """NFO 中的 episode 应作为非标准剧集文件名的集数来源。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    nfo_path = tmp_path / "episode.nfo"
    nfo_path.write_text(
        """
        <episodedetails>
          <showtitle>棋士</showtitle>
          <season>1</season>
          <episode>7.0</episode>
          <uniqueid type="tmdb">6062096</uniqueid>
        </episodedetails>
        """,
        encoding="utf-8",
    )
    video_path = tmp_path / "棋士 第七集.mkv"
    video_path.write_bytes(b"video")

    mediainfo = plugin._parse_nfo_mediainfo(nfo_path)

    assert mediainfo.episode == 7
    assert plugin._season_episode_numbers(video_path, mediainfo, meta=None) == (1, 7)
    assert plugin._season_episode_from_meta(mediainfo, meta=None) == "S01E07"


def test_shared_tvshow_nfo_is_cached(monkeypatch, tmp_path):
    """同一目录共享的 tvshow.nfo 应只解析一次。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    module.ChineseSubtitle._nfo_mediainfo_cache = {}
    nfo_path = tmp_path / "tvshow.nfo"
    nfo_path.write_text(
        "<tvshow><title>棋士</title><uniqueid type=\"tmdb\">6062096</uniqueid></tvshow>",
        encoding="utf-8",
    )
    first_video = tmp_path / "棋士 - S01E01.mkv"
    second_video = tmp_path / "棋士 - S01E02.mkv"
    first_video.write_bytes(b"video")
    second_video.write_bytes(b"video")
    parsed_paths = []

    def parse_nfo(path):
        """记录 NFO 解析次数。"""
        parsed_paths.append(path)
        return types.SimpleNamespace(imdb_id="tt1234567", tmdb_id=6062096)

    monkeypatch.setattr(plugin, "_parse_nfo_mediainfo", parse_nfo)

    assert plugin._mediainfo_from_local_nfo(first_video).imdb_id == "tt1234567"
    assert plugin._mediainfo_from_local_nfo(second_video).imdb_id == "tt1234567"
    assert parsed_paths == [nfo_path]


def test_short_title_substring_does_not_score_as_exact_match(monkeypatch):
    """短片名只是长标题子串时不应按精确匹配评分。"""
    module = _load_plugin_module(monkeypatch)

    score = module.ChineseSubtitle._text_match_score(
        "植物学家",
        "The Chinese Botanists Daughters 植物学家的中国女孩",
    )

    assert score < 45


def test_assrt_short_movie_title_embedded_in_unrelated_phrase_is_rejected(monkeypatch):
    """短中文电影名嵌在无关长词中时不应被下载量推成高分候选。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)

    rejected_score = plugin._assrt_candidate_score(
        target_title="十二宫",
        target_year="2007",
        target_resolution="1080p",
        target_release_text="Zodiac.2007.1080p.BluRay.mkv",
        query="十二宫",
        match_text="聖鬥士星矢：黃道十二宮戰士 圣斗士星矢：黄道十二宫战士 Season 1 第一季",
    )
    accepted_score = plugin._assrt_candidate_score(
        target_title="十二宫",
        target_year="2007",
        target_resolution="1080p",
        target_release_text="Zodiac.2007.1080p.BluRay.mkv",
        query="十二宫",
        match_text="十二宫 Zodiac 2007 1080p BluRay",
    )

    assert rejected_score is None
    assert accepted_score is not None


def test_iter_video_files_skips_bluray_stream_segments(monkeypatch, tmp_path):
    """目录扫描应跳过蓝光 BDMV/STREAM 分段文件。"""
    module = _load_plugin_module(monkeypatch)
    stream_dir = tmp_path / "Movie" / "BDMV" / "STREAM"
    stream_dir.mkdir(parents=True)
    segment = stream_dir / "00000.m2ts"
    normal = tmp_path / "Movie.2026.mkv"
    segment.write_bytes(b"video")
    normal.write_bytes(b"video")

    files = list(module.ChineseSubtitle._iter_video_files(tmp_path))

    assert normal in files
    assert segment not in files


def test_assrt_tv_queries_are_bounded_and_prioritized(monkeypatch, tmp_path):
    """ASSRT 剧集查询应只使用精确季集号，避免宽查询误集。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "棋士" / "Season 1" / "棋士 - S01E02 - 第2集.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    mediainfo = types.SimpleNamespace(
        type=module.MediaType.TV,
        title="棋士",
        en_title="The Match",
        original_title="棋士",
        season=1,
    )
    meta = types.SimpleNamespace(type=module.MediaType.TV, begin_episode=2)

    queries = plugin._assrt_queries(video_path, mediainfo, meta)

    assert len(queries) <= 3
    assert queries == ["棋士 S01E02"]
    assert not any(" E02" in query or "第2集" in query or query == "棋士" for query in queries)


def test_assrt_tv_queries_skip_when_episode_unknown(monkeypatch, tmp_path):
    """缺少集数时 ASSRT 不应退化为纯剧名宽搜。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "棋士" / "Season 1" / "棋士 - 第2集.mkv"
    video_path.parent.mkdir(parents=True)
    video_path.write_bytes(b"video")
    mediainfo = types.SimpleNamespace(type=module.MediaType.TV, title="棋士", season=1)

    assert plugin._assrt_queries(video_path, mediainfo, meta=None) == []


def test_assrt_season_queries_are_bounded(monkeypatch, tmp_path):
    """ASSRT 整季包查询应只使用带季号的高价值变体。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "棋士" / "Season 1" / "棋士 - S01E02 - 第2集.mkv"
    mediainfo = types.SimpleNamespace(type=module.MediaType.TV, title="棋士", en_title="The Match", season=1)
    meta = types.SimpleNamespace(type=module.MediaType.TV, begin_episode=2)

    queries = plugin._assrt_season_queries(video_path, mediainfo, meta, season=1)

    assert len(queries) <= 3
    assert queries == ["棋士 S01"]
    assert not any("全季" in query or query == "棋士" or "Season 1" in query for query in queries)


def test_assrt_backoff_skips_without_sleeping(monkeypatch):
    """ASSRT 流控冷却期内应快速跳过请求。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    module.ChineseSubtitle._assrt_backoff_until = module.time.time() + 60

    def fail_sleep(_seconds):
        """冷却期不应进入 sleep。"""
        raise AssertionError("sleep should not be called during ASSRT backoff")

    monkeypatch.setattr(module.time, "sleep", fail_sleep)
    try:
        assert plugin._assrt_get_res("https://api.assrt.net/v1/sub/search", params={}) is None
    finally:
        module.ChineseSubtitle._assrt_backoff_until = 0


def test_assrt_509_disables_source_for_current_scan(monkeypatch):
    """ASSRT 触发 509 后应暂停本轮目录扫描中的 ASSRT。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)

    class FakeResponse:
        """测试用 ASSRT 509 响应。"""

        status_code = 509
        headers = {}

    class FakeRequestUtils:
        """测试用 HTTP 客户端。"""

        def __init__(self, **_kwargs):
            """忽略请求参数。"""

        @staticmethod
        def get_res(_url, params=None):
            """返回流控响应。"""
            return FakeResponse()

    monkeypatch.setattr(module, "RequestUtils", FakeRequestUtils)
    module.ChineseSubtitle._scan_active = True
    module.ChineseSubtitle._scan_disabled_sources = set()
    module.ChineseSubtitle._assrt_backoff_until = 0
    module.ChineseSubtitle._assrt_last_request_time = 0

    try:
        assert plugin._assrt_get_res("https://api.assrt.net/v1/sub/search", params={}).status_code == 509
        assert "assrt" in module.ChineseSubtitle._scan_disabled_sources
        assert "assrt" not in plugin._enabled_sources()
    finally:
        module.ChineseSubtitle._scan_active = False
        module.ChineseSubtitle._scan_disabled_sources = set()
        module.ChineseSubtitle._assrt_backoff_until = 0


def test_scan_library_skips_duplicate_trigger(monkeypatch):
    """目录扫描重复触发时应快速跳过。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    plugin._scan_enable = True

    def fail_scan():
        """重复触发时不应进入实际扫描。"""
        raise AssertionError("duplicate scan should be skipped")

    monkeypatch.setattr(plugin, "_scan_library_locked", fail_scan)
    module.ChineseSubtitle._scan_task_lock.acquire()
    try:
        plugin.scan_library()
    finally:
        module.ChineseSubtitle._scan_task_lock.release()


def test_scan_library_skips_recent_video_miss(monkeypatch, tmp_path):
    """目录扫描应跳过近期已经完整尝试但未命中的视频。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    plugin._scan_system_library_dirs = False
    plugin._scan_dirs = str(tmp_path)
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")
    processed = []

    def process_video(**kwargs):
        """记录实际进入字幕搜索的视频。"""
        processed.append(kwargs["video_path"].name)
        return False

    monkeypatch.setattr(plugin, "_has_existing_subtitle", lambda _video_path: False)
    monkeypatch.setattr(plugin, "_mediainfo_from_local_nfo", lambda _video_path: None)
    monkeypatch.setattr(plugin, "_process_video", process_video)

    plugin._scan_library_locked()
    plugin._scan_library_locked()

    assert processed == ["Movie.2026.mkv"]


def test_scan_library_deduplicates_video_paths(monkeypatch, tmp_path):
    """目录扫描同一轮遇到重复视频路径时只应处理一次。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")
    processed = []

    def process_video(**kwargs):
        """记录实际处理的视频。"""
        processed.append(kwargs["video_path"].name)
        return False

    monkeypatch.setattr(plugin, "_scan_directories", lambda: [tmp_path, tmp_path])
    monkeypatch.setattr(plugin, "_iter_video_files", lambda _scan_dir: [video_path])
    monkeypatch.setattr(plugin, "_has_existing_subtitle", lambda _video_path: False)
    monkeypatch.setattr(plugin, "_mediainfo_from_local_nfo", lambda _video_path: None)
    monkeypatch.setattr(plugin, "_process_video", process_video)

    plugin._scan_library_locked()

    assert processed == ["Movie.2026.mkv"]


def test_scan_library_logs_summary_after_limit(monkeypatch, tmp_path):
    """目录扫描达到尝试上限后仍应输出汇总。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    plugin._scan_limit = 1
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")
    logs = []

    monkeypatch.setattr(plugin, "_scan_directories", lambda: [tmp_path])
    monkeypatch.setattr(plugin, "_iter_video_files", lambda _scan_dir: [video_path, video_path.with_name("Next.mkv")])
    monkeypatch.setattr(plugin, "_has_existing_subtitle", lambda _video_path: False)
    monkeypatch.setattr(plugin, "_mediainfo_from_local_nfo", lambda _video_path: None)
    monkeypatch.setattr(plugin, "_process_video", lambda **_kwargs: False)
    monkeypatch.setattr(module.logger, "info", lambda message: logs.append(message))

    plugin._scan_library_locked()

    assert any("中文字幕目录扫描达到单次尝试上限" in message for message in logs)
    assert any("中文字幕目录扫描完成" in message for message in logs)


def test_scan_library_resumes_from_scan_cursor(monkeypatch, tmp_path):
    """目录扫描达到单次上限后，下一轮应从上次游标之后继续。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    plugin._scan_limit = 2
    for name in ("A.mkv", "B.mkv", "C.mkv"):
        (tmp_path / name).write_bytes(b"video")
    processed = []

    def process_video(**kwargs):
        """记录实际进入字幕搜索的视频。"""
        processed.append(kwargs["video_path"].name)
        return False

    monkeypatch.setattr(plugin, "_scan_directories", lambda: [tmp_path])
    monkeypatch.setattr(plugin, "_has_existing_subtitle", lambda _video_path: False)
    monkeypatch.setattr(plugin, "_mediainfo_from_local_nfo", lambda _video_path: None)
    monkeypatch.setattr(plugin, "_video_scan_miss_cache_hit", lambda _video_path: False)
    monkeypatch.setattr(plugin, "_record_video_scan_miss", lambda _video_path: None)
    monkeypatch.setattr(plugin, "_process_video", process_video)

    plugin._scan_library_locked()
    plugin._scan_library_locked()

    assert processed == ["A.mkv", "B.mkv", "C.mkv", "A.mkv"]


def test_assrt_search_stops_after_scan_source_disabled(monkeypatch, tmp_path):
    """本轮 ASSRT 熔断后应停止后续查询变体。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "棋士 - S01E02 - 第2集.mkv"
    video_path.write_bytes(b"video")
    searched_queries = []

    def search_by_query(**kwargs):
        """记录查询并模拟首次查询触发本轮熔断。"""
        searched_queries.append(kwargs["query"])
        plugin._disable_source_for_scan("assrt", "测试熔断")
        return []

    monkeypatch.setattr(plugin, "_assrt_queries", lambda *_args: ["棋士 S01E02", "棋士 E02"])
    monkeypatch.setattr(plugin, "_search_assrt_by_query", search_by_query)
    module.ChineseSubtitle._scan_active = True
    module.ChineseSubtitle._scan_disabled_sources = set()
    module.ChineseSubtitle._assrt_backoff_until = 0

    try:
        assert plugin._search_assrt(video_path, mediainfo=None, meta=None) == []
    finally:
        module.ChineseSubtitle._scan_active = False
        module.ChineseSubtitle._scan_disabled_sources = set()

    assert searched_queries == ["棋士 S01E02"]


def test_assrt_interval_wait_rechecks_backoff_before_request(monkeypatch):
    """ASSRT 节流等待后应复查冷却状态再决定是否请求。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    plugin._assrt_interval = 10

    class FakeRequestUtils:
        """测试用 HTTP 客户端。"""

        def __init__(self, **_kwargs):
            """忽略请求参数。"""

        @staticmethod
        def get_res(_url, params=None):
            """等待后进入冷却期时不应发起请求。"""
            raise AssertionError("request should not be sent after backoff starts")

    def start_backoff(_seconds):
        """模拟等待期间其他路径设置了 ASSRT 冷却。"""
        module.ChineseSubtitle._assrt_backoff_until = module.time.time() + 60

    monkeypatch.setattr(module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(module.time, "sleep", start_backoff)
    module.ChineseSubtitle._assrt_backoff_until = 0
    module.ChineseSubtitle._assrt_last_request_time = module.time.time()

    try:
        assert plugin._assrt_get_res("https://api.assrt.net/v1/sub/search", params={}) is None
    finally:
        module.ChineseSubtitle._assrt_backoff_until = 0
        module.ChineseSubtitle._assrt_last_request_time = 0


def test_assrt_backoff_is_persisted_between_instances(monkeypatch):
    """ASSRT 流控冷却应通过插件数据在实例之间共享。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)

    class RateLimitedResponse:
        """测试用 ASSRT 509 响应。"""

        status_code = 509
        headers = {}

    class RateLimitedRequestUtils:
        """测试用 HTTP 客户端。"""

        def __init__(self, **_kwargs):
            """忽略请求参数。"""

        @staticmethod
        def get_res(_url, params=None):
            """返回流控响应。"""
            return RateLimitedResponse()

    monkeypatch.setattr(module, "RequestUtils", RateLimitedRequestUtils)
    module.ChineseSubtitle._assrt_backoff_until = 0
    module.ChineseSubtitle._assrt_last_request_time = 0
    assert plugin._assrt_get_res("https://api.assrt.net/v1/sub/search", params={}).status_code == 509

    class FailRequestUtils:
        """测试用 HTTP 客户端。"""

        def __init__(self, **_kwargs):
            """忽略请求参数。"""

        @staticmethod
        def get_res(_url, params=None):
            """持久化冷却生效时不应发起请求。"""
            raise AssertionError("request should not be sent while persisted backoff is active")

    next_plugin = _plugin(module)
    next_plugin._data = plugin._data
    monkeypatch.setattr(module, "RequestUtils", FailRequestUtils)
    module.ChineseSubtitle._assrt_backoff_until = 0
    module.ChineseSubtitle._assrt_last_request_time = 0

    try:
        assert next_plugin._assrt_get_res("https://api.assrt.net/v1/sub/search", params={}) is None
    finally:
        module.ChineseSubtitle._assrt_backoff_until = 0
        module.ChineseSubtitle._assrt_last_request_time = 0


def test_source_miss_cache_skips_repeated_source_search(monkeypatch, tmp_path):
    """源级未命中缓存应跳过短期内重复搜索。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")
    searched_sources = []

    def search_source(source, *_args):
        """记录实际搜索的字幕源。"""
        searched_sources.append(source)
        return []

    monkeypatch.setattr(plugin, "_enabled_sources", lambda: ["subdl"])
    monkeypatch.setattr(plugin, "_search_source", search_source)
    monkeypatch.setattr(plugin, "_has_existing_subtitle", lambda _video_path: False)

    assert not plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")
    assert not plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")

    assert searched_sources == ["subdl"]


def test_season_source_miss_cache_skips_same_season_after_threshold(monkeypatch, tmp_path):
    """扫描中同季同源连续未命中达到阈值后应跳过后续同季搜索。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_paths = []
    for episode in range(1, 5):
        video_path = tmp_path / f"Show - S01E{episode:02d}.mkv"
        video_path.write_bytes(b"video")
        video_paths.append(video_path)
    mediainfo = types.SimpleNamespace(
        type=module.MediaType.TV,
        title="Show",
        season=1,
        imdb_id="tt1234567",
        tmdb_id=12345,
    )
    searched_files = []

    def search_source(source, video_path, *_args):
        """记录实际搜索的视频并模拟未命中。"""
        searched_files.append((source, video_path.name))
        return []

    monkeypatch.setattr(plugin, "_enabled_sources", lambda: ["subdl"])
    monkeypatch.setattr(plugin, "_search_source", search_source)
    monkeypatch.setattr(plugin, "_has_existing_subtitle", lambda _video_path: False)
    module.ChineseSubtitle._scan_active = True
    module.ChineseSubtitle._scan_disabled_sources = set()

    try:
        for video_path in video_paths:
            assert not plugin._process_video(video_path=video_path, mediainfo=mediainfo, meta=None, storage="local")
    finally:
        module.ChineseSubtitle._scan_active = False
        module.ChineseSubtitle._scan_disabled_sources = set()

    assert searched_files == [
        ("subdl", "Show - S01E01.mkv"),
        ("subdl", "Show - S01E02.mkv"),
        ("subdl", "Show - S01E03.mkv"),
    ]


def test_video_scan_miss_key_ignores_scan_disabled_sources(monkeypatch, tmp_path):
    """视频级未命中缓存不应受本轮源熔断状态影响。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")

    before_key = plugin._video_scan_miss_cache_entry_key(video_path)
    module.ChineseSubtitle._scan_disabled_sources = {"assrt", "opensubtitles"}
    try:
        after_key = plugin._video_scan_miss_cache_entry_key(video_path)
    finally:
        module.ChineseSubtitle._scan_disabled_sources = set()

    assert before_key == after_key


def test_opensubtitles_quota_exhausted_disables_source_for_scan(monkeypatch):
    """OpenSubtitles 达到下载额度后应在本轮扫描内禁用。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    disabled = []
    module.ChineseSubtitle._scan_active = True
    module.ChineseSubtitle._scan_disabled_sources = set()

    monkeypatch.setattr(plugin, "_opensubtitles_download_quota_exhausted", lambda: True)
    monkeypatch.setattr(plugin, "_disable_source_for_scan", lambda source, reason: disabled.append((source, reason)))

    try:
        assert not plugin._source_search_available("opensubtitles")
    finally:
        module.ChineseSubtitle._scan_active = False
        module.ChineseSubtitle._scan_disabled_sources = set()

    assert disabled == [("opensubtitles", "今日下载额度已用完")]


def test_opensubtitles_quota_exhausted_logs_once_per_scan(monkeypatch):
    """OpenSubtitles 额度耗尽后本轮重复检查不应重复写日志。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    plugin.save_data(plugin._opensubtitles_quota_key, {
        "date": plugin._today_key(),
        "count": plugin._opensubtitles_daily_limit,
    })
    logs = []
    module.ChineseSubtitle._scan_active = True
    module.ChineseSubtitle._scan_disabled_sources = set()

    monkeypatch.setattr(module.logger, "warn", lambda message: logs.append(message))

    try:
        assert plugin._opensubtitles_download_quota_exhausted()
        assert plugin._opensubtitles_download_quota_exhausted()
    finally:
        module.ChineseSubtitle._scan_active = False
        module.ChineseSubtitle._scan_disabled_sources = set()

    assert logs == [
        "OpenSubtitles 今日下载额度已用完：5/5，跳过 OpenSubtitles",
        "opensubtitles 本轮目录扫描已暂停：今日下载额度已用完",
    ]


def test_save_from_zip_skips_invalid_members(monkeypatch, tmp_path):
    """压缩包保存时应跳过无效字幕成员。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.1080p.WEB-DL-GRP.mkv"
    video_path.write_bytes(b"video")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("Movie.2024.1080p.WEB-DL-GRP.srt", "<html>bad</html>")
        zf.writestr("fallback.srt", "1\n00:00:01,000 --> 00:00:02,000\n你好\n")

    saved = plugin._save_from_zip(zip_buffer.getvalue(), video_path)

    assert saved == tmp_path / "Movie.2024.1080p.WEB-DL-GRP.zh-CN.srt"
    assert saved.read_text() == "1\n00:00:01,000 --> 00:00:02,000\n你好\n"


def test_save_from_zip_prefers_bilingual_member(monkeypatch, tmp_path):
    """压缩包内多个字幕有效时应优先选择中英双语文件。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.1080p.WEB-DL-GRP.mkv"
    video_path.write_bytes(b"video")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("Movie.2024.1080p.WEB-DL-GRP.srt", "1\n00:00:01,000 --> 00:00:02,000\n你好\n")
        zf.writestr("Movie.2024.1080p.WEB-DL-GRP.中英双语.srt", "1\n00:00:01,000 --> 00:00:02,000\n你好 hello\n")

    saved = plugin._save_from_zip(zip_buffer.getvalue(), video_path)

    assert saved == tmp_path / "Movie.2024.1080p.WEB-DL-GRP.zh-CN.srt"
    assert saved.read_text() == "1\n00:00:01,000 --> 00:00:02,000\n你好 hello\n"


def test_download_url_caches_failed_url(monkeypatch, tmp_path):
    """下载失败的字幕地址应短期缓存并跳过重复请求。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.mkv"
    video_path.write_bytes(b"video")
    calls = []

    class BadResponse:
        """测试用失败响应。"""

        status_code = 500
        headers = {}

        @staticmethod
        def close():
            """关闭响应。"""

    class FakeRequestUtils:
        """测试用 HTTP 客户端。"""

        def __init__(self, **kwargs):
            """记录下载超时配置。"""
            self.timeout = kwargs.get("timeout")

        def get_res(self, url, **kwargs):
            """记录请求并返回失败响应。"""
            calls.append((url, self.timeout, kwargs.get("stream")))
            return BadResponse()

    monkeypatch.setattr(module, "RequestUtils", FakeRequestUtils)

    assert plugin._download_url("https://example.com/sub.srt", video_path) is None
    assert plugin._download_url("https://example.com/sub.srt", video_path) is None

    assert calls == [("https://example.com/sub.srt", 15, True)]


def test_download_url_enforces_total_deadline(monkeypatch, tmp_path):
    """下载地址整体耗时超限时应快速跳过并缓存失败。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.mkv"
    video_path.write_bytes(b"video")
    calls = []

    class SlowResponse:
        """测试用慢响应。"""

        status_code = 200
        headers = {"Content-Type": "text/plain"}

        @staticmethod
        def iter_content(chunk_size=64 * 1024):
            """模拟长时间阻塞的响应读取。"""
            time.sleep(0.2)
            yield "1\n00:00:01,000 --> 00:00:02,000\n你好 hello\n".encode()

        @staticmethod
        def close():
            """关闭响应。"""

    class FakeRequestUtils:
        """测试用 HTTP 客户端。"""

        def __init__(self, **_kwargs):
            """初始化测试客户端。"""

        def get_res(self, url, **_kwargs):
            """记录请求并返回慢响应。"""
            calls.append(url)
            return SlowResponse()

    monkeypatch.setattr(module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(plugin, "_download_deadline_seconds", lambda: 0.01)

    started = time.time()

    assert plugin._download_url("https://example.com/slow.srt", video_path) is None
    assert time.time() - started < 0.15
    assert plugin._download_url("https://example.com/slow.srt", video_path) is None
    assert calls == ["https://example.com/slow.srt"]


def test_download_assrt_limits_urls_per_candidate(monkeypatch, tmp_path):
    """ASSRT 单个候选只应尝试有限数量下载地址。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.mkv"
    video_path.write_bytes(b"video")
    attempted_urls = []

    class DetailResponse:
        """测试用 ASSRT 详情响应。"""

        status_code = 200

        @staticmethod
        def json():
            """返回多个可下载地址。"""
            return {
                "status": 0,
                "sub": {
                    "subs": [{
                        "filelist": [
                            {"url": "https://example.com/1.srt"},
                            {"url": "https://example.com/2.srt"},
                            {"url": "https://example.com/3.srt"},
                        ],
                    }],
                },
            }

    monkeypatch.setattr(plugin, "_assrt_get_res", lambda *_args, **_kwargs: DetailResponse())

    def download_url(url, _video_path):
        """记录实际尝试下载的地址。"""
        attempted_urls.append(url)
        return None

    monkeypatch.setattr(plugin, "_download_url", download_url)

    assert plugin._download_assrt(
        module.SubtitleCandidate(source="ASSRT", title="candidate", raw={"id": 1}),
        video_path,
    ) is None
    assert attempted_urls == ["https://example.com/1.srt", "https://example.com/2.srt"]


def test_download_assrt_deduplicates_repeated_file_urls(monkeypatch, tmp_path):
    """ASSRT 详情返回重复下载地址时应只尝试一次。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.mkv"
    video_path.write_bytes(b"video")
    attempted_urls = []

    class DetailResponse:
        """测试用 ASSRT 详情响应。"""

        status_code = 200

        @staticmethod
        def json():
            """返回包含重复地址的详情。"""
            return {
                "status": 0,
                "sub": {
                    "subs": [{
                        "filelist": [
                            {"url": "https://example.com/sub.srt"},
                            {"url": "https://example.com/sub.srt"},
                        ],
                    }],
                },
            }

    monkeypatch.setattr(plugin, "_assrt_get_res", lambda *_args, **_kwargs: DetailResponse())
    monkeypatch.setattr(plugin, "_download_url", lambda url, _video_path: attempted_urls.append(url) or None)

    candidate = module.SubtitleCandidate(source="ASSRT", title="dup", raw={"id": "dup"})

    assert plugin._download_assrt(candidate, video_path) is None
    assert attempted_urls == ["https://example.com/sub.srt"]


def test_download_assrt_skips_unsupported_file_entries(monkeypatch, tmp_path):
    """ASSRT 详情中的非文本字幕文件应按文件名和编码 URL 提前跳过。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2024.mkv"
    video_path.write_bytes(b"video")
    attempted_urls = []

    class DetailResponse:
        """测试用 ASSRT 详情响应。"""

        status_code = 200

        @staticmethod
        def json():
            """返回混合字幕文件详情。"""
            return {
                "status": 0,
                "sub": {
                    "subs": [{
                        "filelist": [
                            {"f": "Movie.2024.sup", "url": "https://example.com/no-suffix"},
                            {"url": "https://example.com/Movie%2E2024%2Esup"},
                            {"f": "Movie.2024.srt", "url": "https://example.com/Movie.2024.srt"},
                        ],
                    }],
                },
            }

    monkeypatch.setattr(plugin, "_assrt_get_res", lambda *_args, **_kwargs: DetailResponse())
    monkeypatch.setattr(plugin, "_download_url", lambda url, _video_path: attempted_urls.append(url) or None)

    candidate = module.SubtitleCandidate(source="ASSRT", title="mixed", raw={"id": "mixed"})

    assert plugin._download_assrt(candidate, video_path) is None
    assert attempted_urls == ["https://example.com/Movie.2024.srt"]


def test_unsupported_subtitle_url_suffix_decodes_encoded_extension(monkeypatch):
    """编码后的不支持字幕后缀也应被识别。"""
    module = _load_plugin_module(monkeypatch)

    assert module.ChineseSubtitle._unsupported_subtitle_url_suffix("https://example.com/Movie%2E2024%2Esup")
    assert not module.ChineseSubtitle._unsupported_subtitle_url_suffix("https://example.com/Movie%2E2024%2Esrt")
