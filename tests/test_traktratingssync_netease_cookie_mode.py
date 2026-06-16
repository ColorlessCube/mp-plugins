"""豆瓣书影音同步插件网易云 Cookie 主同步模式测试。"""
import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path


class _MediaType(Enum):
    """测试用媒体类型。"""

    MOVIE = "电影"
    TV = "电视剧"


class _Logger:
    """测试用日志桩。"""

    def debug(self, *_args, **_kwargs):
        """记录 debug 日志。"""

    def info(self, *_args, **_kwargs):
        """记录 info 日志。"""

    def warning(self, *_args, **_kwargs):
        """记录 warning 日志。"""

    def error(self, *_args, **_kwargs):
        """记录 error 日志。"""


class _PluginBase:
    """测试用插件基类。"""

    def __init__(self):
        """初始化测试持久化区和配置更新记录。"""
        self._data = {}
        self._config_updates = []

    def get_data(self, key):
        """读取测试持久化数据。"""
        return self._data.get(key)

    def save_data(self, key, value):
        """保存测试持久化数据。"""
        self._data[key] = value

    def update_config(self, config):
        """记录插件配置更新。"""
        self._config_updates.append(dict(config))


class _DoubanHelper:
    """测试用豆瓣 Helper。"""

    def __init__(self, *_args, **_kwargs):
        """初始化豆瓣 Helper 桩。"""


class _WereadHelper:
    """测试用微信读书 Helper。"""

    @staticmethod
    def format_reading_time(seconds):
        """格式化测试阅读时长。"""
        return f"{seconds}s"


class _XiaoyuzhouHelper:
    """测试用小宇宙 Helper。"""

    @staticmethod
    def format_duration(seconds):
        """格式化测试单集时长。"""
        return f"{seconds}s"


def _install_app_stubs(monkeypatch):
    """安装插件导入所需的 MoviePilot 边界桩。"""
    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = []
    schemas_pkg = types.ModuleType("app.schemas")
    schemas_pkg.__path__ = []
    utils_pkg = types.ModuleType("app.utils")
    utils_pkg.__path__ = []

    monkeypatch.setitem(sys.modules, "app", app_pkg)
    monkeypatch.setitem(sys.modules, "app.log", types.SimpleNamespace(logger=_Logger()))
    monkeypatch.setitem(sys.modules, "app.plugins", types.SimpleNamespace(_PluginBase=_PluginBase))
    monkeypatch.setitem(sys.modules, "app.schemas", schemas_pkg)
    monkeypatch.setitem(sys.modules, "app.schemas.types", types.SimpleNamespace(MediaType=_MediaType))
    monkeypatch.setitem(sys.modules, "app.utils", utils_pkg)
    monkeypatch.setitem(sys.modules, "app.utils.http", types.SimpleNamespace(RequestUtils=object))


def _install_helper_stubs(monkeypatch):
    """安装插件相对 helper 依赖桩，只测试 __init__.py 编排逻辑。"""
    package_names = [
        "plugins",
        "plugins.traktratingssync",
    ]
    for name in package_names:
        module = types.ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setitem(
        sys.modules,
        "plugins.traktratingssync.douban_helper",
        types.SimpleNamespace(DoubanHelper=_DoubanHelper),
    )
    monkeypatch.setitem(
        sys.modules,
        "plugins.traktratingssync.netease_helper",
        types.SimpleNamespace(NeteaseHelper=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "plugins.traktratingssync.trakt_helper",
        types.SimpleNamespace(TraktHelper=object),
    )
    monkeypatch.setitem(
        sys.modules,
        "plugins.traktratingssync.weread_helper",
        types.SimpleNamespace(WereadHelper=_WereadHelper),
    )
    monkeypatch.setitem(
        sys.modules,
        "plugins.traktratingssync.xiaoyuzhou_helper",
        types.SimpleNamespace(XiaoyuzhouHelper=_XiaoyuzhouHelper),
    )


def _load_plugin_module(monkeypatch):
    """加载 TraktRatingsSync 插件入口并替换外部依赖。"""
    _install_app_stubs(monkeypatch)
    _install_helper_stubs(monkeypatch)
    plugin_path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "traktratingssync"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(
        "plugins.traktratingssync",
        plugin_path,
        submodule_search_locations=[str(plugin_path.parent)],
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "plugins.traktratingssync", module)
    spec.loader.exec_module(module)
    return module


def test_run_ignores_legacy_openapi_config_without_cookie(monkeypatch):
    """遗留开放平台配置不应触发网易云同步或 CLI 授权流程。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({
        "enable": True,
        "douban_cookie": "dbcl2=douban",
        "netease_app_id": "app-id",
        "netease_private_key": "private-key",
    })
    calls = []
    monkeypatch.setattr(plugin, "_sync_netease", lambda: calls.append("netease"))

    plugin.run()

    assert not plugin._has_netease_source()
    assert calls == []


def test_sync_netease_uses_cookie_helper(monkeypatch):
    """网易云主同步应使用 Cookie Helper 拉取专辑并写入豆瓣同步记录。"""
    module = _load_plugin_module(monkeypatch)
    helper_instances = []

    class FakeNeteaseHelper:
        """测试用网易云 Cookie Helper。"""

        def __init__(self, cookies, notify_fn):
            """记录 Cookie 和通知回调。"""
            self.cookies = cookies
            self.notify_fn = notify_fn
            helper_instances.append(self)

        def get_recent_albums(self, limit):
            """返回固定专辑列表。"""
            assert limit == 5
            return [
                {
                    "album": "Album A",
                    "artist": "Artist A",
                    "song_count": 1,
                    "total_play_count": 3,
                    "songs": ["Song A"],
                }
            ]

    class FakeDoubanHelper:
        """测试用豆瓣音乐提交 Helper。"""

        def __init__(self):
            """初始化提交记录。"""
            self.submissions = []

        def get_music_subject_id(self, title, artist=None):
            """返回固定豆瓣音乐条目。"""
            assert (title, artist) == ("Album A", "Artist A")
            return "Album A 豆瓣", "123456"

        def set_music_status(self, subject_id, status, private=True, rating=None):
            """记录状态提交请求。"""
            self.submissions.append({
                "subject_id": subject_id,
                "status": status,
                "private": private,
                "rating": rating,
            })
            return True

    monkeypatch.setattr(module, "NeteaseHelper", FakeNeteaseHelper)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({
        "enable": True,
        "douban_cookie": "dbcl2=douban",
        "netease_cookie": "MUSIC_U=user-token; __csrf=csrf-token",
        "netease_limit": 5,
        "private": False,
    })
    douban_helper = FakeDoubanHelper()
    plugin._douban_helper = douban_helper

    plugin._sync_netease()

    assert helper_instances[0].cookies == "MUSIC_U=user-token; __csrf=csrf-token"
    assert douban_helper.submissions == [
        {"subject_id": "123456", "status": "collect", "private": False, "rating": None}
    ]
    assert plugin.get_data("netease_albums")["123456"]["album"] == "Album A"
    assert plugin.get_data("netease_album_map")["Album A\tArtist A"]["subject_id"] == "123456"


def test_netease_cookie_auth_notification_uses_persistent_cooldown(monkeypatch):
    """网易云 Cookie 失效通知应按 Cookie 指纹持久化冷却。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({
        "enable": True,
        "netease_cookie": "MUSIC_U=user-token; __csrf=csrf-token",
    })
    notifications = []
    monkeypatch.setattr(
        plugin,
        "_send_bark_notification",
        lambda title, content: notifications.append((title, content)) or True,
    )

    assert plugin._send_netease_cookie_auth_notification("网易云 Cookie 已失效", "需要更新") is True
    assert plugin._send_netease_cookie_auth_notification("网易云 Cookie 已失效", "需要更新") is False

    assert notifications == [("网易云 Cookie 已失效", "需要更新")]
    assert plugin.get_data("netease_cookie_auth_notify_state")["title"] == "网易云 Cookie 已失效"


def test_get_form_only_exposes_netease_cookie_config(monkeypatch):
    """配置页仅暴露需要用户维护的网易云与授权字段。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    form, defaults = plugin.get_form()

    def iter_models(items):
        """遍历表单组件中的 model 字段。"""
        for item in items:
            props = item.get("props") or {}
            model = props.get("model")
            if model:
                yield model
            yield from iter_models(item.get("content") or [])

    def iter_texts(items):
        """遍历表单组件中的文本。"""
        for item in items:
            text = item.get("text")
            if text:
                yield text
            yield from iter_texts(item.get("content") or [])

    models = set(iter_models(form))
    section_titles = set(iter_texts(form))
    editable_models = {
        "enable",
        "private",
        "sync_type",
        "cron",
        "max_sync_count",
        "douban_cookie",
        "bark_webhook_url",
        "trakt_username",
        "trakt_client_id",
        "trakt_client_secret",
        "trakt_manual_mappings",
        "trakt_history_limit",
        "trakt_history_days",
        "weread_api_key",
        "weread_limit",
        "netease_cookie",
        "netease_limit",
        "xiaoyuzhou_cookie",
        "xiaoyuzhou_limit",
    }
    removed_models = {
        "trakt_access_token",
        "netease_app_id",
        "netease_app_secret",
        "netease_private_key",
        "netease_device_id",
        "netease_access_token",
        "netease_refresh_token",
        "netease_token_expires_at",
        "netease_anonymous_access_token",
        "netease_qr_key",
        "netease_qr_url",
    }

    assert {
        "基础设置",
        "通知",
        "豆瓣",
        "Trakt",
        "微信读书",
        "网易云音乐",
        "小宇宙",
    } <= section_titles
    assert "豆瓣与通知" not in section_titles
    assert "阅读与音乐" not in section_titles
    assert editable_models <= models
    assert not (models & removed_models)
    assert "trakt_access_token" in defaults
    assert not (set(defaults) & (removed_models - {"trakt_access_token"}))


def test_get_page_shows_overview_and_platform_sections(monkeypatch):
    """详情页应展示概览和有数据的平台分区。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.save_data("finished", {
        "movie": {
            "douban_id": "1292052",
            "title": "电影 A",
            "year": 1994,
            "media_type": "电影",
            "status": "看完",
            "trakt_rating": 9,
            "douban_rating": 9,
            "sync_time": 1710000000,
        }
    })
    plugin.save_data("watching", {
        "show": {
            "douban_id": "27000000",
            "title": "剧集 B",
            "year": 2024,
            "media_type": "电视剧",
            "status": "在看",
            "sync_time": 1710000100,
        }
    })
    plugin.save_data("weread_books", [{
        "title": "书籍 C",
        "author": "作者",
        "status": "在读",
        "reading_progress": 42,
        "reading_time": 3600,
        "weread_url": "https://weread.qq.com",
    }])
    plugin.save_data("netease_albums", {
        "album": {
            "douban_id": "35000000",
            "douban_title": "专辑 D",
            "artist": "音乐人",
            "song_count": 10,
            "total_play_count": 20,
            "sync_time": 1710000200,
        }
    })
    plugin.save_data("xiaoyuzhou_episodes", [{
        "title": "单集 E",
        "podcast_name": "播客",
        "duration": 120,
        "listen_pct": 0.5,
        "is_finished": False,
    }])

    page = plugin.get_page()

    def iter_items(items):
        """遍历详情页组件。"""
        for item in items:
            yield item
            yield from iter_items(item.get("content") or [])

    texts = {
        item.get("text") or (item.get("props") or {}).get("text")
        for item in iter_items(page)
        if item.get("text") or (item.get("props") or {}).get("text")
    }
    tables = [item for item in iter_items(page) if item.get("component") == "VTable"]

    assert "同步概览" in texts
    assert "Trakt 看过 1" in texts
    assert "Trakt 在看 1" in texts
    assert {"Trakt 视频", "微信读书", "网易云音乐", "小宇宙"} <= texts
    assert "暂无 Trakt 同步历史记录" not in texts
    assert "暂无同步记录，执行一次同步后会在这里显示最近结果。" not in texts
    assert len(tables) == 4


def test_get_page_shows_single_empty_state(monkeypatch):
    """详情页无任何同步记录时只展示统一空态。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()

    page = plugin.get_page()

    def iter_items(items):
        """遍历详情页组件。"""
        for item in items:
            yield item
            yield from iter_items(item.get("content") or [])

    texts = {
        item.get("text") or (item.get("props") or {}).get("text")
        for item in iter_items(page)
        if item.get("text") or (item.get("props") or {}).get("text")
    }
    tables = [item for item in iter_items(page) if item.get("component") == "VTable"]

    assert "同步概览" in texts
    assert "暂无同步记录，执行一次同步后会在这里显示最近结果。" in texts
    assert "暂无 Trakt 同步历史记录" not in texts
    assert tables == []
