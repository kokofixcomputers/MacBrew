import asyncio
import os
import platform
import re
import json
import time
import subprocess
import shutil
import hashlib
import tempfile
import zipfile
import tarfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

APP_NAME = "macbrew"
CACHE_TTL_SECONDS = 60 * 60 * 12
FORMULA_INDEX_URL = "https://formulae.brew.sh/api/formula.json"
CASK_INDEX_URL = "https://formulae.brew.sh/api/cask.json"
FORMULA_RAW_BASE = "https://raw.githubusercontent.com/Homebrew/homebrew-core/refs/heads/main"
CASK_RAW_BASE = "https://raw.githubusercontent.com/Homebrew/homebrew-cask/main"
TIMEOUT = 45
CONFIG_DIR = Path.home() / f".{APP_NAME}"
CACHE_DIR = CONFIG_DIR / "cache"
DOWNLOAD_DIR = CONFIG_DIR / "downloads"
TAPS_DIR = CONFIG_DIR / "taps"


def expand_path(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


@lru_cache(maxsize=1)
def arch_name() -> str:
    m = platform.machine().lower()
    return "arm64" if m in {"arm64", "aarch64"} else "x86_64"


@lru_cache(maxsize=1)
def macos_codename() -> Optional[str]:
    try:
        version = platform.mac_ver()[0]
        major = int(version.split(".")[0]) if version else 0
    except Exception:
        return None
    return {16:"tahoe",15:"sequoia",14:"sonoma",13:"ventura",12:"monterey",11:"big_sur",10:"catalina"}.get(major)


def cleanup_pattern(pattern: str) -> List[Path]:
    pattern = os.path.expandvars(os.path.expanduser(pattern))
    if any(ch in pattern for ch in "*?[]"):
        base = Path(pattern).parent
        name = Path(pattern).name
        if base.exists():
            return list(base.glob(name))
        return []
    return [Path(pattern)]


def _is_fresh(path: Path, ttl: int = CACHE_TTL_SECONDS) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < ttl


def _fetch_text(session, url: str) -> str:
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _fetch_json(session, url: str) -> Any:
    r = session.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _load_text_cache(path: Path, fetcher) -> str:
    if _is_fresh(path):
        return path.read_text()
    text = fetcher()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return text


def _load_json_cache(path: Path, fetcher) -> Any:
    if _is_fresh(path):
        return json.loads(path.read_text())
    data = fetcher()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


async def read_text_async(path: Path) -> str:
    return await asyncio.to_thread(path.read_text)


async def write_text_async(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, content)


async def load_text_cache_async(path: Path, fetcher) -> str:
    if _is_fresh(path):
        return await read_text_async(path)
    text = await asyncio.to_thread(fetcher)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, text)
    return text


async def load_json_cache_async(path: Path, fetcher) -> Any:
    if _is_fresh(path):
        return json.loads(await read_text_async(path))
    data = await asyncio.to_thread(fetcher)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, json.dumps(data, indent=2) + "\n")
    return data
