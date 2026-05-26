import asyncio
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from macbrew_utils import TAPS_DIR, expand_path

@lru_cache(maxsize=128)
def normalize_tap_input(value: str) -> Tuple[str, str, str, str]:
    raw = value.strip()
    if raw.startswith("http://github.com/"):
        raw = "https://" + raw[len("http://"):]
    if raw.startswith("https://github.com/"):
        parsed = raw.rstrip("/")
        parts = parsed.split("github.com/", 1)[1].split("/")
        owner = parts[0]
        repo = parts[1]
        return parsed, owner, repo, f"{owner}/{repo}"
    if "/" not in raw:
        raise ValueError("Tap must be github url or owner/repo")
    owner, repo = raw.split("/", 1)
    if not repo.startswith("homebrew-"):
        repo = f"homebrew-{repo}"
    url = f"https://github.com/{owner}/{repo}"
    return url, owner, repo, f"{owner}/{repo}"


def tap_local_dir(owner: str, repo: str) -> Path:
    return TAPS_DIR / owner / repo


@lru_cache(maxsize=64)
def tap_branch(local: Path) -> str:
    try:
        out = subprocess.check_output(["git", "-C", str(local), "branch", "--show-current"], text=True).strip()
        return out or "HEAD"
    except Exception:
        return "HEAD"


def ensure_tap_repo(value: str) -> Tuple[Path, str, str, str]:
    url, owner, repo, short = normalize_tap_input(value)
    local = tap_local_dir(owner, repo)
    if local.exists():
        return local, url, owner, repo
    local.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", url, str(local)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tap_branch.cache_clear()
    return local, url, owner, repo


async def ensure_tap_repo_async(value: str) -> Tuple[Path, str, str, str]:
    return await asyncio.to_thread(ensure_tap_repo, value)
