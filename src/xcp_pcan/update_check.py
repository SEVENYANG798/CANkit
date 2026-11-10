"""GitHub Releases update check + download for frozen CANkit.exe."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from xcp_pcan import __version__

logger = logging.getLogger(__name__)

# Override with env CANKIT_UPDATE_REPO=owner/name if needed.
DEFAULT_REPO = "SEVENYANG798/CANkit"
UPDATE_ASSET = "CANkit.exe"
USER_AGENT = f"CANkit/{__version__}"
TIMEOUT_S = 12


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    version: str
    tag: str
    notes: str
    download_url: str
    size: int
    sha256_url: str | None = None


def update_repo() -> str:
    return (os.environ.get("CANKIT_UPDATE_REPO") or DEFAULT_REPO).strip()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> Path | None:
    if not is_frozen():
        return None
    return Path(sys.executable).resolve()


def _parse_version(text: str) -> tuple[int, ...]:
    cleaned = text.strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return _parse_version(remote) > _parse_version(local)


def check_for_update() -> UpdateInfo | None:
    """Fetch GitHub latest release. Returns None if up-to-date / offline / misconfigured."""
    repo = update_repo()
    if not repo or "PLACEHOLDER" in repo or "/" not in repo:
        logger.debug("Update repo not configured: %r", repo)
        return None

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        logger.info("Update check skipped: %s", exc)
        return None

    tag = str(payload.get("tag_name") or "")
    version = tag.lstrip("vV") or str(payload.get("name") or "")
    if not version or not is_newer(version):
        return None

    assets = payload.get("assets") or []
    download_url = ""
    size = 0
    sha256_url: str | None = None
    for asset in assets:
        name = str(asset.get("name") or "")
        if name == UPDATE_ASSET:
            download_url = str(asset.get("browser_download_url") or "")
            size = int(asset.get("size") or 0)
        elif name in (f"{UPDATE_ASSET}.sha256", "CANkit.exe.sha256"):
            sha256_url = str(asset.get("browser_download_url") or "") or None
    if not download_url:
        logger.info("Update %s found but no %s asset", version, UPDATE_ASSET)
        return None

    notes = str(payload.get("body") or "").strip()
    return UpdateInfo(
        version=version,
        tag=tag or f"v{version}",
        notes=notes,
        download_url=download_url,
        size=size,
        sha256_url=sha256_url,
    )


def _download(url: str, dest: Path, *, expected_size: int = 0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
    if expected_size > 0 and dest.stat().st_size != expected_size:
        raise OSError(
            f"download size mismatch: got {dest.stat().st_size}, expected {expected_size}"
        )


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 256)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _read_remote_sha256(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        text = resp.read().decode("utf-8", errors="replace").strip()
    # Accept "hash" or "hash  filename"
    return text.split()[0].lower()


def download_update(info: UpdateInfo, dest: Path) -> Path:
    """Download release asset to ``dest`` (usually *.exe.new)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    _download(info.download_url, tmp, expected_size=info.size)
    if info.sha256_url:
        expect = _read_remote_sha256(info.sha256_url)
        got = _file_sha256(tmp)
        if got != expect:
            tmp.unlink(missing_ok=True)
            raise OSError(f"SHA256 mismatch: got {got}, expected {expect}")
    if dest.exists():
        dest.unlink()
    tmp.replace(dest)
    return dest


def write_apply_script(*, current_exe: Path, new_exe: Path) -> Path:
    """Write a cmd script that replaces the running exe after exit, then relaunches."""
    script = Path(tempfile.gettempdir()) / "cankit_apply_update.cmd"
    # Wait for CANkit.exe to exit, then move new over old and start.
    lines = [
        "@echo off",
        "setlocal",
        f'set "TARGET={current_exe}"',
        f'set "NEWFILE={new_exe}"',
        "echo Applying CANkit update...",
        ":wait",
        'ping -n 2 127.0.0.1 >nul',
        'del "%TARGET%" >nul 2>&1',
        'if exist "%TARGET%" goto wait',
        'move /Y "%NEWFILE%" "%TARGET%" >nul',
        'if errorlevel 1 (',
        '  echo Failed to replace CANkit.exe',
        '  pause',
        '  exit /b 1',
        ")",
        'start "" "%TARGET%"',
        'del "%~f0" >nul 2>&1',
    ]
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return script
