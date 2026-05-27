import re
from typing import Any, Dict, List, Optional
import warnings
warnings.filterwarnings("ignore", category=SyntaxWarning)

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
CASK_PKG_PATTERN = re.compile(r'^\s*pkg\s+"([^"]+)"', re.MULTILINE)
CASK_ZAP_PATTERN = re.compile(r'zap\s+trash:\s+\[(.*?)\]', re.DOTALL)
CASK_UNINSTALL_PKGUTIL_PATTERN = re.compile(r'pkgutil:\s*"([^"]+)"')
CASK_CONFLICT_PATTERN = re.compile(r'conflicts_with\s+cask:\s+"([^"]+)"')
CASK_DEPENDS_MACOS_PATTERN = re.compile(r'depends_on\s+macos:\s+"([^"]+)"')
CASK_DEPENDS_MACOS_FLAG_PATTERN = re.compile(r'depends_on\s+:macos')
JSON_DIG_PATTERN = re.compile(r'json\.dig\(([^)]+)\)')


def _extract_do_block(rb: str, start: int) -> Optional[str]:
    depth = 0
    i = start
    while i < len(rb):
        m = re.search(r'\b(do|end)\b', rb[i:])
        if not m:
            break
        token = m.group(1)
        token_start = i + m.start()
        if token == "do":
            depth += 1
        elif token == "end":
            depth -= 1
            if depth == 0:
                return rb[start:token_start]
        i = token_start + len(token)
    return None


def _extract_ruby_paren_expr(text: str, start: int) -> Optional[str]:
    depth = 1
    i = start
    in_string = False
    string_delim = None
    in_regex = False
    escape = False
    while i < len(text):
        ch = text[i]
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif in_string:
            if ch == string_delim:
                in_string = False
        elif in_regex:
            if ch == "/":
                in_regex = False
        else:
            if ch in {"'", '"'}:
                in_string = True
                string_delim = ch
            elif ch == "/":
                in_regex = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[start:i]
        i += 1
    return None


def _parse_livecheck(rb: str, arch_token: str, version: str) -> Optional[Dict[str, Any]]:
    idx = rb.find("livecheck")
    if idx == -1:
        return None
    do_idx = rb.find("do", idx)
    if do_idx == -1:
        return None
    block = _extract_do_block(rb, do_idx)
    if not block:
        return None
    out: Dict[str, Any] = {}
    m = re.search(r'^\s*url\s+"([^"]+)"', block, re.MULTILINE)
    if m:
        url = m.group(1)
        url = url.replace("#{arch}", arch_token)
        url = url.replace("#{version}", version)
        url = url.replace("#{folder}", "mac")  # Default folder for sparkle
        out["url"] = url
    m = re.search(r'^\s*strategy\s+:(\w+)', block, re.MULTILINE)
    if m:
        out["strategy"] = m.group(1)
        if out["strategy"] == "json":
            m2 = JSON_DIG_PATTERN.search(block)
            if m2:
                out["json_path"] = [p.strip().strip('"').strip("'") for p in re.findall(r'"([^"]+)"|\'([^\']+)\'', m2.group(1)) if p]
        elif out["strategy"] == "sparkle":
            # Check for optional block like &:short_version
            if "&:short_version" in block:
                out["sparkle_version_method"] = "short_version"
            elif "&:version" in block:
                out["sparkle_version_method"] = "version"
            else:
                out["sparkle_version_method"] = "version"  # default
    
    regex_idx = block.find("regex(")
    if regex_idx != -1:
        expr = _extract_ruby_paren_expr(block, regex_idx + len("regex("))
        if expr is not None:
            regex_str = expr.strip()
            if regex_str.startswith("r\"") or regex_str.startswith("r'"):
                regex_str = regex_str[2:-1]
            elif regex_str.startswith('"') or regex_str.startswith("'"):
                regex_str = regex_str[1:-1]
            elif regex_str.startswith("/"):
                last_slash = regex_str.rfind("/")
                if last_slash > 0:
                    regex_str = regex_str[1:last_slash]
            out["regex"] = regex_str
            out["strategy"] = out.get("strategy", "regex")

    return out if out else None


def parse_formula_rb(rb: str, arch_token: str = "arm64") -> Dict[str, Any]:
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
    livecheck = _parse_livecheck(rb, arch_token, out.get("version", ""))
    if livecheck:
        out["livecheck"] = livecheck
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
    for m in CASK_PKG_PATTERN.finditer(rb):
        apps.append({"pkg": [m.group(1)]})
    out["artifacts"] = apps
    zap_block = CASK_ZAP_PATTERN.search(rb)
    if zap_block:
        paths = re.findall(r'"([^"]+)"', zap_block.group(1))
        if paths:
            out.setdefault("artifacts", []).append({"zap": [{"trash": paths}]})
    uninstall_pkgutil = CASK_UNINSTALL_PKGUTIL_PATTERN.search(rb)
    if uninstall_pkgutil:
        out["uninstall"] = {"pkgutil": uninstall_pkgutil.group(1)}
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
    livecheck = _parse_livecheck(rb, arch_token, out.get("version", ""))
    if livecheck:
        out["livecheck"] = livecheck
    return out


def _class_to_token(class_name: str) -> str:
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', class_name)
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', s)
    return s.lower()
