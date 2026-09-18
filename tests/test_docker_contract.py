"""Packaging tests: the Docker contract and the solver's runtime dependencies.

These exist because the Docker image is the challenge's **fallback execution path**
if the hosted endpoint is unavailable, so a broken image costs both the fallback
and its rubric points. A container cannot be built in this environment (no Docker
daemon), so these tests verify the parts that *are* machine-checkable from outside:

  * the CBC binary the image will run really exists in the pulp wheel
  * every non-glibc shared library CBC declares is provided by an apt package the
    Dockerfile actually installs
  * every path the Dockerfile COPYs exists in the build context
  * the image config matches the challenge requirements (0.0.0.0, $PORT, no
    secrets, non-root, EXPOSE)

The library check is the important one. The Dockerfile originally installed
``libgomp1`` on the assumption that CBC needs OpenMP; it does not. CBC needs
``libstdc++6``, which ``python:3.12-slim`` does not ship (that image installs only
ca-certificates, netbase and tzdata, then purges its build toolchain). The solver
would therefore fail to start inside the image.

Run: python tests/test_docker_contract.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

# Map a shared-library soname to the Debian package that provides it.
# Only non-glibc entries need to be listed: glibc libraries (libc, libm,
# libpthread, librt, libdl) are always present in any Debian image.
SO_TO_PACKAGE = {
    "libstdc++.so.6": "libstdc++6",
    "libgcc_s.so.1": "libgcc-s1",
    "libgomp.so.1": "libgomp1",
    "libatomic.so.1": "libatomic1",
    "libquadmath.so.0": "libquadmath0",
}

# Packages that arrive as dependencies of the ones named in apt-get install, so
# they do not need to be listed explicitly. libgcc-s1 is a hard dependency of
# libstdc++6 on Debian.
TRANSITIVE_PACKAGES = {
    "libgcc-s1": {"libstdc++6"},
}

# glibc sonames that are always available and therefore never need installing.
GLIBC_ALWAYS_PRESENT = {
    "libc.so.6",
    "libm.so.6",
    "libpthread.so.0",
    "librt.so.1",
    "libdl.so.2",
    "ld-linux-x86-64.so.2",
}


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _apt_packages_installed() -> set[str]:
    """Every package named in an `apt-get install` in the Dockerfile."""
    packages: set[str] = set()
    for line in _dockerfile_text().splitlines():
        stripped = line.strip().lstrip("&").strip()
        if not stripped.startswith("apt-get install"):
            continue
        for token in stripped.split()[2:]:
            token = token.strip("\\").strip()
            if token.startswith("-") or not token:
                continue
            packages.add(token)
    return packages


def _cbc_path() -> Path | None:
    """Locate the linux x86-64 CBC binary inside the installed pulp wheel."""
    try:
        import pulp
    except ImportError:
        return None
    candidate = Path(pulp.__file__).parent / "solverdir" / "cbc" / "linux" / "i64" / "cbc"
    return candidate if candidate.exists() else None


def _needed_libraries(binary: Path) -> set[str]:
    """Extract DT_NEEDED-style sonames from a binary without running it.

    The container-appropriate entry points are unavailable here, so the strings
    are read directly. This is deliberately conservative: any ``lib*.so*``
    token is treated as a dependency.
    """
    data = binary.read_bytes()
    found = set()
    for match in re.finditer(rb"lib[A-Za-z0-9_+.\-]*\.so(\.[0-9]+)*", data):
        token = match.group(0).decode("ascii", errors="ignore")
        # Keep the canonical soname form (e.g. libstdc++.so.6), dropping longer
        # versioned suffixes that occasionally appear in the vDSO strings table.
        if token.count(".so") == 1 and token.endswith(tuple("0123456789")):
            found.add(token)
    return found


# --------------------------------------------------------------------------- #
# Solver runtime dependencies
# --------------------------------------------------------------------------- #
def test_cbc_binary_ships_in_the_pulp_wheel():
    """The image relies on CBC being inside the wheel, not on a system solver."""
    cbc = _cbc_path()
    assert cbc is not None, (
        "linux/i64/cbc not found in the pulp wheel; the Docker image would have no solver"
    )
    assert cbc.stat().st_size > 1_000_000, "CBC binary looks truncated"


def test_every_cbc_library_dependency_is_installed_by_the_dockerfile():
    """No non-glibc library CBC needs may be missing from the image.

    This is the check that catches the libgomp1 mistake: the package installed
    must actually provide a library CBC declares.
    """
    cbc = _cbc_path()
    if cbc is None:
        print("  (skipped: CBC binary not present in this environment)")
        return

    needed = _needed_libraries(cbc)
    non_glibc = needed - GLIBC_ALWAYS_PRESENT

    # apt-get install names packages directly (libstdc++6), not sonames.
    installed = _apt_packages_installed()

    missing = []
    for so in sorted(non_glibc):
        provider = SO_TO_PACKAGE.get(so)
        if provider is None:
            missing.append(f"{so} (unknown Debian package)")
            continue
        if provider in installed:
            continue
        # Accept it if a package we do install pulls it in as a dependency.
        pulled_in_by = TRANSITIVE_PACKAGES.get(provider, set())
        if pulled_in_by & installed:
            continue
        missing.append(f"{so} -> {provider} (not installed, and not a dependency)")

    assert not missing, (
        "CBC needs shared libraries the Dockerfile does not install:\n  "
        + "\n  ".join(missing)
        + f"\n  Dockerfile installs: {sorted(installed)}"
    )


def test_libstdcxx_is_installed_for_cbc():
    """Directly pin the dependency that python:3.12-slim does not ship."""
    cbc = _cbc_path()
    if cbc is None:
        print("  (skipped: CBC binary not present)")
        return

    needed = _needed_libraries(cbc)
    if "libstdc++.so.6" not in needed:
        print("  (CBC no longer links libstdc++ -- revisit this test)")
        return

    installed = _apt_packages_installed()
    assert "libstdc++6" in installed, (
        "CBC links libstdc++.so.6, which python:3.12-slim does not provide; "
        f"the Dockerfile must install libstdc++6 (found: {sorted(installed)})"
    )


# --------------------------------------------------------------------------- #
# Build context
# --------------------------------------------------------------------------- #
def test_every_dockerfile_copy_source_exists():
    """A COPY of a missing path fails the build.

    Sources pulled from an earlier stage (``COPY --from=builder ...``) live
    inside the build, not on the host, so they are skipped here.
    """
    missing = []
    for line in _dockerfile_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY"):
            continue
        parts = stripped.split()
        if any(p.startswith("--from=") for p in parts):
            continue
        args = [p for p in parts[1:] if not p.startswith("--")]
        if len(args) < 2:
            continue
        for src in args[:-1]:  # last token is the destination
            if not (REPO_ROOT / src).exists():
                missing.append(src)
    assert not missing, f"COPY sources missing from the build context: {missing}"


def test_dockerfile_binds_all_interfaces_and_honours_port():
    """The challenge requires 0.0.0.0 and the documented port."""
    text = _dockerfile_text()
    assert "--host 0.0.0.0" in text, "service must bind 0.0.0.0 to be reachable"
    assert "${PORT}" in text or "$PORT" in text, "service must honour $PORT"
    assert "EXPOSE" in text, "exposed port should be documented"


def test_dockerfile_runs_as_non_root():
    text = _dockerfile_text()
    assert re.search(r"^USER\s+(?!root)\S+", text, re.MULTILINE), (
        "image should not run as root"
    )


# --------------------------------------------------------------------------- #
# Secret hygiene
# --------------------------------------------------------------------------- #
def test_env_is_excluded_from_the_build_context():
    """The image must contain no baked-in credentials."""
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8") if DOCKERIGNORE.exists() else ""
    text = _dockerfile_text()

    copies_dot = re.search(r"^COPY\s+\.\s", text, re.MULTILINE) is not None

    if copies_dot:
        # Only a whitelist-style .dockerignore can protect a `COPY . .`.
        assert re.search(r"^\.env$", dockerignore, re.MULTILINE), (
            "Dockerfile uses `COPY . .` but .dockerignore does not exclude .env"
        )
    else:
        # Explicit COPY paths: verify .env is not among them.
        for line in text.splitlines():
            if line.strip().startswith("COPY") and ".env" in line:
                raise AssertionError(f"COPY pulls a .env into the image: {line.strip()}")

    assert re.search(r"^\.env$", dockerignore, re.MULTILINE), (
        ".dockerignore should exclude .env as defence in depth"
    )


def test_no_credential_is_passed_as_a_build_arg():
    """Secrets must arrive at run time (-e), never at build time."""
    text = _dockerfile_text()
    for match in re.finditer(r"^ARG\s+(\w+)", text, re.MULTILINE):
        name = match.group(1).upper()
        assert not any(word in name for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")), (
            f"ARG {match.group(1)} looks like a credential baked into the image"
        )


def test_dockerignore_does_not_exclude_required_paths():
    """An over-broad ignore file can silently break the build."""
    if not DOCKERIGNORE.exists():
        print("  (skipped: no .dockerignore)")
        return
    patterns = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    required = ["app", "tests", "pdf", "README.md", "requirements.txt"]
    for name in required:
        for pattern in patterns:
            normalised = pattern.lstrip("!").rstrip("/")
            assert normalised != name, (
                f".dockerignore pattern {pattern!r} would exclude required path {name!r}"
            )


def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL] {name}: {exc}")
        except Exception as exc:  # pragma: no cover
            failures += 1
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
