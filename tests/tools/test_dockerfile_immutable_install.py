"""Contract tests for the Docker image's immutable /opt/hermes install tree."""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text()


def test_dockerfile_makes_opt_hermes_readonly_for_hermes_user() -> None:
    text = _dockerfile_text()

    # --chmod on the source COPY bakes read-only perms at copy time instead
    # of a separate chmod -R pass (which walked ~30k files — #49113).
    assert "COPY --link --chmod=a+rX,go-w . ." in text
    # The old tree-walking passes must not be present.
    assert "chown -R root:root /opt/hermes" not in text
    assert "chmod -R a+rX /opt/hermes" not in text
    assert "chmod -R a-w /opt/hermes" not in text


def test_dockerfile_does_not_chown_install_trees_to_hermes() -> None:
    text = _dockerfile_text()
    forbidden_patterns = (
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/\.venv",
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/ui-tui",
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/gateway",
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/node_modules",
    )
    for pattern in forbidden_patterns:
        assert not re.search(pattern, text), (
            "runtime install trees under /opt/hermes must stay immutable; "
            f"found forbidden pattern {pattern!r}"
        )


def test_dockerfile_bakes_code_scoped_install_method_stamp() -> None:
    """The 'docker' install-method stamp is baked next to the code.

    detect_install_method() reads the code-scoped stamp
    (/opt/hermes/.install_method) first; baking it at build time keeps the
    published image self-identifying as 'docker' WITHOUT writing into the
    shared $HERMES_HOME data volume (which a host install may also use).
    The stamp is created by root in the shim-wiring RUN block; the hermes
    user can't modify it (go-w from the --chmod on the source COPY).
    """
    text = _dockerfile_text()
    assert "printf 'docker\\n' > /opt/hermes/.install_method" in text

    # The stamp must be in the RUN block that wires the exec shim.
    shim_block = re.search(
        r"RUN mkdir -p /opt/hermes/bin && \\\n"
        r"(?:.*\\\n)+?"
        r"\s+printf 'docker\\n' > /opt/hermes/\.install_method",
        text,
    )
    assert shim_block, "install-method stamp must be in the shim-wiring RUN block"


def test_dockerfile_seals_lazy_installs_with_no_redirect() -> None:
    """The image must refuse installs at run time and set no target directory.

    The build puts each extra that a container can run into the image, so an
    install at run time means that the image does not have a dependency that
    it must ship. A target directory permits such installs. It downloads a
    package that no scanner examined, and it shows unedited pip errors when
    the container cannot reach PyPI.

    This test holds the Dockerfile, stage2-hook.sh and tools/lazy_deps.py to
    one agreement: the image permits no install.
    """
    text = _dockerfile_text()

    assert "ENV HERMES_DISABLE_LAZY_INSTALLS=1" in text
    assert "ENV HERMES_LAZY_INSTALL_TARGET" not in text, (
        "the durable-target redirect must not be re-introduced: with the full "
        "extra set baked in it has no legitimate consumer, and it lets a "
        "container resolve unaudited dependencies at runtime"
    )

    # stage2-hook must not create or chown the target directory.
    stage2 = (REPO_ROOT / "docker" / "stage2-hook.sh").read_text()
    chown_list = stage2.split("for sub in", 1)[1].split(";", 1)[0]
    assert "lazy-packages" not in chown_list, (
        "lazy-packages is not an install target; remove it from the "
        "chown list that runs at each boot"
    )


def test_dockerfile_bakes_every_container_capable_extra() -> None:
    """The build must use --all-extras and remove only what cannot run here.

    A list of `--extra` flags omits each backend that a later commit adds.
    `--all-extras --no-extra ...` includes them. The image permits no install
    at run time, so a missing extra is a feature that the user cannot reach.
    """
    text = _dockerfile_text()

    assert "--all-extras" in text, (
        "the dependency sync must use --all-extras so new extras are included "
        "automatically"
    )

    # Excluded for a physical reason, each of which must stay excluded.
    for extra in ("dev", "termux-all", "voice", "wake"):
        assert f"--no-extra {extra}" in text, (
            f"[{extra}] must stay excluded from the image"
        )

    # silk is audio-ADJACENT but container-valid: pilk is a pure SILK codec
    # that decodes voice notes arriving over the network from WeChat / WeCom /
    # QQ Bot. It must NOT be lumped in with the microphone extras.
    assert "--no-extra silk" not in text, (
        "[silk] decodes network-delivered voice notes for gateway platforms "
        "this image serves — it needs no microphone and must be baked in"
    )


def test_dockerfile_bakes_photon_sidecar_deps() -> None:
    """The Photon sidecar's node_modules must be baked at build time (NS-606).

    The install tree is immutable at runtime, so a lazy `npm ci` on first
    connect would hit EROFS. Baking the deps (from the committed lockfile,
    which also runs the spectrum-ts postinstall patch) makes the hosted
    happy path install-free. Guards the contract between the Dockerfile
    and plugins/platforms/photon/sidecar_paths.resolve_sidecar_dir, which
    runs in place only when the baked deps exist and match the lockfile.
    """
    text = _dockerfile_text()

    assert "plugins/platforms/photon/sidecar/package-lock.json" in text
    assert re.search(
        r"RUN cd plugins/platforms/photon/sidecar && \\\n\s+npm ci", text
    ), "sidecar deps must be installed with `npm ci` (deterministic, runs postinstall patch)"
    # Immutability contract: never chown the sidecar tree to the runtime user.
    assert not re.search(
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/plugins", text
    )
