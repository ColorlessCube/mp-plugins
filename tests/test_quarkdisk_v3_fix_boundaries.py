"""Positive controls for the real transport and anonymous Linux inode boundary."""
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_quarkdisk_v3_download import _download
from test_quarkdisk_v3_compatibility import candidate
from test_quarkdisk_v3_independent_contracts import env, page, raw, extract_class
from test_quarkdisk_v3_independent_probes import host_http, offline
from test_quarkdisk_v3_host_sources import host_source_root


@pytest.mark.parametrize('endpoint', ['/file', '/file/move', '/file/delete'])
def test_uncertain_mutations_are_one_wire_send(candidate, host_http, monkeypatch, endpoint):
    import requests
    from requests.adapters import HTTPAdapter
    sends = []
    def send(adapter, request, **kwargs):
        sends.append(request)
        raise requests.exceptions.ReadTimeout('fixture-only-secret')
    monkeypatch.setattr(HTTPAdapter, 'send', send)
    monkeypatch.setattr(candidate.api_module.time, 'sleep', lambda _: None)
    candidate.api._base_url = 'https://pan.quark.cn/1/clouddrive'
    with pytest.raises(Exception):
        candidate.api_module.QuarkApi._request(candidate.api, endpoint, method='POST', json_data={'fid': '1'})
    assert len(sends) == 1


def test_read_retry_has_a_finite_wire_budget(candidate, host_http, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter
    sends = []
    def send(adapter, request, **kwargs):
        sends.append(request)
        raise requests.exceptions.ReadTimeout('fixture-only-secret')
    monkeypatch.setattr(HTTPAdapter, 'send', send)
    monkeypatch.setattr(candidate.api_module.time, 'sleep', lambda _: None)
    candidate.api._base_url = 'https://pan.quark.cn/1/clouddrive'
    with pytest.raises(Exception):
        candidate.api_module.QuarkApi._request(candidate.api, '/file/sort')
    assert len(sends) == candidate.api._max_retries == 3


def test_publication_uses_the_open_anonymous_inode(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch)
    real_link = os.link
    observed = []
    def link(src, dst, **kwargs):
        fd = int(src.rsplit('/', 1)[1])
        stat = os.fstat(fd)
        assert src == f'/proc/self/fd/{fd}'
        assert stat.st_nlink == 0
        assert kwargs['follow_symlinks'] is True
        assert not list(tmp_path.glob('.quark-download-*'))
        observed.append((stat.st_dev, stat.st_ino))
        return real_link(src, dst, **kwargs)
    monkeypatch.setattr(os, 'link', link)
    result = candidate.api.download(item, tmp_path)
    assert result == tmp_path / item.name
    assert result.read_bytes() == b'abcdef'
    assert (result.stat().st_dev, result.stat().st_ino) == observed[0]


def test_failure_never_unlinks_a_namespace_entry(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch, chunks=(b'abc',))
    foreign = tmp_path / '.quark-download-foreign.part'
    foreign.write_bytes(b'unrelated')
    identity = foreign.stat().st_ino
    unlinks = []
    def deny_unlink(*args, **kwargs):
        unlinks.append(args)
        raise AssertionError('download must not unlink any named object')
    monkeypatch.setattr(os, 'unlink', deny_unlink)
    assert candidate.api.download(item, tmp_path) is None
    assert unlinks == []
    assert foreign.read_bytes() == b'unrelated'
    assert foreign.stat().st_ino == identity
    assert list(tmp_path.iterdir()) == [foreign]


def test_unknown_previous_dictionary_time_is_persistable(env):
    previous = {'/deep/old.mkv': {'size': 10, 'modify_time': None}}
    env.api._request.return_value = page([raw('deep', 'd', True)])
    snapshot = env.plugin.snapshot_storage('夸克网盘', Path('/'), 1234, 1, previous)
    cls = extract_class(env.host/'app/monitor/snapshot.py', 'SnapshotStore', {'save'},
                        {'time': __import__('time'), 'json': __import__('json'), 'logger': env.logger})
    store = cls()
    store.VERSION = 2
    store._cache = Mock()
    store._drop_legacy = Mock()
    assert store.save('夸克网盘', snapshot, file_count=1)
    assert snapshot == {'/deep/old.mkv': {'size': 10, 'mtime_unknown': True}}
    assert previous == {'/deep/old.mkv': {'size': 10, 'modify_time': None}}


def test_missing_anonymous_inode_support_fails_closed(candidate, monkeypatch, tmp_path):
    """Characterization: unsupported platforms do not use an unsafe fallback."""
    item, response, factory = _download(candidate, monkeypatch)
    monkeypatch.delattr(os, 'O_TMPFILE', raising=False)
    foreign = tmp_path / 'unrelated'
    foreign.write_bytes(b'keep')
    assert candidate.api.download(item, tmp_path) is None
    assert list(tmp_path.iterdir()) == [foreign]
    assert foreign.read_bytes() == b'keep'


def test_missing_procfs_publication_fails_closed(candidate, monkeypatch, tmp_path):
    """Characterization: no named-temp fallback or foreign-object cleanup."""
    item, response, factory = _download(candidate, monkeypatch)
    def missing_proc(*args, **kwargs):
        raise FileNotFoundError('fixture procfs unavailable')
    monkeypatch.setattr(os, 'link', missing_proc)
    assert candidate.api.download(item, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_path_receipt_is_not_a_persistent_inode_handle(candidate, monkeypatch, tmp_path):
    """ABI limitation, not a safety guarantee: the consumer must trust the namespace."""
    item, response, factory = _download(candidate, monkeypatch)
    directory = tmp_path / 'chosen'
    directory.mkdir()
    result = candidate.api.download(item, directory)
    assert result == directory / item.name
    assert result.read_bytes() == b'abcdef'
    moved = tmp_path / 'moved'
    directory.rename(moved)
    directory.mkdir()
    (directory / item.name).write_bytes(b'foreign')
    assert (moved / item.name).read_bytes() == b'abcdef'
    assert result.read_bytes() == b'foreign'  # Path cannot bind its future consumer to an inode.
