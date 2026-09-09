"""Static Windows bindings may be reused; identity and authorization may not."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from algo_cli import config


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


class _FakeSecurity:
    def __init__(self):
        self.identity = "S-1-5-21-11-22-33-1000"
        self.owner = self.identity
        self.protected = True
        self.mask = 0
        self.failure = None
        self.fail_conversion_at = 0
        self.loads = []
        self.calls = Counter()
        self.allocations = {}
        self.sid_text = {}
        self.backing = []
        self.tokens = set()
        self.acl_types = set()

    def allocate(self, text, *, owned=True):
        buffer = ctypes.create_unicode_buffer(text)
        address = ctypes.addressof(buffer)
        self.sid_text[address] = text
        if owned:
            self.allocations[address] = buffer
        else:
            self.backing.append(buffer)
        return address

    @staticmethod
    def set_pointer(output, value):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = value

    def load(self, name, *, use_last_error):
        assert use_last_error is True
        if self.failure == "load":
            raise OSError("synthetic missing DLL")
        self.loads.append(name)
        names = (
            "GetCurrentProcess CloseHandle LocalFree" if name == "kernel32" else
            "OpenProcessToken GetTokenInformation ConvertSidToStringSidW GetNamedSecurityInfoW "
            "EqualSid IsValidSid GetLengthSid ConvertStringSidToSidW GetAclInformation GetAce "
            "GetSecurityDescriptorControl"
        ).split()
        if self.failure == "binding" and name == "advapi32":
            names.remove("GetTokenInformation")
        return SimpleNamespace(**{
            method: Mock(side_effect=lambda *args, method=method: self.call(method, *args))
            for method in names
        })

    def call(self, name, *args):
        self.calls[name] += 1
        if self.failure == name:
            return 5 if name == "GetNamedSecurityInfoW" else 0
        if name == "GetCurrentProcess":
            return 100
        if name == "OpenProcessToken":
            token = 1000 + self.calls[name]
            self.tokens.add(token)
            self.set_pointer(args[2], token)
        elif name == "CloseHandle":
            self.tokens.remove(args[0].value)
        elif name == "GetTokenInformation":
            ctypes.cast(args[4], ctypes.POINTER(wintypes.DWORD))[0] = ctypes.sizeof(_TokenUser)
            if args[2] is None:
                return 0
            if self.failure == "token_data":
                return 0
            token = ctypes.cast(args[2], ctypes.POINTER(_TokenUser)).contents
            token.user.sid = self.allocate(self.identity, owned=False)
        elif name == "ConvertSidToStringSidW":
            self.set_pointer(args[1], self.allocate(self.sid_text[args[0]]))
        elif name == "LocalFree":
            self.allocations.pop(args[0].value)
            return None
        elif name == "GetNamedSecurityInfoW":
            self.set_pointer(args[3], self.allocate(self.owner, owned=False))
            self.set_pointer(args[5], 500)
            self.set_pointer(args[7], self.allocate("descriptor"))
            return 0
        elif name == "GetSecurityDescriptorControl":
            ctypes.cast(args[1], ctypes.POINTER(wintypes.WORD))[0] = 0x1000 if self.protected else 0
        elif name == "ConvertStringSidToSidW":
            if self.calls[name] == self.fail_conversion_at:
                return 0
            self.set_pointer(args[1], self.allocate(args[0]))
        elif name == "EqualSid":
            return self.sid_text[args[0].value] == self.sid_text[args[1].value]
        elif name == "GetAclInformation":
            self.acl_types.add(type(args[1]._obj))
            args[1]._obj.ace_count = int(bool(self.mask))
        elif name == "GetAce":
            buffer = ctypes.create_string_buffer(16)
            self.backing.append(buffer)
            address = ctypes.addressof(buffer)
            ctypes.c_ushort.from_address(address + 2).value = 16
            ctypes.c_uint32.from_address(address + 4).value = self.mask
            self.sid_text[address + 8] = "S-1-1-0"
            self.set_pointer(args[2], address)
        elif name == "GetLengthSid":
            return 8
        elif name != "IsValidSid":
            raise AssertionError(name)
        return 1


@pytest.fixture
def native_api(monkeypatch):
    fake = _FakeSecurity()
    factory = getattr(config, "_windows_security_api", None)
    if factory is not None:
        factory.cache_clear()
    monkeypatch.setattr(config, "os", SimpleNamespace(name="nt", fspath=os.fspath))
    monkeypatch.setattr(ctypes, "WinDLL", fake.load, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "synthetic API failure"), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    yield fake
    if factory is not None:
        factory.cache_clear()
    assert fake.allocations == {}
    assert fake.tokens == set()


def test_windows_static_bindings_are_reused_without_reusing_queries(native_api, tmp_path):
    for _ in range(2):
        assert config._windows_private_dacl(tmp_path)
        assert config._windows_current_user_sid_string() == native_api.identity
    assert native_api.calls["GetNamedSecurityInfoW"] == 2
    assert native_api.calls["OpenProcessToken"] == 4
    assert native_api.calls["GetTokenInformation"] == 8
    assert native_api.loads == ["advapi32", "kernel32"]
    assert len(native_api.acl_types) == 1


def test_windows_identity_and_owner_are_fresh_after_warmup(native_api, tmp_path):
    assert config._windows_private_dacl(tmp_path)
    native_api.identity = "S-1-5-21-11-22-33-1001"
    assert config._windows_current_user_sid_string() == native_api.identity
    assert not config._windows_private_dacl(tmp_path)
    native_api.owner = native_api.identity
    assert config._windows_private_dacl(tmp_path)
    native_api.owner = "S-1-5-18"
    assert not config._windows_private_dacl(tmp_path)
    assert config._windows_safe_creation_dacl(tmp_path)


def test_windows_acl_and_validation_mode_are_fresh_after_warmup(native_api, tmp_path):
    assert config._windows_private_dacl(tmp_path)
    native_api.mask = 0x1  # Everyone may read, but cannot change the namespace.
    assert not config._windows_private_dacl(tmp_path)
    assert config._windows_safe_creation_dacl(tmp_path)
    native_api.mask = 0x2  # Creation is unsafe; existing ancestry is still pinned.
    assert not config._windows_safe_creation_dacl(tmp_path)
    assert config._windows_namespace_control_dacl(tmp_path)
    native_api.mask = 0x10000
    assert not config._windows_namespace_control_dacl(tmp_path)
    native_api.mask = 0
    native_api.protected = False
    assert not config._windows_private_dacl(tmp_path)
    assert config._windows_safe_creation_dacl(tmp_path)
    native_api.protected = True
    assert config._windows_private_dacl(tmp_path)


@pytest.mark.parametrize("failure", [
    "OpenProcessToken", "GetTokenInformation", "token_data", "ConvertSidToStringSidW",
    "GetNamedSecurityInfoW", "GetSecurityDescriptorControl", "GetAclInformation", "GetAce", "IsValidSid",
])
def test_windows_warm_api_failures_fail_closed_and_release_resources(native_api, tmp_path, failure):
    assert config._windows_private_dacl(tmp_path)
    native_api.mask = 0x1 if failure in {"GetAce", "IsValidSid"} else 0
    native_api.failure = failure
    assert not config._windows_private_dacl(tmp_path)
    assert native_api.allocations == {} and native_api.tokens == set()
    native_api.failure = None
    native_api.mask = 0
    assert config._windows_private_dacl(tmp_path)


def test_windows_partial_sid_conversion_releases_prior_allocations(native_api, tmp_path):
    native_api.fail_conversion_at = 3
    assert not config._windows_private_dacl(tmp_path)
    assert native_api.allocations == {} and native_api.tokens == set()
    native_api.fail_conversion_at = 0
    assert config._windows_private_dacl(tmp_path)


@pytest.mark.parametrize("failure", ["OpenProcessToken", "GetTokenInformation", "token_data", "ConvertSidToStringSidW"])
def test_windows_identity_failure_after_warmup_never_returns_previous_user(native_api, failure):
    assert config._windows_current_user_sid_string() == native_api.identity
    native_api.failure = failure
    with pytest.raises(OSError):
        config._windows_current_user_sid_string()
    native_api.failure = None
    native_api.identity = "S-1-5-21-11-22-33-1001"
    assert config._windows_current_user_sid_string() == native_api.identity


@pytest.mark.parametrize("failure", ["load", "binding"])
def test_windows_failed_initialization_is_not_cached(native_api, tmp_path, failure):
    native_api.failure = failure
    assert not config._windows_private_dacl(tmp_path)
    with pytest.raises((AttributeError, OSError)):
        config._windows_current_user_sid_string()
    native_api.failure = None
    assert config._windows_private_dacl(tmp_path)
    assert config._windows_current_user_sid_string() == native_api.identity


@pytest.mark.skipif(os.name != "nt", reason="Windows native DACL and ctypes contract")
@pytest.mark.parametrize("cold_start", [False, True], ids=["warm-bindings", "concurrent-initialization"])
def test_native_windows_warm_bindings_observe_acl_changes_and_concurrent_reads(tmp_path: Path, cold_start: bool):
    private = tmp_path / "private"
    private.mkdir()
    config._windows_harden_private_dacl(private)
    assert config._windows_private_dacl(private)
    if cold_start:
        config._windows_security_api.cache_clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(config._windows_private_dacl, [private] * 20))
    bindings = config._windows_security_api()
    icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
    result = subprocess.run(
        [str(icacls), str(private), "/grant", "*S-1-1-0:(OI)(CI)R"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert not config._windows_private_dacl(private)
    assert config._windows_safe_creation_dacl(private)
    config._windows_harden_private_dacl(private)
    assert config._windows_private_dacl(private)
    assert config._windows_security_api() is bindings
