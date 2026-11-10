"""Independent offline probes. Only scratch files and mocked wire boundary."""
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'tests'))
from test_quarkdisk_v3_compatibility import candidate, _FileItem
from test_quarkdisk_v3_download import _download
from test_quarkdisk_v3_host_sources import host_source_root

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    def deny(*args, **kwargs):
        raise AssertionError('no real network allowed')
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(socket.socket, 'connect', deny)


def test_staging_swap_after_identity_check(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch)
    real_link = os.link
    real_unlink = os.unlink
    protected = tmp_path / 'protected'
    protected.write_bytes(b'foreign-existing-data')
    def raced_link(src, dst, *args, **kwargs):
        real_unlink(src, dir_fd=kwargs['src_dir_fd'])
        os.symlink(str(protected), src, dir_fd=kwargs['src_dir_fd'])
        return real_link(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, 'link', raced_link)
    result = candidate.api.download(item, tmp_path)
    actual = {'returned_success': result is not None,
              'published_symlink': (tmp_path / item.name).is_symlink(),
              'read_result': result.read_bytes().decode() if result else None}
    print('STAGING_SWAP', actual)
    assert result is None, 'untrusted replacement was published as a successful download'


def test_cleanup_swap_does_not_delete_foreign_object(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch, chunks=(b'abc',))
    unrelated = tmp_path / 'unrelated'
    unrelated.write_bytes(b'foreign-existing-data')
    real_unlink = os.unlink
    def raced_unlink(path, *args, **kwargs):
        if str(path).startswith('.quark-download-'):
            real_unlink(path, *args, **kwargs)
            os.rename(unrelated, path, dst_dir_fd=kwargs['dir_fd'])
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, 'unlink', raced_unlink)
    result = candidate.api.download(item, tmp_path)
    exists_somewhere = any(p.is_file() and p.read_bytes() == b'foreign-existing-data' for p in tmp_path.iterdir())
    print('CLEANUP_SWAP', {'returned': str(result), 'foreign_data_survived': exists_somewhere})
    assert exists_somewhere, 'cleanup removed foreign replacement after identity check'


def test_directory_swap_after_final_identity_check(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch)
    directory = tmp_path / 'chosen'
    directory.mkdir()
    moved = tmp_path / 'moved'
    real_link = os.link
    def raced_link(src, dst, *args, **kwargs):
        directory.rename(moved)
        directory.mkdir()
        (directory / item.name).write_bytes(b'foreign-existing-data')
        return real_link(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, 'link', raced_link)
    result = candidate.api.download(item, directory)
    print('DIRECTORY_SWAP', {'returned_success': result is not None,
          'returned_bytes': result.read_bytes().decode() if result else None,
          'download_bytes_in_moved_dir': (moved / item.name).read_bytes().decode()})
    assert result is None, 'returned path no longer names the verified downloaded object'


@pytest.fixture
def host_http(candidate, monkeypatch, host_source_root):
    # Full, unchanged exact host module; async-only httpx2 is unavailable locally.
    # Alias httpx solely to permit loading unused async declarations.
    import httpx
    try:
        import httpx2
    except ImportError:
        monkeypatch.setitem(sys.modules, 'httpx2', httpx)
    for name, attrs in {
        'app.runtime.correlation': {'with_correlation_header': lambda h: dict(h or {})},
        'app.runtime.log': {'logger': sys.modules['app.log'].logger},
        'app.runtime.tasks': {'get_task_registry': Mock()},
    }.items():
        m=types.ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)
    spec=importlib.util.spec_from_file_location('_review_exact_host_http', host_source_root / 'host_http.py')
    module=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(candidate.api_module, 'RequestUtils', module.RequestUtils)
    return module


def test_post_unknown_receipt_not_replayed(candidate, host_http, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter
    sends=[]
    def send(adapter, request, **kwargs):
        sends.append({'method': request.method, 'path': request.path_url, 'tls_verify': kwargs['verify']})
        raise requests.exceptions.ReadTimeout('fixture-only-signed-url-secret')
    monkeypatch.setattr(HTTPAdapter, 'send', send)
    monkeypatch.setattr(candidate.api_module.time, 'sleep', lambda value: None)
    api=candidate.api
    api._base_url='https://pan.quark.cn/1/clouddrive'
    with pytest.raises(Exception):
        candidate.api_module.QuarkApi._request(api, '/file', method='POST', json_data={'file_name':'new', 'pdir_fid':'0'})
    print('POST_WIRE_SENDS', sends)
    assert len(sends) == 1, 'POST was replayed after uncertain server receipt'


@pytest.mark.parametrize('host,status', [('objects.example.test',200), ('download.quark.cn',200), ('objects.example.test',302), ('objects.example.test',503)])
def test_download_prepared_request_security(candidate, host_http, monkeypatch, tmp_path, host, status):
    import requests
    from io import BytesIO
    from requests.adapters import HTTPAdapter
    item=_FileItem(storage='夸克网盘', type='file', path='/movie.mkv', name='movie.mkv',fileid='1',size=6)
    candidate.api.get_item_strict=Mock(return_value=item)
    secret='fixture-only-url-signature'
    candidate.api._request=Mock(return_value={'data':[{'download_url':f'https://{host}/data?sign={secret}'}]})
    captures=[]
    def send(adapter, request, **kwargs):
        captures.append((request,kwargs))
        r=requests.Response()
        r.status_code=status
        r.headers.update({'Content-Length':'6','Location':'https://unrelated.example.test/target'})
        r.raw=BytesIO(b'abcdef')
        r.url=request.url
        r.request=request
        return r
    monkeypatch.setattr(HTTPAdapter, 'send', send)
    result=candidate.api.download(item,tmp_path)
    assert len(captures)==1
    req, opts=captures[0]
    assert opts['verify'] is True
    assert 'Authorization' not in req.headers
    if host.endswith('.quark.cn'):
        assert req.headers['Cookie']==candidate.api._cookie
    else:
        assert 'Cookie' not in req.headers
    assert req.headers['Accept-Encoding']=='identity'
    assert (result is not None)==(status==200)
    assert secret not in str(sys.modules['app.log'].logger.mock_calls)
    print('REAL_PREPARED_REQUEST_SECURITY', host, status, 'passed')


def test_signed_url_exception_log_redacted(candidate, host_http, monkeypatch, tmp_path):
    import requests
    from requests.adapters import HTTPAdapter
    item=_FileItem(storage='夸克网盘', type='file', path='/movie.mkv', name='movie.mkv',fileid='1',size=6)
    candidate.api.get_item_strict=Mock(return_value=item)
    secret='fixture-only-url-signature'
    candidate.api._request=Mock(return_value={'data':[{'download_url':f'https://objects.example.test/data?sign={secret}'}]})
    def send(adapter, request, **kwargs):
        raise requests.exceptions.ReadTimeout('could not GET '+request.url)
    monkeypatch.setattr(HTTPAdapter, 'send', send)
    assert candidate.api.download(item,tmp_path) is None
    assert secret not in str(sys.modules['app.log'].logger.mock_calls)
    assert list(tmp_path.iterdir())==[]
    print('SIGNED_URL_LOG_REDACTION', 'passed')


def test_actual_host_market_consumes_v3_index(candidate, host_http, monkeypatch, host_source_root):
    import json
    from contextlib import nullcontext
    root=host_source_root
    def register(name, attrs):
        m=types.ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules,name,m)
        return m
    def load(name, file):
        spec=importlib.util.spec_from_file_location(name,root/file)
        m=importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules,name,m)
        spec.loader.exec_module(m)
        return m
    settings={'VERSION_FLAG':'v3','REPO_GITHUB_HEADERS':lambda **kw: {}}
    version=['3.2.5']
    register('app.foundation',{})
    register('app.domain',{})
    load('app.foundation.url','app_foundation_url.py')
    load('app.foundation.singleton','app_foundation_singleton.py')
    load('app.foundation.version','app_foundation_version.py')
    domain=load('app.domain.plugin','app_domain_plugin.py')
    register('app.foundation.environment',{'is_free_threaded_runtime':lambda:False})
    register('app.runtime.settings',{'get_runtime_setting':lambda key:settings.get(key)})
    register('app.runtime.version',{'get_app_version':lambda:version[0]})
    def identity_decorator(*args,**kwargs):
        def decorate(fn):
            fn.cache_clear=lambda *a,**k: None
            fn.cache_region='scratch-only'
            return fn
        return decorate
    register('app.runtime.cache',{'AsyncCacheControls':object,'cached':identity_decorator,
            'fresh':lambda *a,**k:nullcontext(),'async_fresh':Mock(),'is_fresh':lambda:False})
    monkeypatch.setitem(sys.modules,'app.adapters.network.http',host_http)
    market=load('_review_exact_host_market','host_market_client.py')
    cls=market.PluginMarketTransport
    index=cls._PluginMarketTransport__parse_plugin_index_response((REPO/'package.v3.json').read_text())
    assert set(index)=={'QuarkDisk'}
    meta=index['QuarkDisk']
    assert meta['release'] is False
    assert cls.is_package_plugin_compatible(meta,'v3')
    assert cls._select_compatible_package_version(pid='QuarkDisk',package_version='v3',plugins=index)=='v3'
    settings['VERSION_FLAG']='v2'
    assert not cls.is_package_plugin_compatible(meta,'v3')
    settings['VERSION_FLAG']='v3'
    results={}
    for v,expected in [('3.2.4',False),('3.2.5',True),('3.9.9',True),('4.0.0',False)]:
        version[0]=v
        result,message=cls.check_plugin_install_compatibility(meta)
        results[v]=result
        assert result is expected
    version[0]='3.2.5'
    url,headers=cls._build_plugin_index_request('https://github.com/ColorlessCube/mp-plugins','v3')
    from urllib.parse import urlsplit
    assert urlsplit(url).path.endswith('/package.v3.json')
    plan,message=domain.build_plugin_release_install_plan(plugin_id='QuarkDisk',metadata=meta,
                    release_version=None,release_items=[],current_version='3.2.5')
    assert plan is not None and plan.release_tag is None
    assert domain.build_plugin_release_install_plan(plugin_id='QuarkDisk',metadata=meta,
        release_version='2.0.0',release_items=[],current_version='3.2.5')[0] is None
    print('ACTUAL_MARKET_CONSUMER',{'index_url_ok':True,'v2_rejected':True,
         'version_boundaries':results,'source_only_release_tag':plan.release_tag})


def test_target_appears_before_atomic_link_is_preserved(candidate,monkeypatch,tmp_path):
    item,response,factory=_download(candidate,monkeypatch)
    real_link=os.link
    def raced_link(src,dst,*args,**kwargs):
        (tmp_path/dst).write_bytes(b'foreign-existing-data')
        return real_link(src,dst,*args,**kwargs)
    monkeypatch.setattr(os,'link',raced_link)
    assert candidate.api.download(item,tmp_path) is None
    assert (tmp_path/item.name).read_bytes()==b'foreign-existing-data'
    assert not list(tmp_path.glob('.quark-download-*'))
    print('NO_OVERWRITE_POSITIVE_CONTROL','passed')


def test_progress_callback_failure_does_not_publish(candidate,monkeypatch,tmp_path):
    item,response,factory=_download(candidate,monkeypatch)
    candidate.progress.return_value.update.side_effect=RuntimeError('fixture-only-secret')
    assert candidate.api.download(item,tmp_path) is None
    assert list(tmp_path.iterdir())==[]
    response.close.assert_called_once()
    factory.return_value.close.assert_called_once()
    print('CALLBACK_FAILURE_CLEANUP','passed')
