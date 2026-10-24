# Diagnose and upgrade

Cross-read: [inspect-setup.md](inspect-setup.md), [content-collections.md](content-collections.md), [latest-docs-and-ecosystem.md](latest-docs-and-ecosystem.md).
Vendored official docs:
- `vendor/astro-docs/src/content/docs/en/guides/troubleshooting.mdx`
- `vendor/astro-docs/src/content/docs/en/upgrade-astro.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/upgrade-to/v7.mdx`

## 1. Diagnose without mutating the project

Always start with read-only `doctor` and `info`:

```sh
python -m legends_astro doctor --project <consumer-root>
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs --version
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs info
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs build --verbose
```

### `astro check` requires optional packages

`astro check` is separate from `astro build`. It requires `@astrojs/check` (currently `0.9.12`) and `typescript` (`^5 || ^6`) installed in the project.
- If `doctor` reports `check.usable: false`, **do not run `astro check`**; running it without those packages triggers an interactive install prompt that hangs or fails in non-interactive agent shells.
- `astro sync` does **not** require `@astrojs/check` and works out of the box.

## 2. Common Astro v7 symptoms and fixes

| Symptom or error | Root cause | Fix |
|---|---|---|
| `LegacyContentConfigError` or ignored schema | Using Astro v4/v5 `src/content/config.ts` or `type: 'content'` | Move config to `src/content.config.ts`, import `glob` from `astro/loaders`, and import `z` from `astro/zod` |
| `entry.render is not a function` or `entry.slug` undefined | Astro v6/v7 Content Layer API change | Use `entry.id` and `import { render } from "astro:content"; const { Content } = await render(entry);` |
| Draft Markdown page still accessible at `/blog/secret-draft` | Filtered index list but forgot `getStaticPaths()` | Apply shared `isPublished` filter inside `getStaticPaths()` and verify with `legends-astro verify-build` |
| `Cannot use import statement outside a module` in browser | Added `is:inline` or `src` to `<script>`, which disables bundling | Add `type="module"` explicitly (`<script is:inline type="module" src="...">`) |
| `document is not defined` / `window is not defined` during build | Browser DOM API called inside `.astro` frontmatter (`---`) | Move browser code into a client `<script>` tag |
| Dynamic JS elements (buttons/modals) have no styling | Astro `<style>` is scoped with `data-astro-cid-*` at compile time | Use `<style is:global>` or global CSS for classes attached via `createElement` / `innerHTML` |
| Critical first-paint `<style>` missing from HTML `<head>` | Compiler extracted `<style>` into `/_astro/*.css` | Add `is:inline` to the critical `<style>` tag in `<head>` |
| Build fails asking for an adapter (`Cannot use server-rendered pages without an adapter`) | A route sets `export const prerender = false` or config sets `output: 'server'` | Report the on-demand route to the user; do **not** delete routes to force a static build. Install `@astrojs/node` (or target adapter) only when authorized |
| `astro` command runs Apache Airflow CLI | Windows `winget` `Astronomer.Astro` on `PATH` | Use `node ./node_modules/astro/bin/astro.mjs` |

## 3. Upgrading Astro safely

Never upgrade a consumer project's Astro version as an unrequested side effect of an inspection or content edit. When the user explicitly requests an upgrade:

1. Record current `package.json` and lockfile state, and run `legends-astro doctor --project <dir> --live` to see the current upstream latest (`astro@7.4.2`, Node `>=22.23.4`).
2. Check major breaking changes in `vendor/astro-docs/src/content/docs/en/guides/upgrade-to/v7.mdx`:
   - **v5 -> v7:** `output: 'hybrid'` removed (merged into default `'static'` with per-route `prerender = false`).
   - **v6 -> v7:** Legacy content collections removed in favor of Content Layer (`src/content.config.ts`).
   - **v7:** Vite 8, Rust compiler (strict on unclosed non-void HTML tags), Satteri Markdown processor, `compressHTML: 'jsx'`, Node engine minimum `>=22.12.0` (`>=22.23.4` in `7.4.2`).
3. Apply the pinned upgrade using the project's package manager (`npm install astro@7.4.2 --save-exact`), run `astro sync` and `astro build`, and verify the output with `legends-astro verify-build --dist ./dist --project .`.
