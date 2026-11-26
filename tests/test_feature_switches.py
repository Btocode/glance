"""The feature switches: registry, config tables, the CLI and the tray rows."""
from __future__ import annotations

from dataclasses import replace

import pytest

from glance import cli
from glance import feature_switches as fs
from glance.config import Config, SelectionCfg, load_config, update_config


def test_registry_lists_every_feature_once_with_defaults():
    assert fs.NAMES == ("typing", "selection", "shot", "autocomplete")
    cfg = Config()
    assert fs.is_on(cfg, "selection") is True        # decision 1: on by default
    assert fs.is_on(cfg, "shot") is True              # [shot] enabled, default true
    assert fs.is_on(cfg, "typing") is False           # the shortcut mode is the default
    assert fs.is_on(cfg, "autocomplete") is False     # auto: only with the api backend
    api = replace(cfg, backend=replace(cfg.backend, choice="api"))
    assert fs.is_on(api, "autocomplete") is True
    with pytest.raises(KeyError):
        fs.feature("nope")


def test_menu_rows_leave_the_check_mode_to_its_radio_items():
    rows = fs.menu_rows(Config())
    assert [r.name for r in rows] == ["selection", "shot", "autocomplete"]
    assert rows[0].label == "Selection actions" and rows[1].label == "Glance Shot"


def test_a_feature_without_a_typed_table_is_read_from_raw(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[shot]\nenabled = false\n")
    assert fs.is_on(load_config(p), "shot") is False


def test_selection_table_defaults_and_bad_values(tmp_path):
    assert SelectionCfg() == SelectionCfg(True, 2, "English", False)
    p = tmp_path / "config.toml"
    p.write_text('[selection]\nenabled = false\nmin_words = 0\ntranslate_to = ""\n'
                 'terminals = true\n')
    cfg = load_config(p)
    assert cfg.selection == SelectionCfg(False, 2, "English", True)
    assert "selection" not in cfg.raw


def test_set_feature_writes_the_key_and_reloads(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("# mine\n[general]\ncheck = \"shortcut\"\n")
    reloads = []
    assert fs.set_feature(update_config, p, "selection", False, lambda: reloads.append(1))
    assert fs.set_feature(update_config, p, "typing", True, lambda: reloads.append(2))
    assert fs.set_feature(update_config, p, "shot", False)
    cfg = load_config(p)
    assert reloads == [1, 2] and "# mine" in p.read_text()
    assert not fs.is_on(cfg, "selection") and fs.is_on(cfg, "typing")
    assert not fs.is_on(cfg, "shot")


def test_set_feature_reports_a_broken_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[[[ not toml")
    assert fs.set_feature(update_config, p, "selection", False) is False


@pytest.fixture
def home_cfg(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    monkeypatch.setattr("glance.paths.config_path", lambda: p)
    return p


def test_cli_lists_and_switches(home_cfg, capsys):
    assert cli.main(["features"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in out] == list(fs.NAMES)
    assert out[1].split()[1] == "on"
    assert cli.main(["features", "selection", "off"]) == 0
    assert capsys.readouterr().out.strip() == "selection: off"
    assert load_config(home_cfg).selection.enabled is False
    assert cli.main(["features", "selection"]) == 0
    assert capsys.readouterr().out.strip() == "selection: off"
    assert cli.main(["features", "shot", "off"]) == 0
    assert "enabled = false" in home_cfg.read_text()


def test_cli_refuses_unknown_names(home_cfg):
    with pytest.raises(SystemExit):
        cli.main(["features", "teleport", "on"])
