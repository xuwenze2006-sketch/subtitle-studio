"""Current-user Windows DPAPI storage for local subtitle provider credentials.

Only ciphertext is persisted. No provider requests or environment/registry
changes are performed here. A store cannot normally be moved to another user
or computer; callers should surface CredentialStoreError without logging keys.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import time


SUPPORTED_KEYS = ('SILICONFLOW_API_KEY', 'DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY')
_VERSION = 1
_MAX_KEY_LENGTH = 4096
_MAX_FILE_BYTES = 128 * 1024
_LOCK_TIMEOUT_SECONDS = 10
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class CredentialStoreError(ValueError):
    """Safe credential-store failure whose message never includes credentials."""


def default_path() -> Path:
    """Return the per-user store location without reading or creating it."""
    local = os.environ.get('LOCALAPPDATA')
    base = Path(local) if local else Path.home() / 'AppData' / 'Local'
    return base / 'SubtitlePipeline' / 'credentials.json'


def _require_windows() -> None:
    if sys.platform != 'win32':
        raise CredentialStoreError('本机加密凭据存储仅支持 Windows。')


def _validated(values: dict[str, str]) -> dict[str, str]:
    if not isinstance(values, dict):
        raise CredentialStoreError('API Key 设置格式无效。')
    clean = {}
    for name, value in values.items():
        if name not in SUPPORTED_KEYS:
            raise CredentialStoreError('不支持此 API Key 设置项。')
        if (not isinstance(value, str) or not 1 <= len(value) <= _MAX_KEY_LENGTH
                or not value.isprintable() or any(char.isspace() for char in value)):
            raise CredentialStoreError('API Key 必须为非空文本，不含空白或控制字符，且长度不超过 4096。')
        clean[name] = value
    return clean


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    _require_windows()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    blob_pointer = ctypes.POINTER(_DataBlob)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    description_type = ctypes.POINTER(wintypes.LPWSTR) if decrypt else wintypes.LPCWSTR
    function.argtypes = [blob_pointer, description_type, blob_pointer,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _DataBlob()
    try:
        # LOCAL_MACHINE is deliberately absent: bind to the current Windows user.
        success = function(ctypes.byref(source), None, None, None, None,
                           _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result))
        if not success:
            raise CredentialStoreError('Windows 无法加密或解密本机凭据。')
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if result.pbData:
            if decrypt:
                ctypes.memset(result.pbData, 0, result.cbData)
            kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _protect(data: bytes) -> bytes:
    return _dpapi(data, decrypt=False)


def _unprotect(data: bytes) -> bytes:
    return _dpapi(data, decrypt=True)


@contextmanager
def _store_lock(path: Path):
    """Serialize readers and merge/writes across threads and processes.

    Keep the lock file after closing it: removing a lock file creates races
    between holders of the old file and callers opening a replacement file.
    Windows releases the byte lock automatically if a process exits.
    """
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.lock').open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0')
            stream.flush()
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise CredentialStoreError('本机凭据正在被其他操作使用，请稍后重试。') from None
                time.sleep(0.025)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _read_unlocked(path: Path) -> dict[str, str]:
    try:
        with path.open('rb') as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > _MAX_FILE_BYTES:
        raise CredentialStoreError('本机凭据文件大小无效。')
    envelope = json.loads(raw)
    if (not isinstance(envelope, dict) or set(envelope) != {'version', 'payload'}
            or type(envelope['version']) is not int or envelope['version'] != _VERSION
            or not isinstance(envelope['payload'], str) or not envelope['payload']):
        raise CredentialStoreError('本机凭据文件格式或版本无效。')
    encrypted = base64.b64decode(envelope['payload'], validate=True)
    return _validated(json.loads(_unprotect(encrypted)))


def _write_unlocked(path: Path, values: dict[str, str]) -> None:
    encrypted = _protect(json.dumps(values, ensure_ascii=False).encode('utf-8'))
    envelope = {'version': _VERSION, 'payload': base64.b64encode(encrypted).decode('ascii')}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', prefix=path.name + '.', suffix='.tmp',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(envelope, separators=(',', ':')).encode('ascii'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_saved_secrets(path: str | Path | None = None) -> dict[str, str]:
    """Read saved credentials, or return {} if the store does not exist.

    Malformed or undecryptable stores fail explicitly; they are never treated
    as an empty store and thus never silently replaced by a subsequent save.
    """
    _require_windows()
    try:
        target = Path(path) if path is not None else default_path()
        if not target.exists():
            return {}
        with _store_lock(target):
            return _read_unlocked(target)
    except Exception:
        # Raise outside the handler so neither an exception chain nor its
        # context retains parser/codec messages that might contain plaintext.
        pass
    raise CredentialStoreError('无法读取本机加密凭据；文件可能损坏，或不属于当前 Windows 用户。')


def save_secrets(values: dict[str, str], path: str | Path | None = None) -> None:
    """Validate and merge supported keys, then atomically persist ciphertext."""
    _require_windows()
    values = _validated(values)
    try:
        target = Path(path) if path is not None else default_path()
        with _store_lock(target):
            merged = _read_unlocked(target)
            merged.update(values)
            _write_unlocked(target, merged)
        return
    except Exception:
        pass
    raise CredentialStoreError('无法保存本机加密凭据；请检查文件权限、现有凭据和 Windows 用户状态。')
