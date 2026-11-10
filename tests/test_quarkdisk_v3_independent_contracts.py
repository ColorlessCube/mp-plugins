"""Independent read-only probes: real fixed V3 schemas, no cloud/network/DB."""
import ast
import importlib.util
import socket
import sys
import types
from pathlib import Path
from unittest.mock import Mock
import pytest

from test_quarkdisk_v3_host_sources import host_source_root
PLUGIN = Path(__file__).resolve().parents[1] / "plugins.v3/quarkdisk"


def load(name, path, monkeypatch, package=False):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)] if package else None)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def extract_class(path, name, methods, namespace):
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in methods]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace[name]


class Cache:
    def __init__(self, **kwargs): self.data = {}
    def get(self, key): return self.data.get(key)
    def set(self, key, value): self.data[key] = value
    def delete(self, key): self.data.pop(key, None)
    def clear(self): self.data.clear()


@pytest.fixture
def env(monkeypatch, host_source_root):
    HOST = host_source_root
    ROOT = host_source_root.parent
    def deny(*a, **k): raise AssertionError('All real network forbidden')
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__path__ = []
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)
        return m
    app = mod('app')
    schemas = mod('app.schemas')
    app.schemas = schemas
    host_types = load('app.schemas.types', HOST/'app/schemas/types.py', monkeypatch)
    host_file = load('app.schemas.file', HOST/'app/schemas/file.py', monkeypatch)
    exceptions = load('app.schemas.exception', HOST/'app/schemas/exception.py', monkeypatch)
    schemas.__dict__.update({n:getattr(host_file,n) for n in ('FileItem','FileItemQueryResult','StorageUsage')})
    schemas.StorageOperSelectionEventData = object
    logger = Mock()
    stop = Mock()
    stop.consume_transfer_stop.return_value = False
    stop.is_stopped.return_value = False
    mod('app.core')
    mod('app.core.cache', TTLCache=Cache)
    mod('app.core.config', settings=types.SimpleNamespace(TEMP_PATH=ROOT))
    mod('app.core.event', eventmanager=types.SimpleNamespace(register=lambda event: lambda f:f), Event=object)
    mod('app.log', logger=logger)
    mod('app.plugins', _PluginBase=type('_PluginBase', (), {}))
    mod('app.helper')
    helper = Mock()
    mod('app.helper.storage', StorageHelper=helper)
    mod('app.utils')
    mod('app.utils.http', RequestUtils=Mock(side_effect=AssertionError('HTTP must be mocked')))
    mod('app.runtime')
    mod('app.runtime.stop', runtime_stop_state=stop)
    mod('app.runtime.progress', ProgressHelper=Mock())
    api_module = load('_independent_quark.quark_api', PLUGIN/'quark_api.py', monkeypatch)
    plugin_module = load('_independent_quark', PLUGIN/'__init__.py', monkeypatch, package=True)
    api = object.__new__(api_module.QuarkApi)
    api._disk_name = '夸克网盘'
    api._id_cache = api_module.IdPathCache()
    api._list_rate_limiter = Mock()
    api._path_rate_limiter = Mock()
    api._get_item_fail_records = {}
    api._request = Mock()
    plugin = plugin_module.QuarkDisk()
    plugin._disk_name = '夸克网盘'
    plugin._enabled = True
    plugin._cookie = 'offline-only'
    plugin._quark_api = api
    return types.SimpleNamespace(api=api, plugin=plugin, types=host_types, file=host_file, error=exceptions.StorageQueryError, logger=logger, helper=helper, stop=stop, host=HOST)


def raw(name='x.mkv', fid='1', directory=False, **kw):
    return dict(fid=fid, file_name=name, file_type=0 if directory else 1, size=10, updated_at=123, **kw) if not kw else {**raw(name,fid,directory), **kw}


def page(items, total=None):
    return {'status':200,'code':0,'data':{'list':items},'metadata':{'_total':len(items) if total is None else total}}


@pytest.mark.parametrize('response', [None, {}, page([],1), page([],True), page([], -1), page([], '0'), {'status':True,'code':0,'data':{'list':[]},'metadata':{'_total':0}}])
def test_unknown_is_not_missing(env, response):
    env.api._request.return_value=response
    with pytest.raises(env.error): env.plugin.get_file_item_strict('夸克网盘',Path('/missing'))


def test_real_chain_receipt_consumption(env):
    chain_cls=extract_class(env.host/'app/chain/storage.py','StorageChain',{'get_file_item_strict'}, {'FileItemQueryResult':env.file.FileItemQueryResult,'_SchemaFileItem':env.file.FileItem,'StorageQueryError':env.error})
    chain=chain_cls()
    chain.run_module_strict=lambda method,**kw: env.plugin.get_module()[method](**kw)
    env.api._request.return_value=page([])
    assert chain.get_file_item_strict('夸克网盘',Path('/absent')) is None
    env.api._request.return_value=page([raw()])
    result=chain.get_file_item_strict('夸克网盘',Path('/x.mkv'))
    assert type(result) is env.file.FileItem and result.fileid=='1'
    env.api._request.return_value=None
    with pytest.raises(env.error): chain.get_file_item_strict('夸克网盘',Path('/absent'))


def test_real_transhandler_consumes_provider_strict_failure(env):
    # Execute the unchanged official consumer branch; unrelated event/TransferInfo are output boundaries.
    cls=extract_class(env.host/'app/modules/filemanager/transhandler.py','TransHandler',{'__resolve_overwrite'}, {'StorageQueryError':env.error,'logger':env.logger,'TransferInfo':lambda **kw:types.SimpleNamespace(**kw)})
    handler=cls()
    handler._TransHandler__is_extra_file=lambda *a:False
    env.api._request.return_value=None
    decision=handler._TransHandler__resolve_overwrite(fileitem=env.file.FileItem(storage='local',path='/source',size=10),meta=None,mediainfo=None,target_oper=env.api,target_storage='夸克网盘',target_file=Path('/x.mkv'),transfer_type='move',overwrite_mode='always',need_notify=False)
    assert decision[0:2]==(False,False) and decision[2].success is False


def test_legacy_scalar_snapshot_preserves_unscanned_subtree(env):
    previous={'/deep/old.mkv':10}
    env.api._request.return_value=page([raw('deep','d',True)])
    snapshot=env.plugin.snapshot_storage('夸克网盘',Path('/'),1234,1,previous)
    assert '/deep/old.mkv' in snapshot, f'legacy baseline discarded without reading /deep: {snapshot}'
    assert snapshot['/deep/old.mkv'] == {'size': 10}
    assert previous == {'/deep/old.mkv': 10}
    cls = extract_class(env.host/'app/monitor/snapshot.py', 'SnapshotStore', {'save'},
                        {'time': __import__('time'), 'json': __import__('json'), 'logger': env.logger})
    store = cls()
    store.VERSION = 2
    store._cache = Mock()
    store._drop_legacy = Mock()
    assert store.save('夸克网盘', snapshot, file_count=1)


def test_dictionary_snapshot_preserves_unscanned_subtree(env):
    previous={'/deep/old.mkv':{'size':10,'modify_time':123}}
    env.api._request.return_value=page([raw('deep','d',True)])
    snapshot=env.plugin.snapshot_storage('夸克网盘',Path('/'),1234,1,previous)
    assert snapshot==previous


def test_snapshot_read_fault_discards_partial_result(env):
    env.api._request.side_effect=[page([raw('new.mkv','1'),raw('deep','d',True)]),TimeoutError('not logged')]
    previous={'/old.mkv':{'size':9,'modify_time':1}}
    assert env.plugin.snapshot_storage('夸克网盘',Path('/'),1234,5,previous) is None
    assert previous=={'/old.mkv':{'size':9,'modify_time':1}}


@pytest.mark.parametrize('bad_timestamp',[False,{},[]])
def test_falsey_malformed_timestamp_is_unknown_not_missing(env,bad_timestamp):
    env.api._request.return_value=page([raw(updated_at=bad_timestamp)])
    with pytest.raises(env.error): env.api.get_item_strict(Path('/missing'))


def test_usage_failure_does_not_log_raw_response_field(env):
    marker='SIGNED_URL_BODY_SENTINEL'
    env.api._request.return_value={'data':{'total_capacity':marker,'use_capacity':1}}
    assert env.plugin.storage_manage('夸克网盘','usage')['success'] is False
    logs=' '.join(str(call) for call in env.logger.mock_calls)
    assert marker not in logs, 'raw response field escaped through int() exception logging'


def test_storage_manage_real_enum_and_identity(env):
    for action in (env.types.StorageAction.SAVE_CONFIG,env.types.StorageAction.RESET_CONFIG):
        assert env.plugin.storage_manage('夸克网盘',action,config={'cookie':'ignored'})['success'] is False
    assert env.plugin._cookie=='offline-only'
    env.api._request.assert_not_called()
    assert env.plugin.storage_manage('夸克网盘',env.types.StorageAction.SUPPORT_TRANSTYPE)=={'success':True,'data':{'transtype':{'move':'移动'}}}
    assert env.plugin.plugin_config_prefix=='quarkdisk_'
    assert type(env.plugin).__name__=='QuarkDisk'
    event=types.SimpleNamespace(event_data=types.SimpleNamespace(storage='夸克网盘',storage_oper=None))
    env.plugin.storage_oper_selection(event)
    assert event.event_data.storage_oper is env.api


def test_real_module_contract_signature(env, monkeypatch):
    contracts=load('_official_module_contracts',env.host/'app/runtime/extensions/module/contracts.py',monkeypatch)
    for method in ('get_file_item_strict','snapshot_storage','storage_manage'):
        assert contracts.diagnose_module_callable(method,env.plugin.get_module()[method])==()
    contract=contracts.get_module_method_contract('snapshot_storage')
    assert contract.required_parameters==('storage','path','last_snapshot_time','max_depth','previous_snapshot')
    manage=contracts.get_module_method_contract('storage_manage')
    assert manage.input_contract=='StorageManageRequest'
    assert manage.result_contract=='StorageProviderResult'


@pytest.mark.parametrize('responses',[
    [page([raw('a','1')],2),page([],2)],
    [page([raw('a','1')],2),page([raw('a','1')],2)],
    [page([raw('a','1')],2),page([raw('b','2')],3)],
    [page([raw(file_type=True)])],
    [page([raw(size=False)])],
    [page([raw(updated_at=True)])],
])
def test_incomplete_or_invalid_page_never_confirms_absence(env,responses):
    env.api._request.side_effect=responses
    with pytest.raises(env.error): env.api.get_item_strict(Path('/missing'))


def test_ancestor_second_page_is_exhausted(env):
    env.api._id_cache.add_cache('stale','/media')
    env.api._request.side_effect=[page([raw(f'other-{i}',str(i+1),True) for i in range(100)],101),page([raw('media','101',True)],101),page([raw()])]
    item=env.api.get_item_strict(Path('/media/x.mkv'))
    assert item.path=='/media/x.mkv'
    assert [call.kwargs['params']['_page'] for call in env.api._request.call_args_list]==[1,2,1]


def test_snapshot_missing_mtime_is_persistable_by_real_consumer(env):
    env.api._request.return_value=page([raw(updated_at=None)])
    snapshot=env.plugin.snapshot_storage('夸克网盘',Path('/'),None,5,None)
    assert snapshot['/x.mkv'] == {'size': 10, 'fileid': '1', 'type': 'file', 'mtime_unknown': True}
    assert 'modify_time' not in snapshot['/x.mkv']
    ns={'time':__import__('time'),'json':__import__('json'),'logger':env.logger}
    cls=extract_class(env.host/'app/monitor/snapshot.py','SnapshotStore',{'save'},ns)
    store=cls()
    store.VERSION=2
    store._cache=Mock()
    store._drop_legacy=Mock()
    assert store.save('夸克网盘',snapshot,file_count=1), 'accepted missing timestamp cannot be saved by official SnapshotStore'
