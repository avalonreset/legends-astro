# Primary sources and version ledger

Observed and verified on **2026-11-24**.

## 1. Upstream package versions (npm registry)

| Package | Latest Version (2026-11-24) | Canary Baseline | Node Engines | License |
|---|---|---|---|---|
| `astro` | `7.4.13` | `7.4.13` | `>=22.23.4` | MIT (Fred K. Schott 2021) |
| `create-astro` | `5.3.2` | `5.3.2` | `>=22.23.4` | MIT |
| `@astrojs/check` | `0.9.12` | `0.9.12` | Peer `typescript ^5 \|\| ^6` | MIT |
| `@astrojs/mdx` | `8.1.6` | `8.1.x` | `>=22.23.4` | MIT |
| `@astrojs/sitemap` | `3.7.6` | `3.7.x` | Astro v7 peer | MIT |
| `@astrojs/rss` | `4.0.19` | `4.0.x` | Astro v7 peer | MIT |
| `@astrojs/node` | `11.1.14` | `11.1.x` | Astro v7 peer | MIT |
| `@astrojs/cloudflare` | `14.4.9` | `14.4.x` | Astro v7 peer | MIT |
| `@astrojs/upgrade` | `0.8.1` | `0.8.1` | `>=22.23.4` | MIT |
| `sharp` (override) | `0.35.5` | `^0.35.5` | `>=20.9.0` | Apache-2.0 |
| `undici` (override) | `8.13.1` | `^8.13.1` | `>=22.19.0` | MIT |

## 2. Vendored official documentation snapshot

- **Repository:** `https://github.com/withastro/docs`
- **Commit:** `87c37b156627418328b8d3d4671320ca20487c79` (`2026-11-24T14:03:07Z`)
- **Prior snapshot commit:** `0844aab04ea2f3859132ab97b1e9daa01ade76a1` (`2026-10-22T14:08:43Z`)
- **License:** MIT (`Copyright (c) 2022 withastro`), preserved in `vendor/astro-docs/LICENSE` and `vendor/astro-docs/NOTICE`.
- **Integrity manifest:** `vendor/astro-docs/manifest.json` (33 upstream MDX files + `LICENSE`, `NOTICE`, `README.md` = 36 files verified via `node scripts/verify-sources.mjs`).

## 3. Verified upstream live endpoints and negative probes (2026-11-24)

- `https://registry.npmjs.org/astro/latest` -> HTTP 200 (`version: "7.4.13"`, `engines.node: ">=22.23.4"`).
- `https://api.github.com/repos/withastro/docs/commits/main` -> HTTP 200 (`87c37b156627418328b8d3d4671320ca20487c79`).
- `https://mcp.docs.astro.build/mcp` -> Streamable HTTP MCP server (HTTP 405 on HEAD, `allow: POST`, exposes `search_astro_docs`).
- `https://docs.astro.build/llms.txt` and `https://docs.astro.build/llms-full.txt` -> HTTP 404 NotFound (verified `2026-11-24`; do not use in agent instructions).
