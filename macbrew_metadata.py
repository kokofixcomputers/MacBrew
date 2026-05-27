import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from macbrew_constants import DOWNLOAD_DIR, METADATA_DIR, METADATA_FILE


class MetadataMixin:
    def _get_metadata_path(self, kind: str, token: str, version: Optional[str] = None) -> Path:
        if version:
            return METADATA_DIR / kind / token / f"{version}.json"
        return METADATA_DIR / kind / token / "metadata.json"

    def _remove_metadata(self, kind: str, token: str, version: Optional[str] = None) -> None:
        if version:
            meta_path = self._get_metadata_path(kind, token, version)
            if meta_path.exists():
                meta_path.unlink()
        else:
            token_dir = METADATA_DIR / kind / token
            if token_dir.exists():
                shutil.rmtree(token_dir, ignore_errors=True)

        try:
            kind_dir = METADATA_DIR / kind
            if kind_dir.exists() and not any(kind_dir.iterdir()):
                kind_dir.rmdir()
        except Exception:
            pass

    def _remove_download_cache(self, source_url: Optional[str], token: str) -> None:
        if not source_url:
            return
        filename = Path(urlparse(source_url).path).name
        if not filename:
            return
        download_path = DOWNLOAD_DIR / filename
        if download_path.exists():
            try:
                download_path.unlink()
            except Exception:
                pass

    def _write_metadata(self, path: Path, metadata: Dict[str, Any]) -> None:
        kind = metadata.get("package_kind", "")
        token = metadata.get("token", "")
        version = metadata.get("version")
        if isinstance(version, str) and version.strip() == "":
            version = None

        if kind and token:
            meta_path = self._get_metadata_path(kind, token, version)
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(metadata, indent=2) + "\n")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(metadata, indent=2) + "\n")

    def _read_metadata(self, path: Path) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    def _installed_formula_metadata(self) -> List[Dict[str, Any]]:
        installed: List[Dict[str, Any]] = []
        formula_dir = METADATA_DIR / "formula"
        if not formula_dir.exists():
            return installed

        for token_dir in sorted(formula_dir.iterdir()):
            if not token_dir.is_dir():
                continue
            for meta_file in sorted(token_dir.glob("*.json")):
                try:
                    data = json.loads(meta_file.read_text())
                    if data:
                        installed.append(data)
                except Exception:
                    pass
        return installed

    def _installed_cask_metadata(self) -> List[Dict[str, Any]]:
        installed: List[Dict[str, Any]] = []
        cask_dir = METADATA_DIR / "cask"
        if not cask_dir.exists():
            return installed

        for token_dir in sorted(cask_dir.iterdir()):
            if not token_dir.is_dir():
                continue
            for meta_file in sorted(token_dir.glob("*.json")):
                try:
                    data = json.loads(meta_file.read_text())
                    if data:
                        installed.append(data)
                except Exception:
                    pass
        return installed

    def _installed_metadata(self) -> List[Dict[str, Any]]:
        return self._installed_formula_metadata() + self._installed_cask_metadata()
