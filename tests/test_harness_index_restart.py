"""Protected startup must retain large public indexes without widening auth reads."""

import json
import os

import pytest

from algo_cli import config, harness


@pytest.mark.parametrize("operation", ["protect", "quarantine_skills"])
def test_large_valid_index_survives_protected_startup(operation):
    record = harness._runtime_capability_records()[0]
    record["embedding"] = [0.25, 0.75]
    record["embedding_model"] = "test-model"
    payload = {"records": [record], "record_count": 1, "padding": "x" * config.MAX_JSON_STATE_BYTES}
    harness.INDEX_PATH.write_text(json.dumps(payload))
    assert harness.INDEX_PATH.stat().st_size > config.MAX_JSON_STATE_BYTES
    if operation == "protect":
        harness.configure_protected_memory_authority(True)
    else:
        harness.invalidate_user_skill_records()
    assert harness.INDEX_PATH.exists()
    saved = json.loads(harness.INDEX_PATH.read_text())
    assert saved["records"][0]["embedding"] == [0.25, 0.75]


def test_larger_harness_bound_is_not_available_to_config_or_auth():
    for target in [config.CONFIG_FILE, config.CONFIG_DIR / "auth.json"]:
        target.write_text('{"safe":true}')
        with pytest.raises(OSError):
            config._harness_index_descriptor_payload(target)
        with pytest.raises(OSError):
            config._state_descriptor_payload(target, max_bytes=config.MAX_JSON_STATE_BYTES + 1)


@pytest.mark.skipif(os.name == "nt", reason="POSIX link guard; Windows reparse guards are covered in test_config")
def test_large_index_reader_keeps_symlink_and_hardlink_guards(tmp_path):
    target = tmp_path / "outside.json"
    target.write_text('{"records":[]}')
    harness.INDEX_PATH.symlink_to(target)
    with pytest.raises(OSError):
        config._harness_index_descriptor_payload(harness.INDEX_PATH)
    harness.INDEX_PATH.unlink()
    os.link(target, harness.INDEX_PATH)
    with pytest.raises(OSError):
        config._harness_index_descriptor_payload(harness.INDEX_PATH)


def test_large_index_reader_still_has_an_explicit_bound():
    with harness.INDEX_PATH.open("wb") as handle:
        handle.truncate(config.MAX_HARNESS_INDEX_BYTES + 1)
    with pytest.raises(OSError):
        config._harness_index_descriptor_payload(harness.INDEX_PATH)
