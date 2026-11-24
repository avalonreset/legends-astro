# Release readiness

Release: `legends-astro` 0.3.0. Release date: 2026-11-24.

Status: `0.3.0` release verified across offline Python unit tests, Node doctor and source-manifest negative tests, canary `astro@7.4.13` build and parity verification, clean-environment wheel/sdist installation, and `legends-empire` knowledge-pack attachment.

| Gate | State |
|---|---|
| End-to-end WordPress / Webflow / HTML migration engine (`capture`, `vendor-assets`, `scaffold`, `twin`, `twin-diff`, `source-audit`, `ingest`, `verify-build`) | Completed with zero mandatory pip/npm dependencies |
| Shared security & EDR guardrails (`net_safety.py`: CWE-918 SSRF/DNS-rebinding pinning, CWE-59 symlink refusal, CWE-22 containment, CWE-776 XXE refusal, Windows CFA refusal) | Completed and verified in hermetic unit tests |
| Official-source knowledge vault (`vendor/astro-docs/` commit `87c37b156627418328b8d3d4671320ca20487c79`, 33 MDX guides), `legends.knowledge-pack/v1` pack, and migration receipt template | Completed and verified through `legends-empire` (preview, apply, repeat noop) |
| Attribution and MIT license | Completed (`LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`, `vendor/astro-docs/LICENSE`) |
| House banner, social image, and 7 `r1` SVG architecture cards | Rendered and verified by `scripts/check_release.py` |
| Offline behavior tests | Passed Python `unittest` suite (`tests/`), Node `verify-sources.mjs` (4 negative tamper/escape self-tests), and Node `test-doctor.mjs` |
| Canary Astro v7.4.13 build and handler parity | Passed `astro@7.4.13` build (4 static routes), `test-canary-static.mjs`, `test-handler-parity.mjs`, and `legends-astro verify-build` |
| Clean wheel and source installation | Passed isolated wheel resource smoke check (`smoke_installed_resources.py`) |
| Router recipe and catalog candidate | `skills/cto-legends/SKILL.md` matches canonical `cto-legends` `SKILL.md` SHA-256 (`80c198f2946121113b8f1da2ed12ca6e286ec74d5bea5938a28e5372deb96eaf`); candidate catalog entry updated in `docs/catalog-entry.candidate.json` |
| Linux/macOS/Windows hosted CI | Configured in `.github/workflows/checks.yml` across `ubuntu-latest`, `windows-latest`, and `macos-latest` |
