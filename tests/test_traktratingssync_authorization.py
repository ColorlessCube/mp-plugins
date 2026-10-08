"""Trakt 配置页授权编排与配置迁移测试。"""
import types
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient
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
    assert plugin._data["trakt_token"] == {**token, "access_token": "legacy-access"}
    assert plugin._trakt_access_token == "legacy-access"
    assert "trakt_access_token" not in plugin._config_updates[-1]
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
    assert "trakt_access_token" not in config
    assert plugin._trakt_access_token == ""
    assert config["douban_cookie"] == "cookie"
    assert config["weread_api_key"] == "key"
    assert config["netease_cookie"] == "music-cookie"
    assert config["cron"] == "0 10 * * *"


def test_authorization_button_uses_saved_config_and_keeps_secrets_private(monkeypatch):
    """操作按钮生成链接，移除旧开关和手动回跳配置，令牌保持私有。"""
    plugin, module = _build_plugin(monkeypatch)
    monkeypatch.setattr(plugin, "run", lambda: pytest.fail("授权不能触发同步"))
    redirect = "https://owned.example/api/v1/plugin/TraktRatingsSync/oauth/callback"
    plugin.init_plugin({"trakt_client_id": "new-client", "trakt_redirect_uri": redirect, "douban_cookie": "cookie",
                        "trakt_client_secret": "removed", "trakt_authorize": True, "trakt_authorization_response": "removed"})
    assert not plugin._data.get("trakt_pkce_pending")
    assert plugin._api_trakt_start().success
    pending = dict(plugin._data["trakt_pkce_pending"])
    assert plugin._trakt_authorization_url.startswith("https://auth.trakt.tv/")
    assert not {"trakt_authorize", "trakt_authorization_response", "trakt_client_secret", "trakt_auth_mode", "trakt_authorization_url"} & set(plugin._config_updates[-1])
    plugin.init_plugin(plugin._config_updates[-1])
    assert plugin._data["trakt_pkce_pending"] == pending
    calls = []
    def post(**request):
        """记录一次授权码交换。"""
        calls.append(request)
        return _Response(200, {"access_token": "access", "refresh_token": "refresh", "expires_in": 604800})
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(post_res=post))
    callback = redirect + "?" + urlencode({"code": "private-code", "state": pending["state"]})
    assert plugin._create_trakt_helper().complete_pkce_authorization(callback)
    assert plugin._data["trakt_token"]["access_token"] == "access"
    assert "private-code" not in str(plugin._config_updates)
    assert pending["code_verifier"] not in str(plugin._config_updates)
    assert "access" not in plugin._config_updates[-1].values()
    plugin.init_plugin(plugin._config_updates[-1])
    assert plugin._trakt_access_token == "access"
    assert len(calls) == 1


def test_authorization_button_rejects_non_callback_address(monkeypatch):
    """已删除手动回跳，未登记真实回跳路径时不能生成链接。"""
    plugin, module = _build_plugin(monkeypatch)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: pytest.fail("配置失败不能访问Trakt"))
    plugin.init_plugin({"trakt_client_id": "client", "trakt_redirect_uri": "https://owned.example/", "douban_cookie": "cookie"})
    assert not plugin._api_trakt_start().success
    assert not plugin._data.get("trakt_pkce_pending")


def _build_callback_client(plugin):
    """按 MoviePilot 插件前缀挂载真实回跳路由，并保留 Cookie 鉴权依赖。"""
    api = next(item for item in plugin.get_api() if item["path"] == "/oauth/callback")
    assert api["allow_anonymous"] is True
    assert api["dependencies"]
    options = {key: value for key, value in api.items() if key not in ("path", "allow_anonymous")}
    app = FastAPI()
    app.add_api_route("/api/v1/plugin/TraktRatingsSync" + api["path"], **options)
    return TestClient(app)


def test_redirect_change_invalidates_pending_link_but_preserves_valid_token(monkeypatch):
    """修改回跳地址后旧链接不能继续使用，已保存的有效令牌仍保留。"""
    plugin, module = _build_plugin(monkeypatch)
    plugin._data["trakt_auth_client_id"] = "client"
    plugin._data["trakt_token"] = {"client_id": "client", "access_token": "access"}
    plugin._data["trakt_pkce_pending"] = {"client_id": "client", "redirect_uri": "https://owned.example/old"}
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: pytest.fail("修改地址不能发起令牌请求"))
    plugin.init_plugin({"trakt_client_id": "client", "trakt_access_token": "access",
                        "trakt_redirect_uri": "https://owned.example/new", "trakt_authorization_url": "old-link"})
    assert plugin._data["trakt_pkce_pending"] == {}
    assert plugin._trakt_authorization_url == ""
    assert plugin._data["trakt_token"]["access_token"] == "access"
    assert plugin._trakt_access_token == "access"


def _begin_callback_authorization(plugin):
    """为真实注册的回跳路径生成 PKCE 请求。"""
    redirect = "https://owned.example/api/v1/plugin/TraktRatingsSync/oauth/callback"
    plugin.init_plugin({"trakt_client_id": "client", "trakt_redirect_uri": redirect,
                        "douban_cookie": "preserved-cookie"})
    assert plugin._api_trakt_start().success
    pending = dict(plugin._data["trakt_pkce_pending"])
    return plugin._trakt_callback_path + "?" + urlencode({"code": "private-code", "state": pending["state"]}), pending


@pytest.mark.parametrize("cookie", [None, "test-member", "invalid-cookie"])
def test_callback_requires_logged_in_moviepilot_admin_before_exchange(monkeypatch, cookie):
    """回跳接口不能绕过 Cookie 管理员认证，拒绝请求时不消费 PKCE 请求。"""
    plugin, module = _build_plugin(monkeypatch)
    callback, pending = _begin_callback_authorization(plugin)
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: pytest.fail("未认证浏览器不能交换令牌"))
    client = _build_callback_client(plugin)
    if cookie:
        client.cookies.set("MoviePilot", cookie)
    response = client.get(callback)
    assert response.status_code == 403
    assert plugin._data["trakt_pkce_pending"] == pending
    assert not plugin._trakt_access_token


def test_callback_exchanges_and_saves_token_without_manual_paste_or_sync(monkeypatch):
    """管理员回跳自动交换令牌，不需要复制地址，也不能启动多平台同步。"""
    plugin, module = _build_plugin(monkeypatch)
    callback, pending = _begin_callback_authorization(plugin)
    monkeypatch.setattr(plugin, "run", lambda: pytest.fail("OAuth 回跳不能启动同步"))
    calls = []

    def post(**request):
        """记录交换请求并返回固定授权凭据。"""
        calls.append(request)
        return _Response(200, {"access_token": "access", "refresh_token": "refresh", "expires_in": 604800})

    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: types.SimpleNamespace(post_res=post))
    client = _build_callback_client(plugin)
    client.cookies.set("MoviePilot", "test-admin")
    # 测试服务的内部 HTTP 域名不同于登记的 HTTPS 地址，模拟生产反向代理。
    response = client.get(callback + "&iss=https%3A%2F%2Ftrakt.tv")
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert "授权成功" in response.json()["message"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert calls[0]["json"]["code_verifier"] == pending["code_verifier"]
    assert calls[0]["json"]["redirect_uri"] == plugin._trakt_redirect_uri
    assert "client_secret" not in calls[0]["json"]
    assert plugin._data["trakt_token"]["refresh_token"] == "refresh"
    assert plugin._trakt_access_token == "access"
    assert plugin._data["trakt_pkce_pending"] == {}
    assert plugin._config_updates[-1]["douban_cookie"] == "preserved-cookie"
    assert "private-code" not in str(plugin._config_updates)
    assert "access" not in response.text
    assert client.get(callback).json()["success"] is False
    assert len(calls) == 1
    assert plugin._trakt_access_token == "access"


@pytest.mark.parametrize("failure", ["state", "expired", "duplicate_state", "cancelled", "old_path", "changed_client"])
def test_callback_rejects_invalid_pkce_without_contacting_trakt(monkeypatch, failure):
    """已登录仍必须通过 PKCE 校验，不合法回跳不能交换令牌。"""
    plugin, module = _build_plugin(monkeypatch)
    callback, pending = _begin_callback_authorization(plugin)
    if failure == "state":
        callback = callback.replace(pending["state"], "wrong-state")
    elif failure == "expired":
        plugin._data["trakt_pkce_pending"]["expires_at"] = 0
    elif failure == "duplicate_state":
        callback += "&state=duplicate"
    elif failure == "cancelled":
        callback += "&error=access_denied"
    elif failure == "old_path":
        plugin._trakt_redirect_uri = "https://owned.example/api/v1/trakt-callback"
    else:
        plugin._trakt_client_id = "different-client"
    monkeypatch.setattr(module, "RequestUtils", lambda **_kwargs: pytest.fail("不合法回跳不能交换令牌"))
    client = _build_callback_client(plugin)
    client.cookies.set("MoviePilot", "test-admin")
    response = client.get(callback)
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert not plugin._trakt_access_token
    assert "private-code" not in response.text
    assert pending["state"] not in response.text
