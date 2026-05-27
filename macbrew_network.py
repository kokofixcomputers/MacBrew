import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx
from rapidfuzz import fuzz

from macbrew_constants import APP_NAME, CACHE_DIR, FORMULA_INDEX_URL, CASK_INDEX_URL, TIMEOUT, SearchResult


class NetworkMixin:
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
        ttl = int(self.config.get("cache_ttl_seconds", 0))
        return path.exists() and (time.time() - path.stat().st_mtime) < ttl

    async def _fetch_json_direct_async(self, url: str) -> Any:
        async with self._get_client() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

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
        return results
