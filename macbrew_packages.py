import asyncio
import hashlib
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from InquirerPy import inquirer

from macbrew_constants import APP_NAME, DOWNLOAD_DIR, METADATA_FILE, MacbrewError, console
from macbrew_utils import arch_name, cleanup_pattern, expand_path, macos_codename


class PackageMixin:
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
        codename = macos_codename()
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

            metadata = {
                "package_kind": "formula",
                "token": token,
                "version": version,
                "installed_at": time.time(),
                "source_url": url,
                "sha256": sha256,
                "install_path": str(cellar_version),
            }
            self._write_metadata(cellar_version / METADATA_FILE, metadata)
            console.print(f"[bold green]✔ Installed formula:[/bold green] {token} {version}")
        finally:
            self._installing.discard(token)

    def install_cask(self, token: str) -> List[Path]:
        cask = self._select_cask_payload(self.detail("cask", token))
        artifacts = cask.get("artifacts", []) or []
        app_artifact = next((a for a in artifacts if "app" in a), None)
        pkg_artifact = next((a for a in artifacts if "pkg" in a), None)
        if not app_artifact and not pkg_artifact:
            raise MacbrewError("Only app-style and pkg-style casks are currently supported by this installer.")

        if app_artifact:
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

                    if source_root.is_file() or (source_root.exists() and len(list(source_root.iterdir())) == 1 and list(source_root.iterdir())[0] == downloaded):
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
                                try:
                                    mount_try = self._mount_dmg(downloaded)
                                    source_root = mount_try
                                    mount_root = mount_try
                                except Exception:
                                    pass
                        except Exception:
                            pass

                base_name = app_name[:-4] if app_name.lower().endswith(".app") else app_name
                candidates = list(source_root.rglob(app_name))
                if not candidates and not app_name.lower().endswith(".app"):
                    candidates = list(source_root.rglob(f"{app_name}.app"))

                app_source = None
                if candidates:
                    app_source = candidates[0]
                else:
                    all_apps = [p for p in source_root.rglob("*") if p.name.lower().endswith(".app")]
                    if all_apps:
                        matches = [p for p in all_apps if base_name and base_name.lower() in p.name.lower()]
                        app_source = matches[0] if matches else all_apps[0]

                if not app_source:
                    if downloaded.exists() and downloaded.name.lower().endswith(".app"):
                        app_source = downloaded
                    elif downloaded.is_dir():
                        apps_in_download = [p for p in downloaded.rglob("*") if p.name.lower().endswith(".app")]
                        if apps_in_download:
                            app_source = apps_in_download[0]

                if not app_source:
                    parent_apps = [p for p in downloaded.parent.rglob("*") if p.name.lower().endswith(".app")]
                    if parent_apps:
                        app_source = parent_apps[0]

                if not app_source or not app_source.exists():
                    try:
                        console.print(f"[red]Failed to locate .app for requested name:[/red] {app_name}")
                        console.print(f"[yellow]Source root:[/yellow] {source_root} (exists={source_root.exists()}, is_dir={source_root.is_dir()})")
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
                metadata = {
                    "package_kind": "cask",
                    "token": token,
                    "name": cask.get("name") or token,
                    "version": cask.get("version"),
                    "installed_at": time.time(),
                    "source_url": cask.get("url"),
                    "sha256": cask.get("sha256"),
                    "livecheck": cask.get("livecheck"),
                    "install_path": str(installed_path),
                }
                self._write_metadata(installed_path / METADATA_FILE, metadata)
                return [installed_path]
            finally:
                if mount_root:
                    self._detach_dmg(mount_root)
                if extracted_root and extracted_root.exists():
                    shutil.rmtree(extracted_root, ignore_errors=True)

        pkg_name = pkg_artifact["pkg"][0] if isinstance(pkg_artifact["pkg"], list) else pkg_artifact["pkg"]
        downloaded = self._download_file(cask["url"], cask.get("sha256"))
        pkg_path = downloaded
        if pkg_path.suffix.lower() != ".pkg":
            extracted = self._extract_if_needed(downloaded)
            if extracted.is_dir():
                matches = list(extracted.rglob("*.pkg"))
                if matches:
                    pkg_path = matches[0]
        if not pkg_path.exists() or pkg_path.suffix.lower() != ".pkg":
            raise MacbrewError(f"Could not find PKG installer for {token}.")

        try:
            subprocess.check_call(["installer", "-pkg", str(pkg_path), "-target", "/"])
        except subprocess.CalledProcessError as exc:
            raise MacbrewError(f"PKG install failed: {exc}")

        metadata = {
            "package_kind": "cask",
            "token": token,
            "name": cask.get("name") or token,
            "version": cask.get("version"),
            "installed_at": time.time(),
            "source_url": cask.get("url"),
            "sha256": cask.get("sha256"),
            "livecheck": cask.get("livecheck"),
            "install_path": str(pkg_path),
        }
        self._write_metadata(pkg_path / METADATA_FILE, metadata)
        return [pkg_path]

    def uninstall_cask(self, token: str, zap: bool = False) -> List[Path]:
        cask = self._select_cask_payload(self.detail("cask", token))
        removed: List[Path] = []

        app_artifact = next((a for a in cask.get("artifacts", []) or [] if "app" in a), None)
        if app_artifact:
            app_name = app_artifact["app"][0] if isinstance(app_artifact["app"], list) else app_artifact["app"]
            target = expand_path(app_artifact.get("target") or str(Path(self.config.get("install_root", "/Applications")) / app_name))
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
                removed.append(target)

        for artifact in cask.get("artifacts", []) or []:
            if "uninstall" in artifact:
                uninstall_data = artifact["uninstall"]
                if isinstance(uninstall_data, dict):
                    uninstall_data = [uninstall_data]
                for rule in uninstall_data or []:
                    if not isinstance(rule, dict):
                        continue
                    if "pkgutil" in rule:
                        pkgutil_ids = rule["pkgutil"]
                        if isinstance(pkgutil_ids, str):
                            pkgutil_ids = [pkgutil_ids]
                        for pkg_id in pkgutil_ids:
                            try:
                                subprocess.check_call(["pkgutil", "--forget", pkg_id])
                            except Exception:
                                pass
                    if "delete" in rule:
                        for path_item in rule.get("delete", []) or []:
                            for cp in cleanup_pattern(path_item):
                                try:
                                    if cp.is_dir():
                                        shutil.rmtree(cp, ignore_errors=True)
                                    elif cp.exists() or cp.is_symlink():
                                        cp.unlink(missing_ok=True)
                                    removed.append(cp)
                                except Exception:
                                    pass
                    if "trash" in rule:
                        for path_item in rule.get("trash", []) or []:
                            for cp in cleanup_pattern(path_item):
                                try:
                                    if cp.is_dir():
                                        shutil.rmtree(cp, ignore_errors=True)
                                    elif cp.exists() or cp.is_symlink():
                                        cp.unlink(missing_ok=True)
                                    removed.append(cp)
                                except Exception:
                                    pass

        if zap:
            for a in cask.get("artifacts", []) or []:
                if "zap" in a:
                    for rule in a.get("zap", []) or []:
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

        self._remove_metadata(kind="cask", token=token)
        self._remove_download_cache(cask.get("url"), token)

        return removed

    def update_cask(self, token: str) -> List[Path]:
        self.uninstall_cask(token, zap=False)
        return self.install_cask(token)
