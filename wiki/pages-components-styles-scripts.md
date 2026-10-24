# Pages, components, styles, and scripts

Cross-read: [inspect-setup.md](inspect-setup.md), [content-collections.md](content-collections.md), [migrate-and-wordpress.md](migrate-and-wordpress.md).
Vendored official docs:
- `vendor/astro-docs/src/content/docs/en/basics/astro-pages.mdx`
- `vendor/astro-docs/src/content/docs/en/basics/astro-components.mdx`
- `vendor/astro-docs/src/content/docs/en/basics/layouts.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/routing.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/styling.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/client-side-scripts.mdx`

## 1. Routes and URL shape (`build.format` and `trailingSlash`)

Routes live in `src/pages/` (`.astro`, `.md`, `.html`, `.js`/`.ts` endpoints, or `.mdx` with `@astrojs/mdx`).
When migrating an existing site or matching a backend route table, configure `build.format` and `trailingSlash` in `astro.config.mjs` to match the existing canonical URLs:

| Existing URL shape | `build.format` | `trailingSlash` | Emitted file in `dist/` |
|---|---|---|---|
| `/about/` | `'directory'` (default) | `'always'` | `dist/about/index.html` |
| `/about.html` | `'file'` | `'never'` | `dist/about.html` |
| Mixed `/about` and `/about/` | `'preserve'` | `'ignore'` | Matches source file structure |

Static `redirects` in `astro.config.mjs` without a server adapter emit HTML `<meta http-equiv="refresh">` files, not HTTP 301 headers. Keep real 301 redirects at your origin server or CDN (`legends-cloudflare`).

## 2. Layouts and components

Layouts are standard `.astro` components that wrap page content with `<slot />` and provide the single `<html>`, `<head>`, and `<body>` shell. Astro does not merge multiple `<head>` elements across nested components; keep `<head>` in the top-level layout and pass page metadata (`title`, `description`, `canonical`, `schemaJson`) via `Astro.props`.

Component frontmatter (`---`) runs at build time (or server request time for SSR) and never ships to the browser. Pass build-time values to client scripts using `data-*` attributes on HTML elements.

## 3. Styles: global vs. scoped CSS and critical first-paint

In Astro, `<style>` tags are **scoped by default** using `data-astro-cid-*` attributes added at compile time.
- **Trap 1 (JS-inserted DOM nodes):** Any element created at runtime via `document.createElement()` or `innerHTML` does not receive `data-astro-cid-*` and will be **unstyled** by default `<style>`. Use `<style is:global>` or import a global stylesheet (`import '../styles/global.css'`) whenever styling elements created or toggled by client-side JavaScript.
- **Trap 2 (First-paint inline CSS extraction):** An ordinary `<style>` tag inside `<head>` is extracted by Astro into `/_astro/*.css`. If a page requires an in-document `<style>` block in `<head>` to prevent a white flash before scripts run, add `is:inline`:

```astro
<style is:inline data-first-paint-black>
  html, body { margin: 0; background: #000; color: #fff; }
</style>
```

## 4. Client scripts: preserving attributes and output order

By default, Astro bundles and transforms bare `<script>` tags into `type="module"`.
Adding any attribute (such as `is:inline`, `defer`, `async`, or `src`) opts out of Astro bundling.

- **Always copy the original attributes explicitly** when using `is:inline`:
  ```astro
  <script is:inline type="module" src="/app.js"></script>
  <script is:inline defer src="/guard.js"></script>
  ```
- **Verify built order in `dist/`:** In Astro v7, a `<script is:inline defer src="...">` placed in `<head>` can be hoisted above a `<style is:inline>` block in `<head>`, and a processed `<style is:global>` block can be emitted after a module `<script>`. Always run `legends-astro verify-build --dist ./dist` after building to confirm that critical inline `<style>` tags appear before `<script>` tags in `dist/*.html`.
