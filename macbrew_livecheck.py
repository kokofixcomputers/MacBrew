import re
from typing import Any, Dict, List, Optional

import httpx

from macbrew_constants import APP_NAME, TIMEOUT


class LivecheckMixin:
    def _fetch_livecheck_version(self, livecheck: Dict[str, Any]) -> Optional[str]:
        if not livecheck or not livecheck.get("url"):
            return None

        strategy = livecheck.get("strategy", "unknown")

        try:
            if strategy == "json":
                return self._fetch_json_livecheck_version(livecheck)
            elif strategy == "sparkle":
                return self._fetch_sparkle_livecheck_version(livecheck)
            elif strategy == "regex":
                return self._fetch_regex_livecheck_version(livecheck)
            elif strategy == "header_match":
                return self._fetch_header_match_livecheck_version(livecheck)
        except Exception:
            pass

        return None

    def _fetch_json_livecheck_version(self, livecheck: Dict[str, Any]) -> Optional[str]:
        try:
            data = self._run(self._fetch_json_direct_async(livecheck["url"]))
            if data is None:
                return None
            path = livecheck.get("json_path") or []
            if isinstance(path, list) and path:
                value = data
                for key in path:
                    if isinstance(value, dict):
                        value = value.get(key)
                    else:
                        return None
                return str(value) if value is not None else None
            return None
        except Exception:
            return None

    def _fetch_sparkle_livecheck_version(self, livecheck: Dict[str, Any]) -> Optional[str]:
        try:
            text = self._run(self._fetch_text_async(livecheck["url"]))
            if not text:
                return None

            version_method = livecheck.get("sparkle_version_method", "version")

            if version_method == "short_version":
                m = re.search(r'shortVersionString="([^"]+)"', text)
                if m:
                    return m.group(1)

            m = re.search(r'(?:version|sparkle:version)="([^"]+)"', text)
            if m:
                return m.group(1)

            m = re.search(r'<version>([^<]+)</version>', text)
            if m:
                return m.group(1)

            return None
        except Exception:
            return None

    def _fetch_header_match_livecheck_version(self, livecheck: Dict[str, Any]) -> Optional[str]:
        try:
            with httpx.Client(headers={"User-Agent": f"{APP_NAME}/0.1"}, timeout=TIMEOUT, follow_redirects=False) as client:
                response = client.head(livecheck["url"])
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location") or response.headers.get("Location")
                    if location:
                        return self._extract_version_from_text(location)
                location = str(response.url)
                version = self._extract_version_from_text(location)
                if version:
                    return version
                response = client.get(livecheck["url"])
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location") or response.headers.get("Location")
                    if location:
                        return self._extract_version_from_text(location)
                return self._extract_version_from_text(str(response.url))
        except Exception:
            return None

    def _extract_version_from_text(self, text: str) -> Optional[str]:
        m = re.search(r'v?(\d+(?:\.\d+)+)', text)
        return m.group(1) if m else None

    def _fetch_regex_livecheck_version(self, livecheck: Dict[str, Any]) -> Optional[str]:
        try:
            text = self._run(self._fetch_text_async(livecheck["url"]))
            if not text:
                return None

            regex_pattern = livecheck.get("regex")
            if not regex_pattern:
                return None

            try:
                pattern = re.compile(regex_pattern, re.IGNORECASE)
            except re.error:
                pattern = re.compile(regex_pattern)

            m = pattern.search(text)
            if m:
                if m.groups():
                    return m.group(1)
                return m.group(0)

            return None
        except Exception:
            return None

    def _latest_index_version(self, kind: str, token: str) -> Optional[str]:
        try:
            index = self._load_index(kind)
            entry = next((i for i in index if i.get("token") == token or i.get("name") == token), None)
            if entry and entry.get("version"):
                return str(entry["version"])
        except Exception:
            pass
        try:
            detail = self.detail(kind, token)
            return detail.get("version")
        except Exception:
            return None

    def _is_version_outdated(self, installed_version: str, latest_version: Optional[str]) -> bool:
        if not latest_version:
            return False
        return installed_version != latest_version

    def outdated(self) -> List[Dict[str, Any]]:
        outdated_items: List[Dict[str, Any]] = []
        for item in self._installed_metadata():
            installed_version = item.get("version")
            if not installed_version:
                continue
            latest_version = None
            if item.get("livecheck"):
                latest_version = self._fetch_livecheck_version(item["livecheck"])
            if not latest_version:
                kind = item.get("package_kind")
                latest_version = self._latest_index_version(kind, item.get("token", ""))
            if self._is_version_outdated(installed_version, latest_version):
                outdated_items.append(
                    {
                        "token": item.get("token"),
                        "name": item.get("name") or item.get("token"),
                        "kind": item.get("package_kind"),
                        "installed_version": installed_version,
                        "latest_version": latest_version,
                        "path": item.get("target") or item.get("install_path") or "",
                    }
                )
        return outdated_items
