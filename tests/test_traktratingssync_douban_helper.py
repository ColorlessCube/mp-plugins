import importlib.util
import sys
import types
from pathlib import Path


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


class _Settings:
    """测试用配置桩。"""

    USER_AGENT = "MoviePilot-Test"


class _Response:
    """测试用 HTTP 响应桩。"""

    def __init__(self, status_code, data=None, text="", headers=None, cookies=None):
        """初始化响应。"""
        self.status_code = status_code
        self._data = data or {}
        self.text = text
        self.headers = headers or {}
        self.cookies = cookies or {}

    def json(self):
        """返回 JSON 响应。"""
        return self._data

    def __bool__(self):
        """模拟 requests.Response 的布尔行为。"""
        return self.status_code < 400


def _load_douban_helper_module(monkeypatch):
    """加载 DoubanHelper 并替换 MoviePilot 边界依赖。"""
    monkeypatch.setitem(sys.modules, "app.core.config", types.SimpleNamespace(settings=_Settings()))
    monkeypatch.setitem(sys.modules, "app.core.meta", types.SimpleNamespace(MetaBase=object))
    monkeypatch.setitem(sys.modules, "app.log", types.SimpleNamespace(logger=_Logger()))
    monkeypatch.setitem(sys.modules, "app.utils.http", types.SimpleNamespace(RequestUtils=object))

    helper_path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "traktratingssync"
        / "douban_helper.py"
    )
    spec = importlib.util.spec_from_file_location("traktratingssync_douban_helper_test", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_helper(module, monkeypatch):
    """构造不访问网络的 DoubanHelper 测试实例。"""
    helper = module.DoubanHelper.__new__(module.DoubanHelper)
    helper.cookies = {"dbcl2": "user:token", "ck": "mIHe", "bid": "bid-value"}
    helper.ck = "mIHe"
    helper.headers = {"User-Agent": "MoviePilot-Test", "Cookie": "dbcl2=user:token;ck=mIHe"}
    helper._notify = lambda *_args, **_kwargs: None
    helper._last_search_ts = 0.0
    helper._search_forbidden_until = 0.0
    helper._search_forbidden_count = 0
    monkeypatch.setattr(helper, "_sleep_before_request", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "_throttle_search", lambda *_args, **_kwargs: None)
    return helper


def test_podcast_rexxar_search_sends_ck_and_cookie(monkeypatch):
    """播客 rexxar 搜索应同时携带 ck 参数和 Cookie。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls = []

    class RequestUtilsStub:
        """记录请求参数的请求桩。"""

        def __init__(self, **kwargs):
            """记录初始化参数。"""
            self.kwargs = kwargs

        def get_res(self, **kwargs):
            """返回播客搜索命中。"""
            calls.append({"init": self.kwargs, "request": kwargs})
            return _Response(
                200,
                {
                    "subjects": {
                        "items": [
                            {
                                "layout": "podcast",
                                "target": {
                                    "id": "123456",
                                    "title": "硅谷101",
                                    "uri": "douban://douban.com/podcast/123456",
                                },
                            }
                        ]
                    }
                },
            )

    monkeypatch.setattr(module, "RequestUtils", RequestUtilsStub)

    title, subject_id = helper._search_podcast_subject("硅谷101")

    assert (title, subject_id) == ("硅谷101", "123456")
    assert calls[0]["request"]["params"]["ck"] == "mIHe"
    assert calls[0]["init"]["cookies"] == helper.cookies


def test_podcast_rexxar_need_login_falls_back_to_html(monkeypatch):
    """rexxar 返回 need_login 时应继续尝试 HTML 兜底搜索。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls = []

    class RequestUtilsStub:
        """按调用顺序返回 rexxar need_login 与 HTML 命中。"""

        def __init__(self, **kwargs):
            """记录初始化参数。"""
            self.kwargs = kwargs

        def get_res(self, **kwargs):
            """返回测试响应。"""
            calls.append({"init": self.kwargs, "request": kwargs})
            if len(calls) == 1:
                return _Response(403, {"code": 103, "msg": "need_login"}, "need_login")
            return _Response(
                200,
                text='<a href="https://www.douban.com/podcast/654321/">谐星聊天会</a>',
            )

    monkeypatch.setattr(module, "RequestUtils", RequestUtilsStub)

    title, subject_id = helper._search_podcast_subject("谐星聊天会")

    assert (title, subject_id) == ("谐星聊天会", "654321")
    assert len(calls) == 2


def test_refresh_ck_uses_requestutils_and_extracts_ck(monkeypatch):
    """刷新 ck 时应使用 RequestUtils，并从多个 Set-Cookie 中定位 ck。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls = []

    class RequestUtilsStub:
        """记录请求参数的请求桩。"""

        def __init__(self, **kwargs):
            """记录初始化参数。"""
            self.kwargs = kwargs

        def get_res(self, **kwargs):
            """返回包含 ck 的 Set-Cookie。"""
            calls.append({"init": self.kwargs, "request": kwargs})
            return _Response(
                200,
                headers={"Set-Cookie": "bid=abc; Path=/, ck=fresh-ck; Path=/; Domain=.douban.com"},
            )

    monkeypatch.setattr(module, "RequestUtils", RequestUtilsStub)

    helper._refresh_ck()

    assert helper.cookies["ck"] == "fresh-ck"
    assert calls[0]["init"]["cookies"] == helper.cookies
    assert calls[0]["request"]["url"] == helper._URL_DOUBAN


def test_post_interest_uses_requestutils(monkeypatch):
    """豆瓣状态提交应通过 RequestUtils 发送。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls = []

    class RequestUtilsStub:
        """记录提交参数的请求桩。"""

        def __init__(self, **kwargs):
            """记录初始化参数。"""
            self.kwargs = kwargs

        def post_res(self, **kwargs):
            """返回提交成功响应。"""
            calls.append({"init": self.kwargs, "request": kwargs})
            return _Response(200, {"r": 0})

    monkeypatch.setattr(module, "RequestUtils", RequestUtilsStub)

    assert helper._post_interest(
        url="https://movie.douban.com/j/subject/123/interest",
        referer="https://movie.douban.com/subject/123/",
        host="movie.douban.com",
        data={"ck": "mIHe"},
    )
    assert calls[0]["init"]["cookies"] == helper.cookies
    assert calls[0]["request"]["data"] == {"ck": "mIHe"}
