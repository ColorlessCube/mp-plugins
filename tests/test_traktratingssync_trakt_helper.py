import asyncio
import importlib.util
import socket
import sys
import types
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

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
    data = dict(kwargs.get("data", {}))

    def save_data(key, value):
        """同时更新持久化状态与本次写入记录。"""
        data[key] = value
        saved[key] = value

    helper = module.TraktHelper(
        client_id=kwargs.get("client_id", "client-id"),
        access_token=kwargs.get("access_token", ""),
        username="user",
        save_data_fn=save_data,
        get_data_fn=data.get,
        update_config_fn=lambda patch: updated.update(patch),
        send_notification_fn=lambda _title, _body: None,
        manual_mappings=kwargs.get("manual_mappings"),
        redirect_uri=kwargs.get("redirect_uri", "https://owned.example/callback"),
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
])
def test_all_trakt_requests_use_configured_proxy_and_identify_plugin(monkeypatch, method, args):
    """四类请求均使用 MoviePilot 代理和插件 UA，保留 API key 与 OAuth 认证。"""
    module = _load_trakt_helper_module(monkeypatch)
    calls = []

    def request_factory(**kwargs):
        """记录请求构造参数并阻止调用真实 HTTP。"""
        calls.append(kwargs)

        def request(**request_kwargs):
            """同时记录实际请求地址和参数。"""
            kwargs.update(request_kwargs)
            return _Response(403)

        return types.SimpleNamespace(
            get_res=request,
            post_res=request,
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
    if method in ("_refresh_access_token",):
        assert calls[0]["url"].startswith("https://auth.trakt.tv/oauth/")
    else:
        assert calls[0]["url"].startswith("https://api.trakt.tv/")


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
    from test_traktratingssync_netease_cookie_mode import _install_app_stubs

    _install_app_stubs(monkeypatch)
    package_name = "trakt_progress_test_plugin"
    for helper_name, class_name in (
        ("douban_helper", "DoubanHelper"), ("netease_helper", "NeteaseHelper"),
        ("trakt_helper", "TraktHelper"), ("weread_helper", "WereadHelper"),
        ("xiaoyuzhou_helper", "XiaoyuzhouHelper"),
    ):
        monkeypatch.setitem(sys.modules, f"{package_name}.{helper_name}",
                            types.SimpleNamespace(**{class_name: object}))
    plugin_path = Path(helper_module.__file__).with_name("__init__.py")
    matching_name = f"{package_name}.matching_helper"
    matching_spec = importlib.util.spec_from_file_location(matching_name, plugin_path.with_name("matching_helper.py"))
    matching_module = importlib.util.module_from_spec(matching_spec)
    monkeypatch.setitem(sys.modules, matching_name, matching_module)
    matching_spec.loader.exec_module(matching_module)
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
    """任一路失败均不覆盖缓存；完整成功空结果也保留历史，供后续完成核对。"""
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

    assert [item for item in saved if item[0] == "watching"] == ([("watching", watching)] if should_clear else [])
    assert watching == {"existing": {"title": "保留记录"}}
    assert len(refreshes) == refresh_count


def _pkce_callback(helper, code="one-time-code", **params):
    """用当前授权请求生成测试回跳地址。"""
    pending = helper._get_data("trakt_pkce_pending")
    return helper._redirect_uri + "?" + urlencode({"code": code, "state": pending["state"], **params})


def test_pkce_challenge_matches_rfc7636_and_keeps_verifier_private(monkeypatch):
    """使用公开 RFC 向量校验 S256，配置中不能出现 verifier。"""
    module = _load_trakt_helper_module(monkeypatch)
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    random_values = iter([verifier, "random-state"])
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _size: next(random_values))
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    helper, saved, updated = _build_helper(module, client_secret="")

    authorization_url = helper.begin_pkce_authorization()

    params = parse_qs(urlsplit(authorization_url).query)
    assert authorization_url.startswith("https://auth.trakt.tv/oauth/authorize?")
    assert params["code_challenge"] == ["E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == [helper._redirect_uri]
    assert saved["trakt_pkce_pending"]["expires_at"] == 1600
    assert saved["trakt_pkce_pending"]["code_verifier"] == verifier
    assert verifier not in str(updated)
    assert "client_secret" not in params


@pytest.mark.parametrize("uri", [
    "http://owned.example/callback", "https://localhost/callback", "https://127.0.0.1/callback",
    "https://192.168.1.1/callback", "https://[::1]/callback", "urn:ietf:wg:oauth:2.0:oob",
    "https://user:password@owned.example/callback", "https://owned.example/callback?state=old",
])
def test_pkce_rejects_invalid_redirect_before_resetting_tokens(monkeypatch, uri):
    """无效回跳配置不能启动授权或清除现有凭据。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module, redirect_uri=uri, access_token="existing-token")
    with pytest.raises(ValueError):
        helper.begin_pkce_authorization()
    assert not saved and not updated
    assert helper._access_token == "existing-token"


def test_pkce_exchange_omits_secret_saves_tokens_and_rejects_replay(monkeypatch):
    """交换请求必须带原始 verifier，无 Secret，成功后不能重放。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module, client_secret="", auth_mode="pkce")
    helper.begin_pkce_authorization()
    verifier = saved["trakt_pkce_pending"]["code_verifier"]
    callback = _pkce_callback(helper)
    calls = []

    def request_factory(**kwargs):
        """记录请求头、代理及授权交换参数。"""
        def post(**request):
            """返回固定 OAuth 响应。"""
            calls.append({**kwargs, **request})
            return _Response(200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 604800})
        return types.SimpleNamespace(post_res=post)

    monkeypatch.setattr(module, "RequestUtils", request_factory)
    assert helper.complete_pkce_authorization(callback) is True
    assert calls[0]["json"]["code_verifier"] == verifier
    assert calls[0]["json"]["code"] == "one-time-code"
    assert calls[0]["url"] == "https://auth.trakt.tv/oauth/token"
    assert "client_secret" not in calls[0]["json"]
    assert calls[0]["proxies"] == module.settings.PROXY
    assert calls[0]["headers"]["trakt-api-version"] == "2"
    assert saved["trakt_token"]["client_id"] == "client-id"
    assert saved["trakt_token"]["refresh_token"] == "new-refresh"
    assert saved["trakt_token"]["auth_mode"] == "pkce"
    assert updated["trakt_access_token"] == "new-access"
    assert updated["trakt_authorization_url"] == ""
    assert saved["trakt_pkce_pending"] == {}
    with pytest.raises(ValueError):
        helper.complete_pkce_authorization(callback)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["state", "duplicate_state", "duplicate_code", "redirect", "expired", "client", "cancelled"])
def test_pkce_invalid_callbacks_never_contact_trakt(monkeypatch, failure):
    """state、地址、时效或应用校验失败时，不能发起令牌交换。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, _updated = _build_helper(module, client_secret="")
    helper.begin_pkce_authorization()
    callback = _pkce_callback(helper)
    if failure == "state":
        callback = _pkce_callback(helper, state="wrong-state")
    elif failure == "duplicate_state":
        callback += "&state=another"
    elif failure == "duplicate_code":
        callback += "&code=another"
    elif failure == "redirect":
        callback = callback.replace("owned.example", "attacker.example")
    elif failure == "expired":
        saved["trakt_pkce_pending"]["expires_at"] = 0
    elif failure == "client":
        helper._client_id = "different-client"
    else:
        callback = _pkce_callback(helper, error="access_denied")
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: pytest.fail("不合法的回跳不能请求 Trakt"))
    with pytest.raises(ValueError):
        helper.complete_pkce_authorization(callback)


@pytest.mark.parametrize("response", [None, _Response(403), _Response(200, []), _Response(200, ValueError("private-code"))])
def test_pkce_failed_exchange_consumes_pending_request_without_saving_token(monkeypatch, response):
    """网络、访问权限或响应异常均消费请求，避免重放授权码。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module, client_secret="")
    helper.begin_pkce_authorization()
    callback = _pkce_callback(helper)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(post_res=lambda **_kwargs: response))
    assert helper.complete_pkce_authorization(callback) is False
    assert saved["trakt_pkce_pending"] == {}
    assert not saved.get("trakt_token")
    assert "trakt_access_token" not in updated


def test_expired_config_token_refreshes_without_secret_and_rotates_refresh_token(monkeypatch):
    """配置中的过期令牌不能短路续期，PKCE 刷新不需要 Secret。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module, client_secret="", access_token="expired-access", data={
        "trakt_token": {"client_id": "client-id", "access_token": "expired-access", "refresh_token": "old-refresh", "expires_at": 1},
    })
    calls = []

    def post(**request):
        """记录刷新请求并返回轮换后的凭据。"""
        calls.append(request)
        return _Response(200, {"access_token": "fresh-access", "refresh_token": "rotated-refresh", "expires_in": 604800})

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(post_res=post))
    assert helper.get_access_token() == "fresh-access"
    assert calls[0]["json"] == {"grant_type": "refresh_token", "client_id": "client-id", "refresh_token": "old-refresh", "redirect_uri": helper._redirect_uri}
    assert saved["trakt_token"]["refresh_token"] == "rotated-refresh"
    assert updated["trakt_access_token"] == "fresh-access"


def test_pkce_refresh_preserves_flow_metadata(monkeypatch):
    """续期后的令牌继续标记为 PKCE，避免下次错误携带 Secret。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, _updated = _build_helper(module, data={
        "trakt_token": {"client_id": "client-id", "auth_mode": "pkce", "refresh_token": "old-refresh"},
    })
    calls = []

    def post(**request):
        """记录 PKCE 续期请求。"""
        calls.append(request)
        return _Response(200, {"access_token": "access", "refresh_token": "refresh", "expires_in": 604800})

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(post_res=post))
    assert helper._refresh_access_token() is True
    assert "client_secret" not in calls[0]["json"]
    assert saved["trakt_token"]["auth_mode"] == "pkce"


def test_new_pkce_request_and_failed_exchange_preserve_existing_authorization(monkeypatch):
    """新请求或交换失败不会清除现有授权，成功回跳才替换令牌。"""
    module = _load_trakt_helper_module(monkeypatch)
    token = {"client_id": "client-id", "access_token": "still-valid", "refresh_token": "old-refresh", "expires_at": 9999999999}
    helper, saved, updated = _build_helper(module, access_token="still-valid", data={"trakt_token": token})
    helper.begin_pkce_authorization()
    assert helper.get_access_token() == "still-valid"
    assert "trakt_token" not in saved
    pending = saved["trakt_pkce_pending"]
    callback = helper._redirect_uri + "?" + urlencode({"code": "new-code", "state": pending["state"]})
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(post_res=lambda **_request: _Response(403)))
    assert not helper.complete_pkce_authorization(callback)
    assert helper.get_access_token() == "still-valid"
    assert "trakt_access_token" not in updated
    assert helper._get_data("trakt_token") == token


def test_pkce_without_token_does_not_start_blocking_device_flow(monkeypatch):
    """定时任务遇到尚未授权的新应用，只提示手动授权。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(module, client_secret="")
    assert not hasattr(helper, "_create_device_code_and_wait")
    assert helper.get_access_token() is None


def test_changed_client_id_cannot_reuse_or_refresh_old_credentials(monkeypatch):
    """旧应用令牌不允许发送到新应用的刷新请求。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module, client_secret="", access_token="old-access", data={
        "trakt_token": {"client_id": "deleted-client", "refresh_token": "old-refresh", "expires_at": 9999999999},
    })
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: pytest.fail("不能发送旧应用凭据"))
    assert helper.get_access_token() is None
    assert saved["trakt_token"] == {}
    assert updated["trakt_access_token"] == ""
