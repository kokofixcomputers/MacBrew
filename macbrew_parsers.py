import re
from typing import Any, Dict, List, Optional


def parse_formula_rb(rb: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    m = re.search(r'^\s*class\s+([A-Za-z0-9_]+)\s+<\s+Formula', rb, re.MULTILINE)
    if m:
        out["token"] = _class_to_token(m.group(1))
        out["name"] = out["token"]
        out["full_name"] = out["token"]
    m = re.search(r'^\s*desc\s+"([^"]+)"', rb, re.MULTILINE)
    if m: out["desc"] = m.group(1)
    m = re.search(r'^\s*homepage\s+"([^"]+)"', rb, re.MULTILINE)
    if m: out["homepage"] = m.group(1)

    version = None
    m = re.search(r'^\s*version\s+"([^"]+)"', rb, re.MULTILINE)
    if m:
        version = m.group(1)

    m = re.search(r'if\s+Hardware::CPU\.intel\?(.+?)else\s*(.+?)end', rb, re.DOTALL)
    if m:
        intel_block = m.group(1)
        arm_block = m.group(2)
        iu = re.search(r'^\s*url\s+"([^"]+)"', intel_block, re.MULTILINE)
        isha = re.search(r'^\s*sha256\s+"([a-f0-9]{64})"', intel_block, re.MULTILINE)
        au = re.search(r'^\s*url\s+"([^"]+)"', arm_block, re.MULTILINE)
        asha = re.search(r'^\s*sha256\s+"([a-f0-9]{64})"', arm_block, re.MULTILINE)
        out["arch_variants"] = {
            "intel": {"url": iu.group(1) if iu else None, "sha256": isha.group(1) if isha else None},
            "arm64": {"url": au.group(1) if au else None, "sha256": asha.group(1) if asha else None},
        }
        if out["arch_variants"].get("arm64", {}).get("url"):
            out["url"] = out["arch_variants"]["arm64"]["url"]
        if out["arch_variants"].get("arm64", {}).get("sha256"):
            out["sha256"] = out["arch_variants"]["arm64"]["sha256"]
    else:
        m = re.search(r'^\s*url\s+"([^"]+)"', rb, re.MULTILINE)
        if m: out["url"] = m.group(1)
        m = re.search(r'^\s*sha256\s+"([a-f0-9]{64})"', rb, re.MULTILINE)
        if m: out["sha256"] = m.group(1)

    if version:
        out["version"] = version
    else:
        m = re.search(r'[-_](\d+(?:\.\d+)+)\.(?:tar\.gz|tgz|zip|dmg)$', out.get("url", ""))
        if m: out["version"] = m.group(1)

    deps = re.findall(r'^\s*depends_on\s+"([^"]+)"', rb, re.MULTILINE)
    out["dependencies"] = deps
    out["build_dependencies"] = re.findall(r'^\s*depends_on\s+"([^"]+)"\s*=>\s*:build', rb, re.MULTILINE)
    out["recommended_dependencies"] = re.findall(r'^\s*depends_on\s+"([^"]+)"\s*=>\s*:recommended', rb, re.MULTILINE)
    out["optional_dependencies"] = re.findall(r'^\s*depends_on\s+"([^"]+)"\s*=>\s*:optional', rb, re.MULTILINE)
    out["deprecated"] = bool(re.search(r'^\s*deprecate!', rb, re.MULTILINE))
    out["disabled"] = bool(re.search(r'^\s*disable!', rb, re.MULTILINE))
    return out


def parse_cask_rb(rb: str, arch_token: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    m = re.search(r'^\s*cask\s+"([^"]+)"', rb, re.MULTILINE)
    if m: out["token"] = m.group(1)
    m = re.search(r'^\s*version\s+"([^"]+)"', rb, re.MULTILINE)
    if m: out["version"] = m.group(1)
    m = re.search(r'^\s*url\s+"([^"]+)"', rb, re.MULTILINE)
    if m:
        url = m.group(1).replace("#{version}", out.get("version", "")).replace("#{arch}", arch_token)
        out["url"] = url
    m = re.search(r'^\s*sha256\s+"([a-f0-9]{64})"', rb, re.MULTILINE)
    if m:
        out["sha256"] = m.group(1)
    else:
        m = re.search(r'sha256\s+arm:\s+"([a-f0-9]{64})"', rb)
        if m: out["sha256"] = m.group(1)
        m = re.search(r'sha256\s+intel:\s+"([a-f0-9]{64})"', rb)
        if m and "sha256" not in out: out["sha256"] = m.group(1)
    for field in ("desc", "homepage"):
        m = re.search(rf'^\s*{field}\s+"([^"]+)"', rb, re.MULTILINE)
        if m: out[field] = m.group(1)
    names = re.findall(r'^\s*name\s+"([^"]+)"', rb, re.MULTILINE)
    if names: out["name"] = names
    apps = []
    for m in re.finditer(r'^\s*app\s+"([^"]+)"', rb, re.MULTILINE):
        apps.append({"app": [m.group(1)]})
    out["artifacts"] = apps
    zap_block = re.search(r'zap\s+trash:\s+\[(.*?)\]', rb, re.DOTALL)
    if zap_block:
        paths = re.findall(r'"([^"]+)"', zap_block.group(1))
        if paths:
            out.setdefault("artifacts", []).append({"zap": [{"trash": paths}]})
    conflicts = re.findall(r'conflicts_with\s+cask:\s+"([^"]+)"', rb)
    if conflicts: out["conflicts_with"] = {"cask": conflicts}
    m = re.search(r'depends_on\s+macos:\s+"([^"]+)"', rb)
    if m: out["depends_on"] = {"macos": m.group(1)}
    elif re.search(r'depends_on\s+:macos', rb): out["depends_on"] = {"macos": None}
    out["deprecated"] = bool(re.search(r'^\s*deprecate!', rb, re.MULTILINE))
    out["disabled"] = bool(re.search(r'^\s*disable!', rb, re.MULTILINE))
    return out


def _class_to_token(class_name: str) -> str:
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', class_name)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', s)
    return s.lower()
