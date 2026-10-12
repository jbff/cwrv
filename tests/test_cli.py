# tests/test_cli.py
import pytest
from click.testing import CliRunner


@pytest.fixture()
def rvcw_home(tmp_path, monkeypatch):
    home = tmp_path / "rvcw"
    home.mkdir()
    monkeypatch.setenv("RVCW_HOME", str(home))
    (home / "lights.toml").write_text("[lights.awning]\ndevice_id = 0x2E41\n")
    return home


def test_send_dry_run_lists_schedule(rvcw_home):
    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["send", "E", "--light", "awning", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "0x2E41" in result.output
    assert "ON    150ms" in result.output


def test_send_unknown_light_fails_fast(rvcw_home):
    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["send", "E", "--light", "ghost", "--dry-run"])
    assert result.exit_code != 0
    assert "ghost" in result.output


def test_lights_list(rvcw_home):
    from rvcw.cli import cli

    result = CliRunner().invoke(cli, ["lights"])
    assert result.exit_code == 0
    assert "awning" in result.output and "0x2E41" in result.output
