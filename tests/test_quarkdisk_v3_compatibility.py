"""夸克 V3 兼容候选的离线契约回归，不加载真实配置或连接云盘。"""
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Optional
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

PLUGIN = Path(__file__).resolve().parents[1] / "plugins.v3" / "quarkdisk"


class _FileItem(BaseModel):
    """按官方 v3.2.5 文件边界提供测试所需字段。"""
    storage: str = "夸克网盘"
    path: str = "/"
    type: Optional[str] = None
    name: Optional[str] = None
    basename: Optional[str] = None
    extension: Optional[str] = None
    size: Optional[int] = None
    modify_time: Optional[float] = None
    fileid: Optional[str] = None
    parent_fileid: Optional[str] = None
    pickcode: Optional[str] = None


class _Usage(BaseModel):
    """宿主容量响应边界。"""
    total: float = 0
    available: float = 0


class _QueryResult(BaseModel):
    """官方严格查询回执的必要字段。"""
    item: Optional[_FileItem]


class _StorageQueryError(Exception):
    """宿主严格查询故障边界。"""


class _Cache:
    """隔离宿主持久缓存。"""
    def __init__(self, **_kwargs):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)

    def clear(self):
        self.data.clear()


@pytest.fixture
def candidate(monkeypatch, tmp_path):
    """仅替换宿主边界；所有 sys.modules 变更由 monkeypatch 严格恢复。"""
    assert (PLUGIN / "__init__.py").is_file(), "缺少 V3 专用 QuarkDisk 候选"
    schemas = types.ModuleType("app.schemas")
    schemas.FileItem = _FileItem
    schemas.StorageUsage = _Usage
    schemas.FileItemQueryResult = _QueryResult
    schemas.StorageOperSelectionEventData = object
    eventmanager = types.SimpleNamespace(register=lambda _event: lambda callback: callback)
    stop = Mock()
    stop.is_stopped.return_value = False
    stop.consume_transfer_stop.return_value = False
    progress = Mock(return_value=Mock())
    storage_helper = Mock()
    storage_helper.return_value.get_storagies.return_value = [types.SimpleNamespace(type="夸克网盘", name="夸克网盘")]
    modules = {
        "app": {"schemas": schemas},
        "app.schemas": schemas,
        "app.schemas.types": {"ChainEventType": types.SimpleNamespace(StorageOperSelection="selection")},
        "app.schemas.exception": {"StorageQueryError": _StorageQueryError},
        "app.core": {},
        "app.core.cache": {"TTLCache": _Cache},
        "app.core.config": {"settings": types.SimpleNamespace(TEMP_PATH=tmp_path)},
        "app.core.event": {"eventmanager": eventmanager, "Event": object},
        "app.log": {"logger": Mock()},
        "app.plugins": {"_PluginBase": type("_PluginBase", (), {})},
        "app.helper": {},
        "app.helper.storage": {"StorageHelper": storage_helper},
        "app.utils": {},
        "app.utils.http": {"RequestUtils": Mock(side_effect=AssertionError("必须隔离 HTTP 边界"))},
        "app.runtime": {},
        "app.runtime.stop": {"runtime_stop_state": stop},
        "app.runtime.progress": {"ProgressHelper": progress},
    }
    for name, attrs in modules.items():
        module = attrs if isinstance(attrs, types.ModuleType) else types.ModuleType(name)
        if isinstance(attrs, dict):
            module.__dict__.update(attrs)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    name = "_quarkdisk_v3_candidate"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    # 在导入前登记相对模块，以便 pytest 完整恢复模块状态。
    api_spec = importlib.util.spec_from_file_location(name + ".quark_api", PLUGIN / "quark_api.py")
    api_module = importlib.util.module_from_spec(api_spec)
    monkeypatch.setitem(sys.modules, name + ".quark_api", api_module)
    api_spec.loader.exec_module(api_module)
    spec.loader.exec_module(module)
    api = object.__new__(api_module.QuarkApi)
    api._disk_name = "夸克网盘"
    api._id_cache = api_module.IdPathCache()
    api._get_item_fail_records = {}
    api._list_rate_limiter = Mock()
    api._path_rate_limiter = Mock()
    api._headers = {"User-Agent": "offline-fixture"}
    api._cookie = "fixture-only"
    api._request = Mock(side_effect=AssertionError("未提供离线响应"))
    plugin = module.QuarkDisk()
    plugin._disk_name = "夸克网盘"
    plugin._enabled = True
    plugin._cookie = "fixture-only"
    plugin._quark_api = api
    return types.SimpleNamespace(module=module, api_module=api_module, api=api, plugin=plugin,
                                 storage_helper=storage_helper, stop=stop, progress=progress)


def _raw(name, fid, directory=False, **extra):
    return {"fid": str(fid), "file_name": name, "file_type": 0 if directory else 1,
            "size": 10, "updated_at": 123, **extra}


def _page(items, total=None):
    return {"status": 200, "code": 0, "data": {"list": items},
            "metadata": {"_total": len(items) if total is None else total}}


def test_strict_query_walks_every_ancestor_and_page(candidate):
    """目标父目录位于第二页时，不能把第一页未命中当不存在。"""
    api = candidate.api
    first_page = [_raw(f"other-{i}", i + 1, True) for i in range(100)]
    api._request.side_effect = [
        _page(first_page, 101), _page([_raw("media", 101, True)], 101),
        _page([_raw("movie.mkv", 200)]),
    ]
    result = api.get_item_strict(Path("/media/movie.mkv"))
    assert result.path == "/media/movie.mkv"
    assert result.fileid == "200"
    assert [(call.kwargs["params"]["pdir_fid"], call.kwargs["params"]["_page"])
            for call in api._request.call_args_list] == [("0", 1), ("0", 2), ("101", 1)]


@pytest.mark.parametrize("responses", [
    [TimeoutError("fixture-secret")],
    [{"status": 401, "code": 1, "data": {"list": []}, "metadata": {"_total": 0}}],
    [{}], [None], [{"status": 200, "code": 0, "data": {"list": []}}],
    [_page([], 1)], [_page([], True)], [_page([_raw("../escape", 2)])],
    [_page([{"file_name": "bad", "file_type": 1}])],
    [_page([_raw("x", 1)], 2), _page([_raw("x", 1)], 2)],
    [_page([_raw("x", 1)], 2), _page([], 2)],
    [_page([_raw("x", 1)], 2), _page([_raw("y", 2)], 3)],
])
def test_strict_query_never_converts_unknown_to_missing(candidate, responses):
    """认证、传输、畸形和不完整分页均为故障，不是确认不存在。"""
    candidate.api._request.side_effect = responses
    with pytest.raises(_StorageQueryError) as caught:
        candidate.api.get_item_strict(Path("/missing.mkv"))
    assert "fixture-secret" not in str(caught.value)


def test_strict_query_returns_none_only_after_exhaustion(candidate):
    """完整空目录才是明确未命中，旧缓存不能伪造严格命中。"""
    candidate.api._id_cache.add_cache("stale", "/missing.mkv")
    candidate.api._request.return_value = _page([])
    candidate.api._request.side_effect = None
    assert candidate.api.get_item_strict(Path("/missing.mkv")) is None
    assert candidate.api._request.call_count == 1


def test_strict_module_returns_query_receipt(candidate):
    """V3 链严格查询使用显式回执区分不支持与确认不存在。"""
    plugin, api = candidate.plugin, candidate.api
    api._request.side_effect = [_page([])]
    assert "get_file_item_strict" in plugin.get_module()
    receipt = plugin.get_file_item_strict("夸克网盘", Path("/gone"))
    assert isinstance(receipt, _QueryResult) and receipt.item is None
    assert plugin.get_file_item_strict("other", Path("/gone")) is None
    plugin._quark_api = None
    plugin._enabled = False
    with pytest.raises(_StorageQueryError):
        plugin.get_file_item_strict("夸克网盘", Path("/gone"))


def test_snapshot_uses_full_v3_abi_and_metadata(candidate):
    """五参快照保留未扫描基线、删除已确认消失项，不按不可信目录时间跳扫。"""
    api, plugin = candidate.api, candidate.plugin
    api._request.side_effect = [_page([_raw("keep", 1, True), _raw("new.mkv", 2)])]
    previous = {
        "/keep/old.mkv": {"size": 20, "modify_time": 10, "fileid": "old", "type": "file"},
        "/gone/deleted.mkv": {"size": 20, "modify_time": 10},
        "/outside.mkv": {"size": 1, "modify_time": 1},
    }
    before = json.loads(json.dumps(previous))
    snapshot = plugin.snapshot_storage("夸克网盘", Path("/"), last_snapshot_time=200,
                                       max_depth=1, previous_snapshot=previous)
    assert snapshot == {
        "/keep/old.mkv": previous["/keep/old.mkv"],
        "/new.mkv": {"size": 10, "modify_time": 123, "fileid": "2", "type": "file"},
    }
    assert previous == before
    assert api._request.call_count == 1


def test_snapshot_reconciles_unchanged_directory_and_preserves_unknown(candidate):
    """目录 mtime 不保证嵌套变化，旧目录也要扫描；读取故障返回 None 保留基线。"""
    api, plugin = candidate.api, candidate.plugin
    previous = {"/keep/gone.mkv": {"size": 10, "modify_time": 1}}
    api._request.side_effect = [_page([_raw("keep", 1, True)]), _page([])]
    assert plugin.snapshot_storage("夸克网盘", Path("/"), 200, 5, previous) == {}
    api._request.side_effect = [_page([_raw("keep", 1, True)]), TimeoutError("offline")]
    assert plugin.snapshot_storage("夸克网盘", Path("/"), 200, 5, previous) is None
    api._request.side_effect = [_page([])]
    assert plugin.snapshot_storage("夸克网盘", Path("/gone"), 200, 5, previous) == {}
    assert plugin.snapshot_storage("other", Path("/"), 200, 5, previous) is None


def test_storage_manage_usage_capabilities_and_config_ownership(candidate):
    """统一入口只读取容量、如实声明移动；认证仍由历史插件配置维护。"""
    plugin, api = candidate.plugin, candidate.api
    assert "storage_manage" in plugin.get_module()
    api._request.side_effect = [{"data": {"total_capacity": 100, "use_capacity": 40}}]
    assert plugin.storage_manage("夸克网盘", "usage") == {
        "success": True, "data": {"total": 100, "available": 60},
    }
    assert plugin.storage_manage("夸克网盘", "support_transtype") == {
        "success": True, "data": {"transtype": {"move": "移动"}},
    }
    assert plugin.support_transtype("夸克网盘") == api.support_transtype() == {"move": "移动"}
    assert api.is_support_transtype("copy") is False
    assert plugin.storage_manage("other", "usage") is None
    old_cookie = plugin._cookie
    for action in ("save_config", "reset_config", "generate_qrcode", "delete", "unknown"):
        assert plugin.storage_manage("夸克网盘", action, conf={"cookie": "ignored"})["success"] is False
    assert plugin._cookie == old_cookie
    assert api._request.call_count == 1
    candidate.storage_helper.assert_not_called()
    api._request.side_effect = [TimeoutError("offline")]
    assert plugin.storage_manage("夸克网盘", "usage")["success"] is False


def test_strict_root_query_requires_real_confirmation(candidate):
    """根路径不能绕过认证或传输检查，虚拟根条目不是已查询回执。"""
    candidate.api._request.side_effect = [TimeoutError("offline")]
    with pytest.raises(_StorageQueryError):
        candidate.api.get_item_strict(Path("/"))


@pytest.mark.parametrize("field,value", [("size", -1), ("size", True), ("size", "bad"),
                                        ("updated_at", "bad"), ("updated_at", float("nan"))])
def test_strict_query_rejects_malformed_file_metadata(candidate, field, value):
    """文件元数据损坏不能变成可用于覆盖决策的正常条目。"""
    candidate.api._request.side_effect = [_page([_raw("movie.mkv", 1, **{field: value})])]
    with pytest.raises(_StorageQueryError):
        candidate.api.get_item_strict(Path("/movie.mkv"))


def test_strict_query_normalizes_quark_milliseconds(candidate):
    """V3 快照与 FileItem 的时间单位为 Unix 秒，而非云盘毫秒值。"""
    candidate.api._request.side_effect = [_page([_raw("movie.mkv", 1, updated_at=1700000000123)])]
    assert candidate.api.get_item_strict(Path("/movie.mkv")).modify_time == 1700000000.123


def test_v3_index_and_gpl_provenance_are_packaged(candidate):
    """候选必须有代际索引、完整 GPL 文本和公开上游来源，而非冒充上游新版本。"""
    index = PLUGIN.parents[1] / "package.v3.json"
    assert index.is_file(), "缺少 V3 专用索引"
    metadata = json.loads(index.read_text())
    assert set(metadata) == {"QuarkDisk"}
    entry = metadata["QuarkDisk"]
    assert entry["version"] == candidate.plugin.plugin_version
    assert entry["author"] == "xijin285"
    assert entry["v3"] is True and entry.get("v2") is not True
    assert entry["system_version"] == ">=3.2.5,<4"
    assert entry["release"] is False
    assert "GNU GENERAL PUBLIC LICENSE" in (PLUGIN / "LICENSE").read_text()
    notice = (PLUGIN / "UPSTREAM.md").read_text()
    assert "18dbf2d752de2f7729d06a532125c6a2d9d24a5b" in notice
    assert "GPL" in notice and "MIT" in notice
    assert (PLUGIN / "README.md").is_file()


def test_common_request_uses_host_transport_without_response_leak(candidate, monkeypatch):
    """严格分页实际共用请求入口使用宿主传输并关闭响应，不额外走直接 requests。"""
    api = candidate.api
    api._base_url = "https://pan.quark.cn/1/clouddrive"
    api._drive_url = "https://drive.quark.cn/1/clouddrive"
    response = Mock(status_code=200, cookies={})
    response.json.return_value = _page([])
    client = Mock()
    client.request.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr(candidate.api_module, "RequestUtils", factory)
    monkeypatch.setattr(candidate.api_module.requests, "request", Mock(side_effect=AssertionError("不能绕过宿主 HTTP")))
    monkeypatch.setattr(candidate.api_module.time, "sleep", lambda _value: None)
    reply = candidate.api_module.QuarkApi._request(api, "/file/sort", use_drive=True, params={"_page": 1})
    assert reply == _page([])
    assert factory.call_args.kwargs["verify"] is True
    assert client.request.call_args.kwargs["raise_exception"] is True
    assert client.request.call_args.kwargs["allow_redirects"] is False
    response.close.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("fault, expected", [("timeout", "Timeout"), ("auth", "HTTP 401")])
def test_strict_transport_error_is_diagnostic_without_payload(candidate, monkeypatch, fault, expected):
    """实际请求入口的故障分类须穿过严格查询，不能泄漏响应正文或异常中的凭据。"""
    api = candidate.api
    api._drive_url = "https://drive.quark.cn/1/clouddrive"
    client = Mock()
    response = Mock(status_code=401, cookies={}, text="fixture-secret")
    if fault == "timeout":
        client.request.side_effect = candidate.api_module.requests.exceptions.Timeout("fixture-secret")
    else:
        client.request.return_value = response
    monkeypatch.setattr(candidate.api_module, "RequestUtils", Mock(return_value=client))
    monkeypatch.setattr(candidate.api_module.time, "sleep", lambda _value: None)
    monkeypatch.setattr(api, "_request", candidate.api_module.QuarkApi._request.__get__(api))
    with pytest.raises(_StorageQueryError, match=expected) as caught:
        api.get_item_strict(Path("/missing"))
    assert "fixture-secret" not in str(caught.value)
    assert client.request.call_count == 3
    client.close.assert_called_once()


def test_v3_candidate_is_separate_from_v2():
    """V3 候选必须独立发布，不覆盖已有 V2 插件目录。"""
    assert (PLUGIN / "__init__.py").is_file(), "缺少 V3 专用 QuarkDisk 候选"


def test_v3_keeps_original_plugin_identity(candidate):
    """V3 专用目录保留历史 class、配置前缀、存储名与作者。"""
    plugin = candidate.plugin
    assert type(plugin).__name__ == "QuarkDisk"
    assert plugin.plugin_config_prefix == "quarkdisk_"
    assert plugin.plugin_version == "2.0.0"
    assert plugin.plugin_author == "xijin285"
    assert plugin._disk_name == "夸克网盘"
    assert plugin.get_form()[1] == {"enabled": False, "cookie": ""}
