import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path


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

    def __init__(self, status_code, data=None, text=""):
        """初始化响应状态、JSON 数据和文本。"""
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def json(self):
        """返回 JSON 数据。"""
        return self._data


def _load_trakt_helper_module(monkeypatch):
    """加载 TraktHelper 并替换 MoviePilot 边界依赖。"""
    monkeypatch.setitem(sys.modules, "app.chain.media", types.SimpleNamespace(MediaChain=object))
    monkeypatch.setitem(sys.modules, "app.core.config", types.SimpleNamespace(global_vars=types.SimpleNamespace(loop=None)))
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

    assert helper.fetch_history("shows", "expired-token") == []
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
