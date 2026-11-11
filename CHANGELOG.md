# Changelog

## 0.2.4 - 2026-11-10

- Repaired control-character corruption in `vendor/astro-docs/README.md`, updated `vendor/astro-docs/manifest.json`, and expanded `scripts/check_release.py` to scan all tracked text files (including `vendor/`) for ASCII control characters and `U+FFFD`.
- Removed hidden-window spawn option (`windowsHide`) from `scripts/test-doctor.mjs`, excluded `scripts/test-*.mjs` from wheel `_data` mirroring in `setup.py`, and added `windowsHide` detection to `scripts/check_release.py` (Windows 11 EDR Hard Law 1).
- Hardened `auditor.py` and `cli.py` with CWE-59 symlink chain refusal, Windows Controlled Folder Access directory protection, `O_CREAT | O_EXCL` overwrite guards (`--overwrite`), preview `planSha256` confirmation (`--confirm-plan`), `dist/` path-containment verification, `50 MB` WXR / `10 MB` source file size caps, Expat `<!DOCTYPE` / `<!ENTITY` refusal (CWE-776), and `json.dumps` YAML frontmatter scalar escaping (CWE-74).
- Hardened `probe_live_ecosystem` in `docs_nav.py` with `_NoRedirectHandler`, `ProxyHandler({})`, HTTPS host allowlisting (`registry.npmjs.org`, `api.github.com`), and a `1 MB` response cap (CWE-918).
- Documented `resources` and `knowledge-pack` subcommands and complete Windows 11 EDR Hard Law 6 guardrails across `README.md`, `AGENTS.md`, and `docs/AGENT-RECIPE.md`, and added `.legends-router-pin` to `MANIFEST.in` and `scripts/check_release.py`.

## 0.2.3 - 2026-11-05

- Enforced single-skill router compliance by removing redundant `skills/legends-astro/` directory (`skills/cto-legends/SKILL.md` is the sole shipped skill).
- Added `setup.py` build-time wheel data mirror (`legends_astro/_data/`), `legends-astro resources` subcommand, `.legends-module`, `assets/manifest.json`, `scripts/smoke_installed_resources.py`, and `scripts/verify_knowledge_attachment.py` so standalone wheel installs resolve all knowledge packs, vendored docs, and Node scripts outside a source checkout.
- Upgraded `scripts/check_release.py` and `.github/workflows/checks.yml` with Gold-Standard EDR/metadata/dash gates and multi-OS `empire-attachment` CI verification.

## 0.2.2 - 2026-11-04

- Add explicit agent guardrails in `AGENTS.md`, `docs/AGENT-RECIPE.md`, and `wiki/inspect-setup.md` forbidding detached background `astro dev` / `astro preview` daemons and `--open` browser launches during automated agent runs (preventing Windows EDR local-portal behavioral alerts) in favor of offline `astro build` + `legends-astro verify-build`.

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
