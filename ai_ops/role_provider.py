"""Role-scoped provider credentials. SQLite holds only opaque immutable references.

New files are created before commit and removed on rollback. Replaced files are
retained until maintenance/backup retention so an in-flight request can finish
with its reserved credential, never another role's credential.
"""
from contextlib import contextmanager
import hashlib
import ipaddress
import http.client
import os
from pathlib import Path
import re
import socket
import tempfile
import uuid
from urllib.parse import urlsplit
import urllib.request
from fastapi import HTTPException
from .model_client import ModelFailure, OpenAICompatible


def validate_base_url(value):
    try:
        p = urlsplit(value)
        if p.scheme != 'https' or not p.hostname or p.username is not None or p.password is not None or p.query or p.fragment:
            raise ValueError()
        if p.port not in (None, 443):
            raise ValueError()
        host = p.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError()
        except ValueError as e:
            if re.fullmatch(r'[0-9.]+', host) or ':' in host:
                raise ValueError() from e
        return value.rstrip('/')
    except (ValueError, TypeError):
        raise ValueError('独立接口必须使用无凭据、查询参数的公网 HTTPS 地址（443端口）') from None


def check_destination(url):
    """Reject private/metadata resolution on every independent-provider request."""
    try:
        host = urlsplit(url).hostname
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ModelFailure('MODEL_DESTINATION_REJECTED')
        return addresses
    except ModelFailure:
        raise
    except Exception:
        raise ModelFailure('MODEL_DESTINATION_UNAVAILABLE') from None


class RoleProviders:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, ref):
        if not re.fullmatch(r'[0-9a-f]{64}-[0-9a-f]{32}', ref or ''):
            raise ModelFailure('MODEL_CREDENTIAL_REFERENCE_INVALID')
        return self.directory / (ref + '.key')

    def write(self, role_id, key):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        ref = hashlib.sha256(role_id.encode()).hexdigest() + '-' + uuid.uuid4().hex
        target = self.path(ref)
        fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=self.directory)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(key)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, target)
            return ref
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise

    @contextmanager
    def mutation(self):
        created = []
        try:
            yield created
        except BaseException:
            for ref in created:
                self.path(ref).unlink(missing_ok=True)
            raise

    def prepare(self, role_id, body, old, created):
        data = body.model_dump(exclude={'api_key', 'clear_api_key'})
        # Old clients do not know provider fields: retain independent routing.
        for field in ('provider_mode', 'base_url', 'max_model_steps', 'vision_enabled'):
            if field not in body.model_fields_set and field in old:
                data[field] = old[field]
        # Even inactive drafts must not persist a URL containing credentials.
        if data.get('base_url'):
            try:
                data['base_url'] = validate_base_url(data['base_url'])
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
        ref = old.get('credential_ref')
        if body.clear_api_key:
            ref = None
        key = body.api_key.get_secret_value().strip() if body.api_key else ''
        if key and body.clear_api_key:
            raise HTTPException(422, '不能同时写入和清除 API Key')
        if key:
            try:
                ref = self.write(role_id, key)
                created.append(ref)
            except OSError:
                raise HTTPException(503, '角色密钥存储不可写；配置未保存') from None
        if ref:
            data['credential_ref'] = ref
        if data['provider_mode'] == 'independent':
            try:
                data['base_url'] = validate_base_url(data['base_url'])
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
            if not data['model']:
                raise HTTPException(422, '独立接口需要填写模型名称')
            if data['enabled'] and not ref:
                raise HTTPException(422, '请配置独立 API Key，或先关闭模型')
        return data

    def view(self, settings):
        data = {k: v for k, v in settings.items() if k not in ('credential_ref', 'api_key', 'clear_api_key')}
        data.setdefault('provider_mode', 'inherit')
        data.setdefault('base_url', '')
        ref = settings.get('credential_ref')
        data['api_key_configured'] = bool(ref and self.path(ref).is_file())
        return data

    def client(self, settings, inherited):
        if settings.get('provider_mode', 'inherit') != 'independent':
            if inherited is None:
                raise ModelFailure('MODEL_DEFAULT_PROVIDER_NOT_CONFIGURED')
            return inherited
        ref = settings.get('credential_ref')
        if not ref:
            raise ModelFailure('MODEL_CREDENTIAL_NOT_CONFIGURED')
        return IndependentClient(settings['base_url'], self.path(ref))


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Pin the actual socket to checked DNS answers. TLS still verifies the
        # original hostname/SNI; a second DNS lookup cannot rebind to metadata.
        addresses = check_destination('https://' + self.host)
        for family, kind, protocol, _, address in addresses:
            sock = socket.socket(family, kind, protocol)
            sock.settimeout(self.timeout)
            try:
                sock.connect(address)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except Exception:
                sock.close()
        raise ModelFailure('MODEL_TRANSPORT_OR_JSON_ERROR')


class PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PublicHTTPSConnection, req, context=self._context)


class IndependentClient(OpenAICompatible):
    def __init__(self, base_url, key_file):
        super().__init__(validate_base_url(base_url), key_file)
        from .model_client import NoRedirect
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), PublicHTTPSHandler())
