"""Deterministic source auditing, WordPress/HTML/Firecrawl ingestion, and Astro dist/ parity verification."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class PageHTMLInspector(HTMLParser):
    """Extract SEO metadata, DOM IDs, scripts, styles, headings, forms, links, and assets from HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self._in_title = False
        self._in_head = False
        self._in_style = False
        self._in_script = False
        self._current_script_attrs: dict[str, str | None] = {}
        self._current_script_buf: list[str] = []
        self._current_heading_tag: str | None = None
        self._current_heading_buf: list[str] = []
        self._in_body = False
        self.body_text_parts: list[str] = []

        self.meta_description: str | None = None
        self.robots: str | None = None
        self.canonical: str | None = None
        self.open_graph: dict[str, str] = {}
        self.twitter_card: dict[str, str] = {}
        self.json_ld_blocks: list[Any] = []
        self.dom_ids: list[str] = []
        self.data_attributes: list[str] = []
        self.headings: dict[str, list[str]] = {"h1": [], "h2": [], "h3": []}
        self.scripts: list[dict[str, Any]] = []
        self.styles: list[dict[str, Any]] = []
        self.forms: list[dict[str, str | None]] = []
        self.internal_links: list[str] = []
        self.assets: list[str] = []
        self._tag_sequence: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): v for k, v in attrs}
        self._tag_sequence.append(t)

        if t == "head":
            self._in_head = True
        elif t == "body":
            self._in_body = True
        elif t == "title":
            self._in_title = True

        el_id = attr_map.get("id")
        if el_id and el_id not in self.dom_ids:
            self.dom_ids.append(el_id)

        for k in attr_map:
            if k.startswith("data-") and not k.startswith("data-astro-cid"):
                if k not in self.data_attributes:
                    self.data_attributes.append(k)

        if t == "meta":
            name = (attr_map.get("name") or "").lower()
            prop = (attr_map.get("property") or "").lower()
            content = attr_map.get("content") or ""
            if name == "description" and self.meta_description is None:
                self.meta_description = content
            elif name == "robots" and self.robots is None:
                self.robots = content
            elif name.startswith("twitter:") and content:
                self.twitter_card[name] = content
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

        elif t in ("h1", "h2", "h3"):
            self._current_heading_tag = t
            self._current_heading_buf = []

        elif t == "script":
            self._in_script = True
            self._current_script_attrs = attr_map
            self._current_script_buf = []
            src = attr_map.get("src")
            if src and _is_local_ref(src) and src not in self.assets:
                self.assets.append(src)

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
        elif t == "title":
            self._in_title = False
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
                except Exception:
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


def audit_source(source_path: str | Path, base_url: str | None = None) -> dict[str, Any]:
    """Audit a source HTML file/directory, WordPress XML/JSON export, or Firecrawl JSON/directory."""
    src = Path(source_path).resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source path does not exist: {src}")

    kind = _detect_source_kind(src)
    if kind == "wordpress-xml":
        return _audit_wordpress_xml(src, base_url)
    if kind == "wordpress-json":
        return _audit_wordpress_json(src, base_url)
    if kind == "firecrawl-json":
        return _audit_firecrawl_json(src, base_url)
    return _audit_html_tree(src, base_url)


def _detect_source_kind(src: Path) -> str:
    if src.is_file():
        suffix = src.suffix.lower()
        if suffix == ".xml":
            return "wordpress-xml"
        if suffix == ".json":
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
                if isinstance(data, list) and data and isinstance(data[0], dict) and "slug" in data[0] and "title" in data[0]:
                    return "wordpress-json"
                if isinstance(data, dict) and ("markdown" in data or "html" in data or "data" in data):
                    return "firecrawl-json"
            except Exception:
                pass
        return "html"
    return "html"


def _audit_html_tree(src: Path, base_url: str | None) -> dict[str, Any]:
    html_files: list[Path] = []
    js_files: list[Path] = []
    root_dir = src if src.is_dir() else src.parent

    if src.is_file():
        html_files = [src]
    else:
        for p in sorted(src.rglob("*")):
            if any(part in ("node_modules", ".git", ".astro", "dist") for part in p.relative_to(src).parts):
                continue
            if p.is_file():
                if p.suffix.lower() in (".html", ".htm"):
                    html_files.append(p)
                elif p.suffix.lower() in (".js", ".mjs"):
                    js_files.append(p)

    js_referenced_ids: set[str] = set()
    for jf in js_files:
        try:
            js_referenced_ids.update(_extract_js_referenced_ids(jf.read_text(encoding="utf-8", errors="ignore")))
        except Exception:
            pass

    pages: list[dict[str, Any]] = []
    for hf in html_files:
        raw_bytes = hf.read_bytes()
        sha = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8", errors="replace")
        js_referenced_ids.update(_extract_js_referenced_ids(text))
        rel = hf.relative_to(root_dir).as_posix()
        route, shape = _route_and_shape_from_relpath(rel)
        inspected = inspect_html_text(text)

        first_paint_inline = any(s["inHead"] and s["beforeFirstScript"] for s in inspected.styles)
        req_ids = [i for i in inspected.dom_ids if i in js_referenced_ids] or list(inspected.dom_ids)

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
                "jsonLdCount": len(inspected.json_ld_blocks),
                "jsonLdTypes": _json_ld_types(inspected.json_ld_blocks),
                "headings": inspected.headings,
                "domIds": inspected.dom_ids,
                "requiredDomIds": req_ids,
                "dataAttributes": inspected.data_attributes,
                "scripts": inspected.scripts,
                "hasFirstPaintInlineStyle": first_paint_inline,
                "forms": inspected.forms,
                "internalLinks": inspected.internal_links,
                "assets": inspected.assets,
            }
        )

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
        "jsReferencedIds": sorted(js_referenced_ids),
        "pages": pages,
    }


def _audit_wordpress_xml(src: Path, base_url: str | None) -> dict[str, Any]:
    tree = ET.parse(src)
    root = tree.getroot()
    ns = {
        "wp": "http://wordpress.org/export/1.2/",
        "content": "http://purl.org/rss/1.0/modules/content/",
        "excerpt": "http://wordpress.org/export/1.2/excerpt/",
        "dc": "http://purl.org/dc/elements/1.1/",
    }
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")

    pages: list[dict[str, Any]] = []
    draft_count = 0
    pub_count = 0

    for item in items:
        post_type = (item.findtext("wp:post_type", default="post", namespaces=ns) or "post").strip()
        if post_type not in ("post", "page"):
            continue
        status_raw = (item.findtext("wp:status", default="publish", namespaces=ns) or "publish").strip()
        status = "publish" if status_raw == "publish" else "draft"
        if status == "draft":
            draft_count += 1
        else:
            pub_count += 1

        title = (item.findtext("title") or "").strip() or None
        link = (item.findtext("link") or "").strip()
        slug = (item.findtext("wp:post_name", default="", namespaces=ns) or "").strip()
        pub_date = (item.findtext("wp:post_date", default="", namespaces=ns) or "").strip()
        body_html = item.findtext("content:encoded", default="", namespaces=ns) or ""
        excerpt = (item.findtext("excerpt:encoded", default="", namespaces=ns) or "").strip()

        meta_desc = excerpt or None
        for pm in item.findall("wp:postmeta", namespaces=ns):
            key = (pm.findtext("wp:meta_key", default="", namespaces=ns) or "").strip()
            val = (pm.findtext("wp:meta_value", default="", namespaces=ns) or "").strip()
            if key in ("_yoast_wpseo_metadesc", "rank_math_description", "_aioseo_description") and val:
                meta_desc = val
            elif key in ("_yoast_wpseo_title", "rank_math_title") and val and not title:
                title = val

        parsed_link = urlparse(link) if link else None
        route = parsed_link.path if (parsed_link and parsed_link.path) else (f"/{slug}/" if slug else "/")
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
                "status": status,
                "sha256": hashlib.sha256(body_html.encode("utf-8")).hexdigest(),
                "title": title,
                "metaDescription": meta_desc,
                "canonical": link or None,
                "robots": None,
                "openGraph": {},
                "twitterCard": {},
                "jsonLdCount": 0,
                "jsonLdTypes": [],
                "headings": inspected.headings,
                "domIds": inspected.dom_ids,
                "requiredDomIds": inspected.dom_ids,
                "dataAttributes": inspected.data_attributes,
                "scripts": inspected.scripts,
                "hasFirstPaintInlineStyle": False,
                "forms": inspected.forms,
                "internalLinks": inspected.internal_links,
                "assets": inspected.assets,
                "bodyExcerpt": " ".join(inspected.body_text_parts)[:240],
            }
        )

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
            "jsReferencedIdCount": 0,
            "totalDomIds": sum(len(p["domIds"]) for p in pages),
            "totalScripts": sum(len(p["scripts"]) for p in pages),
        },
        "jsReferencedIds": [],
        "pages": pages,
    }


def _audit_wordpress_json(src: Path, base_url: str | None) -> dict[str, Any]:
    items = json.loads(src.read_text(encoding="utf-8"))
    pages: list[dict[str, Any]] = []
    pub_count = 0
    draft_count = 0
    for item in items:
        status_raw = str(item.get("status", "publish"))
        status = "publish" if status_raw == "publish" else "draft"
        if status == "publish":
            pub_count += 1
        else:
            draft_count += 1
        slug = str(item.get("slug", "")).strip()
        link = str(item.get("link", "")).strip()
        title_obj = item.get("title")
        title = html.unescape(title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj or "")).strip() or None
        content_obj = item.get("content")
        body_html = content_obj.get("rendered", "") if isinstance(content_obj, dict) else str(content_obj or "")
        excerpt_obj = item.get("excerpt")
        excerpt_html = excerpt_obj.get("rendered", "") if isinstance(excerpt_obj, dict) else str(excerpt_obj or "")
        yoast = item.get("yoast_head_json") if isinstance(item.get("yoast_head_json"), dict) else {}
        meta_desc = yoast.get("description") or re.sub(r"<[^>]+>", "", excerpt_html).strip() or None
        parsed_link = urlparse(link) if link else None
        route = parsed_link.path if (parsed_link and parsed_link.path) else (f"/{slug}/" if slug else "/")
        inspected = inspect_html_text(body_html)
        pages.append(
            {
                "route": route,
                "alternateRoutes": [route.rstrip("/")] if route.endswith("/") and route != "/" else [],
                "urlShape": "directory" if route.endswith("/") else "clean",
                "sourceFile": src.name,
                "postType": str(item.get("type", "post")),
                "slug": slug,
                "pubDate": str(item.get("date", "")),
                "status": status,
                "sha256": hashlib.sha256(body_html.encode("utf-8")).hexdigest(),
                "title": title,
                "metaDescription": meta_desc,
                "canonical": yoast.get("canonical") or link or None,
                "robots": None,
                "openGraph": {},
                "twitterCard": {},
                "jsonLdCount": 1 if yoast.get("schema") else 0,
                "jsonLdTypes": [],
                "headings": inspected.headings,
                "domIds": inspected.dom_ids,
                "requiredDomIds": inspected.dom_ids,
                "dataAttributes": inspected.data_attributes,
                "scripts": inspected.scripts,
                "hasFirstPaintInlineStyle": False,
                "forms": inspected.forms,
                "internalLinks": inspected.internal_links,
                "assets": inspected.assets,
            }
        )
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
            "jsReferencedIdCount": 0,
            "totalDomIds": sum(len(p["domIds"]) for p in pages),
            "totalScripts": sum(len(p["scripts"]) for p in pages),
        },
        "jsReferencedIds": [],
        "pages": pages,
    }


def _audit_firecrawl_json(src: Path, base_url: str | None) -> dict[str, Any]:
    raw = json.loads(src.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        if isinstance(raw.get("data"), list):
            records = [r for r in raw["data"] if isinstance(r, dict)]
        elif isinstance(raw.get("data"), dict):
            records = [raw["data"]]
        else:
            records = [raw]
    pages: list[dict[str, Any]] = []
    for rec in records:
        meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
        source_url = str(meta.get("sourceURL") or meta.get("url") or rec.get("url") or "/")
        parsed = urlparse(source_url)
        route = parsed.path or "/"
        html_body = str(rec.get("html") or rec.get("rawHtml") or "")
        md_body = str(rec.get("markdown") or "")
        inspected = inspect_html_text(html_body) if html_body else PageHTMLInspector()
        pages.append(
            {
                "route": route,
                "alternateRoutes": [],
                "urlShape": "directory" if route.endswith("/") and route != "/" else ("file" if route.endswith(".html") else "clean"),
                "sourceFile": src.name,
                "status": "publish",
                "sha256": hashlib.sha256((html_body or md_body).encode("utf-8")).hexdigest(),
                "title": meta.get("title") or inspected.title,
                "metaDescription": meta.get("description") or inspected.meta_description,
                "canonical": source_url if source_url.startswith("http") else inspected.canonical,
                "robots": meta.get("robots") or inspected.robots,
                "openGraph": {k: str(v) for k, v in meta.items() if k.startswith("og:") and v},
                "twitterCard": {},
                "jsonLdCount": len(inspected.json_ld_blocks),
                "jsonLdTypes": _json_ld_types(inspected.json_ld_blocks),
                "headings": inspected.headings,
                "domIds": inspected.dom_ids,
                "requiredDomIds": inspected.dom_ids,
                "dataAttributes": inspected.data_attributes,
                "scripts": inspected.scripts,
                "hasFirstPaintInlineStyle": False,
                "forms": inspected.forms,
                "internalLinks": inspected.internal_links,
                "assets": inspected.assets,
            }
        )
    return {
        "schema": "legends.astro-migration-contract/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceKind": "firecrawl-json",
        "sourcePath": str(src),
        "baseUrl": base_url,
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
        "jsReferencedIds": [],
        "pages": pages,
    }


def _scan_project_drafts(project_dir: Path) -> list[dict[str, str]]:
    """Find Markdown/MDX content files with draft: true in frontmatter."""
    content_dir = project_dir / "src" / "content"
    drafts: list[dict[str, str]] = []
    if not content_dir.is_dir():
        return drafts

    for p in sorted(content_dir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".md", ".mdx"):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", text, re.DOTALL)
        if not m:
            continue
        fm, body = m.group(1), m.group(2).strip()
        if re.search(r"^\s*draft\s*:\s*true\s*$", fm, re.MULTILINE):
            rel = p.relative_to(content_dir).as_posix()
            stem = rel.rsplit(".", 1)[0]
            first_line = next((line.strip() for line in body.splitlines() if len(line.strip()) >= 12 and not line.strip().startswith("#")), "")
            drafts.append(
                {
                    "file": p.relative_to(project_dir).as_posix(),
                    "id": stem,
                    "slug": Path(stem).name,
                    "sampleText": first_line[:120],
                }
            )
    return drafts


def verify_build(
    dist_dir: str | Path,
    project_dir: str | Path | None = None,
    contract_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify an Astro dist/ directory for route parity, SEO/schema preservation, DOM ID ABI, draft exclusion, and asset resolution."""
    dist = Path(dist_dir).resolve()
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if not dist.is_dir():
        return {
            "ok": False,
            "dist": str(dist),
            "failures": [{"code": "dist_missing", "message": f"dist directory does not exist: {dist}"}],
            "warnings": [],
        }

    html_files = sorted(dist.rglob("*.html"))
    if not html_files:
        return {
            "ok": False,
            "dist": str(dist),
            "failures": [{"code": "dist_empty", "message": f"No .html files found in {dist}"}],
            "warnings": [],
        }

    route_map: dict[str, dict[str, Any]] = {}
    all_dist_text: list[str] = []

    for hf in html_files:
        rel = hf.relative_to(dist).as_posix()
        text = hf.read_text(encoding="utf-8", errors="replace")
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

        info = {
            "file": rel,
            "route": route,
            "urlShape": shape,
            "title": inspected.title,
            "metaDescription": inspected.meta_description,
            "canonical": inspected.canonical,
            "jsonLdCount": len(inspected.json_ld_blocks),
            "domIds": inspected.dom_ids,
            "scripts": inspected.scripts,
            "hasFirstPaintInlineStyle": first_paint_inline,
            "headings": inspected.headings,
        }
        route_map[route] = info
        if route.endswith(".html") and route != "/index.html":
            route_map.setdefault(route[:-5], info)
        elif route.endswith("/") and route != "/":
            route_map.setdefault(route.rstrip("/"), info)
        elif route != "/":
            route_map.setdefault(route + "/", info)

    # Check project drafts if project_dir provided
    drafts_checked: list[dict[str, str]] = []
    if project_dir:
        proj = Path(project_dir).resolve()
        drafts_checked = _scan_project_drafts(proj)
        combined_text = "\n".join(all_dist_text)
        for d in drafts_checked:
            slug = d["slug"]
            stem = d["id"]
            for candidate in (f"/{stem}", f"/{stem}.html", f"/{stem}/", f"/{slug}", f"/{slug}.html", f"/{slug}/"):
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

    # Compare against migration contract if provided
    contract_summary: dict[str, Any] | None = None
    if contract_path:
        cpath = Path(contract_path).resolve()
        contract = json.loads(cpath.read_text(encoding="utf-8"))
        c_pages = contract.get("pages", [])
        matched_routes = 0
        for cp in c_pages:
            c_route = cp.get("route", "/")
            c_status = cp.get("status", "publish")
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
            if cp.get("jsonLdCount", 0) > 0 and built.get("jsonLdCount", 0) == 0:
                failures.append(
                    {
                        "code": "contract_jsonld_lost",
                        "route": c_route,
                        "message": f"Route {c_route} had {cp['jsonLdCount']} JSON-LD schema block(s) in source but 0 in dist/.",
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

    return {
        "ok": len(failures) == 0,
        "dist": str(dist),
        "project": str(Path(project_dir).resolve()) if project_dir else None,
        "builtHtmlCount": len(html_files),
        "draftEntriesChecked": len(drafts_checked),
        "contract": contract_summary,
        "routes": [
            {
                "route": info["route"],
                "file": info["file"],
                "title": info["title"],
                "domIdCount": len(info["domIds"]),
                "scriptCount": len(info["scripts"]),
                "hasFirstPaintInlineStyle": info["hasFirstPaintInlineStyle"],
            }
            for hf in html_files
            for info in [route_map[_route_and_shape_from_relpath(hf.relative_to(dist).as_posix())[0]]]
        ],
        "warnings": warnings,
        "failures": failures,
    }


def _resolve_dist_target(dist: Path, href: str) -> bool:
    rel = href.lstrip("/")
    if not rel:
        return (dist / "index.html").is_file()
    candidates = [
        dist / rel,
        dist / f"{rel}.html",
        dist / rel / "index.html",
    ]
    return any(c.is_file() for c in candidates)


def _resolve_dist_asset(dist: Path, src: str) -> bool:
    clean = src.split("?")[0].split("#")[0].lstrip("/")
    if not clean:
        return False
    return (dist / clean).is_file()


def ingest_source_to_astro(
    source_path: str | Path,
    output_dir: str | Path,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or scaffold an Astro v7 Content Layer collection + migration contract from WordPress/HTML/Firecrawl sources."""
    contract = audit_source(source_path)
    out = Path(output_dir).resolve()
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

    entries_to_write: list[tuple[str, str]] = []
    for idx, page in enumerate(contract.get("pages", [])):
        slug = page.get("slug") or page.get("route", "/").strip("/").replace(".html", "").replace("/", "-") or f"index-{idx}"
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", slug).strip("-") or "home"
        title = (page.get("title") or slug).replace('"', '\\"')
        desc = (page.get("metaDescription") or "").replace('"', '\\"')
        route = page.get("route", "/")
        is_draft = page.get("status") == "draft"
        fm_lines = [
            "---",
            f'title: "{title}"',
        ]
        if desc:
            fm_lines.append(f'description: "{desc}"')
        fm_lines.append(f'legacyRoute: "{route}"')
        if page.get("canonical"):
            fm_lines.append(f'canonicalUrl: "{page["canonical"]}"')
        if is_draft:
            fm_lines.append("draft: true")
        fm_lines.append("---")
        fm_lines.append("")
        h1s = page.get("headings", {}).get("h1", [])
        fm_lines.append(f"# {h1s[0] if h1s else page.get('title') or slug}")
        fm_lines.append("")
        if page.get("bodyExcerpt"):
            fm_lines.append(page["bodyExcerpt"])
            fm_lines.append("")
        md_text = "\n".join(fm_lines)
        rel_md = f"src/content/pages/{slug}.md"
        entries_to_write.append((rel_md, md_text))
        planned_files.append({"path": rel_md, "bytes": len(md_text.encode("utf-8")), "draft": is_draft, "legacyRoute": route})

    if apply:
        out.mkdir(parents=True, exist_ok=True)
        (out / "src" / "lib").mkdir(parents=True, exist_ok=True)
        (out / "src" / "content" / "pages").mkdir(parents=True, exist_ok=True)
        (out / "src" / "content.config.ts").write_text(content_config, encoding="utf-8")
        (out / "src" / "lib" / "published.ts").write_text(published_helper, encoding="utf-8")
        (out / "migration-contract.json").write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
        for rel_md, md_text in entries_to_write:
            target = out / rel_md
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(md_text, encoding="utf-8")

    return {
        "ok": True,
        "applied": apply,
        "sourceKind": contract["sourceKind"],
        "sourcePath": contract["sourcePath"],
        "outputDir": str(out),
        "recommendedAstroConfig": contract["recommendedAstroConfig"],
        "plannedFiles": planned_files,
        "next": (
            "Files written. Run `legends-astro verify-build --dist <dist> --project <dir> --contract migration-contract.json` after `astro build`."
            if apply
            else "Preview only. Re-run with `--apply` to write the Content Layer config, Markdown entries, and migration-contract.json."
        ),
    }
