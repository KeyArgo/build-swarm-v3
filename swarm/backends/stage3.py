"""
Gentoo stage3 tarball download and cache management.

Downloads and caches stage3 tarballs from Gentoo mirrors.
Pure stdlib implementation using urllib.request.
"""

import os
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Callable, Optional

MIRROR_URL = "https://distfiles.gentoo.org"
LATEST_PATH = "/releases/amd64/autobuilds/latest-stage3-amd64-openrc.txt"
CACHE_MAX_AGE_DAYS = 7
READ_BUFFER = 8192


def get_cache_dir():
    # type: () -> Path
    """Return the stage3 cache directory, creating it if necessary.

    Respects the STAGE3_CACHE_DIR environment variable.
    Defaults to /var/cache/build-swarm-v3/stage3/ (root) or
    ~/.cache/build-swarm-v3/stage3/ (user).
    """
    env_dir = os.environ.get("STAGE3_CACHE_DIR")
    if env_dir:
        path = Path(env_dir)
    elif os.getuid() == 0:
        path = Path("/var/cache/build-swarm-v3/stage3")
    else:
        path = Path(os.path.expanduser("~/.cache/build-swarm-v3/stage3"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def find_cached_stage3(cache_dir=None):
    # type: (Optional[Path]) -> Optional[Path]
    """Find a cached .tar.xz stage3 tarball that is less than 7 days old.

    Args:
        cache_dir: Directory to search. Defaults to get_cache_dir().

    Returns:
        Path to the cached tarball, or None if no valid cache exists.
    """
    if cache_dir is None:
        cache_dir = get_cache_dir()

    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return None

    now = time.time()
    max_age_seconds = CACHE_MAX_AGE_DAYS * 86400
    best_path = None  # type: Optional[Path]
    best_mtime = 0.0

    for entry in cache_dir.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.endswith(".tar.xz"):
            continue
        if "stage3" not in entry.name:
            continue

        mtime = entry.stat().st_mtime
        age = now - mtime
        if age < max_age_seconds and mtime > best_mtime:
            best_path = entry
            best_mtime = mtime

    return best_path


def parse_latest_url(mirror_url=None):
    # type: (Optional[str]) -> str
    """Fetch and parse the latest stage3 tarball URL from a Gentoo mirror.

    The latest-stage3-amd64-openrc.txt file contains comment lines (starting
    with #) and data lines in the format:
        20260201T170000Z/stage3-amd64-openrc-20260201T170000Z.tar.xz 280000000

    Args:
        mirror_url: Base mirror URL. Defaults to MIRROR_URL.

    Returns:
        Full URL to the latest stage3 tarball.

    Raises:
        RuntimeError: If the latest file cannot be fetched or parsed.
    """
    if mirror_url is None:
        mirror_url = MIRROR_URL

    mirror_url = mirror_url.rstrip("/")
    url = mirror_url + LATEST_PATH

    try:
        response = urllib.request.urlopen(url, timeout=30)
        data = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Failed to fetch latest stage3 listing from {}: {}".format(url, exc)
        )
    except Exception as exc:
        raise RuntimeError(
            "Unexpected error fetching {}: {}".format(url, exc)
        )

    relative_path = None  # type: Optional[str]
    for line in data.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Data line: relative_path size [optional fields]
        parts = line.split()
        if len(parts) >= 1 and parts[0].endswith(".tar.xz"):
            relative_path = parts[0]
            break

    if relative_path is None:
        raise RuntimeError(
            "Could not parse stage3 tarball path from {}. "
            "Content was:\n{}".format(url, data[:500])
        )

    full_url = "{}/releases/amd64/autobuilds/{}".format(
        mirror_url, relative_path
    )
    return full_url


def download_stage3(cache_dir=None, progress_callback=None):
    # type: (Optional[Path], Optional[Callable[[int, int], None]]) -> Path
    """Download the latest stage3 tarball, using cache if available.

    Args:
        cache_dir: Directory to store the tarball. Defaults to get_cache_dir().
        progress_callback: Optional callback(bytes_done, bytes_total) invoked
            during download to report progress.

    Returns:
        Path to the downloaded (or cached) tarball.

    Raises:
        RuntimeError: If the download fails.
    """
    if cache_dir is None:
        cache_dir = get_cache_dir()
    else:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)

    # Check cache first
    cached = find_cached_stage3(cache_dir)
    if cached is not None:
        return cached

    # Resolve the latest tarball URL
    tarball_url = parse_latest_url()

    # Extract filename from URL
    filename = tarball_url.rsplit("/", 1)[-1]
    dest_path = cache_dir / filename
    temp_path = cache_dir / (filename + ".part")

    try:
        response = urllib.request.urlopen(tarball_url, timeout=30)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Failed to connect to mirror for download: {}".format(exc)
        )
    except Exception as exc:
        raise RuntimeError(
            "Unexpected error connecting to {}: {}".format(tarball_url, exc)
        )

    # Determine total size from Content-Length header
    content_length = response.headers.get("Content-Length")
    bytes_total = int(content_length) if content_length else 0
    bytes_done = 0

    try:
        with open(str(temp_path), "wb") as fout:
            while True:
                chunk = response.read(READ_BUFFER)
                if not chunk:
                    break
                fout.write(chunk)
                bytes_done += len(chunk)
                if progress_callback is not None:
                    progress_callback(bytes_done, bytes_total)
    except Exception as exc:
        # Clean up partial download
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise RuntimeError(
            "Download of {} failed after {} bytes: {}".format(
                tarball_url, bytes_done, exc
            )
        )

    # Rename temp file to final destination
    try:
        temp_path.rename(dest_path)
    except OSError:
        # On some systems rename across filesystems fails; fall back to copy
        import shutil
        shutil.move(str(temp_path), str(dest_path))

    return dest_path
