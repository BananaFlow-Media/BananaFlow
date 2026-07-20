"""Single source of truth for the BananaFlow application version.

Every module that needs to surface a version string should import from
here. The packaging build script also reads this module directly so the
EXE VS_VERSIONINFO and Inno Setup metadata stay in sync.

The version is expressed in four coordinated representations:

  * ``__version__``    — the numeric SemVer core (``MAJOR.MINOR.PATCH``).
  * ``VERSION_INFO``   — the same core as an int tuple, for numeric
    comparisons and the Windows ``FixedFileInfo`` block.
  * ``FULL_VERSION``   — the public SemVer string including any
    pre-release suffix (e.g. ``1.0.0-beta.1``). Used for Git tags,
    artifact names, the updater, the About dialog and ``--version``.
  * ``PEP440_VERSION`` — the PEP 440 equivalent (e.g. ``1.0.0b1``) used
    by ``pyproject.toml`` and Python package metadata.

A drift guard lives in ``tests/test_p0_gates.py::TestVersionConsistency``
that fails if any of the following diverge:
  * ``version.__version__`` / ``FULL_VERSION`` / ``PEP440_VERSION``
  * ``pyproject.toml`` [project] version
  * ``core.update_checker.CURRENT_VERSION``
  * the Qt application version set in ``main.py``

Bump ``__version__`` (and ``PRERELEASE`` when applicable) when cutting a
new release; every other representation derives from them.
"""

from __future__ import annotations

__version__: str = "1.0.0"

# Convenience tuple form for code that needs to compare versions
# numerically without parsing the string. Also feeds the numeric
# fields of the Windows VS_VERSIONINFO block.
VERSION_INFO: tuple[int, int, int] = (1, 0, 0)

# Pre-release suffix in SemVer notation, or None for a stable release.
# Examples: "beta.1", "beta.2", "rc.1".
PRERELEASE: str | None = "beta.1"

# Public SemVer string: tags (v-prefixed), artifact names, updater,
# About dialog, CLI --version.
FULL_VERSION: str = f"{__version__}-{PRERELEASE}" if PRERELEASE else __version__


def _pep440(core: str, prerelease: str | None) -> str:
    """Derive the PEP 440 form of the version.

    SemVer pre-release labels map onto PEP 440 segments:
    ``beta.N`` -> ``bN``, ``alpha.N`` -> ``aN``, ``rc.N`` -> ``rcN``.
    """
    if prerelease is None:
        return core
    label, _, number = prerelease.partition(".")
    seg = {"alpha": "a", "beta": "b", "rc": "rc"}[label]
    return f"{core}{seg}{number or '0'}"


# PEP 440 string used by pyproject.toml and package metadata.
PEP440_VERSION: str = _pep440(__version__, PRERELEASE)

# Stable name used in product metadata (EXE description, Qt
# setApplicationName, MusicBrainz User-Agent, etc.).
PRODUCT_NAME: str = "BananaFlow"

# Publisher display string for Windows EXE metadata and Inno Setup.
# BananaFlow Media is the product brand and publisher display name.
COMPANY_NAME: str = "BananaFlow Media"

# Copyright line used in the EXE version-info block. Year is updated
# alongside __version__ on each release.
COPYRIGHT: str = "Copyright © 2026 Chaim Dov Tauman"
