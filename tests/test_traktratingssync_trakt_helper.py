import asyncio
import importlib.util
import socket
import sys
import types
from enum import Enum
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """禁止测试通过 DNS 或 socket 访问任何真实服务。"""
    def deny_network(*_args, **_kwargs):
        """真实网络调用必须在测试边界被替换。"""
        raise AssertionError("测试禁止真实网络访问")

    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)


class _MediaType(Enum):
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


class _Response:
    """测试用 HTTP 响应桩。"""

    def __init__(self, status_code, data=None, text="", content_type="application/json"):
        """初始化响应状态、JSON 数据和文本。"""
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text
        self.headers = {"Content-Type": content_type}

    def json(self):
        """返回 JSON 数据。"""
        if isinstance(self._data, Exception):
            raise self._data
        return self._data

    def __bool__(self):
        """模拟 requests.Response 在 4xx/5xx 时为 False。"""
        return self.status_code < 400


def _load_trakt_helper_module(monkeypatch):
    """加载 TraktHelper 并替换 MoviePilot 边界依赖。"""
    monkeypatch.setitem(sys.modules, "app.chain.media", types.SimpleNamespace(MediaChain=object))
    monkeypatch.setitem(sys.modules, "app.core.config", types.SimpleNamespace(
        global_vars=types.SimpleNamespace(loop=None),
        settings=types.SimpleNamespace(USER_AGENT="MoviePilot/test", PROXY={"https": "http://proxy.invalid"}),
    ))
    monkeypatch.setitem(sys.modules, "app.log", types.SimpleNamespace(logger=_Logger()))
    monkeypatch.setitem(sys.modules, "app.schemas.types", types.SimpleNamespace(MediaType=_MediaType))
    monkeypatch.setitem(sys.modules, "app.utils.http", types.SimpleNamespace(RequestUtils=object))

    helper_path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "traktratingssync"
        / "trakt_helper.py"
    )
    spec = importlib.util.spec_from_file_location("traktratingssync_trakt_helper_test", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.TraktHelper, "_sleep_before_request", lambda *_args: None)
    return module


def _build_helper(module, **kwargs):
    """构造 TraktHelper 测试实例。"""
    saved = {}
    updated = {}
    helper = module.TraktHelper(
        client_id="client-id",
        client_secret="client-secret",
        access_token=kwargs.get("access_token", ""),
        username="user",
        save_data_fn=lambda key, value: saved.update({key: value}),
        get_data_fn=lambda key: kwargs.get("data", {}).get(key),
        update_config_fn=lambda patch: updated.update(patch),
        send_notification_fn=lambda _title, _body: None,
        manual_mappings=kwargs.get("manual_mappings"),
    )
    return helper, saved, updated


def test_manual_mapping_is_used_before_douban_resolution(monkeypatch):
    """手动映射命中时应跳过自动匹配并直接提交豆瓣状态。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(
        module,
        manual_mappings={"imdb:tt6878038": "27099082"},
    )
    monkeypatch.setattr(helper, "_resolve_douban_info", lambda *_args, **_kwargs: {})
    calls = []
    douban_helper = types.SimpleNamespace(
        set_watching_status=lambda **kwargs: calls.append(kwargs) or True,
    )

    assert helper.sync_one_rate(
        {
            "rating": 8,
            "movie": {
                "title": "A Taxi Driver",
                "year": 2017,
                "ids": {"trakt": 294048, "tmdb": 437068, "imdb": "tt6878038"},
            },
        },
        {},
        {},
        module.MediaType.MOVIE,
        douban_helper,
        False,
    ) is True

    assert calls[0]["subject_id"] == "27099082"
    assert calls[0]["status"] == "collect"


def test_tmdb_mapping_uses_moviepilot_result_without_title_fallback(monkeypatch):
    """有 TMDB ID 时应直接使用 MoviePilot 映射结果，不额外标题兜底。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(module)
    calls = []

    class MediaChainStub:
        """MoviePilot 媒体链测试桩。"""

        async def async_get_doubaninfo_by_tmdbid(self, **kwargs):
            """返回 TMDB 映射结果。"""
            calls.append(("tmdb", kwargs))
            return {"id": "27099082", "title": "出租车司机"}

        async def async_match_doubaninfo(self, **kwargs):
            """记录不应发生的标题兜底调用。"""
            calls.append(("match", kwargs))
            return {"id": "00000000", "title": "错误匹配"}

    monkeypatch.setattr(module, "MediaChain", MediaChainStub)

    result = asyncio.run(helper._get_douban_info_by_tmdb(
        tmdb_id=437068,
        imdb_id="tt6878038",
        title="A Taxi Driver",
        year=2017,
        mtype=module.MediaType.MOVIE,
    ))

    assert result["id"] == "27099082"
    assert [name for name, _kwargs in calls] == ["tmdb"]


def test_tmdb_mapping_failure_does_not_fallback_to_trakt_title(monkeypatch):
    """TMDB 映射失败时不应使用 Trakt 标题自行二次匹配。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(module)
    calls = []

    class MediaChainStub:
        """MoviePilot 媒体链测试桩。"""

        async def async_get_doubaninfo_by_tmdbid(self, **kwargs):
            """返回未匹配。"""
            calls.append(("tmdb", kwargs))
            return {}

        async def async_match_doubaninfo(self, **kwargs):
            """记录不应发生的标题兜底调用。"""
            calls.append(("match", kwargs))
            return {"id": "00000000", "title": "错误匹配"}

    monkeypatch.setattr(module, "MediaChain", MediaChainStub)

    result = asyncio.run(helper._get_douban_info_by_tmdb(
        tmdb_id=437068,
        imdb_id="tt6878038",
        title="A Taxi Driver",
        year=2017,
        mtype=module.MediaType.MOVIE,
    ))

    assert result == {}
    assert [name for name, _kwargs in calls] == ["tmdb"]


def test_imdb_title_fallback_is_kept_when_tmdb_missing(monkeypatch):
    """缺少 TMDB ID 时仍可交由 MoviePilot 使用 IMDb/标题兜底。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(module)
    calls = []

    class MediaChainStub:
        """MoviePilot 媒体链测试桩。"""

        async def async_get_doubaninfo_by_tmdbid(self, **kwargs):
            """记录不应发生的 TMDB 映射调用。"""
            calls.append(("tmdb", kwargs))
            return {}

        async def async_match_doubaninfo(self, **kwargs):
            """返回 IMDb/标题兜底映射结果。"""
            calls.append(("match", kwargs))
            return {"id": "27099082", "title": "出租车司机"}

    monkeypatch.setattr(module, "MediaChain", MediaChainStub)

    result = asyncio.run(helper._get_douban_info_by_tmdb(
        tmdb_id=None,
        imdb_id="tt6878038",
        title="A Taxi Driver",
        year=2017,
        mtype=module.MediaType.MOVIE,
    ))

    assert result["id"] == "27099082"
    assert [name for name, _kwargs in calls] == ["match"]


def test_fetch_history_marks_oauth_unauthorized(monkeypatch):
    """Trakt OAuth 接口返回 401 时应记录失效标记。"""
    module = _load_trakt_helper_module(monkeypatch)

    class RequestUtilsStub:
        """返回 401 的请求桩。"""

        def __init__(self, *_args, **_kwargs):
            """初始化请求桩。"""

        def get_res(self, *_args, **_kwargs):
            """返回 401 响应。"""
            return _Response(401, text="unauthorized")

    monkeypatch.setattr(module, "RequestUtils", RequestUtilsStub)
    helper, _saved, _updated = _build_helper(module)

    assert helper.fetch_history("shows", "expired-token") is None
    assert helper.has_oauth_unauthorized() is True
    helper.reset_oauth_unauthorized()
    assert helper.has_oauth_unauthorized() is False


def test_force_reauthorize_refreshes_cached_refresh_token(monkeypatch):
    """强制重新授权时应优先使用缓存的 Refresh Token 续期。"""
    module = _load_trakt_helper_module(monkeypatch)

    class RequestUtilsStub:
        """返回新 token 的请求桩。"""

        def __init__(self, *_args, **_kwargs):
            """初始化请求桩。"""

        def post_res(self, *_args, **_kwargs):
            """返回续期成功响应。"""
            return _Response(
                200,
                {
                    "access_token": "new-access-token",
                    "refresh_token": "new-refresh-token",
                    "expires_in": 7200,
                },
            )

    monkeypatch.setattr(module, "RequestUtils", RequestUtilsStub)
    helper, saved, updated = _build_helper(
        module,
        access_token="expired-access-token",
        data={"trakt_token": {"refresh_token": "old-refresh-token"}},
    )

    assert helper.get_access_token(force_reauthorize=True) == "new-access-token"
    assert saved["trakt_token"]["refresh_token"] == "new-refresh-token"
    assert updated["trakt_access_token"] == "new-access-token"


@pytest.mark.parametrize("method,args", [
    ("fetch_ratings", ("movies",)),
    ("fetch_playback", ("/sync/playback/episodes", "access-token")),
    ("fetch_history", ("shows", "access-token")),
    ("_refresh_access_token", ()),
    ("_create_device_code_and_wait", ()),
    ("_exchange_device_token", ("device-code",)),
])
def test_all_trakt_requests_use_configured_proxy_and_identify_plugin(monkeypatch, method, args):
    """六类请求均使用 MoviePilot 代理和插件 UA，保留 API key 与 OAuth 认证。"""
    module = _load_trakt_helper_module(monkeypatch)
    calls = []

    def request_factory(**kwargs):
        """记录请求构造参数并阻止调用真实 HTTP。"""
        calls.append(kwargs)
        return types.SimpleNamespace(
            get_res=lambda **_kwargs: _Response(403),
            post_res=lambda **_kwargs: _Response(403),
        )

    monkeypatch.setattr(module, "RequestUtils", request_factory)
    helper, _saved, _updated = _build_helper(
        module, data={"trakt_token": {"refresh_token": "refresh-token"}},
    )
    getattr(helper, method)(*args)

    assert len(calls) == 1
    assert calls[0]["proxies"] == module.settings.PROXY
    assert calls[0]["headers"]["User-Agent"] == "MoviePilot/test Plugin/TraktRatingsSync"
    assert calls[0]["headers"]["trakt-api-key"] == "client-id"
    assert calls[0]["headers"]["trakt-api-version"] == "2"
    if method in ("fetch_playback", "fetch_history"):
        assert calls[0]["headers"]["Authorization"] == "Bearer access-token"


@pytest.mark.parametrize("method,source", [
    ("fetch_playback", "/sync/playback/episodes"),
    ("fetch_history", "shows"),
])
@pytest.mark.parametrize("response,expected", [
    (None, None),
    (_Response(403), None),
    (_Response(200, {}), None),
    (_Response(200, ValueError("invalid JSON")), None),
    (_Response(200, []), []),
    (_Response(204), []),
])
def test_progress_sources_distinguish_failure_from_empty(monkeypatch, method, source, response, expected):
    """失败或异常 JSON 返回 None，成功无记录才返回空列表。"""
    module = _load_trakt_helper_module(monkeypatch)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **_kwargs: response,
    ))
    helper, _saved, _updated = _build_helper(module)

    assert getattr(helper, method)(source, "access-token") == expected
    assert helper.has_oauth_unauthorized() is False


@pytest.mark.parametrize("method,source", [
    ("fetch_ratings", "movies"),
    ("fetch_playback", "/sync/playback/episodes"),
    ("fetch_history", "shows"),
])
@pytest.mark.parametrize("content_type,body,is_html", [
    ("text/html", "<!doctype html><html>private-response-marker</html>", True),
    ("application/json", '{"error":"private-response-marker"}', False),
    ("text/plain", "Forbidden private-response-marker", False),
])
def test_403_diagnostics_classify_without_exposing_body(monkeypatch, method, source, content_type, body, is_html):
    """403 分类只给出有限诊断，不泄露正文或误报 token 过期及私有评分。"""
    module = _load_trakt_helper_module(monkeypatch)
    messages = []
    monkeypatch.setattr(module.logger, "warning", messages.append)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **_kwargs: _Response(403, text=body, content_type=content_type),
    ))
    helper, _saved, _updated = _build_helper(module)
    args = (source,) if method == "fetch_ratings" else (source, "access-token")

    getattr(helper, method)(*args)

    message = "\n".join(messages)
    assert f"html={is_html}" in message
    assert f"content_type={content_type}" in message
    assert ("上游拦截" if is_html else "Client ID") in message
    assert "private-response-marker" not in message
    assert "access-token" not in message
    assert "私有" not in message
    assert helper.has_oauth_unauthorized() is False


def _load_progress_plugin(monkeypatch, helper_module):
    """加载真实插件编排方法，仅替换插件基类与其他平台依赖。"""
    monkeypatch.setitem(sys.modules, "app.plugins", types.SimpleNamespace(_PluginBase=object))
    package_name = "trakt_progress_test_plugin"
    for helper_name, class_name in (
        ("douban_helper", "DoubanHelper"), ("netease_helper", "NeteaseHelper"),
        ("trakt_helper", "TraktHelper"), ("weread_helper", "WereadHelper"),
        ("xiaoyuzhou_helper", "XiaoyuzhouHelper"),
    ):
        monkeypatch.setitem(sys.modules, f"{package_name}.{helper_name}",
                            types.SimpleNamespace(**{class_name: object}))
    plugin_path = Path(helper_module.__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location(package_name, plugin_path,
                                                submodule_search_locations=[str(plugin_path.parent)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package_name, module)
    spec.loader.exec_module(module)
    return module.TraktRatingsSync()


@pytest.mark.parametrize("responses,should_clear,refresh_count", [
    ([None, _Response(200, [])], False, 0),
    ([_Response(200, []), _Response(403)], False, 0),
    ([_Response(200, [{"show": {"title": "有效剧集"}}]), _Response(403)], False, 0),
    ([_Response(403), _Response(403)], False, 0),
    ([_Response(200, []), _Response(200, [])], True, 0),
    ([_Response(401), _Response(200, []), _Response(401), _Response(200, [])], False, 1),
    ([_Response(401), _Response(200, []), _Response(200, []), _Response(200, [])], True, 1),
])
def test_progress_sync_preserves_watching_until_both_sources_succeed(monkeypatch, responses, should_clear, refresh_count):
    """任一路失败及刷新后仍失败均保留在看，只有完整成功空结果才清空。"""
    module = _load_trakt_helper_module(monkeypatch)
    pending = iter(responses)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **_kwargs: next(pending),
    ))
    helper, _saved, _updated = _build_helper(module, access_token="access-token")
    refreshes = []
    monkeypatch.setattr(helper, "_refresh_access_token", lambda: refreshes.append(True) or True)
    plugin = _load_progress_plugin(monkeypatch, module)
    plugin._trakt_helper = helper
    plugin._sync_type = "shows"
    watching = {"existing": {"title": "保留记录"}}
    saved = []
    plugin.get_data = lambda _key: watching
    plugin.save_data = lambda key, value: saved.append((key, value))
    monkeypatch.setattr(helper, "sync_one_progress", lambda *_args: pytest.fail("不完整来源不应提交豆瓣"))

    plugin._sync_progress()

    assert saved == ([("watching", {})] if should_clear else [])
    assert watching == {"existing": {"title": "保留记录"}}
    assert len(refreshes) == refresh_count
