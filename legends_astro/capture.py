"""Live HTTP/sitemap/BFS capture and Firecrawl archive normalizer with Migration Triage reporting."""

from __future__ import annotations

import collections
import hashlib
import html
import json
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
    host_matches_allowlist,
    normalize_host,
    reject_controlled_folder_access,
    reject_symlink_chain,
    safe_fetch_bytes,
    safe_parse_xml_bytes,
    safe_read_bytes,
    safe_write_text,
)

ASSET_EXT_RE = re.compile(
    r"\.(?:css|js|mjs|cjs|map|png|jpe?g|gif|webp|avif|svg|ico|bmp|tiff?|"
    r"woff2?|ttf|eot|otf|mp4|webm|mov|avi|mp3|wav|ogg|m4a|pdf|zip|gz|tar|tgz|rar|7z|"
    r"xml|json|txt|csv|rss|atom|doc|docx|xls|xlsx|ppt|pptx)$",
    re.IGNORECASE,
)

_SHORTCODE_RE = re.compile(r"\[/?([a-z][a-z0-9_-]{1,32})(?:\s+[^\]\r\n]{0,160})?\]")
_HTML_TAG_ATTRS_RE = re.compile(r"<(html|body)\b([^>]*)>", re.IGNORECASE)
_ATTR_PAIR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?"""
)
_EXTERNAL_URL_RE = re.compile(r"""(?:src|href|data-poster-url|data-video-urls)\s*=\s*["']?(https?://[^"'\s>]+)""", re.IGNORECASE)
_CSS_URL_HOST_RE = re.compile(r"""url\(\s*(?:&quot;|["'])?(https?://[^"')&\s]+)""", re.IGNORECASE)

_TRACKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google-tag-manager", re.compile(r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]+", re.IGNORECASE)),
    ("google-analytics", re.compile(r"googletagmanager\.com/gtag/js|google-analytics\.com|G-[A-Z0-9]{6,}|UA-\d+-\d+", re.IGNORECASE)),
    ("hotjar", re.compile(r"static\.hotjar\.com|hjid\s*:", re.IGNORECASE)),
    ("wordfence", re.compile(r"wordfence|wfls-|wflogs", re.IGNORECASE)),
    ("meta-pixel", re.compile(r"connect\.facebook\.net/.*/fbevents\.js|fbq\s*\(", re.IGNORECASE)),
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

_NON_SHORTCODE_NAMES = frozenset({"if", "endif", "cdata", "object", "array", "email", "protected"})

_WAF_STATUS_CODES = frozenset({403, 429, 503})
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
            reasons.append(label)
    return reasons


def detect_empty_spa_shell(html_text: str) -> str | None:
    """Detect empty client-rendered JS SPA shells (<div id='root'></div> etc.) with minimal visible text."""
    root_match = _SPA_ROOT_RE.search(html_text)
    if not root_match:
        return None
    scripts = _BUNDLE_SCRIPT_RE.findall(html_text)
    if not scripts:
        return None
    body_m = re.search(r"<body\b[^>]*>(.*?)</body>", html_text, flags=re.IGNORECASE | re.DOTALL)
    body_fragment = body_m.group(1) if body_m else html_text
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
    p = path_str or "/"
    if not p.startswith("/"):
        p = "/" + p
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
        for m in _SHORTCODE_RE.finditer(text_only):
            sc_name = m.group(1).lower()
            if sc_name not in _NON_SHORTCODE_NAMES:
                shortcodes.add(f"[{sc_name}]")

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

        for rx in (_EXTERNAL_URL_RE, _CSS_URL_HOST_RE):
            for m in rx.finditer(raw_html):
                u = m.group(1)
                h = normalize_host(u)
                if h and h != norm_origin:
                    external_cdns.add(h)

    if wf_signals > wp_signals and wf_signals >= 2:
        platform = "webflow"
    elif wp_signals >= 2:
        platform = "wordpress"
    else:
        platform = "html"

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
    resolver: Callable[[str, int], list[str]] | None = None,
    transport: Callable[[str, list[str], dict[str, str], float], tuple[int, dict[str, str], bytes]] | None = None,
) -> tuple[list[str], list[str]]:
    """Recursively parse XML sitemaps / sitemap_index files with XXE refusal and SSRF checks."""
    discovered: list[str] = []
    seen_sitemaps: set[str] = set()
    visited_sitemaps: list[str] = []
    queue: collections.deque[tuple[str, int]] = collections.deque([(sitemap_url, 0)])

    while queue and len(discovered) < max_urls:
        sm_url, depth = queue.popleft()
        if sm_url in seen_sitemaps or depth > max_depth:
            continue
        seen_sitemaps.add(sm_url)
        try:
            res = safe_fetch_bytes(
                sm_url,
                allowed_hosts=allowed_hosts,
                allow_http=allow_http,
                max_bytes=max_page_bytes,
                resolver=resolver,
                transport=transport,
            )
        except Exception:
            continue
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


def unwrap_firecrawl_records(data: Any, *, source_label: str = "") -> list[dict[str, Any]]:
    """Unwrap Firecrawl JSON envelopes: response.data, legends-firecrawl-evidence/v1, top-level data, or bare record."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []

    # legends-firecrawl .raw.json envelope: {"metadata": ..., "response": {"data": [...] | {...}}}
    if isinstance(data.get("response"), dict):
        resp = data["response"]
        if isinstance(resp.get("data"), list):
            return [r for r in resp["data"] if isinstance(r, dict)]
        if isinstance(resp.get("data"), dict):
            return [resp["data"]]
        if "html" in resp or "rawHtml" in resp or "markdown" in resp:
            return [resp]

    # legends-firecrawl-evidence/v1 envelope
    if str(data.get("schema", "")).startswith("legends-firecrawl-evidence/"):
        for key in ("records", "items", "pages", "documents", "evidence", "data"):
            val = data.get(key)
            if isinstance(val, list):
                out: list[dict[str, Any]] = []
                for item in val:
                    if isinstance(item, dict):
                        out.extend(unwrap_firecrawl_records(item, source_label=source_label) or [item])
                if out:
                    return out
            elif isinstance(val, dict):
                return [val]

    if isinstance(data.get("data"), list):
        return [r for r in data["data"] if isinstance(r, dict)]
    if isinstance(data.get("data"), dict):
        return [data["data"]]

    if any(k in data for k in ("html", "rawHtml", "markdown", "content", "url")):
        return [data]

    return []


def collect_firecrawl_sources(source_path: str | Path) -> list[dict[str, Any]]:
    """Walk a Firecrawl JSON file or directory (including .raw.json, .json, .html) and deduplicate by canonical URL."""
    raw_src = Path(source_path).expanduser()
    reject_symlink_chain(raw_src)
    src = raw_src.resolve()
    if not src.exists():
        raise FileNotFoundError(f"Firecrawl source path does not exist: {src}")

    files: list[Path] = []
    if src.is_file():
        files = [src]
    else:
        for p in sorted(src.rglob("*")):
            if any(part in ("node_modules", ".git", ".astro", "dist") for part in p.relative_to(src).parts):
                continue
            if p.is_symlink():
                raise ValueError(f"Refusing symlink in Firecrawl directory: {p}")
            assert_path_within(src, p)
            if not p.is_file():
                continue
            if p.name.endswith(".manifest.json") or p.name == "manifest.json":
                continue
            if p.suffix.lower() in (".json", ".html", ".htm"):
                files.append(p)

    by_canonical: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for f in files:
        raw_bytes = safe_read_bytes(f, max_bytes=DEFAULT_MAX_PAGE_BYTES)
        rel_name = f.relative_to(src).as_posix() if src.is_dir() else f.name
        if f.suffix.lower() in (".html", ".htm"):
            html_body = raw_bytes.decode("utf-8", errors="replace")
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
        records = collect_firecrawl_sources(from_firecrawl)
        for rec in records[:max_pages]:
            html_body = rec["html"]
            if not html_body and rec["markdown"]:
                title = html.escape(str(rec["metadata"].get("title") or rec["route"]))
                html_body = f"<!doctype html><html><head><title>{title}</title></head><body><main>{html.escape(rec['markdown'])}</main></body></html>"
            route, shape = normalize_route_and_shape(rec["route"])
            if not origin_url and rec["url"].startswith(("http://", "https://")):
                p = urllib.parse.urlsplit(rec["url"])
                origin_url = f"{p.scheme}://{p.netloc}"
                origin_host = p.hostname
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
        origin_url = f"{parsed_seed.scheme}://{parsed_seed.netloc}"
        origin_host = parsed_seed.hostname.lower()

        effective_allow: list[str] = [origin_host]
        for h in allow_hosts:
            nh = normalize_host(h)
            if nh and nh not in effective_allow:
                effective_allow.append(nh)

        seed_queue: collections.deque[tuple[str, int]] = collections.deque()
        queued_urls: set[str] = set()

        def enqueue(target_url: str, depth: int) -> None:
            clean = urllib.parse.urldefrag(target_url)[0]
            p = urllib.parse.urlsplit(clean)
            if p.scheme.lower() not in (("https", "http") if allow_http else ("https",)):
                return
            if not host_matches_allowlist(p.hostname or "", effective_allow):
                return
            # Deduplicate query-string variants by default
            norm_path = p.path or "/"
            if should_skip_route(norm_path):
                return
            canon = f"{p.scheme.lower()}://{p.netloc.lower()}{norm_path}"
            if canon not in queued_urls:
                queued_urls.add(canon)
                seed_queue.append((canon, depth))

        if from_sitemap or sitemap_url:
            candidates = [sitemap_url] if sitemap_url else [
                f"{origin_url}/sitemap_index.xml",
                f"{origin_url}/sitemap.xml",
                f"{origin_url}/wp-sitemap.xml",
            ]
            for sm in candidates:
                if not sm:
                    continue
                urls_found, visited = _parse_sitemap_urls(
                    sm,
                    allowed_hosts=effective_allow,
                    allow_http=allow_http,
                    max_page_bytes=max_page_bytes,
                    max_urls=max_pages,
                    resolver=resolver,
                    transport=transport,
                )
                sitemaps_visited.extend(visited)
                for u in urls_found:
                    enqueue(u, 0)
                if urls_found:
                    break

        enqueue(url, 0)

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
