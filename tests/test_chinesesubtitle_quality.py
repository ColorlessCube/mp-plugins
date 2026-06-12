import importlib.util
import io
import sys
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


def test_valid_existing_subtitle_still_skips_search(monkeypatch, tmp_path):
    """已有字幕有效时仍应按默认策略跳过。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "Movie.2026.mkv"
    video_path.write_bytes(b"video")
    subtitle_path = tmp_path / "Movie.2026.zh-CN.srt"
    subtitle_path.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")

    def fail_enabled_sources():
        """已有有效中文字幕时不应进入搜索源。"""
        raise AssertionError("search should be skipped")

    monkeypatch.setattr(plugin, "_enabled_sources", fail_enabled_sources)

    assert not plugin._process_video(video_path=video_path, mediainfo=None, meta=None, storage="local")


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


def test_short_title_substring_does_not_score_as_exact_match(monkeypatch):
    """短片名只是长标题子串时不应按精确匹配评分。"""
    module = _load_plugin_module(monkeypatch)

    score = module.ChineseSubtitle._text_match_score(
        "植物学家",
        "The Chinese Botanists Daughters 植物学家的中国女孩",
    )

    assert score < 45


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
    """ASSRT 剧集查询应限制数量并优先高价值片名变体。"""
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

    assert len(queries) <= 8
    assert queries[:4] == ["棋士 S01E02", "棋士 E02", "棋士 第2集", "棋士"]
    assert not any("EP02" in query or "第二集" in query for query in queries)


def test_assrt_season_queries_are_bounded(monkeypatch, tmp_path):
    """ASSRT 整季包查询应限制低价值季名变体。"""
    module = _load_plugin_module(monkeypatch)
    plugin = _plugin(module)
    video_path = tmp_path / "棋士" / "Season 1" / "棋士 - S01E02 - 第2集.mkv"
    mediainfo = types.SimpleNamespace(type=module.MediaType.TV, title="棋士", en_title="The Match", season=1)
    meta = types.SimpleNamespace(type=module.MediaType.TV, begin_episode=2)

    queries = plugin._assrt_season_queries(video_path, mediainfo, meta, season=1)

    assert len(queries) <= 6
    assert queries[:3] == ["棋士 S01", "棋士 全季", "棋士"]
    assert not any("Season 1" in query or "全集" in query for query in queries)


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
