"""豆瓣书影音同步插件的网易云开放平台 Helper 测试。"""
import importlib.util
from pathlib import Path


def _load_helper_module():
    """从插件仓库源码直接加载网易云开放平台 Helper。"""
    repo_root = Path(__file__).resolve().parents[3]
    import sys

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    helper_path = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "traktratingssync"
        / "netease_openapi_helper.py"
    )
    spec = importlib.util.spec_from_file_location("netease_openapi_helper", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalize_device_id_generates_stable_alnum_value():
    """未配置设备 ID 时，应生成稳定且符合网易云开放平台约束的值。"""
    helper_module = _load_helper_module()
    first = helper_module.NeteaseOpenApiHelper.normalize_device_id("", "app-id")
    second = helper_module.NeteaseOpenApiHelper.normalize_device_id("", "app-id")

    assert first == second
    assert first.isalnum()
    assert len(first) <= 64


def test_format_album_records_keeps_latest_play_order():
    """最近播放专辑记录应转换为插件统一结构并按播放时间倒序。"""
    helper_module = _load_helper_module()
    records = [
        {
            "record": {
                "id": "a1",
                "name": "Album A",
                "artists": [{"name": "Artist A"}],
                "coverImgUrl": "https://example.com/a.jpg",
            },
            "playTime": 100,
        },
        {
            "record": {
                "id": "b1",
                "name": "Album B",
                "artists": [{"name": "Artist B"}],
            },
            "playTime": 200,
        },
        {
            "record": {
                "id": "a1",
                "name": "Album A",
                "artists": [{"name": "Artist A"}],
            },
            "playTime": 300,
        },
    ]

    albums = helper_module.NeteaseOpenApiHelper._format_album_records(records=records, limit=10)

    assert [album["album"] for album in albums] == ["Album A", "Album B"]
    assert albums[0]["artist"] == "Artist A"
    assert albums[0]["total_play_count"] == 2
    assert albums[0]["play_time"] == 300


def test_invalid_private_key_returns_actionable_error_without_request():
    """私钥格式非法时应在本地失败，并返回不含敏感值的配置提示。"""
    helper_module = _load_helper_module()
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="test-app-id",
        private_key="abcde",
        app_secret="test-secret",
    )

    params = helper._build_params(biz_content={"clientId": "test-app-id"})

    assert params is None
    assert "PrivateKey" in helper.get_last_error()
    assert helper.get_token_state()["has_app_secret"] is True


def test_device_json_matches_official_cli_public_params():
    """设备公参应复用官方 CLI 可通过平台校验的取值。"""
    helper_module = _load_helper_module()
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="test-app-id",
        private_key="test",
        device_id="test-device-id",
    )

    import json

    device = json.loads(helper._device_json())

    assert device["deviceType"] == "openapi"
    assert device["os"] == "ncmcli"
    assert device["channel"] == "ncmcli"
    assert device["brand"] == "ncmcli"
    assert device["model"] == "MoviePilot_cli"
    assert device["deviceId"] == "testdeviceid"


def test_request_uses_official_cli_http_methods(monkeypatch):
    """开放平台 Helper 应按官方 CLI 协议区分 GET 和 POST。"""
    helper_module = _load_helper_module()
    calls = []

    class FakeRequestUtils:
        """记录请求方法的 RequestUtils 测试替身。"""

        def __init__(self, *args, **kwargs):
            pass

        def get_json(self, url, params=None):
            """记录 GET 请求。"""
            calls.append(("GET", url, params))
            return {"code": 200, "data": {}}

        def post_json(self, url, data=None, params=None):
            """记录 POST 请求。"""
            calls.append(("POST", url, params))
            return {"code": 200, "data": {}}

    helper = helper_module.NeteaseOpenApiHelper(app_id="app-id", private_key="test")
    monkeypatch.setattr(helper_module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(helper, "_build_params", lambda biz_content, access_token="": {"signed": True})
    monkeypatch.setattr(helper, "_random_delay", lambda endpoint: 0)

    helper._request("/post-endpoint", {"a": 1}, method="POST")
    helper._request("/get-endpoint", {"a": 1}, method="GET")

    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/post-endpoint")
    assert calls[1][0] == "GET"
    assert calls[1][1].endswith("/get-endpoint")


def test_manifest_response_without_code_is_success(monkeypatch):
    """官方 CLI manifest 响应不带 code 时不应被误判为异常。"""
    helper_module = _load_helper_module()

    class FakeRequestUtils:
        """返回 manifest 响应的 RequestUtils 测试替身。"""

        def __init__(self, *args, **kwargs):
            pass

        def post_json(self, url, data=None, params=None):
            """返回不带 code 的 manifest 结构。"""
            return {"manifests": {"root": {"version": "1.0.0"}}}

    helper = helper_module.NeteaseOpenApiHelper(app_id="app-id", private_key="test")
    monkeypatch.setattr(helper_module, "RequestUtils", FakeRequestUtils)
    monkeypatch.setattr(helper, "_build_params", lambda biz_content, access_token="": {"signed": True})
    monkeypatch.setattr(helper, "_random_delay", lambda endpoint: 0)

    response = helper._request(
        "/openapi/v1/ncm/cli/manifest",
        {"cliVersion": "0.1.5", "cachedVersion": "{}"},
        method="POST",
    )

    assert response == {"manifests": {"root": {"version": "1.0.0"}}}
    assert helper.get_last_error() == ""


def test_format_song_records_keeps_openapi_identifiers_and_add_time():
    """红心歌单曲目应保留加密 ID、原始 ID 和红心时间。"""
    helper_module = _load_helper_module()
    records = [
        {
            "originalId": 123,
            "id": "encrypted-song",
            "name": "Song A",
            "duration": 180000,
            "artists": [{"name": "Artist A"}],
            "album": {
                "originalId": 456,
                "id": "encrypted-album",
                "name": "Album A",
            },
            "liked": True,
            "visible": True,
            "coverImgUrl": "https://example.com/a.jpg",
            "extMap": {"addTime": 1781163000000},
        }
    ]

    songs = helper_module.NeteaseOpenApiHelper._format_song_records(records=records, limit=10)

    assert songs == [
        {
            "song": "Song A",
            "artists": ["Artist A"],
            "album": "Album A",
            "netease_song_id": "encrypted-song",
            "netease_original_song_id": 123,
            "netease_album_id": "encrypted-album",
            "netease_original_album_id": 456,
            "duration": 180000,
            "liked": True,
            "visible": True,
            "cover_img_url": "https://example.com/a.jpg",
            "add_time": 1781163000000,
        }
    ]


def test_refresh_access_token_uses_official_endpoint_and_persists_tokens(monkeypatch):
    """RefreshToken 续期应调用官方 v2 接口并持久化新 AT/RT。"""
    helper_module = _load_helper_module()
    captured = {}
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="app-id",
        app_secret="app-secret",
        private_key="test",
        access_token="old-access-token",
        refresh_token="old-refresh-token",
    )

    def fake_request(endpoint, biz_content, access_token="", method="GET"):
        """记录刷新请求并返回新的 token。"""
        captured.update({
            "endpoint": endpoint,
            "biz_content": biz_content,
            "access_token": access_token,
            "method": method,
        })
        return {
            "code": 200,
            "data": {
                "accessToken": "new-access-token",
                "refreshToken": "new-refresh-token",
                "expiresTime": 604800,
            },
        }

    monkeypatch.setattr(helper, "_request", fake_request)

    assert helper.refresh_access_token() is True
    state = helper.get_token_values()
    assert captured == {
        "endpoint": "/openapi/music/basic/user/oauth2/token/refresh/v2",
        "biz_content": {
            "clientId": "app-id",
            "clientSecret": "app-secret",
            "refreshToken": "old-refresh-token",
        },
        "access_token": "old-access-token",
        "method": "POST",
    }
    assert state["access_token"] == "new-access-token"
    assert state["refresh_token"] == "new-refresh-token"
    assert state["token_expires_at"] > 0


def test_refresh_access_token_requires_refresh_token_and_app_secret():
    """缺少 RT 或 AppSecret 时应本地失败，不发起无效请求。"""
    helper_module = _load_helper_module()
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="app-id",
        app_secret="",
        private_key="test",
        refresh_token="refresh-token",
    )

    assert helper.refresh_access_token() is False
    assert "AppSecret" in helper.get_last_error()

    helper = helper_module.NeteaseOpenApiHelper(
        app_id="app-id",
        app_secret="app-secret",
        private_key="test",
        refresh_token="",
    )

    assert helper.refresh_access_token() is False
    assert "Refresh Token" in helper.get_last_error()


def test_expiring_access_token_is_refreshed_before_recent_album_request(monkeypatch):
    """实名接口调用前如果 AT 临近过期，应先刷新再请求数据接口。"""
    helper_module = _load_helper_module()
    calls = []
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="app-id",
        app_secret="app-secret",
        private_key="test",
        access_token="old-access-token",
        refresh_token="refresh-token",
        token_expires_at=1,
    )

    def fake_refresh():
        """模拟刷新成功并写入新 token。"""
        calls.append("refresh")
        helper._access_token = "new-access-token"
        helper._token_expires_at = 9999999999
        return True

    def fake_request(endpoint, biz_content, access_token="", method="GET"):
        """验证数据接口使用刷新后的 token。"""
        calls.append((endpoint, access_token, method))
        return {
            "code": 200,
            "data": {
                "records": [
                    {
                        "record": {
                            "id": "album-id",
                            "name": "Album A",
                            "artists": [{"name": "Artist A"}],
                        },
                        "playTime": 100,
                    }
                ]
            },
        }

    monkeypatch.setattr(helper, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(helper, "_request", fake_request)

    albums = helper.get_recent_albums(limit=10)

    assert calls == [
        "refresh",
        ("/openapi/music/basic/album/play/record/list", "new-access-token", "GET"),
    ]
    assert albums[0]["album"] == "Album A"


def test_unexpired_access_token_continues_when_pre_refresh_fails(monkeypatch):
    """预刷新失败但 AT 仍有效时，本次同步应继续使用现有 token。"""
    import time

    helper_module = _load_helper_module()
    calls = []
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="app-id",
        app_secret="app-secret",
        private_key="test",
        access_token="current-access-token",
        refresh_token="refresh-token",
        token_expires_at=int(time.time()) + 100,
    )

    def fake_refresh():
        """模拟开放平台刷新接口短暂失败。"""
        calls.append("refresh")
        return False

    def fake_request(endpoint, biz_content, access_token="", method="GET"):
        """验证未过期 AT 仍可继续请求数据接口。"""
        calls.append((endpoint, access_token, method))
        return {"code": 200, "data": {"records": []}}

    monkeypatch.setattr(helper, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(helper, "_request", fake_request)

    assert helper.get_recent_albums(limit=10) == []
    assert calls == [
        "refresh",
        ("/openapi/music/basic/album/play/record/list", "current-access-token", "GET"),
    ]


def test_auth_response_1406_refreshes_and_retries_once(monkeypatch):
    """实名接口返回 1406 时应按官方建议刷新 token 后重试一次。"""
    helper_module = _load_helper_module()
    calls = []
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="app-id",
        app_secret="app-secret",
        private_key="test",
        access_token="expired-access-token",
        refresh_token="refresh-token",
        token_expires_at=0,
    )

    def fake_refresh():
        """模拟 1406 后刷新成功。"""
        calls.append("refresh")
        helper._access_token = "new-access-token"
        return True

    def fake_request(endpoint, biz_content, access_token="", method="GET"):
        """第一次返回 1406，第二次返回正常数据。"""
        calls.append((endpoint, access_token, method))
        if len([item for item in calls if isinstance(item, tuple)]) == 1:
            return {"code": 1406, "message": "accessToken过期，请重新授权登录"}
        return {
            "code": 200,
            "data": {
                "records": [
                    {
                        "record": {
                            "id": "album-id",
                            "name": "Album A",
                            "artists": [{"name": "Artist A"}],
                        },
                        "playTime": 100,
                    }
                ]
            },
        }

    monkeypatch.setattr(helper, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(helper, "_request", fake_request)

    albums = helper.get_recent_albums(limit=10)

    assert calls == [
        ("/openapi/music/basic/album/play/record/list", "expired-access-token", "GET"),
        "refresh",
        ("/openapi/music/basic/album/play/record/list", "new-access-token", "GET"),
    ]
    assert albums[0]["album"] == "Album A"


def test_expired_token_refresh_failure_triggers_auth_required_callback(monkeypatch):
    """AT 已过期且 RT 无法续期时，应触发重新认证回调。"""
    helper_module = _load_helper_module()
    callbacks = []
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="app-id",
        app_secret="app-secret",
        private_key="test",
        access_token="expired-access-token",
        refresh_token="refresh-token",
        token_expires_at=1,
        auth_required_fn=lambda: callbacks.append("auth-required"),
    )

    monkeypatch.setattr(helper, "refresh_access_token", lambda: False)

    assert helper.get_recent_albums(limit=10) == []
    assert callbacks == ["auth-required"]
    assert "Token 已过期" in helper.get_last_error()


def test_missing_access_token_triggers_auth_required_callback(monkeypatch):
    """缺少 AT 且无法用 RT 恢复时，应触发重新认证回调。"""
    helper_module = _load_helper_module()
    callbacks = []
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="app-id",
        app_secret="app-secret",
        private_key="test",
        access_token="",
        refresh_token="",
        auth_required_fn=lambda: callbacks.append("auth-required"),
    )

    monkeypatch.setattr(helper, "refresh_access_token", lambda: False)

    assert helper.get_recent_albums(limit=10) == []
    assert callbacks == ["auth-required"]
    assert "未登录" in helper.get_last_error()


def test_auth_response_1406_refresh_failure_triggers_auth_required_callback(monkeypatch):
    """实名接口返回 1406 且刷新失败时，应触发重新认证回调。"""
    helper_module = _load_helper_module()
    callbacks = []
    helper = helper_module.NeteaseOpenApiHelper(
        app_id="app-id",
        app_secret="app-secret",
        private_key="test",
        access_token="expired-access-token",
        refresh_token="refresh-token",
        token_expires_at=0,
        auth_required_fn=lambda: callbacks.append("auth-required"),
    )

    monkeypatch.setattr(helper, "refresh_access_token", lambda: False)
    monkeypatch.setattr(
        helper,
        "_request",
        lambda endpoint, biz_content, access_token="", method="GET": {
            "code": 1406,
            "message": "accessToken过期，请重新授权登录",
        },
    )

    assert helper.get_recent_albums(limit=10) == []
    assert callbacks == ["auth-required"]
    assert "Token 已过期" in helper.get_last_error()
