# Astro operating knowledge

Use this knowledge library with the `legends-astro` CLI or as a standalone Markdown reference. Read the dated [source ledger](sources.md) for exact upstream commits and package versions.

| Task | Runbook | CLI command | Evidence to keep in project |
|---|---|---|---|
| Check environment, Node runtime, lockfile, optional ecosystem, or live version drift | [Inspect and setup](inspect-setup.md) | `legends-astro doctor --project <dir> [--live]` | Doctor JSON receipt |
| Start a Day-1 greenfield agency, blog, or local-business starter | [Agency starter guidelines](agency-starter-guidelines.md) | `legends-astro init --output <dir> [--preset agency\|blog\|local-business]` / `scaffold --mode starter` | Starter plan (`planSha256`) and build receipt |
| Publish or update Day-2 articles without breaking SEO | [Publishing and content ops](publishing-and-content-ops.md) | `legends-astro post --project <dir> [--list\|--update\|--rename-from]` | Post plan, localized images, `public/_redirects` |
| Gate Day-2 changes with the regression manifest | [Publishing and content ops](publishing-and-content-ops.md) | `legends-astro verify-build --baseline site-manifest.json --write-manifest site-manifest.json --overwrite --env production` | `site-manifest.json` and `baselineDiff` receipt |
| Build pages, layouts, CSS, and client scripts without breaking order | [Pages, components, styles, scripts](pages-components-styles-scripts.md) | `legends-astro verify-build --dist <dist>` | `dist/` verification receipt |
| Configure Astro v7 Content Layer collections and prove draft exclusion | [Content collections](content-collections.md) | `legends-astro verify-build --dist <dist> --project <dir>` | Built route and draft-exclusion receipt |
| Migrate Next.js, Nuxt, Gatsby, Vite/CRA, Docusaurus, Hugo, Jekyll, Eleventy, WordPress, Webflow, HTML, or `legends-firecrawl` sites to Astro | [Migrate frameworks, WordPress, and live sites](migrate-and-wordpress.md) | `legends-astro source-audit` / `capture` -> `vendor-assets` -> `scaffold` -> `verify-build` -> `twin-diff` | `migration-contract.json`, `asset-map.json`, parity and twin reports |
| Optimize assets, images, and origin cache headers | [Performance and caching](performance-and-caching.md) | `legends-astro verify-build --dist <dist>` | Asset inventory and header checks |
| Diagnose compiler errors, `astro check`, or version upgrades | [Diagnose and upgrade](diagnose-and-upgrade.md) | `legends-astro doctor --project <dir> --live` | Upgrade diff and post-upgrade build proof |
| Find latest official Astro docs, changelogs, and ecosystem versions | [Latest docs and ecosystem lookup](latest-docs-and-ecosystem.md) | `legends-astro docs-lookup <query> --live-locations` | Retrieved commit SHA and package version |

## Why `legends-astro` exists beyond a raw agent prompt

Asking a general AI agent to "build an Astro site" or "migrate our Next.js/Hugo/WordPress site to Astro" frequently produces code that compiles (`astro build` exits 0) while silently introducing production regressions:
- Using deprecated Astro v4/v5 content APIs (`src/content/config.ts`, `type: 'content'`, `entry.render()`) that fail under Astro v6/v7 Content Layer.
- Over-hydrating static React/Vue/Svelte components or forgetting `client:*` directives on interactive islands, missing `NEXT_PUBLIC_*` / `VITE_*` -> `PUBLIC_*` env var renames, or dropping `vercel.json` / `netlify.toml` redirects.
- Silently scraping WAF challenge pages or empty JS SPA shells instead of falling back to optional `legends-firecrawl`.
- Filtering `draft: true` only on an index listing while `getStaticPaths()` still emits public HTML files for drafts in `dist/`.
- Dropping URL shape parity (`/about.html` vs `/about/`), canonical URLs, `<meta name="description">`, OpenGraph tags, or `application/ld+json` structured data during migration.
- Breaking vanilla client scripts by stripping or renaming DOM `id` attributes, losing `type="module"` or `defer` when adding `is:inline`, or applying scoped CSS to DOM nodes created dynamically via `document.createElement()`.
- Extracting critical first-paint `<style>` blocks into external `/_astro/*.css` stylesheets or hoisting blocking scripts above them.

`legends-astro` replaces guesswork with a complete agency lifecycle: Day-1 greenfield starter (`init` / `scaffold --mode starter`), deterministic multi-framework static repo auditing (`source-audit`), live/Firecrawl capture with WAF/SPA fallback detection (`capture`), two-pass asset localization (`vendor-assets`), preview-first Astro v7.4 scaffolding (`scaffold` / `ingest`), Day-2 article publishing and safe updating (`post`), mechanical post-build verification with regression manifest gating (`verify-build --baseline` / `--write-manifest`), and Playwright Visual Digital Twin diffing (`twin` / `twin-diff`), backed by cryptographically verified official Astro v7.4 MDX guides (including all official `migrate-to-astro/` framework guides) on disk.
