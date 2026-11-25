"""Deterministic source auditing, WordPress/HTML/Firecrawl ingestion, and Astro dist/ parity verification."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .capture import (
    _NON_SHORTCODE_NAMES,
    _SHORTCODE_RE,
    _TRACKER_PATTERNS,
    _strip_script_style_blocks,
    analyze_migration_triage,
    collect_firecrawl_sources,
    extract_root_attrs,
    extract_shortcodes,
    unwrap_firecrawl_records,
)
from .framework_audit import audit_framework_repo, detect_framework_repo
from .net_safety import (
    MAX_XML_BYTES,
    assert_path_within,
    canonicalize_route,
    format_sanitized_origin,
    is_reparse_or_symlink,
    normalize_host,
    reject_controlled_folder_access,
    reject_symlink_chain,
    replace_html_blocks,
    safe_parse_xml_bytes,
    safe_read_bytes,
    safe_write_text,
)
from .vendor import extract_css_urls, parse_srcset

MAX_WXR_XML_BYTES = MAX_XML_BYTES
MAX_SOURCE_FILE_BYTES = 10 * 1024 * 1024

# Backward-compatible aliases for callers and tests
_reject_symlink_chain = reject_symlink_chain
_reject_controlled_folder_access = reject_controlled_folder_access
_safe_read_bytes = safe_read_bytes
_safe_write_text = safe_write_text

_WP_SKIP_POST_TYPES = frozenset(
    {
        "revision",
        "nav_menu_item",
        "attachment",
        "wp_navigation",
        "wp_global_styles",
        "wp_template",
        "wp_template_part",
        "custom_css",
        "customize_changeset",
        "oembed_cache",
        "user_request",
        "acf-field",
        "acf-field-group",
    }
)

_LOCAL_ASSET_PREFIXES = ("/assets/", "/wp-content/", "/wp-includes/", "/_astro/")
_REMOTE_URL_SCAN_RE = re.compile(r"""(?:https?:)?//([a-zA-Z0-9.-]+)(?::\d+)?(?:/|["'\s)>])""")


def _safe_parse_xml(src: Path) -> ET.Element:
    """Parse XML file with byte bounds and unconditional DOCTYPE/ENTITY refusal (CWE-776)."""
    raw_bytes = safe_read_bytes(src, max_bytes=MAX_WXR_XML_BYTES)
    return safe_parse_xml_bytes(raw_bytes, source_label=str(src), max_bytes=MAX_WXR_XML_BYTES)


class PageHTMLInspector(HTMLParser):
    """Extract SEO metadata, DOM IDs, scripts, styles, headings, forms, links, assets, and SRI checks from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._in_title = False
        self._title_captured = False
        self._svg_depth = 0
        self._in_head = False
        self._in_style = False
        self._in_script = False
        self._current_script_attrs: dict[str, str | None] = {}
        self._current_script_buf: list[str] = []
        self._current_heading_tag: str | None = None
        self._current_heading_buf: list[str] = []
        self._in_body = False
        self.body_text_parts: list[str] = []

        self.html_attrs: dict[str, str] = {}
        self.body_attrs: dict[str, str] = {}
        self.meta_description: str | None = None
        self.robots: str | None = None
        self.canonical: str | None = None
        self.csp_metas: list[str] = []
        self.open_graph: dict[str, str] = {}
        self.twitter_card: dict[str, str] = {}
        self.json_ld_blocks: list[Any] = []
        self.json_ld_errors: list[str] = []
        self.dom_ids: list[str] = []
        self.resource_tag_ids: set[str] = set()
        self.data_attributes: list[str] = []
        self.headings: dict[str, list[str]] = {"h1": [], "h2": [], "h3": []}
        self.scripts: list[dict[str, Any]] = []
        self.styles: list[dict[str, Any]] = []
        self.forms: list[dict[str, str | None]] = []
        self.internal_links: list[str] = []
        self.assets: list[str] = []
        self.local_sri_tags: list[dict[str, str]] = []
        self._tag_sequence: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): v for k, v in attrs}
        self._tag_sequence.append(t)

        if t == "html" and not self.html_attrs:
            self.html_attrs = {k: (v or "") for k, v in attrs}
        elif t == "head":
            self._in_head = True
        elif t == "body":
            self._in_body = True
            if not self.body_attrs:
                self.body_attrs = {k: (v or "") for k, v in attrs}
        elif t == "svg":
            self._svg_depth += 1
        elif t == "title":
            if self._in_head and self._svg_depth == 0 and not self._title_captured:
                self._in_title = True

        el_id = attr_map.get("id")
        if el_id:
            if t in ("script", "link", "style"):
                self.resource_tag_ids.add(el_id)
            if el_id not in self.dom_ids:
                self.dom_ids.append(el_id)

        for k in attr_map:
            if k.startswith("data-") and not k.startswith("data-astro-cid"):
                if k not in self.data_attributes:
                    self.data_attributes.append(k)

        if t == "meta":
            name = (attr_map.get("name") or "").lower()
            http_equiv = (attr_map.get("http-equiv") or "").lower()
            prop = (attr_map.get("property") or "").lower()
            content = attr_map.get("content") or ""
            if name == "description" and self.meta_description is None:
                self.meta_description = content
            elif name == "robots" and self.robots is None:
                self.robots = content
            elif name.startswith("twitter:") and content:
                self.twitter_card[name] = content
            if http_equiv == "content-security-policy" and content:
                self.csp_metas.append(content)
            if prop.startswith("og:") and content:
                self.open_graph[prop] = content

        elif t == "link":
            rel = (attr_map.get("rel") or "").lower().split()
            href = attr_map.get("href") or ""
            if "canonical" in rel and href:
                self.canonical = href
            if "stylesheet" in rel and href and _is_local_ref(href):
                if href not in self.assets:
                    self.assets.append(href)
            if attr_map.get("integrity") and href.startswith(_LOCAL_ASSET_PREFIXES):
                self.local_sri_tags.append({"tag": "link", "ref": href, "integrity": str(attr_map.get("integrity"))})

        elif t in ("h1", "h2", "h3"):
            self._current_heading_tag = t
            self._current_heading_buf = []

        elif t == "script":
            self._in_script = True
            self._current_script_attrs = attr_map
            self._current_script_buf = []
            src = attr_map.get("src") or ""
            if src and _is_local_ref(src) and src not in self.assets:
                self.assets.append(src)
            if attr_map.get("integrity") and src.startswith(_LOCAL_ASSET_PREFIXES):
                self.local_sri_tags.append({"tag": "script", "ref": src, "integrity": str(attr_map.get("integrity"))})

        elif t == "style":
            self._in_style = True
            self.styles.append(
                {
                    "inHead": self._in_head,
                    "beforeFirstScript": len(self.scripts) == 0,
                    "hasFirstPaintMarker": any(k.startswith("data-first-paint") for k in attr_map),
                    "attrs": {k: v for k, v in attr_map.items()},
                }
            )

        elif t == "form":
            self.forms.append(
                {
                    "id": attr_map.get("id"),
                    "action": attr_map.get("action"),
                    "method": (attr_map.get("method") or "get").lower(),
                }
            )

        elif t == "a":
            href = attr_map.get("href") or ""
            if _is_local_ref(href):
                clean = href.split("#")[0].split("?")[0]
                if clean and clean not in self.internal_links:
                    self.internal_links.append(clean)

        elif t in ("img", "source", "video", "audio"):
            src = attr_map.get("src") or ""
            if src and _is_local_ref(src) and src not in self.assets:
                self.assets.append(src)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "head":
            self._in_head = False
        elif t == "body":
            self._in_body = False
        elif t == "svg":
            if self._svg_depth > 0:
                self._svg_depth -= 1
        elif t == "title":
            if self._in_title:
                self._in_title = False
                self._title_captured = True
        elif t in ("h1", "h2", "h3") and self._current_heading_tag == t:
            text = " ".join("".join(self._current_heading_buf).split())
            if text:
                self.headings[t].append(text)
            self._current_heading_tag = None
            self._current_heading_buf = []
        elif t == "style":
            self._in_style = False
        elif t == "script" and self._in_script:
            self._in_script = False
            raw_js = "".join(self._current_script_buf).strip()
            s_type = (self._current_script_attrs.get("type") or "").lower()
            if s_type == "application/ld+json" and raw_js:
                try:
                    self.json_ld_blocks.append(json.loads(raw_js))
                except Exception as exc:
                    self.json_ld_errors.append(str(exc))
                    self.json_ld_blocks.append({"_raw": raw_js[:200]})
            else:
                self.scripts.append(
                    {
                        "src": self._current_script_attrs.get("src"),
                        "type": self._current_script_attrs.get("type"),
                        "defer": "defer" in self._current_script_attrs,
                        "async": "async" in self._current_script_attrs,
                        "inHead": self._in_head,
                        "inline": self._current_script_attrs.get("src") is None,
                    }
                )
            self._current_script_attrs = {}
            self._current_script_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._current_heading_tag is not None:
            self._current_heading_buf.append(data)
        if self._in_script:
            self._current_script_buf.append(data)
        elif self._in_body and not self._in_style:
            stripped = data.strip()
            if stripped:
                self.body_text_parts.append(stripped)

    @property
    def title(self) -> str | None:
        val = " ".join("".join(self.title_parts).split())
        return val if val else None


def _is_local_ref(url: str) -> bool:
    u = url.strip()
    if not u or u.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return False
    if u.startswith(("http://", "https://", "//")):
        return False
    return True


def _extract_js_referenced_ids(js_text: str) -> set[str]:
    found: set[str] = set()
    for m in re.finditer(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""", js_text):
        found.add(m.group(1))
    for m in re.finditer(r"""querySelector(?:All)?\(\s*['"]#([A-Za-z0-9_-]+)['"]\s*\)""", js_text):
        found.add(m.group(1))
    return found


def _json_ld_types(blocks: list[Any]) -> list[str]:
    types: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str) and t not in types:
                types.append(t)
            elif isinstance(t, list):
                for item in t:
                    if isinstance(item, str) and item not in types:
                        types.append(item)
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(blocks)
    return types


def _route_and_shape_from_relpath(rel_posix: str) -> tuple[str, str]:
    if rel_posix == "index.html":
        return "/", "root"
    if rel_posix.endswith("/index.html"):
        prefix = "/" + rel_posix[: -len("index.html")]
        return prefix, "directory"
    if rel_posix.endswith(".html"):
        return "/" + rel_posix, "file"
    return "/" + rel_posix, "clean"


def inspect_html_text(html_text: str) -> PageHTMLInspector:
    parser = PageHTMLInspector()
    parser.feed(html_text)
    return parser


_MD_ENTITY_RE = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_WP_COMMENT_PREFIX_RE = re.compile(r"^<!--\s*/?wp:")


def _sanitize_markdown_url(raw_url: str, *, allow_mailto_tel: bool = True) -> str | None:
    """Allow only safe URL schemes for Markdown links/images and escape Markdown URL delimiters (CWE-79)."""
    u = (raw_url or "").strip()
    if not u:
        return None
    # Decode semicolon-terminated HTML character references (CommonMark §2.5) before validation;
    # requiring ';' preserves legitimate query strings such as ?a=1&copy=2.
    for _ in range(4):
        decoded = _MD_ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), u)
        if decoded == u:
            break
        u = decoded
    else:
        if _MD_ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), u) != u:
            return None
    if re.search(r"[\x00-\x1f\x7f]", u):
        return None
    u = u.strip()
    if not u:
        return None
    if u.startswith(("//", "/\\", "\\")):
        return None
    compact = re.sub(r"\s+", "", u).lower()
    allowed = False
    if compact.startswith(("https://", "http://")):
        allowed = True
    elif allow_mailto_tel and compact.startswith(("mailto:", "tel:")):
        allowed = True
    elif u.startswith(("/", "#")):
        allowed = True
    else:
        head = u.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if ":" not in head:
            allowed = True
    if not allowed:
        return None
    return (
        u.replace("\\", "%5C")
        .replace("(", "%28")
        .replace(")", "%29")
        .replace("<", "%3C")
        .replace(">", "%3E")
        .replace(" ", "%20")
    )


def _escape_md_text(data: str, *, escape_brackets: bool = False) -> str:
    out = data.replace("<", "&lt;").replace(">", "&gt;")
    if escape_brackets:
        out = out.replace("[", "\\[").replace("]", "\\]")
    return out


class _HTMLToMarkdownParser(HTMLParser):
    """Deterministic HTML-to-Markdown converter supporting headings, lists, code, links, images, tables, figures, and iframes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0
        self._in_pre = False
        self._in_code = False
        self._code_buf: list[str] = []
        self._code_spans: list[str] = []
        self._code_sentinel_prefix = f"\x00LG_CODE_{id(self)}_"
        self._list_stack: list[str] = []
        self._ol_counters: list[int] = []
        self._link_href: str | None = None
        self._link_buf: list[str] = []
        self._heading_level: int | None = None
        # Table state
        self._in_table = False
        self._table_rows: list[tuple[bool, list[str]]] = []
        self._current_row: list[str] = []
        self._current_row_is_header = False
        self._in_cell = False
        self._cell_buf: list[str] = []
        # Figure state
        self._in_figcaption = False
        self._figcaption_buf: list[str] = []

    def _append_inline(self, text: str) -> None:
        if self._in_cell:
            self._cell_buf.append(text)
        elif self._link_href is not None:
            self._link_buf.append(text)
        elif self._in_figcaption:
            self._figcaption_buf.append(text)
        else:
            self._buf.append(text)

    def _restore_code_spans(self, text: str) -> str:
        if not self._code_spans or self._code_sentinel_prefix not in text:
            return text
        for idx, span in enumerate(self._code_spans):
            token = f"{self._code_sentinel_prefix}{idx}\x00"
            if token in text:
                text = text.replace(token, span)
        return text

    def _flush_code(self) -> None:
        if not self._in_code:
            return
        self._in_code = False
        raw = re.sub(r"[\r\n]+", " ", "".join(self._code_buf))
        self._code_buf = []
        if not raw:
            return
        max_bt = max((len(m.group(0)) for m in re.finditer(r"`+", raw)), default=0)
        delim = "`" * (max_bt + 1)
        if raw.startswith("`") or raw.endswith("`") or (raw.startswith(" ") and raw.endswith(" ") and raw.strip()):
            span = f"{delim} {raw} {delim}"
        else:
            span = f"{delim}{raw}{delim}"
        idx = len(self._code_spans)
        self._code_spans.append(span)
        self._append_inline(f"{self._code_sentinel_prefix}{idx}\x00")

    def _flush_pre(self) -> None:
        raw = "".join(self._buf).strip("\r\n")
        self._buf = []
        self._in_pre = False
        if raw:
            max_bt = max((len(m.group(0)) for m in re.finditer(r"`+", raw)), default=2)
            fence = "`" * max(3, max_bt + 1)
            self.blocks.append(f"{fence}\n{raw}\n{fence}")

    def _flush_link(self) -> None:
        if self._link_href is None:
            return
        if self._in_code:
            self._flush_code()
        link_text = re.sub(r"[ \t\r\n]+", " ", "".join(self._link_buf)).strip()
        href = (self._link_href or "").strip()
        self._link_href = None
        self._link_buf = []
        safe_href = _sanitize_markdown_url(href, allow_mailto_tel=True)
        if link_text and safe_href:
            self._append_inline(f"[{link_text}]({safe_href})")
        elif link_text:
            self._append_inline(link_text)

    def _flush_figcaption(self) -> None:
        if not self._in_figcaption:
            return
        if self._link_href is not None:
            self._flush_link()
        if self._in_code:
            self._flush_code()
        self._in_figcaption = False
        cap = re.sub(r"[ \t\r\n]+", " ", "".join(self._figcaption_buf)).strip()
        cap = self._restore_code_spans(cap)
        self._figcaption_buf = []
        if cap:
            self.blocks.append(f"*{cap}*")

    def _flush_block(self, prefix: str = "", suffix: str = "") -> None:
        if self._in_pre:
            self._flush_pre()
            return
        if self._in_code:
            self._flush_code()
        if self._link_href is not None:
            self._flush_link()
        raw = "".join(self._buf)
        self._buf = []
        text = re.sub(r"[ \t\r\n]+", " ", raw).strip()
        text = re.sub(r"(?m)^(\s*)(-{3,})(\s*)$", r"\1\\\2\3", text)
        text = self._restore_code_spans(text)
        if text:
            self.blocks.append(f"{prefix}{text}{suffix}")

    def finish(self) -> None:
        if self._in_pre:
            self._flush_pre()
        if self._in_code:
            self._flush_code()
        if self._link_href is not None:
            self._flush_link()
        if self._in_figcaption:
            self._flush_figcaption()
        if self._in_table:
            if self._in_cell:
                self._in_cell = False
                cell_text = re.sub(r"[ \t\r\n]+", " ", "".join(self._cell_buf)).strip()
                cell_text = self._restore_code_spans(cell_text)
                self._current_row.append(cell_text)
                self._cell_buf = []
            if self._current_row:
                self._table_rows.append((self._current_row_is_header, self._current_row))
                self._current_row = []
            self._in_table = False
            self._flush_table()
        self._flush_block()

    def _flush_table(self) -> None:
        if not self._table_rows:
            return
        max_cols = max((len(cells) for _, cells in self._table_rows), default=0)
        if max_cols == 0:
            self._table_rows = []
            return
        norm_rows: list[list[str]] = []
        for _, cells in self._table_rows:
            padded = [c.replace("\\", "\\\\").replace("|", "\\|") for c in cells] + [""] * (max_cols - len(cells))
            norm_rows.append(padded)
        header = norm_rows[0]
        sep = ["---"] * max_cols
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for row in norm_rows[1:]:
            lines.append("| " + " | ".join(row) + " |")
        self.blocks.append("\n".join(lines))
        self._table_rows = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): v for k, v in attrs}
        if t in ("head", "script", "style", "noscript", "svg"):
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if t == "table":
            self._flush_block()
            self._in_table = True
            self._table_rows = []
            return
        if self._in_table:
            if t == "tr":
                self._current_row = []
                self._current_row_is_header = False
            elif t in ("th", "td"):
                self._in_cell = True
                self._cell_buf = []
                if t == "th":
                    self._current_row_is_header = True
            return

        if t == "figure":
            self._flush_block()
        elif t == "figcaption":
            self._flush_block()
            self._in_figcaption = True
            self._figcaption_buf = []
        elif t == "iframe":
            self._flush_block()
            src = (attr_map.get("src") or "").strip()
            title = (attr_map.get("title") or "Embedded content").strip()
            safe_src = _sanitize_markdown_url(src, allow_mailto_tel=False)
            if safe_src:
                esc_src = html.escape(safe_src, quote=True)
                esc_title = html.escape(title, quote=True)
                self.blocks.append(f'<iframe src="{esc_src}" title="{esc_title}"></iframe>')
        elif t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_block()
            self._heading_level = int(t[1])
        elif t in ("p", "div", "section", "article", "blockquote"):
            self._flush_block()
        elif t == "br":
            if self._in_pre:
                self._buf.append("\n")
            elif self._in_code:
                self._code_buf.append(" ")
            else:
                self._append_inline("\n")
        elif t == "ul":
            self._flush_block()
            self._list_stack.append("ul")
        elif t == "ol":
            self._flush_block()
            self._list_stack.append("ol")
            self._ol_counters.append(0)
        elif t == "li":
            self._flush_block()
            if self._list_stack and self._list_stack[-1] == "ol" and self._ol_counters:
                self._ol_counters[-1] += 1
        elif t == "pre":
            self._flush_block()
            self._in_pre = True
        elif t == "code":
            if not self._in_pre and not self._in_code:
                self._in_code = True
                self._code_buf = []
        elif t in ("strong", "b"):
            if not self._in_pre and not self._in_code:
                self._append_inline("**")
        elif t in ("em", "i"):
            if not self._in_pre and not self._in_code:
                self._append_inline("*")
        elif t == "a":
            if not self._in_pre:
                if self._in_code:
                    self._flush_code()
                if self._link_href is not None:
                    self._flush_link()
                self._link_href = attr_map.get("href")
                self._link_buf = []
        elif t == "img":
            if not self._in_pre:
                if self._in_code:
                    self._flush_code()
                src = (attr_map.get("src") or "").strip()
                alt = (attr_map.get("alt") or "").strip()
                safe_src = _sanitize_markdown_url(src, allow_mailto_tel=False)
                if safe_src:
                    safe_alt = _escape_md_text(alt, escape_brackets=True)
                    img_md = f"![{safe_alt}]({safe_src})"
                    self._append_inline(img_md)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("head", "script", "style", "noscript", "svg"):
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return

        if t == "table" and self._in_table:
            if self._in_code:
                self._flush_code()
            self._in_table = False
            self._flush_table()
            return
        if self._in_table:
            if t in ("th", "td") and self._in_cell:
                if self._in_code:
                    self._flush_code()
                self._in_cell = False
                cell_text = re.sub(r"[ \t\r\n]+", " ", "".join(self._cell_buf)).strip()
                cell_text = self._restore_code_spans(cell_text)
                self._current_row.append(cell_text)
                self._cell_buf = []
            elif t == "tr":
                if self._current_row:
                    self._table_rows.append((self._current_row_is_header, self._current_row))
                self._current_row = []
            return

        if t == "figcaption" and self._in_figcaption:
            self._flush_figcaption()
        elif t == "figure":
            self._flush_block()
        elif t in ("h1", "h2", "h3", "h4", "h5", "h6") and self._heading_level:
            hashes = "#" * self._heading_level
            self._heading_level = None
            self._flush_block(prefix=f"{hashes} ")
        elif t == "blockquote":
            self._flush_block(prefix="> ")
        elif t in ("p", "div", "section", "article"):
            self._flush_block()
        elif t == "li":
            kind = self._list_stack[-1] if self._list_stack else "ul"
            if kind == "ol" and self._ol_counters:
                marker = f"{self._ol_counters[-1]}. "
            else:
                marker = "- "
            self._flush_block(prefix=marker)
        elif t == "ul":
            if self._list_stack:
                self._list_stack.pop()
        elif t == "ol":
            if self._list_stack:
                self._list_stack.pop()
            if self._ol_counters:
                self._ol_counters.pop()
        elif t == "code":
            if not self._in_pre:
                self._flush_code()
        elif t == "pre":
            if self._in_pre:
                self._flush_pre()
        elif t in ("strong", "b"):
            if not self._in_pre and not self._in_code:
                self._append_inline("**")
        elif t in ("em", "i"):
            if not self._in_pre and not self._in_code:
                self._append_inline("*")
        elif t == "a":
            if self._link_href is not None:
                self._flush_link()

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_pre:
            self._buf.append(data)
            return
        if self._in_code:
            self._code_buf.append(data)
            return
        data = _escape_md_text(data, escape_brackets=self._link_href is not None)
        self._append_inline(data)


def html_to_markdown(html_text: str) -> str:
    """Convert WordPress or legacy HTML fragment/document into clean Markdown."""
    if not html_text or not html_text.strip():
        return ""
    cleaned = replace_html_blocks(
        html_text,
        strip_comments=True,
        comment_filter=lambda b: bool(_WP_COMMENT_PREFIX_RE.match(b)),
        default_replacement="",
    )
    parser = _HTMLToMarkdownParser()
    parser.feed(cleaned)
    if parser.rawdata:
        if parser.rawdata.startswith("<"):
            # Malformed/unclosed tag or comment at EOF: flush linearly as escaped text
            # to avoid CPython HTMLParser.close() O(N^2) rescanning (CWE-1333).
            tail = html.unescape(parser.rawdata) if parser.convert_charrefs else parser.rawdata
            parser.rawdata = ""
            parser.handle_data(tail)
        else:
            parser.close()
    parser.finish()
    return "\n\n".join(parser.blocks).strip()


def audit_source(source_path: str | Path, base_url: str | None = None) -> dict[str, Any]:
    """Audit a source HTML file/directory, raw-capture directory, WordPress XML/JSON export, Firecrawl JSON/directory, or framework repository."""
    raw_src = Path(source_path).expanduser()
    reject_symlink_chain(raw_src)
    src = raw_src.resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source path does not exist: {src}")

    kind = _detect_source_kind(src)
    if kind == "wordpress-xml":
        return _audit_wordpress_xml(src, base_url)
    if kind == "wordpress-json":
        return _audit_wordpress_json(src, base_url)
    if kind == "firecrawl-json":
        return _audit_firecrawl_json(src, base_url)
    if kind == "raw-capture":
        return _audit_raw_capture(src, base_url)
    if kind == "framework-repo":
        return audit_framework_repo(src, base_url)
    return _audit_html_tree(src, base_url)


_AUDIT_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        ".next",
        ".nuxt",
        "dist",
        "build",
        "out",
        ".cache",
        ".astro",
        "_site",
        ".venv",
        "__pycache__",
        "coverage",
        ".turbo",
    }
)


def _required_dom_ids(inspected: PageHTMLInspector, js_referenced_ids: set[str] | None = None) -> list[str]:
    """Compute required DOM IDs excluding <script>, <link>, and <style> resource tag IDs."""
    non_resource_ids = [i for i in inspected.dom_ids if i not in inspected.resource_tag_ids]
    if js_referenced_ids:
        matched = [i for i in non_resource_ids if i in js_referenced_ids]
        if matched:
            return matched
    return list(non_resource_ids)


def _detect_source_kind(src: Path) -> str:
    if src.is_file():
        suffix = src.suffix.lower()
        if suffix == ".xml":
            return "wordpress-xml"
        if suffix == ".json":
            raw_bytes = safe_read_bytes(src, max_bytes=MAX_SOURCE_FILE_BYTES)
            try:
                data = json.loads(raw_bytes.decode("utf-8"))
                if isinstance(data, list) and data and isinstance(data[0], dict) and "slug" in data[0] and "title" in data[0]:
                    return "wordpress-json"
                if isinstance(data, dict) and unwrap_firecrawl_records(data):
                    return "firecrawl-json"
            except Exception:
                pass
        return "html"

    # Directory detection precedence: raw-capture -> framework-repo -> firecrawl-json -> html
    manifest_p = src / "manifest.json"
    if manifest_p.is_file() and not is_reparse_or_symlink(manifest_p):
        try:
            mdata = json.loads(safe_read_bytes(manifest_p, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8"))
            if mdata.get("schema") == "legends.astro-raw-capture/v1":
                return "raw-capture"
        except Exception:
            pass

    # Check framework repository BEFORE scanning arbitrary .json files (prevents fixture JSON false positives & saves walking)
    if detect_framework_repo(src) is not None:
        return "framework-repo"

    has_html = False
    has_firecrawl_json = False
    json_probes = 0
    max_json_probes = 64
    ignored_json_names = frozenset({"manifest.json", "package.json", "package-lock.json", "tsconfig.json", "vercel.json"})
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        cur = Path(dirpath)
        kept_dirs: list[str] = []
        for d in sorted(dirnames):
            if d in _AUDIT_SKIP_DIRS:
                continue
            if is_reparse_or_symlink(cur / d):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            p = cur / fname
            if is_reparse_or_symlink(p) or not p.is_file():
                continue
            suf = p.suffix.lower()
            if suf in (".html", ".htm"):
                has_html = True
            elif (
                suf == ".json"
                and json_probes < max_json_probes
                and not p.name.endswith(".manifest.json")
                and p.name not in ignored_json_names
            ):
                json_probes += 1
                try:
                    data = json.loads(safe_read_bytes(p, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8"))
                    if isinstance(data, dict) and unwrap_firecrawl_records(data):
                        has_firecrawl_json = True
                        return "firecrawl-json"
                except Exception:
                    pass
    if has_firecrawl_json:
        return "firecrawl-json"
    if has_html:
        return "html"
    return "html"


def _audit_raw_capture(src: Path, base_url: str | None) -> dict[str, Any]:
    manifest_p = src / "manifest.json"
    mdata = json.loads(safe_read_bytes(manifest_p, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8"))
    eff_base = base_url or mdata.get("originUrl")
    origin_host = normalize_host(eff_base) if eff_base else mdata.get("originHost")

    pages: list[dict[str, Any]] = []
    triage_inputs: list[dict[str, Any]] = []
    js_referenced_ids: set[str] = set()

    for entry in mdata.get("pages", []):
        rel_file = entry.get("file", "")
        hp = src / rel_file
        assert_path_within(src, hp)
        raw_bytes = safe_read_bytes(hp, max_bytes=MAX_SOURCE_FILE_BYTES)
        text = raw_bytes.decode("utf-8", errors="replace")
        js_referenced_ids.update(_extract_js_referenced_ids(text))
        inspected = inspect_html_text(text)
        route = entry.get("route", "/")
        shape = entry.get("urlShape", "clean")
        first_paint_inline = any(s["inHead"] and s["beforeFirstScript"] for s in inspected.styles)
        req_ids = _required_dom_ids(inspected, js_referenced_ids)
        html_attrs, body_attrs = extract_root_attrs(text)

        pages.append(
            {
                "route": route,
                "alternateRoutes": [route[:-5]] if route.endswith(".html") and route != "/index.html" else [],
                "urlShape": shape,
                "sourceFile": rel_file,
                "captureSlug": entry.get("slug"),
                "finalUrl": entry.get("finalUrl"),
                "status": "publish",
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "title": inspected.title,
                "metaDescription": inspected.meta_description,
                "canonical": inspected.canonical,
                "robots": inspected.robots,
                "openGraph": inspected.open_graph,
                "twitterCard": inspected.twitter_card,
                "jsonLd": inspected.json_ld_blocks,
                "jsonLdCount": len(inspected.json_ld_blocks),
                "jsonLdTypes": _json_ld_types(inspected.json_ld_blocks),
                "headings": inspected.headings,
                "domIds": inspected.dom_ids,
                "requiredDomIds": req_ids,
                "dataAttributes": inspected.data_attributes,
                "htmlAttrs": html_attrs,
                "bodyAttrs": body_attrs,
                "dataWfPage": html_attrs.get("data-wf-page"),
                "scripts": inspected.scripts,
                "hasFirstPaintInlineStyle": first_paint_inline,
                "forms": inspected.forms,
                "internalLinks": inspected.internal_links,
                "assets": inspected.assets,
                "bodyExcerpt": " ".join(inspected.body_text_parts)[:240],
                "markdownBody": html_to_markdown(text),
            }
        )
        triage_inputs.append({"route": route, "html": text})

    shapes = sorted({p["urlShape"] for p in pages if p["urlShape"] != "root"})
    recommended_format = "file" if shapes == ["file"] else ("directory" if shapes == ["directory"] else "preserve")

    return {
        "schema": "legends.astro-migration-contract/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceKind": "raw-capture",
        "sourcePath": str(src),
        "baseUrl": eff_base,
        "recommendedAstroConfig": {
            "output": "static",
            "build.format": recommended_format,
            "trailingSlash": "never" if recommended_format == "file" else ("always" if recommended_format == "directory" else "ignore"),
        },
        "summary": {
            "pageCount": len(pages),
            "publishedCount": len(pages),
            "draftCount": 0,
            "jsReferencedIdCount": len(js_referenced_ids),
            "totalDomIds": sum(len(p["domIds"]) for p in pages),
            "totalScripts": sum(len(p["scripts"]) for p in pages),
        },
        "triage": analyze_migration_triage(triage_inputs, origin_host=origin_host),
        "jsReferencedIds": sorted(js_referenced_ids),
        "pages": pages,
    }


def _audit_html_tree(src: Path, base_url: str | None) -> dict[str, Any]:
    html_files: list[Path] = []
    js_files: list[Path] = []
    root_dir = (src if src.is_dir() else src.parent).resolve()

    if src.is_file():
        html_files = [src]
    else:
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            cur = Path(dirpath)
            kept_dirs: list[str] = []
            for d in sorted(dirnames):
                if d in _AUDIT_SKIP_DIRS:
                    continue
                cand_d = cur / d
                if is_reparse_or_symlink(cand_d):
                    raise ValueError(f"Refusing symlink or junction in source directory: {cand_d}")
                if not cand_d.resolve().is_relative_to(root_dir):
                    raise ValueError(f"Refusing path outside source root: {cand_d}")
                kept_dirs.append(d)
            dirnames[:] = kept_dirs

            for fname in sorted(filenames):
                p = cur / fname
                if is_reparse_or_symlink(p):
                    raise ValueError(f"Refusing symlink or junction in source directory: {p}")
                if not p.resolve().is_relative_to(root_dir):
                    raise ValueError(f"Refusing path outside source root: {p}")
                if p.is_file():
                    if p.suffix.lower() in (".html", ".htm"):
                        html_files.append(p)
                    elif p.suffix.lower() in (".js", ".mjs"):
                        js_files.append(p)

    js_referenced_ids: set[str] = set()
    for jf in js_files:
        raw_js = safe_read_bytes(jf, max_bytes=MAX_SOURCE_FILE_BYTES)
        try:
            js_referenced_ids.update(_extract_js_referenced_ids(raw_js.decode("utf-8", errors="ignore")))
        except Exception:
            pass

    pages: list[dict[str, Any]] = []
    triage_inputs: list[dict[str, Any]] = []
    for hf in html_files:
        raw_bytes = safe_read_bytes(hf, max_bytes=MAX_SOURCE_FILE_BYTES)
        sha = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8", errors="replace")
        js_referenced_ids.update(_extract_js_referenced_ids(text))
        rel = hf.relative_to(root_dir).as_posix()
        route, shape = _route_and_shape_from_relpath(rel)
        inspected = inspect_html_text(text)

        first_paint_inline = any(s["inHead"] and s["beforeFirstScript"] for s in inspected.styles)
        req_ids = _required_dom_ids(inspected, js_referenced_ids)
        html_attrs, body_attrs = extract_root_attrs(text)

        pages.append(
            {
                "route": route,
                "alternateRoutes": [route[:-5]] if route.endswith(".html") and route != "/index.html" else [],
                "urlShape": shape,
                "sourceFile": rel,
                "status": "publish",
                "sha256": sha,
                "title": inspected.title,
                "metaDescription": inspected.meta_description,
                "canonical": inspected.canonical,
                "robots": inspected.robots,
                "openGraph": inspected.open_graph,
                "twitterCard": inspected.twitter_card,
                "jsonLd": inspected.json_ld_blocks,
                "jsonLdCount": len(inspected.json_ld_blocks),
                "jsonLdTypes": _json_ld_types(inspected.json_ld_blocks),
                "headings": inspected.headings,
                "domIds": inspected.dom_ids,
                "requiredDomIds": req_ids,
                "dataAttributes": inspected.data_attributes,
                "htmlAttrs": html_attrs,
                "bodyAttrs": body_attrs,
                "dataWfPage": html_attrs.get("data-wf-page"),
                "scripts": inspected.scripts,
                "hasFirstPaintInlineStyle": first_paint_inline,
                "forms": inspected.forms,
                "internalLinks": inspected.internal_links,
                "assets": inspected.assets,
                "bodyExcerpt": " ".join(inspected.body_text_parts)[:240],
                "markdownBody": html_to_markdown(text),
            }
        )
        triage_inputs.append({"route": route, "html": text})

    shapes = sorted({p["urlShape"] for p in pages if p["urlShape"] != "root"})
    recommended_format = "file" if shapes == ["file"] else ("directory" if shapes == ["directory"] else "preserve")

    return {
        "schema": "legends.astro-migration-contract/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceKind": "html",
        "sourcePath": str(src),
        "baseUrl": base_url,
        "recommendedAstroConfig": {
            "output": "static",
            "build.format": recommended_format,
            "trailingSlash": "never" if recommended_format == "file" else ("always" if recommended_format == "directory" else "ignore"),
        },
        "summary": {
            "pageCount": len(pages),
            "publishedCount": len(pages),
            "draftCount": 0,
            "jsReferencedIdCount": len(js_referenced_ids),
            "totalDomIds": sum(len(p["domIds"]) for p in pages),
            "totalScripts": sum(len(p["scripts"]) for p in pages),
        },
        "triage": analyze_migration_triage(triage_inputs, origin_host=normalize_host(base_url) if base_url else None),
        "jsReferencedIds": sorted(js_referenced_ids),
        "pages": pages,
    }


def _audit_wordpress_xml(src: Path, base_url: str | None) -> dict[str, Any]:
    root = _safe_parse_xml(src)
    ns = {
        "wp": "http://wordpress.org/export/1.2/",
        "content": "http://purl.org/rss/1.0/modules/content/",
        "excerpt": "http://wordpress.org/export/1.2/excerpt/",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")

    # Pass 1: collect attachment ID -> URL for _thumbnail_id resolution
    attachments: dict[str, str] = {}
    for item in items:
        post_type = (item.findtext("wp:post_type", default="post", namespaces=ns) or "post").strip()
        if post_type == "attachment":
            att_id = (item.findtext("wp:post_id", default="", namespaces=ns) or "").strip()
            att_url = (
                (item.findtext("wp:attachment_url", default="", namespaces=ns) or "").strip()
                or (item.findtext("guid") or "").strip()
            )
            if att_id and att_url:
                attachments[att_id] = att_url

    pages: list[dict[str, Any]] = []
    triage_inputs: list[dict[str, Any]] = []
    post_types_seen: set[str] = set()
    draft_count = 0
    pub_count = 0

    for item in items:
        post_type = (item.findtext("wp:post_type", default="post", namespaces=ns) or "post").strip()
        if post_type in _WP_SKIP_POST_TYPES:
            continue
        post_types_seen.add(post_type)

        status_raw = (item.findtext("wp:status", default="publish", namespaces=ns) or "publish").strip()
        status = "publish" if status_raw == "publish" else "draft"
        if status == "draft":
            draft_count += 1
        else:
            pub_count += 1

        raw_title = (item.findtext("title") or "").strip() or None
        seo_title: str | None = None
        link = (item.findtext("link") or "").strip()
        slug = (item.findtext("wp:post_name", default="", namespaces=ns) or "").strip()
        pub_date = (item.findtext("wp:post_date", default="", namespaces=ns) or "").strip()
        author = (item.findtext("dc:creator", default="", namespaces=ns) or "").strip() or None
        body_html = item.findtext("content:encoded", default="", namespaces=ns) or ""
        excerpt = (item.findtext("excerpt:encoded", default="", namespaces=ns) or "").strip()

        categories: list[str] = []
        tags: list[str] = []
        taxonomies: dict[str, list[str]] = {}
        for cat_el in item.findall("category"):
            domain = (cat_el.attrib.get("domain") or "category").strip()
            label = (cat_el.text or "").strip()
            if not label:
                continue
            if domain == "category":
                if label not in categories:
                    categories.append(label)
            elif domain == "post_tag":
                if label not in tags:
                    tags.append(label)
            else:
                taxonomies.setdefault(domain, [])
                if label not in taxonomies[domain]:
                    taxonomies[domain].append(label)

        meta_desc = excerpt or None
        thumbnail_id: str | None = None
        for pm in item.findall("wp:postmeta", namespaces=ns):
            key = (pm.findtext("wp:meta_key", default="", namespaces=ns) or "").strip()
            val = (pm.findtext("wp:meta_value", default="", namespaces=ns) or "").strip()
            if not val:
                continue
            if key in ("_yoast_wpseo_metadesc", "rank_math_description", "_aioseo_description"):
                meta_desc = val
            elif key in ("_yoast_wpseo_title", "rank_math_title", "_aioseo_title"):
                seo_title = val
            elif key == "_thumbnail_id":
                thumbnail_id = val

        # Yoast / RankMath / AIOSEO title takes precedence over raw WP post title
        effective_title = seo_title or raw_title
        featured_image = attachments.get(thumbnail_id) if thumbnail_id else None

        parsed_link = urlparse(link) if link else None
        raw_route = parsed_link.path if (parsed_link and parsed_link.path) else (f"/{slug}/" if slug else "/")
        route = canonicalize_route(raw_route)
        shape = "directory" if route.endswith("/") and route != "/" else ("file" if route.endswith(".html") else "clean")

        inspected = inspect_html_text(body_html)
        pages.append(
            {
                "route": route,
                "alternateRoutes": [route.rstrip("/")] if route.endswith("/") and route != "/" else [],
                "urlShape": shape,
                "sourceFile": src.name,
                "postType": post_type,
                "slug": slug or route.strip("/"),
                "pubDate": pub_date,
                "author": author,
                "categories": categories,
                "tags": tags,
                "taxonomies": taxonomies,
                "featuredImage": featured_image,
                "status": status,
                "sha256": hashlib.sha256(body_html.encode("utf-8")).hexdigest(),
                "title": effective_title,
                "rawTitle": raw_title,
                "metaDescription": meta_desc,
                "canonical": link or None,
                "robots": None,
                "openGraph": {},
                "twitterCard": {},
                "jsonLd": [],
                "jsonLdCount": 0,
                "jsonLdTypes": [],
                "headings": inspected.headings,
                "domIds": inspected.dom_ids,
                "requiredDomIds": _required_dom_ids(inspected),
                "dataAttributes": inspected.data_attributes,
                "scripts": inspected.scripts,
                "hasFirstPaintInlineStyle": False,
                "forms": inspected.forms,
                "internalLinks": inspected.internal_links,
                "assets": inspected.assets,
                "bodyExcerpt": " ".join(inspected.body_text_parts)[:240],
                "markdownBody": html_to_markdown(body_html),
            }
        )
        triage_inputs.append({"route": route, "html": body_html})

    triage = analyze_migration_triage(triage_inputs, origin_host=normalize_host(base_url) if base_url else None)
    triage["detectedPlatform"] = "wordpress"

    return {
        "schema": "legends.astro-migration-contract/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceKind": "wordpress-xml",
        "sourcePath": str(src),
        "baseUrl": base_url,
        "recommendedAstroConfig": {
            "output": "static",
            "build.format": "directory",
            "trailingSlash": "always",
        },
        "summary": {
            "pageCount": len(pages),
            "publishedCount": pub_count,
            "draftCount": draft_count,
            "attachmentCount": len(attachments),
            "postTypes": sorted(post_types_seen),
            "jsReferencedIdCount": 0,
            "totalDomIds": sum(len(p["domIds"]) for p in pages),
            "totalScripts": sum(len(p["scripts"]) for p in pages),
        },
        "triage": triage,
        "jsReferencedIds": [],
        "pages": pages,
    }


def _audit_wordpress_json(src: Path, base_url: str | None) -> dict[str, Any]:
    items = json.loads(safe_read_bytes(src, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8"))
    pages: list[dict[str, Any]] = []
    triage_inputs: list[dict[str, Any]] = []
    post_types_seen: set[str] = set()
    pub_count = 0
    draft_count = 0
    for item in items:
        status_raw = str(item.get("status", "publish"))
        status = "publish" if status_raw == "publish" else "draft"
        if status == "publish":
            pub_count += 1
        else:
            draft_count += 1
        post_type = str(item.get("type", "post"))
        post_types_seen.add(post_type)
        slug = str(item.get("slug", "")).strip()
        link = str(item.get("link", "")).strip()
        title_obj = item.get("title")
        raw_title = html.unescape(title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj or "")).strip() or None
        content_obj = item.get("content")
        body_html = content_obj.get("rendered", "") if isinstance(content_obj, dict) else str(content_obj or "")
        excerpt_obj = item.get("excerpt")
        excerpt_html = excerpt_obj.get("rendered", "") if isinstance(excerpt_obj, dict) else str(excerpt_obj or "")
        yoast = item.get("yoast_head_json") if isinstance(item.get("yoast_head_json"), dict) else {}
        effective_title = str(yoast.get("title") or "").strip() or raw_title
        meta_desc = yoast.get("description") or re.sub(r"<[^<>]+>", "", excerpt_html).strip() or None
        parsed_link = urlparse(link) if link else None
        raw_route = parsed_link.path if (parsed_link and parsed_link.path) else (f"/{slug}/" if slug else "/")
        route = canonicalize_route(raw_route)
        inspected = inspect_html_text(body_html)

        featured_image = None
        embedded = item.get("_embedded") if isinstance(item.get("_embedded"), dict) else {}
        feat_list = embedded.get("wp:featuredmedia")
        if isinstance(feat_list, list) and feat_list and isinstance(feat_list[0], dict):
            featured_image = feat_list[0].get("source_url")
        elif yoast.get("og_image") and isinstance(yoast["og_image"], list) and isinstance(yoast["og_image"][0], dict):
            featured_image = yoast["og_image"][0].get("url")

        author = None
        authors = embedded.get("author")
        if isinstance(authors, list) and authors and isinstance(authors[0], dict):
            author = authors[0].get("name")

        categories: list[str] = []
        tags: list[str] = []
        terms = embedded.get("wp:term")
        if isinstance(terms, list):
            for group in terms:
                if isinstance(group, list):
                    for term in group:
                        if isinstance(term, dict) and term.get("name"):
                            if term.get("taxonomy") == "category":
                                categories.append(str(term["name"]))
                            elif term.get("taxonomy") == "post_tag":
                                tags.append(str(term["name"]))

        pages.append(
            {
                "route": route,
                "alternateRoutes": [route.rstrip("/")] if route.endswith("/") and route != "/" else [],
                "urlShape": "directory" if route.endswith("/") else "clean",
                "sourceFile": src.name,
                "postType": post_type,
                "slug": slug,
                "pubDate": str(item.get("date", "")),
                "author": author,
                "categories": categories,
                "tags": tags,
                "featuredImage": featured_image,
                "status": status,
                "sha256": hashlib.sha256(body_html.encode("utf-8")).hexdigest(),
                "title": effective_title,
                "rawTitle": raw_title,
                "metaDescription": meta_desc,
                "canonical": yoast.get("canonical") or link or None,
                "robots": None,
                "openGraph": {},
                "twitterCard": {},
                "jsonLd": [yoast["schema"]] if yoast.get("schema") else [],
                "jsonLdCount": 1 if yoast.get("schema") else 0,
                "jsonLdTypes": _json_ld_types([yoast["schema"]]) if yoast.get("schema") else [],
                "headings": inspected.headings,
                "domIds": inspected.dom_ids,
                "requiredDomIds": _required_dom_ids(inspected),
                "dataAttributes": inspected.data_attributes,
                "scripts": inspected.scripts,
                "hasFirstPaintInlineStyle": False,
                "forms": inspected.forms,
                "internalLinks": inspected.internal_links,
                "assets": inspected.assets,
                "bodyExcerpt": " ".join(inspected.body_text_parts)[:240],
                "markdownBody": html_to_markdown(body_html),
            }
        )
        triage_inputs.append({"route": route, "html": body_html})

    triage = analyze_migration_triage(triage_inputs, origin_host=normalize_host(base_url) if base_url else None)
    triage["detectedPlatform"] = "wordpress"

    return {
        "schema": "legends.astro-migration-contract/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceKind": "wordpress-json",
        "sourcePath": str(src),
        "baseUrl": base_url,
        "recommendedAstroConfig": {
            "output": "static",
            "build.format": "directory",
            "trailingSlash": "always",
        },
        "summary": {
            "pageCount": len(pages),
            "publishedCount": pub_count,
            "draftCount": draft_count,
            "postTypes": sorted(post_types_seen),
            "jsReferencedIdCount": 0,
            "totalDomIds": sum(len(p["domIds"]) for p in pages),
            "totalScripts": sum(len(p["scripts"]) for p in pages),
        },
        "triage": triage,
        "jsReferencedIds": [],
        "pages": pages,
    }


def _audit_firecrawl_json(src: Path, base_url: str | None) -> dict[str, Any]:
    records = collect_firecrawl_sources(src)
    pages: list[dict[str, Any]] = []
    triage_inputs: list[dict[str, Any]] = []
    eff_base = base_url

    for rec in records:
        meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
        source_url = str(rec.get("url") or "/")
        if not eff_base and source_url.startswith(("http://", "https://")):
            eff_base = format_sanitized_origin(source_url)
        route = canonicalize_route(str(rec.get("route") or "/"))
        html_body = str(rec.get("html") or "")
        md_body = str(rec.get("markdown") or "")
        inspected = inspect_html_text(html_body) if html_body else PageHTMLInspector()
        html_attrs, body_attrs = extract_root_attrs(html_body) if html_body else ({}, {})
        pages.append(
            {
                "route": route,
                "alternateRoutes": [],
                "urlShape": "directory" if route.endswith("/") and route != "/" else ("file" if route.endswith(".html") else "clean"),
                "sourceFile": rec.get("sourceFile") or src.name,
                "status": "publish",
                "sha256": hashlib.sha256((html_body or md_body).encode("utf-8")).hexdigest(),
                "title": meta.get("title") or inspected.title,
                "metaDescription": meta.get("description") or inspected.meta_description,
                "canonical": inspected.canonical,
                "finalUrl": source_url if source_url.startswith("http") else None,
                "robots": meta.get("robots") or inspected.robots,
                "openGraph": {k: str(v) for k, v in meta.items() if k.startswith("og:") and v} or inspected.open_graph,
                "twitterCard": inspected.twitter_card,
                "jsonLd": inspected.json_ld_blocks,
                "jsonLdCount": len(inspected.json_ld_blocks),
                "jsonLdTypes": _json_ld_types(inspected.json_ld_blocks),
                "headings": inspected.headings,
                "domIds": inspected.dom_ids,
                "requiredDomIds": _required_dom_ids(inspected),
                "dataAttributes": inspected.data_attributes,
                "htmlAttrs": html_attrs,
                "bodyAttrs": body_attrs,
                "dataWfPage": html_attrs.get("data-wf-page"),
                "scripts": inspected.scripts,
                "hasFirstPaintInlineStyle": False,
                "forms": inspected.forms,
                "internalLinks": inspected.internal_links,
                "assets": inspected.assets,
                "bodyExcerpt": (md_body or " ".join(inspected.body_text_parts))[:240],
                "markdownBody": md_body.strip() or html_to_markdown(html_body),
            }
        )
        if html_body:
            triage_inputs.append({"route": route, "html": html_body})

    return {
        "schema": "legends.astro-migration-contract/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceKind": "firecrawl-json",
        "sourcePath": str(src),
        "baseUrl": eff_base,
        "recommendedAstroConfig": {
            "output": "static",
            "build.format": "directory",
            "trailingSlash": "ignore",
        },
        "summary": {
            "pageCount": len(pages),
            "publishedCount": len(pages),
            "draftCount": 0,
            "jsReferencedIdCount": 0,
            "totalDomIds": sum(len(p["domIds"]) for p in pages),
            "totalScripts": sum(len(p["scripts"]) for p in pages),
        },
        "triage": analyze_migration_triage(triage_inputs, origin_host=normalize_host(eff_base) if eff_base else None),
        "jsReferencedIds": [],
        "pages": pages,
    }


def _normalize_draft_route_key(raw_route: str) -> str:
    """Normalize a route path to a canonical key for exact draft-leak matching (no trailing slash or .html)."""
    r = canonicalize_route(raw_route.split("?", 1)[0].split("#", 1)[0])
    if r in ("/", "/index.html", "/index.htm"):
        return "/"
    if r.lower().endswith("/index.html"):
        r = r[: -len("/index.html")] or "/"
    elif r.lower().endswith(".html"):
        r = r[:-5] or "/"
    elif r.lower().endswith(".htm"):
        r = r[:-4] or "/"
    r = r.rstrip("/")
    return r or "/"


def _extract_fm_scalar(fm_text: str, key: str) -> str | None:
    m_val = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", fm_text, re.MULTILINE)
    if not m_val:
        return None
    raw = m_val.group(1).strip()
    if not raw or raw.lower() in ("null", "~", "none"):
        return None
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        try:
            val = json.loads(raw) if raw.startswith('"') else raw[1:-1]
            return str(val).strip() or None
        except Exception:
            return raw[1:-1].strip() or None
    return raw


def _is_future_pub_date(raw_date: str | None, *, now_utc: datetime | None = None) -> bool:
    """Return True if raw_date (YYYY-MM-DD or ISO-8601) is strictly in the future relative to UTC now (matching src/lib/published.ts)."""
    if not raw_date:
        return False
    s = str(raw_date).strip()
    if len(s) < 10:
        return False
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        if len(s) == 10:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt > now
        iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) > now
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt > now
        except ValueError:
            return False


def _build_content_leak_record(
    p: Path,
    project_dir: Path,
    content_dir: Path,
    fm: str,
    body: str,
    *,
    pub_date: str | None = None,
) -> dict[str, Any]:
    rel = p.relative_to(content_dir).as_posix()
    stem = rel.rsplit(".", 1)[0]
    parts = [seg for seg in stem.split("/") if seg]
    first_line = next(
        (line.strip() for line in body.splitlines() if len(line.strip()) >= 12 and not line.strip().startswith("#")),
        "",
    )

    norm_routes: set[str] = set()
    for fm_key in ("route", "legacyRoute"):
        fm_route = _extract_fm_scalar(fm, fm_key)
        if fm_route:
            norm_routes.add(_normalize_draft_route_key(fm_route))
    slug_param = _extract_fm_scalar(fm, "slugParam")
    if slug_param:
        norm_routes.add(_normalize_draft_route_key("/" + slug_param.lstrip("/")))

    # Derive collection-relative paths (stripping trailing /index, never bare leaf filenames on nested paths)
    full_parts = parts[:-1] if (len(parts) > 1 and parts[-1].lower() == "index") else parts
    if full_parts:
        norm_routes.add(_normalize_draft_route_key("/" + "/".join(full_parts)))
    if len(full_parts) >= 2:
        # Collection-stripped path (e.g. src/content/blog/post.md -> /post; src/content/docs/v1/overview.md -> /v1/overview)
        norm_routes.add(_normalize_draft_route_key("/" + "/".join(full_parts[1:])))

    candidate_routes: list[str] = []
    for nr in sorted(norm_routes):
        if nr == "/":
            candidate_routes.append("/")
        else:
            candidate_routes.extend([nr, f"{nr}/", f"{nr}.html"])

    rec: dict[str, Any] = {
        "file": p.relative_to(project_dir).as_posix(),
        "id": stem,
        "slug": Path(stem).name,
        "normalizedRoutes": sorted(norm_routes),
        "candidateRoutes": candidate_routes,
        "sampleText": first_line[:120],
    }
    if pub_date is not None:
        rec["pubDate"] = pub_date
    return rec


def _iter_project_content_entries(project_dir: Path) -> list[tuple[Path, Path, str, str]]:
    """Safely read all Markdown/MDX files under src/content/ with frontmatter."""
    reject_symlink_chain(project_dir)
    content_dir = project_dir / "src" / "content"
    entries: list[tuple[Path, Path, str, str]] = []
    if not content_dir.is_dir():
        return entries
    if is_reparse_or_symlink(content_dir):
        raise ValueError(f"Refusing symlink or junction content directory: {content_dir}")
    content_root = content_dir.resolve()

    for p in sorted(content_dir.rglob("*")):
        if is_reparse_or_symlink(p):
            raise ValueError(f"Refusing symlink or junction in content directory: {p}")
        if not p.resolve().is_relative_to(content_root):
            raise ValueError(f"Refusing path outside content root: {p}")
        if not p.is_file() or p.suffix.lower() not in (".md", ".mdx"):
            continue
        text = safe_read_bytes(p, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8", errors="ignore")
        m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.DOTALL)
        if not m:
            continue
        entries.append((p, content_dir, m.group(1), m.group(2).strip()))
    return entries


def _scan_project_drafts(project_dir: Path) -> list[dict[str, Any]]:
    """Find Markdown/MDX content files with draft: true in frontmatter and derive full normalized candidate routes."""
    drafts: list[dict[str, Any]] = []
    for p, content_dir, fm, body in _iter_project_content_entries(project_dir):
        if re.search(r"^\s*draft\s*:\s*true\s*$", fm, re.MULTILINE):
            drafts.append(_build_content_leak_record(p, project_dir, content_dir, fm, body))
    return drafts


def _scan_project_scheduled_posts(
    project_dir: Path,
    *,
    now_utc: datetime | None = None,
) -> list[dict[str, Any]]:
    """Find non-draft Markdown/MDX content files with a future pubDate (> now_utc) and derive candidate routes."""
    scheduled: list[dict[str, Any]] = []
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    for p, content_dir, fm, body in _iter_project_content_entries(project_dir):
        if re.search(r"^\s*draft\s*:\s*true\s*$", fm, re.MULTILINE):
            continue
        raw_pub: str | None = None
        for key in ("pubDate", "publishedAt", "publishedDate", "date"):
            raw_pub = _extract_fm_scalar(fm, key)
            if raw_pub:
                break
        if raw_pub and _is_future_pub_date(raw_pub, now_utc=now):
            scheduled.append(_build_content_leak_record(p, project_dir, content_dir, fm, body, pub_date=raw_pub))
    return scheduled


class _ForbiddenHostActiveScanner(HTMLParser):
    """Extract active asset/embed/stylesheet/script/srcset/CSS url() references to forbidden hosts (ignoring passive canonical/JSON-LD/<a href>)."""

    _PASSIVE_LINK_RELS = frozenset(
        {"canonical", "alternate", "prev", "next", "shortlink", "pingback", "edituri", "wlwmanifest", "https://api.w.org/"}
    )

    def __init__(self, forbid_hosts: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.forbid_hosts = forbid_hosts
        self.hits_by_host: dict[str, list[dict[str, str]]] = {}
        self._in_style = False
        self._style_buf: list[str] = []

    def _record(self, raw_url: str | None, context: str) -> None:
        if not raw_url:
            return
        u = raw_url.strip()
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith(("http://", "https://")):
            return
        host = normalize_host(u)
        if host and host in self.forbid_hosts:
            self.hits_by_host.setdefault(host, []).append({"context": context, "url": u})

    def _scan_css(self, css_text: str, context: str) -> None:
        for ref in extract_css_urls(css_text):
            self._record(ref, context)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "style":
            self._in_style = True
            self._style_buf = []
        elif t == "script":
            s_type = attr_map.get("type", "").lower()
            if s_type != "application/ld+json":
                self._record(attr_map.get("src"), "script[src]")
        elif t == "link":
            rels = set(attr_map.get("rel", "").lower().split())
            if not rels or not (rels <= self._PASSIVE_LINK_RELS):
                self._record(attr_map.get("href"), f"link[rel={attr_map.get('rel', '')}]")
        elif t in ("img", "video", "audio", "source", "iframe", "embed", "track", "object"):
            self._record(attr_map.get("src"), f"{t}[src]")
            self._record(attr_map.get("poster"), f"{t}[poster]")
            self._record(attr_map.get("data"), f"{t}[data]")
        elif t == "meta":
            prop = attr_map.get("property", "").lower()
            name = attr_map.get("name", "").lower()
            if prop in ("og:image", "og:image:url", "og:image:secure_url"):
                self._record(attr_map.get("content"), f"meta[property={prop}]")
            elif name in ("twitter:image", "twitter:image:src"):
                self._record(attr_map.get("content"), f"meta[name={name}]")

        for srcset_attr in ("srcset", "data-srcset"):
            if attr_map.get(srcset_attr):
                for u in parse_srcset(attr_map[srcset_attr]):
                    self._record(u, f"{t}[{srcset_attr}]")
        if attr_map.get("data-src"):
            self._record(attr_map["data-src"], f"{t}[data-src]")
        if attr_map.get("data-poster-url"):
            self._record(attr_map["data-poster-url"], f"{t}[data-poster-url]")
        if attr_map.get("data-video-urls"):
            for u in attr_map["data-video-urls"].split(","):
                self._record(u, f"{t}[data-video-urls]")
        if attr_map.get("style"):
            self._scan_css(attr_map["style"], f"{t}[style]")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style" and self._in_style:
            self._in_style = False
            self._scan_css("".join(self._style_buf), "style")
            self._style_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._style_buf.append(data)


def _scan_forbidden_hosts_html(html_text: str, forbid_hosts: set[str], *, strict: bool = False) -> dict[str, list[dict[str, str]]]:
    if not forbid_hosts:
        return {}
    if strict:
        hits: dict[str, list[dict[str, str]]] = {}
        for m in _REMOTE_URL_SCAN_RE.finditer(html_text):
            h = normalize_host(m.group(1))
            if h in forbid_hosts:
                snippet = html_text[max(0, m.start() - 16) : min(len(html_text), m.end() + 48)].strip()
                hits.setdefault(h, []).append({"context": "raw", "url": snippet})
        return hits
    scanner = _ForbiddenHostActiveScanner(forbid_hosts)
    scanner.feed(html_text)
    return scanner.hits_by_host


def _verify_dist_css_assets(dist: Path, forbid_hosts: set[str], failures: list[dict[str, Any]]) -> None:
    """Verify local CSS url()/@import references resolve inside dist/ and contain no forbidden hosts."""
    for css_file in sorted(dist.rglob("*.css")):
        rel = css_file.relative_to(dist).as_posix()
        text = safe_read_bytes(css_file, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8", errors="replace")

        if forbid_hosts:
            css_hits: dict[str, list[dict[str, str]]] = {}
            for ref in extract_css_urls(text):
                u = ("https:" + ref) if ref.startswith("//") else ref
                if u.startswith(("http://", "https://")):
                    host = normalize_host(u)
                    if host in forbid_hosts:
                        css_hits.setdefault(host, []).append({"context": "css-url", "url": u})
            for m in _REMOTE_URL_SCAN_RE.finditer(text):
                host = normalize_host(m.group(1))
                if host in forbid_hosts and host not in css_hits:
                    snippet = text[m.start() : min(len(text), m.end() + 48)].strip()
                    css_hits.setdefault(host, []).append({"context": "css-raw", "url": snippet})
            for host, hits in sorted(css_hits.items()):
                failures.append(
                    {
                        "code": "forbidden_host_referenced",
                        "file": rel,
                        "host": host,
                        "count": len(hits),
                        "samples": hits[:5],
                        "message": f"Built CSS {rel} still references forbidden host '{host}' ({len(hits)} reference(s)).",
                    }
                )

        for ref in extract_css_urls(text):
            if ref.startswith(("http://", "https://", "//", "data:", "#")):
                continue
            clean = ref.split("?", 1)[0].split("#", 1)[0]
            if not clean:
                continue
            if clean.startswith("/"):
                cand = dist / clean.lstrip("/")
            else:
                cand = css_file.parent / clean
            if not _is_safe_dist_file(dist, cand):
                failures.append(
                    {
                        "code": "missing_css_asset",
                        "file": rel,
                        "asset": ref,
                        "message": f"Built CSS {rel} references missing local asset '{ref}'.",
                    }
                )


def _verify_dist_sitemaps(
    dist: Path,
    draft_routes: set[str],
    failures: list[dict[str, Any]],
    *,
    scheduled_routes: set[str] | frozenset[str] = frozenset(),
    sitemaps_out: list[str] | None = None,
) -> int:
    """Verify dist/ sitemap-index.xml, sitemap-*.xml, sitemap.xml (XXE-safe) for route existence, draft, and scheduled-post exclusion."""
    checked = 0
    sitemap_queue: list[Path] = []
    for name in ("sitemap-index.xml", "sitemap.xml", "sitemap-0.xml"):
        cand = dist / name
        if cand.is_file() and cand not in sitemap_queue:
            sitemap_queue.append(cand)
    for extra in sorted(dist.glob("sitemap-*.xml")):
        if extra.is_file() and extra not in sitemap_queue:
            sitemap_queue.append(extra)
    seen: set[Path] = set()

    while sitemap_queue:
        sm_path = sitemap_queue.pop(0)
        if sm_path in seen or not _is_safe_dist_file(dist, sm_path):
            continue
        seen.add(sm_path)
        rel = sm_path.relative_to(dist).as_posix()
        try:
            raw = safe_read_bytes(sm_path, max_bytes=MAX_SOURCE_FILE_BYTES)
            root = safe_parse_xml_bytes(raw, source_label=rel)
        except Exception as exc:
            failures.append(
                {
                    "code": "sitemap_invalid_xml",
                    "file": rel,
                    "message": f"Sitemap {rel} failed XXE-safe XML parsing: {exc}",
                }
            )
            continue

        checked += 1
        if sitemaps_out is not None and rel not in sitemaps_out:
            sitemaps_out.append(rel)
        tag_local = root.tag.rsplit("}", 1)[-1].lower()
        if tag_local == "sitemapindex":
            for child in root:
                if child.tag.rsplit("}", 1)[-1].lower() == "sitemap":
                    for sub in child:
                        if sub.tag.rsplit("}", 1)[-1].lower() == "loc" and sub.text:
                            loc_path = urlparse(sub.text.strip()).path.lstrip("/")
                            if loc_path:
                                child_file = dist / loc_path
                                if _is_safe_dist_file(dist, child_file):
                                    sitemap_queue.append(child_file)
                                else:
                                    failures.append(
                                        {
                                            "code": "sitemap_missing_child",
                                            "file": rel,
                                            "target": "/" + loc_path,
                                            "message": f"Sitemap index {rel} references missing child sitemap /{loc_path}.",
                                        }
                                    )
        elif tag_local == "urlset":
            for child in root:
                if child.tag.rsplit("}", 1)[-1].lower() == "url":
                    for sub in child:
                        if sub.tag.rsplit("}", 1)[-1].lower() == "loc" and sub.text:
                            loc = sub.text.strip()
                            loc_path = urlparse(loc).path or "/"
                            if not _resolve_dist_target(dist, loc_path):
                                failures.append(
                                    {
                                        "code": "sitemap_broken_route",
                                        "file": rel,
                                        "route": loc_path,
                                        "message": f"Sitemap {rel} lists {loc_path}, which does not exist in dist/.",
                                    }
                                )
                            norm_loc = _normalize_draft_route_key(loc_path)
                            if norm_loc in draft_routes:
                                failures.append(
                                    {
                                        "code": "sitemap_draft_leaked",
                                        "file": rel,
                                        "route": loc_path,
                                        "message": f"Sitemap {rel} includes draft route {loc_path}.",
                                    }
                                )
                            if norm_loc in scheduled_routes:
                                failures.append(
                                    {
                                        "code": "scheduled_post_in_sitemap",
                                        "file": rel,
                                        "route": loc_path,
                                        "message": f"Sitemap {rel} includes future-dated scheduled route {loc_path}.",
                                    }
                                )
    return checked


def _verify_dist_rss_leaks(
    dist: Path,
    drafts_checked: list[dict[str, Any]],
    scheduled_checked: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> None:
    """Verify dist/rss.xml does not leak draft or future-dated scheduled post routes or body snippets."""
    if not drafts_checked and not scheduled_checked:
        return
    rss_path = dist / "rss.xml"
    if not _is_safe_dist_file(dist, rss_path):
        return
    try:
        raw = safe_read_bytes(rss_path, max_bytes=MAX_SOURCE_FILE_BYTES)
        rss_text = raw.decode("utf-8", errors="replace")
    except Exception:
        return

    rss_links: dict[str, str] = {}
    try:
        root = safe_parse_xml_bytes(raw, source_label="rss.xml")
        for elem in root.iter():
            local = elem.tag.rsplit("}", 1)[-1].lower()
            if local in ("link", "guid") and elem.text:
                raw_url = elem.text.strip()
                if raw_url:
                    loc_path = urlparse(raw_url).path or "/"
                    rss_links.setdefault(_normalize_draft_route_key(loc_path), loc_path)
    except Exception:
        pass

    for d in drafts_checked:
        for nr in d.get("normalizedRoutes", []):
            if nr in rss_links:
                failures.append(
                    {
                        "code": "draft_route_leaked",
                        "file": "rss.xml",
                        "draftFile": d["file"],
                        "leakedRoute": rss_links[nr],
                        "message": f"RSS feed rss.xml leaks draft entry {d['file']} ({rss_links[nr]}).",
                    }
                )
                break
        if d.get("sampleText") and d["sampleText"] in rss_text:
            failures.append(
                {
                    "code": "draft_body_leaked",
                    "file": "rss.xml",
                    "draftFile": d["file"],
                    "message": f"Draft body text from {d['file']} appears in built dist/rss.xml.",
                }
            )

    for s in scheduled_checked:
        for nr in s.get("normalizedRoutes", []):
            if nr in rss_links:
                failures.append(
                    {
                        "code": "scheduled_post_route_leaked",
                        "file": "rss.xml",
                        "scheduledFile": s["file"],
                        "pubDate": s.get("pubDate"),
                        "leakedRoute": rss_links[nr],
                        "message": f"RSS feed rss.xml leaks future-dated scheduled entry {s['file']} (pubDate={s.get('pubDate')}, route={rss_links[nr]}).",
                    }
                )
                break
        if s.get("sampleText") and s["sampleText"] in rss_text:
            failures.append(
                {
                    "code": "scheduled_post_body_leaked",
                    "file": "rss.xml",
                    "scheduledFile": s["file"],
                    "pubDate": s.get("pubDate"),
                    "message": f"Scheduled post body text from {s['file']} appears in built dist/rss.xml.",
                }
            )


def _verify_redirects_file(
    dist: Path,
    project_dir: Path | None,
    failures: list[dict[str, Any]],
    *,
    rules_out: list[dict[str, Any]] | None = None,
) -> int:
    """Validate _redirects destinations exist in dist/ and detect redirect loops."""
    redirects_path: Path | None = None
    for cand in (
        dist / "_redirects",
        (project_dir / "public" / "_redirects") if project_dir else None,
    ):
        if cand is not None and cand.is_file() and not is_reparse_or_symlink(cand):
            redirects_path = cand
            break
    if redirects_path is None:
        return 0

    text = safe_read_bytes(redirects_path, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8", errors="replace")
    rules: dict[str, str] = {}
    rule_count = 0

    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        src_r, dst_r = parts[0], parts[1]
        status_code = 301
        if len(parts) >= 3:
            raw_code = parts[2].rstrip("!")
            if raw_code.isdigit():
                status_code = int(raw_code)
        rule_count += 1
        if rules_out is not None:
            rules_out.append({"source": src_r, "destination": dst_r, "status": status_code})
        if src_r == dst_r:
            failures.append(
                {
                    "code": "redirect_loop",
                    "line": line_no,
                    "source": src_r,
                    "destination": dst_r,
                    "message": f"_redirects line {line_no} creates a self-redirect loop: {src_r} -> {dst_r}.",
                }
            )
            continue
        rules[src_r] = dst_r
        if "*" not in dst_r and not dst_r.startswith(("http://", "https://")):
            if not _resolve_dist_target(dist, dst_r):
                failures.append(
                    {
                        "code": "redirect_broken_destination",
                        "line": line_no,
                        "source": src_r,
                        "destination": dst_r,
                        "message": f"_redirects line {line_no} points to missing dist/ target: {dst_r}.",
                    }
                )

    # Multi-hop cycle detection
    for start in rules:
        visited = {start}
        cur = rules.get(start)
        while cur and cur in rules:
            if cur in visited:
                failures.append(
                    {
                        "code": "redirect_loop",
                        "source": start,
                        "destination": cur,
                        "message": f"_redirects contains a redirect cycle involving {start} -> ... -> {cur}.",
                    }
                )
                break
            visited.add(cur)
            cur = rules.get(cur)

    return rule_count


def _verify_env_rules(
    *,
    env: str,
    dist: Path,
    project_dir: Path | None,
    route_records: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> None:
    """Enforce staging Triple-Lock or production indexability rules."""
    robots_path = dist / "robots.txt"
    robots_text = (
        safe_read_bytes(robots_path, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8", errors="replace")
        if _is_safe_dist_file(dist, robots_path)
        else ""
    )

    headers_path: Path | None = None
    for cand in (
        dist / "_headers",
        (project_dir / "public" / "_headers") if project_dir else None,
    ):
        if cand is not None and cand.is_file() and not is_reparse_or_symlink(cand):
            headers_path = cand
            break
    headers_text = (
        safe_read_bytes(headers_path, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8", errors="replace")
        if headers_path
        else ""
    )

    if env == "staging":
        if not re.search(r"(?im)^\s*Disallow\s*:\s*/\s*$", robots_text):
            failures.append(
                {
                    "code": "staging_robots_lock_missing",
                    "message": "Staging Triple-Lock requires dist/robots.txt with 'Disallow: /'.",
                }
            )
        if not re.search(r"(?i)X-Robots-Tag\s*:[^\r\n]*noindex", headers_text):
            failures.append(
                {
                    "code": "staging_headers_lock_missing",
                    "message": "Staging Triple-Lock requires _headers with 'X-Robots-Tag: ... noindex'.",
                }
            )
        for rec in route_records:
            robots_meta = (rec.get("robots") or "").lower()
            if "noindex" not in robots_meta:
                failures.append(
                    {
                        "code": "staging_noindex_missing",
                        "route": rec["route"],
                        "file": rec["file"],
                        "message": f"Staging page {rec['file']} is missing <meta name=\"robots\" content=\"...noindex...\">.",
                    }
                )
            for tracker_name in rec.get("trackers", []):
                failures.append(
                    {
                        "code": "staging_live_tracker_leaked",
                        "route": rec["route"],
                        "file": rec["file"],
                        "tracker": tracker_name,
                        "message": f"Staging page {rec['file']} still contains live tracker '{tracker_name}'.",
                    }
                )
    elif env == "production":
        if re.search(r"(?im)^\s*Disallow\s*:\s*/\s*$", robots_text):
            failures.append(
                {
                    "code": "production_robots_disallow_all",
                    "message": "Production dist/robots.txt contains 'Disallow: /' (staging lock leaked to production).",
                }
            )
        if re.search(r"(?i)X-Robots-Tag\s*:[^\r\n]*noindex", headers_text):
            failures.append(
                {
                    "code": "production_headers_noindex_leaked",
                    "message": "Production _headers still contains 'X-Robots-Tag: ... noindex'.",
                }
            )
        for rec in route_records:
            if rec["route"] in ("/404", "/404/", "/404.html") or rec["file"] in ("404.html", "404/index.html"):
                continue
            robots_meta = (rec.get("robots") or "").lower()
            if "noindex" in robots_meta:
                failures.append(
                    {
                        "code": "production_noindex_leaked",
                        "route": rec["route"],
                        "file": rec["file"],
                        "message": f"Production page {rec['file']} still carries noindex in <meta name=\"robots\">.",
                    }
                )


def _is_404_route(route: str, rel_file: str = "") -> bool:
    return route in ("/404", "/404/", "/404.html") or rel_file in ("404.html", "404/index.html")


def _norm_manifest_route_key(route: str) -> str:
    r = canonicalize_route(route)
    if r == "/":
        return "/"
    return r.rstrip("/")


def _collect_dist_asset_inventory(dist: Path) -> tuple[int, dict[str, int]]:
    asset_count = 0
    by_type: dict[str, int] = {}
    for entry in sorted(dist.rglob("*")):
        if not entry.is_file():
            continue
        suffix = entry.suffix.lower()
        if suffix == ".html":
            continue
        asset_count += 1
        key = suffix if suffix else entry.name
        by_type[key] = by_type.get(key, 0) + 1
    return asset_count, dict(sorted(by_type.items()))


def _build_site_manifest(
    *,
    dist: Path,
    project_dir: Path | None,
    env: str | None,
    route_records: list[dict[str, Any]],
    sitemaps: list[str],
    redirect_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    asset_count, assets_by_type = _collect_dist_asset_inventory(dist)
    return {
        "schema": "legends.astro-site-manifest/v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dist": str(dist),
        "project": str(project_dir) if project_dir else None,
        "env": env,
        "summary": {
            "routeCount": len(route_records),
            "assetCount": asset_count,
            "assetsByType": assets_by_type,
        },
        "sitemaps": sorted(sitemaps),
        "redirects": redirect_rules,
        "routes": [
            {
                "route": info["route"],
                "file": info["file"],
                "urlShape": info["urlShape"],
                "title": info["title"],
                "metaDescription": info["metaDescription"],
                "canonical": info["canonical"],
                "jsonLdTypes": info["jsonLdTypes"],
                "h1Count": len(info["headings"].get("h1", [])),
            }
            for info in route_records
        ],
    }


def _compare_against_baseline(
    *,
    baseline_path: Path,
    dist: Path,
    route_records: list[dict[str, Any]],
    route_map: dict[str, dict[str, Any]],
    redirect_rules: list[dict[str, Any]],
    current_manifest: dict[str, Any],
    allow_canonical_change: bool,
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_bpath = Path(baseline_path).expanduser()
    reject_symlink_chain(raw_bpath)
    bpath = raw_bpath.resolve()
    baseline = json.loads(safe_read_bytes(bpath, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8"))
    if not isinstance(baseline, dict) or baseline.get("schema") != "legends.astro-site-manifest/v1":
        raise ValueError(
            f"Unsupported --baseline schema in {bpath}; expected 'legends.astro-site-manifest/v1'."
        )

    valid_redirects: dict[str, dict[str, Any]] = {}
    for rule in redirect_rules:
        status = int(rule.get("status", 301))
        if status not in (301, 302, 308):
            continue
        src = str(rule.get("source") or "")
        dst = str(rule.get("destination") or "")
        if not src or not dst:
            continue
        dst_ok = dst.startswith("https://") or (dst in route_map) or _resolve_dist_target(dist, dst)
        if dst_ok:
            valid_redirects[_norm_manifest_route_key(src)] = {"source": src, "destination": dst, "status": status}

    removed_routes: list[str] = []
    redirected_routes: list[dict[str, Any]] = []
    baseline_keys: set[str] = set()

    for br in baseline.get("routes", []):
        if not isinstance(br, dict):
            continue
        b_route = str(br.get("route") or "/")
        b_file = str(br.get("file") or "")
        if _is_404_route(b_route, b_file):
            continue
        b_key = _norm_manifest_route_key(b_route)
        baseline_keys.add(b_key)

        cur = route_map.get(b_route)
        if cur is None:
            alt = b_route.rstrip("/") if b_route != "/" and b_route.endswith("/") else (b_route + "/" if b_route != "/" else "/")
            cur = route_map.get(alt)

        if cur is None:
            redir = valid_redirects.get(b_key)
            if redir is None and b_key != "/" and b_route != "/404.html":
                if b_key.endswith(".html"):
                    redir = valid_redirects.get(b_key[:-5])
                else:
                    redir = valid_redirects.get(b_key + ".html")
            if redir is not None:
                redirected_routes.append(
                    {"from": b_route, "to": redir["destination"], "status": redir["status"]}
                )
            else:
                removed_routes.append(b_route)
                failures.append(
                    {
                        "code": "baseline_route_missing",
                        "route": b_route,
                        "message": f"Baseline route {b_route} disappeared from dist/ without a valid 301/302/308 redirect in _redirects.",
                    }
                )
            continue

        # Surviving route metadata & structured-data regression checks
        if br.get("canonical"):
            if not cur.get("canonical"):
                failures.append(
                    {
                        "code": "baseline_canonical_lost",
                        "route": b_route,
                        "expected": br["canonical"],
                        "message": f"Surviving route {b_route} lost its <link rel=\"canonical\"> tag.",
                    }
                )
            elif cur["canonical"] != br["canonical"] and not allow_canonical_change:
                failures.append(
                    {
                        "code": "baseline_canonical_changed",
                        "route": b_route,
                        "expected": br["canonical"],
                        "actual": cur["canonical"],
                        "message": f"Surviving route {b_route} canonical changed from {br['canonical']} to {cur['canonical']} (pass --allow-canonical-change to authorize).",
                    }
                )

        if br.get("title") and not cur.get("title"):
            failures.append(
                {
                    "code": "baseline_title_lost",
                    "route": b_route,
                    "message": f"Surviving route {b_route} lost its <title> tag.",
                }
            )

        if br.get("metaDescription") and not cur.get("metaDescription"):
            failures.append(
                {
                    "code": "baseline_description_lost",
                    "route": b_route,
                    "message": f"Surviving route {b_route} lost its <meta name=\"description\"> tag.",
                }
            )

        if int(br.get("h1Count") or 0) > 0 and len(cur.get("headings", {}).get("h1", [])) == 0:
            failures.append(
                {
                    "code": "baseline_h1_lost",
                    "route": b_route,
                    "message": f"Surviving route {b_route} lost its <h1> heading.",
                }
            )

        cur_types = cur.get("jsonLdTypes", [])
        for req_type in br.get("jsonLdTypes") or []:
            if req_type not in cur_types:
                failures.append(
                    {
                        "code": "baseline_jsonld_type_lost",
                        "route": b_route,
                        "schemaType": req_type,
                        "message": f"Surviving route {b_route} lost JSON-LD @type '{req_type}'.",
                    }
                )

    added_routes: list[str] = []
    for rec in route_records:
        if _is_404_route(rec["route"], rec["file"]):
            continue
        if _norm_manifest_route_key(rec["route"]) not in baseline_keys:
            added_routes.append(rec["route"])

    baseline_assets = int((baseline.get("summary") or {}).get("assetCount", 0))
    current_assets = int((current_manifest.get("summary") or {}).get("assetCount", 0))

    return {
        "baselinePath": str(bpath),
        "addedRoutes": added_routes,
        "removedRoutes": removed_routes,
        "redirectedRoutes": redirected_routes,
        "assetCountDelta": current_assets - baseline_assets,
    }


def verify_build(
    dist_dir: str | Path,
    project_dir: str | Path | None = None,
    contract_path: str | Path | None = None,
    *,
    forbid_hosts: Iterable[str] = (),
    forbid_host_strict: bool = False,
    env: str | None = None,
    baseline_path: str | Path | None = None,
    write_manifest_path: str | Path | None = None,
    overwrite: bool = False,
    allow_canonical_change: bool = False,
) -> dict[str, Any]:
    """Verify an Astro dist/ directory for route parity, SEO/schema preservation, DOM ID ABI, draft exclusion, CSS/SRI/sitemap/redirects, env locks, and baseline manifest regressions."""
    if env is not None and env not in ("staging", "production"):
        raise ValueError(f"Unsupported --env '{env}'; expected 'staging' or 'production'.")

    if write_manifest_path is not None:
        raw_wm = Path(write_manifest_path).expanduser()
        reject_symlink_chain(raw_wm)
        reject_controlled_folder_access(raw_wm.resolve())

    raw_dist = Path(dist_dir).expanduser()
    reject_symlink_chain(raw_dist)
    dist = raw_dist.resolve()
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not dist.is_dir():
        return {
            "ok": False,
            "dist": str(dist),
            "failures": [{"code": "dist_missing", "message": f"dist directory does not exist: {dist}"}],
            "warnings": [],
        }

    for entry in sorted(dist.rglob("*")):
        if is_reparse_or_symlink(entry):
            raise ValueError(f"Refusing symlink or junction in dist directory: {entry}")
        if not entry.resolve().is_relative_to(dist):
            raise ValueError(f"Refusing path outside dist directory: {entry}")

    html_files = sorted(dist.rglob("*.html"))
    if not html_files:
        return {
            "ok": False,
            "dist": str(dist),
            "failures": [{"code": "dist_empty", "message": f"No .html files found in {dist}"}],
            "warnings": [],
        }

    forbid_set = {normalize_host(h) for h in forbid_hosts if h and normalize_host(h)}
    route_map: dict[str, dict[str, Any]] = {}
    route_records: list[dict[str, Any]] = []
    all_dist_text: list[str] = []

    for hf in html_files:
        rel = hf.relative_to(dist).as_posix()
        text = safe_read_bytes(hf, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8", errors="replace")
        all_dist_text.append(text)
        inspected = inspect_html_text(text)
        route, shape = _route_and_shape_from_relpath(rel)

        first_paint_inline = any(s["inHead"] and s["beforeFirstScript"] for s in inspected.styles)
        has_first_paint_attr = any(s["hasFirstPaintMarker"] for s in inspected.styles)

        if has_first_paint_attr and not first_paint_inline:
            failures.append(
                {
                    "code": "first_paint_style_order_broken",
                    "route": route,
                    "file": rel,
                    "message": f"First-paint <style> in {rel} is either outside <head> or placed after a <script> tag.",
                }
            )

        if not inspected.title:
            warnings.append(
                {
                    "code": "missing_title",
                    "route": route,
                    "file": rel,
                    "message": f"Built page {rel} has no <title>.",
                }
            )

        for err in inspected.json_ld_errors:
            failures.append(
                {
                    "code": "invalid_jsonld",
                    "route": route,
                    "file": rel,
                    "message": f"Invalid JSON inside <script type=\"application/ld+json\"> in {rel}: {err}",
                }
            )

        for sri in inspected.local_sri_tags:
            failures.append(
                {
                    "code": "local_asset_sri_integrity",
                    "route": route,
                    "file": rel,
                    "asset": sri["ref"],
                    "message": f"Local {sri['tag']} asset {sri['ref']} in {rel} still carries integrity='{sri['integrity']}' (strip SRI on vendored assets).",
                }
            )

        if "/cdn-cgi/" in text.lower():
            failures.append(
                {
                    "code": "cdn_cgi_leak",
                    "route": route,
                    "file": rel,
                    "message": f"Built page {rel} contains a /cdn-cgi/ reference (decode Cloudflare email-protection or strip CF runtime scripts).",
                }
            )

        visible_text = re.sub(r"<[^<>]+>", " ", _strip_script_style_blocks(text))
        is_wp_page = bool(re.search(r"/wp-content/|/wp-includes/|wp-block-", text, re.IGNORECASE))
        leaked_shortcodes = extract_shortcodes(visible_text, is_wordpress=is_wp_page)
        for sc in leaked_shortcodes:
            failures.append(
                {
                    "code": "raw_shortcode_leak",
                    "route": route,
                    "file": rel,
                    "shortcode": sc,
                    "message": f"Built page {rel} leaks raw WordPress shortcode {sc}.",
                }
            )

        if forbid_set:
            html_hits = _scan_forbidden_hosts_html(text, forbid_set, strict=forbid_host_strict)
            for h, hits in sorted(html_hits.items()):
                failures.append(
                    {
                        "code": "forbidden_host_referenced",
                        "route": route,
                        "file": rel,
                        "host": h,
                        "count": len(hits),
                        "samples": hits[:5],
                        "message": f"Built page {rel} still references forbidden host '{h}' ({len(hits)} reference(s)).",
                    }
                )

        for link in inspected.internal_links:
            if not _resolve_dist_target(dist, link):
                failures.append(
                    {
                        "code": "broken_internal_link",
                        "route": route,
                        "file": rel,
                        "target": link,
                        "message": f"Page {rel} links to {link}, which does not resolve in dist/.",
                    }
                )

        for asset in inspected.assets:
            if not _resolve_dist_asset(dist, asset):
                failures.append(
                    {
                        "code": "missing_asset",
                        "route": route,
                        "file": rel,
                        "asset": asset,
                        "message": f"Page {rel} references local asset {asset}, which does not exist in dist/.",
                    }
                )

        detected_trackers = [name for name, pat in _TRACKER_PATTERNS if pat.search(text)]
        info = {
            "file": rel,
            "route": route,
            "urlShape": shape,
            "title": inspected.title,
            "metaDescription": inspected.meta_description,
            "canonical": inspected.canonical,
            "robots": inspected.robots,
            "openGraph": inspected.open_graph,
            "jsonLdCount": len(inspected.json_ld_blocks),
            "jsonLdTypes": _json_ld_types(inspected.json_ld_blocks),
            "domIds": inspected.dom_ids,
            "dataWfPage": inspected.html_attrs.get("data-wf-page"),
            "scripts": inspected.scripts,
            "hasFirstPaintInlineStyle": first_paint_inline,
            "headings": inspected.headings,
            "trackers": detected_trackers,
        }
        route_records.append(info)
        route_map[route] = info
        if route.endswith(".html") and route != "/index.html":
            route_map.setdefault(route[:-5], info)
        elif route.endswith("/") and route != "/":
            route_map.setdefault(route.rstrip("/"), info)
        elif route != "/":
            route_map.setdefault(route + "/", info)

    # CSS url() / @import resolution + forbidden hosts in CSS
    _verify_dist_css_assets(dist, forbid_set, failures)

    # Check project drafts & future-dated scheduled posts if project_dir provided
    drafts_checked: list[dict[str, Any]] = []
    draft_routes: set[str] = set()
    scheduled_checked: list[dict[str, Any]] = []
    scheduled_routes: set[str] = set()
    proj_resolved = Path(project_dir).resolve() if project_dir else None
    if proj_resolved:
        drafts_checked = _scan_project_drafts(proj_resolved)
        scheduled_checked = _scan_project_scheduled_posts(proj_resolved)
        combined_text = "\n".join(all_dist_text)
        for d in drafts_checked:
            for nr in d.get("normalizedRoutes", []):
                draft_routes.add(nr)
            for candidate in d.get("candidateRoutes", []):
                if candidate in route_map:
                    failures.append(
                        {
                            "code": "draft_route_leaked",
                            "draftFile": d["file"],
                            "leakedRoute": candidate,
                            "message": f"Draft entry {d['file']} emitted public route {candidate} in dist/ (filter getStaticPaths as well as index lists).",
                        }
                    )
                    break
            if d["sampleText"] and d["sampleText"] in combined_text:
                failures.append(
                    {
                        "code": "draft_body_leaked",
                        "draftFile": d["file"],
                        "message": f"Draft body text from {d['file']} appears in built dist/ HTML.",
                    }
                )

        for s in scheduled_checked:
            for nr in s.get("normalizedRoutes", []):
                scheduled_routes.add(nr)
            for candidate in s.get("candidateRoutes", []):
                if candidate in route_map:
                    failures.append(
                        {
                            "code": "scheduled_post_route_leaked",
                            "scheduledFile": s["file"],
                            "pubDate": s.get("pubDate"),
                            "leakedRoute": candidate,
                            "message": f"Future-dated scheduled entry {s['file']} (pubDate={s.get('pubDate')}) emitted public route {candidate} in dist/ (filter getStaticPaths with isPublished).",
                        }
                    )
                    break
            if s["sampleText"] and s["sampleText"] in combined_text:
                failures.append(
                    {
                        "code": "scheduled_post_body_leaked",
                        "scheduledFile": s["file"],
                        "pubDate": s.get("pubDate"),
                        "message": f"Future-dated scheduled entry body text from {s['file']} appears in built dist/ HTML.",
                    }
                )

        _verify_dist_rss_leaks(dist, drafts_checked, scheduled_checked, failures)

    # Verify sitemaps & _redirects
    sitemap_files: list[str] = []
    redirect_rules: list[dict[str, Any]] = []
    sitemaps_checked = _verify_dist_sitemaps(
        dist,
        draft_routes,
        failures,
        scheduled_routes=scheduled_routes,
        sitemaps_out=sitemap_files,
    )
    redirects_checked = _verify_redirects_file(dist, proj_resolved, failures, rules_out=redirect_rules)

    # Verify staging / production environment rules
    if env is not None:
        _verify_env_rules(
            env=env,
            dist=dist,
            project_dir=proj_resolved,
            route_records=route_records,
            failures=failures,
        )

    site_manifest = _build_site_manifest(
        dist=dist,
        project_dir=proj_resolved,
        env=env,
        route_records=route_records,
        sitemaps=sitemap_files,
        redirect_rules=redirect_rules,
    )

    baseline_diff: dict[str, Any] | None = None
    if baseline_path is not None:
        baseline_diff = _compare_against_baseline(
            baseline_path=Path(baseline_path),
            dist=dist,
            route_records=route_records,
            route_map=route_map,
            redirect_rules=redirect_rules,
            current_manifest=site_manifest,
            allow_canonical_change=allow_canonical_change,
            failures=failures,
        )

    # Compare against migration contract if provided
    contract_summary: dict[str, Any] | None = None
    if contract_path:
        raw_cpath = Path(contract_path).expanduser()
        reject_symlink_chain(raw_cpath)
        cpath = raw_cpath.resolve()
        contract = json.loads(safe_read_bytes(cpath, max_bytes=MAX_SOURCE_FILE_BYTES).decode("utf-8"))
        c_pages = contract.get("pages", [])
        expected_ts = contract.get("recommendedAstroConfig", {}).get("trailingSlash")

        if expected_ts in ("always", "never"):
            for rec in route_records:
                if rec["urlShape"] == "root" or rec["file"] in ("404.html", "404/index.html"):
                    continue
                if expected_ts == "always" and rec["urlShape"] != "directory":
                    failures.append(
                        {
                            "code": "trailing_slash_shape_mismatch",
                            "route": rec["route"],
                            "file": rec["file"],
                            "message": f"Contract requires trailingSlash='always' (directory/index.html), but dist/ emitted {rec['file']}.",
                        }
                    )
                elif expected_ts == "never" and rec["urlShape"] != "file":
                    failures.append(
                        {
                            "code": "trailing_slash_shape_mismatch",
                            "route": rec["route"],
                            "file": rec["file"],
                            "message": f"Contract requires trailingSlash='never' (file.html), but dist/ emitted {rec['file']}.",
                        }
                    )

        matched_routes = 0
        for cp in c_pages:
            c_route = cp.get("route", "/")
            c_status = cp.get("status", "publish")
            if c_status == "dynamic":
                continue
            if c_status == "draft":
                if c_route in route_map:
                    failures.append(
                        {
                            "code": "contract_draft_emitted",
                            "route": c_route,
                            "message": f"Contract draft route {c_route} was emitted into dist/.",
                        }
                    )
                continue

            built = route_map.get(c_route)
            if not built:
                for alt in cp.get("alternateRoutes", []):
                    if alt in route_map:
                        built = route_map[alt]
                        break
            if not built:
                failures.append(
                    {
                        "code": "contract_route_missing",
                        "route": c_route,
                        "message": f"Published source route {c_route} is missing from dist/.",
                    }
                )
                continue

            matched_routes += 1

            if cp.get("title") and not built.get("title"):
                failures.append(
                    {
                        "code": "contract_title_lost",
                        "route": c_route,
                        "message": f"Route {c_route} had title '{cp['title']}' in source but has no <title> in dist/.",
                    }
                )
            if cp.get("metaDescription") and not built.get("metaDescription"):
                failures.append(
                    {
                        "code": "contract_meta_description_lost",
                        "route": c_route,
                        "message": f"Route {c_route} lost its <meta name=\"description\"> tag during build.",
                    }
                )
            if cp.get("canonical") and not built.get("canonical"):
                failures.append(
                    {
                        "code": "contract_canonical_lost",
                        "route": c_route,
                        "message": f"Route {c_route} lost its <link rel=\"canonical\"> tag during build.",
                    }
                )
            if cp.get("openGraph") and not built.get("openGraph"):
                failures.append(
                    {
                        "code": "contract_og_lost",
                        "route": c_route,
                        "message": f"Route {c_route} lost its OpenGraph metadata tags during build.",
                    }
                )
            if cp.get("jsonLdCount", 0) > 0 and built.get("jsonLdCount", 0) == 0:
                failures.append(
                    {
                        "code": "contract_jsonld_lost",
                        "route": c_route,
                        "message": f"Route {c_route} had {cp['jsonLdCount']} JSON-LD schema block(s) in source but 0 in dist/.",
                    }
                )
            for req_type in cp.get("jsonLdTypes", []):
                if req_type not in built.get("jsonLdTypes", []):
                    failures.append(
                        {
                            "code": "contract_jsonld_type_lost",
                            "route": c_route,
                            "schemaType": req_type,
                            "message": f"Route {c_route} lost JSON-LD @type '{req_type}' during build.",
                        }
                    )
            if cp.get("headings", {}).get("h1") and not built.get("headings", {}).get("h1"):
                failures.append(
                    {
                        "code": "contract_h1_lost",
                        "route": c_route,
                        "message": f"Route {c_route} had an <h1> in source but none in dist/.",
                    }
                )
            if cp.get("dataWfPage") and built.get("dataWfPage") != cp.get("dataWfPage"):
                failures.append(
                    {
                        "code": "contract_webflow_page_id_mismatch",
                        "route": c_route,
                        "expected": cp.get("dataWfPage"),
                        "actual": built.get("dataWfPage"),
                        "message": f"Route {c_route} data-wf-page mismatch: expected {cp.get('dataWfPage')}, got {built.get('dataWfPage')}.",
                    }
                )

            built_ids = set(built.get("domIds", []))
            for req_id in cp.get("requiredDomIds", []):
                if req_id not in built_ids:
                    failures.append(
                        {
                            "code": "contract_dom_id_missing",
                            "route": c_route,
                            "domId": req_id,
                            "message": f"Route {c_route} is missing required DOM id='{req_id}' from source contract.",
                        }
                    )

            if cp.get("hasFirstPaintInlineStyle") and not built.get("hasFirstPaintInlineStyle"):
                failures.append(
                    {
                        "code": "contract_first_paint_style_lost",
                        "route": c_route,
                        "message": f"Route {c_route} had an inline head <style> before scripts in source, but it was extracted or reordered in dist/ (use <style is:inline>).",
                    }
                )

        contract_summary = {
            "contractPath": str(cpath),
            "sourcePages": len(c_pages),
            "matchedPublishedRoutes": matched_routes,
        }

    ok = len(failures) == 0
    manifest_written_to: str | None = None
    manifest_skipped_reason: str | None = None
    if write_manifest_path is not None:
        if ok:
            written = safe_write_text(
                write_manifest_path,
                json.dumps(site_manifest, indent=2) + "\n",
                overwrite=overwrite,
            )
            manifest_written_to = str(written)
        else:
            manifest_skipped_reason = "verification_failed"

    report: dict[str, Any] = {
        "ok": ok,
        "dist": str(dist),
        "project": str(proj_resolved) if proj_resolved else None,
        "env": env,
        "forbidHosts": sorted(forbid_set),
        "builtHtmlCount": len(html_files),
        "draftEntriesChecked": len(drafts_checked),
        "scheduledEntriesChecked": len(scheduled_checked),
        "sitemapsChecked": sitemaps_checked,
        "redirectRulesChecked": redirects_checked,
        "contract": contract_summary,
        "baselineDiff": baseline_diff,
        "manifestWrittenTo": manifest_written_to,
        "routes": [
            {
                "route": info["route"],
                "file": info["file"],
                "title": info["title"],
                "domIdCount": len(info["domIds"]),
                "scriptCount": len(info["scripts"]),
                "hasFirstPaintInlineStyle": info["hasFirstPaintInlineStyle"],
            }
            for info in route_records
        ],
        "warnings": warnings,
        "failures": failures,
    }
    if manifest_skipped_reason is not None:
        report["manifestSkippedReason"] = manifest_skipped_reason
    return report


def _is_safe_dist_file(dist: Path, candidate: Path) -> bool:
    dist_resolved = dist.resolve()
    if is_reparse_or_symlink(candidate):
        return False
    try:
        reject_symlink_chain(candidate)
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return False
    if not resolved.is_relative_to(dist_resolved):
        return False
    return resolved.is_file() and not is_reparse_or_symlink(resolved)


def _resolve_dist_target(dist: Path, href: str) -> bool:
    clean = urllib.parse.unquote(href.split("?")[0].split("#")[0]).lstrip("/")
    if not clean:
        return _is_safe_dist_file(dist, dist / "index.html")
    trimmed = clean.rstrip("/")
    candidates = [
        dist / clean,
        dist / f"{trimmed}.html",
        dist / trimmed / "index.html",
    ]
    return any(_is_safe_dist_file(dist, c) for c in candidates)


def _resolve_dist_asset(dist: Path, src: str) -> bool:
    clean = urllib.parse.unquote(src.split("?")[0].split("#")[0]).lstrip("/")
    if not clean:
        return False
    return _is_safe_dist_file(dist, dist / clean)


def ingest_source_to_astro(
    source_path: str | Path,
    output_dir: str | Path,
    apply: bool = False,
    *,
    confirm_plan: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Preview or scaffold an Astro v7 Content Layer collection + migration contract from WordPress/HTML/Firecrawl sources."""
    contract = audit_source(source_path)
    raw_out = Path(output_dir).expanduser()
    reject_symlink_chain(raw_out)
    out = raw_out.resolve()
    reject_controlled_folder_access(out)
    planned_files: list[dict[str, Any]] = []

    content_config = """import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

const pages = defineCollection({
  loader: glob({
    pattern: "**/*.{md,mdx}",
    base: "./src/content/pages",
  }),
  schema: z.object({
    title: z.string(),
    description: z.string().optional(),
    legacyRoute: z.string(),
    postType: z.string().optional(),
    author: z.string().optional(),
    categories: z.array(z.string()).optional(),
    tags: z.array(z.string()).optional(),
    featuredImage: z.string().optional(),
    canonicalUrl: z.string().optional(),
    pubDate: z.coerce.date().optional(),
    draft: z.boolean().optional(),
  }),
});

export const collections = { pages };
"""

    published_helper = """export function isPublished(entry: { data: { draft?: boolean } }): boolean {
  if (!import.meta.env.PROD) return true;
  return entry.data.draft !== true;
}
"""

    planned_files.append({"path": "src/content.config.ts", "bytes": len(content_config.encode("utf-8"))})
    planned_files.append({"path": "src/lib/published.ts", "bytes": len(published_helper.encode("utf-8"))})
    planned_files.append({"path": "migration-contract.json", "bytes": len(json.dumps(contract, indent=2).encode("utf-8"))})

    redirects_lines: list[str] = []
    entries_to_write: list[tuple[str, str]] = []
    used_slugs: set[str] = set()
    for idx, page in enumerate(contract.get("pages", [])):
        raw_slug = page.get("slug") or page.get("route", "/").strip("/").replace(".html", "").replace("/", "-") or f"index-{idx}"
        base_slug = re.sub(r"[^A-Za-z0-9_-]", "-", str(raw_slug)).strip("-") or "home"
        slug = base_slug
        counter = 2
        while slug in used_slugs:
            slug = f"{base_slug}-{counter}"
            counter += 1
        used_slugs.add(slug)
        title_raw = str(page.get("title") or slug).replace("\r", " ").replace("\n", " ").strip()
        desc_raw = str(page.get("metaDescription") or "").replace("\r", " ").replace("\n", " ").strip()
        route = re.sub(r"[\r\n\s]+", "", str(page.get("route", "/"))) or "/"
        is_draft = page.get("status") == "draft"
        if not is_draft:
            for alt in page.get("alternateRoutes", []):
                clean_alt = re.sub(r"[\r\n\s]+", "", str(alt or ""))
                if clean_alt and clean_alt != route:
                    redirects_lines.append(f"{clean_alt} {route} 301")
        fm_lines = [
            "---",
            f"title: {json.dumps(title_raw, ensure_ascii=False)}",
        ]
        if desc_raw:
            fm_lines.append(f"description: {json.dumps(desc_raw, ensure_ascii=False)}")
        fm_lines.append(f"legacyRoute: {json.dumps(route, ensure_ascii=False)}")
        if page.get("postType"):
            fm_lines.append(f"postType: {json.dumps(str(page['postType']), ensure_ascii=False)}")
        if page.get("author"):
            fm_lines.append(f"author: {json.dumps(str(page['author']), ensure_ascii=False)}")
        if page.get("categories"):
            fm_lines.append(f"categories: {json.dumps(page['categories'], ensure_ascii=False)}")
        if page.get("tags"):
            fm_lines.append(f"tags: {json.dumps(page['tags'], ensure_ascii=False)}")
        if page.get("featuredImage"):
            fm_lines.append(f"featuredImage: {json.dumps(str(page['featuredImage']), ensure_ascii=False)}")
        if page.get("canonical"):
            canon_raw = str(page["canonical"]).replace("\r", "").replace("\n", "").strip()
            fm_lines.append(f"canonicalUrl: {json.dumps(canon_raw, ensure_ascii=False)}")
        if is_draft:
            fm_lines.append("draft: true")
        fm_lines.append("---")
        fm_lines.append("")
        md_body = (page.get("markdownBody") or "").strip()
        if md_body:
            if not md_body.startswith("#"):
                h1s = page.get("headings", {}).get("h1", [])
                heading_raw = str(h1s[0] if h1s else page.get("title") or slug)
                safe_heading = (
                    heading_raw.replace("\r", " ")
                    .replace("\n", " ")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .strip()
                )
                fm_lines.append(f"# {safe_heading}")
                fm_lines.append("")
            fm_lines.append(md_body)
            fm_lines.append("")
        else:
            h1s = page.get("headings", {}).get("h1", [])
            heading_raw = str(h1s[0] if h1s else page.get("title") or slug)
            safe_heading = (
                heading_raw.replace("\r", " ")
                .replace("\n", " ")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .strip()
            )
            fm_lines.append(f"# {safe_heading}")
            fm_lines.append("")
            if page.get("bodyExcerpt"):
                fm_lines.append(str(page["bodyExcerpt"]).replace("<", "&lt;").replace(">", "&gt;"))
                fm_lines.append("")
        md_text = "\n".join(fm_lines)
        rel_md = f"src/content/pages/{slug}.md"
        entries_to_write.append((rel_md, md_text))
        planned_files.append({"path": rel_md, "bytes": len(md_text.encode("utf-8")), "draft": is_draft, "legacyRoute": route})

    redirects_text = "\n".join(redirects_lines) + ("\n" if redirects_lines else "")
    if redirects_lines:
        planned_files.append({"path": "public/_redirects", "bytes": len(redirects_text.encode("utf-8")), "ruleCount": len(redirects_lines)})

    plan_canonical = json.dumps(
        {
            "sourceKind": contract["sourceKind"],
            "sourcePath": contract["sourcePath"],
            "outputDir": str(out),
            "plannedFiles": [
                {"path": item["path"], "draft": item.get("draft"), "legacyRoute": item.get("legacyRoute")}
                for item in planned_files
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    plan_sha256 = hashlib.sha256(plan_canonical.encode("utf-8")).hexdigest()

    if apply:
        if not confirm_plan or confirm_plan.strip().lower() != plan_sha256.lower():
            raise ValueError(
                f"Refusing ingest --apply without matching --confirm-plan {plan_sha256} (run preview first to obtain planSha256)."
            )
        safe_write_text(out / "src" / "content.config.ts", content_config, overwrite=overwrite)
        safe_write_text(out / "src" / "lib" / "published.ts", published_helper, overwrite=overwrite)
        safe_write_text(out / "migration-contract.json", json.dumps(contract, indent=2) + "\n", overwrite=overwrite)
        if redirects_lines:
            safe_write_text(out / "public" / "_redirects", redirects_text, overwrite=overwrite)
        for rel_md, md_text in entries_to_write:
            safe_write_text(out / rel_md, md_text, overwrite=overwrite)

    return {
        "ok": True,
        "applied": apply,
        "planSha256": plan_sha256,
        "sourceKind": contract["sourceKind"],
        "sourcePath": contract["sourcePath"],
        "outputDir": str(out),
        "recommendedAstroConfig": contract["recommendedAstroConfig"],
        "plannedFiles": planned_files,
        "next": (
            "Files written. Run `legends-astro verify-build --dist <dist> --project <dir> --contract migration-contract.json` after `astro build`."
            if apply
            else f"Preview only. Re-run with `--apply --confirm-plan {plan_sha256}` to write the Content Layer config, Markdown entries, and migration-contract.json."
        ),
    }
