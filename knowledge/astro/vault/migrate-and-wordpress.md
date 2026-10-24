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

## Step 1: Extract the preservation contract (`source-audit`)

Run `source-audit` against the existing source before writing Astro code. Supported inputs:
- A directory of `.html` files (or a single `.html` file) plus its `.js` scripts
- A WordPress WXR XML export (`Tools -> Export` in WP Admin)
- A WordPress REST API JSON dump (`/wp-json/wp/v2/posts` or `/wp-json/wp/v2/pages`)
- A saved `legends-firecrawl` scrape/crawl JSON file

```sh
python -m legends_astro source-audit \
  --source ./legacy-site \
  --output ./migration-contract.json \
  --base-url https://example.com
```

`migration-contract.json` records every route, URL shape (`file`, `directory`, `clean`), `<title>`, `<meta name="description">`, canonical link, OpenGraph map, JSON-LD count and `@type` list, required DOM `id` values referenced by JS, script tags (`type="module"`, `defer`, `async`), and local assets. It also outputs `recommendedAstroConfig` (`build.format` and `trailingSlash`) to match the source URL structure.

## Step 2: Scaffold Content Layer collections (`ingest`)

For WordPress exports, `legends-firecrawl` scrapes, or content-heavy HTML directories, preview and apply `ingest`:

```sh
# 1. Preview generated files without writing:
python -m legends_astro ingest --source ./wordpress-export.xml --output ./astro-site

# 2. Apply to write src/content.config.ts, src/lib/published.ts, Markdown entries, and migration-contract.json:
python -m legends_astro ingest --source ./wordpress-export.xml --output ./astro-site --apply
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

## Step 3: Build and prove parity (`verify-build`)

Build with telemetry disabled, then run `verify-build` against `dist/` and `migration-contract.json`:

```sh
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs sync
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build
python -m legends_astro verify-build \
  --dist ./dist \
  --project . \
  --contract ./migration-contract.json
```

`verify-build` exits non-zero if any published source route is missing, if any draft route or draft body text leaked into `dist/`, if any page lost its `<title>`, `<meta name="description">`, or JSON-LD schema, if any required DOM `id` was dropped, if first-paint `<style is:inline>` order broke, or if any internal link or asset 404s in `dist/`.
