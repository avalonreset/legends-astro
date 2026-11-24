"""Live HTTP/sitemap/BFS capture and Firecrawl archive normalizer with Migration Triage reporting."""

from __future__ import annotations

import collections
import hashlib
import html
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

from .net_safety import (
    DEFAULT_MAX_PAGE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    assert_path_within,
    canonicalize_route,
    format_sanitized_origin,
    host_matches_allowlist,
    is_reparse_or_symlink,
    normalize_host,
    reject_controlled_folder_access,
    reject_symlink_chain,
    safe_fetch_bytes,
    safe_parse_xml_bytes,
    safe_read_bytes,
    safe_write_text,
    validate_outbound_url,
)

ASSET_EXT_RE = re.compile(
    r"\.(?:css|js|mjs|cjs|map|png|jpe?g|gif|webp|avif|svg|ico|bmp|tiff?|"
    r"woff2?|ttf|eot|otf|mp4|webm|mov|avi|mp3|wav|ogg|m4a|pdf|zip|gz|tar|tgz|rar|7z|"
    r"xml|json|txt|csv|rss|atom|doc|docx|xls|xlsx|ppt|pptx)$",
    re.IGNORECASE,
)

_SHORTCODE_RE = re.compile(r"\[/?([a-z][a-z0-9_-]{1,32})(\s+[^\]\r\n]{0,160})?\]")
_SHORTCODE_ATTR_RE = re.compile(r"""\b[a-z][a-z0-9_-]*\s*=\s*(?:"[^"]*"|'[^']*'|[^\s\]]+)""", re.IGNORECASE)
_HTML_TAG_ATTRS_RE = re.compile(r"<(html|body)\b([^>]*)>", re.IGNORECASE)
_ATTR_PAIR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?"""
)
_CSS_URL_HOST_RE = re.compile(
    r"""url\(\s*(?:&quot;|&#34;|["'])?(https?://(?:(?!(?:&quot;|&#34;|["'()\s])).){1,4096})""",
    re.IGNORECASE,
)
_CSS_IMPORT_HOST_RE = re.compile(r"""@import\s+(?:url\(\s*)?["']?(https?://[^"'\s;)]+)""", re.IGNORECASE)

_TRACKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google-tag-manager", re.compile(r"(?i:googletagmanager\.com/gtm\.js)|(?-i:\bGTM-[A-Z0-9]{4,10}\b)")),
    (
        "google-analytics",
        re.compile(
            r"(?i:googletagmanager\.com/gtag/js|google-analytics\.com)"
            r"|(?-i:\bG-(?=[A-Z0-9]*\d)[A-Z0-9]{7,12}\b)"
            r"|(?-i:\bUA-\d{4,10}-\d{1,4}\b)"
        ),
    ),
    ("hotjar", re.compile(r"static\.hotjar\.com|hjid\s*:", re.IGNORECASE)),
    ("wordfence", re.compile(r"wordfence|wfls-|wflogs", re.IGNORECASE)),
    ("meta-pixel", re.compile(r"""connect\.facebook\.net/[^\s"'<>)]{0,256}/fbevents\.js|fbq\s*\(""", re.IGNORECASE)),
    ("clarity", re.compile(r"clarity\.ms/tag/", re.IGNORECASE)),
    ("hubspot", re.compile(r"js\.hs-scripts\.com|hsforms\.net", re.IGNORECASE)),
    ("cloudflare-beacon", re.compile(r"static\.cloudflareinsights\.com/beacon", re.IGNORECASE)),
)

_BUILDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("elementor", re.compile(r"elementor-|/plugins/elementor/", re.IGNORECASE)),
    ("divi", re.compile(r"\bet_pb_|/themes/Divi/", re.IGNORECASE)),
    ("wpbakery", re.compile(r"\bvc_row\b|\bwpb_wrapper\b|js_composer", re.IGNORECASE)),
    ("bricks", re.compile(r"\bbrxe-|/themes/bricks/", re.IGNORECASE)),
    ("oxygen", re.compile(r"\bct-section\b|/plugins/oxygen/", re.IGNORECASE)),
    ("beaver-builder", re.compile(r"\bfl-builder\b|\bfl-row\b", re.IGNORECASE)),
    ("gutenberg", re.compile(r"\bwp-block-|<!--\s*wp:", re.IGNORECASE)),
)

_FORM_PLUGIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("contact-form-7", re.compile(r"\bwpcf7\b|contact-form-7", re.IGNORECASE)),
    ("gravity-forms", re.compile(r"\bgform_wrapper\b|gravityforms", re.IGNORECASE)),
    ("wpforms", re.compile(r"\bwpforms-container\b|wpforms", re.IGNORECASE)),
    ("webflow-form", re.compile(r"\bw-form\b|data-wf-page-id", re.IGNORECASE)),
    ("turnstile", re.compile(r"challenges\.cloudflare\.com/turnstile|cf-turnstile|data-turnstile-sitekey", re.IGNORECASE)),
    ("recaptcha", re.compile(r"google\.com/recaptcha|g-recaptcha|grecaptcha", re.IGNORECASE)),
)

_NON_SHORTCODE_NAMES = frozenset(
    {
        "if",
        "endif",
        "cdata",
        "object",
        "array",
        "email",
        "protected",
        "slug",
        "id",
        "image",
        "img",
        "link",
        "url",
        "path",
        "param",
        "params",
        "query",
        "name",
        "value",
        "key",
        "type",
        "index",
        "page",
        "post",
        "item",
        "note",
        "todo",
        "true",
        "false",
        "null",
        "undefined",
    }
)

_KNOWN_WP_SHORTCODES = frozenset(
    {
        "contact-form-7",
        "contact-form",
        "gravityform",
        "gravityforms",
        "wpforms",
        "ninja_form",
        "ninja_forms",
        "formidable",
        "gallery",
        "caption",
        "embed",
        "audio",
        "video",
        "playlist",
        "woocommerce_cart",
        "woocommerce_checkout",
        "woocommerce_my_account",
        "woocommerce_order_tracking",
        "product",
        "products",
        "product_page",
        "add_to_cart",
        "rev_slider",
        "elementor-template",
        "mc4wp_form",
        "wpcf7",
        "cf7",
    }
)

_KNOWN_WP_SHORTCODE_PREFIXES = (
    "vc_",
    "et_pb_",
    "fusion_",
    "av_",
    "su_",
    "wp_",
    "wpforms",
    "wpcf7",
    "fl_builder",
    "elementor",
    "woocommerce",
    "gravityform",
    "rev_slider",
)


def _is_known_wp_shortcode(name: str) -> bool:
    return name in _KNOWN_WP_SHORTCODES or name.startswith(_KNOWN_WP_SHORTCODE_PREFIXES)


def extract_shortcodes(visible_text: str, *, is_wordpress: bool = False) -> list[str]:
    """Extract likely WordPress shortcodes from visible text while avoiding non-WP route/doc placeholders."""
    closing_tags = {
        m.group(1).lower()
        for m in re.finditer(r"\[/([a-z][a-z0-9_-]{1,32})\]", visible_text)
    }
    found: set[str] = set()
    for m in _SHORTCODE_RE.finditer(visible_text):
        full = m.group(0)
        sc_name = m.group(1).lower()
        attr_part = m.group(2) or ""
        if sc_name in _NON_SHORTCODE_NAMES:
            continue
        if full.startswith("[/"):
            # Closing tag itself only counts if known or matched (handled by opening tag)
            if _is_known_wp_shortcode(sc_name):
                found.add(f"[{sc_name}]")
            continue
        has_attrs = bool(attr_part and _SHORTCODE_ATTR_RE.search(attr_part))
        has_closing = sc_name in closing_tags
        if _is_known_wp_shortcode(sc_name) or has_attrs or has_closing:
            found.add(f"[{sc_name}]")
        elif is_wordpress and ("-" in sc_name or "_" in sc_name):
            found.add(f"[{sc_name}]")
    return sorted(found)


_WAF_STATUS_CODES = frozenset({403, 429, 503})
_CF_HARD_CHALLENGE_RE = re.compile(
    r"just a moment\.\.\.|_cf_chl_opt|cf-browser-verification|attention required!\s*\|\s*cloudflare",
    re.IGNORECASE,
)
_CF_PASSIVE_PLATFORM_RE = re.compile(r"/cdn-cgi/challenge-platform/", re.IGNORECASE)
_WAF_BODY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cloudflare_challenge", re.compile(r"just a moment\.\.\.|/cdn-cgi/challenge-platform/|_cf_chl_opt|cf-browser-verification|attention required!\s*\|\s*cloudflare", re.IGNORECASE)),
    ("cloudflare_turnstile", re.compile(r"challenges\.cloudflare\.com/turnstile|cf-turnstile", re.IGNORECASE)),
    ("datadome_challenge", re.compile(r"geo\.captcha-delivery\.com|\bdatadome\b", re.IGNORECASE)),
    ("perimeterx_challenge", re.compile(r"\bpx-captcha\b|_pxAppId|perimeterx", re.IGNORECASE)),
    ("akamai_waf", re.compile(r"access denied[\s\S]{0,400}(?:akamai|edgesuite|reference\s*#)", re.IGNORECASE)),
    ("incapsula_waf", re.compile(r"_Incapsula_Resource|incapsula incident id", re.IGNORECASE)),
)
_SPA_ROOT_RE = re.compile(
    r"""<div\b[^>]*\bid\s*=\s*["'](root|__next|app|__nuxt|___gatsby)["'][^>]*>\s*</div>""",
    re.IGNORECASE,
)
_BUNDLE_SCRIPT_RE = re.compile(
    r"""<script\b[^>]*(?:\bsrc\s*=\s*["'][^"']+["']|\btype\s*=\s*["']module["'])""",
    re.IGNORECASE,
)


def detect_waf_challenge(status: int, headers: dict[str, str], html_text: str) -> list[str]:
    """Detect WAF, bot-protection, or JS challenge responses from HTTP status, headers, and body."""
    reasons: list[str] = []
    lower_headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if "cf-mitigated" in lower_headers:
        reasons.append(f"cf-mitigated:{lower_headers['cf-mitigated']}")
    if any(k.startswith(("x-datadome", "x-px-")) for k in lower_headers):
        reasons.append("bot-protection-header")
    if status in _WAF_STATUS_CODES:
        reasons.append(f"http_{status}")

    visible_len = len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _strip_script_style_blocks(html_text))).strip())
    for label, pat in _WAF_BODY_PATTERNS:
        if pat.search(html_text):
            # Avoid false positives on normal pages with an embedded Turnstile form widget
            if label == "cloudflare_turnstile" and status < 400 and visible_len >= 250 and "<form" in html_text.lower():
                continue
            # Avoid false positives on 200 OK full pages with Cloudflare's passive /cdn-cgi/challenge-platform/ JS beacon
            if (
                label == "cloudflare_challenge"
                and status < 400
                and visible_len >= 400
                and "cf-mitigated" not in lower_headers
                and _CF_PASSIVE_PLATFORM_RE.search(html_text)
                and not _CF_HARD_CHALLENGE_RE.search(html_text)
            ):
                continue
            reasons.append(label)
    return reasons


class _HeadBodySlicer(HTMLParser):
    """Linear-time O(N) HTML <head>/<body> inner slicer immune to script/style/comment </body> strings and ReDoS."""

    def __init__(self, html_text: str) -> None:
        super().__init__(convert_charrefs=False)
        self._text = html_text
        offsets = [0]
        pos = 0
        for line in html_text.split("\n")[:-1]:
            pos += len(line) + 1
            offsets.append(pos)
        self._line_offsets = offsets
        self.head_start: int | None = None
        self.head_end: int | None = None
        self.body_start: int | None = None
        self.body_end: int | None = None
        self.html_end: int | None = None

    def _offset(self) -> int:
        lineno, col = self.getpos()
        idx = lineno - 1
        if 0 <= idx < len(self._line_offsets):
            return self._line_offsets[idx] + col
        return len(self._text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        start_text = self.get_starttag_text() or ""
        if t == "head" and self.head_start is None:
            self.head_start = self._offset() + len(start_text)
        elif t == "body" and self.body_start is None:
            if self.head_start is not None and self.head_end is None:
                self.head_end = self._offset()
            self.body_start = self._offset() + len(start_text)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "head" and self.head_start is not None and self.head_end is None:
            self.head_end = self._offset()
        elif t == "body" and self.body_start is not None:
            self.body_end = self._offset()
        elif t == "html":
            self.html_end = self._offset()


def extract_head_and_body(html_text: str) -> tuple[str, str]:
    """Slice <head> and <body> inner HTML in linear time without truncating on script/comment </body> strings."""
    slicer = _HeadBodySlicer(html_text)
    slicer.feed(html_text)
    slicer.close()
    if slicer.head_start is None and slicer.body_start is None:
        return "", html_text.strip()
    head_out = ""
    if slicer.head_start is not None:
        h_end = slicer.head_end if slicer.head_end is not None else len(html_text)
        head_out = html_text[slicer.head_start : h_end].strip()
    body_out = ""
    if slicer.body_start is not None:
        b_end = slicer.body_end
        if b_end is None:
            b_end = slicer.html_end if (slicer.html_end is not None and slicer.html_end >= slicer.body_start) else len(html_text)
        body_out = html_text[slicer.body_start : b_end].strip()
    return head_out, body_out


def detect_empty_spa_shell(html_text: str) -> str | None:
    """Detect empty client-rendered JS SPA shells (<div id='root'></div> etc.) with minimal visible text."""
    root_match = _SPA_ROOT_RE.search(html_text)
    if not root_match:
        return None
    scripts = _BUNDLE_SCRIPT_RE.findall(html_text)
    if not scripts:
        return None
    _, body_fragment = extract_head_and_body(html_text)
    if not body_fragment:
        body_fragment = html_text
    cleaned = _strip_script_style_blocks(body_fragment)
    cleaned = re.sub(r"<noscript\b[^>]*>.*?</noscript>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
    visible_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cleaned)).strip()
    if len(visible_text) < 120:
        mount_id = root_match.group(1)
        return f"Empty JS SPA shell (#{mount_id}) with {len(visible_text)} visible chars and {len(scripts)} script bundle(s)"
    return None


def parse_tag_attrs(raw_attrs: str) -> dict[str, str]:
    """Parse HTML attribute string into an ordered dict of attribute names -> values."""
    out: dict[str, str] = {}
    for m in _ATTR_PAIR_RE.finditer(raw_attrs or ""):
        name = m.group(1)
        val = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else (m.group(4) or ""))
        out[name] = html.unescape(val)
    return out


def extract_root_attrs(html_text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Extract <html> and <body> attributes from an HTML document."""
    html_attrs: dict[str, str] = {}
    body_attrs: dict[str, str] = {}
    for m in _HTML_TAG_ATTRS_RE.finditer(html_text):
        tag = m.group(1).lower()
        if tag == "html" and not html_attrs:
            html_attrs = parse_tag_attrs(m.group(2))
        elif tag == "body" and not body_attrs:
            body_attrs = parse_tag_attrs(m.group(2))
    return html_attrs, body_attrs


def route_to_capture_slug(route: str, used: set[str] | None = None) -> str:
    """Map a URL route to a collision-safe '__'-delimited capture slug."""
    clean = route.split("#", 1)[0].split("?", 1)[0].strip()
    if not clean or clean == "/" or clean == "/index.html":
        base = "index"
    else:
        trimmed = clean.strip("/")
        if trimmed.lower().endswith(".html"):
            trimmed = trimmed[:-5]
        elif trimmed.lower().endswith(".htm"):
            trimmed = trimmed[:-4]
        parts = [
            re.sub(r"[^A-Za-z0-9_-]+", "-", urllib.parse.unquote(seg)).strip("-") or "seg"
            for seg in trimmed.split("/")
            if seg
        ]
        base = "__".join(parts) if parts else "index"

    if used is None:
        return base
    slug = base
    counter = 2
    while slug in used:
        slug = f"{base}__{counter}"
        counter += 1
    used.add(slug)
    return slug


def normalize_route_and_shape(path_str: str) -> tuple[str, str]:
    """Normalize a URL path and infer its urlShape ('root', 'directory', 'file', 'clean')."""
    p = canonicalize_route(path_str or "/")
    if p in ("/", "/index.html", "/index.htm"):
        return "/", "root"
    if p.endswith("/index.html"):
        return p[: -len("index.html")], "directory"
    if p.endswith("/"):
        return p, "directory"
    if p.lower().endswith((".html", ".htm")):
        return p, "file"
    return p, "clean"


def should_skip_route(url_path: str) -> bool:
    """Return True for asset extensions, /cdn-cgi/, or wp-admin/feed paths that should not be captured as HTML pages."""
    lower = url_path.lower()
    if "/cdn-cgi/" in lower or lower.startswith("/cdn-cgi"):
        return True
    if ASSET_EXT_RE.search(lower):
        return True
    return False


def _strip_script_style_blocks(html_text: str) -> str:
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<!--.*?-->", " ", cleaned, flags=re.DOTALL)
    return cleaned


class _ExternalAssetHostExtractor(HTMLParser):
    """Extract external asset/embed hosts (excluding <a href> navigation links and <script> JS bodies)."""

    _ASSET_LINK_RELS = frozenset(
        {"stylesheet", "icon", "shortcut", "apple-touch-icon", "preload", "modulepreload", "prefetch", "manifest"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hosts: set[str] = set()
        self._in_style = False
        self._style_buf: list[str] = []

    def _add_url(self, raw: str | None) -> None:
        if not raw:
            return
        u = raw.strip()
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith(("http://", "https://")):
            return
        h = normalize_host(u)
        if h:
            self.hosts.add(h)

    def _scan_css(self, css_text: str) -> None:
        for rx in (_CSS_URL_HOST_RE, _CSS_IMPORT_HOST_RE):
            for m in rx.finditer(css_text):
                self._add_url(m.group(1))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "style":
            self._in_style = True
            self._style_buf = []
        elif t == "script":
            self._add_url(attr_map.get("src"))
        elif t == "link":
            rels = set(attr_map.get("rel", "").lower().split())
            if rels & self._ASSET_LINK_RELS:
                self._add_url(attr_map.get("href"))
        elif t in ("img", "video", "audio", "source", "iframe", "embed", "track"):
            self._add_url(attr_map.get("src"))
            self._add_url(attr_map.get("poster"))
        elif t == "meta":
            prop = attr_map.get("property", "").lower()
            name = attr_map.get("name", "").lower()
            if prop in ("og:image", "og:image:url", "og:image:secure_url") or name in ("twitter:image", "twitter:image:src"):
                self._add_url(attr_map.get("content"))

        for srcset_attr in ("srcset", "data-srcset"):
            val = attr_map.get(srcset_attr)
            if val:
                for part in re.split(r",\s+(?=(?:https?:|//|/|[^\s,]+))", val.strip()):
                    tokens = part.strip().split()
                    if tokens:
                        self._add_url(tokens[0].rstrip(","))
        if attr_map.get("data-src"):
            self._add_url(attr_map["data-src"])
        if attr_map.get("data-poster-url"):
            self._add_url(attr_map["data-poster-url"])
        if attr_map.get("data-video-urls"):
            for item in attr_map["data-video-urls"].split(","):
                self._add_url(item)
        if attr_map.get("style"):
            self._scan_css(attr_map["style"])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style" and self._in_style:
            self._in_style = False
            self._scan_css("".join(self._style_buf))
            self._style_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._style_buf.append(data)


def _extract_external_asset_hosts(html_text: str) -> set[str]:
    p = _ExternalAssetHostExtractor()
    p.feed(html_text)
    return p.hosts


def analyze_migration_triage(
    pages_payload: list[dict[str, Any]],
    *,
    origin_host: str | None = None,
) -> dict[str, Any]:
    """Produce a deterministic Migration Triage Report from captured or audited HTML pages."""
    wp_signals = 0
    wf_signals = 0
    builders: set[str] = set()
    shortcodes: set[str] = set()
    form_plugins: set[str] = set()
    form_count = 0
    ecommerce: set[str] = set()
    cf_email_count = 0
    trackers: set[str] = set()
    external_cdns: set[str] = set()
    wf_domain: str | None = None
    wf_site: str | None = None
    wf_pages: dict[str, str] = {}
    visible_texts: list[str] = []

    norm_origin = normalize_host(origin_host) if origin_host else ""

    for page in pages_payload:
        raw_html = str(page.get("html") or "")
        route = str(page.get("route") or "/")
        if not raw_html:
            continue

        html_attrs, _body_attrs = extract_root_attrs(raw_html)
        if html_attrs.get("data-wf-domain"):
            wf_domain = html_attrs["data-wf-domain"]
            wf_signals += 2
        if html_attrs.get("data-wf-site"):
            wf_site = html_attrs["data-wf-site"]
            wf_signals += 2
        if html_attrs.get("data-wf-page"):
            wf_pages[route] = html_attrs["data-wf-page"]
            wf_signals += 2

        if re.search(r"/wp-content/|/wp-includes/|wp-json|content=[\"']WordPress", raw_html, re.IGNORECASE):
            wp_signals += 2
        if re.search(r"website-files\.com|uploads-ssl\.webflow\.com|webflow\.js", raw_html, re.IGNORECASE):
            wf_signals += 1

        for name, pat in _BUILDER_PATTERNS:
            if pat.search(raw_html):
                builders.add(name)
                if name in ("elementor", "divi", "wpbakery", "bricks", "oxygen", "beaver-builder", "gutenberg"):
                    wp_signals += 1

        visible_html = _strip_script_style_blocks(raw_html)
        text_only = re.sub(r"<[^>]+>", " ", visible_html)
        visible_texts.append(text_only)

        form_count += len(re.findall(r"<form\b", visible_html, re.IGNORECASE))
        for name, pat in _FORM_PLUGIN_PATTERNS:
            if pat.search(raw_html):
                form_plugins.add(name)

        if re.search(r"woocommerce|wc-block-|add-to-cart=", raw_html, re.IGNORECASE):
            ecommerce.add("woocommerce")
            wp_signals += 1
        if re.search(r"cdn\.shopify\.com|shopify-section", raw_html, re.IGNORECASE):
            ecommerce.add("shopify")
        if re.search(r"js\.stripe\.com|checkout\.stripe\.com", raw_html, re.IGNORECASE):
            ecommerce.add("stripe")

        cf_email_count += len(re.findall(r"/cdn-cgi/l/email-protection|data-cfemail\s*=", raw_html, re.IGNORECASE))

        for name, pat in _TRACKER_PATTERNS:
            if pat.search(raw_html):
                trackers.add(name)

        for h in _extract_external_asset_hosts(raw_html):
            if h and h != norm_origin:
                external_cdns.add(h)

    if wf_signals > wp_signals and wf_signals >= 2:
        platform = "webflow"
    elif wp_signals >= 2:
        platform = "wordpress"
    else:
        platform = "html"

    is_wp = platform == "wordpress"
    for text_only in visible_texts:
        shortcodes.update(extract_shortcodes(text_only, is_wordpress=is_wp))

    return {
        "detectedPlatform": platform,
        "pageBuilders": sorted(builders),
        "shortcodes": sorted(shortcodes),
        "forms": {
            "formTagCount": form_count,
            "detectedSystems": sorted(form_plugins),
        },
        "ecommerce": sorted(ecommerce),
        "cloudflareEmailObfuscationCount": cf_email_count,
        "trackers": sorted(trackers),
        "externalCdns": sorted(external_cdns),
        "webflow": {
            "domain": wf_domain,
            "siteId": wf_site,
            "pageIdsByRoute": wf_pages,
        }
        if (wf_domain or wf_site or wf_pages)
        else None,
    }


class _LinkExtractor(HTMLParser):
    """Extract href links from <a> tags for bounded BFS crawling."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            for k, v in attrs:
                if k.lower() == "href" and v:
                    self.hrefs.append(v.strip())


def _extract_links(html_text: str) -> list[str]:
    p = _LinkExtractor()
    p.feed(html_text)
    return p.hrefs


def _decode_html_bytes(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([A-Za-z0-9_-]+)", content_type or "", re.IGNORECASE)
    if m:
        try:
            return body.decode(m.group(1), errors="replace")
        except LookupError:
            pass
    return body.decode("utf-8", errors="replace")


def _parse_sitemap_urls(
    sitemap_url: str,
    *,
    allowed_hosts: list[str],
    allow_http: bool,
    max_page_bytes: int,
    max_urls: int,
    max_depth: int = 4,
    max_sitemaps: int = 32,
    byte_budget: dict[str, int] | None = None,
    resolver: Callable[[str, int], list[str]] | None = None,
    transport: Callable[[str, list[str], dict[str, str], float], tuple[int, dict[str, str], bytes]] | None = None,
) -> tuple[list[str], list[str]]:
    """Recursively parse XML sitemaps / sitemap_index files with XXE refusal, SSRF checks, sitemap count cap, and total-byte accounting."""
    discovered: list[str] = []
    seen_sitemaps: set[str] = set()
    visited_sitemaps: list[str] = []
    queue: collections.deque[tuple[str, int]] = collections.deque([(sitemap_url, 0)])

    while queue and len(discovered) < max_urls and len(seen_sitemaps) < max_sitemaps:
        sm_url, depth = queue.popleft()
        if sm_url in seen_sitemaps or depth > max_depth:
            continue
        seen_sitemaps.add(sm_url)
        fetch_cap = max_page_bytes
        if byte_budget is not None:
            remaining = byte_budget["limit"] - byte_budget["used"]
            if remaining <= 0:
                raise ValueError(f"Capture exceeded --max-total-bytes ({byte_budget['limit']})")
            fetch_cap = min(max_page_bytes, remaining)
        try:
            res = safe_fetch_bytes(
                sm_url,
                allowed_hosts=allowed_hosts,
                allow_http=allow_http,
                max_bytes=fetch_cap,
                resolver=resolver,
                transport=transport,
            )
        except ValueError as exc:
            if byte_budget is not None and "exceeded max_bytes" in str(exc):
                raise ValueError(f"Capture exceeded --max-total-bytes ({byte_budget['limit']})") from exc
            continue
        except Exception:
            continue
        if byte_budget is not None:
            byte_budget["used"] += len(res.body)
            if byte_budget["used"] > byte_budget["limit"]:
                raise ValueError(f"Capture exceeded --max-total-bytes ({byte_budget['limit']})")
        if res.status != 200 or not res.body.strip():
            continue
        visited_sitemaps.append(res.final_url)
        try:
            root = safe_parse_xml_bytes(res.body, source_label=sm_url)
        except Exception:
            continue

        tag_local = root.tag.rsplit("}", 1)[-1].lower()
        if tag_local == "sitemapindex":
            for child in root:
                if child.tag.rsplit("}", 1)[-1].lower() == "sitemap":
                    for sub in child:
                        if sub.tag.rsplit("}", 1)[-1].lower() == "loc" and sub.text:
                            loc = sub.text.strip()
                            if host_matches_allowlist(loc, allowed_hosts):
                                queue.append((loc, depth + 1))
        elif tag_local == "urlset":
            for child in root:
                if child.tag.rsplit("}", 1)[-1].lower() == "url":
                    for sub in child:
                        if sub.tag.rsplit("}", 1)[-1].lower() == "loc" and sub.text:
                            loc = sub.text.strip()
                            parsed = urllib.parse.urlsplit(loc)
                            if host_matches_allowlist(loc, allowed_hosts) and not should_skip_route(parsed.path or "/"):
                                if loc not in discovered:
                                    discovered.append(loc)
                                    if len(discovered) >= max_urls:
                                        break
    return discovered, visited_sitemaps


_FIRECRAWL_SKIP_DIRS = frozenset(
    {"node_modules", ".git", ".next", ".nuxt", "dist", "build", "out", ".cache", ".astro", "_site", ".venv", "__pycache__"}
)


def _looks_like_firecrawl_record(rec: dict[str, Any]) -> bool:
    """Return True only when a dict has genuine Firecrawl page content keys (html/rawHtml/markdown)."""
    if any(isinstance(rec.get(k), str) and rec.get(k) for k in ("html", "rawHtml", "markdown")):
        return True
    meta = rec.get("metadata")
    if isinstance(meta, dict) and (meta.get("sourceURL") or meta.get("url")):
        if any(k in rec for k in ("html", "rawHtml", "markdown")):
            return True
    return False


def unwrap_firecrawl_records(data: Any, *, source_label: str = "") -> list[dict[str, Any]]:
    """Unwrap Firecrawl JSON envelopes: response.data, legends-firecrawl-evidence/v1, top-level data, or bare record."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict) and _looks_like_firecrawl_record(r)]
    if not isinstance(data, dict):
        return []

    # legends-firecrawl .raw.json envelope: {"metadata": ..., "response": {"data": [...] | {...}}}
    if isinstance(data.get("response"), dict):
        resp = data["response"]
        if isinstance(resp.get("data"), list):
            return [r for r in resp["data"] if isinstance(r, dict) and _looks_like_firecrawl_record(r)]
        if isinstance(resp.get("data"), dict):
            return [resp["data"]] if _looks_like_firecrawl_record(resp["data"]) else []
        if _looks_like_firecrawl_record(resp):
            return [resp]

    # legends-firecrawl-evidence/v1 envelope
    if str(data.get("schema", "")).startswith("legends-firecrawl-evidence/"):
        for key in ("records", "items", "pages", "documents", "evidence", "data"):
            val = data.get(key)
            if isinstance(val, list):
                out: list[dict[str, Any]] = []
                for item in val:
                    if isinstance(item, dict):
                        unwrapped = unwrap_firecrawl_records(item, source_label=source_label)
                        if unwrapped:
                            out.extend(unwrapped)
                        elif _looks_like_firecrawl_record(item):
                            out.append(item)
                if out:
                    return out
            elif isinstance(val, dict) and _looks_like_firecrawl_record(val):
                return [val]

    if isinstance(data.get("data"), list):
        return [r for r in data["data"] if isinstance(r, dict) and _looks_like_firecrawl_record(r)]
    if isinstance(data.get("data"), dict):
        return [data["data"]] if _looks_like_firecrawl_record(data["data"]) else []

    if _looks_like_firecrawl_record(data):
        return [data]

    return []


def collect_firecrawl_sources(
    source_path: str | Path,
    *,
    max_files: int | None = None,
    max_total_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Walk a Firecrawl JSON file or directory (including .raw.json, .json, .html) with optional file/byte bounds and deduplicate by canonical URL."""
    raw_src = Path(source_path).expanduser()
    reject_symlink_chain(raw_src)
    src = raw_src.resolve()
    if not src.exists():
        raise FileNotFoundError(f"Firecrawl source path does not exist: {src}")

    files: list[Path] = []
    if src.is_file():
        files = [src]
    else:
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            cur = Path(dirpath)
            kept_dirs: list[str] = []
            for d in sorted(dirnames):
                if d in _FIRECRAWL_SKIP_DIRS:
                    continue
                cand_d = cur / d
                if is_reparse_or_symlink(cand_d):
                    raise ValueError(f"Refusing symlink or junction in Firecrawl directory: {cand_d}")
                assert_path_within(src, cand_d)
                kept_dirs.append(d)
            dirnames[:] = kept_dirs
            for fname in sorted(filenames):
                p = cur / fname
                if is_reparse_or_symlink(p):
                    raise ValueError(f"Refusing symlink or junction in Firecrawl directory: {p}")
                assert_path_within(src, p)
                if not p.is_file():
                    continue
                if p.name.endswith(".manifest.json") or p.name == "manifest.json":
                    continue
                if p.suffix.lower() in (".json", ".html", ".htm"):
                    files.append(p)
                    if max_files is not None and len(files) >= max_files:
                        break
            if max_files is not None and len(files) >= max_files:
                break

    by_canonical: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    bytes_read = 0

    for f in files:
        if max_total_bytes is not None:
            remaining = max_total_bytes - bytes_read
            if remaining <= 0:
                raise ValueError(f"Firecrawl source reading exceeded --max-total-bytes ({max_total_bytes})")
            file_cap = min(DEFAULT_MAX_PAGE_BYTES, remaining)
        else:
            file_cap = DEFAULT_MAX_PAGE_BYTES
        raw_bytes = safe_read_bytes(f, max_bytes=file_cap)
        bytes_read += len(raw_bytes)
        if max_total_bytes is not None and bytes_read > max_total_bytes:
            raise ValueError(f"Firecrawl source reading exceeded --max-total-bytes ({max_total_bytes})")
        rel_name = f.relative_to(src).as_posix() if src.is_dir() else f.name
        if f.suffix.lower() in (".html", ".htm"):
            html_body = raw_bytes.decode("utf-8", errors="replace")
            if not html_body.strip():
                continue
            route, _shape = normalize_route_and_shape(rel_name)
            canon_key = route
            rec = {
                "url": route,
                "route": route,
                "html": html_body,
                "markdown": "",
                "metadata": {},
                "sourceFile": rel_name,
            }
            if canon_key not in by_canonical or len(html_body) > len(by_canonical[canon_key].get("html", "")):
                if canon_key not in by_canonical:
                    order.append(canon_key)
                by_canonical[canon_key] = rec
            continue

        try:
            parsed_json = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            continue
        for item in unwrap_firecrawl_records(parsed_json, source_label=rel_name):
            meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            source_url = str(
                meta.get("sourceURL")
                or meta.get("url")
                or item.get("sourceURL")
                or item.get("url")
                or "/"
            ).strip()
            parsed_u = urllib.parse.urlsplit(source_url)
            route, _shape = normalize_route_and_shape(parsed_u.path or "/")
            if should_skip_route(route):
                continue
            canon_key = f"{(parsed_u.hostname or '').lower()}{route}" if parsed_u.hostname else route
            html_body = str(item.get("rawHtml") or item.get("html") or "")
            md_body = str(item.get("markdown") or "")
            if not html_body.strip() and not md_body.strip():
                continue
            rec = {
                "url": source_url,
                "route": route,
                "html": html_body,
                "markdown": md_body,
                "metadata": meta,
                "sourceFile": rel_name,
            }
            prev = by_canonical.get(canon_key)
            if prev is None:
                order.append(canon_key)
                by_canonical[canon_key] = rec
            elif len(html_body) > len(prev.get("html", "")):
                by_canonical[canon_key] = rec

    return [by_canonical[k] for k in order]


def capture_site(
    *,
    output_dir: str | Path,
    url: str | None = None,
    from_firecrawl: str | Path | None = None,
    from_sitemap: bool = False,
    sitemap_url: str | None = None,
    crawl: bool = False,
    max_pages: int = 50,
    max_depth: int = 2,
    max_page_bytes: int = DEFAULT_MAX_PAGE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    allow_hosts: Iterable[str] = (),
    allow_http: bool = False,
    overwrite: bool = False,
    resolver: Callable[[str, int], list[str]] | None = None,
    transport: Callable[[str, list[str], dict[str, str], float], tuple[int, dict[str, str], bytes]] | None = None,
) -> dict[str, Any]:
    """Capture a site from live URL (sitemap/BFS) or local Firecrawl archives into raw-capture/ + migration-contract.json."""
    if not url and not from_firecrawl:
        raise ValueError("capture requires either --url <origin> or --from-firecrawl <file-or-dir>.")
    if url and from_firecrawl:
        raise ValueError("Specify either --url or --from-firecrawl, not both.")

    raw_out = Path(output_dir).expanduser()
    reject_symlink_chain(raw_out)
    out = raw_out.resolve()
    reject_controlled_folder_access(out)

    pages_dir = out / "raw-capture" / "pages"
    used_slugs: set[str] = set()
    captured_entries: list[dict[str, Any]] = []
    triage_inputs: list[dict[str, Any]] = []
    sitemaps_visited: list[str] = []
    skipped_urls: list[dict[str, str]] = []
    fallback_reasons: set[str] = set()
    fallback_affected: list[dict[str, Any]] = []
    origin_url: str | None = None
    origin_host: str | None = None
    total_bytes = 0

    if from_firecrawl:
        records = collect_firecrawl_sources(
            from_firecrawl,
            max_files=max(max_pages * 4, 64),
            max_total_bytes=max_total_bytes,
        )
        for rec in records[:max_pages]:
            html_body = rec["html"]
            if not html_body.strip() and rec["markdown"].strip():
                title = html.escape(str(rec["metadata"].get("title") or rec["route"]))
                html_body = f"<!doctype html><html><head><title>{title}</title></head><body><main>{html.escape(rec['markdown'])}</main></body></html>"
            if not html_body.strip():
                continue
            route, shape = normalize_route_and_shape(rec["route"])
            if not origin_url and rec["url"].startswith(("http://", "https://")):
                origin_url = format_sanitized_origin(rec["url"])
                origin_host = normalize_host(origin_url) if origin_url else None
            slug = route_to_capture_slug(route, used_slugs)
            html_bytes = html_body.encode("utf-8")
            total_bytes += len(html_bytes)
            if total_bytes > max_total_bytes:
                raise ValueError(f"Capture exceeded --max-total-bytes ({max_total_bytes})")
            rel_file = f"pages/{slug}.html"
            safe_write_text(pages_dir / f"{slug}.html", html_body, overwrite=overwrite)
            html_attrs, body_attrs = extract_root_attrs(html_body)
            captured_entries.append(
                {
                    "slug": slug,
                    "route": route,
                    "urlShape": shape,
                    "sourceUrl": rec["url"],
                    "finalUrl": rec["url"],
                    "status": 200,
                    "file": rel_file,
                    "bytes": len(html_bytes),
                    "sha256": hashlib.sha256(html_bytes).hexdigest(),
                    "htmlAttrs": html_attrs,
                    "bodyAttrs": body_attrs,
                    "dataWfPage": html_attrs.get("data-wf-page"),
                    "sourceArtifact": rec["sourceFile"],
                }
            )
            triage_inputs.append({"route": route, "html": html_body})
    else:
        assert url is not None
        parsed_seed = urllib.parse.urlsplit(url)
        if not parsed_seed.scheme or not parsed_seed.hostname:
            raise ValueError(f"Invalid --url: {url}")
        origin_url = format_sanitized_origin(url)
        origin_host = normalize_host(parsed_seed.hostname)

        effective_allow: list[str] = [origin_host]
        for h in allow_hosts:
            nh = normalize_host(h) if not h.strip().startswith("*.") else h.strip().lower()
            if nh and nh not in effective_allow:
                effective_allow.append(nh)

        # Upfront seed SSRF/scheme validation BEFORE creating any output directories on disk (CWE-918)
        validate_outbound_url(
            url,
            allowed_hosts=effective_allow,
            allow_http=allow_http,
            resolver=resolver,
        )
        if sitemap_url:
            validate_outbound_url(
                sitemap_url,
                allowed_hosts=effective_allow,
                allow_http=allow_http,
                resolver=resolver,
            )

        seed_queue: collections.deque[tuple[str, int]] = collections.deque()
        queued_urls: set[str] = set()

        def enqueue(target_url: str, depth: int) -> None:
            clean = urllib.parse.urldefrag(target_url)[0]
            p = urllib.parse.urlsplit(clean)
            if p.scheme.lower() not in (("https", "http") if allow_http else ("https",)):
                return
            if p.username is not None or p.password is not None:
                return
            if not host_matches_allowlist(p.hostname or "", effective_allow):
                return
            # Deduplicate query-string variants by default
            norm_path = canonicalize_route(p.path or "/")
            if should_skip_route(norm_path):
                return
            sanitized_orig = format_sanitized_origin(clean)
            if not sanitized_orig:
                return
            canon = f"{sanitized_orig}{norm_path}"
            if canon not in queued_urls:
                queued_urls.add(canon)
                seed_queue.append((canon, depth))

        # Always enqueue seed --url BEFORE sitemap URLs so root/seed page is never dropped under --max-pages N
        enqueue(url, 0)

        if from_sitemap or sitemap_url:
            candidates = [sitemap_url] if sitemap_url else [
                f"{origin_url}/sitemap_index.xml",
                f"{origin_url}/sitemap.xml",
                f"{origin_url}/wp-sitemap.xml",
            ]
            byte_budget = {"used": total_bytes, "limit": max_total_bytes}
            for sm in candidates:
                if not sm:
                    continue
                urls_found, visited = _parse_sitemap_urls(
                    sm,
                    allowed_hosts=effective_allow,
                    allow_http=allow_http,
                    max_page_bytes=max_page_bytes,
                    max_urls=max_pages,
                    byte_budget=byte_budget,
                    resolver=resolver,
                    transport=transport,
                )
                total_bytes = byte_budget["used"]
                sitemaps_visited.extend(visited)
                for u in urls_found:
                    enqueue(u, 0)
                if urls_found:
                    break

        seen_routes: set[str] = set()
        while seed_queue and len(captured_entries) < max_pages:
            target_url, depth = seed_queue.popleft()
            try:
                res = safe_fetch_bytes(
                    target_url,
                    allowed_hosts=effective_allow,
                    allow_http=allow_http,
                    max_bytes=max_page_bytes,
                    resolver=resolver,
                    transport=transport,
                )
            except Exception as exc:
                skipped_urls.append({"url": target_url, "reason": str(exc)})
                continue

            if res.status >= 400:
                err_text = _decode_html_bytes(res.body[:65536], res.content_type)
                waf_hits = detect_waf_challenge(res.status, res.headers, err_text)
                if waf_hits:
                    fallback_reasons.add("waf_or_bot_challenge")
                    fallback_affected.append(
                        {
                            "url": target_url,
                            "status": res.status,
                            "reason": "waf_or_bot_challenge",
                            "markers": waf_hits,
                        }
                    )
                skipped_urls.append({"url": target_url, "reason": f"HTTP {res.status}"})
                continue

            ctype = res.content_type.lower()
            if ctype and "html" not in ctype and "xhtml" not in ctype:
                skipped_urls.append({"url": target_url, "reason": f"Non-HTML Content-Type: {res.content_type}"})
                continue

            final_parsed = urllib.parse.urlsplit(res.final_url)
            route, shape = normalize_route_and_shape(final_parsed.path or "/")
            if should_skip_route(route) or route in seen_routes:
                continue
            seen_routes.add(route)

            html_text = _decode_html_bytes(res.body, res.content_type)
            waf_hits_200 = detect_waf_challenge(res.status, res.headers, html_text)
            if waf_hits_200:
                fallback_reasons.add("waf_or_bot_challenge")
                fallback_affected.append(
                    {
                        "url": res.final_url,
                        "status": res.status,
                        "reason": "waf_or_bot_challenge",
                        "markers": waf_hits_200,
                    }
                )
            spa_detail = detect_empty_spa_shell(html_text)
            if spa_detail:
                fallback_reasons.add("empty_js_spa_shell")
                fallback_affected.append(
                    {
                        "url": res.final_url,
                        "status": res.status,
                        "reason": "empty_js_spa_shell",
                        "detail": spa_detail,
                    }
                )
            html_bytes = html_text.encode("utf-8")
            total_bytes += len(html_bytes)
            if total_bytes > max_total_bytes:
                raise ValueError(f"Capture exceeded --max-total-bytes ({max_total_bytes})")

            slug = route_to_capture_slug(route, used_slugs)
            rel_file = f"pages/{slug}.html"
            safe_write_text(pages_dir / f"{slug}.html", html_text, overwrite=overwrite)
            html_attrs, body_attrs = extract_root_attrs(html_text)
            captured_entries.append(
                {
                    "slug": slug,
                    "route": route,
                    "urlShape": shape,
                    "sourceUrl": target_url,
                    "finalUrl": res.final_url,
                    "status": res.status,
                    "redirects": res.redirects,
                    "file": rel_file,
                    "bytes": len(html_bytes),
                    "sha256": hashlib.sha256(html_bytes).hexdigest(),
                    "htmlAttrs": html_attrs,
                    "bodyAttrs": body_attrs,
                    "dataWfPage": html_attrs.get("data-wf-page"),
                }
            )
            triage_inputs.append({"route": route, "html": html_text})

            if crawl and depth < max_depth:
                for href in _extract_links(html_text):
                    if href.startswith(("#", "mailto:", "tel:", "javascript:")):
                        continue
                    abs_u = urllib.parse.urljoin(res.final_url, href)
                    enqueue(abs_u, depth + 1)

    triage = analyze_migration_triage(triage_inputs, origin_host=origin_host)

    recommended_fallback = bool(fallback_reasons)
    target_for_hint = origin_url or (url or "")
    firecrawl_fallback: dict[str, Any] = {
        "recommended": recommended_fallback,
        "reasons": sorted(fallback_reasons),
        "affectedUrls": fallback_affected,
        "suggestedCommands": [
            f'cto-legends run legends-firecrawl -- scrape "{target_for_hint}" --formats rawHtml,markdown --output <firecrawl-dir>',
            f'legends-astro capture --from-firecrawl <firecrawl-dir> --output "{out}"',
        ]
        if recommended_fallback
        else [],
    }

    shapes = sorted({p["urlShape"] for p in captured_entries if p["urlShape"] != "root"})
    rec_format = "file" if shapes == ["file"] else ("directory" if shapes == ["directory"] else "preserve")

    manifest_payload: dict[str, Any] = {
        "schema": "legends.astro-raw-capture/v1",
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "sourceMode": "firecrawl" if from_firecrawl else "live-http",
        "originUrl": origin_url,
        "originHost": origin_host,
        "sitemapsVisited": sitemaps_visited,
        "pageCount": len(captured_entries),
        "totalBytes": total_bytes,
        "recommendedAstroConfig": {
            "output": "static",
            "build.format": rec_format,
            "trailingSlash": "never" if rec_format == "file" else ("always" if rec_format == "directory" else "ignore"),
        },
        "triage": triage,
        "firecrawlFallback": firecrawl_fallback,
        "pages": captured_entries,
        "skipped": skipped_urls,
    }
    manifest_path = safe_write_text(
        out / "raw-capture" / "manifest.json",
        json.dumps(manifest_payload, indent=2) + "\n",
        overwrite=overwrite,
    )

    # Also build & save migration-contract.json via auditor
    from .auditor import audit_source

    contract = audit_source(out / "raw-capture", base_url=origin_url)
    contract["triage"] = triage
    contract["firecrawlFallback"] = firecrawl_fallback
    contract_path = safe_write_text(
        out / "migration-contract.json",
        json.dumps(contract, indent=2) + "\n",
        overwrite=overwrite,
    )

    return {
        "ok": True,
        "outputDir": str(out),
        "manifestPath": str(manifest_path),
        "contractPath": str(contract_path),
        "pageCount": len(captured_entries),
        "totalBytes": total_bytes,
        "skippedCount": len(skipped_urls),
        "triage": triage,
        "firecrawlFallback": firecrawl_fallback,
        "recommendedAstroConfig": manifest_payload["recommendedAstroConfig"],
    }
