"""Bark 回执不确定性及真实 HTTP 边界脱敏回归，禁止实际发送。"""
import types

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, ReadTimeout, RequestException, SSLError

import pytest

from test_traktratingssync_presentation import _plugin


def _capture_logs(monkeypatch, module):
    """同时捕获兼容 LoggerManager 的四级日志。"""
    messages = []
    for level in ("debug", "info", "warning", "error"):
        monkeypatch.setattr(module.logger, level, lambda message, *args: messages.append(str(message) % args if args else str(message)))
    return messages


def test_bark_missing_ack_is_unknown_and_keeps_issue_pending(monkeypatch):
    """无回执不能称已失败或已送达，不能清空来源异常与待确认状态。"""
    plugin, module = _plugin(monkeypatch, bark_webhook_url="https://bark.example/test-device-key",
                             notification_channel="bark", trakt_client_id="test-client")
    calls = []
    messages = _capture_logs(monkeypatch, module)
    monkeypatch.setattr(module.time, "time", lambda: 100000)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        post_res=lambda **request: calls.append(request)))
    assert not plugin._notify_issue("Trakt", "读取未完成", "")
    state = plugin.get_data("notification_issues")["Trakt"]
    assert state["active"] is True and state["delivery_pending"] is True
    assert state["last_success_at"] == 0 and state["last_attempt_at"] == 100000
    assert len(calls) == 1
    assert not plugin._notify_issue("Trakt", "读取未完成", "")
    assert len(calls) == 1
    text = "\n".join(messages)
    assert "送达未确认" in text and "结果未知" in text
    assert "HTTP None" not in text and "发送失败" not in text and "发送成功" not in text


@pytest.mark.parametrize("ack,accepted", [
    ({"code": 200, "message": "success"}, True),
    ({"code": 500, "message": "private-response-sentinel"}, False),
    ({}, False), ({"code": "200"}, False), ({"code": True}, False),
    ([], False), (ValueError("private-json-sentinel"), False),
])
def test_bark_requires_valid_application_ack_and_closes_response(monkeypatch, ack, accepted):
    """HTTP 200 不足以确认接受，拒绝和缺失回执不得更新成功状态。"""
    plugin, module = _plugin(monkeypatch, bark_webhook_url="https://bark.example/test-device-key")
    messages = _capture_logs(monkeypatch, module)
    calls, closed = [], []

    def response_json():
        """模拟完整缓冲正文或解析错误。"""
        if isinstance(ack, Exception):
            raise ack
        return ack

    response = types.SimpleNamespace(status_code=200, json=response_json, close=lambda: closed.append(True))
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(
        post_res=lambda **request: calls.append(request) or response))
    assert plugin._send_bark_notification("private-title-sentinel", "private-body-sentinel") is accepted
    assert len(calls) == 1 and closed == [True]
    text = "\n".join(messages)
    assert all(value not in text for value in ("private-title", "private-body", "private-response", "private-json"))
    assert ("服务端已接受" in text) is accepted
    if not accepted and not (isinstance(ack, dict) and ack.get("code") == 500):
        assert "送达未确认" in text


def _install_http_boundary(monkeypatch, module, messages):
    """模拟宿主 RequestUtils 的日志边界，保留真实 requests 会话与重定向机制。"""
    class RequestBoundary:
        """传输前后的插件行为是真实代码，仅宿主 HTTP 封装以接口桩隔离。"""

        def __init__(self, session=None, **kwargs):
            """保存传给宿主的真实会话和请求参数。"""
            self.session = session
            self.kwargs = kwargs

        def post_res(self, **kwargs):
            """保留宿主记录异常原文的风险边界。"""
            try:
                request = self.session.request if self.session is not None else requests.request
                return request("POST", headers=self.kwargs.get("headers"), timeout=self.kwargs.get("timeout"), **kwargs)
            except RequestException as error:
                messages.append(str(error))
                return None

    monkeypatch.setattr(module, "RequestUtils", RequestBoundary)


@pytest.mark.parametrize("error_type", [ReadTimeout, ConnectionError, SSLError, RequestException])
def test_bark_sanitizes_before_host_http_logger_without_retry(monkeypatch, error_type):
    """请求对象和路径中的设备Key/正文不能到达宿主 debug 日志。"""
    plugin, module = _plugin(monkeypatch, bark_webhook_url="https://bark.example/private-device-sentinel")
    messages = _capture_logs(monkeypatch, module)
    _install_http_boundary(monkeypatch, module, messages)
    calls = []

    def send(_adapter, request, **kwargs):
        """在真实 HTTPAdapter.send 边界注入含凭据的异常。"""
        calls.append((request, kwargs))
        raise error_type("private-exception-sentinel " + request.url, request=request)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    assert not plugin._send_bark_notification("private-title-sentinel", "private-body-sentinel")
    assert len(calls) == 1
    assert "private-" not in "\n".join(messages)
    assert "送达未确认" in "\n".join(messages)


@pytest.mark.parametrize("status", [200, 307, 308, 403, 502])
def test_bark_http_boundary_verifies_tls_prevents_redirect_replay_and_closes(monkeypatch, status):
    """在真实请求适配器下检查 HTTPS、禁止重定向和单次投递边界。"""
    plugin, module = _plugin(monkeypatch, bark_webhook_url="https://bark.example/private-device-sentinel")
    messages = _capture_logs(monkeypatch, module)
    _install_http_boundary(monkeypatch, module, messages)
    calls, closed = [], []

    def send(_adapter, request, **kwargs):
        """即使目标返回重定向，第二次网络发送也不允许。"""
        calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = status
        response._content = b'{"code":200}'
        response.request = request
        response.headers["Location"] = "https://other.example/private-redirect-sentinel"
        response.close = lambda: closed.append(True)
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    assert plugin._send_bark_notification("private-title-sentinel", "private-body-sentinel") is (status == 200)
    assert len(calls) == 1
    # requests 在禁止跟随后仍准备 next 并释放一次重定向响应，插件最终释放是幂等的。
    assert closed == [True] * (2 if status in (307, 308) else 1)
    assert calls[0][1].get("verify") is True
    assert "private-" not in "\n".join(messages)
