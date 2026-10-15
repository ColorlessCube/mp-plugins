"""观察年龄仅提示、网站认证仍拦截的离线回归测试。"""
import json
import socket

import pytest

from test_traktratingssync_ass_cookie import NOW, _bundle, _config, _cookie, _iso, _load_adapter, _responses
from test_traktratingssync_douban_helper import _Response, _load_douban_helper_module
from test_traktratingssync_netease_cookie_mode import _load_plugin_module


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """禁止回归测试产生真实出站。"""
    def deny(*_args, **_kwargs):
        raise AssertionError("禁止真实网络")
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.mark.parametrize("site", ["douban", "netease"])
@pytest.mark.parametrize("age", [3600, 3601, 400000, 9000000])
def test_old_observation_remains_usable(monkeypatch, tmp_path, site, age):
    """浏览器离线数月也不等同于 Cookie 失效，不重标时间。"""
    module = _load_adapter(monkeypatch)
    cookies = [_cookie()] if site == "douban" else [_cookie("MUSIC_U", "music", "netease")]
    bundle = _bundle(site, cookies)
    bundle.update(snapshot_created_at=_iso(NOW - age - 100), observed_at=_iso(NOW - age))
    original = json.dumps(bundle)
    helper = module.AssCookieHelper(_config(tmp_path), site)
    assert helper._project(original, NOW)
    assert helper.observation_age_seconds == age
    assert json.dumps(bundle) == original


@pytest.mark.parametrize("failure,code", [
    ("invalid", "ass_cookie_invalidated"), ("expired_cookie", "ass_cookie_required_missing"),
    ("identity", "ass_cookie_binding_mismatch"), ("future", "ass_cookie_clock_skew"),
])
def test_age_relaxation_preserves_hard_failures(monkeypatch, tmp_path, failure, code):
    """观察年龄放宽不影响退出、Cookie 到期、身份和未来时钟检查。"""
    module = _load_adapter(monkeypatch)
    bundle = _bundle()
    bundle.update(snapshot_created_at=_iso(NOW - 500000), observed_at=_iso(NOW - 400000))
    if failure == "invalid":
        bundle.update(state="invalid", reason="required_cookie_missing", cookies=[])
    elif failure == "expired_cookie":
        bundle["cookies"][0].update(session=False, expirationDate=NOW)
    elif failure == "identity":
        bundle["account_id"] = "other"
    else:
        bundle["observed_at"] = _iso(NOW + 61)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    with pytest.raises(module.AssCookieError, match=f"^{code}$"):
        helper._project(json.dumps(bundle), NOW)
    assert helper.observation_age_seconds is None


def test_stale_read_still_requires_current_authorization(monkeypatch, tmp_path):
    """老观察通过后，下次鉴权失败仍不得返回缓存 Cookie。"""
    module = _load_adapter(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    replies = _responses()
    bundle = _bundle()
    bundle.update(snapshot_created_at=_iso(NOW - 500000), observed_at=_iso(NOW - 400000))
    replies[2]["value"] = json.dumps(bundle)
    def get(*_args):
        if replies:
            return replies.pop(0)
        raise module.AssCookieError("ass_authentication_rejected")
    monkeypatch.setattr(helper, "_get", get)
    assert helper.read_cookies() == {"dbcl2": '"123:test"'}
    with pytest.raises(module.AssCookieError, match="authentication_rejected"):
        helper.read_cookies()
    assert helper.observation_age_seconds is None


def test_stale_hint_does_not_mark_source_failed_and_clears_on_refresh(monkeypatch):
    """提示独立于来源故障，重复读取不刷日志，新观察后清除提示。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({"douban_cookie_source": "ass"})
    logs = []
    monkeypatch.setattr(module.logger, "warning", lambda *args: logs.append(args))
    class Adapter:
        """只返回合成 Cookie 与观察年龄。"""
        observation_age_seconds = 400000
        def __init__(self, *_args):
            """不读取文件或网络。"""
        def read_cookies(self):
            """返回合成的完整消费结果。"""
            return {"dbcl2": "SENSITIVE_SENTINEL"}
    monkeypatch.setattr(module, "AssCookieHelper", Adapter)
    for _ in range(3):
        assert plugin._read_ass_cookies("douban")
    assert len(logs) == 1
    assert not plugin._source_issue_this_run
    state = plugin.get_data("ass_observation_warnings")
    assert state["douban"]["age_seconds"] == 400000
    assert "SENSITIVE_SENTINEL" not in json.dumps(state) + str(logs)
    page = json.dumps(plugin.get_page(), ensure_ascii=False)
    assert "观察时间较旧" in page and "不直接阻断" in page
    Adapter.observation_age_seconds = 0
    plugin._read_ass_cookies("douban")
    assert not plugin.get_data("ass_observation_warnings")
    Adapter.observation_age_seconds = 400000
    plugin._read_ass_cookies("douban")
    assert plugin.get_data("ass_observation_warnings") and len(logs) == 1


@pytest.mark.parametrize("response,authenticated,persistent", [
    (_Response(200, text='<script>var _USER_ID = "123";</script>', cookies={"ck": "fresh"}), True, False),
    (_Response(200, text='<div id="db-global-nav"><div class="nav-user-account"><a href="https://www.douban.com/accounts/logout?source=main">退出</a></div></div>', cookies={"ck": "fresh"}), True, False),
    (_Response(200, text='<div id="db-global-nav"><div class="nav-user-account"><a href="/accounts/logout">退出</a></div></div>', cookies={"ck": "fresh"}), True, False),
    (_Response(200, text='<a href="https://www.douban.com/accounts/logout">正文伪造退出</a>', cookies={"ck": "fresh"}), False, False),
    (_Response(200, text='<div id="db-global-nav"><div class="nav-user-account"><a href="https://evil.invalid/accounts/logout">退出</a></div></div>', cookies={"ck": "fresh"}), False, False),
    (_Response(200, text='<script>var _USER_ID = "456";</script><div id="db-global-nav"><div class="nav-user-account"><a href="/accounts/logout">退出</a></div></div>', cookies={"ck": "fresh"}), False, True),
    (_Response(200, text='<script>var _USER_ID = "456";</script>', cookies={"ck": "fresh"}), False, True),
    (_Response(200, text='<script>var _USER_ID = "";</script>', cookies={"ck": "fresh"}), False, True),
    (_Response(200, text='<html>anonymous</html>', cookies={"ck": "fresh"}), False, False),
    (_Response(302, headers={"Location": "https://accounts.douban.com/passport/login"}), False, True),
    (_Response(200, text='<script src="TCaptcha.js"></script>', cookies={"ck": "fresh"}), False, True),
    (_Response(429, cookies={"ck": "fresh"}), False, False),
    (_Response(503, cookies={"ck": "fresh"}), False, False),
    (None, False, False),
    (_Response(200, text='<script>var _USER_ID = "123";</script>'), False, False),
    (_Response(200, text='<a href="https://www.douban.com/people/123/">他人主页</a>', cookies={"ck": "fresh"}), False, False),
    (_Response(200, {"code": 103, "msg": "need_login"}, cookies={"ck": "fresh"}), False, True),
    (_Response(403, cookies={"ck": "fresh"}), False, True),
])
def test_ass_login_needs_more_than_csrf(monkeypatch, response, authenticated, persistent):
    """CSRF 不是登录证明，匿名、账号不符和网络失败都不得写入。"""
    module = _load_douban_helper_module(monkeypatch)
    monkeypatch.setattr(module.time, "sleep", lambda *_args: None)
    calls = []
    class Request:
        """仅模拟只读首页，不允许发起写入。"""
        def __init__(self, **_kwargs):
            """不打开真实 Session。"""
        def get_res(self, **kwargs):
            """记录唯一的首页登录检查。"""
            calls.append(kwargs)
            return response
    helper = module.DoubanHelper(credential_provider=lambda: {"dbcl2": '"123:test"'},
                                request_factory=Request, credential_reference="ass:fixed")
    assert helper.is_authenticated == authenticated
    assert helper.requests_paused != authenticated
    assert bool(helper._state.get("requires_verification")) == persistent
    assert len(calls) == 1 and calls[0]["allow_redirects"] is False
    if not authenticated:
        assert not helper.ck
        assert not helper._post_interest("https://movie.douban.com/j/subject/123/interest",
                                         "https://movie.douban.com/subject/123/", "movie.douban.com", {})


def test_stale_snapshot_run_defers_then_recovers_and_submits_pending(monkeypatch, tmp_path):
    """完整编排先因网络失败保留队列，下一轮旧快照登录有效即可补交。"""
    module = _load_plugin_module(monkeypatch)
    douban = _load_douban_helper_module(monkeypatch)
    monkeypatch.setattr(module, "DoubanHelper", douban.DoubanHelper)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    monkeypatch.setattr(douban.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(douban.DoubanHelper, "_read_interest_fields", lambda *_args: {})
    bundle = _bundle()
    bundle.update(snapshot_created_at=_iso(NOW - 500000), observed_at=_iso(NOW - 400000))
    calls, posts = [], []
    def get(_self, _session, _token, path, _deadline):
        calls.append(path)
        replies = _responses()
        if path.endswith("access-status"):
            return replies[0]
        if path.endswith("/metadata"):
            return replies[1]
        return {**replies[2], "value": json.dumps(bundle)}
    monkeypatch.setattr(module.AssCookieHelper, "_get", get)
    class Website:
        """模拟暂时不可用后恢复的已登录网站。"""
        available = False
        def __init__(self, **kwargs):
            """只保留本次合成 Cookie。"""
            self.cookies = dict(kwargs["cookies"])
        def get_res(self, **kwargs):
            """只有首页 GET 是登录检查，禁止任意站点。"""
            assert kwargs["url"] == "https://www.douban.com/"
            if not self.available:
                return None
            return _Response(200, text='<script>var _USER_ID = "123";</script>', cookies={"ck": "test-ck"})
        def post_res(self, **kwargs):
            """只有通过账号/CSRF 检查后才能提交现有队列。"""
            assert self.cookies == {"dbcl2": '"123:test"', "ck": "test-ck"}
            posts.append(kwargs)
            return _Response(200, {"r": 0})
    monkeypatch.setattr(module, "CredentialRequestUtils", Website)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({**_config(tmp_path), "enable": True, "douban_cookie_source": "ass"})
    monkeypatch.setattr(plugin, "_send_notification", lambda *_args: True)
    url = "https://movie.douban.com/j/subject/123/interest"
    pending = {url: {"data": {"interest": "collect"}, "referer": "https://movie.douban.com/subject/123/", "host": "movie.douban.com"}}
    plugin.save_data("douban_sync_state", {"pending": pending})
    plugin.run()
    state = plugin.get_data("douban_sync_state")
    assert url in state["pending"] and not state.get("requires_verification") and not posts
    assert plugin.get_data("last_run")["status"] == "paused"
    Website.available = True
    plugin.run()
    assert len(posts) == 1 and posts[0]["url"] == url
    assert not plugin.get_data("douban_sync_state")["pending"]
    assert plugin.get_data("last_run")["written"] == 1
    assert not plugin.get_data("last_run")["source_errors"]
    assert plugin.get_data("ass_observation_warnings")["douban"]["age_seconds"] == 400000
    assert calls.count("/api/v1/secrets/access-status") == 3
    assert len(calls) == 12
    assert plugin._douban_helper is None and not plugin._run_lock.locked()
    assert "123:test" not in json.dumps(plugin._data)
