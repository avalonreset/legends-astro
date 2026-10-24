"""Public release hygiene and provenance check for legends-astro."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "CITATION.cff",
    "README.md",
    "AGENTS.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "MANIFEST.in",
    "pyproject.toml",
    "package.json",
    "docs/AGENT-RECIPE.md",
    "docs/EMPIRE.md",
    "docs/RELEASE-READINESS.md",
    "docs/router-provenance.json",
    "docs/catalog-entry.candidate.json",
    "skills/cto-legends/SKILL.md",
    "skills/legends-astro/SKILL.md",
    "knowledge/astro/pack.json",
    "vendor/astro-docs/manifest.json",
    "references/sources.md",
    "templates/migration-receipt.md",
    "wiki/index.md",
    "wiki/inspect-setup.md",
    "wiki/pages-components-styles-scripts.md",
    "wiki/content-collections.md",
    "wiki/migrate-and-wordpress.md",
    "wiki/performance-and-caching.md",
    "wiki/diagnose-and-upgrade.md",
    "wiki/latest-docs-and-ecosystem.md",
    "wiki/sources.md",
    "assets/banner.png",
    "assets/banner.webp",
    "assets/preview.png",
    "assets/social-preview.png",
    ".github/workflows/checks.yml",
]

FORBIDDEN_PATTERNS = [
    re.compile(r"C:\\Users\\", re.IGNORECASE),
    re.compile(r"E:\\", re.IGNORECASE),
    re.compile(r"/mnt/[cde]/", re.IGNORECASE),
    re.compile(r"ctolegends\.com", re.IGNORECASE),
    re.compile(r"rccol", re.IGNORECASE),
]

MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iter_repo_files():
    skip_dirs = {".git", "__pycache__", ".pytest_cache", "build", "dist", "legends_astro.egg-info"}
    for root, dirs, files in os.walk(ROOT, followlinks=False):
        rel_root = Path(root).relative_to(ROOT)
        for d in list(dirs):
            full_d = Path(root) / d
            if full_d.is_symlink() or (hasattr(full_d, "is_junction") and full_d.is_junction()):
                yield full_d
            if d in skip_dirs or d == "node_modules" or d == ".astro":
                dirs.remove(d)
        for f in sorted(files):
            yield Path(root) / f


def check_links(md_path: Path) -> list[str]:
    errors: list[str] = []
    text = md_path.read_text(encoding="utf-8")
    # Strip fenced code blocks and inline code spans before link scanning
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    stripped = re.sub(r"`[^`\n]+`", "", stripped)
    for raw_target in MARKDOWN_LINK_RE.findall(stripped):
        target = raw_target.strip().split("#", 1)[0].split("?", 1)[0]
        if not target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            continue
        resolved = (md_path.parent / target).resolve()
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            errors.append(f"{md_path.relative_to(ROOT)} escapes repo via {raw_target}")
            continue
        if not resolved.exists():
            errors.append(f"{md_path.relative_to(ROOT)} broken link: {raw_target}")
    return errors


def check_router_provenance() -> list[str]:
    errors: list[str] = []
    prov_path = ROOT / "docs" / "router-provenance.json"
    skill_path = ROOT / "skills" / "cto-legends" / "SKILL.md"
    if not prov_path.is_file() or not skill_path.is_file():
        return ["missing router provenance or skill file"]
    prov = json.loads(prov_path.read_text(encoding="utf-8"))
    if prov.get("vendored_path") != "skills/cto-legends/SKILL.md":
        errors.append("router-provenance.json vendored_path must be skills/cto-legends/SKILL.md")
    if sha256(skill_path) != prov.get("sha256"):
        errors.append("skills/cto-legends/SKILL.md hash does not match router-provenance.json")
    return errors


def check_knowledge_pack() -> list[str]:
    errors: list[str] = []
    pack_dir = ROOT / "knowledge" / "astro"
    manifest_path = pack_dir / "pack.json"
    if not manifest_path.is_file():
        return ["missing knowledge/astro/pack.json"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "legends.knowledge-pack/v1":
        errors.append("pack.json schema must be legends.knowledge-pack/v1")
    if manifest.get("module") != "legends-astro":
        errors.append("pack.json module must be legends-astro")
    for item in manifest.get("files", []):
        rel = item.get("path", "")
        vault_file = pack_dir / "vault" / rel
        if not vault_file.is_file():
            errors.append(f"missing knowledge pack file: {rel}")
            continue
        if sha256(vault_file) != item.get("sha256"):
            errors.append(f"knowledge pack hash mismatch: {rel}")
    return errors


def check_vendor_manifest() -> list[str]:
    errors: list[str] = []
    vendor_dir = ROOT / "vendor" / "astro-docs"
    manifest_path = vendor_dir / "manifest.json"
    if not manifest_path.is_file():
        return ["missing vendor/astro-docs/manifest.json"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "legends-astro-vendor-docs/v2":
        errors.append("vendor manifest schema must be legends-astro-vendor-docs/v2")
    for item in manifest.get("files", []):
        rel = item.get("localPath", "")
        f = vendor_dir / rel
        if not f.is_file():
            errors.append(f"missing vendor docs file: {rel}")
            continue
        if sha256(f) != item.get("sha256"):
            errors.append(f"vendor docs hash mismatch: {rel}")
    return errors


def main() -> int:
    errors: list[str] = []
    for rel in REQUIRED_FILES:
        if not (ROOT / rel).is_file():
            errors.append(f"missing required file: {rel}")

    if (ROOT / "canary" / "node_modules").exists():
        errors.append("canary/node_modules must not be present in public source tree")
    if (ROOT / "canary" / "dist").exists():
        errors.append("canary/dist must not be present in public source tree")
    if (ROOT / "canary" / ".astro").exists():
        errors.append("canary/.astro must not be present in public source tree")

    for path in iter_repo_files():
        rel_str = path.relative_to(ROOT).as_posix()
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            errors.append(f"symlink or junction not allowed: {rel_str}")
            continue
        if path.suffix.lower() in {".png", ".webp", ".ico", ".ttf", ".woff2"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if rel_str != "scripts/check_release.py":
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(text):
                    errors.append(f"forbidden pattern {pattern.pattern!r} in {rel_str}")
        if path.suffix.lower() == ".md" and not rel_str.startswith("vendor/"):
            errors.extend(check_links(path))
            if "\u2014" in text:
                errors.append(f"em dash found in authored markdown: {rel_str}")

    errors.extend(check_router_provenance())
    errors.extend(check_knowledge_pack())
    errors.extend(check_vendor_manifest())

    result = {"ok": not errors, "root": str(ROOT), "error_count": len(errors), "errors": errors}
    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
