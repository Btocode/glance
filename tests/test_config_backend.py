import os

import pytest

from glance.config import (
    DEFAULT_MODELS,
    BackendCfg,
    Config,
    ConfigError,
    load_config,
    update_config,
)
from glance.paths import usage_path


def test_backend_defaults(tmp_path):
    b = load_config(tmp_path / "nope.toml").backend
    assert b == BackendCfg() == Config().backend
    assert b.choice is None and b.enabled == ()
    assert b.models == DEFAULT_MODELS == {
        "claude": "haiku", "codex": "gpt-6-luna", "gemini": "flash-lite"}
    assert (b.per_minute, b.per_day) == (10, 300)
    assert b.speculate is None and b.live_autocomplete is None
    assert (b.api_provider, b.api_model, b.api_base_url) == ("anthropic", "", "")
    assert b.analyze_with == ()


def test_backend_section_parsed(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[backend]\nchoice = "codex"\nenabled = ["claude", "api"]\nper_minute = 4\n'
        'per_day = 50\nspeculate = true\nlive_autocomplete = false\n'
        'api_provider = "openai"\napi_model = "m"\napi_base_url = "https://x.test/v1"\n'
        'analyze_with = ["codex"]\n[backend.models]\ncodex = "gpt-x"\n'
    )
    b = load_config(p).backend
    assert b == BackendCfg(
        choice="codex", enabled=("claude", "api"), models={**DEFAULT_MODELS, "codex": "gpt-x"},
        per_minute=4, per_day=50, speculate=True, live_autocomplete=False,
        api_provider="openai", api_model="m", api_base_url="https://x.test/v1", analyze_with=("codex",),
    )


@pytest.mark.parametrize("body, key", [
    ("choice = 3", "backend.choice"),
    ('speculate = "yes"', "backend.speculate"),
    ('per_day = "many"', "backend.per_day"),
    ("per_day = -1", "backend.per_day"),
    ("per_minute = true", "backend.per_minute"),
    ('enabled = "claude"', "backend.enabled"),
    ("models = 1", "backend.models"),
])
def test_backend_wrong_types_raise(tmp_path, body, key):
    p = tmp_path / "c.toml"
    p.write_text("[backend]\n" + body + "\n")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert e.value.key == key


def test_usage_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert usage_path() == tmp_path / "glance" / "usage.json"


def test_update_config_creates_file_and_parent(tmp_path):
    p = tmp_path / "glance" / "config.toml"
    update_config(p, "backend.choice", "claude")
    assert p.read_text() == '[backend]\nchoice = "claude"\n'
    assert load_config(p).backend.choice == "claude"


def test_update_config_preserves_comments_and_other_keys(tmp_path):
    p = tmp_path / "c.toml"
    src = ('# mine\n[general]\nchip_idle_ms = 500  # snappy\n\n'
           '[backend]\n# which AI\nchoice = "claude"\nper_day = 20\n')
    p.write_text(src)
    update_config(p, "backend.choice", "codex")
    assert p.read_text() == src.replace('"claude"', '"codex"')
    update_config(p, "backend.enabled", ("claude", "api"))
    update_config(p, "backend.models.codex", "gpt-x")
    update_config(p, "backend.speculate", True)
    c = load_config(p)
    assert c.general.chip_idle_ms == 500
    assert c.backend.choice == "codex" and c.backend.enabled == ("claude", "api")
    assert c.backend.models["codex"] == "gpt-x" and c.backend.speculate is True
    assert c.backend.per_day == 20
    assert "# mine" in p.read_text() and "# snappy" in p.read_text()
    assert "# which AI" in p.read_text()


def test_update_config_none_removes_key(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[backend]\nspeculate = true\nper_day = 20\n")
    update_config(p, "backend.speculate", None)
    update_config(p, "backend.absent", None)
    assert load_config(p).backend.speculate is None
    assert p.read_text() == "[backend]\nper_day = 20\n"


def test_update_config_is_atomic_and_leaves_no_temp(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[general]\nchip_idle_ms = 500\n")
    update_config(p, "backend.choice", "api")
    assert sorted(x.name for x in tmp_path.iterdir()) == ["c.toml"]


def test_update_config_refuses_to_clobber_invalid_toml(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[general\nbroken")
    with pytest.raises(ConfigError):
        update_config(p, "backend.choice", "api")
    assert p.read_text() == "[general\nbroken"
    p.write_text("backend = 3\n")
    with pytest.raises(ConfigError):
        update_config(p, "backend.choice", "api")
    assert p.read_text() == "backend = 3\n"


def test_update_config_keeps_symlinked_config(tmp_path):
    real = tmp_path / "dotfiles" / "glance.toml"
    real.parent.mkdir()
    real.write_text("# linked\n")
    link = tmp_path / "config.toml"
    os.symlink(real, link)
    update_config(link, "backend.choice", "gemini")
    assert link.is_symlink()
    assert real.read_text().startswith("# linked\n")
    assert load_config(real).backend.choice == "gemini"
