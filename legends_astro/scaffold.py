"""Preview-first Astro v7.4.13 project scaffolder (--mode clone | editorial | starter) with CF email decoding, tracker stripping, and Triple-Lock staging."""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

from .auditor import audit_source
from .capture import extract_head_and_body, extract_root_attrs, normalize_route_and_shape, route_to_capture_slug
from .net_safety import (
    DEFAULT_MAX_PAGE_BYTES,
    assert_path_within,
    extract_html_tag_bodies,
    is_reparse_or_symlink,
    reject_controlled_folder_access,
    reject_symlink_chain,
    replace_html_blocks,
    safe_read_bytes,
    safe_write_text,
)
from .vendor import rewrite_html_assets

ASTRO_TARGET_VERSION = "7.4.13"
ASTRO_ENGINE_MIN_NODE = ">=22.23.4"
SHARP_OVERRIDE = "^0.35.5"
UNDICI_OVERRIDE = "^8.13.1"
SITEMAP_TARGET_VERSION = "^3.7.6"
STARTER_PRESETS = ("agency", "blog", "local-business")
TRAILING_SLASH_MODES = ("always", "never", "ignore")

_CF_EMAIL_LINK_RE = re.compile(
    r"""<a\b([^<>]*?)href=["'](?:/cdn-cgi/l/email-protection#([0-9a-fA-F]{4,}))["']([^<>]*)>.*?</a>""",
    re.IGNORECASE | re.DOTALL,
)
_CF_EMAIL_DATA_ANCHOR_RE = re.compile(
    r"""<a\b([^<>]*?)data-cfemail=["']([0-9a-fA-F]{4,})["']([^<>]*)>.*?</a>""",
    re.IGNORECASE | re.DOTALL,
)
_CF_EMAIL_SPAN_RE = re.compile(
    r"""<(?:span|a)\b[^<>]*data-cfemail=["']([0-9a-fA-F]{4,})["'][^<>]*>.*?</(?:span|a)>""",
    re.IGNORECASE | re.DOTALL,
)
_CF_EMAIL_SCRIPT_RE = re.compile(
    r"""<script\b[^<>]*src=["'][^"']*/cdn-cgi/scripts/[^"']*email-decode[^"']*["'][^<>]*>\s*</script>""",
    re.IGNORECASE | re.DOTALL,
)

_TRACKER_MATCH_RE = re.compile(
    r"(?i:googletagmanager\.com|google-analytics\.com|gtag\s*\()"
    r"|(?-i:\bGTM-[A-Z0-9]{4,10}\b)"
    r"|(?i:static\.hotjar\.com|hjid\s*:|wordfence|wfls-|wflogs"
    r"|google\.com/recaptcha|grecaptcha|wpcf7-recaptcha|contact-form-7/modules/recaptcha"
    r"|cleantalk-spam-protect|apbct[-_]"
    r"|challenges\.cloudflare\.com/turnstile"
    r"|static\.cloudflareinsights\.com/beacon|/cdn-cgi/scripts/|/cdn-cgi/challenge-platform/"
    r"""|connect\.facebook\.net/[^\s"'<>)]{0,256}/fbevents\.js|fbq\s*\(|clarity\.ms/tag/)""",
)
_TURNSTILE_ATTR_RE = re.compile(
    r"""(?<!\s)\s++data-turnstile-sitekey\s*+=\s*+(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)
_META_ROBOTS_RE = re.compile(r"""<meta\b[^<>]*name\s*+=\s*+["']robots["'][^<>]*>""", re.IGNORECASE)


def decode_cf_email_hex(hex_str: str) -> str:
    """Decode Cloudflare email-protection XOR-obfuscated hex string into plaintext email."""
    cleaned = hex_str.strip()
    if len(cleaned) < 4 or len(cleaned) % 2 != 0:
        return ""
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        return ""
    key = raw[0]
    decoded = bytes(b ^ key for b in raw[1:])
    return decoded.decode("utf-8", errors="replace")


def decode_cloudflare_emails(html_text: str) -> str:
    """Decode all Cloudflare email-protection links/spans and remove email-decode scripts."""
    low = html_text.lower()
    if "email-protection" not in low and "data-cfemail" not in low and "email-decode" not in low:
        return html_text

    def _replace_href_link(m: re.Match[str]) -> str:
        email = decode_cf_email_hex(m.group(2))
        if not email:
            return m.group(0)
        escaped = html.escape(email, quote=True)
        before = re.sub(r"""\s*data-cfemail\s*=\s*["'][^"']*["']""", "", m.group(1), flags=re.IGNORECASE)
        after = re.sub(r"""\s*data-cfemail\s*=\s*["'][^"']*["']""", "", m.group(3), flags=re.IGNORECASE)
        return f'<a{before}href="mailto:{escaped}"{after}>{escaped}</a>'

    def _replace_data_anchor(m: re.Match[str]) -> str:
        email = decode_cf_email_hex(m.group(2))
        if not email:
            return m.group(0)
        escaped = html.escape(email, quote=True)
        attrs = re.sub(
            r"""href\s*=\s*["'][^"']*email-protection[^"']*["']""",
            f'href="mailto:{escaped}"',
            m.group(1) + m.group(3),
            flags=re.IGNORECASE,
        )
        if "href=" not in attrs.lower():
            attrs = f' href="mailto:{escaped}"' + attrs
        return f"<a{attrs}>{escaped}</a>"

    def _replace_span(m: re.Match[str]) -> str:
        email = decode_cf_email_hex(m.group(1))
        return html.escape(email) if email else m.group(0)

    out = _CF_EMAIL_LINK_RE.sub(_replace_href_link, html_text)
    out = _CF_EMAIL_DATA_ANCHOR_RE.sub(_replace_data_anchor, out)
    out = _CF_EMAIL_SPAN_RE.sub(_replace_span, out)
    out = _CF_EMAIL_SCRIPT_RE.sub("", out)
    return out


def strip_trackers(html_text: str) -> str:
    """Remove live analytics, GTM, Hotjar, Wordfence, reCAPTCHA, Turnstile, and Cloudflare beacon/email scripts."""
    out = replace_html_blocks(
        html_text,
        tags=("script", "noscript"),
        replacer=lambda _kind, block: "" if _TRACKER_MATCH_RE.search(block) else block,
    )
    out = _TURNSTILE_ATTR_RE.sub("", out)
    return out


def _inject_staging_noindex(head_html: str) -> str:
    cleaned = _META_ROBOTS_RE.sub("", head_html)
    tag = '<meta name="robots" content="noindex, nofollow, noarchive, nosnippet">'
    return f"{tag}\n{cleaned.lstrip()}"


def _extract_head_and_body(html_text: str) -> tuple[str, str]:
    return extract_head_and_body(html_text)


def _load_asset_map_bundle(asset_map_path: str | Path | None) -> tuple[dict[str, str], str | None, dict[str, str]]:
    if not asset_map_path:
        return {}, None, {}
    raw_p = Path(asset_map_path).expanduser()
    reject_symlink_chain(raw_p)
    data = json.loads(safe_read_bytes(raw_p.resolve(), max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8"))
    if isinstance(data, dict) and isinstance(data.get("map"), dict):
        amap = {str(k): str(v) for k, v in data["map"].items()}
        origin = str(data["originUrl"]) if data.get("originUrl") else None
        rel_map = (
            {str(k): str(v) for k, v in data["rootRelativeMap"].items()}
            if isinstance(data.get("rootRelativeMap"), dict)
            else {}
        )
        return amap, origin, rel_map
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}, None, {}
    return {}, None, {}


def _load_asset_map(asset_map_path: str | Path | None) -> dict[str, str]:
    amap, _, _ = _load_asset_map_bundle(asset_map_path)
    return amap


def _package_json_content(project_name: str, *, include_sitemap: bool = False) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", project_name.lower()).strip("-") or "astro-site"
    deps: dict[str, str] = {
        "astro": ASTRO_TARGET_VERSION,
    }
    if include_sitemap:
        deps["@astrojs/sitemap"] = SITEMAP_TARGET_VERSION
    pkg = {
        "name": slug,
        "private": True,
        "version": "0.1.0",
        "type": "module",
        "engines": {
            "node": ASTRO_ENGINE_MIN_NODE,
        },
        "scripts": {
            "build": "node ./node_modules/astro/bin/astro.mjs build",
            "sync": "node ./node_modules/astro/bin/astro.mjs sync",
            "check": "node ./node_modules/astro/bin/astro.mjs check",
        },
        "dependencies": deps,
        "overrides": {
            "sharp": SHARP_OVERRIDE,
            "undici": UNDICI_OVERRIDE,
        },
    }
    return json.dumps(pkg, indent=2) + "\n"


def _astro_config_content(
    *,
    build_format: str,
    trailing_slash: str,
    site_url: str | None = None,
    editorial: bool = False,
    enable_sitemap: bool = False,
) -> str:
    lines = ['import { defineConfig } from "astro/config";']
    if enable_sitemap:
        lines.append('import sitemap from "@astrojs/sitemap";')
    lines.append("")
    if editorial:
        lines.append("// Optional Astro v7 Fonts API (https://docs.astro.build/en/guides/fonts/)")
        lines.append("// and @astrojs/sitemap can be added here when moving to production.")
    lines.append("export default defineConfig({")
    if site_url:
        lines.append(f"  site: {json.dumps(site_url)},")
    lines.append('  output: "static",')
    lines.append(f"  trailingSlash: {json.dumps(trailing_slash)},")
    lines.append("  build: {")
    lines.append(f"    format: {json.dumps(build_format)},")
    lines.append("  },")
    if enable_sitemap:
        lines.append("  integrations: [sitemap()],")
    lines.append("});")
    return "\n".join(lines) + "\n"


def _published_helper_content() -> str:
    return (
        "export function isPublished(entry: { data: { draft?: boolean } }): boolean {\n"
        "  if (!import.meta.env.PROD) return true;\n"
        "  return entry.data.draft !== true;\n"
        "}\n"
    )


def _starter_published_helper_content() -> str:
    return (
        "export function isPublished(entry: { data: { draft?: boolean; pubDate?: Date | string } }): boolean {\n"
        "  if (!import.meta.env.PROD) return true;\n"
        "  if (entry.data.draft === true) return false;\n"
        "  if (entry.data.pubDate) {\n"
        "    const ts =\n"
        "      entry.data.pubDate instanceof Date\n"
        "        ? entry.data.pubDate.valueOf()\n"
        "        : Date.parse(String(entry.data.pubDate));\n"
        "    if (!Number.isNaN(ts) && ts > Date.now()) return false;\n"
        "  }\n"
        "  return true;\n"
        "}\n"
    )


def _validate_site_url(site_url: str | None) -> str | None:
    if site_url is None:
        return None
    cleaned = str(site_url).strip()
    if not cleaned:
        return None
    if re.search(r"[\x00-\x1f\x7f\r\n\s]", cleaned):
        raise ValueError(f"Invalid --site URL containing whitespace or control characters: {site_url!r}")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() not in ("https", "http") or not parsed.hostname:
        raise ValueError(f"Invalid --site URL (expected https:// or http:// with hostname): {site_url!r}")
    return cleaned.rstrip("/")


def _starter_preset_copy(preset: str, site_name: str) -> dict[str, Any]:
    if preset == "blog":
        return {
            "defaultDescription": f"{site_name} publishes technical guides, essays, and editorial updates built on Astro v7.",
            "heroKicker": "Editorial Publication",
            "heroTitle": f"Welcome to {site_name}",
            "heroLead": "Fast, static-first writing powered by Astro v7 Content Layer, clean semantic HTML, and built-in RSS.",
            "schemaType": "Organization",
            "highlights": [
                ("Content Layer Native", "Markdown articles validated with Astro v7 glob() and astro/zod schemas."),
                ("Draft and Schedule Safe", "Drafts and future-dated posts are excluded automatically from production builds."),
                ("SEO and RSS Ready", "Canonical tags, OpenGraph metadata, BlogPosting JSON-LD, and RSS 2.0 out of the box."),
            ],
            "welcomeTitle": f"Launching the {site_name} editorial journal",
            "welcomeDescription": f"Discover how {site_name} organizes Markdown articles, drafts, scheduled posts, SEO metadata, and zero-dep RSS feeds on Astro v7.",
            "welcomeCategory": "Editorial",
            "welcomeTags": ["astro", "blog", "publishing"],
            "welcomeBody": (
                "## Built for reliable daily publishing\n\n"
                "This starter uses Astro v7 Content Layer collections defined in `src/content.config.ts` so every article "
                "is validated at build time before deployment.\n\n"
                "## Publishing new articles\n\n"
                "Use `legends-astro post` to preview frontmatter, localize images into `public/images/<collection>/<slug>/`, and run SEO "
                "preflight checks before applying changes.\n"
            ),
        }
    if preset == "local-business":
        return {
            "defaultDescription": f"{site_name} provides trusted local services, clear scheduling, and helpful neighborhood updates.",
            "heroKicker": "Local Services",
            "heroTitle": f"Trusted local expertise from {site_name}",
            "heroLead": "Clear service information, LocalBusiness structured data, and local news built for speed and search visibility.",
            "schemaType": "LocalBusiness",
            "highlights": [
                ("Local Search Structured Data", "Emits LocalBusiness JSON-LD alongside BlogPosting and BreadcrumbList schemas."),
                ("Fast Mobile Performance", "Zero client-side JavaScript overhead and clean responsive CSS for local visitors."),
                ("Simple Ongoing Updates", "Publish seasonal offers, guides, and announcements safely with legends-astro post."),
            ],
            "welcomeTitle": f"Welcome to {site_name}: Serving our local community",
            "welcomeDescription": f"Learn how {site_name} delivers reliable local services, clear scheduling, LocalBusiness schema, and fast updates on Astro v7.",
            "welcomeCategory": "Community",
            "welcomeTags": ["local-business", "updates", "services"],
            "welcomeBody": (
                "## Clear service details for local customers\n\n"
                "Update your business name, description, and structured data details in `src/lib/site.ts` so search engines "
                "and local directories receive accurate information.\n\n"
                "## Sharing updates and seasonal guides\n\n"
                "Add new articles in `src/content/blog/` using `legends-astro post` and verify every release with "
                "`legends-astro verify-build`.\n"
            ),
        }
    # Default: agency
    return {
        "defaultDescription": f"{site_name} builds high-performance static websites, digital systems, and editorial platforms on Astro v7.",
        "heroKicker": "Digital Agency Starter",
        "heroTitle": f"{site_name} builds fast, verifiable websites",
        "heroLead": "Production-grade Astro v7 architecture with Content Layer validation, SEO metadata, structured data, and automated release checks.",
        "schemaType": "Organization",
        "highlights": [
            ("Deterministic Architecture", "Strict Astro v7 Content Layer schemas, predictable trailing-slash routing, and clean semantic markup."),
            ("Production Safety Gates", "Draft and future-date exclusion in production plus baseline manifest regression verification."),
            ("Day-2 Content Operations", "Publish, update, and safely rename posts with automatic 301 redirect rules via legends-astro post."),
        ],
        "welcomeTitle": f"Inside the {site_name} Astro v7 starter architecture",
        "welcomeDescription": f"An overview of how {site_name} structures Astro v7 Content Layer collections, SEO metadata, RSS feeds, and automated checks.",
        "welcomeCategory": "Engineering",
        "welcomeTags": ["astro", "agency", "architecture"],
        "welcomeBody": (
            "## Why this starter is structured this way\n\n"
            "Every route in this project is statically generated with Astro v7.4 using `src/content.config.ts`, `glob()`, "
            "and `astro/zod`. Drafts and scheduled future posts are filtered out of both index listings and dynamic routes "
            "during production builds.\n\n"
            "## Day-2 publishing workflow\n\n"
            "Publish or update articles with `legends-astro post`, build locally with `node ./node_modules/astro/bin/astro.mjs build`, "
            "and verify zero regressions against your site manifest with `legends-astro verify-build --baseline`.\n"
        ),
    }


def _plan_starter_project(
    *,
    out: Path,
    plan_file: Any,
    project_display_name: str,
    preset: str,
    eff_site: str | None,
    trailing_slash: str,
    build_format: str,
    staging_lock: bool,
    enable_sitemap: bool,
) -> None:
    copy = _starter_preset_copy(preset, project_display_name)
    plan_file("package.json", _package_json_content(project_display_name, include_sitemap=enable_sitemap))
    plan_file(
        "astro.config.mjs",
        _astro_config_content(
            build_format=build_format,
            trailing_slash=trailing_slash,
            site_url=eff_site,
            editorial=False,
            enable_sitemap=enable_sitemap,
        ),
    )

    content_config = """import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

const blog = defineCollection({
  loader: glob({
    pattern: "**/*.{md,mdx}",
    base: "./src/content/blog",
  }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    pubDate: z.coerce.date(),
    updatedDate: z.coerce.date().optional(),
    author: z.string().optional(),
    categories: z.array(z.string()).default([]),
    tags: z.array(z.string()).default([]),
    heroImage: z.string().optional(),
    heroAlt: z.string().optional(),
    canonicalUrl: z.string().optional(),
    draft: z.boolean().optional(),
  }),
});

const pages = defineCollection({
  loader: glob({
    pattern: "**/*.{md,mdx}",
    base: "./src/content/pages",
  }),
  schema: z.object({
    title: z.string(),
    description: z.string().optional(),
    updatedDate: z.coerce.date().optional(),
    canonicalUrl: z.string().optional(),
    draft: z.boolean().optional(),
  }),
});

export const collections = { blog, pages };
"""
    plan_file("src/content.config.ts", content_config)
    plan_file("src/lib/published.ts", _starter_published_helper_content())

    site_ts = f"""export type TrailingSlashMode = "always" | "never" | "ignore";
export type StarterPreset = "agency" | "blog" | "local-business";

export const SITE_CONFIG = {{
  name: {json.dumps(project_display_name, ensure_ascii=False)},
  preset: {json.dumps(preset)} as StarterPreset,
  trailingSlash: {json.dumps(trailing_slash)} as TrailingSlashMode,
  defaultDescription: {json.dumps(copy["defaultDescription"], ensure_ascii=False)},
  siteUrl: {json.dumps(eff_site)},
  enableSitemap: {json.dumps(enable_sitemap)},
  stagingLock: {json.dumps(staging_lock)},
}} as const;

export function formatRoute(rawPath: string): string {{
  const cleaned = String(rawPath || "/").trim();
  if (!cleaned || cleaned === "/") return "/";
  const core = cleaned.replace(/^\\/+|\\/+$/g, "");
  if (!core) return "/";
  if (SITE_CONFIG.trailingSlash === "never") {{
    return `/${{core}}`;
  }}
  return `/${{core}}/`;
}}

export function buildOrganizationSchema(siteOrigin?: string): Record<string, unknown> {{
  const schema: Record<string, unknown> = {{
    "@context": "https://schema.org",
    "@type": {json.dumps(copy["schemaType"])},
    name: SITE_CONFIG.name,
    description: SITE_CONFIG.defaultDescription,
  }};
  const resolvedUrl = siteOrigin || SITE_CONFIG.siteUrl;
  if (resolvedUrl) {{
    schema.url = resolvedUrl;
  }}
  return schema;
}}

export interface ArticleSchemaInput {{
  title: string;
  description: string;
  pubDate: Date;
  updatedDate?: Date;
  author?: string;
  image?: string;
  url?: string;
  siteOrigin?: string;
}}

export function buildArticleSchema(input: ArticleSchemaInput): Record<string, unknown> {{
  const schema: Record<string, unknown> = {{
    "@context": "https://schema.org",
    "@type": "BlogPosting",
    headline: input.title,
    description: input.description,
    datePublished: input.pubDate.toISOString(),
    publisher: {{
      "@type": {json.dumps(copy["schemaType"])},
      name: SITE_CONFIG.name,
    }},
  }};
  if (input.updatedDate) {{
    schema.dateModified = input.updatedDate.toISOString();
  }}
  if (input.author) {{
    schema.author = {{
      "@type": "Person",
      name: input.author,
    }};
  }}
  if (input.image) {{
    schema.image =
      input.siteOrigin && input.image.startsWith("/")
        ? new URL(input.image, input.siteOrigin).href
        : input.image;
  }}
  if (input.url) {{
    schema.mainEntityOfPage = {{
      "@type": "WebPage",
      "@id": input.url,
    }};
  }}
  return schema;
}}

export function buildBreadcrumbSchema(
  items: Array<{{ name: string; url?: string }}>,
): Record<string, unknown> {{
  return {{
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    itemListElement: items.map((item, idx) => {{
      const entry: Record<string, unknown> = {{
        "@type": "ListItem",
        position: idx + 1,
        name: item.name,
      }};
      if (item.url) {{
        entry.item = item.url;
      }}
      return entry;
    }}),
  }};
}}
"""
    plan_file("src/lib/site.ts", site_ts)

    base_layout = """---
import "../styles/global.css";
import { SITE_CONFIG, formatRoute } from "../lib/site";

export interface Props {
  title: string;
  description?: string;
  canonicalUrl?: string;
  image?: string;
  ogType?: string;
  robots?: string;
  jsonLd?: Record<string, unknown>[];
}

const {
  title,
  description = SITE_CONFIG.defaultDescription,
  canonicalUrl,
  image,
  ogType = "website",
  robots,
  jsonLd = [],
} = Astro.props;

const pageTitle = title === SITE_CONFIG.name ? title : `${title} | ${SITE_CONFIG.name}`;
const siteBase = Astro.site?.href ?? SITE_CONFIG.siteUrl ?? undefined;
const resolvedCanonical =
  canonicalUrl ?? (Astro.site ? new URL(Astro.url.pathname, Astro.site).href : undefined);
const resolvedImage =
  image && siteBase && image.startsWith("/") ? new URL(image, siteBase).href : image;
const effectiveRobots = SITE_CONFIG.stagingLock
  ? "noindex, nofollow, noarchive, nosnippet"
  : robots;
const suppressCanonical = Boolean(robots && /noindex/i.test(robots));
const blogHref = formatRoute("/blog");
---
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    {effectiveRobots && <meta name="robots" content={effectiveRobots} />}
    <title>{pageTitle}</title>
    <meta name="description" content={description} />
    {!suppressCanonical && resolvedCanonical && <link rel="canonical" href={resolvedCanonical} />}
    <link
      rel="alternate"
      type="application/rss+xml"
      title={`${SITE_CONFIG.name} RSS Feed`}
      href="/rss.xml"
    />
    {SITE_CONFIG.enableSitemap && !SITE_CONFIG.stagingLock && (
      <link rel="sitemap" href="/sitemap-index.xml" />
    )}
    <meta property="og:type" content={ogType} />
    <meta property="og:site_name" content={SITE_CONFIG.name} />
    <meta property="og:title" content={pageTitle} />
    <meta property="og:description" content={description} />
    {resolvedCanonical && <meta property="og:url" content={resolvedCanonical} />}
    {resolvedImage && <meta property="og:image" content={resolvedImage} />}
    <meta name="twitter:card" content={resolvedImage ? "summary_large_image" : "summary"} />
    <meta name="twitter:title" content={pageTitle} />
    <meta name="twitter:description" content={description} />
    {resolvedImage && <meta name="twitter:image" content={resolvedImage} />}
    {jsonLd.map((block) => (
      <script
        type="application/ld+json"
        set:html={JSON.stringify(block).replace(/</g, "\\u003c")}
      />
    ))}
  </head>
  <body>
    <header class="site-header">
      <div class="container header-inner">
        <a class="brand" href="/">{SITE_CONFIG.name}</a>
        <nav aria-label="Primary">
          <a href="/">Home</a>{" "}
          <a href={blogHref}>Blog</a>{" "}
          <a href="/rss.xml">RSS</a>
        </nav>
      </div>
    </header>
    <main class="container main-content">
      <slot />
    </main>
    <footer class="site-footer">
      <div class="container">
        <p>{SITE_CONFIG.name}</p>
      </div>
    </footer>
  </body>
</html>
"""
    plan_file("src/layouts/BaseLayout.astro", base_layout)

    blog_post_layout = """---
import BaseLayout from "./BaseLayout.astro";
import {
  SITE_CONFIG,
  buildArticleSchema,
  buildBreadcrumbSchema,
  formatRoute,
} from "../lib/site";

export interface Props {
  title: string;
  description: string;
  pubDate: Date;
  updatedDate?: Date;
  author?: string;
  tags?: string[];
  heroImage?: string;
  heroAlt?: string;
  canonicalUrl?: string;
}

const {
  title,
  description,
  pubDate,
  updatedDate,
  author,
  tags = [],
  heroImage,
  heroAlt,
  canonicalUrl,
} = Astro.props;

const siteBase = Astro.site?.href ?? SITE_CONFIG.siteUrl ?? undefined;
const pageUrl =
  canonicalUrl ?? (Astro.site ? new URL(Astro.url.pathname, Astro.site).href : undefined);
const blogPath = formatRoute("/blog");
const articleSchema = buildArticleSchema({
  title,
  description,
  pubDate,
  updatedDate,
  author,
  image: heroImage,
  url: pageUrl,
  siteOrigin: siteBase,
});
const breadcrumbSchema = buildBreadcrumbSchema([
  { name: "Home", url: siteBase ? new URL("/", siteBase).href : undefined },
  { name: "Blog", url: siteBase ? new URL(blogPath, siteBase).href : undefined },
  { name: title, url: pageUrl },
]);

const formattedPub = pubDate.toISOString().slice(0, 10);
const formattedUpdated = updatedDate ? updatedDate.toISOString().slice(0, 10) : undefined;
---
<BaseLayout
  title={title}
  description={description}
  canonicalUrl={canonicalUrl}
  image={heroImage}
  ogType="article"
  jsonLd={[articleSchema, breadcrumbSchema]}
>
  <article class="post-article">
    <header class="post-header">
      <h1>{title}</h1>
      <p class="post-meta">
        <time datetime={pubDate.toISOString()}>{formattedPub}</time>
        {formattedUpdated && (
          <span>
            {" "}Updated <time datetime={updatedDate?.toISOString()}>{formattedUpdated}</time>
          </span>
        )}
        {author && <span>{" "}By {author}</span>}
      </p>
      {tags.length > 0 && (
        <ul class="tag-list" aria-label="Tags">
          {tags.map((tag) => (
            <li class="tag-pill">{tag}</li>
          ))}
        </ul>
      )}
    </header>
    {heroImage && (
      <figure class="post-hero">
        <img src={heroImage} alt={heroAlt ?? title} loading="eager" />
      </figure>
    )}
    <div class="prose">
      <slot />
    </div>
  </article>
</BaseLayout>
"""
    plan_file("src/layouts/BlogPostLayout.astro", blog_post_layout)

    highlights_json = json.dumps(
        [{"title": t, "detail": d} for t, d in copy["highlights"]],
        ensure_ascii=False,
        indent=2,
    )
    index_astro = f"""---
import {{ getCollection }} from "astro:content";
import BaseLayout from "../layouts/BaseLayout.astro";
import {{ isPublished }} from "../lib/published";
import {{ SITE_CONFIG, buildOrganizationSchema, formatRoute }} from "../lib/site";

const posts = (await getCollection("blog", isPublished))
  .sort((a, b) => b.data.pubDate.valueOf() - a.data.pubDate.valueOf())
  .slice(0, 3);

const highlights = {highlights_json};
const orgSchema = buildOrganizationSchema(Astro.site?.href ?? SITE_CONFIG.siteUrl ?? undefined);
const blogHref = formatRoute("/blog");
---
<BaseLayout
  title={{SITE_CONFIG.name}}
  description={{SITE_CONFIG.defaultDescription}}
  jsonLd={{[orgSchema]}}
>
  <section class="hero">
    <p class="kicker">{html.escape(copy["heroKicker"])}</p>
    <h1>{html.escape(copy["heroTitle"])}</h1>
    <p class="lead">{html.escape(copy["heroLead"])}</p>
    <p class="hero-actions">
      <a class="button" href={{blogHref}}>Read the blog</a>
    </p>
  </section>

  <section class="card-grid" aria-label="Highlights">
    {{highlights.map((item) => (
      <div class="card">
        <h2>{{item.title}}</h2>
        <p>{{item.detail}}</p>
      </div>
    ))}}
  </section>

  <section class="latest-posts">
    <div class="section-head">
      <h2>Latest posts</h2>
      <a href={{blogHref}}>View all</a>
    </div>
    {{posts.length === 0 ? (
      <p>No published posts yet.</p>
    ) : (
      <ul class="post-list">
        {{posts.map((post) => (
          <li class="post-item">
            <h3>
              <a href={{formatRoute(`/blog/${{post.id}}`)}}>{{post.data.title}}</a>
            </h3>
            <p class="post-meta">{{post.data.pubDate.toISOString().slice(0, 10)}}</p>
            <p>{{post.data.description}}</p>
          </li>
        ))}}
      </ul>
    )}}
  </section>
</BaseLayout>
"""
    plan_file("src/pages/index.astro", index_astro)

    blog_index_astro = """---
import { getCollection } from "astro:content";
import BaseLayout from "../../layouts/BaseLayout.astro";
import { isPublished } from "../../lib/published";
import { SITE_CONFIG, formatRoute } from "../../lib/site";

const posts = (await getCollection("blog", isPublished)).sort(
  (a, b) => b.data.pubDate.valueOf() - a.data.pubDate.valueOf(),
);
---
<BaseLayout
  title="Blog"
  description={`Latest articles and updates from ${SITE_CONFIG.name}.`}
>
  <section class="blog-index">
    <h1>Blog</h1>
    {posts.length === 0 ? (
      <p>No published articles yet.</p>
    ) : (
      <ul class="post-list">
        {posts.map((post) => (
          <li class="post-item">
            <h2>
              <a href={formatRoute(`/blog/${post.id}`)}>{post.data.title}</a>
            </h2>
            <p class="post-meta">{post.data.pubDate.toISOString().slice(0, 10)}</p>
            <p>{post.data.description}</p>
          </li>
        ))}
      </ul>
    )}
  </section>
</BaseLayout>
"""
    plan_file("src/pages/blog/index.astro", blog_index_astro)

    blog_slug_astro = """---
import { getCollection, render } from "astro:content";
import BlogPostLayout from "../../layouts/BlogPostLayout.astro";
import { isPublished } from "../../lib/published";

export async function getStaticPaths() {
  const posts = await getCollection("blog", isPublished);
  return posts.map((post) => ({
    params: { id: post.id },
    props: { post },
  }));
}

const { post } = Astro.props;
const { Content } = await render(post);
---
<BlogPostLayout
  title={post.data.title}
  description={post.data.description}
  pubDate={post.data.pubDate}
  updatedDate={post.data.updatedDate}
  author={post.data.author}
  tags={post.data.tags}
  heroImage={post.data.heroImage}
  heroAlt={post.data.heroAlt}
  canonicalUrl={post.data.canonicalUrl}
>
  <Content />
</BlogPostLayout>
"""
    plan_file("src/pages/blog/[...id].astro", blog_slug_astro)

    rss_xml_ts = """import type { APIRoute } from "astro";
import { getCollection } from "astro:content";
import { isPublished } from "../lib/published";
import { SITE_CONFIG, formatRoute } from "../lib/site";

function escapeXml(value: string): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

export const GET: APIRoute = async (context) => {
  const site = context.site ?? new URL(SITE_CONFIG.siteUrl || "https://example.invalid");
  const posts = (await getCollection("blog", isPublished)).sort(
    (a, b) => b.data.pubDate.valueOf() - a.data.pubDate.valueOf(),
  );

  const itemsXml = posts
    .map((post) => {
      const link = new URL(formatRoute(`/blog/${post.id}`), site).href;
      return [
        "    <item>",
        `      <title>${escapeXml(post.data.title)}</title>`,
        `      <link>${escapeXml(link)}</link>`,
        `      <guid isPermaLink="true">${escapeXml(link)}</guid>`,
        `      <pubDate>${escapeXml(post.data.pubDate.toUTCString())}</pubDate>`,
        `      <description>${escapeXml(post.data.description)}</description>`,
        "    </item>",
      ].join("\\n");
    })
    .join("\\n");

  const xml = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<rss version="2.0">',
    "  <channel>",
    `    <title>${escapeXml(SITE_CONFIG.name)}</title>`,
    `    <link>${escapeXml(site.href)}</link>`,
    `    <description>${escapeXml(SITE_CONFIG.defaultDescription)}</description>`,
    itemsXml,
    "  </channel>",
    "</rss>",
    "",
  ].join("\\n");

  return new Response(xml, {
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
    },
  });
};
"""
    plan_file("src/pages/rss.xml.ts", rss_xml_ts)

    page_404 = """---
import BaseLayout from "../layouts/BaseLayout.astro";
---
<BaseLayout title="404 Not Found" robots="noindex, nofollow">
  <section class="not-found">
    <h1>404 Not Found</h1>
    <p>The page you requested could not be found.</p>
    <p><a href="/">Return home</a></p>
  </section>
</BaseLayout>
"""
    plan_file("src/pages/404.astro", page_404)

    if staging_lock:
        plan_file("public/robots.txt", _staging_robots_txt())
        plan_file("public/_headers", _staging_headers_txt())
    else:
        robots_lines = ["User-agent: *", "Allow: /"]
        if eff_site and enable_sitemap:
            robots_lines.append(f"Sitemap: {eff_site.rstrip('/')}/sitemap-index.xml")
        plan_file("public/robots.txt", "\n".join(robots_lines) + "\n")

    global_css = """*,
*::before,
*::after {
  box-sizing: border-box;
}

:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --surface: #f6f7f9;
  --fg: #111827;
  --muted: #4b5563;
  --border: #e5e7eb;
  --accent: #1d4ed8;
  --accent-fg: #ffffff;
  --max-width: 72rem;
  --measure: 68ch;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115;
    --surface: #171a21;
    --fg: #f3f4f6;
    --muted: #9ca3af;
    --border: #2a2f3a;
    --accent: #60a5fa;
    --accent-fg: #0b1120;
  }
}

body {
  margin: 0;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.65;
  background: var(--bg);
  color: var(--fg);
}

a {
  color: var(--accent);
}

img {
  max-width: 100%;
  height: auto;
  display: block;
}

pre {
  overflow-x: auto;
  padding: 1rem;
  border-radius: 0.5rem;
  background: var(--surface);
  border: 1px solid var(--border);
}

code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}

.container {
  width: min(100% - 2rem, var(--max-width));
  margin-inline: auto;
}

.site-header {
  border-bottom: 1px solid var(--border);
  padding-block: 1rem;
}

.header-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.brand {
  font-weight: 700;
  text-decoration: none;
  color: var(--fg);
}

nav a {
  text-decoration: none;
  margin-left: 1rem;
}

.main-content {
  padding-block: 2.5rem;
}

.hero {
  max-width: var(--measure);
  margin-bottom: 2.5rem;
}

.kicker {
  font-size: 0.875rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 0.5rem;
}

.lead {
  font-size: 1.125rem;
  color: var(--muted);
}

.button {
  display: inline-block;
  padding: 0.625rem 1.125rem;
  border-radius: 0.375rem;
  background: var(--accent);
  color: var(--accent-fg);
  text-decoration: none;
  font-weight: 600;
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
  gap: 1rem;
  margin-bottom: 2.5rem;
}

.card {
  padding: 1.25rem;
  border-radius: 0.5rem;
  background: var(--surface);
  border: 1px solid var(--border);
}

.section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
}

.post-list {
  list-style: none;
  padding: 0;
  margin: 1rem 0 0;
  display: grid;
  gap: 1.25rem;
}

.post-item {
  padding-bottom: 1.25rem;
  border-bottom: 1px solid var(--border);
}

.post-meta {
  font-size: 0.9rem;
  color: var(--muted);
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  list-style: none;
  padding: 0;
  margin: 0.75rem 0 0;
}

.tag-pill {
  font-size: 0.8rem;
  padding: 0.2rem 0.6rem;
  border-radius: 999px;
  background: var(--surface);
  border: 1px solid var(--border);
}

.post-article {
  max-width: var(--measure);
}

.post-hero {
  margin: 1.5rem 0;
}

.site-footer {
  border-top: 1px solid var(--border);
  padding-block: 1.5rem;
  color: var(--muted);
  font-size: 0.9rem;
}
"""
    plan_file("src/styles/global.css", global_css)

    welcome_md = (
        "---\n"
        f"title: {json.dumps(copy['welcomeTitle'], ensure_ascii=False)}\n"
        f"description: {json.dumps(copy['welcomeDescription'], ensure_ascii=False)}\n"
        'pubDate: "2026-11-25"\n'
        f"author: {json.dumps(project_display_name, ensure_ascii=False)}\n"
        f"categories: {json.dumps([copy['welcomeCategory']], ensure_ascii=False)}\n"
        f"tags: {json.dumps(copy['welcomeTags'], ensure_ascii=False)}\n"
        "draft: false\n"
        "---\n\n"
        f"{copy['welcomeBody']}"
    )
    plan_file("src/content/blog/welcome.md", welcome_md, draft=False, legacyRoute="/blog/welcome/")
    plan_file("src/content/pages/.gitkeep", "")

    guidelines_md = f"""# Astro Build Guidelines ({project_display_name})

These rules govern build, publishing, and release verification for this Astro v{ASTRO_TARGET_VERSION} project.

## 1. Local Astro Invocation

- Always run the project-local Astro CLI with telemetry disabled:
  `ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build`
- Never invoke a global `astro` command on PATH.
- Requires Node `{ASTRO_ENGINE_MIN_NODE}` (LTS 24.x recommended).

## 2. Astro v7 Content Layer Contract

- Collections live in `src/content.config.ts` using `glob()` from `astro/loaders` and `z` from `astro/zod`.
- Reference entries by `entry.id` (never legacy `entry.slug`) and render via `render(entry)` from `astro:content`.
- Filter all production listings, RSS feeds, and `getStaticPaths()` through `isPublished()` in `src/lib/published.ts` so both `draft: true` and future-dated `pubDate` entries are excluded from production builds.

## 3. Routing, RSS, and Sitemap Rules

- Configured `trailingSlash`: `{trailing_slash}` (`build.format: "{build_format}"`).
- Format internal blog links through `formatRoute()` in `src/lib/site.ts`.
- Set `site` in `astro.config.mjs` before enabling production sitemaps or canonical URLs.
- Keep `<title>`, `<meta name="description">`, single `<h1>` per page (rendered by `BlogPostLayout.astro`; do not add a duplicate `# H1` in Markdown bodies), and valid JSON-LD.

## 4. Day-2 Publishing and Regression Verification

- Create, update, or safely rename blog posts via `legends-astro post --project . ...` (always preview `planSha256` before `--apply --confirm-plan`).
- After every `astro build`, run:
  `legends-astro verify-build --dist ./dist --project . --baseline site-manifest.json --write-manifest site-manifest.json --overwrite --env {"staging" if staging_lock else "production"}`
- When renaming a published slug, keep the `301` redirect in `public/_redirects` so baseline route verification passes without broken URLs.
"""
    plan_file("ASTRO-BUILD-GUIDELINES.md", guidelines_md)


def _staging_robots_txt() -> str:
    return "User-agent: *\nDisallow: /\n"


def _staging_headers_txt() -> str:
    return "/*\n  X-Robots-Tag: noindex, nofollow, noarchive, nosnippet\n"


def _collect_clone_pages(source_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load raw HTML pages from a raw-capture/ directory or arbitrary HTML source directory/file."""
    contract = audit_source(source_path)
    pages_out: list[dict[str, Any]] = []

    manifest_path = None
    if source_path.is_dir():
        for cand in (source_path / "manifest.json", source_path / "raw-capture" / "manifest.json"):
            if cand.is_file() and not is_reparse_or_symlink(cand):
                manifest_path = cand
                break

    if manifest_path is not None:
        mdata = json.loads(safe_read_bytes(manifest_path, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8"))
        if mdata.get("schema") == "legends.astro-raw-capture/v1":
            capture_root = manifest_path.parent
            for item in mdata.get("pages", []):
                rel_file = item.get("file", "")
                hp = capture_root / rel_file
                assert_path_within(capture_root, hp)
                raw_html = safe_read_bytes(hp, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8", errors="replace")
                pages_out.append(
                    {
                        "slug": item.get("slug") or route_to_capture_slug(item.get("route", "/")),
                        "route": item.get("route", "/"),
                        "urlShape": item.get("urlShape", "clean"),
                        "html": raw_html,
                        "status": "publish",
                        "alternateRoutes": [],
                    }
                )
            if pages_out:
                return pages_out, contract

    # Fallback: read HTML files directly from source_path
    root_dir = source_path if source_path.is_dir() else source_path.parent
    used: set[str] = set()
    for pinfo in contract.get("pages", []):
        rel = pinfo.get("sourceFile", "")
        raw_html = ""
        if rel:
            cand = root_dir / rel if source_path.is_dir() else source_path
            if cand.is_file() and not is_reparse_or_symlink(cand) and cand.suffix.lower() in (".html", ".htm"):
                assert_path_within(root_dir, cand)
                raw_html = safe_read_bytes(cand, max_bytes=DEFAULT_MAX_PAGE_BYTES).decode("utf-8", errors="replace")
        if not raw_html:
            title_esc = html.escape(str(pinfo.get("title") or pinfo.get("route") or "Page"))
            desc_meta = (
                f'<meta name="description" content="{html.escape(str(pinfo["metaDescription"]), quote=True)}">'
                if pinfo.get("metaDescription")
                else ""
            )
            body_md = html.escape(str(pinfo.get("markdownBody") or pinfo.get("bodyExcerpt") or ""))
            raw_html = f"<!doctype html><html><head><title>{title_esc}</title>{desc_meta}</head><body><main><p>{body_md}</p></main></body></html>"

        route = str(pinfo.get("route") or "/")
        slug = route_to_capture_slug(route, used)
        pages_out.append(
            {
                "slug": slug,
                "route": route,
                "urlShape": pinfo.get("urlShape", "clean"),
                "html": raw_html,
                "status": pinfo.get("status", "publish"),
                "alternateRoutes": pinfo.get("alternateRoutes", []),
            }
        )
    return pages_out, contract


def scaffold_astro_project(
    *,
    source_path: str | Path | None = None,
    output_dir: str | Path,
    mode: str = "clone",
    asset_map_path: str | Path | None = None,
    decode_cf_email: bool = False,
    staging_lock: bool = False,
    strip_trackers_flag: bool = False,
    site_url: str | None = None,
    name: str | None = None,
    preset: str = "agency",
    trailing_slash: str = "always",
    with_sitemap: bool | None = None,
    apply: bool = False,
    confirm_plan: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Preview or write an Astro v7.4.13 project in --mode clone, editorial, or starter."""
    if mode not in ("clone", "editorial", "starter"):
        raise ValueError(f"Unsupported scaffold mode '{mode}'; expected 'clone', 'editorial', or 'starter'.")

    raw_out = Path(output_dir).expanduser()
    reject_symlink_chain(raw_out)
    out = raw_out.resolve()
    reject_controlled_folder_access(out)

    src: Path | None = None
    if mode in ("clone", "editorial"):
        if not source_path:
            raise ValueError(f"--source is required when --mode is '{mode}'.")
        raw_src = Path(source_path).expanduser()
        reject_symlink_chain(raw_src)
        src = raw_src.resolve()
        if not src.exists():
            raise FileNotFoundError(f"Source path does not exist: {src}")
    else:
        if preset not in STARTER_PRESETS:
            raise ValueError(f"Unsupported --preset '{preset}'; expected one of {', '.join(STARTER_PRESETS)}.")
        if trailing_slash not in TRAILING_SLASH_MODES:
            raise ValueError(
                f"Unsupported --trailing-slash '{trailing_slash}'; expected one of {', '.join(TRAILING_SLASH_MODES)}."
            )

    asset_map, asset_origin_url, root_relative_map = (
        _load_asset_map_bundle(asset_map_path) if mode in ("clone", "editorial") else ({}, None, {})
    )
    files_to_write: list[tuple[str, str]] = []
    planned_meta: list[dict[str, Any]] = []

    def plan_file(rel_posix: str, content: str, **extra: Any) -> None:
        files_to_write.append((rel_posix, content))
        item: dict[str, Any] = {"path": rel_posix, "bytes": len(content.encode("utf-8"))}
        item.update(extra)
        planned_meta.append(item)

    starter_meta: dict[str, Any] = {}
    contract: dict[str, Any] = {}

    if mode == "starter":
        eff_site = _validate_site_url(site_url)
        project_display_name = (
            re.sub(r"[\r\n\t]+", " ", str(name if name is not None else out.name)).strip() or "Agency Site"
        )
        build_format = {"always": "directory", "never": "file", "ignore": "preserve"}[trailing_slash]
        if with_sitemap is True:
            enable_sitemap = True
        elif with_sitemap is False:
            enable_sitemap = False
        else:
            enable_sitemap = bool(eff_site) and not staging_lock

        _plan_starter_project(
            out=out,
            plan_file=plan_file,
            project_display_name=project_display_name,
            preset=preset,
            eff_site=eff_site,
            trailing_slash=trailing_slash,
            build_format=build_format,
            staging_lock=staging_lock,
            enable_sitemap=enable_sitemap,
        )
        starter_meta = {
            "name": project_display_name,
            "preset": preset,
            "site": eff_site,
            "trailingSlash": trailing_slash,
            "withSitemap": enable_sitemap,
            "recommendedAstroConfig": {
                "output": "static",
                "trailingSlash": trailing_slash,
                "build.format": build_format,
            },
        }
    elif mode == "clone":
        assert src is not None
        clone_pages, contract = _collect_clone_pages(src)
        rec_cfg = contract.get("recommendedAstroConfig", {})
        build_format = str(rec_cfg.get("build.format", "directory"))
        trailing_slash = str(rec_cfg.get("trailingSlash", "ignore"))
        eff_site = site_url or contract.get("baseUrl")

        plan_file("package.json", _package_json_content(out.name))
        plan_file(
            "astro.config.mjs",
            _astro_config_content(
                build_format=build_format,
                trailing_slash=trailing_slash,
                site_url=eff_site,
                editorial=False,
            ),
        )

        clone_content_config = """import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

const pages = defineCollection({
  loader: glob({
    pattern: "**/*.{md,mdx}",
    base: "./src/content/pages",
  }),
  schema: z.object({
    title: z.string(),
    route: z.string(),
    slugParam: z.string().nullable(),
    htmlAttrs: z.record(z.string(), z.string()).default({}),
    bodyAttrs: z.record(z.string(), z.string()).default({}),
    headHtml: z.string().default(""),
    bodyHtml: z.string().default(""),
    draft: z.boolean().optional(),
  }),
});

export const collections = { pages };
"""
        plan_file("src/content.config.ts", clone_content_config)
        plan_file("src/lib/published.ts", _published_helper_content())

        legacy_layout = """---
export interface Props {
  htmlAttrs?: Record<string, string>;
  bodyAttrs?: Record<string, string>;
  headHtml?: string;
  bodyHtml?: string;
}

const {
  htmlAttrs = {},
  bodyAttrs = {},
  headHtml = "",
  bodyHtml = "",
} = Astro.props;
---
<!doctype html>
<html {...htmlAttrs}>
  <head>
    <Fragment set:html={headHtml} />
  </head>
  <body {...bodyAttrs}>
    <Fragment set:html={bodyHtml} />
  </body>
</html>
"""
        plan_file("src/layouts/LegacyDocument.astro", legacy_layout)

        catch_all_page = """---
import { getCollection } from "astro:content";
import LegacyDocument from "../layouts/LegacyDocument.astro";
import { isPublished } from "../lib/published";

export async function getStaticPaths() {
  const entries = await getCollection("pages", isPublished);
  return entries
    .filter((entry) => entry.id !== "404" && entry.data.route !== "/404" && entry.data.route !== "/404.html")
    .map((entry) => ({
      params: { slug: entry.data.slugParam ?? undefined },
      props: { entry },
    }));
}

const { entry } = Astro.props;
---
<LegacyDocument
  htmlAttrs={entry.data.htmlAttrs}
  bodyAttrs={entry.data.bodyAttrs}
  headHtml={entry.data.headHtml}
  bodyHtml={entry.data.bodyHtml}
/>
"""
        plan_file("src/pages/[...slug].astro", catch_all_page)

        redirects_lines: list[str] = []
        captured_404: dict[str, Any] | None = None

        for page in clone_pages:
            raw_html = page["html"]
            if asset_map or root_relative_map:
                raw_html = rewrite_html_assets(
                    raw_html,
                    asset_map,
                    origin_url=asset_origin_url or eff_site,
                    root_relative_map=root_relative_map,
                )
            if decode_cf_email:
                raw_html = decode_cloudflare_emails(raw_html)
            if strip_trackers_flag:
                raw_html = strip_trackers(raw_html)

            html_attrs, body_attrs = extract_root_attrs(raw_html)
            head_html, body_html = _extract_head_and_body(raw_html)
            if staging_lock:
                head_html = _inject_staging_noindex(head_html)

            title_bodies = extract_html_tag_bodies(head_html, "title")
            title_val = html.unescape(" ".join(title_bodies[0].split())) if title_bodies else page["slug"]

            route, _shape = normalize_route_and_shape(page["route"])
            is_404 = route in ("/404", "/404/", "/404.html") or page["slug"] == "404"

            if route == "/":
                slug_param: str | None = None
            else:
                sp = route.strip("/")
                if sp.lower().endswith(".html"):
                    sp = sp[:-5]
                elif sp.lower().endswith(".htm"):
                    sp = sp[:-4]
                slug_param = sp or None

            if is_404:
                captured_404 = {
                    "htmlAttrs": html_attrs,
                    "bodyAttrs": body_attrs,
                    "headHtml": head_html,
                    "bodyHtml": body_html,
                }
                continue

            is_draft = page.get("status") == "draft"
            if not is_draft:
                for alt in page.get("alternateRoutes", []):
                    clean_alt = re.sub(r"[\r\n\s]+", "", str(alt or ""))
                    if clean_alt and clean_alt != route:
                        redirects_lines.append(f"{clean_alt} {route} 301")

            fm_lines = [
                "---",
                f"title: {json.dumps(title_val, ensure_ascii=False)}",
                f"route: {json.dumps(route, ensure_ascii=False)}",
                f"slugParam: {json.dumps(slug_param, ensure_ascii=False)}",
                f"htmlAttrs: {json.dumps(html_attrs, ensure_ascii=False)}",
                f"bodyAttrs: {json.dumps(body_attrs, ensure_ascii=False)}",
                f"headHtml: {json.dumps(head_html, ensure_ascii=False)}",
                f"bodyHtml: {json.dumps(body_html, ensure_ascii=False)}",
            ]
            if is_draft:
                fm_lines.append("draft: true")
            fm_lines.append("---")
            fm_lines.append("")
            md_content = "\n".join(fm_lines)
            plan_file(
                f"src/content/pages/{page['slug']}.md",
                md_content,
                draft=is_draft,
                legacyRoute=route,
            )

        if captured_404 is not None:
            page_404 = (
                "---\n"
                'import LegacyDocument from "../layouts/LegacyDocument.astro";\n'
                f"const htmlAttrs = {json.dumps(captured_404['htmlAttrs'], ensure_ascii=False)};\n"
                f"const bodyAttrs = {json.dumps(captured_404['bodyAttrs'], ensure_ascii=False)};\n"
                f"const headHtml = {json.dumps(captured_404['headHtml'], ensure_ascii=False)};\n"
                f"const bodyHtml = {json.dumps(captured_404['bodyHtml'], ensure_ascii=False)};\n"
                "---\n"
                "<LegacyDocument htmlAttrs={htmlAttrs} bodyAttrs={bodyAttrs} headHtml={headHtml} bodyHtml={bodyHtml} />\n"
            )
        else:
            has_clone_root = any(
                normalize_route_and_shape(p["route"])[0] == "/" and p.get("status") != "draft"
                for p in clone_pages
            )
            noindex_meta = '<meta name="robots" content="noindex, nofollow">'
            home_link_html = '<p><a href="/">Return home</a></p>' if has_clone_root else ""
            page_404 = (
                "---\n"
                'import LegacyDocument from "../layouts/LegacyDocument.astro";\n'
                "---\n"
                "<LegacyDocument\n"
                '  htmlAttrs={{ lang: "en" }}\n'
                f'  headHtml={{`<meta charset="utf-8" /><title>404 Not Found</title>{noindex_meta}`}}\n'
                f'  bodyHtml={{`<main><h1>404 Not Found</h1>{home_link_html}</main>`}}\n'
                "/>\n"
            )
        plan_file("src/pages/404.astro", page_404)

        if redirects_lines:
            plan_file("public/_redirects", "\n".join(redirects_lines) + "\n", ruleCount=len(redirects_lines))
        if staging_lock:
            plan_file("public/robots.txt", _staging_robots_txt())
            plan_file("public/_headers", _staging_headers_txt())
        plan_file("migration-contract.json", json.dumps(contract, indent=2) + "\n")

    else:
        # --mode editorial
        assert src is not None
        contract = audit_source(src)
        rec_cfg = contract.get("recommendedAstroConfig", {})
        build_format = str(rec_cfg.get("build.format", "directory"))
        trailing_slash = str(rec_cfg.get("trailingSlash", "always"))
        eff_site = site_url or contract.get("baseUrl")

        plan_file("package.json", _package_json_content(out.name))
        plan_file(
            "astro.config.mjs",
            _astro_config_content(
                build_format=build_format,
                trailing_slash=trailing_slash,
                site_url=eff_site,
                editorial=True,
            ),
        )

        editorial_content_config = """import { defineCollection } from "astro:content";
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
    slugParam: z.string().nullable().optional(),
    postType: z.string().default("page"),
    author: z.string().optional(),
    categories: z.array(z.string()).default([]),
    tags: z.array(z.string()).default([]),
    featuredImage: z.string().optional(),
    canonicalUrl: z.string().optional(),
    openGraph: z.record(z.string(), z.string()).default({}),
    jsonLd: z.array(z.unknown()).default([]),
    pubDate: z.coerce.date().optional(),
    draft: z.boolean().optional(),
  }),
});

export const collections = { pages };
"""
        plan_file("src/content.config.ts", editorial_content_config)
        plan_file("src/lib/published.ts", _published_helper_content())

        staging_meta = (
            '    <meta name="robots" content="noindex, nofollow, noarchive, nosnippet" />\n'
            if staging_lock
            else ""
        )
        base_layout = f"""---
export interface Props {{
  title: string;
  description?: string;
  canonicalUrl?: string;
  featuredImage?: string;
  openGraph?: Record<string, string>;
  jsonLd?: unknown[];
}}

const {{
  title,
  description,
  canonicalUrl,
  featuredImage,
  openGraph = {{}},
  jsonLd = [],
}} = Astro.props;
const ogEntries = Object.entries(openGraph);
---
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
{staging_meta}    <title>{{title}}</title>
    {{description && <meta name="description" content={{description}} />}}
    {{canonicalUrl && <link rel="canonical" href={{canonicalUrl}} />}}
    {{ogEntries.map(([prop, val]) => <meta property={{prop}} content={{val}} />)}}
    {{featuredImage && !openGraph["og:image"] && <meta property="og:image" content={{featuredImage}} />}}
    {{jsonLd.map((block) => (
      <script type="application/ld+json" set:html={{JSON.stringify(block).replace(/</g, "\\\\u003c")}} />
    ))}}
  </head>
  <body>
    <main>
      <slot />
    </main>
  </body>
</html>
"""
        plan_file("src/layouts/BaseLayout.astro", base_layout)

        has_editorial_root = any(
            (re.sub(r"[\r\n\s]+", "", str(p.get("route", "/"))) or "/") == "/" and p.get("status") != "draft"
            for p in contract.get("pages", [])
        )

        editorial_slug_page = """---
import { getCollection, render } from "astro:content";
import BaseLayout from "../layouts/BaseLayout.astro";
import { isPublished } from "../lib/published";

export async function getStaticPaths() {
  const entries = await getCollection("pages", isPublished);
  return entries
    .filter(
      (entry) =>
        entry.id !== "404" &&
        entry.data.legacyRoute !== "/404" &&
        entry.data.legacyRoute !== "/404/" &&
        entry.data.legacyRoute !== "/404.html" &&
        (entry.data.slugParam !== null || entry.data.legacyRoute === "/"),
    )
    .map((entry) => ({
      params: { slug: entry.data.slugParam ?? undefined },
      props: { entry },
    }));
}

const { entry } = Astro.props;
const { Content } = await render(entry);
---
<BaseLayout
  title={entry.data.title}
  description={entry.data.description}
  canonicalUrl={entry.data.canonicalUrl}
  featuredImage={entry.data.featuredImage}
  openGraph={entry.data.openGraph}
  jsonLd={entry.data.jsonLd}
>
  <article>
    <Content />
  </article>
</BaseLayout>
"""
        plan_file("src/pages/[...slug].astro", editorial_slug_page)

        if not has_editorial_root:
            editorial_index = """---
import { getCollection } from "astro:content";
import BaseLayout from "../layouts/BaseLayout.astro";
import { isPublished } from "../lib/published";

const entries = (await getCollection("pages", isPublished)).sort((a, b) =>
  a.data.title.localeCompare(b.data.title),
);
---
<BaseLayout title="Migrated Content Index">
  <h1>Migrated Content</h1>
  <ul>
    {entries.map((entry) => (
      <li>
        <a href={entry.data.legacyRoute}>{entry.data.title}</a>
      </li>
    ))}
  </ul>
</BaseLayout>
"""
            plan_file("src/pages/index.astro", editorial_index)

        redirects_lines = []
        used_slugs: set[str] = set()
        captured_editorial_404: dict[str, Any] | None = None
        for idx, page in enumerate(contract.get("pages", [])):
            route, _shape = normalize_route_and_shape(
                re.sub(r"[\r\n\s]+", "", str(page.get("route", "/"))) or "/"
            )
            raw_slug = page.get("slug") or route_to_capture_slug(route)
            is_404 = route in ("/404", "/404/", "/404.html") or str(raw_slug).strip() == "404"

            title_raw = str(page.get("title") or raw_slug or "404 Not Found").replace("\r", " ").replace("\n", " ").strip()
            desc_raw = str(page.get("metaDescription") or "").replace("\r", " ").replace("\n", " ").strip()
            og_raw = dict(page.get("openGraph") or {})
            if (asset_map or root_relative_map) and og_raw:
                for ok_k, ok_v in list(og_raw.items()):
                    if ok_k in ("og:image", "og:image:url", "og:image:secure_url"):
                        og_raw[ok_k] = rewrite_html_assets(
                            str(ok_v),
                            asset_map,
                            origin_url=asset_origin_url or eff_site,
                            root_relative_map=root_relative_map,
                        )
            json_ld_raw = [b for b in (page.get("jsonLd") or []) if not (isinstance(b, dict) and "_raw" in b)]

            if is_404:
                captured_editorial_404 = {
                    "title": title_raw or "404 Not Found",
                    "description": desc_raw or None,
                    "canonicalUrl": str(page["canonical"]).replace("\r", "").replace("\n", "").strip()
                    if page.get("canonical")
                    else None,
                    "openGraph": og_raw,
                    "jsonLd": json_ld_raw,
                }
                continue

            base_slug = re.sub(r"[^A-Za-z0-9_-]", "-", str(raw_slug)).strip("-") or f"page-{idx}"
            slug = base_slug
            counter = 2
            while slug in used_slugs:
                slug = f"{base_slug}-{counter}"
                counter += 1
            used_slugs.add(slug)

            if route == "/":
                slug_param = None
            else:
                sp = route.strip("/")
                if sp.lower().endswith(".html"):
                    sp = sp[:-5]
                slug_param = sp or None

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
            fm_lines.append(f"slugParam: {json.dumps(slug_param, ensure_ascii=False)}")
            if page.get("postType"):
                fm_lines.append(f"postType: {json.dumps(str(page['postType']), ensure_ascii=False)}")
            if page.get("author"):
                fm_lines.append(f"author: {json.dumps(str(page['author']), ensure_ascii=False)}")
            if page.get("categories"):
                fm_lines.append(f"categories: {json.dumps(page['categories'], ensure_ascii=False)}")
            if page.get("tags"):
                fm_lines.append(f"tags: {json.dumps(page['tags'], ensure_ascii=False)}")
            if page.get("featuredImage"):
                feat = str(page["featuredImage"])
                if asset_map or root_relative_map:
                    feat = rewrite_html_assets(
                        feat,
                        asset_map,
                        origin_url=asset_origin_url or eff_site,
                        root_relative_map=root_relative_map,
                    )
                fm_lines.append(f"featuredImage: {json.dumps(feat, ensure_ascii=False)}")
            if page.get("canonical"):
                canon_raw = str(page["canonical"]).replace("\r", "").replace("\n", "").strip()
                fm_lines.append(f"canonicalUrl: {json.dumps(canon_raw, ensure_ascii=False)}")
            if og_raw:
                fm_lines.append(f"openGraph: {json.dumps(og_raw, ensure_ascii=False)}")
            if json_ld_raw:
                fm_lines.append(f"jsonLd: {json.dumps(json_ld_raw, ensure_ascii=False)}")
            if page.get("pubDate"):
                fm_lines.append(f"pubDate: {json.dumps(str(page['pubDate']).strip(), ensure_ascii=False)}")
            if is_draft:
                fm_lines.append("draft: true")
            fm_lines.append("---")
            fm_lines.append("")

            md_body = (page.get("markdownBody") or "").strip()
            if (asset_map or root_relative_map) and md_body:
                md_body = rewrite_html_assets(
                    md_body,
                    asset_map,
                    origin_url=asset_origin_url or eff_site,
                    root_relative_map=root_relative_map,
                )
            if md_body:
                if not md_body.startswith("#"):
                    h1s = page.get("headings", {}).get("h1", [])
                    heading_raw = str(h1s[0] if h1s else title_raw)
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
                safe_heading = (
                    title_raw.replace("\r", " ")
                    .replace("\n", " ")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .strip()
                )
                fm_lines.append(f"# {safe_heading}")
                fm_lines.append("")
                if page.get("bodyExcerpt"):
                    safe_excerpt = str(page["bodyExcerpt"]).replace("<", "&lt;").replace(">", "&gt;")
                    fm_lines.append(safe_excerpt)
                    fm_lines.append("")

            plan_file(
                f"src/content/pages/{slug}.md",
                "\n".join(fm_lines),
                draft=is_draft,
                legacyRoute=route,
                postType=page.get("postType", "page"),
            )

        if captured_editorial_404 is not None:
            editorial_404 = (
                "---\n"
                'import BaseLayout from "../layouts/BaseLayout.astro";\n'
                f"const title = {json.dumps(captured_editorial_404['title'], ensure_ascii=False)};\n"
                f"const description = {json.dumps(captured_editorial_404['description'], ensure_ascii=False)};\n"
                f"const canonicalUrl = {json.dumps(captured_editorial_404['canonicalUrl'], ensure_ascii=False)};\n"
                f"const openGraph = {json.dumps(captured_editorial_404['openGraph'], ensure_ascii=False)};\n"
                f"const jsonLd = {json.dumps(captured_editorial_404['jsonLd'], ensure_ascii=False)};\n"
                "---\n"
                "<BaseLayout\n"
                "  title={title}\n"
                "  description={description ?? undefined}\n"
                "  canonicalUrl={canonicalUrl ?? undefined}\n"
                "  openGraph={openGraph}\n"
                "  jsonLd={jsonLd}\n"
                ">\n"
                "  <h1>{title}</h1>\n"
                '  <p><a href="/">Return home</a></p>\n'
                "</BaseLayout>\n"
            )
        else:
            editorial_404 = """---
import BaseLayout from "../layouts/BaseLayout.astro";
---
<BaseLayout title="404 Not Found">
  <h1>404 Not Found</h1>
  <p><a href="/">Return home</a></p>
</BaseLayout>
"""
        plan_file("src/pages/404.astro", editorial_404)

        if redirects_lines:
            plan_file("public/_redirects", "\n".join(redirects_lines) + "\n", ruleCount=len(redirects_lines))
        if staging_lock:
            plan_file("public/robots.txt", _staging_robots_txt())
            plan_file("public/_headers", _staging_headers_txt())
        plan_file("migration-contract.json", json.dumps(contract, indent=2) + "\n")

    if mode == "starter":
        plan_canonical = json.dumps(
            {
                "mode": "starter",
                "outputDir": str(out),
                "name": starter_meta["name"],
                "preset": starter_meta["preset"],
                "site": starter_meta["site"],
                "trailingSlash": starter_meta["trailingSlash"],
                "stagingLock": staging_lock,
                "withSitemap": starter_meta["withSitemap"],
                "plannedFiles": [
                    {
                        "path": item["path"],
                        "bytes": item["bytes"],
                        "draft": item.get("draft"),
                        "legacyRoute": item.get("legacyRoute"),
                    }
                    for item in planned_meta
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        plan_canonical = json.dumps(
            {
                "mode": mode,
                "sourcePath": str(src),
                "outputDir": str(out),
                "stagingLock": staging_lock,
                "decodeCfEmail": decode_cf_email,
                "stripTrackers": strip_trackers_flag,
                "plannedFiles": [
                    {
                        "path": item["path"],
                        "bytes": item["bytes"],
                        "draft": item.get("draft"),
                        "legacyRoute": item.get("legacyRoute"),
                    }
                    for item in planned_meta
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    plan_sha256 = hashlib.sha256(plan_canonical.encode("utf-8")).hexdigest()

    if apply:
        if not confirm_plan or confirm_plan.strip().lower() != plan_sha256.lower():
            raise ValueError(
                f"Refusing scaffold --apply without matching --confirm-plan {plan_sha256} (run preview first to obtain planSha256)."
            )
        for rel_posix, content in files_to_write:
            dest = out / rel_posix
            assert_path_within(out, dest)
            safe_write_text(dest, content, overwrite=overwrite)

    if mode == "starter":
        return {
            "ok": True,
            "mode": "starter",
            "applied": apply,
            "planSha256": plan_sha256,
            "sourcePath": None,
            "outputDir": str(out),
            "name": starter_meta["name"],
            "preset": starter_meta["preset"],
            "site": starter_meta["site"],
            "trailingSlash": starter_meta["trailingSlash"],
            "stagingLock": staging_lock,
            "withSitemap": starter_meta["withSitemap"],
            "astroVersion": ASTRO_TARGET_VERSION,
            "recommendedAstroConfig": starter_meta["recommendedAstroConfig"],
            "plannedFileCount": len(planned_meta),
            "plannedFiles": planned_meta,
            "next": (
                f"Initialized Astro {ASTRO_TARGET_VERSION} starter ({starter_meta['preset']} preset). Run `npm install`, `ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build`, and `legends-astro verify-build --dist ./dist --project . --write-manifest site-manifest.json --env {'staging' if staging_lock else 'production'}`."
                if apply
                else f"Preview only (starter mode, {starter_meta['preset']} preset). Re-run with `--apply --confirm-plan {plan_sha256}` to write files."
            ),
        }

    return {
        "ok": True,
        "mode": mode,
        "applied": apply,
        "planSha256": plan_sha256,
        "sourcePath": str(src),
        "outputDir": str(out),
        "stagingLock": staging_lock,
        "decodeCfEmail": decode_cf_email,
        "stripTrackers": strip_trackers_flag,
        "astroVersion": ASTRO_TARGET_VERSION,
        "recommendedAstroConfig": contract.get("recommendedAstroConfig"),
        "plannedFileCount": len(planned_meta),
        "plannedFiles": planned_meta,
        "next": (
            f"Scaffolded Astro {ASTRO_TARGET_VERSION} project ({mode} mode). Run `npm install`, `ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build`, and `legends-astro verify-build --dist <dist> --project <dir> --contract migration-contract.json`."
            if apply
            else f"Preview only ({mode} mode). Re-run with `--apply --confirm-plan {plan_sha256}` to write files."
        ),
    }
