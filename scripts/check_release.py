"""Offline release checks for links, private residue, EDR hygiene, and required artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "dist", "build", "__pycache__", ".pytest_cache", ".integration", "node_modules", ".astro"}
REQUIRED = [
    ".legends-module",
    "README.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "CITATION.cff",
    "AGENTS.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "MANIFEST.in",
    "pyproject.toml",
    "package.json",
    "setup.py",
    "wiki/index.md",
    "references/sources.md",
    "docs/AGENT-RECIPE.md",
    "docs/EMPIRE.md",
    "docs/RELEASE-READINESS.md",
    "docs/router-provenance.json",
    "docs/catalog-entry.candidate.json",
    "templates/migration-receipt.md",
    "skills/cto-legends/SKILL.md",
    "knowledge/astro/pack.json",
    "vendor/astro-docs/manifest.json",
    "assets/manifest.json",
    "assets/banner.png",
    "assets/banner.webp",
    "assets/preview.png",
    "assets/social-preview.png",
    "scripts/smoke_installed_resources.py",
    "scripts/verify_knowledge_attachment.py",
    ".github/workflows/checks.yml",
]
TEXT_SUFFIXES = {".md", ".py", ".json", ".toml", ".yml", ".in", ".txt", ".cfg", ".ini", ".cff", ".mjs"}
FORBIDDEN = [
    r"(?i)[a-z]:\\users\\",
    r"(?i)\.tail[a-z0-9]+\.ts\.net",
    r"(?i)sk_live_[a-z0-9]{12,}",
    r"(?i)rk_live_[a-z0-9]{12,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",
    r"CREATE_NO_WINDOW",
    r"0x08000000",
    r"(?i)-WindowStyle\s+Hidden",
    r"(?i)-ExecutionPolicy\s+Bypass",
    r"ctypes\.windll",
    r"(?i)CryptUnprotectData",
    r"(?i)GetAsyncKeyState",
    r"(?i)IndexedDB",
    r"(?i)ctolegends\.com",
    r"(?i)rccol",
]
FORBIDDEN_FILES = {".pem", ".key", ".p12", ".pfx", ".ps1", ".bat", ".cmd", ".vbs", ".ahk"}
PNG_METADATA_CHUNKS = {"tEXt", "iTXt", "zTXt", "eXIf"}
WEBP_METADATA_CHUNKS = {b"EXIF", b"XMP "}
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def image_metadata_issues(path: Path) -> list[str]:
    """Flag raster metadata chunks that can carry authoring residue."""
    try:
        raw = path.read_bytes()
    except OSError:
        return [f"Cannot read image: {path}"]
    issues: list[str] = []
    suffix = path.suffix.lower()
    if suffix == ".png":
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            return [f"Not a PNG file: {path}"]
        offset = 8
        while offset + 8 <= len(raw):
            length = struct.unpack(">I", raw[offset : offset + 4])[0]
            chunk = raw[offset + 4 : offset + 8]
            try:
                name = chunk.decode("latin1")
            except UnicodeError:
                break
            if name in PNG_METADATA_CHUNKS:
                issues.append(f"Image metadata chunk {name} in {path.name}; strip authoring residue.")
            if name == "IEND":
                break
            offset += 12 + length
    elif suffix == ".webp":
        if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
            return [f"Not a WebP file: {path}"]
        offset = 12
        while offset + 8 <= len(raw):
            tag = raw[offset : offset + 4]
            length = struct.unpack("<I", raw[offset + 4 : offset + 8])[0]
            if tag in WEBP_METADATA_CHUNKS:
                issues.append(f"Image metadata chunk {tag!r} in {path.name}; strip authoring residue.")
                break
            offset += 8 + length + (length % 2)
    return issues


def check_links(md_path: Path, root: Path) -> list[str]:
    errors: list[str] = []
    text = md_path.read_text(encoding="utf-8-sig")
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    stripped = re.sub(r"`[^`\n]+`", "", stripped)
    rel = md_path.relative_to(root)
    for target_link in MARKDOWN_LINK_RE.findall(stripped):
        if target_link.startswith(("https://", "http://", "mailto:", "#")):
            continue
        cleaned = unquote(target_link.split("#", 1)[0].split("?", 1)[0]).strip()
        if not cleaned or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", cleaned):
            continue
        resolved = (md_path.parent / cleaned).resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
            errors.append(f"Broken or external local link: {rel}: {target_link}")
    return errors


def check(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for name in REQUIRED:
        if not (root / name).is_file():
            errors.append(f"Missing required file: {name}")

    skills_dir = root / "skills"
    if skills_dir.is_dir():
        skill_files = [
            p.relative_to(skills_dir).as_posix()
            for p in skills_dir.rglob("*")
            if p.is_file()
        ]
        if skill_files != ["cto-legends/SKILL.md"]:
            errors.append(f"One-skill rule violation under skills/: {skill_files}")

    manifest = root / "assets" / "manifest.json"
    if manifest.is_file():
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8-sig"))["files"]
        except (ValueError, KeyError):
            errors.append("assets/manifest.json is not a manifest with files.")
            entries = []
        for entry in entries:
            target = root / "assets" / entry.get("name", "")
            try:
                raw = target.read_bytes()
            except OSError:
                errors.append(f"Manifest asset missing: {entry.get('name')}")
                continue
            if len(raw) != entry.get("bytes"):
                errors.append(f"Manifest byte mismatch: {entry.get('name')}")
            if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
                errors.append(f"Manifest hash mismatch: {entry.get('name')}")

    prov_path = root / "docs" / "router-provenance.json"
    skill_path = root / "skills" / "cto-legends" / "SKILL.md"
    if prov_path.is_file() and skill_path.is_file():
        prov = json.loads(prov_path.read_text(encoding="utf-8-sig"))
        if hashlib.sha256(skill_path.read_bytes()).hexdigest() != prov.get("sha256"):
            errors.append("skills/cto-legends/SKILL.md hash does not match router-provenance.json")

    pack_path = root / "knowledge" / "astro" / "pack.json"
    if pack_path.is_file():
        try:
            pack = json.loads(pack_path.read_text(encoding="utf-8-sig"))
            vault_dir = pack_path.parent / "vault"
            for f_entry in pack.get("files", []):
                v_target = vault_dir / f_entry.get("path", "")
                if not v_target.is_file():
                    errors.append(f"Pack file missing: {f_entry.get('path')}")
                elif hashlib.sha256(v_target.read_bytes()).hexdigest() != f_entry.get("sha256"):
                    errors.append(f"Pack file hash mismatch: {f_entry.get('path')}")
        except Exception as exc:
            errors.append(f"Invalid knowledge/astro/pack.json: {exc}")

    vendor_manifest = root / "vendor" / "astro-docs" / "manifest.json"
    if vendor_manifest.is_file():
        try:
            v_data = json.loads(vendor_manifest.read_text(encoding="utf-8-sig"))
            for item in v_data.get("files", []):
                rel_p = item.get("localPath", "")
                vf = vendor_manifest.parent / rel_p
                if not vf.is_file():
                    errors.append(f"Missing vendor docs file: {rel_p}")
                elif hashlib.sha256(vf.read_bytes()).hexdigest() != item.get("sha256"):
                    errors.append(f"Vendor docs hash mismatch: {rel_p}")
        except Exception as exc:
            errors.append(f"Invalid vendor/astro-docs/manifest.json: {exc}")

    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if not path.is_file() or any(p in SKIP or p.endswith(".egg-info") for p in rel.parts):
            continue
        if path.suffix.lower() in FORBIDDEN_FILES:
            errors.append(f"Forbidden file extension must not ship: {rel}")
            continue
        if path.suffix.lower() in {".png", ".webp"}:
            errors.extend(f"{rel}: {issue}" for issue in image_metadata_issues(path))
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name not in {"LICENSE", "NOTICE", ".legends-module"}:
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        if path.name != "check_release.py":
            for pattern in FORBIDDEN:
                if re.search(pattern, text):
                    errors.append(f"Possible private or EDR-sensitive residue ({pattern}): {rel}")
        if path.suffix != ".md" or rel.as_posix().startswith("vendor/"):
            continue
        if "\u2014" in text or "\u2013" in text:
            errors.append(f"Disallowed dash in copy: {rel}")
        errors.extend(check_links(path, root))
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Offline release checks.")
    parser.add_argument("--root", type=Path, default=ROOT)
    options = parser.parse_args()
    failures = check(options.root)
    for failure in failures:
        print(failure)
    print(f"Release checks: {'FAIL' if failures else 'PASS'} ({len(failures)} issues)")
    sys.exit(bool(failures))
