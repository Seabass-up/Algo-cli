"""Algo-owned source identity for the qualified Echo Veil runtime.

This module is deliberately stdlib-only so both the runtime bridge and Henry's
dependency audit can authenticate Echo source bytes before importing Echo.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import stat
from types import ModuleType
from typing import Iterator, NoReturn, Sequence


QUALIFIED_ECHO_SOURCE_PATHS = (
    "echo_veil/__init__.py",
    "echo_veil/_bounded_process.py",
    "echo_veil/_json.py",
    "echo_veil/agent_cli.py",
    "echo_veil/agent_memory.py",
    "echo_veil/agent_preflight.py",
    "echo_veil/agent_security.py",
    "echo_veil/ann.py",
    "echo_veil/archive.py",
    "echo_veil/capability.py",
    "echo_veil/caretaker.py",
    "echo_veil/cloudflare_provider.py",
    "echo_veil/confidence.py",
    "echo_veil/conflict.py",
    "echo_veil/crypto_shield.py",
    "echo_veil/deployment.py",
    "echo_veil/drift.py",
    "echo_veil/guarded_runner.py",
    "echo_veil/memory_layers.py",
    "echo_veil/migration.py",
    "echo_veil/oracle.py",
    "echo_veil/persistence.py",
    "echo_veil/proximity.py",
    "echo_veil/vectors.py",
    "echo_veil/vine.py",
    "echo_veil/workspace.py",
    "echo_veil/zkp.py",
    "echo_veil_origin/__init__.py",
    "echo_veil_origin/_json.py",
    "echo_veil_origin/app.py",
    "echo_veil_origin/core.py",
    "echo_veil_origin/openfhe_engine.py",
    "echo_veil_origin/proof_verifier.py",
)
QUALIFIED_ECHO_SOURCE_TREE_SHA256 = "507fb12670afb7cfeaa7fb17d1aaff232969c4ad8b32099402576758963f6b50"
MAX_QUALIFIED_SOURCE_BYTES = 16 * 1024 * 1024


class QualifiedEchoSourceError(RuntimeError):
    """The installed Echo source tree differs from Algo's qualified commit."""


def _reject(reason: str) -> NoReturn:
    raise QualifiedEchoSourceError(reason)


def _canonical_python_source(payload: bytes) -> bytes:
    """Normalize the CRLF form produced by Windows VCS checkouts."""

    # VCS checkouts on Windows may materialize the qualified LF source with
    # CRLF line endings.  Python normalizes CRLF before tokenization, so
    # authenticate and retain that same canonical stream.  Deliberately do not
    # normalize arbitrary lone CR bytes.  Every other byte remains covered by
    # the pinned tree digest, and the snapshot loader executes only these
    # canonical bytes.
    return payload.replace(b"\r\n", b"\n")


@dataclass(frozen=True)
class QualifiedEchoSourceSnapshot:
    """Immutable source bytes captured from the qualified package tree."""

    distribution_root: Path
    tree_sha256: str
    files: tuple[tuple[str, bytes], ...]

    def payload_for_module(self, fullname: str) -> tuple[str, bytes, bool] | None:
        selected_package = ""
        for package_name in ("echo_veil", "echo_veil_origin"):
            if fullname == package_name:
                relative = f"{package_name}/__init__.py"
                package = True
                selected_package = package_name
                break
            if fullname.startswith(f"{package_name}.") and fullname.count(".") == 1:
                relative = f"{package_name}/{fullname.rsplit('.', 1)[1]}.py"
                package = False
                selected_package = package_name
                break
        if not selected_package:
            return None
        for path, payload in self.files:
            if path == relative:
                return path, payload, package
        return None


class _QualifiedEchoSnapshotLoader(importlib.abc.Loader):
    def __init__(
        self,
        snapshot: QualifiedEchoSourceSnapshot,
        fullname: str,
        relative: str,
        payload: bytes,
        package: bool,
    ) -> None:
        self.snapshot = snapshot
        self.fullname = fullname
        self.relative = relative
        self.payload = payload
        self.package = package

    @property
    def origin(self) -> str:
        return str(self.snapshot.distribution_root / self.relative)

    def create_module(self, _spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        try:
            code = compile(
                self.payload,
                self.origin,
                "exec",
                dont_inherit=True,
            )
        except (SyntaxError, ValueError) as exc:
            raise ImportError("qualified Echo source compilation failed") from exc
        module.__file__ = self.origin
        if self.package:
            module.__path__ = [str(Path(self.origin).parent)]  # type: ignore[attr-defined]
        exec(code, module.__dict__)


class QualifiedEchoSnapshotFinder(importlib.abc.MetaPathFinder):
    """Load every Echo module from the authenticated in-memory snapshot."""

    def __init__(self, snapshot: QualifiedEchoSourceSnapshot) -> None:
        self.snapshot = snapshot

    def owns_module(self, module: ModuleType) -> bool:
        spec = getattr(module, "__spec__", None)
        loader = getattr(spec, "loader", None)
        return isinstance(loader, _QualifiedEchoSnapshotLoader) and loader.snapshot is self.snapshot

    def find_spec(
        self,
        fullname: str,
        _path: Sequence[str] | None = None,
        _target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        managed = fullname in {"echo_veil", "echo_veil_origin"} or fullname.startswith(
            ("echo_veil.", "echo_veil_origin.")
        )
        if not managed:
            return None
        selected = self.snapshot.payload_for_module(fullname)
        if selected is None:
            raise ModuleNotFoundError(
                "Echo module is outside the qualified source snapshot",
                name=fullname,
            )
        relative, payload, package = selected
        loader = _QualifiedEchoSnapshotLoader(
            self.snapshot,
            fullname,
            relative,
            payload,
            package,
        )
        return importlib.util.spec_from_loader(
            fullname,
            loader,
            origin=loader.origin,
            is_package=package,
        )


def _source_identity(information: os.stat_result) -> tuple[int, ...]:
    common = (
        int(information.st_dev),
        int(information.st_ino),
        int(stat.S_IFMT(information.st_mode)),
        int(information.st_nlink),
        int(information.st_size),
        int(information.st_mtime_ns),
    )
    if os.name == "nt":
        return (*common, int(getattr(information, "st_file_attributes", 0)))
    return (*common, int(information.st_mode), int(information.st_ctime_ns))


def _directory_identity(information: os.stat_result) -> tuple[int, ...]:
    common = (
        int(information.st_dev),
        int(information.st_ino),
        int(stat.S_IFMT(information.st_mode)),
    )
    if os.name == "nt":
        return (*common, int(getattr(information, "st_file_attributes", 0)))
    return (*common, int(information.st_mode), int(information.st_ctime_ns))


def _windows_namespace_authorized(path: Path) -> bool:
    """Reject an untrusted owner or ACE able to replace this path edge."""

    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        get_named_security = advapi32.GetNamedSecurityInfoW
        get_named_security.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        get_named_security.restype = wintypes.DWORD
        advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertStringSidToSidW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        advapi32.EqualSid.restype = wintypes.BOOL
        advapi32.IsValidSid.argtypes = (ctypes.c_void_p,)
        advapi32.IsValidSid.restype = wintypes.BOOL
        advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
        advapi32.GetLengthSid.restype = wintypes.DWORD
        advapi32.GetAclInformation.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_int,
        )
        advapi32.GetAclInformation.restype = wintypes.BOOL
        advapi32.GetAce.argtypes = (
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi32.GetAce.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = ()
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel32.LocalFree.restype = ctypes.c_void_p

        class _SidAndAttributes(ctypes.Structure):
            _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

        class _TokenUser(ctypes.Structure):
            _fields_ = [("user", _SidAndAttributes)]

        class _AclSizeInformation(ctypes.Structure):
            _fields_ = [
                ("ace_count", wintypes.DWORD),
                ("acl_bytes_in_use", wintypes.DWORD),
                ("acl_bytes_free", wintypes.DWORD),
            ]

        class _AceHeader(ctypes.Structure):
            _fields_ = [
                ("ace_type", ctypes.c_ubyte),
                ("ace_flags", ctypes.c_ubyte),
                ("ace_size", wintypes.WORD),
            ]

        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        security_descriptor = ctypes.c_void_p()
        result = get_named_security(
            os.fspath(path),
            1,
            0x00000001 | 0x00000004,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if result != 0 or not owner.value or not dacl.value or not security_descriptor.value:
            if security_descriptor.value:
                kernel32.LocalFree(security_descriptor)
            return False
        token = wintypes.HANDLE()
        converted: list[ctypes.c_void_p] = []
        try:
            if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
                return False
            required = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
            if required.value <= 0:
                return False
            token_buffer = ctypes.create_string_buffer(required.value)
            if not advapi32.GetTokenInformation(token, 1, token_buffer, required, ctypes.byref(required)):
                return False
            current_sid = ctypes.cast(token_buffer, ctypes.POINTER(_TokenUser)).contents.user.sid
            trusted = [ctypes.c_void_p(current_sid)]
            for sid_text in (
                "S-1-5-18",
                "S-1-5-32-544",
                "S-1-3-0",
                "S-1-3-4",
                "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
            ):
                sid = ctypes.c_void_p()
                if not advapi32.ConvertStringSidToSidW(sid_text, ctypes.byref(sid)):
                    return False
                converted.append(sid)
                trusted.append(sid)
            if not any(advapi32.EqualSid(owner, sid) for sid in (trusted[0], trusted[1], trusted[2], trusted[-1])):
                return False
            acl_information = _AclSizeInformation()
            if not advapi32.GetAclInformation(
                dacl,
                ctypes.byref(acl_information),
                ctypes.sizeof(acl_information),
                2,
            ):
                return False
            unsafe = 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000
            for index in range(int(acl_information.ace_count)):
                ace_pointer = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)) or not ace_pointer.value:
                    return False
                header = ctypes.cast(ace_pointer, ctypes.POINTER(_AceHeader)).contents
                if int(header.ace_size) < 8:
                    return False
                if int(header.ace_flags) & 0x08 or int(header.ace_type) not in {0, 4, 5, 9, 11}:
                    continue
                mask = int(ctypes.c_uint32.from_address(ace_pointer.value + 4).value)
                if not mask & unsafe:
                    continue
                if int(header.ace_type) not in {0, 9} or int(header.ace_size) < 12:
                    return False
                sid = ctypes.c_void_p(ace_pointer.value + 8)
                if not advapi32.IsValidSid(sid):
                    return False
                sid_length = int(advapi32.GetLengthSid(sid))
                if sid_length <= 0 or 8 + sid_length > int(header.ace_size):
                    return False
                if not any(advapi32.EqualSid(sid, trusted_sid) for trusted_sid in trusted):
                    return False
            return True
        finally:
            for sid in converted:
                kernel32.LocalFree(sid)
            if token:
                kernel32.CloseHandle(token)
            kernel32.LocalFree(security_descriptor)
    except Exception:
        return False


def _windows_native_final_path(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_final_path.restype = wintypes.DWORD
    required = int(get_final_path(wintypes.HANDLE(handle), None, 0, 0))
    if required <= 0 or required > 32_768:
        _reject("qualified_source_handle_path")
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = int(get_final_path(wintypes.HANDLE(handle), buffer, len(buffer), 0))
    if written <= 0 or written >= len(buffer):
        _reject("qualified_source_handle_path")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


@contextmanager
def _windows_pinned_directory_chain(
    path: Path,
) -> Iterator[tuple[tuple[Path, tuple[int, ...]], ...]]:
    import ctypes
    from ctypes import wintypes

    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.anchor or absolute.anchor.startswith("\\\\"):
        _reject("qualified_source_root_identity")
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handles: list[int] = []
    captured: list[tuple[Path, tuple[int, ...]]] = []
    current = Path(absolute.anchor)
    try:
        for part in (None, *absolute.parts[1:]):
            if part is not None:
                current /= part
            opened = create_file(
                os.fspath(current),
                0x00000080,  # FILE_READ_ATTRIBUTES; source trust comes from the digest
                0x00000001 | 0x00000002,
                None,
                3,
                0x02000000 | 0x00200000,
                None,
            )
            if opened in {None, ctypes.c_void_p(-1).value}:
                _reject("qualified_source_root_identity")
            handle = int(opened)
            handles.append(handle)
            try:
                information = current.lstat()
                final_path = _windows_native_final_path(handle)
                final_information = final_path.lstat()
            except OSError:
                _reject("qualified_source_root_identity")
            # Every lexical component remains open without SHARE_DELETE until
            # capture completes, so a broad hosted-workspace ACL cannot rebind
            # the source path.  Unlike mutable storage, content trust here is
            # supplied by the exact qualified tree digest; an in-place writer
            # can at most cause a mismatch/denial, never an accepted mutation.
            if (
                _is_reparse(information)
                or _is_reparse(final_information)
                or not stat.S_ISDIR(information.st_mode)
                or _directory_identity(information) != _directory_identity(final_information)
                or not os.path.samefile(current, final_path)
            ):
                _reject("qualified_source_root_identity")
            captured.append((current, _directory_identity(information)))
        result = tuple(captured)
        yield result
        _recheck_windows_directory_chain(result)
    finally:
        for handle in reversed(handles):
            close_handle(wintypes.HANDLE(handle))


def _windows_directory_chain(path: Path) -> tuple[tuple[Path, tuple[int, ...]], ...]:
    if os.name == "nt":
        with _windows_pinned_directory_chain(path) as pinned:
            return pinned
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    captured_entries: list[tuple[Path, tuple[int, ...]]] = []
    if not current.anchor:
        _reject("qualified_source_root_identity")
    for part in (None, *absolute.parts[1:]):
        if part is not None:
            current /= part
        try:
            information = current.lstat()
        except OSError:
            _reject("qualified_source_root_identity")
        if _is_reparse(information) or not stat.S_ISDIR(information.st_mode):
            _reject("qualified_source_root_identity")
        captured_entries.append((current, _directory_identity(information)))
    return tuple(captured_entries)


def _recheck_windows_directory_chain(
    captured: tuple[tuple[Path, tuple[int, ...]], ...],
) -> None:
    for path, expected in captured:
        try:
            information = path.lstat()
        except OSError:
            _reject("qualified_source_root_changed")
        if (
            _is_reparse(information)
            or not stat.S_ISDIR(information.st_mode)
            or _directory_identity(information) != expected
        ):
            _reject("qualified_source_root_changed")


def _is_reparse(information: os.stat_result) -> bool:
    return stat.S_ISLNK(information.st_mode) or (
        os.name == "nt"
        and bool(
            int(getattr(information, "st_file_attributes", 0))
            & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        )
    )


def _windows_final_path(descriptor: int) -> Path | None:
    if os.name != "nt":
        return None
    try:
        import msvcrt

        return _windows_native_final_path(int(getattr(msvcrt, "get_osfhandle")(descriptor)))
    except QualifiedEchoSourceError:
        raise
    except (AttributeError, OSError, TypeError, ValueError):
        _reject("qualified_source_handle_path")


def _read_qualified_source_by_path(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        _reject("qualified_source_open")
    if (
        _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 <= before.st_size <= MAX_QUALIFIED_SOURCE_BYTES
    ):
        _reject("qualified_source_identity")
    file_flags = os.O_RDONLY
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        file_flags |= int(getattr(os, name, 0))
    descriptor: int | None = None
    try:
        descriptor = os.open(path, file_flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _source_identity(opened) != _source_identity(before):
            _reject("qualified_source_open_changed")
        final_path = _windows_final_path(descriptor)
        if final_path is not None:
            try:
                final_info = final_path.lstat()
                same_file = os.path.samefile(final_path, path)
            except OSError:
                _reject("qualified_source_handle_path")
            if _is_reparse(final_info) or _source_identity(final_info) != _source_identity(opened) or not same_file:
                _reject("qualified_source_handle_path")
        payload = bytearray()
        while chunk := os.read(
            descriptor,
            min(128 * 1024, MAX_QUALIFIED_SOURCE_BYTES + 1 - len(payload)),
        ):
            payload.extend(chunk)
            if len(payload) > MAX_QUALIFIED_SOURCE_BYTES:
                _reject("qualified_source_bounds")
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            _is_reparse(current)
            or _source_identity(after) != _source_identity(opened)
            or _source_identity(current) != _source_identity(opened)
            or len(payload) != opened.st_size
        ):
            _reject("qualified_source_changed")
        return bytes(payload)
    except QualifiedEchoSourceError:
        raise
    except OSError:
        _reject("qualified_source_open")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _capture_qualified_echo_source_tree_by_path(
    root: Path,
    root_info: os.stat_result,
) -> QualifiedEchoSourceSnapshot:
    """Capture on Windows, whose CRT cannot open directories as descriptors."""

    with _windows_pinned_directory_chain(root) as ancestry:
        return _capture_qualified_echo_source_tree_by_bound_path(root, root_info, ancestry=ancestry)


def _capture_qualified_echo_source_tree_by_bound_path(
    root: Path,
    root_info: os.stat_result,
    *,
    ancestry: tuple[tuple[Path, tuple[int, ...]], ...],
) -> QualifiedEchoSourceSnapshot:
    root_identity = _directory_identity(root_info)
    digest = hashlib.sha256()
    total = 0
    captured: list[tuple[str, bytes]] = []
    for package_name in ("echo_veil", "echo_veil_origin"):
        package_path = root / package_name
        with _windows_pinned_directory_chain(package_path) as package_ancestry:
            try:
                package_info = package_path.lstat()
            except OSError:
                _reject("qualified_source_package_open")
            if _is_reparse(package_info) or not stat.S_ISDIR(package_info.st_mode):
                _reject("qualified_source_package_identity")
            package_identity = _directory_identity(package_info)
            expected_names = {
                Path(path).name for path in QUALIFIED_ECHO_SOURCE_PATHS if path.startswith(f"{package_name}/")
            }
            try:
                entries = list(os.scandir(package_path))
            except OSError:
                _reject("qualified_source_scan")
            actual_sources = {
                entry.name for entry in entries if entry.name.endswith(".py") and entry.is_file(follow_symlinks=False)
            }
            if actual_sources != expected_names:
                _reject("qualified_source_set")
            for relative in (path for path in QUALIFIED_ECHO_SOURCE_PATHS if path.startswith(f"{package_name}/")):
                _recheck_windows_directory_chain(package_ancestry)
                raw_payload = _read_qualified_source_by_path(root / relative)
                total += len(raw_payload)
                if total > MAX_QUALIFIED_SOURCE_BYTES:
                    _reject("qualified_source_bounds")
                payload = _canonical_python_source(raw_payload)
                path_bytes = relative.encode("utf-8")
                digest.update(path_bytes)
                digest.update(b"\0")
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
                captured.append((relative, payload))
            try:
                current_package = package_path.lstat()
            except OSError:
                _reject("qualified_source_package_changed")
            if _is_reparse(current_package) or _directory_identity(current_package) != package_identity:
                _reject("qualified_source_package_changed")
            _recheck_windows_directory_chain(package_ancestry)
            _recheck_windows_directory_chain(ancestry)
    try:
        current_root = root.lstat()
    except OSError:
        _reject("qualified_source_root_changed")
    if _is_reparse(current_root) or _directory_identity(current_root) != root_identity:
        _reject("qualified_source_root_changed")
    _recheck_windows_directory_chain(ancestry)
    value = digest.hexdigest()
    if value != QUALIFIED_ECHO_SOURCE_TREE_SHA256:
        _reject("qualified_source_digest")
    return QualifiedEchoSourceSnapshot(
        distribution_root=root,
        tree_sha256=value,
        files=tuple(captured),
    )


def capture_qualified_echo_source_tree(distribution_root: Path) -> QualifiedEchoSourceSnapshot:
    """Capture exact source bytes through a pinned no-follow dirfd."""

    root = Path(distribution_root)
    try:
        root_info = root.lstat()
    except OSError:
        _reject("qualified_source_missing")
    if not stat.S_ISDIR(root_info.st_mode) or _is_reparse(root_info):
        _reject("qualified_source_root_identity")
    if os.name == "nt":
        return _capture_qualified_echo_source_tree_by_path(root, root_info)
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW"):
        flags |= int(getattr(os, name, 0))
    try:
        root_fd = os.open(root, flags)
    except OSError:
        _reject("qualified_source_root_open")
    try:
        pinned = os.fstat(root_fd)
        if not stat.S_ISDIR(pinned.st_mode) or (pinned.st_dev, pinned.st_ino) != (root_info.st_dev, root_info.st_ino):
            _reject("qualified_source_root_changed")
        digest = hashlib.sha256()
        total = 0
        captured: list[tuple[str, bytes]] = []
        file_flags = os.O_RDONLY
        for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
            file_flags |= int(getattr(os, name, 0))
        directory_flags = flags
        for package_name in ("echo_veil", "echo_veil_origin"):
            try:
                package_fd = os.open(package_name, directory_flags, dir_fd=root_fd)
            except OSError:
                _reject("qualified_source_package_open")
            try:
                package_info = os.fstat(package_fd)
                if not stat.S_ISDIR(package_info.st_mode):
                    _reject("qualified_source_package_identity")
                expected_names = {
                    Path(path).name for path in QUALIFIED_ECHO_SOURCE_PATHS if path.startswith(f"{package_name}/")
                }
                try:
                    entries = list(os.scandir(package_fd))
                except OSError:
                    _reject("qualified_source_scan")
                actual_sources = {entry.name for entry in entries if entry.name.endswith(".py")}
                if actual_sources != expected_names:
                    _reject("qualified_source_set")
                for relative in (path for path in QUALIFIED_ECHO_SOURCE_PATHS if path.startswith(f"{package_name}/")):
                    name = Path(relative).name
                    try:
                        descriptor = os.open(name, file_flags, dir_fd=package_fd)
                    except OSError:
                        _reject("qualified_source_open")
                    try:
                        before = os.fstat(descriptor)
                        if (
                            not stat.S_ISREG(before.st_mode)
                            or before.st_nlink != 1
                            or not 0 <= before.st_size <= MAX_QUALIFIED_SOURCE_BYTES
                        ):
                            _reject("qualified_source_identity")
                        payload = bytearray()
                        while chunk := os.read(
                            descriptor,
                            min(
                                128 * 1024,
                                MAX_QUALIFIED_SOURCE_BYTES + 1 - len(payload),
                            ),
                        ):
                            payload.extend(chunk)
                            if len(payload) > MAX_QUALIFIED_SOURCE_BYTES:
                                _reject("qualified_source_bounds")
                        after = os.fstat(descriptor)
                        stable = (
                            "st_dev",
                            "st_ino",
                            "st_mode",
                            "st_nlink",
                            "st_size",
                            "st_mtime_ns",
                            "st_ctime_ns",
                        )
                        if (
                            any(getattr(before, field) != getattr(after, field) for field in stable)
                            or len(payload) != before.st_size
                        ):
                            _reject("qualified_source_changed")
                    finally:
                        os.close(descriptor)
                    total += len(payload)
                    if total > MAX_QUALIFIED_SOURCE_BYTES:
                        _reject("qualified_source_bounds")
                    canonical_payload = _canonical_python_source(bytes(payload))
                    path_bytes = relative.encode("utf-8")
                    digest.update(path_bytes)
                    digest.update(b"\0")
                    digest.update(len(canonical_payload).to_bytes(8, "big"))
                    digest.update(canonical_payload)
                    captured.append((relative, canonical_payload))
                current_package = os.fstat(package_fd)
                if (current_package.st_dev, current_package.st_ino) != (
                    package_info.st_dev,
                    package_info.st_ino,
                ):
                    _reject("qualified_source_package_changed")
            finally:
                os.close(package_fd)
        current = os.fstat(root_fd)
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            _reject("qualified_source_root_changed")
        value = digest.hexdigest()
        if value != QUALIFIED_ECHO_SOURCE_TREE_SHA256:
            _reject("qualified_source_digest")
        return QualifiedEchoSourceSnapshot(
            distribution_root=root,
            tree_sha256=value,
            files=tuple(captured),
        )
    finally:
        os.close(root_fd)


def verify_qualified_echo_source_tree(distribution_root: Path) -> str:
    """Verify the source tree and return its Algo-owned canonical digest."""

    return capture_qualified_echo_source_tree(distribution_root).tree_sha256


__all__ = [
    "QUALIFIED_ECHO_SOURCE_PATHS",
    "QUALIFIED_ECHO_SOURCE_TREE_SHA256",
    "QualifiedEchoSnapshotFinder",
    "QualifiedEchoSourceError",
    "QualifiedEchoSourceSnapshot",
    "capture_qualified_echo_source_tree",
    "verify_qualified_echo_source_tree",
]
