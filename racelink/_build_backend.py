"""Minimal stdlib-only build backend for local RaceLink packaging."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import os
from pathlib import Path
import tarfile
import time
import zipfile

from racelink._version import VERSION


NAME = "racelink-host"
SUMMARY = "Host-side software for the RaceLink wireless control system."
REQUIRES_PYTHON = ">=3.10"
DEPENDENCIES = (
    "Flask>=3.0,<4",
    "pyserial>=3.5,<4",
)
PROJECT_URL = "Repository, https://github.com/PSi86/RaceLink_Host"
ENTRY_POINTS = {
    "console_scripts": [
        "racelink-standalone = racelink.integrations.standalone.webapp:run_standalone",
        "racelink-host-version = racelink._version:print_version",
    ]
}

ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_BUILD_EPOCH = 315532800  # 1980-01-01T00:00:00Z


def _build_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is None:
        return _DEFAULT_BUILD_EPOCH
    try:
        return max(int(raw), 0)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be an integer") from exc


def _wheel_timestamp() -> tuple[int, int, int, int, int, int]:
    timestamp = max(_build_epoch(), _DEFAULT_BUILD_EPOCH)
    return time.gmtime(timestamp)[:6]


def _package_data_sources() -> list[tuple[Path, str]]:
    """Walk the package's static/ tree for files to include in the wheel.

    Phase 1 PoC removed the legacy ``racelink/pages/`` Jinja templates;
    the SPA shell now lives at ``racelink/static/dist/index.html`` and
    its hashed JS/CSS bundles under ``racelink/static/dist/assets/``.
    Both are discovered by the recursive walk over ``static/``.

    Filtered (not needed at runtime, would only bloat the wheel):

    * ``*.map`` — JavaScript / CSS source maps. Browser DevTools
      consume them when stepping through the bundle, irrelevant to
      the Flask-served SPA. Dropping the four maps in the 2026-05-22
      dist saves ~3.3 MB / ≈ 62 % of the wheel size.
    * ``.vite/`` directory — Vite's build manifest, used by SSR / dev
      tooling. The production ``index.html`` already references the
      hashed asset paths directly.
    """
    sources: list[tuple[Path, str]] = []
    package_root = ROOT / "racelink"
    static_root = package_root / "static"
    for path in sorted(static_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".map":
            continue
        rel = path.relative_to(static_root)
        if ".vite" in rel.parts:
            continue
        sources.append((path, f"racelink/static/{rel.as_posix()}"))
    return sources


def _dist_info_dir() -> str:
    return f"{NAME.replace('-', '_')}-{VERSION}.dist-info"


def _wheel_name() -> str:
    return f"{NAME.replace('-', '_')}-{VERSION}-py3-none-any.whl"


def _sdist_name() -> str:
    return f"{NAME}-{VERSION}.tar.gz"


def _metadata_text() -> str:
    lines = [
        "Metadata-Version: 2.1",
        f"Name: {NAME}",
        f"Version: {VERSION}",
        f"Summary: {SUMMARY}",
        f"Requires-Python: {REQUIRES_PYTHON}",
        f"Project-URL: {PROJECT_URL}",
    ]
    for requirement in DEPENDENCIES:
        lines.append(f"Requires-Dist: {requirement}")
    return "\n".join(lines) + "\n"


def _wheel_text() -> str:
    return "\n".join(
        [
            "Wheel-Version: 1.0",
            "Generator: racelink._build_backend",
            "Root-Is-Purelib: true",
            "Tag: py3-none-any",
            "",
        ]
    )


def _entry_points_text() -> str:
    lines: list[str] = []
    for group, entries in ENTRY_POINTS.items():
        lines.append(f"[{group}]")
        lines.extend(entries)
        lines.append("")
    return "\n".join(lines)


def _top_level_text() -> str:
    return "racelink\n"


def _iter_sources(*, include_build_backend: bool) -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    # ``_build_backend.py`` is only consumed by the PEP-517 build hook —
    # at runtime nobody imports it, so the wheel (the deployed install)
    # skips it to avoid shipping build-time code. The sdist MUST keep it:
    # ``python -m build`` builds the wheel *from the sdist*, and that step
    # re-imports this backend (per pyproject.toml's ``build-backend``).
    # Dropping it from the sdist makes the sdist unbuildable, surfacing as
    # ``BackendUnavailable: Cannot import 'racelink._build_backend'``.
    for path in sorted((ROOT / "racelink").rglob("*.py")):
        if path.name == "_build_backend.py" and not include_build_backend:
            continue
        sources.append((path, path.relative_to(ROOT).as_posix()))
    sources.extend(_package_data_sources())
    return sources


def _record_line(rel_path: str, data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"{rel_path},sha256={encoded},{len(data)}"


def _metadata_files() -> dict[str, bytes]:
    dist_info = _dist_info_dir()
    return {
        f"{dist_info}/METADATA": _metadata_text().encode("utf-8"),
        f"{dist_info}/WHEEL": _wheel_text().encode("utf-8"),
        f"{dist_info}/entry_points.txt": _entry_points_text().encode("utf-8"),
        f"{dist_info}/top_level.txt": _top_level_text().encode("utf-8"),
    }


def _write_metadata_dir(target: Path) -> str:
    dist_info = target / _dist_info_dir()
    dist_info.mkdir(parents=True, exist_ok=True)
    for rel_path, data in _metadata_files().items():
        (target / rel_path).write_bytes(data)
    return dist_info.name


def _wheel_zip_info(rel_path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(rel_path, date_time=_wheel_timestamp())
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _tar_info(rel_path: str, data: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(rel_path)
    info.size = len(data)
    info.mtime = _build_epoch()
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None) -> str:
    del config_settings, metadata_directory
    wheel_dir = Path(wheel_directory)
    wheel_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = wheel_dir / _wheel_name()

    entries: list[tuple[str, bytes]] = []
    for src_path, rel_path in _iter_sources(include_build_backend=False):
        entries.append((rel_path, src_path.read_bytes()))
    entries.extend(_metadata_files().items())

    records = [_record_line(rel_path, data) for rel_path, data in entries]
    record_path = f"{_dist_info_dir()}/RECORD"
    record_bytes = ("\n".join(records + [f"{record_path},,"]) + "\n").encode("utf-8")
    entries.append((record_path, record_bytes))

    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel_path, data in entries:
            archive.writestr(_wheel_zip_info(rel_path), data)

    return wheel_path.name


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None) -> str:
    del config_settings
    return _write_metadata_dir(Path(metadata_directory))


def get_requires_for_build_wheel(config_settings=None) -> list[str]:
    del config_settings
    return []


def build_sdist(sdist_directory, config_settings=None) -> str:
    del config_settings
    sdist_dir = Path(sdist_directory)
    sdist_dir.mkdir(parents=True, exist_ok=True)
    sdist_name = _sdist_name()
    sdist_path = sdist_dir / sdist_name
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        entries = list(_iter_sources(include_build_backend=True))
        entries.extend(
            [
                (ROOT / "pyproject.toml", "pyproject.toml"),
                (ROOT / "README.md", "README.md"),
            ]
        )
        for src_path, rel_path in entries:
            arcname = f"{NAME}-{VERSION}/{rel_path}"
            data = src_path.read_bytes()
            archive.addfile(_tar_info(arcname, data), io.BytesIO(data))
    with sdist_path.open("wb") as file_handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=file_handle, mtime=_build_epoch()) as gzip_handle:
            gzip_handle.write(tar_buffer.getvalue())
    return sdist_path.name
