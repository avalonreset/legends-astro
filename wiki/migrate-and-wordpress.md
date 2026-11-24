# Migrate HTML, WordPress, or Firecrawl sites to Astro

Cross-read: [inspect-setup.md](inspect-setup.md), [pages-components-styles-scripts.md](pages-components-styles-scripts.md), [content-collections.md](content-collections.md), [performance-and-caching.md](performance-and-caching.md).
Vendored official docs:
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/index.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-wordpress.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/cms/wordpress.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-nextjs.mdx`

## Why migrations fail when done with raw agent prompts

When a user asks an AI agent to convert a WordPress page, static HTML directory, or scraped site to Astro, the agent typically rewrites markup from memory and declares victory once `astro build` succeeds. That routinely destroys:
1. **URL parity:** Changing `/about.html` to `/about/` or dropping trailing slashes without 301 redirects.
2. **SEO and structured data:** Losing `<title>`, `<meta name="description">`, `<link rel="canonical">`, OpenGraph tags, or Yoast/RankMath `application/ld+json` blocks.
3. **Client JavaScript DOM ABI:** Renaming or omitting `id="..."` attributes that existing scripts query with `document.getElementById()`.
4. **First-paint and CSS scoping:** Breaking dynamic JS-created elements with Astro's default scoped `<style>` or extracting inline first-paint styles.

`legends-astro` enforces a three-step closed loop so migrations are provably complete.

## Step 1: Capture & triage or extract the preservation contract (`capture` / `source-audit`)

For live WordPress, Webflow, or static HTML origins (or local `legends-firecrawl` directories), run `capture` to download or normalize HTML pages into `raw-capture/`, emit `migration-contract.json`, and generate a deterministic Migration Triage Report (platform, page builders, `[shortcodes]`, forms/Turnstile/reCAPTCHA, WooCommerce, Cloudflare email obfuscation, trackers, external CDNs):

```sh
python -m legends_astro capture \
  --url https://example.com \
  --from-sitemap --crawl --max-pages 50 \
  --output ./work

# Or normalize a local legends-firecrawl capture file or directory:
python -m legends_astro capture \
  --from-firecrawl ./firecrawl-captures \
  --output ./work
```

Or run `source-audit` directly against local HTML, WordPress WXR `.xml`, WordPress REST `.json`, or Firecrawl files/directories:

```sh
python -m legends_astro source-audit \
  --source ./legacy-site \
  --output ./migration-contract.json \
  --base-url https://example.com
```

`migration-contract.json` records every route, URL shape (`file`, `directory`, `clean`), `<title>` (with Yoast/RankMath title precedence), `<meta name="description">`, canonical link, OpenGraph map, JSON-LD count and `@type` list, Webflow `data-wf-page`, Custom Post Types, taxonomies, authors, `_thumbnail_id` featured images, required DOM `id` values referenced by JS, script tags, and converted Markdown (`<table>`, `<figure>`, `<iframe>`).

## Step 2: Localize remote assets (`vendor-assets`)

Run `vendor-assets` with explicit `--allow-host` entries (origin + WordPress/Webflow CDNs) to download Pass 1 HTML assets (`src`, `href`, `srcset`, `data-poster-url`, `data-video-urls`, inline `background-image`, `dynamic_css.php` -> `.css`) and Pass 2 recursive CSS `@import` / `url(...)` fonts and backgrounds into `public/` and `asset-map.json`:

```sh
python -m legends_astro vendor-assets \
  --source ./work/raw-capture \
  --output ./astro-site \
  --allow-host example.com \
  --allow-host cdn.prod.website-files.com \
  --dry-run
```

## Step 3: Scaffold Astro v7.4.13 project (`scaffold` or `ingest`)

Preview-first (`planSha256` -> `--apply --confirm-plan <sha256>`):
- Use `--mode clone` for exact DOM fidelity (`LegacyDocument.astro` with `<Fragment set:html={...} />`, `--asset-map`, `--decode-cf-email`, `--strip-trackers`, `--staging-lock`).
- Use `--mode editorial` for clean Markdown CMS pages preserving CPTs, categories, tags, authors, and featured images.
- Or use `ingest` for Content Layer collection-only scaffolding.

```sh
# 1. Preview generated files without writing:
python -m legends_astro scaffold --source ./work/raw-capture --output ./astro-site --mode clone --asset-map ./astro-site/asset-map.json --decode-cf-email --strip-trackers --staging-lock

# 2. Apply using the returned planSha256:
python -m legends_astro scaffold --source ./work/raw-capture --output ./astro-site --mode clone --asset-map ./astro-site/asset-map.json --decode-cf-email --strip-trackers --staging-lock --apply --confirm-plan <planSha256>
```

### WordPress-specific migration guidance

You have two architectural choices when moving from WordPress to Astro:
1. **Full Static Migration (decommissioning WordPress):**
   - Export WXR XML or pull `/wp-json/wp/v2/posts?per_page=100` and `/wp-json/wp/v2/pages?per_page=100`.
   - Run `legends-astro ingest --source <export> --output <astro-dir> --apply`.
   - Download `/wp-content/uploads/...` media files into `src/assets/` (for Astro `<Image />` optimization) or `public/wp-content/uploads/` (to keep existing image URLs intact on the first pass).
   - Match WordPress permalink structure (`/%postname%/` uses `build.format: 'directory'` and `trailingSlash: 'always'`).
2. **Headless WordPress (keeping WordPress as editor):**
   - Use Astro v7 Content Layer custom loaders or fetch from `https://<wp-site>/wp-json/wp/v2/posts` at build time inside a loader in `src/content.config.ts`. See `vendor/astro-docs/src/content/docs/en/guides/cms/wordpress.mdx`.

### Preserving an existing Node/backend request handler

When extracting static pages from an existing Node application (where `server.mjs` or an origin server handles auth, cookies, webhooks, CSP, and consent-gated analytics):
- Keep `output: 'static'` in Astro. You do not need `@astrojs/node` when the existing process remains the HTTP server.
- Emit inert HTML in `dist/` and let the request handler inject analytics only when consent cookies and Global Privacy Control (`Sec-GPC`) allow it.
- Allowlist `/_astro/*` for fingerprinted immutable assets with path-traversal protection (`dist/_astro`), and never expose the raw project root.

## Step 4: Build, prove parity (`verify-build`), and diff visual twins (`twin` / `twin-diff`)

Build with telemetry disabled, then run `verify-build` against `dist/` and `migration-contract.json`:

```sh
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs sync
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build
python -m legends_astro verify-build \
  --dist ./dist \
  --project . \
  --contract ./migration-contract.json \
  --forbid-host cdn.prod.website-files.com \
  --env staging
```

When Playwright is available, capture Visual Digital Twins and run offline `twin-diff` (zero detached servers; `--dist` serves via an ephemeral in-process `127.0.0.1:0` read-only server closed in `finally`):

```sh
python -m legends_astro twin --dist ./dist --output ./twin-candidate
python -m legends_astro twin-diff --baseline ./twin-baseline/twin.json --candidate ./twin-candidate/twin.json
```

`verify-build` exits non-zero if any published source route is missing, if any draft route or draft body text leaked into `dist/`, if any page lost its `<title>`, `<meta name="description">`, canonical, OG, JSON-LD `@type`, H1, or Webflow `data-wf-page`, if any required DOM `id` was dropped, if first-paint `<style is:inline>` order broke, if any CSS `url()`/`@import` asset or internal link 404s, if local assets carry stale `integrity=` SRI attributes, if `/cdn-cgi/` or `[shortcode]` leaked, or if staging/production environment locks fail.
