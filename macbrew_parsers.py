import re
from typing import Any, Dict, List, Optional

CLASS_PATTERN = re.compile(r'^\s*class\s+([A-Za-z0-9_]+)\s+<\s+Formula', re.MULTILINE)
DESC_PATTERN = re.compile(r'^\s*desc\s+"([^"]+)"', re.MULTILINE)
HOMEPAGE_PATTERN = re.compile(r'^\s*homepage\s+"([^"]+)"', re.MULTILINE)
VERSION_PATTERN = re.compile(r'^\s*version\s+"([^"]+)"', re.MULTILINE)
URL_PATTERN = re.compile(r'^\s*url\s+"([^"]+)"', re.MULTILINE)
SHA256_PATTERN = re.compile(r'^\s*sha256\s+"([a-f0-9]{64})"', re.MULTILINE)
ARCH_COND_PATTERN = re.compile(r'if\s+Hardware::CPU\.intel\?(.+?)else\s*(.+?)end', re.DOTALL)
DEPEND_PATTERN = re.compile(r'^\s*depends_on\s+"([^"]+)"', re.MULTILINE)
DEPEND_BUILD_PATTERN = re.compile(r'^\s*depends_on\s+"([^"]+)"\s*=>\s*:build', re.MULTILINE)
DEPEND_RECOMMENDED_PATTERN = re.compile(r'^\s*depends_on\s+"([^"]+)"\s*=>\s*:recommended', re.MULTILINE)
DEPEND_OPTIONAL_PATTERN = re.compile(r'^\s*depends_on\s+"([^"]+)"\s*=>\s*:optional', re.MULTILINE)
DEPRECATE_PATTERN = re.compile(r'^\s*deprecate!', re.MULTILINE)
DISABLE_PATTERN = re.compile(r'^\s*disable!', re.MULTILINE)

CASK_TOKEN_PATTERN = re.compile(r'^\s*cask\s+"([^"]+)"', re.MULTILINE)
CASK_URL_PATTERN = re.compile(r'^\s*url\s+"([^"]+)"', re.MULTILINE)
CASK_SHA256_PATTERN = re.compile(r'^\s*sha256\s+"([a-f0-9]{64})"', re.MULTILINE)
CASK_SHA256_ARM_PATTERN = re.compile(r'sha256\s+arm:\s+"([a-f0-9]{64})"')
CASK_SHA256_INTEL_PATTERN = re.compile(r'sha256\s+intel:\s+"([a-f0-9]{64})"')
CASK_FIELD_PATTERN = re.compile(r'^({})\s+"([^"]+)"'.format("|".join(["desc", "homepage"])), re.MULTILINE)
CASK_NAME_PATTERN = re.compile(r'^\s*name\s+"([^"]+)"', re.MULTILINE)
CASK_APP_PATTERN = re.compile(r'^\s*app\s+"([^"]+)"', re.MULTILINE)
CASK_ZAP_PATTERN = re.compile(r'zap\s+trash:\s+\[(.*?)\]', re.DOTALL)
CASK_CONFLICT_PATTERN = re.compile(r'conflicts_with\s+cask:\s+"([^"]+)"')
CASK_DEPENDS_MACOS_PATTERN = re.compile(r'depends_on\s+macos:\s+"([^"]+)"')
CASK_DEPENDS_MACOS_FLAG_PATTERN = re.compile(r'depends_on\s+:macos')


def parse_formula_rb(rb: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    m = CLASS_PATTERN.search(rb)
    if m:
        out["token"] = _class_to_token(m.group(1))
        out["name"] = out["token"]
        out["full_name"] = out["token"]
    m = DESC_PATTERN.search(rb)
    if m:
        out["desc"] = m.group(1)
    m = HOMEPAGE_PATTERN.search(rb)
    if m:
        out["homepage"] = m.group(1)

    version = None
    m = VERSION_PATTERN.search(rb)
    if m:
        version = m.group(1)

    m = ARCH_COND_PATTERN.search(rb)
    if m:
        intel_block = m.group(1)
        arm_block = m.group(2)
        iu = URL_PATTERN.search(intel_block)
        isha = SHA256_PATTERN.search(intel_block)
        au = URL_PATTERN.search(arm_block)
        asha = SHA256_PATTERN.search(arm_block)
        out["arch_variants"] = {
            "intel": {"url": iu.group(1) if iu else None, "sha256": isha.group(1) if isha else None},
            "arm64": {"url": au.group(1) if au else None, "sha256": asha.group(1) if asha else None},
        }
        if out["arch_variants"]["arm64"].get("url"):
            out["url"] = out["arch_variants"]["arm64"]["url"]
        if out["arch_variants"]["arm64"].get("sha256"):
            out["sha256"] = out["arch_variants"]["arm64"]["sha256"]
    else:
        m = URL_PATTERN.search(rb)
        if m:
            out["url"] = m.group(1)
        m = SHA256_PATTERN.search(rb)
        if m:
            out["sha256"] = m.group(1)

    if version:
        out["version"] = version
    else:
        m = re.search(r'[-_](\d+(?:\.\d+)+)\.(?:tar\.gz|tgz|zip|dmg)$', out.get("url", ""))
        if m:
            out["version"] = m.group(1)

    out["dependencies"] = DEPEND_PATTERN.findall(rb)
    out["build_dependencies"] = DEPEND_BUILD_PATTERN.findall(rb)
    out["recommended_dependencies"] = DEPEND_RECOMMENDED_PATTERN.findall(rb)
    out["optional_dependencies"] = DEPEND_OPTIONAL_PATTERN.findall(rb)
    out["deprecated"] = bool(DEPRECATE_PATTERN.search(rb))
    out["disabled"] = bool(DISABLE_PATTERN.search(rb))
    return out


def parse_cask_rb(rb: str, arch_token: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    m = CASK_TOKEN_PATTERN.search(rb)
    if m:
        out["token"] = m.group(1)
    m = VERSION_PATTERN.search(rb)
    if m:
        out["version"] = m.group(1)
    m = CASK_URL_PATTERN.search(rb)
    if m:
        url = m.group(1).replace("#{version}", out.get("version", "")).replace("#{arch}", arch_token)
        out["url"] = url
    m = CASK_SHA256_PATTERN.search(rb)
    if m:
        out["sha256"] = m.group(1)
    else:
        m = CASK_SHA256_ARM_PATTERN.search(rb)
        if m:
            out["sha256"] = m.group(1)
        m = CASK_SHA256_INTEL_PATTERN.search(rb)
        if m and "sha256" not in out:
            out["sha256"] = m.group(1)
    for field in ("desc", "homepage"):
        m = re.search(rf'^\s*{field}\s+"([^"]+)"', rb, re.MULTILINE)
        if m:
            out[field] = m.group(1)
    names = CASK_NAME_PATTERN.findall(rb)
    if names:
        out["name"] = names
    apps = []
    for m in CASK_APP_PATTERN.finditer(rb):
        apps.append({"app": [m.group(1)]})
    out["artifacts"] = apps
    zap_block = CASK_ZAP_PATTERN.search(rb)
    if zap_block:
        paths = re.findall(r'"([^"]+)"', zap_block.group(1))
        if paths:
            out.setdefault("artifacts", []).append({"zap": [{"trash": paths}]})
    conflicts = CASK_CONFLICT_PATTERN.findall(rb)
    if conflicts:
        out["conflicts_with"] = {"cask": conflicts}
    m = CASK_DEPENDS_MACOS_PATTERN.search(rb)
    if m:
        out["depends_on"] = {"macos": m.group(1)}
    elif CASK_DEPENDS_MACOS_FLAG_PATTERN.search(rb):
        out["depends_on"] = {"macos": None}
    out["deprecated"] = bool(DEPRECATE_PATTERN.search(rb))
    out["disabled"] = bool(DISABLE_PATTERN.search(rb))
    return out


def _class_to_token(class_name: str) -> str:
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', class_name)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', s)
    return s.lower()
