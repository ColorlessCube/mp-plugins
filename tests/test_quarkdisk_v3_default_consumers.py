"""Offline lifecycle regressions using exact official V3.2.5 consumer bodies.

Fixtures are full public sources from jxxghp/MoviePilot revision
4ed5fa079897e4b02615abb01b78efc1d1272cae; never host configuration.
Only explicit I/O boundaries are doubled; response classes and files are real.
"""
import ast
import asyncio
import datetime
import hashlib
import os
import shutil
import stat
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import Mock

import pytest
from fastapi import Depends, HTTPException
from starlette.responses import FileResponse, Response

from test_quarkdisk_v3_compatibility import candidate
from test_quarkdisk_v3_download import _download
from test_quarkdisk_v3_host_sources import host_source_root
from test_quarkdisk_v3_independent_contracts import extract_class
from test_quarkdisk_v3_independent_probes import offline

FIXTURES = Path(__file__).parent / "fixtures" / "quarkdisk_v3_consumers"
DIGESTS = {
    "app/api/endpoints/storage.py": "02ec671ff937501debf06aa898bbcc58071210e55e586a29b4657ec26cec76cc",
    "app/adapters/system/host.py": "1d7b7c5526d3dfbab095b3dd2bec1d380bf8b61bdfd8bbabde538f1a93baff89",
    "app/startup/initializers/modules.py": "4a6726aa0e83ce58a0820920a9e4eb946fc8ea41b4fe83f62e0026ed7d6ae25f",
}


def _consumer(source, name, namespace, class_name=None):
    raw = (FIXTURES / source).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == DIGESTS[source]
    tree = ast.parse(raw)
    body = tree.body
    if class_name:
        body = next(n for n in body if isinstance(n, ast.ClassDef) and n.name == class_name).body
    fn = next(n for n in body if isinstance(n, ast.FunctionDef) and n.name == name)
    fn.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), fn], type_ignores=[])
    # Only fixed, digest-verified public function bodies, never runtime input.
    exec(compile(ast.fix_missing_locations(module), source, "exec"), namespace)
    return namespace[name]


def _endpoint(name, candidate, results):
    def download_file(*args, **kwargs):
        result = candidate.plugin.download_file(*args, **kwargs)
        results.append(result)
        return result
    return _consumer("app/api/endpoints/storage.py", name, {
        "Depends": Depends, "get_current_active_manage_user": lambda: None,
        "_ensure_manage_storage_item": lambda item, principal: item,
        "StorageChain": lambda: types.SimpleNamespace(download_file=download_file),
        "FileResponse": FileResponse, "Response": Response, "HTTPException": HTTPException,
        "_SchemaResponse": lambda **kw: types.SimpleNamespace(**kw),
    })


def _body(response):
    """Actually exercise deferred FileResponse reads, not just path inspection."""
    messages = []
    async def consume():
        async def send(message):
            messages.append(message)
        async def receive():
            return {"type": "http.disconnect"}
        await response({"type": "http", "method": "GET", "headers": [],
                        "asgi": {"spec_version": "2.4"}}, receive, send)
    asyncio.run(consume())
    assert messages[0]["status"] == 200
    return b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")


@pytest.mark.parametrize("name", ["download", "image"])
def test_real_default_endpoint_accepts_a_second_request(candidate, monkeypatch, tmp_path, name):
    item, response, factory = _download(candidate, monkeypatch, name="海报.jpg")
    results = []
    call = _endpoint(name, candidate, results)
    first = call(item, None)
    expected_type = FileResponse if name == "download" else Response
    assert type(first) is expected_type
    first_path = results[0]
    assert first_path.read_bytes() == b"abcdef"
    assert first_path.name == item.name
    assert first.media_type == "image/jpeg"
    assert _body(first) == b"abcdef"
    response.iter_content.return_value = iter([b"uvwxyz"])
    second = call(item, None)
    assert type(second) is expected_type, "normal repeated endpoint request failed on its own retained temp file"
    assert results[1] != first_path
    assert results[1].name == item.name
    assert second.media_type == "image/jpeg"
    assert _body(second) == b"uvwxyz"
    assert first_path.read_bytes() == b"abcdef"
    assert _body(first) == b"abcdef", "first FileResponse must remain readable after another request"
    assert factory.return_value.get_res.call_count == 2


@pytest.mark.parametrize("name", ["download", "image"])
def test_same_basename_in_distinct_remote_folders(candidate, monkeypatch, tmp_path, name):
    item, response, factory = _download(candidate, monkeypatch, name="poster.jpg")
    other = item.model_copy(update={"path": "/other/poster.jpg", "fileid": "2"})
    candidate.api.get_item_strict.side_effect = lambda path: {item.path: item, other.path: other}[str(path)]
    results = []
    call = _endpoint(name, candidate, results)
    first = call(item, None)
    response.iter_content.return_value = iter([b"UVWXYZ"])
    second = call(other, None)
    assert type(first) is type(second) is (FileResponse if name == "download" else Response)
    assert _body(first) == b"abcdef"
    assert _body(second) == b"UVWXYZ"
    assert results[0] != results[1]
    assert [p.name for p in results] == [item.name, item.name]
    assert [p.read_bytes() for p in results] == [b"abcdef", b"UVWXYZ"]
    assert [c.kwargs["json_data"]["fids"] for c in candidate.api._request.call_args_list] == [["1"], ["2"]]


@pytest.mark.parametrize("name", ["download", "image"])
@pytest.mark.parametrize("distinct_remote", [False, True])
def test_concurrent_defaults_do_not_collide(candidate, monkeypatch, tmp_path, name, distinct_remote):
    item, _response, factory = _download(candidate, monkeypatch, name="poster.jpg")
    other = item.model_copy(update={"path": "/other/poster.jpg", "fileid": "2"}) if distinct_remote else item
    candidate.api.get_item_strict.side_effect = lambda path: {i.path: i for i in (item, other)}[str(path)]
    candidate.api._request.side_effect = lambda *a, **kw: {"data": [{"download_url": "https://download.example.test/" + kw["json_data"]["fids"][0]}]}
    barrier = Barrier(2)
    responses = []
    def client_factory(**kwargs):
        def get_res(url, **kwargs):
            body = b"UVWXYZ" if url.endswith("/2") else b"abcdef"
            def chunks():
                barrier.wait(timeout=10)
                yield body
            response = Mock(status_code=200, headers={"Content-Length": "6"})
            response.iter_content.side_effect = lambda **kw: chunks()
            responses.append(response)
            return response
        return types.SimpleNamespace(get_res=get_res, close=lambda: None)
    factory.side_effect = client_factory
    results = []
    call = _endpoint(name, candidate, results)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(call, i, None) for i in (item, other)]
        replies = [f.result(timeout=20) for f in futures]
    assert all(type(r) is (FileResponse if name == "download" else Response) for r in replies)
    assert [_body(r) for r in replies] == [b"abcdef", b"UVWXYZ" if distinct_remote else b"abcdef"]
    assert len(results) == len(set(results)) == 2
    assert all(p.name == item.name and p.parent.parent == tmp_path for p in results)
    assert all(stat.S_IMODE(p.parent.stat().st_mode) == 0o700 for p in results)
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in results)
    assert all(r.close.call_count == 1 for r in responses)


@pytest.mark.parametrize("fault", ["stream", "truncated", "http", "cancel"])
def test_failed_default_does_not_publish_or_clean_foreign_files(candidate, monkeypatch, tmp_path, fault):
    item, response, factory = _download(candidate, monkeypatch)
    first = candidate.plugin.download_file(item)
    assert first.read_bytes() == b"abcdef"
    foreign = tmp_path / item.name
    foreign.write_bytes(b"foreign-existing")
    neighbor = first.parent / "unrelated"
    neighbor.write_bytes(b"keep-neighbor")
    existing = {p for p in tmp_path.rglob("*") if p.is_file()}
    if fault == "stream":
        def broken():
            yield b"abc"
            raise OSError("fixture-only-error")
        response.iter_content.return_value = broken()
    elif fault == "truncated":
        response.iter_content.return_value = iter([b"abc"])
    elif fault == "http":
        response.status_code = 503
    else:
        response.iter_content.return_value = iter([b"abcdef"])
        candidate.stop.consume_transfer_stop.side_effect = [False, False, True]
    assert candidate.plugin.download_file(item) is None
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == existing
    assert first.read_bytes() == b"abcdef"
    assert foreign.read_bytes() == b"foreign-existing"
    assert neighbor.read_bytes() == b"keep-neighbor"
    assert response.close.call_count == 2
    assert factory.return_value.close.call_count == 2


def test_default_does_not_reuse_or_overwrite_existing_basename(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch)
    existing = tmp_path / item.name
    existing.write_bytes(b"foreign-existing")
    result = candidate.api.download(item)
    assert result is not None and result != existing
    assert result.name == item.name
    assert result.read_bytes() == b"abcdef"
    assert existing.read_bytes() == b"foreign-existing"
    # Explicit directory and explicit complete filename still refuse overwrite.
    assert candidate.api.download(item, tmp_path) is None
    assert candidate.api.download(item, existing) is None
    assert existing.read_bytes() == b"foreign-existing"
    assert factory.return_value.get_res.call_count == 1


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_default_namespace_collision_fails_without_reuse(candidate, monkeypatch, tmp_path, kind):
    item, response, factory = _download(candidate, monkeypatch)
    fixed = types.SimpleNamespace(hex="fixed-fixture-id")
    monkeypatch.setattr(candidate.api_module.uuid, "uuid4", lambda: fixed)
    existing = tmp_path / ".quark-download-fixed-fixture-id"
    foreign_dir = tmp_path / "foreign"
    foreign_dir.mkdir()
    if kind == "directory":
        existing.mkdir()
    else:
        existing.symlink_to(foreign_dir, target_is_directory=True)
    foreign = existing / item.name
    foreign.write_bytes(b"foreign-existing")
    assert candidate.api.download(item) is None
    assert foreign.read_bytes() == b"foreign-existing"
    assert existing.is_symlink() == (kind == "symlink")
    factory.assert_not_called()


def test_default_rejects_symlink_temp_ancestor(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(candidate.api_module.settings, "TEMP_PATH", linked)
    assert candidate.api.download(item) is None
    assert list(outside.iterdir()) == []
    factory.assert_not_called()


def test_real_transfer_temp_directory_lifetime(candidate, monkeypatch, tmp_path, host_source_root):
    item, response, factory = _download(candidate, monkeypatch)
    seen = {}
    def download(**kwargs):
        directory = kwargs["path"]
        seen["directory"] = directory
        seen["mode"] = stat.S_IMODE(directory.stat().st_mode)
        result = candidate.api.download(**kwargs)
        assert result == directory / item.name
        assert result.read_bytes() == b"abcdef"
        return result
    def move(src, dst):
        shutil.move(src, dst)
        return 0, ""
    cls = extract_class(host_source_root / "app/modules/filemanager/transhandler.py", "TransHandler", {"__download_to_local"}, {
        "TemporaryDirectory": TemporaryDirectory, "Path": Path,
        "SystemUtils": types.SimpleNamespace(move=move),
    })
    target = tmp_path / "target" / "renamed.mkv"
    result = cls._TransHandler__download_to_local(item, types.SimpleNamespace(download=download), target, "copy")
    assert result == (True, "")
    assert target.read_bytes() == b"abcdef"
    assert seen["mode"] == 0o700
    assert not seen["directory"].exists()


def test_real_host_cleanup_retains_fresh_default_and_expires_old_result(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch)
    results = []
    call = _endpoint("download", candidate, results)
    old_response = call(item, None)
    old = results[-1]
    response.iter_content.return_value = iter([b"UVWXYZ"])
    fresh_response = call(item, None)
    fresh = results[-1]
    clear = _consumer("app/adapters/system/host.py", "clear", {"datetime": datetime}, "SystemUtils")
    cache = tmp_path / "cache"
    settings = {"TEMP_PATH": tmp_path, "TEMP_FILE_DAYS": 3, "CACHE_PATH": cache, "GLOBAL_IMAGE_CACHE_DAYS": 7}
    clear_temp = _consumer("app/startup/initializers/modules.py", "clear_temp", {
        "SystemUtils": types.SimpleNamespace(clear=clear),
        "get_runtime_setting": settings.__getitem__, "clear_package_tool_cache": lambda: None,
    })
    clear_temp()
    assert _body(old_response) == b"abcdef"
    assert _body(fresh_response) == b"UVWXYZ"
    assert old.parent.is_dir() and fresh.parent.is_dir()
    expired = (datetime.datetime.now() - datetime.timedelta(days=10)).timestamp()
    os.utime(old, (expired, expired))
    clear_temp()
    assert not old.exists() and not old.parent.exists()
    assert _body(fresh_response) == b"UVWXYZ"
    assert fresh.parent.is_dir()



def test_first_file_response_can_stream_while_second_default_completes(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch)
    paths = []
    call = _endpoint("download", candidate, paths)
    first = call(item, None)
    assert type(first) is FileResponse
    first.chunk_size = 3
    messages = []
    second_responses = []
    async def consume():
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not second_responses:
                assert message["body"] == b"abc" and message["more_body"]
                response.iter_content.return_value = iter([b"UVWXYZ"])
                second = call(item, None)
                assert type(second) is FileResponse
                second_responses.append(second)
                assert paths[0].read_bytes() == b"abcdef"
                assert paths[1].read_bytes() == b"UVWXYZ"
        await first({"type": "http", "method": "GET", "headers": [],
                     "asgi": {"spec_version": "2.4"}}, receive, send)
    asyncio.run(consume())
    assert b"".join(m.get("body", b"") for m in messages) == b"abcdef"
    assert _body(second_responses[0]) == b"UVWXYZ"
    assert paths[0] != paths[1]
    assert paths[0].exists()


def test_default_publication_race_never_overwrites_or_removes_foreign_file(candidate, monkeypatch, tmp_path):
    item, response, factory = _download(candidate, monkeypatch)
    real_link = os.link
    foreign = []
    def competing_link(src, dst, *args, **kwargs):
        directories = list(tmp_path.glob(".quark-download-*"))
        assert len(directories) == 1
        path = directories[0] / dst
        path.write_bytes(b"foreign-won-publication")
        foreign.append(path)
        return real_link(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, "link", competing_link)
    assert candidate.api.download(item) is None
    assert foreign[0].read_bytes() == b"foreign-won-publication"
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == foreign
    response.close.assert_called_once()
