"""ASS 只读适配器的离线契约、安全传输及插件失败隔离测试。"""
import importlib.util
import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
import requests

from test_traktratingssync_douban_helper import _build_helper, _load_douban_helper_module, _Response
from test_traktratingssync_netease_cookie_mode import _load_plugin_module

TENANT = "00000000-0000-4000-8000-000000000001"
SECRET = "00000000-0000-4000-8000-000000000002"
KEY = "00000000-0000-4000-8000-000000000003"
TOKEN = "ass_" + UUID(KEY).hex + "_" + "x" * 43
NOW = 1800000000.0


def _iso(value=NOW):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")


def _load_adapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.utils.http", types.SimpleNamespace(RequestUtils=object))
    path = Path(__file__).resolve().parents[1] / "plugins/traktratingssync/ass_cookie_helper.py"
    spec = importlib.util.spec_from_file_location("ass_cookie_helper_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(tmp_path):
    key_file = tmp_path / "read-key"
    key_file.write_text(TOKEN + "\n")
    key_file.chmod(0o600)
    return {"ass_origin": "https://ass.invalid:18443", "ass_key_file": str(key_file),
            "ass_tenant_id": TENANT, "douban_ass_secret_id": SECRET, "douban_ass_account_id": "main",
            "netease_ass_secret_id": SECRET, "netease_ass_account_id": "main"}


def _cookie(name="dbcl2", value='"123:test"', site="douban", host=False):
    domain = "douban.com" if site == "douban" else "music.163.com"
    return {"name": name, "value": value, "domain": domain if host else "." + domain,
            "path": "/", "hostOnly": host, "secure": False, "httpOnly": True,
            "sameSite": "unspecified", "session": True, "storeId": "0"}


def _bundle(site="douban", cookies=None, version=1):
    return {"format": "ass-cookie-bundle", "schema_version": version, "site_id": site,
            "account_id": "main", "producer_id": TENANT, "snapshot_id": SECRET,
            "ordering_mode": "snapshot_created_at", "snapshot_created_at": _iso(NOW - 100),
            "cookie_generated_at": None, "observed_at": _iso(), "state": "captured", "reason": None,
            "source_origins": ["https://www.douban.com" if site == "douban" else "https://music.163.com"],
            "cookies": cookies if cookies is not None else [_cookie()]}


def test_domain_dbcl2_balanced_quotes_and_observation_refresh(monkeypatch, tmp_path):
    """保留原生 dbcl2 引号并区分生成时间与观察刷新。"""
    module = _load_adapter(monkeypatch)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    bundle = _bundle(cookies=[_cookie(), _cookie("bid", "test-bid"), _cookie("ck", "old-ck")])
    assert helper._project(json.dumps(bundle), NOW) == {"dbcl2": '"123:test"', "bid": "test-bid"}
    # 老生成时间仍可被新鲜观察证明；不伪造网站签发时间。
    bundle["snapshot_created_at"] = _iso(NOW - 100000)
    assert helper._project(json.dumps(bundle), NOW)["dbcl2"] == '"123:test"'


@pytest.mark.parametrize("equal", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_v2_csrf_scope_is_explicit_even_for_equal_values(monkeypatch, tmp_path, equal, reverse):
    """双作用域即使值相同也拒绝隐式选择，显式选择与顺序无关。"""
    module = _load_adapter(monkeypatch)
    cookies = [_cookie("MUSIC_U", "music", "netease"), _cookie("__csrf", "domain", "netease"),
               _cookie("__csrf", "domain" if equal else "host", "netease", True)]
    if reverse:
        cookies.reverse()
    bundle = _bundle("netease", cookies, 2)
    config = _config(tmp_path)
    helper = module.AssCookieHelper(config, "netease")
    with pytest.raises(module.AssCookieError, match="^ass_cookie_ambiguous$"):
        helper._project(json.dumps(bundle), NOW)
    for scope, expected in (("host", "domain" if equal else "host"), ("domain", "domain")):
        config["netease_ass_csrf_scope"] = scope
        projected = module.AssCookieHelper(config, "netease")._project(json.dumps(bundle), NOW)
        assert projected == {"MUSIC_U": "music", "__csrf": expected}
    bundle["schema_version"] = 1
    with pytest.raises(module.AssCookieError, match="^ass_cookie_bundle_invalid$"):
        module.AssCookieHelper(config, "netease")._project(json.dumps(bundle), NOW)


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": "2"}, {"schema_version": 3},
    {"snapshot_id": "not-uuid"}, {"producer_id": "not-uuid"}, {"cookie_generated_at": _iso()},
    {"observed_at": _iso(NOW - 4000)}, {"observed_at": _iso(NOW + 61)},
    {"snapshot_created_at": _iso(NOW + 1)}, {"observed_at": "2026-01-01T00:00:00"},
    {"observed_at": "2027-01-15T08:00:00.000001Z"},
    {"account_id": "other"}, {"site_id": "other"}, {"source_origins": ["https://evil.invalid"]},
    {"cookies": []}, {"reason": "required_cookie_missing"}, {"unknown": "field"},
])
def test_bundle_fail_closed(monkeypatch, tmp_path, change):
    """拒绝错误身份、格式、时钟、版本与过期观察。"""
    module = _load_adapter(monkeypatch)
    bundle = _bundle()
    bundle.update(change)
    with pytest.raises(module.AssCookieError):
        module.AssCookieHelper(_config(tmp_path), "douban")._project(json.dumps(bundle), NOW)


@pytest.mark.parametrize("change", [
    {"storeId": "1"}, {"partitionKey": {"topLevelSite": "https://evil.invalid"}},
    {"domain": ".evil.invalid"}, {"path": "/other"}, {"hostOnly": True}, {"hostOnly": "false"},
    {"secure": 1}, {"sameSite": "unknown"}, {"expirationDate": NOW + 1},
    {"value": "unsafe;value"}, {"value": "unsafe\r\nCookie: injected"}, {"value": '"unbalanced'},
    {"value": "non-ascii-中文"}, {"value": "x" * 4097},
])
def test_cookie_attributes_and_header_injection_are_rejected(monkeypatch, tmp_path, change):
    """所有 Cookie 都必须通过上下文、边界和请求头安全验证。"""
    module = _load_adapter(monkeypatch)
    cookie = _cookie()
    cookie.update(change)
    with pytest.raises(module.AssCookieError):
        module.AssCookieHelper(_config(tmp_path), "douban")._project(json.dumps(_bundle(cookies=[cookie])), NOW)


def test_expiry_invalidation_duplicate_and_size_bounds(monkeypatch, tmp_path):
    """过期、墓碑、重复身份、重复字段及超限内容均不能消费。"""
    module = _load_adapter(monkeypatch)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    expired = _cookie()
    expired.update(session=False, expirationDate=NOW)
    with pytest.raises(module.AssCookieError, match="required_missing"):
        helper._project(json.dumps(_bundle(cookies=[expired])), NOW)
    invalid = _bundle(cookies=[])
    invalid.update(state="invalid", reason="required_cookie_missing")
    with pytest.raises(module.AssCookieError, match="invalidated"):
        helper._project(json.dumps(invalid), NOW)
    for raw in (json.dumps(_bundle(cookies=[_cookie(), _cookie()])),
                json.dumps(_bundle()).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 2'),
                json.dumps(_bundle()).replace('"session": true', '"expirationDate": NaN, "session": false'),
                "x" * 16385, json.dumps(_bundle(cookies=[_cookie()] * 101))):
        with pytest.raises(module.AssCookieError, match="bundle_invalid"):
            helper._project(raw, NOW)


@pytest.mark.parametrize("scope", ["host", "domain"])
def test_explicit_csrf_scope_does_not_fall_back(monkeypatch, tmp_path, scope):
    """已指定的 CSRF 作用域缺失时不能改用另一个作用域。"""
    module = _load_adapter(monkeypatch)
    config = _config(tmp_path)
    config["netease_ass_csrf_scope"] = scope
    cookies = [_cookie("MUSIC_U", "music", "netease"),
               _cookie("__csrf", "wrong", "netease", host=scope == "domain")]
    with pytest.raises(module.AssCookieError, match="required_missing"):
        module.AssCookieHelper(config, "netease")._project(json.dumps(_bundle("netease", cookies)), NOW)


@pytest.mark.parametrize("origin", ["http://ass.invalid", "https://user:pass@ass.invalid", "https://ass.invalid/path",
                                    "https://ass.invalid?key=x", "https://ass.invalid/#x", "https://ass.invalid:bad"])
def test_invalid_connections_rejected_before_read(monkeypatch, tmp_path, origin):
    """不允许带认证信息、路径、查询或非 HTTPS 的连接配置。"""
    module = _load_adapter(monkeypatch)
    config = _config(tmp_path)
    config["ass_origin"] = origin
    with pytest.raises(module.AssCookieError, match="configuration_invalid"):
        module.AssCookieHelper(config, "douban")


def test_key_file_permissions_symlinks_rotation_and_no_cache(monkeypatch, tmp_path):
    """文件密钥受权限和链接检查保护，替换后的无效密钥不会回退缓存。"""
    module = _load_adapter(monkeypatch)
    config = _config(tmp_path)
    helper = module.AssCookieHelper(config, "douban")
    assert helper._read_key() == TOKEN
    path = Path(config["ass_key_file"])
    path.chmod(0o644)
    with pytest.raises(module.AssCookieError, match="key_file_invalid"):
        helper._read_key()
    path.chmod(0o600)
    path.write_text("not-a-key")
    with pytest.raises(module.AssCookieError, match="key_file_invalid"):
        helper._read_key()
    path.write_text(TOKEN + "x" * 130)
    with pytest.raises(module.AssCookieError, match="key_file_invalid"):
        helper._read_key()
    path.write_text(TOKEN)
    link = tmp_path / "link"
    link.symlink_to(path)
    config["ass_key_file"] = str(link)
    with pytest.raises(module.AssCookieError, match="key_file_invalid"):
        module.AssCookieHelper(config, "douban")._read_key()
    assert not any("token" in key for key in vars(helper))


def _responses():
    status = {"tenant_id": TENANT, "key_id": KEY, "value_scope_authorized": True, "revision": 1,
              "active_grant_count": 2, "checked_at": _iso(), "expires_at": None}
    metadata = {"id": SECRET, "name": "douban", "current_version": 4}
    value = {"name": "douban", "version": 4, "value": json.dumps(_bundle())}
    return [status, metadata, value, dict(metadata)]


def test_fresh_read_pins_key_tenant_secret_and_version(monkeypatch, tmp_path):
    """每次操作重读身份与版本，实例只保留非敏感连接配置。"""
    module = _load_adapter(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    responses = _responses() + _responses()
    paths = []
    def fake_get(_session, token, path, _deadline):
        assert token == TOKEN
        paths.append(path)
        return responses.pop(0)
    monkeypatch.setattr(helper, "_get", fake_get)
    assert helper.read_cookies() == {"dbcl2": '"123:test"'}
    assert helper.read_cookies() == {"dbcl2": '"123:test"'}
    assert len(paths) == 8 and paths[0] == paths[4] == "/api/v1/secrets/access-status"
    assert set(vars(helper)) == {"_site", "_origin", "_key_file", "_tenant", "_secret", "_account",
                                 "_site_id", "_age", "_csrf_scope", "_get"}  # _get 是测试桩。


@pytest.mark.parametrize("index,change", [(0, {"tenant_id": SECRET}), (0, {"key_id": SECRET}),
    (0, {"value_scope_authorized": False}), (0, {"active_grant_count": 3}),
    (0, {"checked_at": _iso(NOW - 61)}),
    (0, {"expires_at": _iso(NOW - 1)}), (1, {"id": TENANT}), (1, {"name": "other"}),
    (1, {"current_version": True}), (2, {"version": 5}), (2, {"version": True}),
    (3, {"current_version": 5}), (3, {"id": TENANT})])
def test_authorized_read_rejects_binding_races(monkeypatch, tmp_path, index, change):
    """身份不符和读取期间的版本或条目变化必须阻断。"""
    module = _load_adapter(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    responses = _responses()
    responses[index].update(change)
    monkeypatch.setattr(helper, "_get", lambda *_args: responses.pop(0))
    with pytest.raises(module.AssCookieError):
        helper.read_cookies()


class _StreamResponse:
    """可记录关闭及分块读取的合成响应。"""

    def __init__(self, status=200, content=b'{"ok": true}'):
        """初始化固定 HTTP 状态与合成响应字节。"""
        self.status_code = status
        self.content = content
        self.closed = False

    def iter_content(self, _size):
        """只返回测试合成数据，不打开网络。"""
        yield self.content


@pytest.mark.parametrize("status,code", [(302, "ass_unavailable"), (401, "ass_authentication_rejected"),
    (403, "ass_access_denied"), (404, "ass_secret_unavailable"), (429, "ass_rate_limited"), (503, "ass_unavailable")])
def test_transport_tls_no_redirect_and_fixed_http_errors(monkeypatch, tmp_path, status, code):
    """传输始终校验 TLS、禁止跳转、关闭响应并返回固定错误。"""
    module = _load_adapter(monkeypatch)
    response = _StreamResponse(status)
    calls = []
    class RequestStub:
        """记录安全传输选项的 RequestUtils 边界桩。"""
        def __init__(self, **kwargs):
            """记录安全选项，不产生请求。"""
            calls.append(kwargs)
        @contextmanager
        def response_manager(self, *args, **kwargs):
            """模拟自动关闭，不产生 HTTP 请求。"""
            calls.append((args, kwargs))
            try:
                yield response
            finally:
                response.closed = True
    monkeypatch.setattr(module, "RequestUtils", RequestStub)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    with pytest.raises(module.AssCookieError, match="^" + code + "$"):
        helper._get(None, TOKEN, "/api/v1/secrets/access-status", module.time.monotonic() + 20)
    assert calls[1][1] == {"verify": True, "allow_redirects": False, "stream": True}
    assert calls[0]["timeout"] == (2, 5) and response.closed


@pytest.mark.parametrize("content", [b"x" * (20 * 1024 + 1), b"[]", b"{bad}", b'{"key":1,"key":2}', b'{"x":NaN}'])
def test_bounded_response_and_sanitized_decoder_failure(monkeypatch, tmp_path, content):
    """超长响应和非法 JSON 不进入 Cookie 解码或异常输出。"""
    module = _load_adapter(monkeypatch)
    class RequestStub:
        def __init__(self, **_kwargs):
            """只接受测试传入的连接选项。"""
        @contextmanager
        def response_manager(self, *_args, **_kwargs):
            """返回合成响应流，不连接服务器。"""
            yield _StreamResponse(content=content)
    monkeypatch.setattr(module, "RequestUtils", RequestStub)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    with pytest.raises(module.AssCookieError, match="^ass_response_invalid$"):
        helper._get(None, TOKEN, "/fixed", module.time.monotonic() + 20)


def test_requestutils_cannot_log_sensitive_transport_exception(monkeypatch):
    """传输异常中的敏感哨兵不能传给 RequestUtils 日志。"""
    module = _load_adapter(monkeypatch)
    def fail(*_args, **_kwargs):
        raise requests.RequestException("SENSITIVE_COOKIE_SENTINEL")
    monkeypatch.setattr(requests.Session, "request", fail)
    with module._SafeSession() as session:
        with pytest.raises(requests.RequestException) as caught:
            session.request("GET", "https://test.invalid")
    assert str(caught.value) == "credential_transport_failed"
    assert caught.value.__suppress_context__


def test_plugin_preserves_reference_config_without_read_or_plaintext_writeback(monkeypatch, tmp_path):
    """初始化不联网，配置和表单只有引用且保持手动默认兼容。"""
    module = _load_plugin_module(monkeypatch)
    config = {**_config(tmp_path), "douban_cookie_source": "ass", "netease_cookie_source": "ass"}
    plugin = module.TraktRatingsSync()
    plugin.init_plugin(config)
    assert plugin._has_netease_source()
    plugin._merge_update_config({"xiaoyuzhou_cookie": "manual-other-platform"})
    saved = plugin._config_updates[-1]
    assert all(saved[key] == value for key, value in config.items())
    assert "ass_key" not in saved and "ass_cookie" not in saved
    assert saved["douban_cookie"] == saved["netease_cookie"] == ""
    form, defaults = plugin.get_form()
    models = []
    def walk(items):
        for item in items:
            models.append(item.get("props", {}).get("model"))
            walk(item.get("content", []))
    walk(form)
    assert set(plugin._ass_defaults()) <= set(models)
    assert defaults["douban_cookie_source"] == defaults["netease_cookie_source"] == "manual"


def test_netease_failure_does_not_fall_back_to_cached_or_manual(monkeypatch):
    """ASS 读取失败跳过网易云，不消费手动或缓存凭据。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({"netease_cookie_source": "ass", "netease_cookie": "MUSIC_U=manual"})
    plugin._netease_helper = object()
    monkeypatch.setattr(plugin, "_send_notification", lambda *_args: True)
    plugin._sync_netease()
    assert plugin._netease_helper is None
    issue = plugin.get_data("notification_issues")["网易云音乐"]
    assert issue["category"] == "ass" and issue["active"]
    assert plugin._netease_cookie == "MUSIC_U=manual"


def test_netease_operation_uses_one_read_and_clears_helper(monkeypatch):
    """网易云账号及播放记录构成一次操作，下一次必须重新读取。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({"netease_cookie_source": "ass"})
    reads, helpers = [], []
    monkeypatch.setattr(plugin, "_read_ass_cookies", lambda site: reads.append(site) or {"MUSIC_U": "sentinel"})
    class NeteaseStub:
        """模拟账号和历史读取均使用同一个操作 Cookie。"""
        def __init__(self, cookies, **_kwargs):
            """保存单次操作的合成凭据。"""
            self.cookies = cookies
            helpers.append(self)
        def get_recent_albums(self, limit):
            """无专辑不触发豆瓣写入。"""
            assert self.cookies == {"MUSIC_U": "sentinel"} and limit == 20
            return []
    monkeypatch.setattr(module, "NeteaseHelper", NeteaseStub)
    plugin._sync_netease()
    plugin._sync_netease()
    assert reads == ["netease", "netease"] and len(helpers) == 2
    assert all(not helper.cookies for helper in helpers) and plugin._netease_helper is None


def test_ass_pause_is_not_cleared_by_cookie_refresh(monkeypatch):
    """切换为 ASS 或更新 Cookie 不能自动解除验证码暂停。"""
    module = _load_douban_helper_module(monkeypatch)
    state = {"requires_verification": True, "reason": "验证码", "cookie_fingerprint": "old"}
    calls = []
    helper = module.DoubanHelper(credential_provider=lambda: calls.append("read") or {"dbcl2": "new"},
                                credential_reference="ass:fixed", get_data_fn=lambda _key: state,
                                save_data_fn=lambda *_args: None)
    assert helper.requests_paused and helper._state["requires_verification"] and not calls


def test_douban_rechecks_before_write_and_keeps_one_snapshot_for_get_post(monkeypatch):
    """每个目标新鲜读取，CSRF、内容保护和提交复用同一快照。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    calls, reads = [], []
    helper._credential_provider = lambda: reads.append("read") or {"dbcl2": "fresh"}
    class RequestStub:
        """CSRF、内容和提交请求均使用合成响应。"""
        def __init__(self, **kwargs):
            """拍下操作内的合成凭据用于一致性断言。"""
            self.cookies = dict(kwargs["cookies"])
        def get_res(self, **kwargs):
            """返回新 CSRF，不调用网站。"""
            calls.append(("csrf", self.cookies, kwargs))
            return _Response(200, cookies={"ck": "freshck"})
        def post_res(self, **kwargs):
            """记录同一份凭据的写入。"""
            calls.append(("post", self.cookies, kwargs))
            return _Response(200, {"r": 0})
    monkeypatch.setattr(module, "RequestUtils", RequestStub)
    monkeypatch.setattr(helper, "_read_interest_fields", lambda *_args: calls.append(("existing", dict(helper.cookies), {})) or {})
    url = "https://movie.douban.com/j/subject/123/interest"
    helper._state["pending"][url] = {"data": {"interest": "collect"}, "referer": "https://movie.douban.com/subject/123/", "host": "movie.douban.com"}
    assert helper._submit_pending(url)
    assert reads == ["read"] and [call[0] for call in calls] == ["csrf", "existing", "post"]
    assert calls[1][1] == calls[2][1] == {"dbcl2": "fresh", "ck": "freshck"}
    assert calls[0][2]["verify"] is True and calls[2][2]["allow_redirects"] is False
    assert "fresh" not in json.dumps(helper._state)


def test_douban_ass_failure_stops_all_requests_and_preserves_queue(monkeypatch):
    """ASS 故障不耗写入额度、不丢队列、不制造网站验证码状态。"""
    module = _load_douban_helper_module(monkeypatch)
    helper = _build_helper(module, monkeypatch)
    def fail():
        raise RuntimeError("SENSITIVE_SENTINEL")
    helper._credential_provider = fail
    url = "https://movie.douban.com/j/subject/123/interest"
    entry = {"data": {"interest": "collect"}, "referer": "https://movie.douban.com/subject/123/", "host": "movie.douban.com"}
    helper._state["pending"][url] = entry
    assert not helper._submit_pending(url)
    assert helper.requests_paused and helper.cookies == {} and helper.ck is None
    assert helper._state["pending"][url] == entry and not helper._attempted
    assert not helper._state.get("requires_verification")


def test_douban_ass_unavailable_stops_all_sources_and_run_releases_plaintext(monkeypatch):
    """目标凭据失败阻断所有来源写入，保留队列并释放本轮 Helper。"""
    module = _load_plugin_module(monkeypatch)
    douban_module = _load_douban_helper_module(monkeypatch)
    monkeypatch.setattr(module, "DoubanHelper", douban_module.DoubanHelper)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({"enable": True, "douban_cookie_source": "ass", "weread_api_key": "manual-source"})
    pending = {"https://movie.douban.com/j/subject/123/interest": {
        "data": {"interest": "collect", "private": ""}, "referer": "https://movie.douban.com/subject/123/", "host": "movie.douban.com"}}
    plugin.save_data("douban_sync_state", {"pending": pending, "synced": {}})
    sources = []
    monkeypatch.setattr(plugin, "_sync_weread", lambda: sources.append("weread"))
    monkeypatch.setattr(plugin, "_send_notification", lambda *_args: True)
    plugin.run()
    assert not sources and plugin._douban_helper is None and not plugin._run_lock.locked()
    assert plugin.get_data("douban_sync_state")["pending"] == pending
    assert plugin.get_data("last_run")["written"] == 0
    assert plugin.get_data("notification_issues")["豆瓣"]["category"] == "ass"


def test_netease_curl_warnings_and_responses_never_echo_credentials(monkeypatch):
    """解析警告、网站正文和传输异常都不能进入日志或通知。"""
    logs = []
    class LoggerStub:
        """仅收集合成测试日志，不连接任何系统日志端点。"""
        def __getattr__(self, _name):
            return lambda *args, **_kwargs: logs.append(str(args))
    monkeypatch.setitem(sys.modules, "app.log", types.SimpleNamespace(logger=LoggerStub()))
    monkeypatch.setitem(sys.modules, "app.utils.http", types.SimpleNamespace(RequestUtils=object))
    path = Path(__file__).resolve().parents[1] / "plugins/traktratingssync/netease_helper.py"
    spec = importlib.util.spec_from_file_location("netease_helper_ass_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sentinel = "SENSITIVE_SENTINEL"
    raw = f"curl 'https://music.163.com/weapi/test?csrf_token={sentinel}url' -H 'Cookie: MUSIC_U={sentinel}; __csrf={sentinel}one; __csrf={sentinel}two'"
    helper = module.NeteaseHelper(raw, notify_fn=lambda *args: logs.append(str(args)))
    monkeypatch.setattr(helper, "_sleep_before_request", lambda *_args: None)
    class ResponseStub:
        """模拟恶意响应文本及带敏感信息的 HTTP 错误。"""
        status_code = 200
        text = sentinel
        def raise_for_status(self):
            """错误只交给固定分类，不能记录正文或异常文本。"""
            if self.status_code != 200:
                raise requests.HTTPError(sentinel, response=self)
        def json(self):
            """上游消息故意含哨兵，消费者不能输出它。"""
            return {"code": 301, "msg": sentinel}
    response = ResponseStub()
    class RequestStub:
        """所有请求在 Mock 边界结束。"""
        def __init__(self, **_kwargs):
            """不输出 Cookie 字典。"""
        def post_res(self, **kwargs):
            """验证 HTTPS 安全选项并返回合成响应。"""
            assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
            return response
    monkeypatch.setattr(module, "RequestUtils", RequestStub)
    assert helper._request("nuser/account/get") is None
    response.status_code = 403
    assert helper._request("nuser/account/get") is None
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(sentinel)))
    assert helper._request("nuser/account/get") is None
    assert sentinel not in " ".join(logs)


def test_credential_transport_forces_tls_and_no_redirects(monkeypatch):
    """网站调用无法覆盖安全选项，Session 关闭且不使用环境代理。"""
    module = _load_adapter(monkeypatch)
    calls = []
    class RequestStub:
        """仅检查 RequestUtils 收到的参数。"""
        def __init__(self, session, **_kwargs):
            """确认没有跨请求共享或环境注入的会话。"""
            assert session.trust_env is False
        def get_res(self, **kwargs):
            """返回不含凭据的合成结果。"""
            calls.append(kwargs)
            return "ok"
    monkeypatch.setattr(module, "RequestUtils", RequestStub)
    assert module.CredentialRequestUtils(cookies={"dbcl2": "test"}).get_res(
        url="https://www.douban.com", verify=False, allow_redirects=True) == "ok"
    assert calls[0]["verify"] is True and calls[0]["allow_redirects"] is False


def test_netease_misconfiguration_does_not_block_douban(monkeypatch, tmp_path):
    """网易云的独立引用与作用域错误不能影响豆瓣消费。"""
    module = _load_adapter(monkeypatch)
    config = _config(tmp_path)
    config.update(netease_ass_csrf_scope="invalid", netease_ass_secret_id="invalid")
    assert module.AssCookieHelper(config, "douban")._project(json.dumps(_bundle()), NOW)
    with pytest.raises(module.AssCookieError, match="configuration_invalid"):
        module.AssCookieHelper(config, "netease")


def test_netease_ass_failure_allows_other_sources_to_continue(monkeypatch):
    """仅来源失败时其余平台继续，并正确标记部分完成。"""
    module = _load_plugin_module(monkeypatch)
    plugin = module.TraktRatingsSync()
    plugin.init_plugin({"enable": True, "douban_cookie": "dbcl2=manual", "netease_cookie_source": "ass",
                        "xiaoyuzhou_cookie": "manual"})
    sources = []
    monkeypatch.setattr(plugin, "_sync_xiaoyuzhou", lambda: sources.append("xiaoyuzhou"))
    monkeypatch.setattr(plugin, "_send_notification", lambda *_args: True)
    plugin.run()
    assert sources == ["xiaoyuzhou"]
    assert plugin.get_data("last_run")["status"] == "partial"
    assert plugin.get_data("last_run")["source_errors"] == ["网易云音乐"]


def test_access_status_accepts_server_microsecond_timestamps(monkeypatch, tmp_path):
    """access-status 与快照不同，服务器身份回执允许微秒精度。"""
    module = _load_adapter(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    responses = _responses()
    responses[0]["checked_at"] = datetime.fromtimestamp(NOW - 0.123456, timezone.utc).isoformat()
    responses[0]["expires_at"] = datetime.fromtimestamp(NOW + 100.123456, timezone.utc).isoformat()
    monkeypatch.setattr(helper, "_get", lambda *_args: responses.pop(0))
    assert helper.read_cookies() == {"dbcl2": '"123:test"'}


def test_complete_rest_read_and_revocation_before_next_operation(monkeypatch, tmp_path):
    """完整 Mock REST 流程验证序列化与 Cookie jar 隔离，撤销后禁止读取旧值。"""
    module = _load_adapter(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    responses = [_StreamResponse(content=json.dumps(data).encode()) for data in _responses()]
    responses.append(_StreamResponse(status=401))
    closed, paths = [], []
    class RequestStub:
        """Mock 四次 GET 后撤销，不能重新消费先前的值。"""
        def __init__(self, session, headers, **_kwargs):
            """只检查合成 token，jar 不能跨 GET 携带身份 Cookie。"""
            assert session.trust_env is False and not session.cookies
            assert headers["Authorization"] == "Bearer " + TOKEN
            self.session = session
        @contextmanager
        def response_manager(self, method, url, **kwargs):
            """所有返回都仅来自合成数据。"""
            assert method == "GET" and kwargs["verify"] and not kwargs["allow_redirects"]
            paths.append(url.removeprefix("https://ass.invalid:18443"))
            response = responses.pop(0)
            self.session.cookies.set("server", "unexpected")
            try:
                yield response
            finally:
                closed.append(True)
    monkeypatch.setattr(module, "RequestUtils", RequestStub)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    assert helper.read_cookies() == {"dbcl2": '"123:test"'}
    with pytest.raises(module.AssCookieError, match="authentication_rejected"):
        helper.read_cookies()
    assert len(paths) == len(closed) == 5 and not responses
    assert paths == ["/api/v1/secrets/access-status", "/api/v1/secrets/douban/metadata",
                     "/api/v1/secrets/douban/value", "/api/v1/secrets/douban/metadata",
                     "/api/v1/secrets/access-status"]


def test_read_deadline_blocks_before_transport(monkeypatch, tmp_path):
    """整体期限已到时不再打开后续 HTTP 请求。"""
    module = _load_adapter(monkeypatch)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    with pytest.raises(module.AssCookieError, match="^ass_unavailable$"):
        helper._get(None, TOKEN, "/fixed", 99)


def test_release_metadata_matches_plugin_and_documentation(monkeypatch):
    """代码、市场版本与接入说明同批维护。"""
    module = _load_plugin_module(monkeypatch)
    root = Path(__file__).resolve().parents[1]
    metadata = json.loads((root / "package.json").read_text())["TraktRatingsSync"]
    assert metadata["version"] == module.TraktRatingsSync.plugin_version == "3.21.0"
    assert "v3.21.0" in metadata["history"]
    assert (root / "plugins/traktratingssync/ASS.md").is_file()
