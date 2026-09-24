"""Rotating config backups, the pytest real-home write guard, and config restore."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from algo_cli import cli_config, config
from algo_cli.config import Config


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX permission and symlink semantics")


def _backups() -> list[Path]:
    return sorted(path for path in config.CONFIG_DIR.iterdir() if config._CONFIG_BACKUP_RE.fullmatch(path.name))


def _save(model: str) -> bytes:
    cfg = Config()
    cfg.model = model
    cfg.save()
    return config.CONFIG_FILE.read_bytes()


def _point_config_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", root)
    monkeypatch.setattr(config, "CONFIG_FILE", root / "config.json")
    monkeypatch.setattr(config, "MEMORY_FILE", root / "memory.json")
    monkeypatch.setattr(config, "HISTORY_DIR", root / "saves")


def test_save_retains_previous_version_as_private_timestamped_backup() -> None:
    first = _save("model-a")
    assert _backups() == []

    _save("model-b")

    backups = _backups()
    assert len(backups) == 1
    assert backups[0].read_bytes() == first
    assert stat.S_IMODE(backups[0].lstat().st_mode) == 0o600
    created = config._config_backup_time(backups[0].name)
    assert created is not None and abs((datetime.now(timezone.utc) - created).total_seconds()) < 60


def test_unchanged_save_does_not_create_backup() -> None:
    _save("same")
    _save("same")
    assert _backups() == []


def test_backups_are_deduplicated_by_content() -> None:
    a = _save("model-a")
    b = _save("model-b")  # backs up a
    _save("model-a")  # backs up b
    _save("model-b")  # current a already retained

    contents = sorted(path.read_bytes() for path in _backups())
    assert contents == sorted([a, b])


def test_rotation_keeps_only_newest_versions() -> None:
    versions = [_save(f"model-{index}") for index in range(config.CONFIG_BACKUP_KEEP + 4)]

    backups = _backups()
    assert len(backups) == config.CONFIG_BACKUP_KEEP
    assert [path.read_bytes() for path in backups] == versions[-config.CONFIG_BACKUP_KEEP - 1 : -1]


def test_backup_names_stay_monotonic_when_clock_is_frozen_or_backwards(monkeypatch) -> None:
    frozen = datetime(2030, 1, 1, tzinfo=timezone.utc)

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    _save("model-0")
    monkeypatch.setattr(config, "datetime", FrozenClock)
    versions = [_save(f"model-{index}") for index in range(1, 4)]
    frozen = datetime(2001, 1, 1, tzinfo=timezone.utc)
    versions.append(_save("model-4"))

    backups = _backups()
    assert len(backups) == 4 and len({path.name for path in backups}) == 4
    # Name order equals write order, so rotation always drops the oldest.
    assert [path.read_bytes() for path in backups][1:] == versions[:-1]


def test_symlinked_backup_entries_are_never_read_counted_or_removed(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"outside": true}')
    _save("model-0")
    link = config.CONFIG_DIR / "config.json.bak.20000101T000000000000Z"
    link.symlink_to(outside)

    for index in range(1, config.CONFIG_BACKUP_KEEP + 3):
        _save(f"model-{index}")

    assert link.is_symlink() and outside.read_text() == '{"outside": true}'
    assert len(_backups()) == config.CONFIG_BACKUP_KEEP + 1  # five regular backups plus the ignored link
    assert link.name not in {item["name"] for item in config.list_config_backups()}
    with pytest.raises(OSError):
        config.restore_config_backup(link.name)
    assert outside.read_text() == '{"outside": true}'


def test_symlinked_config_file_is_refused_without_following(tmp_path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.symlink_to(outside)

    with pytest.raises(OSError):
        Config().save()

    assert outside.read_text() == "{}"
    assert _backups() == []


def test_memory_repair_also_rotates_a_backup() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    original = b'{"d057_enabled": true, "model": "keep"}'
    config.CONFIG_FILE.write_bytes(original)

    result = config.repair_memory_configuration()

    assert result["changed"] is True
    assert [path.read_bytes() for path in _backups()] == [original]


def _clear_session_like_slash_clear(cfg: Config) -> None:
    cfg.session_summary = ""
    cfg.attempt_ledger.clear()
    cfg.save()


def test_cleared_session_summary_is_not_kept_in_any_backup() -> None:
    secret = "SECRET-SUMMARY-TEXT"
    cfg = Config.load()
    cfg.continuum_enabled = False
    cfg.model = "model-a"
    cfg.save()
    cfg.session_summary = secret
    cfg.save()
    assert secret in config.CONFIG_FILE.read_text()

    _clear_session_like_slash_clear(cfg)

    assert secret not in config.CONFIG_FILE.read_text()
    assert _backups(), "settings history should still be retained"
    for backup in _backups():
        assert secret.encode() not in backup.read_bytes()
    for backup in _backups():
        config.restore_config_backup(backup.name)
        assert Config.load().session_summary == ""
        assert Config.load().model == "model-a"


def test_backup_blanks_session_state_but_keeps_settings() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ledger_entry = {"tool": "run_shell", "summary": "LEDGER-SECRET"}
    config.CONFIG_FILE.write_text(
        json.dumps(
            {"model": "keep-me", "session_summary": "SUMMARY-SECRET", "attempt_ledger": [ledger_entry]}, indent=2
        )
    )

    _save("model-b")

    [backup] = _backups()
    assert json.loads(backup.read_text()) == {"model": "keep-me", "session_summary": "", "attempt_ledger": []}
    assert stat.S_IMODE(backup.lstat().st_mode) == 0o600


def test_summary_only_changes_share_one_settings_backup() -> None:
    cfg = Config()
    cfg.continuum_enabled = False
    cfg.save()
    for index in range(3):
        cfg.session_summary = f"summary {index}"
        cfg.save()

    assert len(_backups()) == 1
    assert b"summary " not in _backups()[0].read_bytes()


def test_unparseable_config_is_backed_up_verbatim() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    corrupt = b'{"model": "half-written", "session_summary": "x"'
    config.CONFIG_FILE.write_bytes(corrupt)

    _save("model-b")

    assert [path.read_bytes() for path in _backups()] == [corrupt]


# --- pytest real-home guard -------------------------------------------------


def test_conftest_isolation_is_not_the_real_home_and_writes_normally() -> None:
    assert config._running_under_pytest()
    real_roots = config._real_home_config_dirs()
    assert real_roots
    resolved = Path(os.path.realpath(config.CONFIG_DIR))
    assert all(resolved != root and root not in resolved.parents for root in real_roots)
    config._refuse_real_config_write_under_test(config.CONFIG_FILE)
    _save("isolated")
    assert config.CONFIG_FILE.exists()


@pytest.mark.parametrize("dirname", [".algo_cli", ".ollama_cli"])
def test_guard_refuses_config_writers_in_real_home_under_pytest(monkeypatch, tmp_path, dirname) -> None:
    home = tmp_path / "account-home"
    home.mkdir()
    monkeypatch.setattr(config, "_account_home_directory", lambda: home)
    real = home / dirname
    _point_config_at(monkeypatch, real)

    with pytest.raises(config.ConfigWriteUnderTestError, match="ALGO_CLI_CONFIG_DIR"):
        Config().save()
    with pytest.raises(config.ConfigWriteUnderTestError):
        config.repair_memory_configuration()
    with pytest.raises(config.ConfigWriteUnderTestError):
        config._atomic_write_text(real / "memory.json", "[]")
    with pytest.raises(config.ConfigWriteUnderTestError):
        config._write_private_migration_file(real, "config.json", b"{}")
    assert not real.exists()


def test_guard_sees_through_symlinked_alias_of_real_home(monkeypatch, tmp_path) -> None:
    home = tmp_path / "account-home"
    (home / ".algo_cli").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(home / ".algo_cli", target_is_directory=True)
    monkeypatch.setattr(config, "_account_home_directory", lambda: home)
    _point_config_at(monkeypatch, alias)

    with pytest.raises(config.ConfigWriteUnderTestError):
        Config().save()
    assert list((home / ".algo_cli").iterdir()) == []


def test_guard_ignores_home_env_redirection_to_an_isolated_dir(monkeypatch, tmp_path) -> None:
    # A test that points HOME at tmp_path is isolated; only the account home counts.
    monkeypatch.setenv("HOME", str(tmp_path))
    _point_config_at(monkeypatch, tmp_path / ".algo_cli")

    _save("isolated-home")

    assert (tmp_path / ".algo_cli" / "config.json").exists()


def test_guard_is_inactive_outside_pytest(monkeypatch, tmp_path) -> None:
    home = tmp_path / "account-home"
    home.mkdir()
    monkeypatch.setattr(config, "_account_home_directory", lambda: home)
    _point_config_at(monkeypatch, home / ".algo_cli")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delitem(sys.modules, "pytest")

    assert not config._running_under_pytest()
    _save("normal-runtime")
    assert (home / ".algo_cli" / "config.json").exists()


def test_guard_detects_pytest_by_environment_alone(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::test (call)")
    assert config._running_under_pytest()


# --- restore -----------------------------------------------------------------


def test_restore_round_trip_backs_up_current_file_first() -> None:
    first = _save("model-a")
    second = _save("model-b")
    [backup] = _backups()

    result = config.restore_config_backup(backup.name)

    assert config.CONFIG_FILE.read_bytes() == first
    assert stat.S_IMODE(config.CONFIG_FILE.lstat().st_mode) == 0o600
    assert result["previous_backup"].read_bytes() == second
    assert Config.load().model == "model-a"
    # Restoring back is a clean round trip.
    config.restore_config_backup(result["previous_backup"].name)
    assert config.CONFIG_FILE.read_bytes() == second


def test_restore_rejects_paths_unknown_names_and_invalid_payloads() -> None:
    before = _save("model-a")
    for name in ("../config.json", "config.json", "/etc/passwd", "config.json.bak.nope"):
        with pytest.raises(ValueError):
            config.restore_config_backup(name)
    with pytest.raises(FileNotFoundError):
        config.restore_config_backup("config.json.bak.20200101T000000000000Z")
    bad = config.CONFIG_DIR / "config.json.bak.20200101T000000000000Z"
    bad.write_text("[1, 2]")
    with pytest.raises(ValueError):
        config.restore_config_backup(bad.name)
    assert config.CONFIG_FILE.read_bytes() == before


def test_list_includes_memory_repair_backups_newest_first() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text('{"d057_enabled": true}')
    config.repair_memory_configuration()
    _save("later")

    listed = config.list_config_backups()

    kinds = {item["kind"] for item in listed}
    assert kinds == {"rotating", "memory-repair"}
    assert [item["created"] for item in listed] == sorted((item["created"] for item in listed), reverse=True)
    assert all(item["size"] == item["path"].lstat().st_size for item in listed)


def test_cli_restore_list_and_restore(monkeypatch) -> None:
    first = _save("model-a")
    _save("model-b")
    [backup] = _backups()

    with cli_config.console.capture() as captured:
        assert cli_config.run(["restore", "--list"], interactive=False) == 0
    listing = captured.get()
    assert backup.name in listing and f"{len(first)} bytes" in listing and "UTC" in listing

    with cli_config.console.capture() as captured:
        assert cli_config.run(["restore", backup.name], interactive=False) == 0
    assert "Restored config.json" in captured.get()
    assert config.CONFIG_FILE.read_bytes() == first

    with cli_config.console.capture():
        assert cli_config.run(["restore", "../escape"], interactive=False) == 2
        assert cli_config.run(["restore", "config.json.bak.20200101T000000000000Z"], interactive=False) == 1
        assert cli_config.run(["restore", "--list", backup.name], interactive=False) == 2


def test_cli_restore_without_backups_reports_none() -> None:
    with cli_config.console.capture() as captured:
        assert cli_config.run(["restore"], interactive=False) == 0
    assert "No config backups" in captured.get()


def test_repl_config_restore_lists_but_refuses_live_restore(monkeypatch) -> None:
    from algo_cli import main

    calls: list[list[str]] = []
    errors: list[str] = []
    monkeypatch.setattr(cli_config, "run", lambda argv: calls.append(list(argv)) or 0)
    monkeypatch.setattr(main, "show_error", errors.append)

    main.run_config_command("restore --list")
    main.run_config_command("restore config.json.bak.20200101T000000000000Z")

    assert calls == [["restore", "--list"]]
    assert errors and "algo-cli config restore NAME" in errors[0]


def test_restoring_memory_repair_backup_does_not_resurrect_cleared_session_state() -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ledger_entry = {"tool": "run_shell", "summary": "LEDGER-SECRET"}
    config.CONFIG_FILE.write_text(
        json.dumps(
            {
                "d057_enabled": True,
                "model": "keep-me",
                "session_summary": "SUMMARY-SECRET",
                "attempt_ledger": [ledger_entry],
            },
            indent=2,
        )
    )
    repair_backup = config.repair_memory_configuration()["backup_path"]
    current = json.loads(config.CONFIG_FILE.read_text())
    current.update(session_summary="", attempt_ledger=[])
    config.CONFIG_FILE.write_text(json.dumps(current, indent=2))

    result = config.restore_config_backup(repair_backup.name)

    restored = config.CONFIG_FILE.read_bytes()
    assert b"SUMMARY-SECRET" not in restored and b"LEDGER-SECRET" not in restored
    assert json.loads(restored) == {
        "d057_enabled": True,
        "model": "keep-me",
        "session_summary": "",
        "attempt_ledger": [],
    }
    assert result["size"] == len(restored)
    assert result["sha256"] == hashlib.sha256(restored).hexdigest()


INVALID_STAMP_BACKUP = "config.json.bak.99999999T999999999999Z"


def test_backup_name_with_unparsable_timestamp_is_not_a_backup() -> None:
    _save("model-a")
    bogus = config.CONFIG_DIR / INVALID_STAMP_BACKUP
    bogus.write_text('{"model": "bogus"}')
    assert config._config_backup_time(bogus.name) is None

    assert bogus.name not in {item["name"] for item in config.list_config_backups()}
    for index in range(config.CONFIG_BACKUP_KEEP + 2):
        _save(f"model-{index}")
    assert Config.load().model == f"model-{config.CONFIG_BACKUP_KEEP + 1}"
    assert bogus.read_text() == '{"model": "bogus"}'
    assert bogus not in config._rotating_config_backups()
    with pytest.raises(ValueError):
        config.restore_config_backup(bogus.name)
    with cli_config.console.capture() as captured:
        assert cli_config.run(["restore", "--list"], interactive=False) == 0
    assert bogus.name not in captured.get()


def test_backup_name_at_the_end_of_time_does_not_block_saves() -> None:
    _save("model-a")
    far = config.CONFIG_DIR / f"{config.CONFIG_FILE.name}.bak.99991231T235959999999Z"
    far.write_text('{"model": "far"}')
    assert config._config_backup_time(far.name) is None

    for index in range(3):
        _save(f"model-{index}")
    assert Config.load().model == "model-2"
    assert far.read_text() == '{"model": "far"}'
    assert far not in config._rotating_config_backups()
