"""V3 传输目录下载的真实文件系统安全回归，HTTP 仅在明确边界回放。"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_quarkdisk_v3_compatibility import _FileItem, candidate


def _download(candidate, monkeypatch, name="movie.mkv", chunks=(b"abc", b"def"), status=200):
    item = _FileItem(storage="夸克网盘", type="file", path="/movie.mkv", name=name,
                     fileid="1", size=6)
    api = candidate.api
    monkeypatch.setattr(api, "get_item", Mock(return_value=item))
    monkeypatch.setattr(api, "get_item_strict", Mock(return_value=item))
    api._request.side_effect = None
    api._request.return_value = {"data": [{"download_url": "https://download.example.test/file"}]}
    response = Mock(status_code=status, headers={"Content-Length": "6"})
    response.iter_content.return_value = iter(chunks)
    client = Mock()
    client.get_res.return_value = response
    factory = Mock(return_value=client)
    # 新路径使用宿主 RequestUtils，旧路径的 requests.get 也隔离，确保 RED 不触网。
    monkeypatch.setattr(candidate.api_module, "RequestUtils", factory, raising=False)
    monkeypatch.setattr(candidate.api_module.requests, "get", Mock(return_value=response))
    return item, response, factory


def test_download_checks_content_length_when_size_is_unknown(candidate, monkeypatch, tmp_path):
    """缺少云端大小时仍核对 HTTP 长度，不能把短响应标成完整下载。"""
    item, response, _factory = _download(candidate, monkeypatch, chunks=(b"abc",))
    item.size = None
    assert candidate.api.download(item, tmp_path) is None
    assert list(tmp_path.iterdir()) == []
    response.close.assert_called_once()


def test_download_cleanup_failure_does_not_reverse_committed_result(candidate, monkeypatch, tmp_path):
    """已完整发布的本地文件不因关闭响应失败而变成异常或泄漏其他资源。"""
    item, response, factory = _download(candidate, monkeypatch)
    response.close.side_effect = OSError("fixture-secret")
    result = candidate.api.download(item, tmp_path)
    assert result == tmp_path / item.name
    assert result.read_bytes() == b"abcdef"
    factory.return_value.close.assert_called_once()
    candidate.progress.return_value.end.assert_called_once_with(text="下载完成", value=100)


def test_download_rejects_replaced_staging_file(candidate, monkeypatch, tmp_path):
    """不能把被替换的临时符号链接作为已完成媒体返回。"""
    item, response, _factory = _download(candidate, monkeypatch)
    protected = tmp_path / "protected"
    protected.write_text("unrelated")

    def replaced():
        yield b"abcdef"
        temporary = next(tmp_path.glob(".quark-download-*.part"))
        temporary.unlink()
        temporary.symlink_to(protected)

    response.iter_content.return_value = replaced()
    assert candidate.api.download(item, tmp_path) is None
    assert not (tmp_path / item.name).exists()
    assert protected.read_text() == "unrelated"


def test_download_accepts_existing_v3_temporary_directory(candidate, monkeypatch, tmp_path):
    """实际写入核心传来的已存在目录，返回其中的完整文件路径。"""
    item, response, factory = _download(candidate, monkeypatch)
    directory = tmp_path / "transfer"
    directory.mkdir()
    result = candidate.plugin.download_file(item, directory)
    assert result == directory / "movie.mkv"
    assert result.read_bytes() == b"abcdef"
    assert list(directory.iterdir()) == [result]
    response.close.assert_called_once()
    assert factory.call_args.kwargs["verify"] is True
    assert factory.return_value.get_res.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/b", "a\\b", ".", "..", "bad\x00name"])
def test_download_rejects_remote_path_components(candidate, monkeypatch, tmp_path, name):
    """不将远端文件名解释为路径，也不静默截断后写到意外位置。"""
    item, _response, _factory = _download(candidate, monkeypatch, name=name)
    assert candidate.api.download(item, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling"])
def test_download_never_overwrites_preexisting_target(candidate, monkeypatch, tmp_path, kind):
    """已有目标及符号链接均被拒绝，异常清理不能删除它们。"""
    item, _response, _factory = _download(candidate, monkeypatch)
    directory = tmp_path / "download"
    directory.mkdir()
    target = directory / item.name
    protected = tmp_path / "protected"
    protected.write_bytes(b"unrelated")
    if kind == "file":
        target.write_bytes(b"existing")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(protected if kind == "symlink" else tmp_path / "absent")
    assert candidate.api.download(item, directory) is None
    assert target.exists() or target.is_symlink()
    assert protected.read_bytes() == b"unrelated"
    if kind == "file":
        assert target.read_bytes() == b"existing"
    assert list(directory.iterdir()) == [target]


def test_download_rejects_symlink_ancestor(candidate, monkeypatch, tmp_path):
    """文件名本身安全也不能经父目录符号链接逃逸。"""
    item, _response, _factory = _download(candidate, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    assert candidate.api.download(item, linked / "nested" / "new.mkv") is None
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("fault", ["stream", "truncated", "http", "cancel"])
def test_failed_download_cleans_only_own_temporary_file(candidate, monkeypatch, tmp_path, fault):
    """流中断、短响应、非成功 HTTP 或取消均不能留半成品、误报完成或删除旁文件。"""
    item, response, _factory = _download(candidate, monkeypatch, status=503 if fault == "http" else 200)
    if fault == "stream":
        def broken():
            yield b"abc"
            raise OSError("fixture-secret")
        response.iter_content.return_value = broken()
    elif fault == "truncated":
        response.iter_content.return_value = iter([b"abc"])
    elif fault == "cancel":
        candidate.stop.consume_transfer_stop.side_effect = [False, False, True]
    untouched = tmp_path / "unrelated.txt"
    untouched.write_text("keep")
    assert candidate.api.download(item, tmp_path) is None
    assert list(tmp_path.iterdir()) == [untouched]
    assert untouched.read_text() == "keep"
    progress = candidate.progress.return_value
    assert not any(call.kwargs.get("value") == 100 for call in progress.end.call_args_list)
