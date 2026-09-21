"""Pins for the documented installer and policy constraints.

These tests guard rules that live in prose (CLAUDE.md, install.sh comments)
so a future edit breaks loudly instead of silently.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def test_every_installed_file_is_placed():
    """A new installed file fails here until it gets a `place` line.

    engine/ is placed as one directory symlink, so its files are covered by
    the place_dir line rather than one line each.
    """
    text = (REPO / "install.sh").read_text()
    placed = set(re.findall(r"^place (\S+)", text, re.M))
    placed_dirs = set(re.findall(r"^place_dir (\S+)", text, re.M))
    expected = set()
    for dirname in ("bin", "lib", "engine"):
        for path in (REPO / dirname).iterdir():
            if path.is_file():
                expected.add(path.relative_to(REPO).as_posix())
    expected |= {
        "proxy.py",
        "profile/CLAUDE.md",
        "profile/hooks/continue-gate",
        "profile/statusline.sh",
    }
    missing = sorted(
        src
        for src in expected
        if src not in placed and not any(src == d or src.startswith(d + "/") for d in placed_dirs)
    )
    assert not missing, f"add a `place` line for: {missing}"


def test_every_profile_skill_is_placed():
    """A new profile skill fails here until it gets a `place_skill` line."""
    text = (REPO / "install.sh").read_text()
    placed = set(re.findall(r"^place_skill (\S+)", text, re.M))
    expected = {p.name for p in (REPO / "profile" / "skills").iterdir() if p.is_dir()}
    missing = sorted(expected - placed)
    assert not missing, f"add a `place_skill` line for: {missing}"


def test_settings_template_has_no_model_key():
    """A settings model pin would outrank ANTHROPIC_MODEL (see install.sh)."""
    tmpl = json.loads((REPO / "profile" / "settings.json.tmpl").read_text())
    assert "model" not in tmpl


def test_baked_in_rules_match_yaml_template(proxy):
    """The baked-ins mirror templates/rewrite-rules.yaml; this checks they match."""
    yaml = pytest.importorskip("yaml", reason="pyyaml is optional; CI installs it")
    doc = yaml.safe_load((REPO / "templates" / "rewrite-rules.yaml").read_text())
    assert [proxy.Rule(**e) for e in doc["rules"]] == proxy.BAKED_IN_RULES
    profiles = {k: proxy.Profile(**v) for k, v in doc["models"].items()}
    assert profiles == proxy.BAKED_IN_PROFILES


def _manifest_value(key):
    """One `key = "value"` from md-plugin.toml. A regex, because tomllib is 3.11+ and CI is 3.9."""
    text = (REPO / "md-plugin.toml").read_text()
    match = re.search(rf'^{key} = "([^"]*)"$', text, re.M)
    assert match, f"md-plugin.toml has no {key}"
    return match.group(1)


def test_md_driver_store_is_the_profile_the_launcher_uses():
    """md watches `config_dir` for the session; model-env.sh decides where it is written.

    If the two ever disagree, every Muse session md launches reads as dead the moment it starts,
    and md relaunches it.
    """
    env = (REPO / "lib" / "model-env.sh").read_text()
    exported = re.search(r'^export CLAUDE_CONFIG_DIR="\$HOME/([^"]+)"$', env, re.M)
    assert exported, "model-env.sh no longer exports CLAUDE_CONFIG_DIR under $HOME"
    assert _manifest_value("config_dir") == "~/" + exported.group(1)


def test_md_driver_launcher_is_an_executable_in_the_repo():
    text = (REPO / "md-plugin.toml").read_text()
    argv = re.search(r'^argv = \["\{checkout\}/([^"]+)"\]$', text, re.M)
    assert argv, "the [driver] argv is not a single {checkout}-relative launcher"
    launcher = REPO / argv.group(1)
    assert launcher.is_file()
    assert launcher.stat().st_mode & 0o111, f"{argv.group(1)} is not executable"
    assert "exec claude" in launcher.read_text()
