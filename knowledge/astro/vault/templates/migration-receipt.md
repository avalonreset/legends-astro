# Astro migration and build verification receipt

- **Date:** YYYY-MM-DD
- **Project path:** `<project-root>`
- **Source input:** `<source-path-or-url>`
- **Astro version:** `<7.4.2 | 7.3.1>`
- **Node runtime:** `<node -v>`

## 1. Pre-migration contract (`source-audit`)

- Contract path: `<project-root>/migration-contract.json`
- Source kind: `<html | wordpress-xml | wordpress-json | firecrawl-json>`
- Total source routes: `<N>`
- JS-referenced DOM IDs: `<list or count>`
- Recommended Astro config: `build.format: '<file|directory|preserve>'`, `trailingSlash: '<never|always|ignore>'`

## 2. Build and parity verification (`verify-build`)

- Command run:
  ```sh
  ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build
  python -m legends_astro verify-build --dist ./dist --project . --contract ./migration-contract.json
  ```
- Built HTML files: `<N>`
- Draft entries checked: `<N>` (0 leaked routes, 0 leaked body strings)
- SEO and JSON-LD parity: `<PASS | FAIL>`
- Required DOM IDs preserved: `<PASS | FAIL>`
- First-paint `<style is:inline>` order: `<PASS | FAIL>`
- Internal links and assets resolved: `<PASS | FAIL>`

## 3. Residual limits

- Browser computed-style and interactive checks: `<PASS | UNTESTED>`
- Origin / CDN cache headers verified: `<PASS | UNTESTED>`
