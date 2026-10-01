"""The hosted image installs cp-engine; the deploy stages exactly what it needs.

WHY (architecture plan step 1). Until 2026-09-30 the image shipped
`server.py` + `observability.py` + a hand-vendored `vendor/cp_engine` closure
guarded by a drift test (#283, #287, #295). The image now `pip install`s the
engine from the same commit, and the build context is a repo-root-shaped
staging dir built by `deploy.sh`. These tests pin the three places that must
agree — the Dockerfile's COPY sources, deploy.sh's staged paths, and the
local-build `.dockerignore` — plus the staging itself, because a path that is
COPYed but not staged is a build that fails only on Railway.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[1]


def _copy_sources() -> set[str]:
    text = (_HERE / "Dockerfile").read_text(encoding="utf-8")
    text = text.replace("\\\n", " ")
    out: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("COPY "):
            continue
        parts = line.split()[1:]
        out.update(p for p in parts[:-1] if not p.startswith("--"))
    return out


def _staged_paths() -> set[str]:
    text = (_HERE / "deploy.sh").read_text(encoding="utf-8")
    block = re.search(r"paths=\((.*?)\)", text, re.S)
    assert block, "deploy.sh has no paths=(...) array"
    return set(block.group(1).split())


def test_there_is_no_vendored_engine():
    assert not (_HERE / "vendor").exists(), "vendor/ is back — import the engine instead"
    src = (_HERE / "server.py").read_text(encoding="utf-8")
    assert "sys.path.append" not in src and "sys.path.insert" not in src


def test_the_image_installs_the_engine_from_the_checkout():
    text = (_HERE / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY src ./cp-engine/src" in text
    assert "pip install --no-cache-dir ./cp-engine" in text
    # The token must never land in a persisted git config layer.
    assert "git config --global" not in text


def test_every_copy_source_is_staged_by_deploy_sh():
    staged = _staged_paths()
    for src in _copy_sources():
        if src.endswith("BUILD_COMMI[T]"):
            continue  # written by deploy.sh itself
        assert any(src == p or src.startswith(p + "/") for p in staged), (
            f"Dockerfile COPYs {src!r} but deploy.sh does not stage it — "
            "the Railway build would fail on a file the local build had"
        )
    for p in staged:
        assert (_ROOT / p).exists(), f"deploy.sh stages {p!r}, which does not exist"


def test_the_local_build_context_admits_every_copy_source():
    rules = (_HERE / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    admitted = {r[1:].rstrip("/") for r in rules if r.startswith("!")}
    for src in _copy_sources():
        src = src.replace("BUILD_COMMI[T]", "BUILD_COMMIT")
        assert any(src == a or src.startswith(a + "/") for a in admitted), (
            f"{src!r} is COPYed but excluded by Dockerfile.dockerignore"
        )


def test_railway_toml_points_at_the_dockerfile_from_the_stage_root():
    text = (_HERE / "railway.toml").read_text(encoding="utf-8")
    m = re.search(r'dockerfilePath\s*=\s*"([^"]+)"', text)
    assert m and (_ROOT / m.group(1)).resolve() == (_HERE / "Dockerfile").resolve()


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                    reason="needs git and bash")
def test_deploy_sh_stages_a_buildable_context():
    """Run the real staging (no Railway, no Docker) and check its shape."""
    proc = subprocess.run(
        ["bash", str(_HERE / "deploy.sh"), "--stage-only", "--allow-dirty"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    stage = Path(proc.stdout.strip().splitlines()[-1])
    try:
        assert (stage / "railway.toml").is_file(), "railway.toml must sit at the upload root"
        assert (stage / "src" / "cp_engine" / "__init__.py").is_file()
        assert (stage / "pyproject.toml").is_file()
        assert (stage / "prototypes/hosted-mcp/server.py").is_file()
        commit = (stage / "prototypes/hosted-mcp/BUILD_COMMIT").read_text().strip()
        assert re.fullmatch(r"[0-9a-f]{7,40}(-dirty)?", commit), commit
        assert not (stage / ".venv").exists() and not (stage / "tests").exists()
        for src in _copy_sources():
            src = src.replace("BUILD_COMMI[T]", "BUILD_COMMIT")
            assert (stage / src).exists(), f"staged context lacks {src}"
    finally:
        shutil.rmtree(stage, ignore_errors=True)
