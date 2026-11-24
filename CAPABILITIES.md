# Capabilities and verification scope

Module: `legends-astro` **0.3.0** (successor to `legends-astro-kit` 0.1.0), verified **2026-11-24**.

## Executed and verified in `legends-astro v0.3.0`

- **Dual CLI & Router Contract:** Python CLI (`python -m legends_astro` / `legends-astro`) and Node helpers (`scripts/doctor.mjs`, `scripts/verify-sources.mjs`, `scripts/twin.mjs`) with zero mandatory Python or Node package dependencies.
- **Shared Network & Filesystem Safety (`net_safety.py`):**
  - CWE-918 SSRF & DNS-rebinding IP pinning (rejects loopback, RFC1918 private, CGNAT `100.64/10`, link-local/metadata, IPv4-mapped IPv6, unspecified, and multicast/reserved ranges), explicit `--allow-host` allowlisting, `https://` default (`--allow-http` opt-in), manual redirect hop validation (max 5 hops), bounded streaming reads, and strict TLS 1.2+ CA verification.
  - CWE-59 symlink-chain refusal, Windows Controlled Folder Access refusal, CWE-22 path containment, `O_CREAT | O_EXCL` no-clobber writes, and CWE-776 XXE-safe XML parsing.
- **Project, Module & Optional Ecosystem Readiness (`doctor`):**
  - Verifies Node runtime against Astro v7.4.13 engine requirements (`>=22.23.4`, tested canary baseline `24.15.0`, compatible warning on odd majors such as `v25.9.0`).
  - Supports `npm` (`package-lock.json`), `pnpm` (`pnpm-lock.yaml`), `yarn` (`yarn.lock`), and `bun` (`bun.lock` / `bun.lockb`) without mutating consumer lockfiles.
  - Detects legacy `src/content/config.ts` paths, on-demand routes (`prerender = false`), optional `@astrojs/check` + `typescript` availability, global `Astronomer.Astro` PATH collisions, and read-only non-blocking `ecosystem` status (`legends-firecrawl`, Playwright).
  - Live ecosystem probe (`doctor --live`) queries `registry.npmjs.org` for `astro` (`7.4.13`) and 8 official `@astrojs/*` companions (including `@astrojs/cloudflare`) plus `api.github.com/repos/withastro/docs/commits/main`.
- **Multi-Framework Source Repo Audit (`framework_audit.py` via `source-audit`):**
  - Strictly read-only static inspection (zero JS/TS/Ruby/Go config execution, symlink-chain refusal, path containment, file-size bounds, automatic skipping of `node_modules`/`.git`/`.next`/`.nuxt`/`dist`/`build`/`out`/`.cache`/`.astro`).
  - Auto-detects Next.js (App Router, Pages Router, and hybrid), Nuxt, Gatsby, Vite / Create React App (React, Vue, Svelte, Preact, Solid), Docusaurus, Hugo, Jekyll, Eleventy / 11ty, and SvelteKit while preserving precedence for `raw-capture/manifest.json`, Firecrawl JSON/dirs, WordPress WXR/REST, and HTML trees.
  - Emits structured `frameworkMigration` inside `migration-contract.json` (plus populated `pages`): `uiFrameworks`, `recommendedIntegrations`, `outputRecommendation` (`static` vs `server`), static/dynamic/catchall `routes`, `apiRoutes`, static `redirects` (`vercel.json`, `netlify.toml`, `_redirects`, `next.config.*`, `nuxt.config.*`), `islands` (`interactiveIslands` vs `staticCandidateComponents`), `envVars` (`PUBLIC_*` renames without reading secret values), `frameworkImportsAndTraps`, `contentCollections`, and exact vendored `recommendedDocs` IDs.
- **Site Capture, Migration Triage & Smart Optional `legends-firecrawl` Fallback (`capture`):**
  - Live `--url` capture via recursive XML sitemaps (`--from-sitemap`) and/or bounded BFS link crawling (`--crawl --max-pages N --max-depth D`), OR local `--from-firecrawl <file-or-dir>` unwrapping `response.data`, top-level `data`, and `legends-firecrawl-evidence/v1`.
  - Collision-safe `__` slug mapping, canonical redirect normalization, asset and `/cdn-cgi/` filtering, and deterministic Migration Triage Report (platform `wordpress`/`webflow`/`html`, page builders, `[shortcodes]`, forms/Turnstile/reCAPTCHA, WooCommerce, Cloudflare email obfuscation, live trackers, external CDNs).
  - Detects WAF/bot challenges (HTTP `403`/`429`/`503`, `cf-mitigated`, Cloudflare/Turnstile/Akamai/PerimeterX/DataDome) and empty JS SPA shells (`#root`, `#__next`, `#app`, `#__nuxt`) and emits `firecrawlFallback` with recommended `cto-legends run legends-firecrawl` + `--from-firecrawl` commands.
- **Two-Pass Asset Localizer (`vendor-assets`):**
  - Pass 1 HTML extraction (`src`, `href`, `srcset`, `data-poster-url`, `data-video-urls`, inline `background-image: url(...)`, `dynamic_css.php` -> `.css`) plus Pass 2 recursive CSS `@import` and `url(...)` crawler for fonts and background assets.
  - Deterministic `public/assets/...` and `public/wp-content/...` localization, `asset-map.json`, `--dry-run` preview, longest-URL-first HTML/CSS rewriter, and SRI `integrity=`/`crossorigin=` cleanup on local rewritten tags.
- **Astro v7.4.13 Project Scaffolder (`scaffold --mode clone|editorial` & `ingest`):**
  - Preview-first (`planSha256` -> `--apply --confirm-plan <sha256>`) generation of immediately buildable Astro v7.4.13 projects (`src/content.config.ts` with `glob()` + `astro/zod`, `entry.id`, `src/lib/published.ts`, `404.astro`, `public/_redirects`).
  - `--mode clone`: `LegacyDocument.astro` preserving `htmlAttrs`/`bodyAttrs`/`<head>` via `<Fragment set:html={...} />`, plus `--asset-map`, `--decode-cf-email` (real XOR hex decoder), `--strip-trackers`, and `--staging-lock` (Triple-Lock: `public/robots.txt`, `meta robots noindex`, `public/_headers` `X-Robots-Tag`).
  - `--mode editorial`: WordPress/HTML CMS scaffold preserving Custom Post Types, categories/tags, authors, `_thumbnail_id` featured images, Yoast/RankMath SEO titles, and clean Markdown (`<table>`, `<figure>`, `<iframe>`).
- **Visual Digital Twin & Parity Diff (`twin` & `twin-diff`):**
  - Optional runtime Playwright discovery (`--playwright-from` / `PLAYWRIGHT_PACKAGE_JSON` / local `node_modules`) capturing `1440x900` desktop (viewport + full-page capped `16000px` + `--crop`) and `390x844` mobile screenshots plus computed styles (`loadedFonts`, `horizontalOverflow`, runtime errors, sections, radii, buttons, surfaces, footers, headings).
  - Supports `--url` (SSRF + `page.route` guarded) or `--dist` (ephemeral in-process `127.0.0.1:0` read-only static server closed in `finally`).
  - Pure offline `twin-diff` comparing baseline and candidate `twin.json` files and exiting non-zero on regressions.
- **Post-Build Parity & Quality Gate (`verify-build`):**
  - Verifies route parity, zero draft route/body leaks, `<title>`/description/canonical/OG/JSON-LD `@type` + valid JSON/H1/Webflow `data-wf-page` parity, required DOM `id` retention, `<style is:inline>` order, internal link/asset + CSS `url()`/`@import` resolution, local-asset SRI check, `--forbid-host`, `--env staging|production`, `trailingSlash` shape parity, XXE-safe sitemap verification, `_redirects` loop/target checks, and zero `/cdn-cgi/` or `[shortcode]` leaks.
- **Curated Official Astro v7.4 Documentation Snapshot (`vendor/astro-docs/`):**
  - 45 official MDX files (48 manifest files including `LICENSE`, `NOTICE`, `README.md`) from `withastro/docs@87c37b156627418328b8d3d4671320ca20487c79` (`2026-11-24T14:03:07Z`), including all 15 official `migrate-to-astro/` guides, verified offline via SHA-256 hashes and negative self-tests (`scripts/verify-sources.mjs`).
- **Attachable Empire Knowledge Pack (`knowledge/astro/pack.json`):**
  - Schema `legends.knowledge-pack/v1` with SHA-256 verified operational runbooks and templates.
- **Isolated Static Canary (`canary/`):**
  - Locked `astro@7.4.13` 4-page static build plus loopback HTTP stub parity tests (`test-canary-static.mjs`, `test-handler-parity.mjs`).

## Explicit boundaries and exclusions

- Does not spawn detached background `astro dev` or `astro preview` daemons or launch user browsers with `--open`.
- Does not replace `legends-cloudflare` for DNS/edge rules or `legends-coolify` / `legends-hetzner` for server deployment.
- Does not silently upgrade a consumer's Astro version or convert non-npm lockfiles.
