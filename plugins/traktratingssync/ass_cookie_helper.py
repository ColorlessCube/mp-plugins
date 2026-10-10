"""插件内的 ASS 只读 Cookie 适配器；不提供 API、MCP、任意请求或磁盘缓存。"""
import json
import math
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit
from uuid import UUID

import requests
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.utils.http import RequestUtils


class AssCookieError(Exception):
    """仅携带固定错误分类，禁止包装含凭据的底层异常。"""


class _SafeSession(requests.Session):
    """阻止 RequestUtils 将传输异常的请求对象或凭据记录到日志。"""

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        """将传输异常替换为固定消息，正常响应仍由调用方验证。"""
        try:
            return super().request(*args, **kwargs)
        except requests.RequestException:
            raise requests.RequestException("credential_transport_failed") from None


class CredentialRequestUtils:
    """网站凭据请求仍通过 RequestUtils，使用独立安全 Session 且禁止跳转。"""

    def __init__(self, **kwargs: Any) -> None:
        """仅在一次请求内保存请求参数，不积累网站 Cookie jar。"""
        self._kwargs = kwargs

    def _request(self, method: str, kwargs):
        with _SafeSession() as session:
            session.trust_env = False
            kwargs.update(verify=True, allow_redirects=False)
            request = RequestUtils(session=session, **self._kwargs)
            return getattr(request, method)(**kwargs)

    def get_res(self, **kwargs: Any) -> Optional[requests.Response]:
        """发送固定网站 GET，不让凭据跟随 HTTP 重定向。"""
        return self._request("get_res", kwargs)

    def post_res(self, **kwargs: Any) -> Optional[requests.Response]:
        """发送固定网站 POST，不打印底层传输异常。"""
        return self._request("post_res", kwargs)


class _Cookie(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    name: str = Field(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$")
    value: SecretStr = Field(max_length=4096)
    domain: str = Field(min_length=1, max_length=254)
    path: str = Field(pattern=r"^/", max_length=1024)
    hostOnly: bool
    secure: bool
    httpOnly: bool
    sameSite: Literal["no_restriction", "lax", "strict", "unspecified"]
    session: bool
    storeId: Literal["0"]
    expirationDate: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)


class _Bundle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    format: Literal["ass-cookie-bundle"]
    schema_version: int
    site_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,62}$")
    account_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,62}$")
    producer_id: str
    snapshot_id: str
    ordering_mode: Literal["snapshot_created_at"]
    snapshot_created_at: str
    cookie_generated_at: None = None
    observed_at: str
    state: Literal["captured", "invalid"]
    reason: Optional[Literal["required_cookie_missing"]] = None
    source_origins: List[str] = Field(min_length=1, max_length=20)
    cookies: List[_Cookie] = Field(max_length=100, repr=False)


def _json_object(pairs):
    """拒绝重复 JSON 字段，避免解码器之间出现不同身份解释。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _decode(raw):
    """只解码有界 UTF-8 JSON 对象，不接受非有限数。"""
    def invalid_constant(_value):
        raise ValueError
    data = json.loads(raw, object_pairs_hook=_json_object, parse_constant=invalid_constant)
    if not isinstance(data, dict):
        raise ValueError
    return data


def _timestamp(value: str, milliseconds: bool = True) -> float:
    """检查带时区的毫秒时间，不将生成时间当作网站签发时间。"""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or (milliseconds and parsed.microsecond % 1000):
        raise ValueError
    return parsed.timestamp()


def _uuid(value: str) -> str:
    """使用规范 UUID 固定引用身份。"""
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError
    return value


class AssCookieHelper:
    """每次操作从受保护文件读取 Read Key，并验证身份、版本与完整快照。"""

    _TOKEN = re.compile(r"ass_([0-9a-f]{32})_[A-Za-z0-9_-]{43}\Z")
    _SOURCES = {"douban": "https://www.douban.com", "netease": "https://music.163.com"}

    def __init__(self, config: Dict[str, Any], site: str) -> None:
        """绑定固定网站及精确条目，配置中只保存连接与非敏感引用。"""
        try:
            if site not in self._SOURCES:
                raise ValueError
            self._site = site
            origin = config.get("ass_origin", "")
            url = urlsplit(origin)
            if (url.scheme != "https" or not url.hostname or url.username is not None
                    or url.password is not None or url.path not in ("", "/")
                    or url.query or url.fragment or any(ord(c) <= 32 for c in origin)):
                raise ValueError
            _ = url.port
            self._origin = origin.rstrip("/")
            self._key_file = Path(config.get("ass_key_file", ""))
            if not self._key_file.is_absolute():
                raise ValueError
            self._tenant = _uuid(config.get("ass_tenant_id"))
            self._secret = _uuid(config.get(f"{site}_ass_secret_id"))
            self._account = config.get(f"{site}_ass_account_id", "")
            self._site_id = config.get(f"{site}_ass_site_id", site)
            if any(not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,62}", v)
                   for v in (self._account, self._site_id)):
                raise ValueError
            self._age = config.get("ass_max_age", 3600)
            if type(self._age) is not int or not 60 <= self._age <= 86400:
                raise ValueError
            self._csrf_scope = (config.get("netease_ass_csrf_scope", "reject_ambiguous")
                                if site == "netease" else "reject_ambiguous")
            if self._csrf_scope not in ("reject_ambiguous", "host", "domain"):
                raise ValueError
        except Exception:
            raise AssCookieError("ass_configuration_invalid") from None

    def _read_key(self) -> str:
        """拒绝符号链接、特殊文件、宽权限及超长输入；不缓存密钥。"""
        try:
            fd = os.open(self._key_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                        or info.st_mode & 0o077 or info.st_mode & 0o111):
                    raise ValueError
                token = source.read(130).removesuffix(b"\n").decode("ascii")
            if not self._TOKEN.fullmatch(token):
                raise ValueError
            return token
        except Exception:
            raise AssCookieError("ass_key_file_invalid") from None

    def _get(self, session, token: str, path: str, deadline: float) -> dict:
        """TLS 校验、无重定向、限时限长读取；输出仅在进程内消费。"""
        try:
            if time.monotonic() >= deadline:
                raise AssCookieError("ass_unavailable")
            if session is not None:
                session.cookies.clear()
            with RequestUtils(session=session, timeout=(2, 5), headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json",
            }).response_manager("GET", self._origin + path, verify=True,
                                allow_redirects=False, stream=True) as response:
                if response is None:
                    raise AssCookieError("ass_unavailable")
                if response.status_code != 200:
                    code = {401: "ass_authentication_rejected", 403: "ass_access_denied",
                            404: "ass_secret_unavailable", 429: "ass_rate_limited"}.get(
                                response.status_code, "ass_unavailable")
                    raise AssCookieError(code)
                raw = bytearray()
                for chunk in response.iter_content(1024):
                    if time.monotonic() > deadline or len(raw) + len(chunk) > 20 * 1024:
                        raise AssCookieError("ass_response_invalid")
                    raw.extend(chunk)
                return _decode(raw.decode("utf-8"))
        except AssCookieError:
            raise
        except Exception:
            raise AssCookieError("ass_response_invalid") from None

    def read_cookies(self) -> Dict[str, str]:
        """新鲜授权读取；失败不回退手动或上次 Cookie，不写回插件配置。"""
        token = self._read_key()
        try:
            deadline = time.monotonic() + 20
            with _SafeSession() as session:
                session.trust_env = False
                status = self._get(session, token, "/api/v1/secrets/access-status", deadline)
                now = time.time()
                if (status.get("tenant_id") != self._tenant
                        or status.get("key_id") != str(UUID(self._TOKEN.fullmatch(token).group(1)))
                        or status.get("value_scope_authorized") is not True
                        or type(status.get("revision")) is not int or status["revision"] < 1
                        or type(status.get("active_grant_count")) is not int
                        or not 1 <= status["active_grant_count"] <= 2
                        or not -5 <= now - _timestamp(status["checked_at"], milliseconds=False) <= 60
                        or (status.get("expires_at") is not None
                            and _timestamp(status["expires_at"], milliseconds=False) <= now)):
                    raise AssCookieError("ass_identity_mismatch")
                path = f"/api/v1/secrets/{self._site}"
                before = self._get(session, token, path + "/metadata", deadline)
                version = before.get("current_version")
                if (before.get("id") != self._secret or before.get("name") != self._site
                        or type(version) is not int or version < 1):
                    raise AssCookieError("ass_secret_binding_changed")
                value = self._get(session, token, path + "/value", deadline)
                after = self._get(session, token, path + "/metadata", deadline)
                if (after.get("id") != self._secret or after.get("name") != self._site
                        or type(after.get("current_version")) is not int
                        or after["current_version"] != version or value.get("name") != self._site
                        or type(value.get("version")) is not int or value["version"] != version):
                    raise AssCookieError("ass_secret_binding_changed")
                return self._project(value.get("value"), time.time())
        except AssCookieError:
            raise
        except Exception:
            raise AssCookieError("ass_response_invalid") from None

    def _project(self, raw: str, now: float) -> Dict[str, str]:
        """验证完整 v1/v2 快照后，按固定域、根路径及明确作用域投影。"""
        try:
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16384:
                raise ValueError
            bundle = _Bundle.model_validate(_decode(raw))
            if bundle.schema_version not in (1, 2):
                raise ValueError
            _uuid(bundle.producer_id)
            _uuid(bundle.snapshot_id)
            created, observed = _timestamp(bundle.snapshot_created_at), _timestamp(bundle.observed_at)
            if observed < created:
                raise ValueError
            if (bundle.site_id != self._site_id or bundle.account_id != self._account
                    or set(bundle.source_origins) != {self._SOURCES[self._site]}):
                raise AssCookieError("ass_cookie_binding_mismatch")
            if bundle.state == "invalid":
                if bundle.cookies or bundle.reason != "required_cookie_missing":
                    raise ValueError
                raise AssCookieError("ass_cookie_invalidated")
            if not bundle.cookies or bundle.reason is not None:
                raise ValueError
            if not -60 <= now - observed <= self._age or created - now > 60:
                raise AssCookieError("ass_cookie_expired")
            allowed = ({"dbcl2": "douban.com", "bid": "douban.com", "ck": "douban.com"}
                       if self._site == "douban" else {"MUSIC_U": "music.163.com", "__csrf": "music.163.com"})
            required = "dbcl2" if self._site == "douban" else "MUSIC_U"
            identities = set()
            selected = {}
            for cookie in bundle.cookies:
                domain = cookie.domain.removeprefix(".")
                if (cookie.name not in allowed or domain != allowed[cookie.name] or cookie.path != "/"
                        or (cookie.hostOnly and cookie.domain.startswith("."))
                        or cookie.session != (cookie.expirationDate is None)):
                    raise ValueError
                identity = (cookie.name, domain, cookie.path)
                if bundle.schema_version == 2:
                    identity += (cookie.hostOnly,)
                if identity in identities:
                    raise ValueError
                identities.add(identity)
                value = cookie.value.get_secret_value()
                # 豆瓣原生 dbcl2 有平衡的外层双引号；保留原值，不转义或剥离。
                inner = value[1:-1] if len(value) >= 2 and value.startswith('"') and value.endswith('"') else value
                if any(not (c == "!" or "#" <= c <= "+" or "-" <= c <= ":"
                            or "<" <= c <= "[" or "]" <= c <= "~") for c in inner):
                    raise ValueError
                if cookie.expirationDate is not None and (not math.isfinite(cookie.expirationDate)
                                                         or cookie.expirationDate <= now):
                    continue
                if cookie.name == required and cookie.hostOnly:
                    continue
                if self._site == "douban" and (cookie.name == "ck" or cookie.hostOnly):
                    continue
                if cookie.name == "__csrf" and self._csrf_scope != "reject_ambiguous":
                    if cookie.hostOnly != (self._csrf_scope == "host"):
                        continue
                if cookie.name in selected:
                    raise AssCookieError("ass_cookie_ambiguous")
                selected[cookie.name] = value
            if not selected.get(required):
                raise AssCookieError("ass_cookie_required_missing")
            if self._site == "netease" and self._csrf_scope != "reject_ambiguous" and "__csrf" not in selected:
                raise AssCookieError("ass_cookie_required_missing")
            return selected
        except AssCookieError:
            raise
        except Exception:
            raise AssCookieError("ass_cookie_bundle_invalid") from None
