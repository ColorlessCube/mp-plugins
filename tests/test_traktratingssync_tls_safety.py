"""Trakt TLS 恢复与完整异常图安全回归，全程禁止真实出站。"""
import ssl
import types

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, ProxyError, RequestException, SSLError
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import MaxRetryError, ProxyError as Urllib3ProxyError, SSLError as Urllib3SSLError

from test_traktratingssync_trakt_helper import _build_helper, _load_trakt_helper_module, _Response


@pytest.mark.parametrize("method,args,payload", [
    ("fetch_ratings", ("shows",), []),
    ("fetch_playback", ("/sync/playback/episodes", "test-access"), []),
    ("fetch_history", ("shows", "test-access"), []),
    ("fetch_show_progress", ("123", "test-access"), {"seasons": []}),
    ("fetch_show_seasons", ("123",), []),
    ("fetch_show_details", ("123",), {"title": "示例", "year": 2026, "ids": {"trakt": 123}}),
])
def test_read_endpoints_explicitly_verify_certificates(monkeypatch, method, args, payload):
    """每个只读入口显式验证 HTTPS，不能依赖宿主不安全默认值。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls = []
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        get_res=lambda **request: calls.append(request) or _Response(200, payload)))
    assert getattr(helper, method)(*args) == payload
    assert len(calls) == 1
    assert calls[0].get("verify") is True
    assert calls[0]["allow_redirects"] is False
    assert not updated
    assert set(saved) <= {"trakt_seasons", "trakt_shows"}


def _tls_chain(inner):
    """构造 requests/urllib3 的真实嵌套结构，不依赖错误文案。"""
    return SSLError(MaxRetryError(HTTPSConnectionPool("trakt.example"), "/private-url", Urllib3SSLError(inner)))


@pytest.mark.parametrize("failure,category", [
    (_tls_chain(ssl.SSLCertVerificationError(1, "private-certificate-sentinel")), "tls_certificate"),
    (_tls_chain(ssl.SSLEOFError(8, "private-eof-sentinel")), "tls_eof"),
    (_tls_chain(ssl.SSLError(1, "private-other-sentinel")), "tls_other"),
    (SSLError("EOF occurred certificate verify failed private-text-sentinel"), "tls_other"),
])
def test_tls_classification_uses_structured_types_not_text(monkeypatch, failure, category):
    """固定分类由异常结构决定，文案中的 EOF/证书字样不作为证据。"""
    module = _load_trakt_helper_module(monkeypatch)
    assert module.TraktHelper._classify_read_error(failure) == (category, category == "tls_eof")


@pytest.mark.parametrize("recovers", [True, False])
def test_confirmed_tls_eof_read_recovers_with_existing_bound(monkeypatch, recovers):
    """只有确认的 EOF GET 可最多三次，恢复空列表不等于失败。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    calls, sleeps, messages = [], [], []
    failure = _tls_chain(ssl.SSLEOFError(8, "private-eof-sentinel test-access"))

    def get_response(**request):
        """只在离线边界注入 EOF，不进行真实请求。"""
        calls.append(request)
        if recovers and len(calls) == 2:
            return _Response(200, [])
        raise failure

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(get_res=get_response))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.logger, "warning", messages.append)
    assert helper.fetch_history("shows", "test-access") == ([] if recovers else None)
    assert len(calls) == (2 if recovers else 3)
    assert sleeps == ([1.0] if recovers else [1.0, 2.0])
    assert all(request["verify"] is True for request in calls)
    assert any("transport=tls_eof" in message for message in messages)
    assert all("private-" not in message and "test-access" not in message for message in messages)
    assert not helper.has_oauth_unauthorized() and not saved and not updated


@pytest.mark.parametrize("shape", ["certificate", "unknown_parent", "generic_tls_parent", "mixed_leaf", "cycle",
                                        "deep", "wide", "nested_args", "reason_string", "unknown_reason", "proxy_original_text", "shared_dag"])
def test_tls_exception_graph_fails_closed_on_every_unsafe_path(monkeypatch, shape):
    """证书、未知中间节点、混合原因与不完整图均不因 EOF 叶子而重试。"""
    eof = ssl.SSLEOFError(8, "private-eof-sentinel")
    failure = _tls_chain(eof)
    expected = ("tls_other", False)
    if shape == "certificate":
        failure.__context__ = ssl.SSLCertVerificationError(1, "private-certificate-sentinel")
        expected = ("tls_certificate", False)
    elif shape in ("unknown_parent", "generic_tls_parent"):
        unknown = OSError("private-unknown-sentinel") if shape == "unknown_parent" else ssl.SSLError(1, "private-unknown-sentinel")
        unknown.__cause__ = eof
        failure = _tls_chain(unknown)
    elif shape == "mixed_leaf":
        failure.__context__ = ConnectionResetError("private-reset-sentinel")
    elif shape == "cycle":
        eof.__cause__ = failure
    elif shape == "deep":
        current = failure
        for _ in range(40):
            current.__context__ = ConnectionError("private-wrapper-sentinel")
            current = current.__context__
        current.__cause__ = eof
    elif shape == "wide":
        failure.args += tuple(ConnectionError("private-wrapper-sentinel") for _ in range(40))
    elif shape == "nested_args":
        failure.args += ([ssl.SSLCertVerificationError(1, "private-certificate-sentinel")],)
    elif shape in ("reason_string", "unknown_reason"):
        setattr(failure, "reason", "CERTIFICATE_VERIFY_FAILED" if shape == "reason_string" else "UNKNOWN_TLS_REASON")
        if shape == "reason_string":
            expected = ("tls_certificate", False)
    elif shape == "proxy_original_text":
        failure = ProxyError(Urllib3ProxyError("proxy", OSError("private-407-sentinel")))
        failure.args[0].original_error = "private-407-sentinel"
        failure.args[0].args = ("proxy",)
        failure.__cause__ = eof
    else:
        failure.__cause__ = eof
        failure.__context__ = eof
        expected = ("tls_eof", True)
    module = _load_trakt_helper_module(monkeypatch)
    assert module.TraktHelper._classify_read_error(failure) == expected


@pytest.mark.parametrize("inner,attempts", [(ssl.SSLEOFError(8, "private-eof-sentinel"), 3),
                                          (ssl.SSLCertVerificationError(1, "private-cert-sentinel"), 1)])
def test_read_transport_sanitizes_before_host_logging_preserving_tls_graph(monkeypatch, inner, attempts):
    """会话先阻止宿主记录底层异常原文，分类仍能检查原始结构。"""
    module = _load_trakt_helper_module(monkeypatch)
    helper, saved, updated = _build_helper(module)
    messages, calls, sleeps = [], [], []

    class RequestBoundary:
        """模拟宿主的异常日志和 raise_exception 接口，不替代真实会话。"""

        def __init__(self, session=None, **kwargs):
            """保存每次构造的安全会话。"""
            self.session = session
            self.kwargs = kwargs

        def get_res(self, raise_exception=False, **kwargs):
            """宿主仍会输出异常字符串，必须在到达这里之前脱敏。"""
            try:
                request = self.session.request if self.session is not None else requests.request
                return request("GET", headers=self.kwargs["headers"], timeout=self.kwargs["timeout"],
                               proxies=self.kwargs["proxies"], **kwargs)
            except RequestException as error:
                messages.append(str(error))
                if raise_exception:
                    raise
                return None

    def send(_adapter, request, **kwargs):
        """仅在适配器出口注入原始异常图。"""
        calls.append((request, kwargs))
        raise _tls_chain(inner)

    monkeypatch.setattr(module, "RequestUtils", RequestBoundary)
    monkeypatch.setattr(HTTPAdapter, "send", send)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    monkeypatch.setattr(module.logger, "warning", messages.append)
    assert helper.fetch_history("shows", "test-access") is None
    assert len(calls) == attempts and len(sleeps) == attempts - 1
    assert "private-" not in "\n".join(messages)
    assert "test-access" not in "\n".join(messages)
    assert not saved and not updated and not helper.has_oauth_unauthorized()
