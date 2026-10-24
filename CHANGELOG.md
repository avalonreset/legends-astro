# Changelog

## 0.2.0 - 2026-10-24

- Renamed public module from `legends-astro-kit` to `legends-astro` (preserving `legends-astro-kit` as a legacy compatibility alias).
- Added Python CLI (`legends-astro`) with `doctor`, `source-audit`, `ingest`, `verify-build`, `docs-lookup`, and `knowledge-pack` subcommands.
- Added deterministic `source-audit` and `ingest` for HTML trees, WordPress WXR XML, WordPress REST JSON, and `legends-firecrawl` JSON, including HTML-to-Markdown body conversion and `public/_redirects` generation.
- Added mechanical `verify-build` post-build parity verifier checking route preservation, SEO/JSON-LD parity, client JS DOM `id` ABI, draft exclusion, first-paint `<style is:inline>` ordering, and internal link/asset resolution.
- Expanded vendored `withastro/docs` snapshot to commit `0844aab04ea2f3859132ab97b1e9daa01ade76a1` (24 official MDX pages + license/notice/readme = 27 verified files) and reviewed upstream `astro@7.4.2` / canary pin `astro@7.3.1`.
- Added attachable `legends-empire` knowledge pack (`knowledge/astro/pack.json`, `legends.knowledge-pack/v1`).
