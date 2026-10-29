# Release readiness

Release: `legends-astro` 0.2.1. Release date: 2026-10-24.

Status: public `0.2.1` release verified across offline Python unit tests, Node doctor and source-manifest negative tests, canary `astro@7.3.1` build and parity verification, clean-environment wheel/sdist installation, and `legends-empire` knowledge-pack attachment.

| Gate | State |
|---|---|
| Product rename (`legends-astro-kit` to `legends-astro`) and public package | Completed (`legacy_aliases: ["legends-astro-kit"]`) |
| Official-source knowledge vault (`vendor/astro-docs/` commit `0844aab04ea2f3859132ab97b1e9daa01ade76a1`), `legends.knowledge-pack/v1` pack, and migration receipt template | Completed and verified through `legends-empire` (preview, apply, repeat noop) |
| Attribution and MIT license | Completed (`LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`, `vendor/astro-docs/LICENSE`) |
| House banner and social image | Rendered (`assets/banner.png`, `assets/banner.webp`, `assets/preview.png`, `assets/social-preview.png`) and visually inspected |
| Offline behavior tests | Passed Python `unittest` suite (`tests/test_legends_astro.py`), Node `verify-sources.mjs` (4 negative tamper/escape self-tests), and Node `test-doctor.mjs` |
| Canary Astro v7 build and handler parity | Passed `astro@7.3.1` build (4 static routes), `test-canary-static.mjs`, `test-handler-parity.mjs`, and `legends-astro verify-build` |
| Clean wheel and source installation | Passed isolated wheel and source-distribution installs with offline `legends-astro doctor` |
| Router recipe and catalog candidate | `skills/cto-legends/SKILL.md` matches canonical `cto-legends` `SKILL.md` SHA-256 (`80c198f2946121113b8f1da2ed12ca6e286ec74d5bea5938a28e5372deb96eaf`); candidate catalog entry prepared in `docs/catalog-entry.candidate.json` |
| Linux/macOS/Windows hosted CI | Configured in `.github/workflows/checks.yml` across `ubuntu-latest`, `windows-latest`, and `macos-latest` |
