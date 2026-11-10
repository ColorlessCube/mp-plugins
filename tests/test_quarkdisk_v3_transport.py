"""继承上传流程只验证宿主传输适配，不连接真实 OSS 或调用真实云端鉴权。"""
from unittest.mock import Mock

import pytest

from test_quarkdisk_v3_compatibility import candidate


@pytest.mark.parametrize("operation", ["parts", "commit"])
def test_inherited_upload_uses_host_transport(candidate, monkeypatch, tmp_path, operation):
    """保留原签名与请求载荷，上传数据传输不能绕过宿主请求工具。"""
    api = candidate.api
    api._request_upload_auth = Mock(return_value="fixture-auth-not-a-live-credential")
    response = Mock(status_code=200, headers={"ETag": '"fixture-etag"'})
    client = Mock()
    client.request.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr(candidate.api_module, "RequestUtils", factory)
    for method in ("put", "post"):
        monkeypatch.setattr(candidate.api_module.requests, method,
                            Mock(side_effect=AssertionError("禁止直接云端请求")))
    pre_data = {"bucket": "fixture-bucket", "obj_key": "fixture-key",
                "upload_url": "objects.example.test", "upload_id": "fixture-upload-id"}
    if operation == "parts":
        local = tmp_path / "fixture.bin"
        local.write_bytes(b"abcdef")
        assert api._upload_parts(local, pre_data, 10, "application/octet-stream") == ['"fixture-etag"']
        assert client.request.call_args.kwargs["data"] == b"abcdef"
        assert client.request.call_args.kwargs["params"]["partNumber"] == 1
    else:
        assert api._commit_upload(pre_data, ['"fixture-etag"']) is True
        assert b"&quot;fixture-etag&quot;" not in client.request.call_args.kwargs["data"]
        assert b'"fixture-etag"' in client.request.call_args.kwargs["data"]
    assert factory.call_args.kwargs["verify"] is True
    assert client.request.call_args.kwargs["allow_redirects"] is False
    assert client.request.call_count == 1
    response.close.assert_called_once()
    client.close.assert_called_once()
