"""Pins for continue-gate, the Stop hook that blocks stalled turn endings.

Fixtures are real turn-final assistant texts from a Sep 20-21 wave session
plus hand-built edge cases. The hook script runs for real via subprocess with
CLAUDE_MUSE_DIR pointed at tmp_path, so counter state never touches the live
install. The filename prefix is the verdict: block-announce-*, block-loop-*,
block-asking-* must exit 2 with their tag, ok-* must exit 0.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "profile" / "hooks" / "continue-gate"
FIXTURES = Path(__file__).parent / "fixtures" / "continue-gate"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="continue-gate fails open without jq"
)

TAGS = {
    "block-announce-": "blocked (announce-stop,",
    "block-loop-": "blocked (unarmed-loop-claim,",
    "block-asking-": "blocked (asking-permission,",
}


def _run(payload: bytes, tmp_path):
    proc = subprocess.run(
        [str(GATE)],
        input=payload,
        capture_output=True,
        timeout=10,
        env={**os.environ, "CLAUDE_MUSE_DIR": str(tmp_path)},
    )
    return proc.returncode, proc.stdout.decode(), proc.stderr.decode()


def _fixture_names():
    return sorted(p.name for p in FIXTURES.glob("*.json"))


def test_fixture_verdicts_match_prefix(tmp_path):
    """Every fixture blocks or allows exactly as its name says."""
    names = _fixture_names()
    assert names, "no fixtures found"
    for name in names:
        code, out, err = _run((FIXTURES / name).read_bytes(), tmp_path)
        if name.startswith("ok-"):
            assert code == 0, f"{name}: blocked with {out.strip()}"
            assert "allowing" in out, f"{name}: {out.strip()}"
        else:
            tag = next(v for k, v in TAGS.items() if name.startswith(k))
            assert code == 2, f"{name}: allowed with {out.strip()}"
            assert tag in out, f"{name}: {out.strip()}"
            wrapped = [line for line in err.splitlines() if line.startswith((" ", "\t"))]
            assert not wrapped, f"{name}: stderr has hard-wrapped lines: {wrapped}"


def test_block_bound_gives_up_after_two(tmp_path):
    """Re-entries share one counter; at the bound the gate stands down."""
    doc = json.loads((FIXTURES / "block-announce-colon-613.json").read_bytes())
    first = json.dumps(doc).encode()
    doc["stop_hook_active"] = True
    reentry = json.dumps(doc).encode()
    assert _run(first, tmp_path)[0] == 2
    assert _run(reentry, tmp_path)[0] == 2
    code, out, _ = _run(reentry, tmp_path)
    assert code == 0
    assert "allowing after 2 blocks" in out


def test_fresh_episode_resets_counter(tmp_path):
    """A new turn gets a fresh budget even after a bound was hit."""
    doc = json.loads((FIXTURES / "block-announce-colon-613.json").read_bytes())
    doc["stop_hook_active"] = True
    reentry = json.dumps(doc).encode()
    _run(reentry, tmp_path)
    _run(reentry, tmp_path)
    assert _run(reentry, tmp_path)[0] == 0  # bound hit
    doc["stop_hook_active"] = False
    assert _run(json.dumps(doc).encode(), tmp_path)[0] == 2  # fresh turn blocks again
