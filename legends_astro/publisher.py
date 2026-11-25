"""Day-2 Article-to-Live publisher, updater, safe slug renamer, image localizer, and editorial SEO preflight linter."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .net_safety import (
    DEFAULT_MAX_ASSET_BYTES,
    DEFAULT_MAX_PAGE_BYTES,
    assert_path_within,
    canonicalize_route,
    is_reparse_or_symlink,
    reject_controlled_folder_access,
    reject_symlink_chain,
    safe_read_bytes,
    safe_write_bytes,
    safe_write_text,
    sanitize_rel_path,
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_WINDOWS_RESERVED_SLUGS = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
)
_FIELD_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PLACEHOLDER_CASE_SENSITIVE_RE = re.compile(r"\b(?:TBD|TODO|FIXME|INSERT_[A-Z0-9_]*)\b")
_PLACEHOLDER_CASE_INSENSITIVE_RE = re.compile(r"\[placeholder\]|\blorem\s+ipsum\b", re.IGNORECASE)
_SVG_XSS_RE = re.compile(
    r"<\s*script\b|on[a-z]+\s*=|javascript\s*:|<\s*foreignobject\b|<!\s*entity\b|<!\s*doctype\b",
    re.IGNORECASE,
)
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\r\n]+)\)")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

ALLOWED_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg"})

_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "headline"),
    "description": ("description", "excerpt", "summary"),
    "pubDate": ("pubDate", "publishedAt", "publishedDate", "date"),
    "updatedDate": ("updatedDate", "updatedAt", "modifiedDate", "lastModified"),
    "author": ("author", "byline"),
    "categories": ("categories",),
    "tags": ("tags",),
    "heroImage": ("heroImage", "featuredImage", "image", "cover", "coverImage", "thumbnail"),
    "heroAlt": ("heroAlt", "imageAlt", "coverAlt", "featuredImageAlt"),
    "canonicalUrl": ("canonicalUrl", "canonical"),
    "draft": ("draft", "isDraft"),
}


def validate_slug(raw_slug: str, *, label: str = "slug") -> str:
    """Strictly validate a content slug (CWE-22 traversal and Windows reserved name prevention)."""
    if not isinstance(raw_slug, str):
        raise ValueError(f"Invalid {label}: expected string.")
    if "\x00" in raw_slug or "/" in raw_slug or "\\" in raw_slug or ".." in raw_slug:
        raise ValueError(f"Refusing unsafe {label} (CWE-22): {raw_slug!r}")
    slug = raw_slug.strip()
    if not slug or len(slug) > 120 or not _SLUG_RE.match(slug):
        raise ValueError(
            f"Invalid {label} {raw_slug!r}: must match ^[a-z0-9]+(?:-[a-z0-9]+)*$ (lowercase ASCII letters, digits, hyphens)."
        )
    if slug.lower() in _WINDOWS_RESERVED_SLUGS:
        raise ValueError(f"Refusing Windows reserved {label}: {raw_slug!r}")
    return slug


def _slugify_title(title: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)[:100].strip("-")
    if not cleaned or cleaned in _WINDOWS_RESERVED_SLUGS:
        cleaned = f"post-{cleaned}" if cleaned else "post"
    return validate_slug(cleaned, label="generated slug")


def _validate_date_str(raw_date: str, *, label: str = "date") -> str:
    cleaned = str(raw_date).strip()
    if not cleaned or _CONTROL_CHAR_RE.search(cleaned) or not _DATE_RE.match(cleaned):
        raise ValueError(f"Invalid {label} {raw_date!r}: expected YYYY-MM-DD or ISO-8601 timestamp.")
    # Verify calendar date validity
    try:
        datetime.strptime(cleaned[:10], "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid calendar {label} {raw_date!r}: {exc}") from exc
    return cleaned


def _today_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_scheduled_date(date_val: Any) -> bool:
    if not date_val:
        return False
    s = str(date_val).strip()
    if len(s) < 10:
        return False
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if len(s) > 10 and "T" in s:
            iso = s.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed > now
        return dt.date() > now.date()
    except ValueError:
        return False


def validate_image_bytes(ext: str, raw_bytes: bytes, *, source_label: str = "<image>") -> None:
    """Validate image extension allowlist, magic bytes, and SVG XSS safety (CWE-79)."""
    suffix = ext.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError(
            f"Refusing unsupported image extension '{ext}' for {source_label} (allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))})."
        )
    if not raw_bytes:
        raise ValueError(f"Refusing empty image file: {source_label}")

    if suffix == ".png":
        if not raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"Image magic-byte mismatch for PNG file {source_label}.")
    elif suffix in (".jpg", ".jpeg"):
        if not raw_bytes.startswith(b"\xff\xd8\xff"):
            raise ValueError(f"Image magic-byte mismatch for JPEG file {source_label}.")
    elif suffix == ".gif":
        if not raw_bytes.startswith((b"GIF87a", b"GIF89a")):
            raise ValueError(f"Image magic-byte mismatch for GIF file {source_label}.")
    elif suffix == ".webp":
        if len(raw_bytes) < 12 or raw_bytes[:4] != b"RIFF" or raw_bytes[8:12] != b"WEBP":
            raise ValueError(f"Image magic-byte mismatch for WebP file {source_label}.")
    elif suffix == ".avif":
        has_avif_brand = len(raw_bytes) >= 12 and (
            raw_bytes[4:12] in (b"ftypavif", b"ftypavis")
            or (raw_bytes[4:12] == b"ftypmif1" and b"avif" in raw_bytes[12:36])
        )
        if not has_avif_brand:
            raise ValueError(f"Image magic-byte mismatch for AVIF file {source_label}.")
    elif suffix == ".svg":
        try:
            svg_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"SVG file {source_label} is not valid UTF-8: {exc}") from exc
        if "<svg" not in svg_text.lower():
            raise ValueError(f"SVG file {source_label} does not contain an <svg> root element.")
        if _SVG_XSS_RE.search(svg_text):
            raise ValueError(
                f"Refusing unsafe SVG {source_label} containing active script, event handler, foreignObject, or XML entity/DOCTYPE (CWE-79)."
            )


def _extract_balanced_block(text: str, open_idx: int, open_ch: str = "{", close_ch: str = "}") -> str:
    depth = 0
    in_str: str | None = None
    escaped = False
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if in_str is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in ('"', "'", "`"):
            in_str = ch
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[open_idx : idx + 1]
    return text[open_idx:]


def inspect_content_config(project_dir: Path, collection: str) -> dict[str, Any]:
    """Statically inspect src/content.config.{ts,mjs,js} without executing JS/TS."""
    result: dict[str, Any] = {
        "configPath": None,
        "collections": {},
        "collectionBase": None,
        "detectedFields": [],
        "fieldMap": {role: aliases[0] for role, aliases in _ROLE_ALIASES.items()},
    }
    cfg_path: Path | None = None
    for cand_name in ("src/content.config.ts", "src/content.config.mjs", "src/content.config.js"):
        cand = project_dir / cand_name
        if cand.is_file() and not is_reparse_or_symlink(cand):
            assert_path_within(project_dir, cand)
            cfg_path = cand
            break
    if cfg_path is None:
        return result

    result["configPath"] = cfg_path.relative_to(project_dir).as_posix()
    text = safe_read_bytes(cfg_path, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8", errors="replace")

    # Find defineCollection blocks declared via `const <name> = defineCollection({ ... })` or `<name>: defineCollection({ ... })`
    col_pattern = re.compile(
        r"(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*defineCollection\s*\(\s*\{"
        r"|([A-Za-z_][A-Za-z0-9_]*)\s*:\s*defineCollection\s*\(\s*\{",
    )
    collections_info: dict[str, dict[str, Any]] = {}
    for m in col_pattern.finditer(text):
        col_name = m.group(1) or m.group(2)
        brace_idx = m.end() - 1
        block = _extract_balanced_block(text, brace_idx, "{", "}")
        base_match = re.search(r"""base\s*:\s*["']([^"'\r\n]+)["']""", block)
        base_rel = None
        if base_match:
            try:
                base_rel = sanitize_rel_path(base_match.group(1))
            except ValueError:
                base_rel = None

        fields: list[str] = []
        schema_match = re.search(r"z\.object\s*\(\s*\{", block)
        if schema_match:
            schema_block = _extract_balanced_block(block, schema_match.end() - 1, "{", "}")
            for fm in re.finditer(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:", schema_block):
                fname = fm.group(1)
                if fname not in fields:
                    fields.append(fname)

        collections_info[col_name] = {
            "base": base_rel,
            "fields": fields,
        }

    result["collections"] = collections_info
    selected = collections_info.get(collection)
    if selected:
        result["collectionBase"] = selected.get("base")
        detected_fields = list(selected.get("fields", []))
        result["detectedFields"] = detected_fields
        field_map = dict(result["fieldMap"])
        for role, aliases in _ROLE_ALIASES.items():
            for alias in aliases:
                if alias in detected_fields:
                    field_map[role] = alias
                    break
        result["fieldMap"] = field_map
    return result


def _inspect_astro_trailing_slash(project_dir: Path) -> str:
    """Statically inspect astro.config.{mjs,ts,js,cjs} for trailingSlash setting (zero execution)."""
    for cand_name in ("astro.config.mjs", "astro.config.ts", "astro.config.js", "astro.config.cjs"):
        cand = project_dir / cand_name
        if cand.is_file() and not is_reparse_or_symlink(cand):
            assert_path_within(project_dir, cand)
            text = safe_read_bytes(cand, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8", errors="replace")
            m = re.search(r"""trailingSlash\s*:\s*["'](always|never|ignore)["']""", text)
            if m:
                return m.group(1)
            break
    return "always"


def _detect_layout_renders_h1(project_dir: Path) -> bool:
    """Statically check if project Astro layouts render an <h1> element."""
    layouts_dir = project_dir / "src" / "layouts"
    if not layouts_dir.is_dir() or is_reparse_or_symlink(layouts_dir):
        return False
    for af in sorted(layouts_dir.rglob("*.astro")):
        if is_reparse_or_symlink(af):
            continue
        assert_path_within(project_dir, af)
        txt = safe_read_bytes(af, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8", errors="replace")
        if re.search(r"<h1(?:\s|>)", txt, re.IGNORECASE):
            return True
    return False


def _parse_yaml_scalar(raw_val: str) -> Any:
    val = raw_val.strip()
    if val == "":
        return ""
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if val.startswith(("{", "[", '"')):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            pass
    if val.startswith("'") and val.endswith("'") and len(val) >= 2:
        return val[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", val):
        try:
            return int(val)
        except ValueError:
            pass
    return val


def parse_frontmatter(raw_text: str) -> tuple[dict[str, Any], list[tuple[str, str]], str]:
    """Parse YAML frontmatter without external dependencies while preserving raw unknown blocks."""
    text = raw_text.lstrip("\ufeff")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, [], text

    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return {}, [], text

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1 :]).lstrip("\r\n")
    parsed: dict[str, Any] = {}
    raw_entries: list[tuple[str, str]] = []

    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$", line)
        if not m:
            i += 1
            continue
        key = m.group(1)
        rest = m.group(2).strip()
        block_lines = [line]
        j = i + 1
        while j < len(fm_lines):
            nxt = fm_lines[j]
            if not nxt.strip():
                # Peek ahead: only include blank line if followed by indented line
                if j + 1 < len(fm_lines) and (fm_lines[j + 1].startswith(" ") or fm_lines[j + 1].startswith("\t")):
                    block_lines.append(nxt)
                    j += 1
                    continue
                break
            if nxt.startswith(" ") or nxt.startswith("\t") or (rest == "" and nxt.lstrip().startswith("- ")):
                block_lines.append(nxt)
                j += 1
            else:
                break

        raw_block = "\n".join(block_lines)
        raw_entries.append((key, raw_block))

        if rest == "" and len(block_lines) > 1:
            sub_lines = [bl.strip() for bl in block_lines[1:] if bl.strip()]
            if sub_lines and all(sl.startswith("- ") for sl in sub_lines):
                parsed[key] = [_parse_yaml_scalar(sl[2:]) for sl in sub_lines]
            else:
                parsed[key] = "\n".join(block_lines[1:])
        else:
            parsed[key] = _parse_yaml_scalar(rest)
        i = j

    return parsed, raw_entries, body


def _sanitize_single_line(val: str) -> str:
    return re.sub(r"[\r\n]+", " ", val).strip()


def _serialize_frontmatter(
    managed_ordered: list[tuple[str, Any]],
    preserved_raw_entries: list[tuple[str, str]],
) -> str:
    """Serialize YAML frontmatter safely via JSON quoting (CWE-74 immune) while preserving unknown custom keys."""
    lines = ["---"]
    managed_keys = {k for k, _ in managed_ordered}
    for key, val in managed_ordered:
        if val is None:
            continue
        if isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        elif isinstance(val, list):
            clean_items = [_sanitize_single_line(str(item)) for item in val if str(item).strip()]
            lines.append(f"{key}: {json.dumps(clean_items, ensure_ascii=False)}")
        elif isinstance(val, (int, float)):
            lines.append(f"{key}: {val}")
        elif isinstance(val, dict):
            lines.append(f"{key}: {json.dumps(val, ensure_ascii=False)}")
        else:
            clean_str = _sanitize_single_line(str(val))
            lines.append(f"{key}: {json.dumps(clean_str, ensure_ascii=False)}")

    seen_preserved: set[str] = set()
    for key, raw_block in preserved_raw_entries:
        if key in managed_keys or key in seen_preserved:
            continue
        seen_preserved.add(key)
        # Ensure preserved block contains no control chars
        if _CONTROL_CHAR_RE.search(raw_block):
            continue
        lines.append(raw_block)

    lines.append("---")
    return "\n".join(lines) + "\n"


def _strip_leading_body_h1(body: str) -> tuple[str, bool]:
    """Strip a leading Markdown '# H1' heading line if present."""
    lines = body.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and re.match(r"^#\s+\S", lines[idx]):
        remaining = lines[idx + 1 :]
        while remaining and not remaining[0].strip():
            remaining.pop(0)
        return "\n".join(remaining).rstrip() + ("\n" if remaining else ""), True
    return body, False


def _body_has_h1(body: str) -> bool:
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in body.splitlines():
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            continue
        if not in_fence and re.match(r"^#\s+\S", line):
            return True
    return False


def _count_words(text: str) -> int:
    # Strip fenced code blocks and markdown syntax lightly
    cleaned = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    cleaned = re.sub(r"[#*_`>\[\]()!|-]+", " ", cleaned)
    return len([w for w in cleaned.split() if w])


def _extract_image_target(raw_paren: str) -> tuple[str, str]:
    """Split Markdown image destination and optional title suffix."""
    s = raw_paren.strip()
    if s.startswith("<") and ">" in s:
        end = s.index(">")
        return s[1:end].strip(), s[end + 1 :]
    m = re.match(r"""^(\S+)(\s+(?:"[^"]*"|'[^']*'))?\s*$""", s)
    if m:
        return m.group(1), m.group(2) or ""
    return s, ""


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_POSIX_SYSTEM_ABS_PREFIXES = (
    "/etc/",
    "/tmp/",
    "/var/",
    "/usr/",
    "/home/",
    "/root/",
    "/Users/",
    "/private/",
    "/proc/",
    "/sys/",
    "/dev/",
)


def _resolve_local_image_path(
    target: str,
    search_roots: list[Path],
    *,
    confine_to_roots: bool = False,
) -> Path | None:
    if "\x00" in target:
        raise ValueError(f"Refusing NUL byte in image path (CWE-22, inline_image_path_escape): {target!r}")
    if confine_to_roots:
        low = target.lower()
        if (
            target.startswith("\\")
            or target.startswith("~")
            or low.startswith("file:")
            or _WINDOWS_DRIVE_RE.match(target)
            or Path(target).is_absolute()
        ):
            raise ValueError(
                f"Refusing absolute filesystem path in inline Markdown image (CWE-22, inline_image_path_escape): {target!r}"
            )
        p = Path(target)
        for root in search_roots:
            root_resolved = root.resolve()
            cand = root_resolved / p
            reject_symlink_chain(cand)
            try:
                cand_resolved = cand.resolve()
                assert_path_within(root_resolved, cand_resolved)
            except ValueError as exc:
                raise ValueError(
                    f"Refusing inline Markdown image path escaping input directory (CWE-22, inline_image_path_escape): {target!r}"
                ) from exc
            if cand_resolved.is_file():
                return cand_resolved
        return None

    p = Path(target).expanduser()
    if p.is_absolute():
        reject_symlink_chain(p)
        return p if p.is_file() else None
    for root in search_roots:
        cand = root / p
        reject_symlink_chain(cand)
        if cand.is_file():
            return cand.resolve()
    return None


def _localize_images(
    *,
    project_dir: Path,
    collection: str = "blog",
    slug: str,
    hero_src: str | None,
    body: str,
    search_roots: list[Path],
    inline_search_roots: list[Path] | None = None,
    confine_hero: bool = False,
) -> tuple[str | None, str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and plan localization of --hero and inline local Markdown images into public/images/<collection>/<slug>/."""
    planned_assets: list[dict[str, Any]] = []
    lint_errors: list[dict[str, Any]] = []
    used_names: set[str] = set()
    localized_by_source: dict[Path, str] = {}
    safe_collection = sanitize_rel_path(collection).strip("/") or "blog"
    eff_inline_roots = list(inline_search_roots) if inline_search_roots is not None else list(search_roots)

    def plan_local_file(img_path: Path, source_label: str) -> str:
        resolved = img_path.resolve()
        if resolved in localized_by_source:
            return localized_by_source[resolved]
        reject_symlink_chain(img_path)
        raw_bytes = safe_read_bytes(resolved, max_bytes=DEFAULT_MAX_ASSET_BYTES)
        validate_image_bytes(resolved.suffix, raw_bytes, source_label=source_label)

        safe_name = sanitize_rel_path(resolved.name)
        base_stem = Path(safe_name).stem or "image"
        suffix = Path(safe_name).suffix.lower()
        cand_name = f"{base_stem}{suffix}"
        counter = 2
        while cand_name in used_names:
            cand_name = f"{base_stem}-{counter}{suffix}"
            counter += 1
        used_names.add(cand_name)

        rel_dest = f"public/images/{safe_collection}/{slug}/{cand_name}"
        public_url = f"/images/{safe_collection}/{slug}/{cand_name}"
        dest_abs = project_dir / rel_dest
        assert_path_within(project_dir, dest_abs)
        planned_assets.append(
            {
                "source": str(resolved),
                "path": rel_dest,
                "publicUrl": public_url,
                "bytes": len(raw_bytes),
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "data": raw_bytes,
            }
        )
        localized_by_source[resolved] = public_url
        return public_url

    final_hero = hero_src
    if hero_src:
        h = hero_src.strip()
        if h.startswith(("https://", "http://")):
            final_hero = h
        elif h.startswith("/"):
            try:
                safe_rel = sanitize_rel_path(h.lstrip("/"))
                pub_file = project_dir / "public" / safe_rel
                assert_path_within(project_dir / "public", pub_file)
                if not pub_file.is_file():
                    lint_errors.append(
                        {
                            "code": "broken_local_image",
                            "target": h,
                            "message": f"Hero image '{h}' does not exist under public/.",
                        }
                    )
            except ValueError as exc:
                lint_errors.append(
                    {
                        "code": "broken_local_image",
                        "target": h,
                        "message": f"Invalid hero image path '{h}': {exc}",
                    }
                )
            final_hero = h
        else:
            hero_roots = eff_inline_roots if confine_hero else search_roots
            found = _resolve_local_image_path(h, hero_roots, confine_to_roots=confine_hero)
            if found is None:
                raise FileNotFoundError(f"Hero image file not found: {hero_src}")
            final_hero = plan_local_file(found, hero_src)

    # Process inline Markdown images outside fenced code blocks
    out_lines: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in body.splitlines():
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            out_lines.append(line)
            continue

        if in_fence or "![" not in line:
            out_lines.append(line)
            continue

        def _replace_img(match: re.Match[str]) -> str:
            alt_text = match.group(1)
            raw_paren = match.group(2)
            target, title_suffix = _extract_image_target(raw_paren)
            if not alt_text.strip():
                lint_errors.append(
                    {
                        "code": "missing_inline_image_alt",
                        "target": target,
                        "message": f"Inline image '{target}' is missing alt text.",
                    }
                )
            low_t = target.lower()
            if not target or low_t.startswith(("https://", "http://", "//", "data:")):
                return match.group(0)
            if target.startswith("/"):
                if "\x00" in target or target.startswith("/\\") or ".." in Path(target.replace("\\", "/")).parts:
                    raise ValueError(
                        f"Refusing unsafe inline Markdown image path (CWE-22, inline_image_path_escape): {target!r}"
                    )
                try:
                    safe_rel = sanitize_rel_path(target.lstrip("/"))
                    pub_file = project_dir / "public" / safe_rel
                    assert_path_within(project_dir / "public", pub_file)
                except ValueError as exc:
                    raise ValueError(
                        f"Refusing unsafe inline Markdown image path (CWE-22, inline_image_path_escape): {target!r}: {exc}"
                    ) from exc
                already_planned = any(a["publicUrl"] == target for a in planned_assets)
                if not pub_file.is_file() and not already_planned:
                    if target.startswith(_POSIX_SYSTEM_ABS_PREFIXES) or (os.name != "nt" and Path(target).is_file()):
                        raise ValueError(
                            f"Refusing absolute filesystem path in inline Markdown image (CWE-22, inline_image_path_escape): {target!r}"
                        )
                    lint_errors.append(
                        {
                            "code": "broken_local_image",
                            "target": target,
                            "message": f"Inline image '{target}' does not exist under public/.",
                        }
                    )
                return match.group(0)

            found = _resolve_local_image_path(target, eff_inline_roots, confine_to_roots=True)
            if found is None:
                lint_errors.append(
                    {
                        "code": "broken_local_image",
                        "target": target,
                        "message": f"Local inline image file not found: '{target}'.",
                    }
                )
                return match.group(0)

            public_url = plan_local_file(found, target)
            return f"![{alt_text}]({public_url}{title_suffix})"

        out_lines.append(_MD_IMAGE_RE.sub(_replace_img, line))

    new_body = "\n".join(out_lines)
    if body.endswith("\n") and not new_body.endswith("\n"):
        new_body += "\n"
    return final_hero, new_body, planned_assets, lint_errors


def lint_post(
    *,
    title: str,
    description: str,
    body: str,
    hero_image: str | None,
    hero_alt: str | None,
    layout_has_h1: bool,
    raw_frontmatter_values: Iterable[str] = (),
    extra_errors: Iterable[dict[str, Any]] = (),
    strict_typography: bool = False,
) -> dict[str, Any]:
    """Run editorial SEO preflight lint returning {ok, errors, warnings}."""
    errors: list[dict[str, Any]] = list(extra_errors)
    warnings: list[dict[str, Any]] = []

    if not title or not title.strip():
        errors.append({"code": "missing_title", "message": "Post is missing a non-empty title."})
    else:
        tlen = len(title.strip())
        if tlen < 30 or tlen > 65:
            warnings.append(
                {
                    "code": "title_length",
                    "length": tlen,
                    "message": f"Title length is {tlen} chars (recommended 30..65 chars).",
                }
            )

    if hero_image and (not hero_alt or not hero_alt.strip()):
        errors.append(
            {
                "code": "missing_hero_alt",
                "message": "Hero image was provided without alt text (pass --hero-alt or set heroAlt).",
            }
        )

    for raw_v in raw_frontmatter_values:
        if raw_v and _CONTROL_CHAR_RE.search(str(raw_v)):
            errors.append(
                {
                    "code": "frontmatter_control_chars",
                    "message": "Frontmatter field contains disallowed ASCII control characters.",
                }
            )
            break

    # Placeholder markers in title, description, and body (outside fenced code blocks)
    body_no_code = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    for field_name, sample in (("title", title), ("description", description), ("body", body_no_code)):
        if not sample:
            continue
        m_cs = _PLACEHOLDER_CASE_SENSITIVE_RE.search(sample)
        m_ci = _PLACEHOLDER_CASE_INSENSITIVE_RE.search(sample)
        hit = m_cs.group(0) if m_cs else (m_ci.group(0) if m_ci else None)
        if hit:
            errors.append(
                {
                    "code": "placeholder_content",
                    "field": field_name,
                    "marker": hit,
                    "message": f"Placeholder marker '{hit}' found in {field_name}.",
                }
            )

    if not description or not description.strip():
        warnings.append(
            {
                "code": "missing_description",
                "message": "Post is missing a meta description (recommended 110..160 chars).",
            }
        )
    else:
        dlen = len(description.strip())
        if dlen < 110 or dlen > 160:
            warnings.append(
                {
                    "code": "description_length",
                    "length": dlen,
                    "message": f"Description length is {dlen} chars (recommended 110..160 chars).",
                }
            )

    if layout_has_h1 and _body_has_h1(body):
        warnings.append(
            {
                "code": "duplicate_h1",
                "message": "Project layout already renders <h1>; remove '# Heading' from body or pass --strip-body-h1.",
            }
        )

    if strict_typography:
        for field_name, sample in (("title", title), ("description", description), ("body", body)):
            if "\u2014" in sample or "\u2013" in sample:
                warnings.append(
                    {
                        "code": "disallowed_dash",
                        "field": field_name,
                        "message": f"Em-dash or en-dash found in {field_name} under --strict-typography.",
                    }
                )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


def _format_collection_route(collection: str, slug: str, trailing_slash: str) -> str:
    prefix = "" if collection == "pages" else f"/{collection}"
    base = f"{prefix}/{slug}"
    if trailing_slash == "never":
        return base
    return base + "/"


def _norm_redirect_key(route: str) -> str:
    r = canonicalize_route(route)
    if r == "/":
        return "/"
    return r.rstrip("/")


def _plan_redirects_update(
    *,
    project_dir: Path,
    old_route: str,
    new_route: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Compute updated public/_redirects content with deduplication, chain flattening, and cycle refusal."""
    norm_old = _norm_redirect_key(old_route)
    norm_new = _norm_redirect_key(new_route)
    if norm_old == norm_new:
        raise ValueError(f"Refusing self-redirect in public/_redirects: {old_route} -> {new_route}")

    redirects_path = project_dir / "public" / "_redirects"
    existing_lines: list[str] = []
    if redirects_path.exists():
        reject_symlink_chain(redirects_path)
        assert_path_within(project_dir, redirects_path)
        existing_text = safe_read_bytes(redirects_path, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode(
            "utf-8", errors="replace"
        )
        existing_lines = existing_text.splitlines()

    # Build graph of existing rules (excluding any existing rule from norm_old which will be replaced)
    graph: dict[str, str] = {}
    for raw in existing_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        src_k = _norm_redirect_key(parts[0])
        dst_k = _norm_redirect_key(parts[1])
        if src_k == norm_old:
            continue
        graph[src_k] = dst_k

    # Detect cycle: does norm_new -> ... -> norm_old exist?
    visited: set[str] = {norm_old}
    cur: str | None = norm_new
    while cur is not None:
        if cur in visited:
            raise ValueError(
                f"Refusing redirect cycle in public/_redirects: adding {old_route} -> {new_route} creates a cycle back to {cur}."
            )
        visited.add(cur)
        cur = graph.get(cur)

    # Rebuild lines: flatten any rule pointing to norm_old -> new_route, drop existing rule from norm_old
    updated_lines: list[str] = []
    flattened_count = 0
    for raw in existing_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            updated_lines.append(raw)
            continue
        parts = stripped.split()
        if len(parts) < 2:
            updated_lines.append(raw)
            continue
        src_r, dst_r = parts[0], parts[1]
        rest_parts = parts[2:]
        if _norm_redirect_key(src_r) == norm_old:
            # Replaced by the new canonical 301 rule below (deduplicated)
            continue
        if _norm_redirect_key(dst_r) == norm_old:
            status_tok = rest_parts[0] if rest_parts else "301"
            updated_lines.append(f"{src_r} {new_route} {status_tok}")
            flattened_count += 1
        else:
            updated_lines.append(raw)

    updated_lines.append(f"{old_route} {new_route} 301")
    content = "\n".join(updated_lines).rstrip("\n") + "\n"
    rules_summary = [{"source": old_route, "destination": new_route, "status": 301, "flattenedChains": flattened_count}]
    return content, rules_summary


def _resolve_content_dir(
    project_dir: Path,
    collection: str,
    content_dir_override: str | None,
    config_info: dict[str, Any],
) -> tuple[str, Path]:
    if content_dir_override:
        safe_rel = sanitize_rel_path(content_dir_override)
        target = project_dir / safe_rel
        assert_path_within(project_dir, target)
        return collection, target

    if config_info.get("collectionBase"):
        target = project_dir / config_info["collectionBase"]
        assert_path_within(project_dir, target)
        return collection, target

    default_dir = project_dir / "src" / "content" / collection
    if default_dir.is_dir():
        assert_path_within(project_dir, default_dir)
        return collection, default_dir

    if collection == "blog":
        pages_dir = project_dir / "src" / "content" / "pages"
        if pages_dir.is_dir() and not default_dir.exists():
            assert_path_within(project_dir, pages_dir)
            return "pages", pages_dir

    assert_path_within(project_dir, default_dir)
    return collection, default_dir


def _find_existing_post_file(content_dir: Path, slug: str) -> Path | None:
    for ext in (".md", ".mdx"):
        cand = content_dir / f"{slug}{ext}"
        if cand.exists():
            reject_symlink_chain(cand)
            if cand.is_file():
                return cand
    return None


def _parse_field_map_overrides(raw_overrides: Iterable[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw_overrides or ():
        if "=" not in str(item):
            raise ValueError(f"Invalid --field-map entry {item!r}: expected key=customKey.")
        k, v = str(item).split("=", 1)
        k = k.strip()
        v = v.strip()
        if k not in _ROLE_ALIASES:
            raise ValueError(
                f"Unsupported --field-map role {k!r}; expected one of {', '.join(sorted(_ROLE_ALIASES))}."
            )
        if not _FIELD_KEY_RE.match(v):
            raise ValueError(f"Invalid --field-map custom key {v!r}: must be a valid frontmatter identifier.")
        out[k] = v
    return out


def publish_post(
    *,
    project_dir: str | Path,
    collection: str = "blog",
    content_dir_override: str | None = None,
    list_only: bool = False,
    update: bool = False,
    rename_from: str | None = None,
    input_path: str | Path | None = None,
    body_file: str | Path | None = None,
    slug: str | None = None,
    title: str | None = None,
    description: str | None = None,
    author: str | None = None,
    tags: list[str] | None = None,
    categories: list[str] | None = None,
    pub_date: str | None = None,
    updated_date: str | None = None,
    no_updated_date: bool = False,
    hero: str | None = None,
    hero_alt: str | None = None,
    draft_flag: bool = False,
    publish_flag: bool = False,
    strip_body_h1: bool = False,
    strict_typography: bool = False,
    field_map_overrides: Iterable[str] | None = None,
    apply: bool = False,
    confirm_plan: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Day-2 post publisher supporting --list, create, --update, and --rename-from with preview-first planSha256."""
    if draft_flag and publish_flag:
        raise ValueError("Cannot pass both --draft and --publish.")

    raw_proj = Path(project_dir).expanduser()
    reject_symlink_chain(raw_proj)
    project = raw_proj.resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"Project directory does not exist: {project}")
    if not list_only:
        reject_controlled_folder_access(project)

    if not _FIELD_KEY_RE.match(collection):
        raise ValueError(f"Invalid --collection name: {collection!r}")

    cfg_info = inspect_content_config(project, collection)
    resolved_collection, content_dir = _resolve_content_dir(project, collection, content_dir_override, cfg_info)
    # Re-inspect field map if collection fell back to pages
    if resolved_collection != collection:
        cfg_info = inspect_content_config(project, resolved_collection)
    field_map = dict(cfg_info["fieldMap"])
    field_map.update(_parse_field_map_overrides(field_map_overrides))
    layout_has_h1 = _detect_layout_renders_h1(project)

    def _fm_get(fm_dict: dict[str, Any], role: str, default: Any = None) -> Any:
        mapped_key = field_map[role]
        if mapped_key in fm_dict:
            return fm_dict[mapped_key]
        for alias in _ROLE_ALIASES[role]:
            if alias in fm_dict:
                return fm_dict[alias]
        return default

    # ------------------------------------------------------------------
    # Inventory mode (--list)
    # ------------------------------------------------------------------
    if list_only:
        entries: list[dict[str, Any]] = []
        if content_dir.is_dir():
            reject_symlink_chain(content_dir)
            for md_file in sorted(content_dir.rglob("*")):
                if not md_file.is_file() or md_file.suffix.lower() not in (".md", ".mdx"):
                    continue
                if is_reparse_or_symlink(md_file):
                    raise ValueError(f"Refusing symlink or reparse-point content file (CWE-59): {md_file}")
                assert_path_within(project, md_file)
                raw = safe_read_bytes(md_file, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8", errors="replace")
                fm, _, body_txt = parse_frontmatter(raw)
                rel_in_col = md_file.relative_to(content_dir).as_posix()
                entry_id = re.sub(r"\.(?:md|mdx)$", "", rel_in_col, flags=re.IGNORECASE)
                entry_slug = md_file.stem
                e_title = str(_fm_get(fm, "title", "") or "")
                e_desc = str(_fm_get(fm, "description", "") or "")
                e_pub = _fm_get(fm, "pubDate")
                e_upd = _fm_get(fm, "updatedDate")
                e_draft = bool(_fm_get(fm, "draft", False) is True)
                e_hero = _fm_get(fm, "heroImage")
                e_hero_alt = _fm_get(fm, "heroAlt")
                e_lint = lint_post(
                    title=e_title,
                    description=e_desc,
                    body=body_txt,
                    hero_image=str(e_hero) if e_hero else None,
                    hero_alt=str(e_hero_alt) if e_hero_alt else None,
                    layout_has_h1=layout_has_h1,
                    strict_typography=strict_typography,
                )
                entries.append(
                    {
                        "id": entry_id,
                        "slug": entry_slug,
                        "file": md_file.relative_to(project).as_posix(),
                        "title": e_title or None,
                        "description": e_desc or None,
                        "pubDate": str(e_pub) if e_pub else None,
                        "updatedDate": str(e_upd) if e_upd else None,
                        "draft": e_draft,
                        "scheduled": _is_scheduled_date(e_pub),
                        "heroImage": str(e_hero) if e_hero else None,
                        "wordCount": _count_words(body_txt),
                        "lint": e_lint,
                    }
                )
        entries.sort(key=lambda e: (str(e.get("pubDate") or ""), e["id"]), reverse=True)
        return {
            "ok": True,
            "mode": "list",
            "projectDir": str(project),
            "collection": resolved_collection,
            "contentDir": content_dir.relative_to(project).as_posix(),
            "configDetected": cfg_info["configPath"],
            "fieldMap": field_map,
            "entryCount": len(entries),
            "entries": entries,
        }

    # ------------------------------------------------------------------
    # Determine mutating mode: rename, update, or create
    # ------------------------------------------------------------------
    mode = "rename" if rename_from else ("update" if update else "create")

    input_fm: dict[str, Any] = {}
    input_raw_entries: list[tuple[str, str]] = []
    input_body: str | None = None
    input_stem: str | None = None
    search_roots: list[Path] = []
    inline_roots: list[Path] = []

    if input_path is not None:
        raw_in = Path(input_path).expanduser()
        reject_symlink_chain(raw_in)
        in_abs = raw_in.resolve()
        if not in_abs.is_file():
            raise FileNotFoundError(f"Input Markdown file not found: {in_abs}")
        raw_text = safe_read_bytes(in_abs, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8", errors="replace")
        input_fm, input_raw_entries, input_body = parse_frontmatter(raw_text)
        input_stem = in_abs.stem
        search_roots.append(in_abs.parent)
        inline_roots = [in_abs.parent]

    if body_file is not None:
        raw_bf = Path(body_file).expanduser()
        reject_symlink_chain(raw_bf)
        bf_abs = raw_bf.resolve()
        if not bf_abs.is_file():
            raise FileNotFoundError(f"Body Markdown file not found: {bf_abs}")
        bf_text = safe_read_bytes(bf_abs, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8", errors="replace")
        bf_fm, bf_raw_entries, bf_body = parse_frontmatter(bf_text)
        if not input_fm and bf_fm:
            input_fm = bf_fm
            input_raw_entries = bf_raw_entries
        input_body = bf_body
        search_roots.append(bf_abs.parent)
        inline_roots = [bf_abs.parent]

    search_roots.append(project)
    search_roots.append(Path.cwd())
    if not inline_roots:
        inline_roots = [content_dir, project]

    existing_fm: dict[str, Any] = {}
    existing_raw_entries: list[tuple[str, str]] = []
    existing_body = ""
    existing_file: Path | None = None
    old_slug_valid: str | None = None
    target_ext = ".md"

    if mode == "rename":
        if not slug:
            raise ValueError("--rename-from requires --slug <new-slug>.")
        old_slug_valid = validate_slug(str(rename_from), label="--rename-from")
        target_slug = validate_slug(slug, label="--slug")
        if old_slug_valid == target_slug:
            raise ValueError(f"Refusing rename: --rename-from and --slug are both '{target_slug}'.")
        existing_file = _find_existing_post_file(content_dir, old_slug_valid)
        if existing_file is None:
            raise FileNotFoundError(f"Source post for --rename-from '{old_slug_valid}' not found in {content_dir}.")
        assert_path_within(project, existing_file)
        target_ext = existing_file.suffix.lower()
        new_cand = content_dir / f"{target_slug}{target_ext}"
        if new_cand.exists() and not overwrite:
            raise FileExistsError(f"Target slug file already exists without --overwrite: {new_cand}")
        raw_existing = safe_read_bytes(existing_file, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode(
            "utf-8", errors="replace"
        )
        existing_fm, existing_raw_entries, existing_body = parse_frontmatter(raw_existing)
    elif mode == "update":
        raw_target_slug = slug or (str(input_fm.get("slug")).strip() if input_fm.get("slug") else None) or input_stem
        if not raw_target_slug:
            raise ValueError("--update requires --slug <slug> (or --input with matching slug/filename).")
        target_slug = validate_slug(raw_target_slug, label="--slug")
        existing_file = _find_existing_post_file(content_dir, target_slug)
        if existing_file is None:
            raise FileNotFoundError(f"Existing post '{target_slug}' not found in {content_dir}.")
        assert_path_within(project, existing_file)
        target_ext = existing_file.suffix.lower()
        raw_existing = safe_read_bytes(existing_file, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode(
            "utf-8", errors="replace"
        )
        existing_fm, existing_raw_entries, existing_body = parse_frontmatter(raw_existing)
    else:
        # create mode
        if slug is not None:
            target_slug = validate_slug(slug, label="--slug")
        elif input_fm.get("slug"):
            target_slug = validate_slug(str(input_fm["slug"]), label="frontmatter slug")
        else:
            title_seed = title or str(_fm_get(input_fm, "title", "") or "") or input_stem or ""
            if not title_seed.strip():
                raise ValueError("Create mode requires --input <file.md> or --title <str>.")
            target_slug = _slugify_title(title_seed)

    # Merge fields cleanly (CLI > input frontmatter > existing frontmatter)
    raw_cli_values: list[str] = []
    for raw_v in (title, description, author, hero_alt, pub_date, updated_date):
        if raw_v is not None:
            raw_cli_values.append(raw_v)
    for k_v in input_fm.values():
        if isinstance(k_v, str):
            raw_cli_values.append(k_v)

    final_title = (
        title
        if title is not None
        else (
            str(_fm_get(input_fm, "title"))
            if _fm_get(input_fm, "title") is not None
            else str(_fm_get(existing_fm, "title", "") or "")
        )
    )
    final_desc = (
        description
        if description is not None
        else (
            str(_fm_get(input_fm, "description"))
            if _fm_get(input_fm, "description") is not None
            else str(_fm_get(existing_fm, "description", "") or "")
        )
    )
    final_author = (
        author
        if author is not None
        else (
            str(_fm_get(input_fm, "author"))
            if _fm_get(input_fm, "author") is not None
            else (str(_fm_get(existing_fm, "author")) if _fm_get(existing_fm, "author") is not None else None)
        )
    )

    def _merge_list(cli_list: list[str] | None, role: str) -> list[str]:
        if cli_list is not None:
            return [str(x).strip() for x in cli_list if str(x).strip()]
        inp_val = _fm_get(input_fm, role)
        if inp_val is not None:
            return [str(x).strip() for x in (inp_val if isinstance(inp_val, list) else [inp_val]) if str(x).strip()]
        ex_val = _fm_get(existing_fm, role)
        if ex_val is not None:
            return [str(x).strip() for x in (ex_val if isinstance(ex_val, list) else [ex_val]) if str(x).strip()]
        return []

    final_categories = _merge_list(categories, "categories")
    final_tags = _merge_list(tags, "tags")

    # pubDate & updatedDate rules
    if mode in ("update", "rename"):
        if pub_date is not None:
            final_pub_date = _validate_date_str(pub_date, label="--pub-date")
        elif _fm_get(existing_fm, "pubDate") is not None:
            final_pub_date = str(_fm_get(existing_fm, "pubDate")).strip()
        elif _fm_get(input_fm, "pubDate") is not None:
            final_pub_date = _validate_date_str(str(_fm_get(input_fm, "pubDate")), label="pubDate")
        else:
            final_pub_date = _today_utc_date()

        if no_updated_date:
            ex_upd = _fm_get(existing_fm, "updatedDate")
            final_updated_date = str(ex_upd).strip() if ex_upd else None
        elif updated_date is not None:
            final_updated_date = _validate_date_str(updated_date, label="--updated-date")
        else:
            final_updated_date = _today_utc_date()
    else:
        if pub_date is not None:
            final_pub_date = _validate_date_str(pub_date, label="--pub-date")
        elif _fm_get(input_fm, "pubDate") is not None:
            final_pub_date = _validate_date_str(str(_fm_get(input_fm, "pubDate")), label="pubDate")
        else:
            final_pub_date = _today_utc_date()

        if updated_date is not None:
            final_updated_date = _validate_date_str(updated_date, label="--updated-date")
        elif _fm_get(input_fm, "updatedDate") is not None:
            final_updated_date = _validate_date_str(str(_fm_get(input_fm, "updatedDate")), label="updatedDate")
        else:
            final_updated_date = None

    # draft vs publish rules
    if publish_flag:
        final_draft = False
    elif draft_flag:
        final_draft = True
    elif _fm_get(input_fm, "draft") is not None:
        final_draft = bool(_fm_get(input_fm, "draft") is True)
    elif mode in ("update", "rename") and _fm_get(existing_fm, "draft") is not None:
        final_draft = bool(_fm_get(existing_fm, "draft") is True)
    else:
        final_draft = True

    final_hero_src = (
        hero
        if hero is not None
        else (
            str(_fm_get(input_fm, "heroImage"))
            if _fm_get(input_fm, "heroImage") is not None
            else (str(_fm_get(existing_fm, "heroImage")) if _fm_get(existing_fm, "heroImage") is not None else None)
        )
    )
    final_hero_alt = (
        hero_alt
        if hero_alt is not None
        else (
            str(_fm_get(input_fm, "heroAlt"))
            if _fm_get(input_fm, "heroAlt") is not None
            else (str(_fm_get(existing_fm, "heroAlt")) if _fm_get(existing_fm, "heroAlt") is not None else None)
        )
    )
    final_canonical = (
        str(_fm_get(input_fm, "canonicalUrl"))
        if _fm_get(input_fm, "canonicalUrl") is not None
        else (
            str(_fm_get(existing_fm, "canonicalUrl")) if _fm_get(existing_fm, "canonicalUrl") is not None else None
        )
    )

    body_text = input_body if input_body is not None else existing_body
    h1_stripped = False
    if strip_body_h1:
        body_text, h1_stripped = _strip_leading_body_h1(body_text)

    # Localize hero + inline images
    localized_hero, localized_body, planned_assets, image_lint_errors = _localize_images(
        project_dir=project,
        collection=resolved_collection,
        slug=target_slug,
        hero_src=final_hero_src,
        body=body_text,
        search_roots=search_roots,
        inline_search_roots=inline_roots,
        confine_hero=(hero is None and _fm_get(input_fm, "heroImage") is not None),
    )

    lint_report = lint_post(
        title=final_title,
        description=final_desc,
        body=localized_body,
        hero_image=localized_hero,
        hero_alt=final_hero_alt,
        layout_has_h1=layout_has_h1,
        raw_frontmatter_values=raw_cli_values,
        extra_errors=image_lint_errors,
        strict_typography=strict_typography,
    )

    # Build managed frontmatter entries using mapped field names
    managed_ordered: list[tuple[str, Any]] = [
        (field_map["title"], final_title),
        (field_map["description"], final_desc if final_desc else None),
        (field_map["pubDate"], final_pub_date),
        (field_map["updatedDate"], final_updated_date),
        (field_map["author"], final_author),
        (field_map["categories"], final_categories if final_categories else None),
        (field_map["tags"], final_tags if final_tags else None),
        (field_map["heroImage"], localized_hero),
        (field_map["heroAlt"], final_hero_alt if localized_hero or final_hero_alt else None),
        (field_map["canonicalUrl"], final_canonical),
        (field_map["draft"], final_draft),
    ]

    # Preserve unknown custom frontmatter keys (existing first, then input) while stripping all aliases of managed roles
    managed_role_aliases: set[str] = set()
    for role, aliases in _ROLE_ALIASES.items():
        managed_role_aliases.add(field_map[role])
        managed_role_aliases.update(aliases)
    managed_role_aliases.add("slug")

    preserved_entries: list[tuple[str, str]] = []
    seen_custom: set[str] = set()
    for src_entries in (input_raw_entries, existing_raw_entries):
        for k, raw_blk in src_entries:
            if k in managed_role_aliases or k in seen_custom:
                continue
            seen_custom.add(k)
            preserved_entries.append((k, raw_blk))

    fm_text = _serialize_frontmatter(managed_ordered, preserved_entries)
    clean_body = localized_body.lstrip("\r\n")
    if clean_body and not clean_body.endswith("\n"):
        clean_body += "\n"
    full_markdown = fm_text + ("\n" + clean_body if clean_body else "")

    target_rel_path = (content_dir / f"{target_slug}{target_ext}").relative_to(project).as_posix()
    target_abs = project / target_rel_path
    assert_path_within(project, target_abs)

    planned_text_files: list[tuple[str, str, bool]] = [
        (target_rel_path, full_markdown, mode == "update" or overwrite)
    ]
    planned_removals: list[str] = []
    redirect_summary: list[dict[str, Any]] = []

    if mode == "rename":
        assert old_slug_valid is not None and existing_file is not None
        old_rel = existing_file.relative_to(project).as_posix()
        planned_removals.append(old_rel)
        ts_mode = _inspect_astro_trailing_slash(project)
        old_route = _format_collection_route(resolved_collection, old_slug_valid, ts_mode)
        new_route = _format_collection_route(resolved_collection, target_slug, ts_mode)
        redirects_content, redirect_summary = _plan_redirects_update(
            project_dir=project,
            old_route=old_route,
            new_route=new_route,
        )
        planned_text_files.append(("public/_redirects", redirects_content, True))

    planned_files_meta = [
        {
            "path": rel_p,
            "bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        for rel_p, content, _ in planned_text_files
    ]
    planned_assets_meta = [
        {
            "path": a["path"],
            "publicUrl": a["publicUrl"],
            "bytes": a["bytes"],
            "sha256": a["sha256"],
        }
        for a in planned_assets
    ]

    plan_canonical = json.dumps(
        {
            "mode": mode,
            "projectDir": str(project),
            "collection": resolved_collection,
            "slug": target_slug,
            "renameFrom": old_slug_valid,
            "plannedFiles": planned_files_meta,
            "plannedAssets": planned_assets_meta,
            "plannedRemovals": planned_removals,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    plan_sha256 = hashlib.sha256(plan_canonical.encode("utf-8")).hexdigest()

    if apply:
        if lint_report["errors"]:
            err_codes = ", ".join(e["code"] for e in lint_report["errors"])
            raise ValueError(
                f"Refusing post --apply due to editorial lint error(s): {err_codes}. Fix errors and re-preview."
            )
        if not confirm_plan or confirm_plan.strip().lower() != plan_sha256.lower():
            raise ValueError(
                f"Refusing post --apply without matching --confirm-plan {plan_sha256} (run preview first to obtain planSha256)."
            )
        # Write localized image assets first
        for asset in planned_assets:
            dest_asset = project / asset["path"]
            assert_path_within(project, dest_asset)
            safe_write_bytes(dest_asset, asset["data"], overwrite=overwrite or mode in ("update", "rename"))
        # Write post + optional _redirects
        for rel_p, content, allow_ow in planned_text_files:
            dest_p = project / rel_p
            assert_path_within(project, dest_p)
            safe_write_text(dest_p, content, overwrite=allow_ow)
        # Remove old slug file on rename ONLY after new file + _redirects succeed
        for rem_rel in planned_removals:
            rem_abs = project / rem_rel
            reject_symlink_chain(rem_abs)
            assert_path_within(project, rem_abs)
            if rem_abs.is_file():
                os.unlink(rem_abs)

    return {
        "ok": True if (not apply or lint_report["ok"]) else False,
        "mode": mode,
        "applied": apply,
        "planSha256": plan_sha256,
        "projectDir": str(project),
        "collection": resolved_collection,
        "contentDir": content_dir.relative_to(project).as_posix(),
        "slug": target_slug,
        "renameFrom": old_slug_valid,
        "file": target_rel_path,
        "draft": final_draft,
        "pubDate": final_pub_date,
        "updatedDate": final_updated_date,
        "heroImage": localized_hero,
        "h1Stripped": h1_stripped,
        "fieldMap": field_map,
        "redirectsAdded": redirect_summary,
        "plannedFiles": planned_files_meta,
        "plannedAssets": planned_assets_meta,
        "plannedRemovals": planned_removals,
        "lint": lint_report,
        "next": (
            "Post written. Run `ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build` and `legends-astro verify-build --dist ./dist --project . --baseline site-manifest.json`."
            if apply
            else (
                f"Fix lint error(s) before applying."
                if lint_report["errors"]
                else f"Preview only ({mode} mode). Re-run with `--apply --confirm-plan {plan_sha256}` to write files."
            )
        ),
    }
