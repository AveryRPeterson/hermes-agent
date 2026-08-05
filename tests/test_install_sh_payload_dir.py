"""E2E tests for install.sh --payload-dir offline staging (plan §3).

Exercise the real shell functions against real fixtures — a git "payload
repo" clone, a manifest.json in the shape stage-agent-payloads.mjs writes —
never asserting on install.sh's text. Mirrors the pattern of
test_install_sh_bootstrap_marker.py.
"""

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

# Functions the payload test-harness needs, extracted from install.sh by name.
_FUNCS = (
    "payload_has",
    "payload_tag",
    "payload_refuses_source_checkout",
    "payload_stage_repo",
    "write_install_manifest_file",
    "write_install_mode_manifest",
)


def _extract_functions_script():
    parts = [
        f"eval \"$(sed -n '/^{name}()/,/^}}/p' {INSTALL_SH})\"" for name in _FUNCS
    ]
    return "\n".join(parts)


def run_payload_snippet(body, *, payload_dir="", install_dir=""):
    """Run a bash snippet with install.sh's payload functions defined."""
    script = f"""
set -e
PAYLOAD_DIR={str(payload_dir)!r}
INSTALL_DIR={str(install_dir)!r}
{_extract_functions_script()}
log_info() {{ echo "INFO: $*" >&2; }}
log_warn() {{ echo "WARN: $*" >&2; }}
log_error() {{ echo "ERROR: $*" >&2; }}
log_success() {{ echo "OK: $*" >&2; }}
{body}
"""
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )


def make_payload(tmp_path, *, items=("repo",), tag="v0.1.0", commits=2):
    """Build a payload dir shaped like stage-agent-payloads.mjs output."""
    payload = tmp_path / "agent-payload"
    payload.mkdir(exist_ok=True)
    manifest = {
        "schemaVersion": 1,
        "tag": tag,
        "items": {i: {"status": "staged"} for i in items},
    }
    (payload / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if "repo" in items:
        repo = payload / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        for i in range(commits):
            (repo / f"f{i}.txt").write_text(f"payload {i}\n")
            _git(repo, "add", ".")
            _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", f"c{i}")
    return payload


def _git(cwd, *args):
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {args}: {result.stderr}"
    return result.stdout.strip()


class TestPayloadHas:
    def test_staged_item_detected(self, tmp_path):
        payload = make_payload(tmp_path, items=("repo", "wheels"))
        r = run_payload_snippet("payload_has wheels && echo YES", payload_dir=payload)
        assert "YES" in r.stdout

    def test_missing_item_and_skipped_status_rejected(self, tmp_path):
        payload = make_payload(tmp_path, items=("repo",))
        manifest = json.loads((payload / "manifest.json").read_text())
        manifest["items"]["wheels"] = {"status": "skipped", "reason": "explicit-skip"}
        (payload / "manifest.json").write_text(json.dumps(manifest))
        r = run_payload_snippet(
            "payload_has wheels && echo YES || echo NO", payload_dir=payload
        )
        assert "NO" in r.stdout

    def test_no_payload_dir_is_false(self, tmp_path):
        r = run_payload_snippet("payload_has repo && echo YES || echo NO")
        assert "NO" in r.stdout

    def test_thin_stub_manifest_is_false(self, tmp_path):
        payload = tmp_path / "agent-payload"
        payload.mkdir()
        (payload / "manifest.json").write_text(
            json.dumps({"schemaVersion": 1, "thin": True, "items": {}})
        )
        r = run_payload_snippet("payload_has repo && echo YES || echo NO", payload_dir=payload)
        assert "NO" in r.stdout


class TestPayloadTag:
    def test_reads_tag(self, tmp_path):
        payload = make_payload(tmp_path, tag="v2.3.4")
        r = run_payload_snippet("payload_tag", payload_dir=payload)
        assert r.stdout.strip() == "v2.3.4"


class TestPayloadStageRepo:
    def test_fresh_materialization_is_git_shaped(self, tmp_path):
        """Fresh install: repo copied with .git so eject/update premises hold."""
        payload = make_payload(tmp_path)
        install_dir = tmp_path / "hermes-agent"
        r = run_payload_snippet(
            "payload_stage_repo", payload_dir=payload, install_dir=install_dir
        )
        assert r.returncode == 0, r.stderr
        assert (install_dir / ".git").is_dir()
        assert (install_dir / "f0.txt").read_text() == "payload 0\n"
        payload_head = _git(payload / "repo", "rev-parse", "HEAD")
        assert _git(install_dir, "rev-parse", "HEAD") == payload_head

    def test_existing_checkout_updated_via_file_fetch(self, tmp_path):
        """Existing bundled checkout: fetched + hard-reset to payload HEAD, offline."""
        payload = make_payload(tmp_path, commits=3)
        install_dir = tmp_path / "hermes-agent"
        # Simulate an older bundled install: clone payload repo then rewind.
        subprocess.run(
            ["git", "clone", "-q", str(payload / "repo"), str(install_dir)],
            check=True, capture_output=True,
        )
        _git(install_dir, "reset", "--hard", "-q", "HEAD~2")
        assert not (install_dir / "f2.txt").exists()

        r = run_payload_snippet(
            "payload_stage_repo", payload_dir=payload, install_dir=install_dir
        )
        assert r.returncode == 0, r.stderr
        assert (install_dir / "f2.txt").exists()
        assert _git(install_dir, "rev-parse", "HEAD") == _git(
            payload / "repo", "rev-parse", "HEAD"
        )

    def test_refuses_source_managed_checkout(self, tmp_path):
        """The eject contract: a source-mode checkout is never overwritten."""
        payload = make_payload(tmp_path)
        install_dir = tmp_path / "hermes-agent"
        install_dir.mkdir()
        (install_dir / ".hermes-install.json").write_text(
            json.dumps({"schemaVersion": 1, "installMode": "source",
                        "channel": "main", "manageStyle": "ejected"})
        )
        (install_dir / "user-file.txt").write_text("precious\n")

        r = run_payload_snippet(
            "payload_stage_repo && echo STAGE-OK",
            payload_dir=payload, install_dir=install_dir,
        )
        # Succeeds (stage satisfied) but leaves the checkout alone.
        assert "STAGE-OK" in r.stdout
        assert (install_dir / "user-file.txt").read_text() == "precious\n"
        assert not (install_dir / "f0.txt").exists()

    def test_no_repo_item_falls_back(self, tmp_path):
        payload = make_payload(tmp_path, items=("wheels",))
        r = run_payload_snippet(
            "payload_stage_repo && echo STAGED || echo FALLBACK",
            payload_dir=payload, install_dir=tmp_path / "x",
        )
        assert "FALLBACK" in r.stdout


class TestWriteInstallModeManifest:
    def test_bundled_manifest_after_payload_install(self, tmp_path):
        payload = make_payload(tmp_path, tag="v1.0.0")
        install_dir = tmp_path / "hermes-agent"
        install_dir.mkdir()
        r = run_payload_snippet(
            "write_install_mode_manifest",
            payload_dir=payload, install_dir=install_dir,
        )
        assert r.returncode == 0, r.stderr
        manifest = json.loads((install_dir / ".hermes-install.json").read_text())
        assert manifest["installMode"] == "bundled"
        assert manifest["channel"] == "stable"
        assert manifest["manageStyle"] == "adopted"
        assert manifest["pinnedTag"] == "v1.0.0"
        assert manifest["schemaVersion"] == 1

    def test_source_manifest_without_payload(self, tmp_path):
        install_dir = tmp_path / "hermes-agent"
        install_dir.mkdir()
        r = run_payload_snippet(
            "write_install_mode_manifest", install_dir=install_dir
        )
        assert r.returncode == 0, r.stderr
        manifest = json.loads((install_dir / ".hermes-install.json").read_text())
        assert manifest["installMode"] == "source"
        assert manifest["channel"] == "main"
        assert "manageStyle" not in manifest

    def test_ejected_manifest_is_never_overwritten(self, tmp_path):
        """Sticky opt-out: even a payload install preserves ejected."""
        payload = make_payload(tmp_path)
        install_dir = tmp_path / "hermes-agent"
        install_dir.mkdir()
        original = {
            "schemaVersion": 1, "installMode": "source",
            "channel": "stable", "manageStyle": "ejected",
        }
        (install_dir / ".hermes-install.json").write_text(json.dumps(original))
        r = run_payload_snippet(
            "write_install_mode_manifest",
            payload_dir=payload, install_dir=install_dir,
        )
        assert r.returncode == 0, r.stderr
        after = json.loads((install_dir / ".hermes-install.json").read_text())
        assert after["manageStyle"] == "ejected"
        assert after["installMode"] == "source"


class TestManifestReadableByPython:
    def test_shell_written_manifest_parses_via_install_manifest_module(self, tmp_path):
        """Cross-language contract: install.sh's writer ↔ Python's reader."""
        from hermes_cli.install_manifest import read_install_manifest

        payload = make_payload(tmp_path, tag="v3.0.0")
        install_dir = tmp_path / "hermes-agent"
        install_dir.mkdir()
        run_payload_snippet(
            "write_install_mode_manifest",
            payload_dir=payload, install_dir=install_dir,
        )
        manifest = read_install_manifest(install_dir)
        assert manifest["installMode"] == "bundled"
        assert manifest["channel"] == "stable"
        assert manifest["manageStyle"] == "adopted"
        assert manifest["pinnedTag"] == "v3.0.0"
