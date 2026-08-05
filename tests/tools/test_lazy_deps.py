"""Behaviour of tools/lazy_deps.py that is not pyproject.toml.

The specs come from the extras now, and uv.lock pins them, so nothing here
restates a package or a version. What is left is the code around the lookup:

* the allowlist — only a key in LAZY_DEPS may install
* the gate — security.allow_lazy_installs and the sealed-image flag
* ensure() — no-op when satisfied, and a clear error when pip lies
* active_features / refresh_active_features — the `hermes update` pass
* install_specs — the path for a package that no extra can hold

tests/tools/test_lazy_deps_extras_mapping.py covers the map and the reader.
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
        monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
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

    def test_the_sealed_gate_runs_before_the_installer(self, monkeypatch):
        """A sealed deployment must stop the install, whatever the specs are."""
        monkeypatch.setattr(ld, "_allow_lazy_installs", lambda: False)
        monkeypatch.setattr(
            ld, "_venv_pip_install",
            lambda *a, **kw: pytest.fail("pip should not be called"),
        )
        result = ld.install_specs(["zzzpkg==1.0"])
        assert result.ok is False
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

