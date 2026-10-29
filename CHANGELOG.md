# Changelog

## 0.2.1 - 2026-10-29

- Raise minimum supported Python runtime to Python 3.11 (`requires-python = ">=3.11"`) across package metadata, documentation, and multi-OS CI.
- Upgrade transitive `sharp` (`>=0.35.5`, fixing `CVE-2026-96889`) and `undici` (`>=8.12.2`, fixing `CVE-2026-109383`) in `canary/package-lock.json` via npm `overrides`.
- Harden `.github/workflows/checks.yml` with 40-character commit SHA action pins, `persist-credentials: false`, and `pip>=26.2.2` before package installation.

## 0.2.0 - 2026-10-24

- Renamed public module from `legends-astro-kit` to `legends-astro` (preserving `legends-astro-kit` as a legacy compatibility alias).
- Added Python CLI (`legends-astro`) with `doctor`, `source-audit`, `ingest`, `verify-build`, `docs-lookup`, and `knowledge-pack` subcommands.
- Added deterministic `source-audit` and `ingest` for HTML trees, WordPress WXR XML, WordPress REST JSON, and `legends-firecrawl` JSON, including HTML-to-Markdown body conversion and `public/_redirects` generation.
- Added mechanical `verify-build` post-build parity verifier checking route preservation, SEO/JSON-LD parity, client JS DOM `id` ABI, draft exclusion, first-paint `<style is:inline>` ordering, and internal link/asset resolution.
- Expanded vendored `withastro/docs` snapshot to commit `0844aab04ea2f3859132ab97b1e9daa01ade76a1` (24 official MDX pages + license/notice/readme = 27 verified files) and reviewed upstream `astro@7.4.2` / canary pin `astro@7.3.1`.
- Added attachable `legends-empire` knowledge pack (`knowledge/astro/pack.json`, `legends.knowledge-pack/v1`).
