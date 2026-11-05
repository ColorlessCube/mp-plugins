"""Trakt 只读请求的有界重试回归测试，禁止真实出站。"""
import io
import ssl
import types

import pytest
from requests import Response
from requests.exceptions import ChunkedEncodingError, ConnectionError, ProxyError, ReadTimeout, RequestException, SSLError
from urllib3.connection import HTTPConnection
from urllib3.exceptions import NewConnectionError, ProxyError as Urllib3ProxyError, SSLError as Urllib3SSLError

from test_traktratingssync_trakt_helper import _build_helper, _load_trakt_helper_module, _Response


def test_ratings_recovers_from_missing_response_without_mutating_state(monkeypatch):
    """一次无响应后应重读成功，不把瞬时故障留到下一轮。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    responses = iter([None, _Response(200, [])])
    calls, sleeps = [], []

    def get_response(**request):
        """模拟一次请求层无响应，随后返回合法空评分。"""
        calls.append(request)
        return next(responses)

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    assert helper.fetch_ratings("shows") == []
    assert len(calls) == 2
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 5
    assert not saved and not updated
    assert not helper.has_oauth_unauthorized()


@pytest.mark.parametrize("method,args,result", [
    ("fetch_ratings", ("shows",), []),
    ("fetch_playback", ("/sync/playback/episodes", "test-access"), []),
    ("fetch_history", ("shows", "test-access"), []),
    ("fetch_show_progress", ("123", "test-access"), {"seasons": []}),
    ("fetch_show_seasons", ("123",), []),
    ("fetch_show_details", ("123",), {"title": "示例剧集", "year": 2026, "ids": {"trakt": 123}}),
])
@pytest.mark.parametrize("failure", [ConnectionError, ReadTimeout])
def test_all_read_endpoints_recover_from_transport_exception(monkeypatch, method, args, result, failure):
    """评分、历史、播放与季度 GET 均应恢复，不能吞掉可重试异常。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(module)
    calls, sleeps = [], []

    def get_response(**request):
        """第一请求连接失败，第二请求成功。"""
        calls.append(request)
        if len(calls) == 1:
            raise failure("private-transport-sentinel test-access")
        return _Response(200, result)

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    assert getattr(helper, method)(*args) == result
    assert len(calls) == 2 and len(sleeps) == 1
    assert all(request.get("raise_exception") is True for request in calls)
    assert all(request.get("allow_redirects") is False for request in calls)
    assert not helper.has_oauth_unauthorized()


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_failure_is_retried_and_responses_are_closed(monkeypatch, status):
    """网关暂时失败可重读，成功和失败响应都释放连接。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    responses, closed, calls = [], [], []
    for code in [status, 200]:
        response = Response()
        response.status_code = code
        response._content = b"[]"
        response.headers["Content-Type"] = "application/json"
        monkeypatch.setattr(response, "close", lambda code=code: closed.append(code))
        responses.append(response)

    def get_response(**request):
        """模拟网关失败后的完整缓冲响应。"""
        calls.append(request)
        return responses[len(calls) - 1]

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)
    assert helper.fetch_ratings("shows") == []
    assert len(calls) == 2
    assert closed == [status, 200]
    assert not saved and not updated


@pytest.mark.parametrize("failure,transport,attempts", [
    (None, "no_response", 3),
    (ConnectionError("private-transport-sentinel test-access"), "connection", 3),
    (ProxyError("private-transport-sentinel test-access"), "proxy", 1),
    (ReadTimeout("private-transport-sentinel test-access"), "timeout", 3),
    (ChunkedEncodingError("private-transport-sentinel test-access"), "transfer", 3),
    (SSLError("private-transport-sentinel test-access"), "tls", 1),
    (RequestException("private-transport-sentinel test-access"), "request", 1),
])
def test_transport_failure_is_bounded_classified_and_redacted(monkeypatch, failure, transport, attempts):
    """耗尽重试才报告失败；日志只输出固定分类，不泄露底层异常内容。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls, sleeps, messages = [], [], []

    def get_response(**request):
        """持续产生同一种传输故障。"""
        calls.append(request)
        if failure is not None:
            raise failure
        return None

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    for level in ["debug", "info", "warning", "error"]:
        monkeypatch.setattr(module.logger, level, lambda message, *_args: messages.append(str(message)))
    assert helper.fetch_history("shows", "test-access") is None
    assert len(calls) == attempts and len(sleeps) == attempts - 1
    assert sleeps == ([1.0, 2.0] if attempts == 3 else [])
    assert f"transport={transport}" in "\n".join(messages)
    assert all("private-transport-sentinel" not in m and "test-access" not in m and "https://" not in m for m in messages)
    assert helper.get_request_issue_kind() == "temporary"
    assert not helper.has_oauth_unauthorized()
    assert not saved and not updated


@pytest.mark.parametrize("status,category", [(200, "temporary"), (204, "temporary"), (302, "temporary"),
    (401, "auth"), (403, "application"), (404, "not_found"), (429, "rate_limit")])
def test_terminal_responses_are_not_retried_or_reauthorized(monkeypatch, status, category):
    """成功、重定向、授权、权限、未找到和限流响应不能盲目重试。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls, sleeps = [], []
    response = _Response(status, [])
    response.headers["Retry-After"] = "3600"
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **request: calls.append(request) or response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(helper, "_refresh_access_token", lambda: pytest.fail("GET 重试层不能刷新 OAuth"))
    result = helper.fetch_history("shows", "test-access")
    assert result == ([] if status in (200, 204) else None)
    assert len(calls) == 1 and not sleeps
    assert helper.has_oauth_unauthorized() is (status == 401)
    assert helper.get_request_issue_kind() == category
    assert not saved and not updated


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_retries_stop_after_three_attempts(monkeypatch, status):
    """网关持续失败也必须停止，不能把来源失败伪装成空列表。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls, sleeps = [], []
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **request: calls.append(request) or _Response(status)))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    assert helper.fetch_history("shows", "test-access") is None
    assert len(calls) == 3 and sleeps == [1.0, 2.0]
    assert helper.get_request_issue_kind() == "temporary"
    assert not saved and not updated


@pytest.mark.parametrize("payload", [{}, ValueError("private-json-sentinel")])
def test_successful_but_invalid_payload_is_not_retried(monkeypatch, payload):
    """JSON 结构或解析异常不是网络抖动，保持来源失败而非反复拉取。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls = []
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **request: calls.append(request) or _Response(200, payload)))
    assert helper.fetch_ratings("shows") is None
    assert len(calls) == 1 and not saved and not updated


def test_oauth_refresh_post_is_not_retried(monkeypatch):
    """OAuth POST 失败只能保留现有凭据，不能套用 GET 自动重试。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module, data={
        "trakt_token": {"client_id": "client-id", "refresh_token": "test-refresh"},
    })
    calls = []
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        post_res=lambda **request: calls.append(request) or _Response(503)))
    assert helper._refresh_access_token() is False
    assert len(calls) == 1 and not saved and not updated


@pytest.mark.parametrize("tls_type", [SSLError, Urllib3SSLError, ssl.SSLError])
@pytest.mark.parametrize("link", ["original_error", "reason", "__cause__", "__context__", "args"])
def test_wrapped_tls_is_never_retried(monkeypatch, tls_type, link):
    """代理及连接异常的结构化链中出现 TLS 时必须立即停止，且不记录原文。"""
    tls_error = tls_type("certificate verify failed private-tls-sentinel test-access")
    if link == "original_error":
        failure = ProxyError(Urllib3ProxyError("proxy", tls_error))
    else:
        failure = ConnectionError("private-tls-sentinel test-access")
        setattr(failure, link, (tls_error,) if link == "args" else tls_error)
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls, sleeps, messages = [], [], []

    def get_response(**request):
        """持续抛出真实异常结构而不连接网络。"""
        calls.append(request)
        raise failure

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.logger, "warning", messages.append)
    assert helper.fetch_history("shows", "test-access") is None
    assert len(calls) == 1 and sleeps == []
    assert any("transport=tls" in message for message in messages)
    assert all("private-tls-sentinel" not in message and "test-access" not in message for message in messages)
    assert not saved and not updated and not helper.has_oauth_unauthorized()


@pytest.mark.parametrize("wrapped", [False, True, "transient_context"])
def test_connect_407_proxy_failure_is_not_retried(monkeypatch, wrapped):
    """CONNECT 407 无结构化瞬时原因，不能按代理连接错误重试或刷新令牌。"""
    original = OSError("Tunnel connection failed: 407 Proxy Authentication Required private-proxy-sentinel")
    failure = ProxyError(Urllib3ProxyError("proxy", original) if wrapped else original)
    if wrapped == "transient_context":
        failure.__context__ = NewConnectionError(HTTPConnection("proxy.invalid"), "private-proxy-sentinel")
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls, sleeps, messages = [], [], []

    def get_response(**request):
        """模拟请求层包装的代理隧道永久失败。"""
        calls.append(request)
        raise failure

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.logger, "warning", messages.append)
    assert helper.fetch_history("shows", "test-access") is None
    assert len(calls) == 1 and sleeps == []
    assert any("transport=proxy" in message for message in messages)
    assert all("407 Proxy Authentication" not in message and "private-proxy-sentinel" not in message for message in messages)
    assert not saved and not updated and not helper.has_oauth_unauthorized()


@pytest.mark.parametrize("message,link,transient", [
    ("Tunnel connection failed: 407 Proxy Authentication Required", "__context__", ConnectionResetError),
    ("unknown proxy failure", "__cause__", ReadTimeout),
])
def test_unknown_proxy_original_error_with_transient_chain_is_not_retried(monkeypatch, message, link, transient):
    """实际代理故障类型未知时，瞬时上下文或原因不能使非叶故障变成可重试。"""
    original = OSError(f"{message} private-proxy-sentinel")
    setattr(original, link, transient("private-transient-sentinel"))
    failure = ProxyError(Urllib3ProxyError("proxy", original))
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls, sleeps, messages = [], [], []

    def get_response(**request):
        """将瞬时异常挂在 original_error，而非最外层代理包装。"""
        calls.append(request)
        raise failure

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.logger, "warning", messages.append)
    assert helper.fetch_history("shows", "test-access") is None
    assert len(calls) == 1
    assert sleeps == []
    assert any("transport=proxy" in message for message in messages)
    assert all("private-proxy-sentinel" not in message and "private-transient-sentinel" not in message
               and "test-access" not in message for message in messages)
    assert not saved and not updated and not helper.has_oauth_unauthorized()


@pytest.mark.parametrize("shape", ["cycle", "deep"])
def test_uncertain_exception_graph_is_bounded_and_not_retried(monkeypatch, shape):
    """循环或超长异常链不能挂起分类，也不能把未排除的 TLS 当作可重试。"""
    failure = ProxyError("private-proxy-sentinel")
    if shape == "cycle":
        connection = NewConnectionError(HTTPConnection("proxy.invalid"), "private-proxy-sentinel")
        failure.__cause__ = connection
        connection.__cause__ = failure
    else:
        current = failure
        for _index in range(40):
            current.__cause__ = ConnectionError("private-proxy-sentinel")
            current = current.__cause__
        current.__cause__ = Urllib3SSLError("private-tls-sentinel")
    module = _load_trakt_helper_module(monkeypatch)
    helper, _saved, _updated = _build_helper(module)
    calls, sleeps = [], []

    def get_response(**request):
        """返回有环或超长异常结构。"""
        calls.append(request)
        raise failure

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    assert helper.fetch_history("shows", "test-access") is None
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("recovers", [False, True])
def test_recoverable_proxy_connection_failure_keeps_bounded_retry(monkeypatch, recovers):
    """明确的代理连接建立失败仍可恢复，最大三次并按一秒、二秒退避。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls, sleeps = [], []

    def get_response(**request):
        """使用 urllib3 实际代理异常和连接异常类型。"""
        calls.append(request)
        if recovers and len(calls) == 2:
            return _Response(200, [])
        raise ProxyError(Urllib3ProxyError("proxy", NewConnectionError(HTTPConnection("proxy.invalid"), "private-proxy-sentinel")))

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    assert helper.fetch_history("shows", "test-access") == ([] if recovers else None)
    assert len(calls) == (2 if recovers else 3)
    assert sleeps == ([1.0] if recovers else [1.0, 2.0])
    assert not saved and not updated and not helper.has_oauth_unauthorized()


def test_oauth_no_response_does_not_inherit_previous_get_transport(monkeypatch):
    """同实例 GET 的 TLS 诊断不能污染无响应 OAuth POST；续期仍只发一次。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module, data={
        "trakt_token": {"client_id": "client-id", "refresh_token": "test-refresh"},
    })
    gets, posts, sleeps, messages = [], [], [], []

    def get_response(**request):
        """产生先前 GET 的 TLS 分类。"""
        gets.append(request)
        raise SSLError("private-tls-sentinel")

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=get_response, post_res=lambda **request: posts.append(request)))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.logger, "warning", messages.append)
    assert helper.fetch_history("shows", "test-access") is None
    assert any("transport=tls" in message for message in messages)
    messages.clear()
    assert helper._refresh_access_token() is False
    assert len(gets) == 1 and len(posts) == 1 and sleeps == []
    assert messages and all("transport=tls" not in message for message in messages)
    assert all("private-tls-sentinel" not in message and "test-refresh" not in message for message in messages)
    assert not saved and not updated


def test_real_buffered_response_remains_json_readable_after_close(monkeypatch):
    """不替换真实 Response.close/json，完整缓冲后释放连接仍能解析正文。"""
    class BufferedRaw(io.BytesIO):
        """提供离线正文及可观察的底层连接释放边界。"""
        releases = 0

        def release_conn(self) -> None:
            """记录真实 Response.close 触发的连接归还。"""
            self.releases += 1

    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    response = Response()
    response.status_code = 200
    response.raw = BufferedRaw(b"[]")
    calls = []

    def get_response(**request):
        """在 HTTP 边界模拟非流式请求已完整消费正文。"""
        calls.append(request)
        assert request["stream"] is False
        assert response.content == b"[]"
        return response

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    assert helper.fetch_history("shows", "test-access") == []
    assert len(calls) == 1 and response.raw.releases == 1
    assert response.json() == []
    assert not saved and not updated
