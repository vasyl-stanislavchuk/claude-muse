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
