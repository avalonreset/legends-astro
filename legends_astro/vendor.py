"""Two-pass remote asset localizer (HTML + recursive CSS @import/url()) and longest-URL-first rewriter."""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import html
import json
import os
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

from .net_safety import (
    DEFAULT_MAX_ASSET_BYTES,
    DEFAULT_MAX_PAGE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    _TAG_BOUNDARY_CHARS,
    assert_path_within,
    extract_html_tag_bodies,
    find_html_close_tag,
    host_matches_allowlist,
    is_reparse_or_symlink,
    normalize_host,
    reject_controlled_folder_access,
    reject_symlink_chain,
    replace_html_blocks,
    safe_fetch_bytes,
    safe_read_bytes,
    safe_write_bytes,
    safe_write_text,
    sanitize_rel_path,
    scan_html_open_tag,
)

_CSS_URL_RE = re.compile(
    r"""url\(\s*(?:&quot;|&#34;|["'])?((?:(?!(?:&quot;|&#34;|["'()\s])).){1,4096}?)(?:&quot;|&#34;|["'])?\s*\)""",
    re.IGNORECASE,
)
_CSS_URL_QUOTED_RE = re.compile(
    r"""url\(\s*(?P<q1>&quot;|&#34;|["'])?(?P<url>(?:(?!(?:&quot;|&#34;|["'()\s])).){1,4096}?)(?P<q2>&quot;|&#34;|["'])?\s*\)""",
    re.IGNORECASE,
)
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*)?(?:["']([^"']+)["']|([^\s;)]+))\s*\)?""",
    re.IGNORECASE,
)
_TAG_SRI_RE = re.compile(r"<(link|script)\b([^<>]*)>", re.IGNORECASE)
_INTEGRITY_ATTR_RE = re.compile(r"""(?<!\s)\s++integrity\s*+=\s*+(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_CROSSORIGIN_ATTR_RE = re.compile(r"""(?<!\s)\s++crossorigin(?:\s*+=\s*+(?:"[^"]*"|'[^']*'|[^\s>]+))?""", re.IGNORECASE)
_LOCAL_ASSET_HREF_SRC_RE = re.compile(
    r"""\b(?:href|src)\s*=\s*(?:["']?)/(?:assets|wp-content|wp-includes)/""",
    re.IGNORECASE,
)

_FONT_EXTS = frozenset({".woff2", ".woff", ".ttf", ".otf", ".eot"})
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".svg"})
_ASSET_LINK_RELS = frozenset(
    {"stylesheet", "icon", "shortcut", "apple-touch-icon", "preload", "modulepreload", "prefetch", "manifest", "mask-icon"}
)
_CONTENT_TYPE_EXT_MAP: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "font/woff2": ".woff2",
    "font/woff": ".woff",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff2": ".woff2",
    "application/font-woff": ".woff",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/x-javascript": ".js",
    "text/css": ".css",
}


def parse_srcset(srcset_value: str) -> list[str]:
    """Extract URLs from an HTML srcset attribute with width (500w) or density (2x) descriptors."""
    urls: list[str] = []
    if not srcset_value:
        return urls
    # Split candidate entries separated by commas followed by whitespace and a URL start
    parts = re.split(r",\s+(?=(?:https?:|//|/|[^\s,]+))", srcset_value.strip())
    for part in parts:
        tokens = part.strip().split()
        if tokens:
            candidate = tokens[0].rstrip(",")
            if candidate and not candidate.startswith("data:"):
                urls.append(candidate)
    return urls


class _AssetExtractor(HTMLParser):
    """Extract remote/local asset URLs from HTML tags, srcset, data-video-urls, data-poster-url, and inline CSS."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self._in_style = False
        self._style_buf: list[str] = []

    def _add(self, raw: str | None) -> None:
        if not raw:
            return
        u = raw.strip()
        if not u or u.startswith(("data:", "mailto:", "tel:", "javascript:", "#")):
            return
        if u not in self.urls:
            self.urls.append(u)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if t == "style":
            self._in_style = True
            self._style_buf = []

        if t == "link":
            rel = attr_map.get("rel", "").lower()
            if any(tok in rel.split() for tok in _ASSET_LINK_RELS):
                self._add(attr_map.get("href"))
            if "imagesrcset" in attr_map:
                for u in parse_srcset(attr_map["imagesrcset"]):
                    self._add(u)
        elif t == "meta":
            prop = attr_map.get("property", "").lower()
            name = attr_map.get("name", "").lower()
            if prop in ("og:image", "og:image:url", "og:image:secure_url") or name in ("twitter:image", "twitter:image:src"):
                content = attr_map.get("content", "").strip()
                if content.startswith(("http://", "https://", "//", "/")):
                    self._add(content)
        elif t in ("script", "img", "video", "audio", "source", "embed", "track"):
            self._add(attr_map.get("src"))
            self._add(attr_map.get("poster"))

        if "srcset" in attr_map:
            for u in parse_srcset(attr_map["srcset"]):
                self._add(u)
        if "data-srcset" in attr_map:
            for u in parse_srcset(attr_map["data-srcset"]):
                self._add(u)
        if "data-src" in attr_map:
            self._add(attr_map["data-src"])
        if "data-poster-url" in attr_map:
            self._add(attr_map["data-poster-url"])
        if "data-video-urls" in attr_map:
            for item in attr_map["data-video-urls"].split(","):
                self._add(item)

        inline_style = attr_map.get("style")
        if inline_style:
            for m in _CSS_URL_RE.finditer(inline_style):
                self._add(html.unescape(m.group(1)))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style" and self._in_style:
            self._in_style = False
            css_text = "".join(self._style_buf)
            self._style_buf = []
            for u in extract_css_urls(css_text):
                self._add(u)

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._style_buf.append(data)


def extract_css_urls(css_text: str) -> list[str]:
    """Extract @import and url(...) references from CSS text."""
    found: list[str] = []
    for m in _CSS_IMPORT_RE.finditer(css_text):
        u = (m.group(1) or m.group(2) or "").strip()
        if u and not u.startswith(("data:", "#")) and u not in found:
            found.append(u)
    for m in _CSS_URL_RE.finditer(css_text):
        u = html.unescape(m.group(1)).strip()
        if u and not u.startswith(("data:", "#")) and u not in found:
            found.append(u)
    return found


_INLINE_STYLE_ATTR_RE = re.compile(r"""\bstyle\s*+=\s*+(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE | re.DOTALL)


def extract_html_asset_urls(html_text: str) -> list[str]:
    """Extract asset URLs from HTML markup, excluding <script> bodies and HTML comments from CSS url() scanning."""
    p = _AssetExtractor()
    p.feed(html_text)
    # Strip <script> blocks and HTML comments in O(N) before running regex CSS url() fallback
    # restricted strictly to <style> blocks and style="..." attributes (never JS new URL(...)).
    sanitized = replace_html_blocks(html_text, tags=("script",), strip_comments=True, default_replacement=" ")
    css_fragments: list[str] = extract_html_tag_bodies(sanitized, "style")
    for m in _INLINE_STYLE_ATTR_RE.finditer(sanitized):
        css_fragments.append(m.group(1) if m.group(1) is not None else (m.group(2) or ""))
    for frag in css_fragments:
        for m in _CSS_URL_RE.finditer(frag):
            u = html.unescape(m.group(1)).strip()
            if u and not u.startswith(("data:", "#")) and u not in p.urls:
                p.urls.append(u)
    return p.urls


def _normalize_fetch_url(raw_url: str, base_url: str | None = None) -> str | None:
    u = raw_url.strip()
    if not u or u.startswith(("data:", "mailto:", "tel:", "javascript:", "#")):
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif not u.startswith(("http://", "https://")):
        if base_url:
            u = urllib.parse.urljoin(base_url, u)
        else:
            return None
    # Encode literal spaces cleanly for HTTP requests
    parsed = urllib.parse.urlsplit(u)
    safe_path = parsed.path.replace(" ", "%20")
    safe_query = parsed.query.replace(" ", "%20")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, safe_path, safe_query, ""))


def _local_public_path_for(
    remote_url: str,
    *,
    content_type: str = "",
    origin_host: str | None = None,
    used_paths: set[str] | None = None,
) -> str:
    """Compute a deterministic local public path (/wp-content/... or /assets/...)."""
    parsed = urllib.parse.urlsplit(remote_url)
    host = (parsed.hostname or "asset").lower()
    rel_clean = sanitize_rel_path(parsed.path or "asset")
    if not rel_clean:
        rel_clean = "asset"

    # Infer file extensions from Content-Type for dynamic/extensionless endpoints (e.g. /_next/image?..., dynamic_css.php)
    ctype_main = (content_type or "").split(";", 1)[0].strip().lower()
    if rel_clean.lower().endswith(".php") and ctype_main == "text/css":
        rel_clean = rel_clean[:-4] + ".css"
    elif "." not in Path(rel_clean).name:
        inferred_ext = _CONTENT_TYPE_EXT_MAP.get(ctype_main)
        if inferred_ext:
            rel_clean = rel_clean + inferred_ext

    # Disambiguate meaningful query strings (e.g. dynamic_css.php?id=5) while ignoring bare ?ver=
    if parsed.query:
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        non_ver = {k: v for k, v in qs.items() if k.lower() not in ("ver", "v", "cb")}
        if non_ver:
            qhash = hashlib.sha256(parsed.query.encode("utf-8")).hexdigest()[:8]
            p = Path(rel_clean)
            rel_clean = str(p.with_name(f"{p.stem}-{qhash}{p.suffix}")).replace("\\", "/")

    norm_origin = normalize_host(origin_host) if origin_host else ""
    if rel_clean.startswith(("wp-content/", "wp-includes/")):
        public_rel = rel_clean
    elif norm_origin and host == norm_origin:
        public_rel = f"assets/{rel_clean}"
    else:
        host_bucket = re.sub(r"[^a-z0-9-]+", "-", host.replace(".", "-")).strip("-") or "cdn"
        public_rel = f"assets/{host_bucket}/{rel_clean}"

    public_path = "/" + public_rel.lstrip("/")
    if used_paths is not None and public_path in used_paths:
        uhash = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()[:8]
        p = Path(public_rel)
        cand = "/" + str(p.with_name(f"{p.stem}-{uhash}{p.suffix}")).replace("\\", "/")
        counter = 2
        while cand in used_paths:
            cand = "/" + str(p.with_name(f"{p.stem}-{uhash}-{counter}{p.suffix}")).replace("\\", "/")
            counter += 1
        public_path = cand
    if used_paths is not None:
        used_paths.add(public_path)
    return public_path


def strip_sri_on_local_tags(html_text: str) -> str:
    """Remove integrity= and crossorigin= attributes on <link>/<script> tags pointing to local vendored assets."""

    def _clean(match: re.Match[str]) -> str:
        tag = match.group(1)
        attrs = match.group(2)
        if not _LOCAL_ASSET_HREF_SRC_RE.search(attrs):
            return match.group(0)
        new_attrs = _INTEGRITY_ATTR_RE.sub("", attrs)
        new_attrs = _CROSSORIGIN_ATTR_RE.sub("", new_attrs)
        return f"<{tag}{new_attrs}>"

    return _TAG_SRI_RE.sub(_clean, html_text)


_PRECONNECT_LINK_RE = re.compile(r"<link\b[^<>]*>", re.IGNORECASE)
_REL_ATTR_VAL_RE = re.compile(r"""\brel\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)
_HREF_ATTR_VAL_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.IGNORECASE)


def strip_vendored_preconnect_links(html_text: str, asset_map: dict[str, str]) -> str:
    """Remove <link rel="preconnect|dns-prefetch"> tags targeting hosts whose assets were vendored locally."""
    vendored_hosts = {normalize_host(u) for u in asset_map.keys() if normalize_host(u)}
    if not vendored_hosts:
        return html_text

    def _filter_link(match: re.Match[str]) -> str:
        tag = match.group(0)
        rel_m = _REL_ATTR_VAL_RE.search(tag)
        if not rel_m:
            return tag
        rel_val = (rel_m.group(1) or rel_m.group(2) or rel_m.group(3) or "").lower().split()
        if not any(tok in rel_val for tok in ("preconnect", "dns-prefetch")):
            return tag
        href_m = _HREF_ATTR_VAL_RE.search(tag)
        if not href_m:
            return tag
        href_val = (href_m.group(1) or href_m.group(2) or href_m.group(3) or "").strip()
        if href_val.startswith("//"):
            href_val = "https:" + href_val
        host = normalize_host(href_val)
        if host and host in vendored_hosts:
            return ""
        return tag

    return _PRECONNECT_LINK_RE.sub(_filter_link, html_text)


def rewrite_urls_longest_first(text: str, replacements: dict[str, str]) -> str:
    """Replace remote URLs with local public paths in a single longest-URL-first regex pass to prevent multi-pass clobbering."""
    if not replacements:
        return text
    expanded: dict[str, str] = {}
    for remote, local in replacements.items():
        if not remote:
            continue
        expanded[remote] = local
        # Also handle HTML-escaped ampersands and unescaped spaces
        if "&" in remote:
            expanded.setdefault(remote.replace("&", "&amp;"), local)
        if "%20" in remote:
            expanded.setdefault(remote.replace("%20", " "), local)
        if remote.startswith("https://"):
            expanded.setdefault("//" + remote[len("https://") :], local)
    if not expanded:
        return text
    keys = sorted(expanded.keys(), key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(k) for k in keys))
    return pattern.sub(lambda m: expanded[m.group(0)], text)


_OPEN_TAG_RE = re.compile(r"^<([a-zA-Z][a-zA-Z0-9:-]*)([\s\S]*?)(/?)>$")
_TAG_ATTR_RE = re.compile(
    r"""(?<!\s)(?P<prefix>\s++)(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)(?P<eq>\s*+=\s*+(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<uq>[^\s"'=<>`]+)))?"""
)


def _build_root_relative_map(
    asset_map: dict[str, str],
    *,
    origin_url: str | None = None,
    root_relative_map: dict[str, str] | None = None,
) -> dict[str, str]:
    rel_map: dict[str, str] = {}

    def _add(key: str, local: str) -> None:
        if not key or key == local:
            return
        rel_map.setdefault(key, local)
        unesc = html.unescape(key)
        if unesc != key:
            rel_map.setdefault(unesc, local)
        if "&" in unesc:
            rel_map.setdefault(unesc.replace("&", "&amp;"), local)
        if "%20" in unesc:
            rel_map.setdefault(unesc.replace("%20", " "), local)

    if root_relative_map:
        for k, v in root_relative_map.items():
            if k.startswith("/") and not k.startswith("//"):
                _add(k, v)

    norm_origin = normalize_host(origin_url) if origin_url else ""
    for remote, local in asset_map.items():
        if remote.startswith("/") and not remote.startswith("//"):
            _add(remote, local)
            continue
        if not remote.startswith(("http://", "https://")):
            continue
        p = urllib.parse.urlsplit(remote)
        host = normalize_host(p.hostname or "")
        if not host:
            continue
        host_bucket = re.sub(r"[^a-z0-9-]+", "-", host.replace(".", "-")).strip("-") or "cdn"
        if norm_origin:
            is_same_origin = host == norm_origin
        else:
            is_same_origin = local.startswith(("/wp-content/", "/wp-includes/")) or not local.startswith(
                f"/assets/{host_bucket}/"
            )
        if is_same_origin:
            rel_key = (p.path or "/") + (f"?{p.query}" if p.query else "")
            _add(rel_key, local)
    return rel_map


def _lookup_rel_asset(raw_val: str, rel_map: dict[str, str]) -> str | None:
    v = raw_val.strip()
    if not v or not v.startswith("/") or v.startswith("//"):
        return None
    base, hash_sep, frag = v.partition("#")
    candidates = [base]
    unesc = html.unescape(base)
    if unesc != base:
        candidates.append(unesc)
    if " " in unesc:
        candidates.append(unesc.replace(" ", "%20"))
    if "%20" in unesc:
        candidates.append(unesc.replace("%20", " "))
    for cand in candidates:
        hit = rel_map.get(cand)
        if hit:
            return f"{hit}#{frag}" if hash_sep else hit
    return None


def _rewrite_srcset_value(val: str, rel_map: dict[str, str]) -> str:
    parts = re.split(r"(,\s+)(?=(?:https?:|//|/|[^\s,]+))", val)
    out: list[str] = []
    for part in parts:
        if re.fullmatch(r",\s+", part):
            out.append(part)
            continue
        m = re.match(r"^(\s*)(\S+)([\s\S]*)$", part)
        if not m:
            out.append(part)
            continue
        lead, url_tok, rest = m.group(1), m.group(2), m.group(3)
        trailing_comma = ""
        if url_tok.endswith(","):
            url_tok = url_tok[:-1]
            trailing_comma = ","
        hit = _lookup_rel_asset(url_tok, rel_map)
        if hit:
            out.append(f"{lead}{hit}{trailing_comma}{rest}")
        else:
            out.append(part)
    return "".join(out)


def _escape_css_url(url: str) -> str:
    """Percent-encode quotes, parens, and whitespace in replacement URLs (CWE-79)."""
    return (
        re.sub(r"\s", lambda m: f"%{ord(m.group(0)):02X}", url)
        .replace("'", "%27")
        .replace('"', "%22")
        .replace("(", "%28")
        .replace(")", "%29")
    )


def _rewrite_css_urls_with_lookup(css_text: str, lookup_fn: Callable[[str], str | None]) -> str:
    def _replace_url(m: re.Match[str]) -> str:
        raw_url = m.group("url")
        hit = lookup_fn(raw_url)
        if not hit:
            return m.group(0)
        safe_hit = _escape_css_url(hit)
        q1 = m.group("q1") or ""
        q2 = m.group("q2") or q1
        return f"url({q1}{safe_hit}{q2})"

    def _replace_import(m: re.Match[str]) -> str:
        raw_url = (m.group(1) or m.group(2) or "").strip()
        hit = lookup_fn(raw_url)
        if not hit:
            return m.group(0)
        safe_hit = _escape_css_url(hit)
        if "url(" in m.group(0).lower():
            return f'@import url("{safe_hit}")'
        return f'@import "{safe_hit}"'

    out = _CSS_IMPORT_RE.sub(_replace_import, css_text)
    out = _CSS_URL_QUOTED_RE.sub(_replace_url, out)
    return out


def _rewrite_opening_tag_rel_assets(tag_text: str, rel_map: dict[str, str]) -> str:
    m_open = _OPEN_TAG_RE.match(tag_text)
    if not m_open:
        return tag_text
    tag_name = m_open.group(1)
    attrs_raw = m_open.group(2)
    closing_slash = m_open.group(3)
    t = tag_name.lower()

    # Collect attribute metadata to gate <link rel=...> and <meta property/name=...>
    attr_values: dict[str, str] = {}
    for am in _TAG_ATTR_RE.finditer(attrs_raw):
        aname = am.group("name").lower()
        aval = am.group("dq") if am.group("dq") is not None else (am.group("sq") if am.group("sq") is not None else (am.group("uq") or ""))
        attr_values[aname] = aval

    rewrite_href = False
    if t == "link":
        rels = set(attr_values.get("rel", "").lower().split())
        rewrite_href = bool(rels & _ASSET_LINK_RELS)

    rewrite_src_poster = t in ("script", "img", "video", "audio", "source", "embed", "track")
    rewrite_meta_content = False
    if t == "meta":
        prop = attr_values.get("property", "").lower()
        mname = attr_values.get("name", "").lower()
        rewrite_meta_content = prop in ("og:image", "og:image:url", "og:image:secure_url") or mname in (
            "twitter:image",
            "twitter:image:src",
        )

    def _sub_attr(am: re.Match[str]) -> str:
        if not am.group("eq"):
            return am.group(0)
        aname = am.group("name").lower()
        if am.group("dq") is not None:
            quote, val = '"', am.group("dq")
        elif am.group("sq") is not None:
            quote, val = "'", am.group("sq")
        else:
            quote, val = "", am.group("uq") or ""

        new_val: str | None = None
        if aname == "href" and rewrite_href:
            new_val = _lookup_rel_asset(val, rel_map)
        elif aname in ("src", "poster") and rewrite_src_poster:
            new_val = _lookup_rel_asset(val, rel_map)
        elif aname == "content" and rewrite_meta_content:
            new_val = _lookup_rel_asset(val, rel_map)
        elif aname in ("data-src", "data-poster-url"):
            new_val = _lookup_rel_asset(val, rel_map)
        elif aname in ("srcset", "data-srcset", "imagesrcset"):
            cand = _rewrite_srcset_value(val, rel_map)
            if cand != val:
                new_val = cand
        elif aname == "data-video-urls":
            items = val.split(",")
            changed = False
            rebuilt: list[str] = []
            for item in items:
                hit = _lookup_rel_asset(item, rel_map)
                if hit:
                    changed = True
                    rebuilt.append(hit)
                else:
                    rebuilt.append(item)
            if changed:
                new_val = ",".join(rebuilt)
        elif aname == "style":
            cand = _rewrite_css_urls_with_lookup(val, lambda u: _lookup_rel_asset(u, rel_map))
            if cand != val:
                new_val = cand

        if new_val is None:
            return am.group(0)
        if quote == '"':
            safe_val = new_val.replace('"', "&quot;")
            return f'{am.group("prefix")}{am.group("name")}="{safe_val}"'
        if quote == "'":
            safe_val = new_val.replace("'", "&#39;")
            return f"{am.group('prefix')}{am.group('name')}='{safe_val}'"
        if re.search(r"""[\s"'=<>`]""", new_val):
            safe_val = new_val.replace('"', "&quot;")
            return f'{am.group("prefix")}{am.group("name")}="{safe_val}"'
        return f"{am.group('prefix')}{am.group('name')}={new_val}"

    new_attrs = _TAG_ATTR_RE.sub(_sub_attr, attrs_raw)
    if new_attrs == attrs_raw:
        return tag_text
    return f"<{tag_name}{new_attrs}{closing_slash}>"


def _rewrite_html_rel_assets(html_text: str, rel_map: dict[str, str]) -> str:
    if not rel_map or not html_text or "<" not in html_text:
        return html_text

    low = html_text.lower()
    n = len(html_text)
    pos = 0
    out: list[str] = []
    no_comment_end = False
    no_script_end = False
    no_style_end = False

    while pos < n:
        lt = html_text.find("<", pos)
        if lt == -1:
            out.append(html_text[pos:])
            break
        if lt > pos:
            out.append(html_text[pos:lt])
            pos = lt

        # 1. HTML comment: <!-- ... -->
        if html_text.startswith("<!--", pos):
            if not no_comment_end:
                end = html_text.find("-->", pos + 4)
                if end != -1:
                    out.append(html_text[pos : end + 3])
                    pos = end + 3
                    continue
                no_comment_end = True
            out.append("<!--")
            pos += 4
            continue

        # Must start with an ASCII letter to be an opening tag
        if pos + 1 >= n or not html_text[pos + 1].isascii() or not html_text[pos + 1].isalpha():
            out.append("<")
            pos += 1
            continue

        # 2. <script ...> ... </script>
        if low.startswith("script", pos + 1) and pos + 7 < n and low[pos + 7] in _TAG_BOUNDARY_CHARS:
            gt, next_pos = scan_html_open_tag(html_text, pos)
            if gt == -1:
                out.append(html_text[pos:next_pos])
                pos = next_pos
                continue
            open_tag = _rewrite_opening_tag_rel_assets(html_text[pos : gt + 1], rel_map)
            if not no_script_end:
                close_start, close_after = find_html_close_tag(low, "script", gt + 1)
                if close_start != -1:
                    out.append(open_tag)
                    out.append(html_text[gt + 1 : close_after])
                    pos = close_after
                    continue
                no_script_end = True
            out.append(open_tag)
            pos = gt + 1
            continue

        # 3. <style ...> ... </style>
        if low.startswith("style", pos + 1) and pos + 6 < n and low[pos + 6] in _TAG_BOUNDARY_CHARS:
            gt, next_pos = scan_html_open_tag(html_text, pos)
            if gt == -1:
                out.append(html_text[pos:next_pos])
                pos = next_pos
                continue
            open_tag = html_text[pos : gt + 1]
            if not no_style_end:
                close_start, close_after = find_html_close_tag(low, "style", gt + 1)
                if close_start != -1:
                    css_body = html_text[gt + 1 : close_start]
                    rewritten_css = _rewrite_css_urls_with_lookup(css_body, lambda u: _lookup_rel_asset(u, rel_map))
                    out.append(open_tag)
                    out.append(rewritten_css)
                    out.append(html_text[close_start:close_after])
                    pos = close_after
                    continue
                no_style_end = True
            out.append(open_tag)
            pos = gt + 1
            continue

        # 4. General opening tag: <tag ...>
        gt, next_pos = scan_html_open_tag(html_text, pos)
        if gt == -1:
            out.append(html_text[pos:next_pos])
            pos = next_pos
            continue
        out.append(_rewrite_opening_tag_rel_assets(html_text[pos : gt + 1], rel_map))
        pos = gt + 1

    return "".join(out)


def rewrite_html_assets(
    html_text: str,
    asset_map: dict[str, str],
    *,
    origin_url: str | None = None,
    root_relative_map: dict[str, str] | None = None,
) -> str:
    """Rewrite absolute and same-origin root-relative asset URLs in HTML safely, strip local SRI, and drop vendored preconnect hints."""
    abs_map = {k: _escape_css_url(v) for k, v in asset_map.items() if k.startswith(("http://", "https://", "//"))}
    rel_map = _build_root_relative_map(asset_map, origin_url=origin_url, root_relative_map=root_relative_map)
    rewritten = rewrite_urls_longest_first(html_text, abs_map)
    if rel_map:
        rewritten = _rewrite_html_rel_assets(rewritten, rel_map)
    rewritten = strip_sri_on_local_tags(rewritten)
    return strip_vendored_preconnect_links(rewritten, asset_map)


def rewrite_css_assets(css_text: str, asset_map: dict[str, str], *, css_remote_url: str | None = None) -> str:
    """Rewrite absolute and relative url(...) / @import URLs in CSS in a single construct-scoped pass without substring clobbering."""
    if not asset_map:
        return css_text

    def _lookup_css_asset(raw_ref: str) -> str | None:
        u = html.unescape(raw_ref).strip()
        if not u or u.startswith(("data:", "#")):
            return None
        if u in asset_map:
            return asset_map[u]
        if u.startswith("//"):
            https_u = "https:" + u
            if https_u in asset_map:
                return asset_map[https_u]
        norm = _normalize_fetch_url(u, base_url=css_remote_url)
        if norm and norm in asset_map:
            return asset_map[norm]
        return None

    return _rewrite_css_urls_with_lookup(css_text, _lookup_css_asset)


_VENDOR_SKIP_DIRS = frozenset(
    {"node_modules", ".git", ".next", ".nuxt", "dist", "build", "out", ".cache", ".astro", "_site", ".venv", "__pycache__"}
)
_REJECTED_HTML_CTYPES = frozenset({"text/html", "application/xhtml+xml"})


def vendor_assets(
    *,
    source_dir: str | Path,
    output_dir: str | Path,
    allow_hosts: Iterable[str] = (),
    origin_url: str | None = None,
    asset_map_path: str | Path | None = None,
    allow_http: bool = False,
    dry_run: bool = False,
    overwrite: bool = False,
    max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_css_depth: int = 4,
    concurrency: int = 6,
    resolver: Callable[[str, int], list[str]] | None = None,
    transport: Callable[[str, list[str], dict[str, str], float], tuple[int, dict[str, str], bytes]] | None = None,
) -> dict[str, Any]:
    """Download allowlisted remote assets from HTML (Pass 1) and CSS @import/url() (Pass 2) into public/ + asset-map.json."""
    raw_src = Path(source_dir).expanduser()
    reject_symlink_chain(raw_src)
    src = raw_src.resolve()
    if not src.exists():
        raise FileNotFoundError(f"Source directory does not exist: {src}")

    raw_out = Path(output_dir).expanduser()
    reject_symlink_chain(raw_out)
    out = raw_out.resolve()
    reject_controlled_folder_access(out)

    resolved_map_target: Path
    if asset_map_path:
        raw_map = Path(asset_map_path).expanduser()
        reject_symlink_chain(raw_map)
        resolved_map_target = raw_map.resolve()
        reject_controlled_folder_access(resolved_map_target)
    else:
        resolved_map_target = out / "asset-map.json"

    # Discover capture manifest if present to infer page base URLs
    manifest_origin = origin_url
    page_url_by_file: dict[str, str] = {}
    for candidate_manifest in (src / "manifest.json", src / "raw-capture" / "manifest.json"):
        if candidate_manifest.is_file() and not is_reparse_or_symlink(candidate_manifest):
            try:
                mdata = json.loads(safe_read_bytes(candidate_manifest, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8"))
                if mdata.get("schema") == "legends.astro-raw-capture/v1":
                    manifest_origin = manifest_origin or mdata.get("originUrl")
                    for p in mdata.get("pages", []):
                        if p.get("file") and p.get("finalUrl"):
                            page_url_by_file[p["file"]] = p["finalUrl"]
            except Exception:
                pass

    origin_host = normalize_host(manifest_origin) if manifest_origin else None
    allow_list: list[str] = []
    for h in allow_hosts:
        nh = normalize_host(h) if not h.strip().startswith("*.") else h.strip().lower()
        if nh and nh not in allow_list:
            allow_list.append(nh)
    if not allow_list:
        raise ValueError(
            "vendor-assets requires explicit --allow-host <domain> (repeatable for origin and CDN hosts)."
        )

    # Collect HTML files using pruned os.walk + reparse/symlink rejection
    html_files: list[Path] = []
    if src.is_file():
        html_files = [src]
    else:
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            cur = Path(dirpath)
            kept_dirs: list[str] = []
            for d in sorted(dirnames):
                if d in _VENDOR_SKIP_DIRS:
                    continue
                cand_d = cur / d
                if is_reparse_or_symlink(cand_d):
                    raise ValueError(f"Refusing symlink or junction in source directory: {cand_d}")
                assert_path_within(src, cand_d)
                kept_dirs.append(d)
            dirnames[:] = kept_dirs
            for fname in sorted(filenames):
                p = cur / fname
                if is_reparse_or_symlink(p):
                    raise ValueError(f"Refusing symlink or junction in source directory: {p}")
                assert_path_within(src, p)
                if p.is_file() and p.suffix.lower() in (".html", ".htm"):
                    html_files.append(p)

    # Pass 1: extract candidate URLs from all HTML files
    candidate_urls: list[str] = []
    seen_candidates: set[str] = set()
    raw_root_rel_by_norm: dict[str, list[str]] = {}
    skipped_hosts: dict[str, int] = {}

    for hf in html_files:
        rel_posix = hf.relative_to(src).as_posix() if src.is_dir() else hf.name
        base_for_page = page_url_by_file.get(rel_posix) or manifest_origin
        html_text = safe_read_bytes(hf, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8", errors="replace")
        for raw_u in extract_html_asset_urls(html_text):
            norm_u = _normalize_fetch_url(raw_u, base_url=base_for_page)
            if not norm_u:
                continue
            host = normalize_host(norm_u)
            if not host_matches_allowlist(host, allow_list):
                skipped_hosts[host] = skipped_hosts.get(host, 0) + 1
                continue
            if raw_u.startswith("/") and not raw_u.startswith("//"):
                rel_variants = raw_root_rel_by_norm.setdefault(norm_u, [])
                if raw_u not in rel_variants:
                    rel_variants.append(raw_u)
            if norm_u not in seen_candidates:
                seen_candidates.add(norm_u)
                candidate_urls.append(norm_u)

    if dry_run:
        used_preview: set[str] = set()
        planned = [
            {
                "remoteUrl": u,
                "localPublicPath": _local_public_path_for(u, origin_host=origin_host, used_paths=used_preview),
            }
            for u in candidate_urls
        ]
        return {
            "ok": True,
            "dryRun": True,
            "sourceDir": str(src),
            "outputDir": str(out),
            "assetMapPath": str(resolved_map_target),
            "allowHosts": allow_list,
            "htmlFilesScanned": len(html_files),
            "plannedAssetCount": len(planned),
            "plannedAssets": planned,
            "skippedHosts": skipped_hosts,
        }

    # Execute Pass 1 + Pass 2 (recursive CSS @import and url()) with bounded sliding-window concurrency
    asset_map: dict[str, str] = {}
    root_relative_map: dict[str, str] = {}
    final_url_to_local: dict[str, str] = {}
    used_local_paths: set[str] = set()
    downloaded_records: list[dict[str, Any]] = []
    record_by_local: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    css_files_to_rewrite: list[tuple[Path, str, str]] = []  # (local_file_path, remote_final_url, local_public_path)
    total_bytes = 0
    processed_urls: set[str] = set()
    state_lock = threading.Lock()
    public_root = out / "public"
    max_workers = max(1, min(int(concurrency), 16))
    max_in_flight = max_workers * 2

    def _record_root_rel_variants(remote_u: str, local_pub: str) -> None:
        for raw_rel in raw_root_rel_by_norm.get(remote_u, ()):
            if raw_rel != local_pub:
                root_relative_map.setdefault(raw_rel, local_pub)
        parsed_r = urllib.parse.urlsplit(remote_u)
        if origin_host and normalize_host(parsed_r.hostname or "") == origin_host:
            rel_key = (parsed_r.path or "/") + (f"?{parsed_r.query}" if parsed_r.query else "")
            if rel_key != local_pub:
                root_relative_map.setdefault(rel_key, local_pub)

    def _fetch_one(remote_url: str, css_depth: int, discovered_from: str) -> tuple[str, int, str, Any, str | None]:
        try:
            res = safe_fetch_bytes(
                remote_url,
                allowed_hosts=allow_list,
                allow_http=allow_http,
                max_bytes=max_asset_bytes,
                resolver=resolver,
                transport=transport,
            )
            return remote_url, css_depth, discovered_from, res, None
        except Exception as exc:
            return remote_url, css_depth, discovered_from, None, str(exc)

    current_wave: list[tuple[str, int, str]] = []
    for u in candidate_urls:
        if u not in processed_urls:
            processed_urls.add(u)
            current_wave.append((u, 0, "html"))

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    aborted = False
    try:
        while current_wave:
            next_wave: list[tuple[str, int, str]] = []
            wave_iter = iter(current_wave)
            pending: set[concurrent.futures.Future[Any]] = set()

            def _refill() -> None:
                while len(pending) < max_in_flight:
                    item = next(wave_iter, None)
                    if item is None:
                        break
                    pending.add(executor.submit(_fetch_one, item[0], item[1], item[2]))

            _refill()
            while pending:
                done, pending = concurrent.futures.wait(
                    pending,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    remote_url, css_depth, discovered_from, res, err_msg = fut.result()
                    if err_msg is not None or res is None:
                        with state_lock:
                            errors.append({"url": remote_url, "error": err_msg or "fetch failed"})
                        continue

                    if res.status != 200:
                        with state_lock:
                            errors.append({"url": remote_url, "error": f"HTTP {res.status}"})
                        continue

                    ctype_main = (res.content_type or "").split(";", 1)[0].strip().lower()
                    if ctype_main in _REJECTED_HTML_CTYPES:
                        with state_lock:
                            errors.append(
                                {
                                    "url": remote_url,
                                    "reason": "skipped_html_response",
                                    "error": f"Skipped HTML response ({res.content_type}) for asset URL (finalUrl: {res.final_url})",
                                }
                            )
                        continue

                    with state_lock:
                        existing_local = final_url_to_local.get(res.final_url)
                        if existing_local is not None:
                            asset_map[remote_url] = existing_local
                            _record_root_rel_variants(remote_url, existing_local)
                            continue

                        total_bytes += len(res.body)
                        if total_bytes > max_total_bytes:
                            raise ValueError(f"vendor-assets exceeded --max-total-bytes ({max_total_bytes})")

                        local_pub = _local_public_path_for(
                            res.final_url,
                            content_type=res.content_type,
                            origin_host=origin_host,
                            used_paths=used_local_paths,
                        )
                        dest_file = public_root / local_pub.lstrip("/")
                        assert_path_within(public_root, dest_file)
                        safe_write_bytes(dest_file, res.body, overwrite=overwrite)

                        asset_map[remote_url] = local_pub
                        asset_map[res.final_url] = local_pub
                        final_url_to_local[res.final_url] = local_pub
                        _record_root_rel_variants(remote_url, local_pub)
                        _record_root_rel_variants(res.final_url, local_pub)

                        is_css = local_pub.lower().endswith(".css") or "text/css" in res.content_type.lower()
                        rec = {
                            "remoteUrl": remote_url,
                            "finalUrl": res.final_url,
                            "localPublicPath": local_pub,
                            "bytes": len(res.body),
                            "contentType": res.content_type,
                            "discoveredFrom": discovered_from,
                            "sha256": hashlib.sha256(res.body).hexdigest(),
                        }
                        downloaded_records.append(rec)
                        record_by_local[local_pub] = rec

                        # Pass 2: crawl CSS @import and url(...)
                        if is_css and css_depth < max_css_depth:
                            css_files_to_rewrite.append((dest_file, res.final_url, local_pub))
                            css_text = res.body.decode("utf-8", errors="replace")
                            for sub_raw in extract_css_urls(css_text):
                                sub_url = _normalize_fetch_url(sub_raw, base_url=res.final_url)
                                if not sub_url or sub_url in processed_urls:
                                    continue
                                sub_host = normalize_host(sub_url)
                                if not host_matches_allowlist(sub_host, allow_list):
                                    skipped_hosts[sub_host] = skipped_hosts.get(sub_host, 0) + 1
                                    continue
                                processed_urls.add(sub_url)
                                next_wave.append((sub_url, css_depth + 1, f"css:{local_pub}"))
                # Discard references to completed futures immediately before refilling window
                done.clear()
                _refill()

            current_wave = next_wave
    except Exception:
        aborted = True
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    finally:
        if not aborted:
            executor.shutdown(wait=True, cancel_futures=False)

    # Rewrite downloaded CSS files so url(...) and @import resolve locally and refresh stored digests/sizes
    rewritten_css: list[str] = []
    for css_path, css_remote, local_pub in css_files_to_rewrite:
        original_css = safe_read_bytes(css_path, max_bytes=max_asset_bytes).decode("utf-8", errors="replace")
        updated_css = rewrite_css_assets(original_css, asset_map, css_remote_url=css_remote)
        if updated_css != original_css:
            safe_write_text(css_path, updated_css, overwrite=True)
            updated_bytes = updated_css.encode("utf-8")
            rec = record_by_local.get(local_pub)
            if rec is not None:
                total_bytes += len(updated_bytes) - int(rec["bytes"])
                rec["bytes"] = len(updated_bytes)
                rec["sha256"] = hashlib.sha256(updated_bytes).hexdigest()
            rewritten_css.append("/" + css_path.relative_to(public_root).as_posix())

    astro_font_candidates = [
        r["localPublicPath"]
        for r in downloaded_records
        if Path(r["localPublicPath"]).suffix.lower() in _FONT_EXTS
    ]
    astro_image_candidates = [
        r["localPublicPath"]
        for r in downloaded_records
        if Path(r["localPublicPath"]).suffix.lower() in _IMAGE_EXTS
    ]

    map_payload: dict[str, Any] = {
        "schema": "legends.astro-asset-map/v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceDir": str(src),
        "outputDir": str(out),
        "originUrl": manifest_origin,
        "originHost": origin_host,
        "allowHosts": allow_list,
        "assetCount": len(downloaded_records),
        "totalBytes": total_bytes,
        "map": asset_map,
        "rootRelativeMap": root_relative_map,
        "assets": downloaded_records,
        "rewrittenCssFiles": rewritten_css,
        "skippedHosts": skipped_hosts,
        "errors": errors,
        "astroOptimizationCandidates": {
            "fonts": astro_font_candidates,
            "images": astro_image_candidates,
        },
    }
    map_path = safe_write_text(
        resolved_map_target,
        json.dumps(map_payload, indent=2) + "\n",
        overwrite=overwrite,
    )

    return {
        "ok": True,
        "dryRun": False,
        "assetMapPath": str(map_path),
        "assetCount": len(downloaded_records),
        "rewrittenCssCount": len(rewritten_css),
        "totalBytes": total_bytes,
        "skippedHosts": skipped_hosts,
        "errorCount": len(errors),
        "errors": errors,
    }
