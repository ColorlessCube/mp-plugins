"""Trakt 配置页授权编排与配置迁移测试。"""
import types
from urllib.parse import urlencode

import pytest

from test_traktratingssync_netease_cookie_mode import _load_plugin_module
from test_traktratingssync_trakt_helper import _load_trakt_helper_module, _Response, block_network


def _build_plugin(monkeypatch):
    """复用边界桩并注入真实 Trakt Helper，保持测试无真实网络。"""
    helper_module = _load_trakt_helper_module(monkeypatch)
    plugin_module = _load_plugin_module(monkeypatch)
    monkeypatch.setattr(plugin_module, "TraktHelper", helper_module.TraktHelper)
    return plugin_module.TraktRatingsSync(), helper_module


def test_existing_config_load_preserves_tokens_and_never_starts_authorization(monkeypatch):
    """升级加载旧配置时保留凭据，不能自动请求授权或同步。"""
    plugin, module = _build_plugin(monkeypatch)
    token = {"refresh_token": "legacy-refresh"}
    plugin._data["trakt_token"] = token
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: pytest.fail("加载配置不能请求 Trakt"))
    plugin.init_plugin({"enable": True, "trakt_client_id": "legacy-client", "trakt_access_token": "legacy-access"})
    assert plugin._data["trakt_token"] == token
    assert plugin._trakt_access_token == "legacy-access"
    assert plugin._config_updates == []
    assert plugin._data["trakt_auth_client_id"] == "legacy-client"


def test_changed_client_resets_only_trakt_auth_and_preserves_history_and_platforms(monkeypatch):
    """更换应用只重置授权；已有同步历史和其他平台配置不受影响。"""
    plugin, _module = _build_plugin(monkeypatch)
    plugin._data = {"trakt_auth_client_id": "old-client", "trakt_token": {"refresh_token": "old-refresh"},
                    "finished": {"movie": "history"}, "watching": {"show": "history"}, "netease_finished": {"album": "history"}}
    plugin.init_plugin({"trakt_client_id": "new-client", "trakt_access_token": "old-access", "douban_cookie": "cookie",
                        "weread_api_key": "key", "netease_cookie": "music-cookie", "cron": "0 10 * * *"})
    assert plugin._data["trakt_token"] == {}
    assert plugin._data["finished"] == {"movie": "history"}
    assert plugin._data["watching"] == {"show": "history"}
    assert plugin._data["netease_finished"] == {"album": "history"}
    config = plugin._config_updates[-1]
    assert config["trakt_access_token"] == ""
    assert config["douban_cookie"] == "cookie"
    assert config["weread_api_key"] == "key"
    assert config["netease_cookie"] == "music-cookie"
    assert config["cron"] == "0 10 * * *"


def test_config_authorization_actions_are_consumed_and_do_not_sync(monkeypatch):
    """保存链接生成和回跳操作后重载不会重复执行，也不能启动同步。"""
    plugin, module = _build_plugin(monkeypatch)
    monkeypatch.setattr(plugin, "run", lambda: pytest.fail("授权不能触发同步"))
    config = {"trakt_client_id": "new-client", "trakt_redirect_uri": "https://owned.example/callback",
              "trakt_authorize": True, "douban_cookie": "cookie"}
    plugin.init_plugin(config)
    generated = plugin._config_updates[-1]
    assert generated["trakt_authorize"] is False
    assert generated["trakt_auth_mode"] == "pkce"
    assert generated["trakt_authorization_url"].startswith("https://auth.trakt.tv/")
    pending = dict(plugin._data["trakt_pkce_pending"])
    plugin.init_plugin(generated)
    assert plugin._data["trakt_pkce_pending"] == pending
    calls = []

    def post(**request):
        """验证授权码在交换前已从配置清除，再返回授权结果。"""
        assert plugin._config_updates[-1]["trakt_authorization_response"] == ""
        calls.append(request)
        return _Response(200, {"access_token": "access", "refresh_token": "refresh", "expires_in": 604800})

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(post_res=post))
    callback_config = {**generated, "trakt_authorization_response": config["trakt_redirect_uri"] + "?" + urlencode({"code": "private-code", "state": pending["state"]})}
    plugin.init_plugin(callback_config)
    final = plugin._config_updates[-1]
    assert final["trakt_authorization_response"] == ""
    assert final["trakt_access_token"] == "access"
    assert final["trakt_authorization_url"] == ""
    assert "授权成功" in final["trakt_auth_message"]
    assert "private-code" not in str(plugin._config_updates)
    assert pending["code_verifier"] not in str(plugin._config_updates)
    plugin.init_plugin(final)
    assert len(calls) == 1


def test_config_reports_invalid_callback_without_losing_other_settings(monkeypatch):
    """不合法回跳输入清除后给出可见提示，不覆盖其他平台配置。"""
    plugin, module = _build_plugin(monkeypatch)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: pytest.fail("无待授权请求不能访问 Trakt"))
    plugin.init_plugin({"trakt_client_id": "client", "douban_cookie": "cookie", "trakt_authorization_response": "https://owned.example/?code=private"})
    assert plugin._config_updates[-1]["trakt_authorization_response"] == ""
    assert plugin._config_updates[-1]["douban_cookie"] == "cookie"
    assert "重新生成授权链接" in plugin._config_updates[-1]["trakt_auth_message"]
