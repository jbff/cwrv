from pathlib import Path

import pytest

from rvcw.lights import LightMap, load_map, save_combo, save_light


def write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "lights.toml"
    p.write_text(content)
    return p


def test_load_and_resolve_light(tmp_path):
    from rvcw.lights import LightEntry

    p = write(tmp_path, "[lights.awning]\ndevice_id = 0x2E41\n")
    m = load_map(p)
    assert m.resolve("awning") == [LightEntry("awning", 0x2E41)]


def test_missing_file_is_empty(tmp_path):
    m = load_map(tmp_path / "nope.toml")
    with pytest.raises(KeyError, match="awning"):
        m.resolve("awning")


def test_combo_resolution(tmp_path):
    p = write(
        tmp_path,
        "[lights.awning]\ndevice_id = 0x2E41\n[lights.spots]\ndevice_id = 0x2E42\n"
        "[combos.night_mode]\nmembers = [\"awning\", \"spots\"]\n",
    )
    m = load_map(p)
    assert [e.device_id for e in m.resolve("night_mode")] == [0x2E41, 0x2E42]


def test_combo_missing_member_raises(tmp_path):
    p = write(tmp_path, "[combos.bad]\nmembers = [\"ghost\"]\n")
    m = load_map(p)
    with pytest.raises(KeyError, match="ghost"):
        m.resolve("bad")


def test_save_light_roundtrip(tmp_path):
    p = save_light(tmp_path / "lights.toml", "awning", 0x2E41)
    reloaded = load_map(p)
    assert reloaded.resolve("awning")[0].device_id == 0x2E41


def test_save_combo_roundtrip(tmp_path):
    p = save_light(tmp_path / "lights.toml", "awning", 0x2E41)
    p = save_light(p, "spots", 0x2E42)
    save_combo(p, "night_mode", ["awning", "spots"])
    m = load_map(p)
    assert [e.name for e in m.resolve("night_mode")] == ["awning", "spots"]


def test_bad_name_raises(tmp_path):
    with pytest.raises(ValueError, match="name"):
        save_light(tmp_path / "l.toml", "Bad Name", 0x2E41)
