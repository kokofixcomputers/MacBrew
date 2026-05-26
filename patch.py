from pathlib import Path
p=Path('main.py')
s=p.read_text()
start=s.index('def show_info_pretty(data: Dict[str, Any]) -> None:')
end=s.index('def cmd_search(app: Macbrew, args: argparse.Namespace) -> None:')
new='''def show_info_pretty(data: Dict[str, Any]) -> None:
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
    console.print(Panel(meta, box=box.SIMPLE_HEAVY, border_style="dim"))

    if not is_cask:
        if data.get("arch_variants"):
            t = Table("ARCH", "URL", "SHA256", title="ARCH VARIANTS", box=box.SIMPLE_HEAVY, border_style="blue")
            for k, v in data["arch_variants"].items():
                t.add_row(k, v.get("url") or "", (v.get("sha256") or "")[:16] + "…" if v.get("sha256") else "")
            console.print(t)

        files = data.get("bottle", {}).get("stable", {}).get("files", {})
        if files:
            t = Table("KEY", "ARCH", "SHA256", title="BOTTLES", box=box.SIMPLE_HEAVY, border_style="blue")
            for key, entry in files.items():
                arch_label = "arm64" if key.startswith("arm64_") else "x86_64"
                t.add_row(key, arch_label, (entry.get("sha256") or "")[:16] + "…")
            console.print(t)

        urls = data.get("urls", {})
        if urls.get("stable", {}).get("url"):
            dl = Table.grid(padding=(0, 2))
            dl.add_column(style="bold yellow", no_wrap=True)
            dl.add_column()
            dl.add_row("Stable URL", urls["stable"]["url"])
            if urls["stable"].get("checksum"):
                dl.add_row("Checksum", urls["stable"]["checksum"])
            if urls.get("head", {}).get("url"):
                dl.add_row("HEAD URL", urls["head"]["url"])
            console.print(Panel(dl, title="SOURCE", box=box.SIMPLE_HEAVY, border_style="yellow"))

        exes = data.get("executables") or []
        if exes:
            t = Table("BINARY", title="EXECUTABLES", box=box.SIMPLE_HEAVY, border_style="green")
            for e in exes:
                t.add_row(e)
            console.print(t)

        dep_sections = [
            ("Dependencies", "blue", data.get("dependencies", []) or []),
            ("Build Dependencies", "dim blue", data.get("build_dependencies", []) or []),
            ("Recommended", "cyan", data.get("recommended_dependencies", []) or []),
            ("Optional", "magenta", data.get("optional_dependencies", []) or []),
            ("Uses from macOS", "dim yellow", [next(iter(d.values()), None) or next(iter(d.keys()), None) if isinstance(d, dict) else d for d in (data.get("uses_from_macos") or [])]),
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
                t.add_row(v)
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

'''
s = s[:start] + new + s[end:]
p.write_text(s)
print('patched full pretty printer')