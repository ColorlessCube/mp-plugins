"""本地插件测试统一禁止真实出站，并恢复自动导入的插件模块。"""
import socket
import sys

import pytest


@pytest.fixture(autouse=True)
def isolate_plugin_boundaries(monkeypatch):
    """外部请求必须 mock；相对导入不能把测试桩留到后续用例。"""
    def deny_network(*_args, **_kwargs):
        raise AssertionError("插件测试禁止真实网络访问")

    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    before = {key: value for key, value in sys.modules.items() if key.startswith("plugins.")}
    yield
    for key in list(sys.modules):
        if key.startswith("plugins.") and key not in before:
            sys.modules.pop(key, None)
    sys.modules.update(before)
