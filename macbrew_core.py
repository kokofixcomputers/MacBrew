import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from InquirerPy import inquirer
from InquirerPy.validator import PathValidator

from macbrew_constants import (
    APP_NAME,
    CACHE_DIR,
    CONFIG_DIR,
    CONFIG_PATH,
    DEFAULT_CONFIG,
    DOWNLOAD_DIR,
    FORMULA_RAW_BASE,
    CASK_RAW_BASE,
    METADATA_DIR,
    MacbrewError,
    SearchResult,
    TAPS_DIR,
)
from macbrew_metadata import MetadataMixin
from macbrew_network import NetworkMixin
from macbrew_livecheck import LivecheckMixin
from macbrew_packages import PackageMixin
from macbrew_parsers import parse_formula_rb, parse_cask_rb
from macbrew_taps import ensure_tap_repo, tap_branch, normalize_tap_input
from macbrew_utils import expand_path


class Macbrew(PackageMixin, LivecheckMixin, NetworkMixin, MetadataMixin):
    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        TAPS_DIR.mkdir(parents=True, exist_ok=True)
        METADATA_DIR.mkdir(parents=True, exist_ok=True)
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
        return results

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

    def show(self, query: str) -> Dict[str, Any]:
        kind, token = self.resolve(query)
        return self.detail(kind, token)

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
