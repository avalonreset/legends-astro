# Migrate frameworks, WordPress, Webflow, HTML, or Firecrawl sites to Astro

Cross-read: [inspect-setup.md](inspect-setup.md), [pages-components-styles-scripts.md](pages-components-styles-scripts.md), [content-collections.md](content-collections.md), [performance-and-caching.md](performance-and-caching.md).
Vendored official migration guides (all searchable offline via `legends-astro docs-lookup`):
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/index.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-nextjs.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-nuxtjs.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-gatsby.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-create-react-app.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-docusaurus.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-hugo.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-jekyll.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-eleventy.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-sveltekit.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-vuepress.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-gitbook.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-gridsome.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-pelican.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/migrate-to-astro/from-wordpress.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/cms/wordpress.mdx`

## Why migrations fail when done with raw agent prompts

When a user asks an AI agent to convert an existing framework repository, WordPress site, static HTML directory, or scraped site to Astro, the agent typically rewrites markup from memory and declares victory once `astro build` succeeds. That routinely destroys:
1. **URL and redirect parity:** Changing `/about.html` to `/about/`, dropping trailing slashes, or losing `vercel.json`, `netlify.toml`, `next.config.*`, or `_redirects` rules.
2. **Island hydration and env boundaries:** Shipping unnecessary client JS for static components, omitting `client:*` on interactive hooks/components, or forgetting `NEXT_PUBLIC_*` / `NUXT_PUBLIC_*` / `GATSBY_*` / `VITE_*` / `REACT_APP_*` to `PUBLIC_*` renames.
3. **SEO and structured data:** Losing `<title>`, `<meta name="description">`, `<link rel="canonical">`, OpenGraph tags, or Yoast/RankMath `application/ld+json` blocks.
4. **Client JavaScript DOM ABI & first-paint CSS:** Renaming `id="..."` attributes queried by existing scripts, breaking dynamic JS-created elements with scoped `<style>`, or extracting inline first-paint styles.

`legends-astro` enforces a closed loop so both **source-repo migrations** and **live/CMS site migrations** are provably complete.

## Path A: Multi-framework source repository audit (`source-audit`)

Point `source-audit` directly at a local repository checkout (strictly read-only static inspection; never executes JS/TS/Ruby/Go configs and automatically skips `node_modules`, `.git`, `.next`, `.nuxt`, `dist`, `build`, `out`, `.cache`, `.astro`):

```sh
python -m legends_astro source-audit \
  --source ./my-legacy-repo \
  --output ./migration-contract.json
```

Auto-detected frameworks:
- **Next.js** (App Router `app/`, Pages Router `pages/`, or hybrid)
- **Nuxt** (`nuxt.config.*`, `pages/`, `server/api/`)
- **Gatsby** (`gatsby-config.*`, `src/pages/`)
- **Vite / Create React App SPA** (React, Vue, Svelte, Preact, Solid)
- **Docusaurus** (`docusaurus.config.*`, `docs/`, `blog/`)
- **Hugo** (`hugo.toml`/`hugo.yaml`/`config.toml` + `content/`/`layouts/`)
- **Jekyll** (`_config.yml` + `_posts/`/`_layouts/`)
- **Eleventy / 11ty** (`.eleventy.js` / `eleventy.config.*`)
- **SvelteKit** (`svelte.config.*` + `src/routes/`)

`migration-contract.json` emits both `pages` and a structured `frameworkMigration` blueprint:
- `uiFrameworks`, `recommendedIntegrations` (`@astrojs/react`, `@astrojs/vue`, `@astrojs/svelte`, `@astrojs/mdx`, `@astrojs/sitemap`, `@astrojs/node`, `@astrojs/cloudflare`), and `outputRecommendation` (`static` vs `server` when API routes exist).
- `routes` (static, `[slug]` dynamic, `[...catchall]`, content entries) and `apiRoutes`.
- `redirects` parsed statically from `vercel.json`, `netlify.toml`, `public/_redirects`, `static/_redirects`, `next.config.*`, and `nuxt.config.*`.
- `islands`: splits UI components into `interactiveIslands` (`'use client'`, React hooks, Vue reactivity, Svelte runes/lifecycle, browser globals -> suggested `client:*` directive) vs `staticCandidateComponents` (convert to zero-JS `.astro`).
- `envVars`: discovers public env prefixes (`NEXT_PUBLIC_*`, `NUXT_PUBLIC_*`, `GATSBY_*`, `VITE_*`, `REACT_APP_*`) with suggested `PUBLIC_*` renames without ever reading secret values.
- `frameworkImportsAndTraps`: counts `next/image`, `next/link`, `next/head`, `next/router`, `next/navigation`, `gatsby-plugin-image`, Hugo `{{< ... >}}` shortcodes, Jekyll Liquid `{% ... %}`, CSS Modules, and Tailwind.
- `contentCollections`: discovers Markdown/MDX roots and sample frontmatter keys ready for Astro v7 `src/content.config.ts`.
- `recommendedDocs`: exact vendored `docs-lookup` IDs for the detected framework.

## Path B: Live site, WordPress, Webflow, HTML, or `legends-firecrawl` migration

### Step 1: Capture & triage (`capture` / `source-audit`)

For live WordPress, Webflow, or static HTML origins (or local `legends-firecrawl` directories), run `capture` to download or normalize HTML pages into `raw-capture/`, emit `migration-contract.json`, and generate a deterministic Migration Triage Report:

```sh
python -m legends_astro capture \
  --url https://example.com \
  --from-sitemap --crawl --max-pages 50 \
  --output ./work
```

**Smart optional `legends-firecrawl` fallback:** During live `--url` capture, `capture` automatically inspects responses for:
1. WAF / bot-protection challenges (HTTP `403`/`429`/`503`, `cf-mitigated`, Cloudflare "Just a moment..." / Turnstile / Akamai / PerimeterX / DataDome markers), and
2. Empty JS SPA shells (`<div id="root"></div>`, `<div id="__next"></div>`, `<div id="app"></div>`, `<div id="__nuxt"></div>` with minimal visible text while loading JS bundles).

When detected, `capture` sets `firecrawlFallback.recommended = true` with `reasons`, `affectedUrls`, and exact handoff commands using optional `legends-firecrawl`:

```sh
cto-legends run legends-firecrawl -- scrape "https://example.com" --formats rawHtml,markdown --output ./firecrawl-captures
python -m legends_astro capture --from-firecrawl ./firecrawl-captures --output ./work
```

Or run `source-audit` directly against local HTML, WordPress WXR `.xml`, WordPress REST `.json`, or Firecrawl files/directories:

```sh
python -m legends_astro source-audit \
  --source ./legacy-site \
  --output ./migration-contract.json \
  --base-url https://example.com
```

### Step 2: Localize remote assets (`vendor-assets`)

Run `vendor-assets` with explicit `--allow-host` entries (origin + WordPress/Webflow CDNs) to download Pass 1 HTML assets (`src`, `href`, `srcset`, `data-poster-url`, `data-video-urls`, inline `background-image`, `dynamic_css.php` -> `.css`) and Pass 2 recursive CSS `@import` / `url(...)` fonts and backgrounds into `public/` and `asset-map.json`:

```sh
python -m legends_astro vendor-assets \
  --source ./work/raw-capture \
  --output ./astro-site \
  --allow-host example.com \
  --allow-host cdn.prod.website-files.com \
  --dry-run
```

### Step 3: Scaffold Astro v7.4.13 project (`scaffold` or `ingest`)

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
   - Run `legends-astro ingest --source <export> --output <astro-dir> --apply` (after previewing `planSha256`).
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
