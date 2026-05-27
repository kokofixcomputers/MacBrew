from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from macbrew_utils import (
    APP_NAME,
    CACHE_TTL_SECONDS,
    FORMULA_INDEX_URL,
    CASK_INDEX_URL,
    FORMULA_RAW_BASE,
    CASK_RAW_BASE,
    TIMEOUT,
)

CONFIG_DIR = Path.home() / f".{APP_NAME}"
CACHE_DIR = CONFIG_DIR / "cache"
DOWNLOAD_DIR = CONFIG_DIR / "downloads"
TAPS_DIR = CONFIG_DIR / "taps"
METADATA_DIR = CONFIG_DIR / "metadata"
CONFIG_PATH = CONFIG_DIR / "config.json"
METADATA_FILE = ".macbrew-meta.json"
DEFAULT_CONFIG = {
    "install_root": "/Applications",
    "formula_prefix": "/opt/macbrew",
    "cache_ttl_seconds": CACHE_TTL_SECONDS,
    "auto_refresh": True,
    "prefer_local_applications": False,
}
console = Console()


class MacbrewError(Exception):
    pass


@dataclass
class SearchResult:
    kind: str
    token: str
    name: str
    desc: str
    homepage: str
    score: float
