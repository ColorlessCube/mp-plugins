"""新完整浏览器会话只投影Cookie；全部数据合成，所有网络边界打桩。"""
import copy
import json
from contextlib import contextmanager

import pytest

from test_traktratingssync_ass_cookie import (
    NOW, SECRET, TOKEN, _bundle, _config, _cookie, _iso, _load_adapter, _responses, _StreamResponse,
)


def _session(site="douban", version=1, mixed=False):
    data = _bundle(site, version=version)
    data.pop("cookie_generated_at")
    data["format"] = "ass-browser-session"
    if site == "netease":
        data["cookies"] = [_cookie("MUSIC_U", "music", site), _cookie("__csrf", "domain", site), _cookie("__csrf", "host", site, True)]
    origin = data["source_origins"][0]
    data["pages"] = [{"origin": origin, "page_url": origin + "/", "referrer": None,
                      "visibility_state": "visible", "env": {"navigator": {}, "screen": {}, "intl": {}},
                      "storage": {"local": {"large": "X" * 118331}, "session": {}},
                      "headers": {}, "headers_status": "not_observed"}]
    if version == 2:
        data.update(capture_mode="mixed" if mixed else "complete", pages_observed_at=_iso(NOW - 100000) if mixed else data["observed_at"],
                    page_baseline_id="10000000-0000-4000-8000-000000000001" if mixed else data["snapshot_id"])
    return data


@pytest.mark.parametrize("version,mixed", [(1, False), (2, False), (2, True)])
def test_full_and_mixed_cookie_only_projection_never_returns_page_payload(monkeypatch, tmp_path, version, mixed):
    """完整/混合接受相同Cookie，不使用旧页面年龄证明登录，也不输出页面数据。"""
    module = _load_adapter(monkeypatch)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    data = _session(version=version, mixed=mixed)
    data["cookies"] += [_cookie("analytics", "stats"), _cookie("dbcl2", "subpath", host=True) | {"path": "/other", "domain": "www.douban.com"}]
    assert helper._project(json.dumps(data), NOW) == {"dbcl2": '"123:test"'}
    assert helper.observation_age_seconds == 0
    assert not any(isinstance(v, (dict, list)) for v in vars(helper).values())


@pytest.mark.parametrize("reverse", [True, False])
@pytest.mark.parametrize("equal", [True, False])
def test_browser_csrf_selection_remains_explicit_and_order_independent(monkeypatch, tmp_path, reverse, equal):
    """完整包里的双CSRF依旧必须显式选择，包含额外Cookie也不取first/last。"""
    module = _load_adapter(monkeypatch)
    data = _session("netease", 2, True)
    if equal:
        data["cookies"][-1]["value"] = "domain"
    data["cookies"] += [_cookie("unrelated", "other", "netease")]
    if reverse:
        data["cookies"].reverse()
    config = _config(tmp_path)
    with pytest.raises(module.AssCookieError, match="ambiguous"):
        module.AssCookieHelper(config, "netease")._project(json.dumps(data), NOW)
    for scope in ("host", "domain"):
        helper = module.AssCookieHelper(config | {"netease_ass_csrf_scope": scope}, "netease")
        assert helper._project(json.dumps(data), NOW) == {"MUSIC_U": "music", "__csrf": "domain" if equal else scope}


def _mutate(data, case):
    if case == "duplicate": data["cookies"].append(copy.deepcopy(data["cookies"][0]))
    elif case == "foreign_domain": data["cookies"][0]["domain"] = ".outside.invalid"
    elif case == "partitioned": data["cookies"][0]["partitionKey"] = "outside"
    elif case == "store": data["cookies"][0]["storeId"] = "1"
    elif case == "control_value": data["cookies"].append(_cookie("extra", "sentinel\n"))
    elif case == "cookie_utf8": data["cookies"].append(_cookie("extra", "汉" * 6000))
    elif case == "page_missing": data["pages"].clear()
    elif case == "page_query": data["pages"][0]["page_url"] += "?token=sentinel"
    elif case == "referrer": data["pages"][0]["referrer"] = "https://outside.invalid/"
    elif case == "headers": data["pages"][0]["headers"] = {"authorization": "sentinel"}
    elif case == "storage_key": data["pages"][0]["storage"]["local"] = {"汉" * 171: "X"}
    elif case == "storage_count": data["pages"][0]["storage"]["session"] = {str(i): "X" for i in range(501)}
    elif case == "environment": data["pages"][0]["env"]["navigator"] = {str(i): "X" for i in range(41)}
    elif case == "fake_page_time": data["pages_observed_at"] = _iso(NOW + 1)
    elif case == "fake_baseline": data["page_baseline_id"] = data["snapshot_id"]
    elif case == "clock": data["observed_at"] = _iso(NOW + 61)
    elif case == "version_bool": data["schema_version"] = True
    elif case == "unknown": data["unexpected"] = "sentinel"
    elif case == "oversize": data["pages"][0]["storage"]["local"]["large"] = "X" * (256 * 1024)
    elif case == "extra_source": data["source_origins"].append(data["source_origins"][0])
    else: raise AssertionError(case)


@pytest.mark.parametrize("case", ["duplicate", "foreign_domain", "partitioned", "store", "control_value", "cookie_utf8", "page_missing", "page_query", "referrer", "headers", "storage_key", "storage_count", "environment", "fake_page_time", "fake_baseline", "clock", "version_bool", "unknown", "oversize", "extra_source"])
def test_rejects_malformed_whole_packet_without_plaintext_errors(monkeypatch, tmp_path, case):
    """不因只投影Cookie而放过全包越界、歧义、伪新鲜度或权限数据。"""
    module = _load_adapter(monkeypatch)
    data = _session(version=2, mixed=True)
    _mutate(data, case)
    with pytest.raises(module.AssCookieError) as error:
        module.AssCookieHelper(_config(tmp_path), "douban")._project(json.dumps(data), NOW)
    assert "sentinel" not in str(error.value) and '123:test' not in str(error.value)
    assert error.value.__context__ is None or error.value.__suppress_context__


def test_invalidated_empty_expired_and_ambiguous_cookie_never_fall_back(monkeypatch, tmp_path):
    """显式失效、空/过期必需项、重复物理Cookie始终阻断。"""
    module = _load_adapter(monkeypatch)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    data = _session()
    invalid = data | {"state": "invalid", "reason": "manual_invalidation", "pages": [], "cookies": []}
    with pytest.raises(module.AssCookieError, match="invalidated"):
        helper._project(json.dumps(invalid), NOW)
    for cookies in ([], [_cookie() | {"session": False, "expirationDate": NOW - 1}], [_cookie(host=True) | {"domain": "www.douban.com"}]):
        with pytest.raises(module.AssCookieError, match="required_missing"):
            helper._project(json.dumps(data | {"cookies": cookies}), NOW)


def test_new_name_uuid_and_migration_grant_limit_are_explicit(monkeypatch, tmp_path):
    """新名称与UUID双重绑定；默认不接受扩大授权，显式迁移上限才通过。"""
    module = _load_adapter(monkeypatch)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    config = _config(tmp_path) | {"douban_ass_secret_name": "session.douban.main"}
    def reads(helper, grants):
        responses = _responses()
        responses[0]["active_grant_count"] = grants
        for index in (1, 2, 3): responses[index]["name"] = "session.douban.main"
        responses[2]["value"] = json.dumps(_session(version=2, mixed=True))
        paths = []
        def get(_session, _token, path, _deadline):
            paths.append(path)
            return responses.pop(0)
        monkeypatch.setattr(helper, "_get", get)
        return paths
    helper = module.AssCookieHelper(config, "douban")
    reads(helper, 4)
    with pytest.raises(module.AssCookieError, match="identity_mismatch"): helper.read_cookies()
    helper = module.AssCookieHelper(config | {"ass_max_grants": 4}, "douban")
    paths = reads(helper, 4)
    assert helper.read_cookies() == {"dbcl2": '"123:test"'}
    assert paths[1] == "/api/v1/secrets/session.douban.main/metadata"
    helper = module.AssCookieHelper(config | {"ass_max_grants": 4}, "douban")
    reads(helper, 5)
    with pytest.raises(module.AssCookieError, match="identity_mismatch"): helper.read_cookies()


@pytest.mark.parametrize("name", ["", "../douban", "douban/value", "https://evil.invalid", "douban?x=1"])
def test_rejects_unpinned_name_paths(monkeypatch, tmp_path, name):
    """名称不能逃逸既有精确ASS路径。"""
    module = _load_adapter(monkeypatch)
    with pytest.raises(module.AssCookieError, match="configuration_invalid"):
        module.AssCookieHelper(_config(tmp_path) | {"douban_ass_secret_name": name}, "douban")


def test_large_escaped_value_response_is_bounded_without_relaxing_metadata(monkeypatch, tmp_path):
    """仅value响应提供转义包容量；元数据仍20KiB，超过2MiB立即停止。"""
    module = _load_adapter(monkeypatch)
    data = _session()
    data["pages"][0]["storage"]["local"]["large"] = '"' * 50000
    content = json.dumps({"value": json.dumps(data)}).encode()
    assert len(content) > 20 * 1024
    class RequestStub:
        def __init__(self, **_kwargs):
            """替换请求边界，不使用真实网络。"""
        @contextmanager
        def response_manager(self, *_args, **_kwargs):
            """返回合成响应，确保边界只按路径放宽。"""
            yield _StreamResponse(content=content)
    monkeypatch.setattr(module, "RequestUtils", RequestStub)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    assert helper._get(None, TOKEN, "/api/v1/secrets/session.douban.main/value", module.time.monotonic() + 20)["value"] == json.dumps(data)
    with pytest.raises(module.AssCookieError, match="response_invalid"):
        helper._get(None, TOKEN, "/api/v1/secrets/access-status", module.time.monotonic() + 20)
    content = b"X" * (2 * 1024 * 1024 + 1)
    with pytest.raises(module.AssCookieError, match="response_invalid"):
        helper._get(None, TOKEN, "/api/v1/secrets/session.douban.main/value", module.time.monotonic() + 20)


def test_exact_session_utf8_budget_and_cookie_count_boundary(monkeypatch, tmp_path):
    """总包精确256KiB可用，多一字节或第501个Cookie拒绝。"""
    module = _load_adapter(monkeypatch)
    helper = module.AssCookieHelper(_config(tmp_path), "douban")
    data = _session()
    data["pages"][0]["storage"]["local"]["large"] = ""
    overhead = len(json.dumps(data).encode())
    data["pages"][0]["storage"]["local"]["large"] = "X" * (module.MAX_SESSION_BYTES - overhead)
    raw = json.dumps(data)
    assert len(raw.encode()) == module.MAX_SESSION_BYTES
    assert helper._project(raw, NOW) == {"dbcl2": '\"123:test\"'}
    with pytest.raises(module.AssCookieError): helper._project(raw + " ", NOW)
    data = _session()
    data["cookies"] += [_cookie("stats_" + str(i), "X") for i in range(499)]
    assert helper._project(json.dumps(data), NOW) == {"dbcl2": '\"123:test\"'}
    data["cookies"].append(_cookie("one_too_many", "X"))
    with pytest.raises(module.AssCookieError): helper._project(json.dumps(data), NOW)


@pytest.mark.parametrize("version", [1, 2])
def test_new_format_never_relaxes_legacy_limits_or_uses_manual_fallback(monkeypatch, tmp_path, version):
    """新包放宽不改变旧格式16KiB限制，也不接受新包伪旧格式。"""
    module = _load_adapter(monkeypatch)
    data = _session(version=version)
    data["format"] = "ass-cookie-bundle"
    with pytest.raises(module.AssCookieError, match="bundle_invalid"):
        module.AssCookieHelper(_config(tmp_path), "douban")._project(json.dumps(data), NOW)
