from pathlib import Path

import pytest

from rvcw.lights import LightEntry, LightTarget, load_map, save_combo, save_light


def write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "lights.toml"
    p.write_text(content)
    return p


def test_light_target_key_is_the_table_device_pair():
    t = LightTarget(0x01, 0x2E)
    assert t.key == (0x01, 0x2E)


def test_light_target_is_frozen():
    t = LightTarget(0x01, 0x2E)
    with pytest.raises(AttributeError):
        t.device_id = 0x2F  # type: ignore[misc]


def test_load_and_resolve_light(tmp_path):
    p = write(tmp_path, "[lights.awning]\ntable = 0x01\ndevice = 0x2E\n")
    m = load_map(p)
    assert m.resolve("awning") == [LightEntry("awning", 0x01, 0x2E)]


def test_removed_device_id_key_is_rejected_at_load(tmp_path):
    """Review Focus 4: NO silent remap of the pre-COBS mapping — load fails
    with the migration message naming the light."""
    p = write(tmp_path, "[lights.awning]\ndevice_id = 0x2E41\n")
    with pytest.raises(
        ValueError,
        match="light 'awning' uses the removed device_id key — re-run learn for the COBS path",
    ):
        load_map(p)


def test_removed_flat_scalar_entry_is_rejected_at_load(tmp_path):
    p = write(tmp_path, "[lights]\nawning = 0x2E41\n")
    with pytest.raises(ValueError, match="removed device_id key"):
        load_map(p)


def test_device_id_only_table_entry_is_rejected(tmp_path):
    # a [lights.NAME] table carrying device_id (the old sub-table shape)
    p = write(tmp_path, "[lights.awning]\ndevice_id = 0x2E\n")
    with pytest.raises(ValueError, match="removed device_id key"):
        load_map(p)


def test_light_with_only_table_rejected(tmp_path):
    p = write(tmp_path, "[lights.awning]\ntable = 0x01\n")
    with pytest.raises(ValueError, match="awning"):
        load_map(p)


def test_missing_file_is_empty(tmp_path):
    m = load_map(tmp_path / "nope.toml")
    with pytest.raises(KeyError, match="awning"):
        m.resolve("awning")


def test_combo_resolution(tmp_path):
    p = write(
        tmp_path,
        "[lights.awning]\ntable = 0x01\ndevice = 0x2E\n"
        "[lights.spots]\ntable = 0x01\ndevice = 0x2F\n"
        "[combos.night_mode]\nmembers = [\"awning\", \"spots\"]\n",
    )
    m = load_map(p)
    assert [e.device_id for e in m.resolve("night_mode")] == [0x2E, 0x2F]
    assert [e.table_id for e in m.resolve("night_mode")] == [0x01, 0x01]


def test_combo_missing_member_raises(tmp_path):
    p = write(tmp_path, "[combos.bad]\nmembers = [\"ghost\"]\n")
    m = load_map(p)
    with pytest.raises(KeyError, match="ghost"):
        m.resolve("bad")


def test_save_light_roundtrip(tmp_path):
    p = save_light(tmp_path / "lights.toml", "awning", 0x01, 0x2E)
    reloaded = load_map(p)
    entry = reloaded.resolve("awning")[0]
    assert (entry.table_id, entry.device_id) == (0x01, 0x2E)


def test_saved_toml_uses_table_and_device_keys(tmp_path):
    p = save_light(tmp_path / "lights.toml", "awning", 0x02, 0x2E)
    text = (tmp_path / "lights.toml").read_text()
    assert "[lights.awning]" in text
    assert "table = 0x02" in text
    assert "device = 0x002E" in text


def test_save_light_accepts_a_loaded_map(tmp_path):
    p = save_light(tmp_path / "lights.toml", "awning", 0x01, 0x2E)
    save_light(p, "spots", 0x01, 0x2F)
    m = load_map(p)
    assert m.resolve("spots")[0].device_id == 0x2F


def test_save_combo_roundtrip(tmp_path):
    p = save_light(tmp_path / "lights.toml", "awning", 0x01, 0x2E)
    p = save_light(p, "spots", 0x01, 0x2F)
    save_combo(p, "night_mode", ["awning", "spots"])
    m = load_map(p)
    assert [e.name for e in m.resolve("night_mode")] == ["awning", "spots"]


def test_bad_name_raises(tmp_path):
    with pytest.raises(ValueError, match="name"):
        save_light(tmp_path / "l.toml", "Bad Name", 0x01, 0x2E)
