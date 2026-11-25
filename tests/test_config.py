import os

import pytest

from glance.config import AppCfg, ConfigError, ConfigWatcher, load_config
from tests.fakes import FakeClock


def test_defaults_when_file_missing(tmp_path):
    c = load_config(tmp_path / "nope.toml")
    g = c.general
    assert (g.chip_idle_ms, g.analyze_idle_ms, g.min_chars_chip, g.min_chars_analyze) == (
        400, 1200, 15, 25)
    assert g.hotkeys["panel"] == "Super+Alt+space"
    assert c.app("Slack").type_max_chars == 80 and "seahorse" in c.privacy.app_denylist
    assert c.ui.chip_px == 22


def test_app_override(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[apps.slack]\nwriteback = "paste"\nundo_steps = 2\n')
    assert load_config(p).app("Slack") == AppCfg(writeback="paste", undo_steps=2)


def test_gtk_source_view_apps_paste_by_default(tmp_path):
    # A typed replace is two undo steps in a GtkSourceView, a pasted one is one (S4.4).
    c = load_config(tmp_path / "nope.toml")
    assert c.app("Gedit").writeback == "paste" and c.app("org.gnome.gedit").writeback == "paste"
    assert c.app("Slack").writeback == "auto"


def test_app_table_overrides_a_builtin_default_field_by_field(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[apps.gedit]\ntype_delay_ms = 5\n')
    assert load_config(p).app("gedit") == AppCfg(writeback="paste", type_delay_ms=5)
    p.write_text('[apps.gedit]\nwriteback = "type"\n')
    assert load_config(p).app("gedit").writeback == "type"


def test_unknown_keys_ignored_and_raw_kept(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[general]\nbogus = 1\nchip_idle_ms = 500\n[backend]\nkind = "x"\n')
    c = load_config(p)
    assert c.general.chip_idle_ms == 500 and c.raw["backend"] == {"kind": "x"}


def test_partial_hotkeys_merge_over_defaults(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[general.hotkeys]\npanel = "Super+Alt+x"\n')
    h = load_config(p).general.hotkeys
    assert h["panel"] == "Super+Alt+x" and h["revert"] == "Super+Alt+z"


def test_wrong_type_raises(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[general]\nchip_idle_ms = "soon"\n')
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "chip_idle_ms" in str(e.value)


def test_bool_not_accepted_as_int(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[general]\nchip_idle_ms = true\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_toml_raises_config_error(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[general\nchip")
    with pytest.raises(ConfigError):
        load_config(p)


def _bump(p, n):
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + n))


def test_reload_on_change(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[general]\nmin_chars_chip = 15\n")
    clock, seen, errors = FakeClock(), [], []
    w = ConfigWatcher(p, clock, seen.append, errors.append)
    w.start()
    clock.advance(1000)
    assert seen == []  # unchanged file: no callback
    p.write_text("[general]\nmin_chars_chip = 30\n")
    _bump(p, 5)
    clock.advance(1000)
    assert seen[-1].general.min_chars_chip == 30
    assert w.current.general.min_chars_chip == 30 and errors == []


def test_reload_keeps_last_good_on_invalid_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[general]\nchip_idle_ms = 400\n")
    clock, seen, errors = FakeClock(), [], []
    w = ConfigWatcher(p, clock, seen.append, errors.append)
    w.start()
    p.write_text("[general\nchip")
    _bump(p, 5)
    clock.advance(1000)
    clock.advance(1000)
    assert w.current.general.chip_idle_ms == 400 and len(errors) == 1 and seen == []
    p.write_text("[general]\nchip_idle_ms = 700\n")
    _bump(p, 10)
    clock.advance(1000)
    assert w.current.general.chip_idle_ms == 700 and len(seen) == 1


def test_file_deleted_and_recreated(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[general]\nchip_idle_ms = 700\n")
    clock, seen, errors = FakeClock(), [], []
    w = ConfigWatcher(p, clock, seen.append, errors.append)
    w.start()
    p.unlink()
    clock.advance(1000)
    assert w.current.general.chip_idle_ms == 400 and len(seen) == 1
