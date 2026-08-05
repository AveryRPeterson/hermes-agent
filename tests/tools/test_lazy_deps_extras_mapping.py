"""LAZY_DEPS maps a feature name to an extra in pyproject.toml.

Two things need a test here.

The map itself: each value must name an extra that pyproject.toml declares.
Nothing else checks that. A typo makes the feature raise FeatureUnavailable
at first use, on the one machine that enabled that backend.

The reader: extra_specs expands a `hermes-agent[x]` reference, and that code
is ours. A cycle, a lost marker, or a silent empty result would each ship a
wrong package set. uv resolves the extras its own way and cannot catch a
fault in our reader.

Nothing here restates a version. pyproject.toml holds the specs, uv.lock
pins them, and `uv lock --check` and `uv audit` read the lockfile.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import lazy_deps as ld  # noqa: E402


class TestFeatureExtraMapping:
    def test_every_feature_maps_to_a_declared_extra(self):
        """A value in LAZY_DEPS must name an extra that pyproject declares.

        This is the whole contract of the map. A typo here raises
        FeatureUnavailable at first use of that backend, and only on a
        machine that enabled it.
        """
        declared = set(ld._optional_dependencies())
        missing = {
            feature: extra
            for feature, extra in ld.LAZY_DEPS.items()
            if extra not in declared
        }
        assert not missing, (
            f"LAZY_DEPS names extras that pyproject.toml does not declare: "
            f"{missing}"
        )

    def test_every_feature_resolves_to_at_least_one_spec(self):
        """An extra can exist and still be empty after composition.

        An empty result installs nothing and reports success, so the backend
        stays broken with no error to read.
        """
        empty = [f for f in ld.LAZY_DEPS if not ld.feature_specs(f)]
        assert not empty, f"features that resolve to no packages: {empty}"


class TestExtraComposition:
    """extra_specs expands `hermes-agent[x]`. That expansion is our code."""

    def test_self_references_resolve(self):
        """[messaging] contains [telegram], so its specs must appear."""
        composed = set(ld.extra_specs("messaging"))
        assert set(ld.extra_specs("telegram")) <= composed
        assert not any(s.startswith("hermes-agent[") for s in composed), (
            "a self-reference must be expanded, not passed to pip"
        )

    def test_cycles_terminate(self, monkeypatch):
        """A cycle in the extras must not hang or recurse without end."""
        monkeypatch.setattr(ld, "_optional_dependencies", lambda: {
            "a": ("hermes-agent[b]",),
            "b": ("hermes-agent[a]",),
        })
        assert ld.extra_specs("a") == ()

    def test_unknown_extra_resolves_to_nothing(self, monkeypatch):
        monkeypatch.setattr(ld, "_optional_dependencies", lambda: {})
        assert ld.extra_specs("nope") == ()

    def test_marker_on_self_reference_is_distributed(self, monkeypatch):
        """`hermes-agent[x]; marker` must put the marker on each spec.

        [wake] holds `hermes-agent[wake-tflite]; platform_system == 'Darwin'`.
        Losing that marker installs a macOS-only package on Linux.
        """
        monkeypatch.setattr(ld, "_optional_dependencies", lambda: {
            "big": ("hermes-agent[small]; platform_system == 'Darwin'",),
            "small": ("pkg-a==1.0", "pkg-b==2.0"),
        })
        assert set(ld.extra_specs("big")) == {
            "pkg-a==1.0; platform_system == 'Darwin'",
            "pkg-b==2.0; platform_system == 'Darwin'",
        }
