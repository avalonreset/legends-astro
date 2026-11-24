"""Two-pass remote asset localizer (HTML + recursive CSS @import/url()) and longest-URL-first rewriter."""

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
    DEFAULT_MAX_ASSET_BYTES,
    DEFAULT_MAX_PAGE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    assert_path_within,
    host_matches_allowlist,
    normalize_host,
    reject_controlled_folder_access,
    reject_symlink_chain,
    safe_fetch_bytes,
    safe_read_bytes,
    safe_write_bytes,
    safe_write_text,
    sanitize_rel_path,
)

_CSS_URL_RE = re.compile(
    r"""url\(\s*(?:&quot;|&#34;|["'])?([^"')&\s]+?)(?:&quot;|&#34;|["'])?\s*\)""",
    re.IGNORECASE,
)
_CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*)?(?:["']([^"']+)["']|([^\s;)]+))\s*\)?""",
    re.IGNORECASE,
)
_TAG_SRI_RE = re.compile(r"<(link|script)\b([^>]*)>", re.IGNORECASE)
_INTEGRITY_ATTR_RE = re.compile(r"""\s+integrity\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_CROSSORIGIN_ATTR_RE = re.compile(r"""\s+crossorigin(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?""", re.IGNORECASE)
_LOCAL_ASSET_HREF_SRC_RE = re.compile(
    r"""(?:href|src)\s*=\s*["']/(?:assets|wp-content|wp-includes)/""",
    re.IGNORECASE,
)

_FONT_EXTS = frozenset({".woff2", ".woff", ".ttf", ".otf", ".eot"})
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".svg"})


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
            if any(tok in rel.split() for tok in ("stylesheet", "icon", "shortcut", "apple-touch-icon", "preload", "prefetch", "manifest")):
                self._add(attr_map.get("href"))
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


def extract_html_asset_urls(html_text: str) -> list[str]:
    """Extract asset URLs from HTML markup."""
    p = _AssetExtractor()
    p.feed(html_text)
    # Also scan raw HTML for entity-encoded inline background-image:url(&quot;...&quot;)
    for m in _CSS_URL_RE.finditer(html_text):
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

    # Handle dynamic_css.php -> .css when text/css
    is_css_ct = "text/css" in (content_type or "").lower()
    if rel_clean.lower().endswith(".php") and is_css_ct:
        rel_clean = rel_clean[:-4] + ".css"
    elif "." not in Path(rel_clean).name and is_css_ct:
        rel_clean = rel_clean + ".css"

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
        public_path = "/" + str(p.with_name(f"{p.stem}-{uhash}{p.suffix}")).replace("\\", "/")
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


def rewrite_urls_longest_first(text: str, replacements: dict[str, str]) -> str:
    """Replace remote URLs with local public paths sorted longest-URL-first to prevent substring clobbering."""
    if not replacements:
        return text
    expanded: dict[str, str] = {}
    for remote, local in replacements.items():
        expanded[remote] = local
        # Also handle HTML-escaped ampersands and unescaped spaces
        if "&" in remote:
            expanded.setdefault(remote.replace("&", "&amp;"), local)
        if "%20" in remote:
            expanded.setdefault(remote.replace("%20", " "), local)
        if remote.startswith("https://"):
            expanded.setdefault("//" + remote[len("https://") :], local)
    out = text
    for remote in sorted(expanded.keys(), key=len, reverse=True):
        if remote in out:
            out = out.replace(remote, expanded[remote])
    return out


def rewrite_html_assets(html_text: str, asset_map: dict[str, str]) -> str:
    """Rewrite remote URLs in HTML longest-first and strip SRI integrity/crossorigin on rewritten local tags."""
    rewritten = rewrite_urls_longest_first(html_text, asset_map)
    return strip_sri_on_local_tags(rewritten)


def rewrite_css_assets(css_text: str, asset_map: dict[str, str], *, css_remote_url: str | None = None) -> str:
    """Rewrite absolute and relative url(...) / @import URLs in CSS longest-first."""
    local_repls = dict(asset_map)
    if css_remote_url:
        for raw_ref in extract_css_urls(css_text):
            if raw_ref.startswith(("http://", "https://", "//", "data:", "#")):
                continue
            resolved = _normalize_fetch_url(raw_ref, base_url=css_remote_url)
            if resolved and resolved in asset_map:
                local_repls[raw_ref] = asset_map[resolved]
    return rewrite_urls_longest_first(css_text, local_repls)


def vendor_assets(
    *,
    source_dir: str | Path,
    output_dir: str | Path,
    allow_hosts: Iterable[str] = (),
    origin_url: str | None = None,
    allow_http: bool = False,
    dry_run: bool = False,
    overwrite: bool = False,
    max_asset_bytes: int = DEFAULT_MAX_ASSET_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_css_depth: int = 4,
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

    # Discover capture manifest if present to infer page base URLs
    manifest_origin = origin_url
    page_url_by_file: dict[str, str] = {}
    for candidate_manifest in (src / "manifest.json", src / "raw-capture" / "manifest.json"):
        if candidate_manifest.is_file() and not candidate_manifest.is_symlink():
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

    # Collect HTML files
    html_files: list[Path] = []
    if src.is_file():
        html_files = [src]
    else:
        for p in sorted(src.rglob("*")):
            if any(part in ("node_modules", ".git", ".astro", "dist") for part in p.relative_to(src).parts):
                continue
            if p.is_symlink():
                raise ValueError(f"Refusing symlink in source directory: {p}")
            assert_path_within(src, p)
            if p.is_file() and p.suffix.lower() in (".html", ".htm"):
                html_files.append(p)

    # Pass 1: extract candidate URLs from all HTML files
    candidate_urls: list[str] = []
    seen_candidates: set[str] = set()
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
            "allowHosts": allow_list,
            "htmlFilesScanned": len(html_files),
            "plannedAssetCount": len(planned),
            "plannedAssets": planned,
            "skippedHosts": skipped_hosts,
        }

    # Execute Pass 1 + Pass 2 (recursive CSS @import and url())
    asset_map: dict[str, str] = {}
    used_local_paths: set[str] = set()
    downloaded_records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    css_files_to_rewrite: list[tuple[Path, str]] = []  # (local_file_path, remote_final_url)
    total_bytes = 0

    queue: collections.deque[tuple[str, int, str]] = collections.deque(
        (u, 0, "html") for u in candidate_urls
    )
    processed_urls: set[str] = set()

    public_root = out / "public"

    while queue:
        remote_url, css_depth, discovered_from = queue.popleft()
        if remote_url in processed_urls:
            continue
        processed_urls.add(remote_url)

        host = normalize_host(remote_url)
        if not host_matches_allowlist(host, allow_list):
            skipped_hosts[host] = skipped_hosts.get(host, 0) + 1
            continue

        try:
            res = safe_fetch_bytes(
                remote_url,
                allowed_hosts=allow_list,
                allow_http=allow_http,
                max_bytes=max_asset_bytes,
                resolver=resolver,
                transport=transport,
            )
        except Exception as exc:
            errors.append({"url": remote_url, "error": str(exc)})
            continue

        if res.status != 200:
            errors.append({"url": remote_url, "error": f"HTTP {res.status}"})
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
        if res.final_url != remote_url:
            asset_map[res.final_url] = local_pub

        is_css = local_pub.lower().endswith(".css") or "text/css" in res.content_type.lower()
        downloaded_records.append(
            {
                "remoteUrl": remote_url,
                "finalUrl": res.final_url,
                "localPublicPath": local_pub,
                "bytes": len(res.body),
                "contentType": res.content_type,
                "discoveredFrom": discovered_from,
                "sha256": hashlib.sha256(res.body).hexdigest(),
            }
        )

        # Pass 2: crawl CSS @import and url(...)
        if is_css and css_depth < max_css_depth:
            css_files_to_rewrite.append((dest_file, res.final_url))
            css_text = res.body.decode("utf-8", errors="replace")
            for sub_raw in extract_css_urls(css_text):
                sub_url = _normalize_fetch_url(sub_raw, base_url=res.final_url)
                if not sub_url or sub_url in processed_urls:
                    continue
                sub_host = normalize_host(sub_url)
                if not host_matches_allowlist(sub_host, allow_list):
                    skipped_hosts[sub_host] = skipped_hosts.get(sub_host, 0) + 1
                    continue
                queue.append((sub_url, css_depth + 1, f"css:{local_pub}"))

    # Rewrite downloaded CSS files so url(...) and @import resolve locally
    rewritten_css: list[str] = []
    for css_path, css_remote in css_files_to_rewrite:
        original_css = safe_read_bytes(css_path, max_bytes=max_asset_bytes).decode("utf-8", errors="replace")
        updated_css = rewrite_css_assets(original_css, asset_map, css_remote_url=css_remote)
        if updated_css != original_css:
            safe_write_text(css_path, updated_css, overwrite=True)
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
        "allowHosts": allow_list,
        "assetCount": len(downloaded_records),
        "totalBytes": total_bytes,
        "map": asset_map,
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
        out / "asset-map.json",
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
