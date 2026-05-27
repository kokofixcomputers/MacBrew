#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

from InquirerPy import inquirer
from InquirerPy.validator import PathValidator
from rich import box
from rich.panel import Panel
from rich.table import Table

from macbrew_constants import APP_NAME, CACHE_DIR, DOWNLOAD_DIR, MacbrewError, SearchResult, console
from macbrew_core import Macbrew
from macbrew_utils import expand_path
import warnings
warnings.filterwarnings("ignore", message="invalid escape sequence")


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


def cmd_outdated(app: Macbrew, args: argparse.Namespace) -> None:
    items = app.outdated()
    if not items:
        console.print("[bold green]All installed packages are up to date.[/bold green]")
        return
    t = Table("TYPE", "TOKEN", "INSTALLED", "LATEST", "PATH", title="Outdated Packages", box=box.SIMPLE_HEAVY, border_style="yellow")
    for item in items:
        t.add_row(
            item["kind"],
            item["token"],
            item["installed_version"],
            item["latest_version"] or "?",
            item["path"] or "",
        )
    console.print(t)


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

    sp.add_parser("outdated", help="Show installed formulae and casks that are outdated")

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
            "outdated": cmd_outdated,
        }
        dispatch[args.command](app, args)
        return 0
    except (Exception,) as e:
        if hasattr(e, '__class__') and e.__class__.__name__ == 'HTTPError':
            console.print(f"[red]{e}[/red]", file=sys.stderr)
        elif isinstance(e, MacbrewError):
            console.print(f"[red]Error: {e}[/red]", file=sys.stderr)
        elif isinstance(e, KeyboardInterrupt):
            console.print("Aborted.", file=sys.stderr)
        else:
            console.print(f"[red]{e}[/red]", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
