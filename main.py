#!/usr/bin/env python3
import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from InquirerPy import inquirer
from InquirerPy.validator import PathValidator
from rapidfuzz import fuzz
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from macbrew_utils import (
    APP_NAME,
    CACHE_TTL_SECONDS,
    FORMULA_INDEX_URL,
    CASK_INDEX_URL,
    FORMULA_RAW_BASE,
    CASK_RAW_BASE,
    TIMEOUT,
    expand_path,
    arch_name,
    macos_codename,
    cleanup_pattern,
)
from macbrew_parsers import parse_formula_rb, parse_cask_rb
from macbrew_taps import ensure_tap_repo, tap_branch, normalize_tap_input


CONFIG_DIR = Path.home() / f".{APP_NAME}"
CACHE_DIR = CONFIG_DIR / "cache"
DOWNLOAD_DIR = CONFIG_DIR / "downloads"
TAPS_DIR = CONFIG_DIR / "taps"
CONFIG_PATH = CONFIG_DIR / "config.json"
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


class Macbrew:
    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        TAPS_DIR.mkdir(parents=True, exist_ok=True)
        self.config = self._load_config()
        self._installing: set = set()
        self._index_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._formula_search_items: List[Tuple[Dict[str, Any], List[str], str]] = []
        self._cask_search_items: List[Tuple[Dict[str, Any], List[str], str]] = []
        self._tap_items_cache: Optional[List[Dict[str, Any]]] = None

    def _load_config(self) -> Dict[str, Any]:
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
            return dict(DEFAULT_CONFIG)
        data = json.loads(CONFIG_PATH.read_text())
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged

    def save_config(self) -> None:
        CONFIG_PATH.write_text(json.dumps(self.config, indent=2) + "\n")

    def _run(self, coro):
        return asyncio.run(coro)

    def _get_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": f"{APP_NAME}/0.1"},
            timeout=TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    def _cache_file(self, kind: str) -> Path:
        return CACHE_DIR / f"{kind}.json"

    def _detail_cache_file(self, kind: str, token: str) -> Path:
        safe = token.replace("/", "_").replace("@", "_")
        return CACHE_DIR / f"{kind}-{safe}.rb"

    def _is_fresh(self, path: Path) -> bool:
        ttl = int(self.config.get("cache_ttl_seconds", CACHE_TTL_SECONDS))
        return path.exists() and (time.time() - path.stat().st_mtime) < ttl

    async def _fetch_text_async(self, url: str) -> str:
        async with httpx.AsyncClient(
            headers={"User-Agent": f"{APP_NAME}/0.1"},
            timeout=TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def _fetch_json_async(self, url: str, cache_path: Path, force: bool = False) -> Any:
        if not force and self._is_fresh(cache_path):
            return json.loads(await asyncio.to_thread(cache_path.read_text))
        async with httpx.AsyncClient(
            headers={"User-Agent": f"{APP_NAME}/0.1"},
            timeout=TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(cache_path.write_text, json.dumps(data, indent=2) + "\n")
        return data

    async def refresh(self, force: bool = False) -> None:
        await asyncio.gather(
            self._fetch_json_async(FORMULA_INDEX_URL, self._cache_file("formula"), force=force),
            self._fetch_json_async(CASK_INDEX_URL, self._cache_file("cask"), force=force),
        )
        self._index_cache.clear()

    def _load_index(self, kind: str) -> List[Dict[str, Any]]:
        if kind in self._index_cache:
            return self._index_cache[kind]
        path = self._cache_file(kind)
        if not path.exists() or (self.config.get("auto_refresh", True) and not self._is_fresh(path)):
            try:
                asyncio.get_running_loop()
                running = True
            except RuntimeError:
                running = False

            if running:
                # We're already inside an event loop (e.g. called from an async context).
                # Use a synchronous HTTP fetch here to avoid calling asyncio.run()
                url = FORMULA_INDEX_URL if kind == "formula" else CASK_INDEX_URL
                try:
                    client = httpx.Client(
                        headers={"User-Agent": f"{APP_NAME}/0.1"},
                        timeout=TIMEOUT,
                        follow_redirects=True,
                    )
                    resp = client.get(url)
                    resp.raise_for_status()
                    data = resp.json()
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, indent=2) + "\n")
            else:
                self._run(self.refresh(force=False))
                data = json.loads(path.read_text())
        else:
            # Cache exists and is fresh (or auto_refresh disabled) — read it
            data = json.loads(path.read_text())
        self._index_cache[kind] = data
        self._prepare_search_cache(kind, data)
        return data

    def _prepare_search_cache(self, kind: str, data: List[Dict[str, Any]]) -> None:
        if kind == "formula":
            self._formula_search_items = []
            for item in data:
                name = item.get("name", "")
                aliases = item.get("aliases", []) or []
                candidates = [name, *aliases]
                normalized = [c.lower() for c in candidates if c]
                desc = (item.get("desc", "") or "").lower()
                self._formula_search_items.append((item, normalized, desc))
        else:
            self._cask_search_items = []
            for item in data:
                token = item.get("token", "")
                names = item.get("name", []) or []
                old_tokens = item.get("old_tokens", []) or []
                candidates = [token, *names, *old_tokens]
                normalized = [c.lower() for c in candidates if c]
                desc = (item.get("desc", "") or "").lower()
                self._cask_search_items.append((item, normalized, desc))

    def _score_formula(self, candidates: List[str], desc: str, query: str) -> float:
        q = query.lower().strip()
        best_name = max((fuzz.WRatio(q, c) for c in candidates if c), default=0)
        desc_score = fuzz.partial_ratio(q, desc) if desc else 0
        prefix_bonus = 15 if any(c.startswith(q) for c in candidates) else 0
        exact_bonus = 20 if any(c == q for c in candidates) else 0
        return best_name * 0.7 + desc_score * 0.3 + prefix_bonus + exact_bonus

    def _score_cask(self, candidates: List[str], desc: str, query: str) -> float:
        q = query.lower().strip()
        best_name = max((fuzz.WRatio(q, c) for c in candidates if c), default=0)
        desc_score = fuzz.partial_ratio(q, desc) if desc else 0
        prefix_bonus = 15 if any(c.startswith(q) for c in candidates) else 0
        exact_bonus = 20 if any(c == q for c in candidates) else 0
        return best_name * 0.75 + desc_score * 0.25 + prefix_bonus + exact_bonus

    def search(self, query: str, limit: int = 15) -> List[SearchResult]:
        q = query.lower().strip()
        if not q:
            return []
        if not self._formula_search_items:
            self._load_index("formula")
        if not self._cask_search_items:
            self._load_index("cask")

        results: List[SearchResult] = []
        for item, candidates, desc in self._formula_search_items:
            score = self._score_formula(candidates, desc, q)
            if score >= 45:
                results.append(
                    SearchResult(
                        "formula",
                        item["name"],
                        item["name"],
                        item.get("desc", ""),
                        item.get("homepage", ""),
                        score,
                    )
                )
        for item, candidates, desc in self._cask_search_items:
            score = self._score_cask(candidates, desc, q)
            if score >= 45:
                display_name = ", ".join(item.get("name", [])[:2]) or item["token"]
                results.append(
                    SearchResult(
                        "cask",
                        item["token"],
                        display_name,
                        item.get("desc", ""),
                        item.get("homepage", ""),
                        score,
                    )
                )
        results.sort(key=lambda r: (-r.score, r.token))
        return results[:limit]

    def _tap_branch(self, local: Path) -> str:
        try:
            out = subprocess.check_output(
                ["git", "-C", str(local), "branch", "--show-current"],
                text=True,
            ).strip()
            return out or "HEAD"
        except Exception:
            return "HEAD"

    def _load_tap_items(self) -> List[Dict[str, Any]]:
        if self._tap_items_cache is not None:
            return self._tap_items_cache
        items: List[Dict[str, Any]] = []
        for repo_root in sorted(TAPS_DIR.rglob(".git")):
            local = repo_root.parent
            try:
                branch = self._tap_branch(local)
                owner = local.parent.name
                repo = local.name
                for top, kind in [("Formula", "formula"), ("Casks", "cask")]:
                    base = local / top
                    if not base.exists():
                        continue
                    for path in sorted(base.rglob("*.rb")):
                        rel = path.relative_to(local)
                        items.append(
                            {
                                "kind": kind,
                                "token": path.stem,
                                "name": path.stem,
                                "homepage": f"https://github.com/{owner}/{repo}/blob/{branch}/{rel.as_posix()}",
                                "tap_repo": f"{owner}/{repo}",
                                "tap_branch": branch,
                                "tap_path": rel.as_posix(),
                            }
                        )
            except Exception:
                continue
        self._tap_items_cache = items
        return items

    def search_taps(self, query: str, limit: int = 20) -> List[SearchResult]:
        q = query.lower().strip()
        results: List[SearchResult] = []
        for item in self._load_tap_items():
            name = item["token"]
            if name.lower() == q:
                score = 100
            elif name.lower().startswith(q) or q in name.lower():
                score = 85
            else:
                continue
            results.append(
                SearchResult(
                    item["kind"],
                    item["token"],
                    item["name"],
                    f"tap: {item['tap_repo']}",
                    item["homepage"],
                    score,
                )
            )
        results.sort(key=lambda r: (-r.score, r.token))
        return results[:limit]

    def resolve(self, query: str) -> Tuple[str, str]:
        tap_results = self.search_taps(query, limit=20)
        if tap_results:
            exact = [r for r in tap_results if r.token.lower() == query.lower()]
            if len(exact) == 1:
                return exact[0].kind, exact[0].token
            choice = inquirer.select(
                message="Select package",
                choices=[
                    {"name": f"[tap/{r.kind}] {r.token} — {r.desc}", "value": (r.kind, r.token)}
                    for r in tap_results
                ],
                default=(tap_results[0].kind, tap_results[0].token),
                qmark="🔎",
            ).execute()
            return choice

        results = self.search(query, limit=10)
        if not results:
            raise MacbrewError(f"No formula or cask matched '{query}'.")

        exact = [r for r in results if r.token.lower() == query.lower()]
        if len(exact) == 1:
            return exact[0].kind, exact[0].token

        choice = inquirer.select(
            message="Select package",
            choices=[
                {"name": f"[{r.kind}] {r.token} — {r.desc}", "value": (r.kind, r.token)}
                for r in results
            ],
            default=(results[0].kind, results[0].token),
            qmark="🔎",
        ).execute()
        return choice

    def _arch_placeholder(self) -> str:
        return "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "intel"

    def _tap_file_for_token(
        self, token: str, kind: Optional[str] = None
    ) -> Optional[Tuple[Path, str, str, str]]:
        for repo_root in TAPS_DIR.rglob(".git"):
            local = repo_root.parent
            owner = local.parent.name
            repo = local.name
            tops = ["Formula", "Casks"] if kind is None else (["Formula"] if kind == "formula" else ["Casks"])
            for top in tops:
                base = local / top
                if not base.exists():
                    continue
                for path in base.rglob(f"{token}.rb"):
                    branch = self._tap_branch(local)
                    return path, owner, repo, branch
        return None

    async def detail_async(self, kind: str, token: str, force: bool = False) -> Dict[str, Any]:
        tap_hit = self._tap_file_for_token(token, kind=kind)
        if tap_hit:
            path, owner, repo, branch = tap_hit
            rb_cache = self._detail_cache_file(kind, token)
            if not force and self._is_fresh(rb_cache):
                rb = await asyncio.to_thread(rb_cache.read_text)
            else:
                rb = await asyncio.to_thread(path.read_text)
                await asyncio.to_thread(rb_cache.write_text, rb)
            if kind == "formula":
                data = await asyncio.to_thread(parse_formula_rb, rb)
            else:
                data = await asyncio.to_thread(parse_cask_rb, rb, self._arch_placeholder())
            data["package_kind"] = kind
            return data

        index = self._load_index(kind)
        name_key = "name" if kind == "formula" else "token"
        entry = next((i for i in index if i.get(name_key) == token), None)
        if entry is None:
            raise MacbrewError(f"Package '{token}' not found in {kind} index.")

        ruby_source_path = entry.get("ruby_source_path")
        if not ruby_source_path:
            raise MacbrewError(f"No ruby_source_path for '{token}' in index.")

        raw_url = f"{FORMULA_RAW_BASE}/{ruby_source_path}" if kind == "formula" else f"{CASK_RAW_BASE}/{ruby_source_path}"
        rb_cache = self._detail_cache_file(kind, token)

        if not force and self._is_fresh(rb_cache):
            rb = await asyncio.to_thread(rb_cache.read_text)
        else:
            rb = await self._fetch_text_async(raw_url)
            await asyncio.to_thread(rb_cache.write_text, rb)

        if kind == "formula":
            data = await asyncio.to_thread(parse_formula_rb, rb)
        else:
            data = await asyncio.to_thread(parse_cask_rb, rb, self._arch_placeholder())
        data["package_kind"] = kind
        return data

    def detail(self, kind: str, token: str, force: bool = False) -> Dict[str, Any]:
        return self._run(self.detail_async(kind, token, force))

    def choose_install_root(self, default_target: Optional[str] = None) -> Path:
        config_default = self.config.get("install_root", "/Applications")
        chosen_default = default_target or config_default
        value = inquirer.text(
            message="Install path",
            default=chosen_default,
            validate=PathValidator(is_dir=True, message="Directory must exist"),
            qmark="📦",
        ).execute()
        return Path(os.path.expanduser(value)).resolve()

    def _select_cask_payload(self, cask: Dict[str, Any]) -> Dict[str, Any]:
        arch = platform.machine().lower()
        selected = dict(cask)
        if arch in {"x86_64", "amd64"}:
            if cask.get("arch_variants"):
                selected.update(cask["arch_variants"].get("intel", {}))
        elif arch in {"arm64", "aarch64"}:
            if cask.get("arch_variants"):
                selected.update(cask["arch_variants"].get("arm64", {}))
        return selected

    async def _download_file_async(self, url: str, expected_sha256: Optional[str]) -> Path:
        filename = Path(urlparse(url).path).name or "download.bin"
        destination = DOWNLOAD_DIR / filename
        hasher = hashlib.sha256()
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self._get_client() as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length") or 0)
                bar_width = 40
                downloaded_bytes = 0
                last_print = 0.0
                with destination.open("wb") as fh:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        hasher.update(chunk)
                        downloaded_bytes += len(chunk)
                        # Throttle updates to ~10Hz
                        now = time.time()
                        if now - last_print >= 0.1:
                            last_print = now
                            if total:
                                frac = min(1.0, downloaded_bytes / total)
                                filled = int(frac * bar_width)
                                bar = "=" * max(0, filled - 1) + (">" if filled > 0 and filled < bar_width else "=") + " " * max(0, bar_width - filled)
                                pct = int(frac * 100)
                                console.print(f"[cyan][{bar}][/cyan] [bold]{pct}%[/bold] {downloaded_bytes // 1024}KB/{total // 1024}KB", end="\r")
                            else:
                                console.print(f"[cyan]Downloading...[/cyan] [bold]{downloaded_bytes // 1024}KB[/bold]", end="\r")
                # ensure final newline/complete bar
                try:
                    if total:
                        console.print()
                    else:
                        console.print()
                except Exception:
                    pass
        if expected_sha256 and expected_sha256 != "no_check":
            actual = hasher.hexdigest()
            if actual.lower() != expected_sha256.lower():
                await asyncio.to_thread(destination.unlink, missing_ok=True)
                raise MacbrewError(f"SHA256 mismatch for {filename}")
        return destination

    def _download_file(self, url: str, expected_sha256: Optional[str]) -> Path:
        return self._run(self._download_file_async(url, expected_sha256))

    def _mount_dmg(self, dmg_path: Path) -> Path:
        mount_root = Path(tempfile.mkdtemp(prefix="macbrew-mount-"))
        subprocess.run(
            ["hdiutil", "attach", str(dmg_path), "-nobrowse", "-readonly", "-mountpoint", str(mount_root)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return mount_root

    def _detach_dmg(self, mount_root: Path) -> None:
        subprocess.run(
            ["hdiutil", "detach", str(mount_root), "-force"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(mount_root, ignore_errors=True)

    def _extract_if_needed(self, archive_path: Path) -> Path:
        suffixes = [s.lower() for s in archive_path.suffixes]
        if archive_path.suffix.lower() == ".zip":
            out = Path(tempfile.mkdtemp(prefix="macbrew-unzip-"))
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(out)
            return out
        if suffixes[-2:] in [[".tar", ".gz"], [".tar", ".xz"], [".tar", ".bz2"]]:
            out = Path(tempfile.mkdtemp(prefix="macbrew-untar-"))
            with tarfile.open(archive_path) as tf:
                tf.extractall(out)
            return out
        return archive_path.parent

    def _copy_app_bundle(self, source: Path, target_dir: Path, target_override: Optional[str] = None) -> Path:
        app_name = target_override or source.name
        destination = target_dir / app_name
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.copytree(source, destination, symlinks=True)
        return destination

    def _cask_target_dir(self, default_target: str) -> Path:
        target = expand_path(default_target)
        return target.parent if target.suffix == ".app" or str(target).endswith(".app") else target

    def _bottle_key_for_arch(self) -> List[str]:
        arch = arch_name()
        codename = self._detect_macos_codename()
        if arch == "arm64":
            candidates = []
            if codename:
                candidates.append(f"arm64_{codename}")
            candidates += [
                "arm64_tahoe",
                "arm64_sequoia",
                "arm64_sonoma",
                "arm64_ventura",
                "arm64_monterey",
                "arm64_big_sur",
            ]
        else:
            candidates = []
            if codename:
                candidates.append(codename)
            candidates += [
                "tahoe",
                "sequoia",
                "sonoma",
                "ventura",
                "monterey",
                "big_sur",
                "catalina",
            ]
        return candidates

    def _select_bottle(self, formula: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        files = formula.get("bottle", {}).get("stable", {}).get("files", {})
        if not files:
            return None
        for key in self._bottle_key_for_arch():
            if key in files:
                entry = files[key]
                return entry["url"], entry["sha256"]
        return None

    def _formula_prefix(self) -> Path:
        return expand_path(self.config.get("formula_prefix", "/opt/macbrew"))

    def _is_formula_installed(self, token: str, version: str) -> bool:
        return (self._formula_prefix() / "Cellar" / token / version).exists()

    def install_formula(self, token: str, _depth: int = 0) -> None:
        if token in self._installing:
            return
        self._installing.add(token)
        try:
            formula = self.detail("formula", token)
            version = formula.get("version") or formula.get("versions", {}).get("stable", "unknown")
            if self._is_formula_installed(token, version):
                console.print(f"[dim]Already installed: {token} {version}[/dim]")
                return

            all_deps = []
            for key in ("dependencies", "build_dependencies", "recommended_dependencies", "optional_dependencies"):
                all_deps.extend(formula.get(key, []) or [])
            if all_deps:
                indent = "  " * _depth
                console.print(f"{indent}[yellow]↳ Dependencies of {token}:[/yellow] {', '.join(all_deps)}")
                for dep in all_deps:
                    dep_token = dep.replace("@", "").replace("/", "-")
                    if dep_token and dep_token != token:
                        self.install_formula(dep_token, _depth=_depth + 1)

            bottle = self._select_bottle(formula)
            if bottle:
                url, sha256 = bottle
                kind = "bottle"
            else:
                url = formula.get("url") or formula.get("urls", {}).get("stable", {}).get("url")
                sha256 = formula.get("sha256") or formula.get("urls", {}).get("stable", {}).get("checksum")
                kind = "source"
                if not url:
                    raise MacbrewError(f"No bottle or source URL found for formula '{token}'.")
            console.print(f"[dim]Downloading {token} {version} ({kind} / {arch_name()})...[/dim]")
            downloaded = self._download_file(url, sha256)
            extracted = self._extract_if_needed(downloaded)
            prefix = self._formula_prefix()
            cellar_version = prefix / "Cellar" / token / version
            cellar_version.mkdir(parents=True, exist_ok=True)

            for item in extracted.iterdir():
                dest = cellar_version / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                shutil.move(str(item), str(dest))

            bin_dir = cellar_version / "bin"
            if bin_dir.is_dir():
                link_bin = prefix / "bin"
                link_bin.mkdir(exist_ok=True)
                for exe in bin_dir.iterdir():
                    if exe.is_file():
                        link = link_bin / exe.name
                        if link.exists() or link.is_symlink():
                            link.unlink()
                        link.symlink_to(exe)

            console.print(f"[bold green]✔ Installed formula:[/bold green] {token} {version}")
        finally:
            self._installing.discard(token)

    def install_cask(self, token: str) -> List[Path]:
        cask = self._select_cask_payload(self.detail("cask", token))
        artifacts = cask.get("artifacts", []) or []
        app_artifact = next((a for a in artifacts if "app" in a), None)
        if not app_artifact:
            raise MacbrewError("Only app-style casks are currently supported by this installer.")

        app_name = app_artifact["app"][0] if isinstance(app_artifact["app"], list) else app_artifact["app"]
        default_target = app_artifact.get("target") or str(Path(self.config.get("install_root", "/Applications")) / app_name)
        install_dir = self.choose_install_root(default_target=str(self._cask_target_dir(default_target)))
        target_path = expand_path(app_artifact.get("target") or str(install_dir / app_name))
        if target_path.exists() and not inquirer.confirm(message="Overwrite existing app?", default=False, qmark="⚠️").execute():
            return []

        downloaded = self._download_file(cask["url"], cask.get("sha256"))
        mount_root = extracted_root = None
        try:
            if downloaded.suffix.lower() == ".dmg":
                source_root = self._mount_dmg(downloaded)
                mount_root = source_root
            else:
                source_root = self._extract_if_needed(downloaded)
                extracted_root = source_root

                # If extraction returned the parent directory (common when file has no known suffix),
                # try to detect archive content and extract, or attempt to mount as a dmg.
                if source_root.is_file() or (source_root.exists() and len(list(source_root.iterdir())) == 1 and list(source_root.iterdir())[0] == downloaded):
                    # Try zip
                    try:
                        if zipfile.is_zipfile(downloaded):
                            out = Path(tempfile.mkdtemp(prefix="macbrew-unzip-"))
                            with zipfile.ZipFile(downloaded) as zf:
                                zf.extractall(out)
                            source_root = out
                            extracted_root = out
                        elif tarfile.is_tarfile(downloaded):
                            out = Path(tempfile.mkdtemp(prefix="macbrew-untar-"))
                            with tarfile.open(downloaded) as tf:
                                tf.extractall(out)
                            source_root = out
                            extracted_root = out
                        else:
                            # As a last resort, try mounting the file as a dmg (hdiutil can often mount raw images)
                            try:
                                mount_try = self._mount_dmg(downloaded)
                                source_root = mount_try
                                mount_root = mount_try
                            except Exception:
                                pass
                    except Exception:
                        # ignore extraction errors and continue with original source_root
                        pass

            # Try several strategies to locate the app bundle inside the installer media.
            # Normalize base name without the .app suffix for fuzzy matching.
            base_name = app_name[:-4] if app_name.lower().endswith(".app") else app_name

            # 1) Exact matches (allow both with and without .app)
            candidates = list(source_root.rglob(app_name))
            if not candidates and not app_name.lower().endswith(".app"):
                candidates = list(source_root.rglob(f"{app_name}.app"))

            # 2) If no exact candidate, find any .app and prefer ones containing base_name.
            app_source = None
            if candidates:
                app_source = candidates[0]
            else:
                # Primary scan: case-insensitive search for any .app under source_root
                all_apps = [p for p in source_root.rglob("*") if p.name.lower().endswith(".app")]
                if all_apps:
                    matches = [p for p in all_apps if base_name and base_name.lower() in p.name.lower()]
                    app_source = matches[0] if matches else all_apps[0]

            # 3) Additional fallbacks: the downloaded file itself might be a .app directory
            if not app_source:
                if downloaded.exists() and downloaded.name.lower().endswith(".app"):
                    app_source = downloaded
                elif downloaded.is_dir():
                    # search inside downloaded directory
                    apps_in_download = [p for p in downloaded.rglob("*") if p.name.lower().endswith(".app")]
                    if apps_in_download:
                        app_source = apps_in_download[0]

            # 4) As a last resort, also look one level up (some archives place .app next to the archive)
            if not app_source:
                parent_apps = [p for p in downloaded.parent.rglob("*") if p.name.lower().endswith(".app")]
                if parent_apps:
                    app_source = parent_apps[0]

            if not app_source or not app_source.exists():
                # Debug output to help locate why matches failed
                try:
                    console.print(f"[red]Failed to locate .app for requested name:[/red] {app_name}")
                    console.print(f"[yellow]Source root:[/yellow] {source_root} (exists={source_root.exists()}, is_dir={source_root.is_dir()})")
                    # List top-level entries to help debugging
                    try:
                        entries = list(sorted(source_root.iterdir()))
                        console.print(f"[yellow]Top-level entries under source root (showing up to 50):[/yellow]")
                        for p in entries[:50]:
                            console.print(f"  - {p} {'(dir)' if p.is_dir() else '(file)'}")
                        if len(entries) > 50:
                            console.print(f"  ... and {len(entries)-50} more entries ...")
                    except Exception:
                        console.print("  (could not list source_root entries)")
                    console.print("[yellow]Candidates from exact rglob(app_name):[/yellow]")
                    for p in candidates:
                        console.print(f"  - {p} {'(dir)' if p.is_dir() else '(file)'}")
                except Exception:
                    pass
                try:
                    console.print("[yellow]All .app entries under source root:[/yellow]")
                    for p in all_apps:
                        console.print(f"  - {p} {'(dir)' if p.is_dir() else '(file)'}")
                except Exception:
                    pass
                try:
                    console.print("[yellow]Apps inside downloaded path (if checked):[/yellow]")
                    for p in (apps_in_download if 'apps_in_download' in locals() else []):
                        console.print(f"  - {p} {'(dir)' if p.is_dir() else '(file)'}")
                except Exception:
                    pass
                try:
                    console.print("[yellow]Parent-dir apps nearby:[/yellow]")
                    for p in (parent_apps if 'parent_apps' in locals() else []):
                        console.print(f"  - {p} {'(dir)' if p.is_dir() else '(file)'}")
                except Exception:
                    pass
                try:
                    console.print(f"[yellow]Downloaded path:[/yellow] {downloaded} (exists={downloaded.exists()}, is_dir={downloaded.is_dir()})")
                except Exception:
                    pass
                raise MacbrewError(f"Could not find {app_name} (or any .app) inside installer media.")

            target_name = Path(app_artifact.get("target", app_name)).name
            installed_path = self._copy_app_bundle(app_source, install_dir, target_override=target_name)
            return [installed_path]
        finally:
            if mount_root:
                self._detach_dmg(mount_root)
            if extracted_root and extracted_root.exists():
                shutil.rmtree(extracted_root, ignore_errors=True)

    def uninstall_cask(self, token: str, zap: bool = False) -> List[Path]:
        cask = self._select_cask_payload(self.detail("cask", token))
        removed: List[Path] = []
        app_name = next(
            (a["app"][0] if isinstance(a["app"], list) else a["app"] for a in cask.get("artifacts", []) or [] if "app" in a),
            None,
        )
        if app_name:
            target = expand_path(self.config.get("install_root", "/Applications")) / app_name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                removed.append(target)

        if zap:
            for a in cask.get("artifacts", []) or []:
                if "zap" in a:
                    for rule in a.get("zap", []):
                        if isinstance(rule, dict):
                            for p in rule.get("trash", []) or []:
                                for cp in cleanup_pattern(p):
                                    try:
                                        if cp.is_dir():
                                            shutil.rmtree(cp, ignore_errors=True)
                                        elif cp.exists() or cp.is_symlink():
                                            cp.unlink(missing_ok=True)
                                        removed.append(cp)
                                    except Exception:
                                        pass
        return removed

    def update_cask(self, token: str) -> List[Path]:
        self.uninstall_cask(token, zap=False)
        return self.install_cask(token)

    async def show_async(self, query: str) -> Dict[str, Any]:
        kind, token = self.resolve(query)
        return await self.detail_async(kind, token)

    def show(self, query: str) -> Dict[str, Any]:
        return self._run(self.show_async(query))

    def tap(self, value: str) -> Dict[str, Any]:
        local, url, owner, repo = ensure_tap_repo(value)
        branch = tap_branch(local)
        self._tap_items_cache = None
        items: List[Dict[str, Any]] = []
        for top in ["Formula", "Casks"]:
            base = local / top
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.rb")):
                rel = path.relative_to(local)
                blob_url = f"https://github.com/{owner}/{repo}/blob/{branch}/{rel.as_posix()}"
                items.append(
                    {
                        "kind": top[:-1].lower(),
                        "name": path.name,
                        "path": rel.as_posix(),
                        "url": blob_url,
                        "commit_meta": "",
                    }
                )
        return {"repo": f"{owner}/{repo}", "url": url, "local": str(local), "branch": branch, "items": items}

    def list_taps(self) -> List[Dict[str, Any]]:
        taps = []
        for repo_root in sorted(TAPS_DIR.rglob(".git")):
            local = repo_root.parent
            owner = local.parent.name
            repo = local.name
            branch = self._tap_branch(local)
            formula_count = len(list((local / "Formula").rglob("*.rb"))) if (local / "Formula").exists() else 0
            cask_count = len(list((local / "Casks").rglob("*.rb"))) if (local / "Casks").exists() else 0
            taps.append(
                {
                    "repo": f"{owner}/{repo}",
                    "local": str(local),
                    "branch": branch,
                    "formula_count": formula_count,
                    "cask_count": cask_count,
                }
            )
        return taps

    def untap(self, value: str) -> str:
        _, owner, repo, short = normalize_tap_input(value)
        local = TAPS_DIR / owner / repo
        if not local.exists():
            raise MacbrewError(f"Tap '{short}' is not installed.")
        shutil.rmtree(local)
        self._tap_items_cache = None
        try:
            owner_dir = local.parent
            if owner_dir.exists() and not any(owner_dir.iterdir()):
                owner_dir.rmdir()
        except Exception:
            pass
        return short


def print_header(title: str) -> None:
    console.print(
        Panel.fit(
            f"""🧩 MACBREW - macOS Package Explorer
─────────────────────────────────────────────
{title}""",
            box=box.ROUNDED,
            border_style="cyan",
            padding=(0, 1),
        )
    )


def print_results(results: List[SearchResult]) -> None:
    if not results:
        console.print("[dim]No matches found.[/dim]")
        return
    for r in results:
        tag = "[blue][formula][/blue]" if r.kind == "formula" else "[green][cask][/green]"
        console.print(f"{tag} [bold]{r.token}[/bold]")
        if r.name and r.name != r.token:
            console.print(f"  {r.name}")
        if r.desc:
            console.print(f"  [dim]{r.desc}[/dim]")
        if r.homepage:
            console.print(f"  [dim]{r.homepage}[/dim]")
        console.print()


def print_tap_results(info: Dict[str, Any]) -> None:
    console.print(
        Panel.fit(
            f"""Tap: {info['repo']}
{info['url']}
Local: {info['local']}""",
            border_style="cyan",
        )
    )
    for item in info["items"]:
        console.print(f"[bold]{item['name']}[/bold]  [dim]{item['path']}[/dim]")
        console.print(f"  {item['url']}")
        if item.get("commit_meta"):
            console.print(f"  [dim]{item['commit_meta']}[/dim]")
        console.print()


from typing import Any, Dict

def show_info_pretty(data: Dict[str, Any]) -> None:
    is_cask = data.get("package_kind") == "cask" or ("token" in data and data.get("package_kind") != "formula")
    title = data.get("token") or data.get("name", data.get("full_name", "???"))
    print_header(f"PACKAGE INFO: {title}")

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="bold green", no_wrap=True)
    meta.add_column()
    meta.add_row("Identifier", data.get("token") or data.get("name") or data.get("full_name", "???"))
    meta.add_row("Type", "Cask" if is_cask else "Formula")

    if data.get("desc"):
        meta.add_row("Description", data["desc"])
    if data.get("homepage"):
        meta.add_row("Homepage", data["homepage"])
    if is_cask and data.get("version"):
        meta.add_row("Version", data["version"])

    if not is_cask:
        if data.get("version"):
            meta.add_row("Version", data["version"])
        if data.get("license"):
            meta.add_row("License", data["license"])
        if data.get("tap"):
            meta.add_row("Tap", data["tap"])
        if data.get("revision"):
            meta.add_row("Revision", str(data["revision"]))
        if data.get("version_scheme"):
            meta.add_row("Version Scheme", str(data["version_scheme"]))
        if data.get("linked_keg"):
            meta.add_row("Linked Keg", str(data["linked_keg"]))

    console.print(Panel(meta, box=box.SIMPLE_HEAVY, border_style="dim"))

    if not is_cask:
        if data.get("arch_variants"):
            t = Table("ARCH", "URL", "SHA256", title="ARCH VARIANTS", box=box.SIMPLE_HEAVY, border_style="blue")
            for k, v in data["arch_variants"].items():
                t.add_row(k, v.get("url") or "", (v.get("sha256") or "")[:16] + "…" if v.get("sha256") else "")
            console.print(t)

        bottle = data.get("bottle") or {}
        files = bottle.get("stable", {}).get("files", {}) or bottle.get("files", {})
        if files:
            t = Table("KEY", "ARCH", "SHA256", title="BOTTLES", box=box.SIMPLE_HEAVY, border_style="blue")
            for key, entry in files.items():
                arch_label = "arm64" if key.startswith("arm64_") else "x86_64"
                sha = entry.get("sha256") or ""
                t.add_row(key, arch_label, sha[:16] + "…" if sha else "")
            console.print(t)

        stable = data.get("stable") or {}
        head = data.get("head") or {}
        urls = data.get("urls", {})
        if stable.get("url") or head.get("url") or urls.get("stable", {}).get("url"):
            dl = Table.grid(padding=(0, 2))
            dl.add_column(style="bold yellow", no_wrap=True)
            dl.add_column()
            if stable.get("url"):
                dl.add_row("Stable URL", stable["url"])
            elif urls.get("stable", {}).get("url"):
                dl.add_row("Stable URL", urls["stable"]["url"])
            if stable.get("checksum"):
                dl.add_row("Stable SHA256", stable["checksum"])
            elif urls.get("stable", {}).get("checksum"):
                dl.add_row("Checksum", urls["stable"]["checksum"])
            if head.get("url"):
                dl.add_row("HEAD URL", head["url"])
            if head.get("branch"):
                dl.add_row("HEAD Branch", head["branch"])
            if head.get("version"):
                dl.add_row("HEAD Version", head["version"])
            console.print(Panel(dl, title="SOURCE", box=box.SIMPLE_HEAVY, border_style="yellow"))

        exes = data.get("executables") or []
        if exes:
            t = Table("BINARY", title="EXECUTABLES", box=box.SIMPLE_HEAVY, border_style="green")
            for e in exes:
                t.add_row(e)
            console.print(t)

        conflicts = data.get("conflicts_with") or data.get("conflicts") or []
        if conflicts:
            t = Table("PACKAGE", title="CONFLICTS", box=box.SIMPLE_HEAVY, border_style="red")
            for c in conflicts:
                t.add_row(str(c))
            console.print(t)

        dep_sections = [
            ("Dependencies", "blue", data.get("dependencies", []) or []),
            ("Build Dependencies", "dim blue", data.get("build_dependencies", []) or []),
            ("Recommended", "cyan", data.get("recommended_dependencies", []) or []),
            ("Optional", "magenta", data.get("optional_dependencies", []) or []),
            (
                "Uses from macOS",
                "dim yellow",
                [
                    next(iter(d.values()), None) or next(iter(d.keys()), None) if isinstance(d, dict) else d
                    for d in (data.get("uses_from_macos") or [])
                ],
            ),
        ]
        for header, color, items in dep_sections:
            items = [i for i in items if i]
            if items:
                t = Table("DEP", title=header, box=box.SIMPLE_HEAVY, border_style=color)
                for d in items:
                    t.add_row(str(d))
                console.print(t)

        if data.get("caveats"):
            console.print(Panel(data["caveats"].strip(), title="CAVEATS", border_style="dim magenta"))

        analytics = (data.get("analytics") or {}).get("install") or {}
        if analytics:
            t = Table("PERIOD", "COUNT", title="INSTALLS (30/90/365d)", box=box.SIMPLE_HEAVY, border_style="dim cyan")
            for period in ["30d", "90d", "365d"]:
                val = analytics.get(period)
                if isinstance(val, dict):
                    val = sum(val.values())
                t.add_row(period, str(val if val is not None else 0))
            console.print(t)

        if data.get("versioned_formulae"):
            t = Table("VERSIONED", title="VERSIONED FORMULAE", box=box.SIMPLE_HEAVY, border_style="dim magenta")
            for v in data["versioned_formulae"]:
                t.add_row(str(v))
            console.print(t)

    else:
        url = data.get("url")
        sha = data.get("sha256")
        if url:
            dl = Table.grid(padding=(0, 2))
            dl.add_column(style="bold yellow", no_wrap=True)
            dl.add_column()
            dl.add_row("URL", url)
            if sha:
                dl.add_row("SHA256", sha)
            console.print(Panel(dl, title="DOWNLOAD", box=box.SIMPLE_HEAVY, border_style="yellow"))

        apps = [a for a in (data.get("artifacts") or []) if "app" in a]
        if apps:
            t = Table("BUNDLE", "TARGET", "KIND", title="APP ARTIFACTS", box=box.SIMPLE_HEAVY, show_lines=False, border_style="green")
            for a in apps:
                name = a["app"][0] if isinstance(a["app"], list) else a["app"]
                target = a.get("target", f"/Applications/{name}")
                t.add_row(name, target, "app")
            console.print(t)

        zaps = []
        for a in data.get("artifacts", []) or []:
            if "zap" in a:
                for rule in a["zap"]:
                    if isinstance(rule, dict):
                        zaps.extend(rule.get("trash", []) or [])
        if zaps:
            t = Table("PATH", title="CLEANUP (ZAP)", box=box.SIMPLE_HEAVY, show_lines=False, border_style="magenta")
            for p in zaps:
                t.add_row(p)
            console.print(t)

        analytics = (data.get("analytics") or {}).get("install") or {}
        if analytics:
            t = Table("PERIOD", "COUNT", title="INSTALLS (30/90/365d)", box=box.SIMPLE_HEAVY, border_style="dim cyan")
            for period in ["30d", "90d", "365d"]:
                val = analytics.get(period)
                if isinstance(val, dict):
                    val = sum(val.values())
                t.add_row(period, str(val if val is not None else 0))
            console.print(t)


def cmd_search(app: Macbrew, args: argparse.Namespace) -> None:
    print_results(app.search(args.query, args.limit))


def cmd_install(app: Macbrew, args: argparse.Namespace) -> None:
    kind, token = app.resolve(args.query)
    if kind == "cask":
        installed = app.install_cask(token)
        if installed:
            console.print("[bold green]✔ Installed:[/bold green]")
            for p in installed:
                console.print(f"  {p}")
    else:
        app.install_formula(token)


def cmd_uninstall(app: Macbrew, args: argparse.Namespace) -> None:
    kind, token = app.resolve(args.query)
    if kind != "cask":
        raise MacbrewError("Uninstall currently supports casks only.")
    zap = args.zap or inquirer.confirm(
        message="Also remove user settings and caches (zap)?",
        default=False,
        qmark="🧹",
    ).execute()
    removed = app.uninstall_cask(token, zap=zap)
    if removed:
        console.print("[bold green]✔ Removed:[/bold green]")
        for p in removed:
            console.print(f"  {p}")
    else:
        console.print("[dim]Nothing to remove.[/dim]")


def cmd_update(app: Macbrew, args: argparse.Namespace) -> None:
    kind, token = app.resolve(args.query)
    if kind != "cask":
        raise MacbrewError("Update currently supports casks only.")
    installed = app.update_cask(token)
    if installed:
        console.print("[bold green]✔ Updated:[/bold green]")
        for p in installed:
            console.print(f"  {p}")


def cmd_info(app: Macbrew, args: argparse.Namespace) -> None:
    data = app.show(args.query)
    show_info_pretty(data)


def cmd_refresh(app: Macbrew, args: argparse.Namespace) -> None:
    app._run(app.refresh(force=args.force))
    console.print("[bold green]✔ Cache refreshed.[/bold green]")


def cmd_config(app: Macbrew, args: argparse.Namespace) -> None:
    if args.set_install_root:
        app.config["install_root"] = str(expand_path(args.set_install_root))
        app.save_config()
    if args.set_formula_prefix:
        app.config["formula_prefix"] = str(expand_path(args.set_formula_prefix))
        app.save_config()
    console.print_json(data=app.config)


def cmd_cleanup(app: Macbrew, args: argparse.Namespace) -> None:
    removed_files = 0
    for p in CACHE_DIR.glob("*"):
        if p.is_file():
            p.unlink()
            removed_files += 1
    console.print(f"[bold green]✔ Cleared {removed_files} cache files.[/bold green]")
    removed_downloads = 0
    for p in DOWNLOAD_DIR.iterdir():
        if p.is_file():
            p.unlink()
            removed_downloads += 1
    console.print(f"[bold green]✔ Removed {removed_downloads} downloaded archives.[/bold green]")


def cmd_tap(app: Macbrew, args: argparse.Namespace) -> None:
    info = app.tap(args.repo)
    print_tap_results(info)


def cmd_tap_list(app: Macbrew, args: argparse.Namespace) -> None:
    taps = app.list_taps()
    if not taps:
        console.print("[dim]No taps installed.[/dim]")
        return
    t = Table("TAP", "BRANCH", "FORMULAE", "CASKS", title="Installed Taps", box=box.SIMPLE_HEAVY, border_style="cyan")
    for tap in taps:
        t.add_row(tap["repo"], tap["branch"], str(tap["formula_count"]), str(tap["cask_count"]))
    console.print(t)
    for tap in taps:
        console.print(f"[dim]{tap['local']}[/dim]")


def cmd_untap(app: Macbrew, args: argparse.Namespace) -> None:
    removed = app.untap(args.repo)
    console.print(f"[bold green]✔ Removed tap:[/bold green] {removed}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP_NAME, description="Fast Homebrew-style search/install for macOS")
    sp = p.add_subparsers(dest="command", required=True)

    s = sp.add_parser("search", help="Search formulas and casks")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=15)

    i = sp.add_parser("install", help="Install a formula or cask")
    i.add_argument("query")

    u = sp.add_parser("uninstall", help="Uninstall a cask")
    u.add_argument("query")
    u.add_argument("--zap", action="store_true", help="Also remove user settings and caches")

    up = sp.add_parser("update", help="Reinstall a cask to the latest version")
    up.add_argument("query")

    inf = sp.add_parser("info", help="Show package info")
    inf.add_argument("query")

    r = sp.add_parser("refresh", help="Refresh cached indexes")
    r.add_argument("--force", action="store_true")

    c = sp.add_parser("config", help="Inspect or update config")
    c.add_argument("--set-install-root")
    c.add_argument("--set-formula-prefix")

    tp = sp.add_parser("tap", help="Clone or inspect a Homebrew tap")
    tp.add_argument("repo")

    sp.add_parser("tap-list", help="List installed taps")

    ut = sp.add_parser("untap", help="Remove an installed tap")
    ut.add_argument("repo")

    sp.add_parser("cleanup", help="Delete cached metadata and downloaded archives")

    return p


def main() -> int:
    args = build_parser().parse_args()
    app = Macbrew()
    try:
        dispatch = {
            "search": cmd_search,
            "install": cmd_install,
            "uninstall": cmd_uninstall,
            "update": cmd_update,
            "info": cmd_info,
            "refresh": cmd_refresh,
            "config": cmd_config,
            "tap": cmd_tap,
            "tap-list": cmd_tap_list,
            "untap": cmd_untap,
            "cleanup": cmd_cleanup,
        }
        dispatch[args.command](app, args)
        return 0
    except (httpx.HTTPError, subprocess.CalledProcessError) as e:
        console.print(f"[red]{e}[/red]", file=sys.stderr)
    except KeyboardInterrupt:
        console.print("Aborted.", file=sys.stderr)
    except MacbrewError as e:
        console.print(f"[red]Error: {e}[/red]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())