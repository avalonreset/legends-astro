# Performance and caching

Cross-read: [pages-components-styles-scripts.md](pages-components-styles-scripts.md), [migrate-and-wordpress.md](migrate-and-wordpress.md).
Vendored official docs:
- `vendor/astro-docs/src/content/docs/en/guides/images.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/prefetch.mdx`
- `vendor/astro-docs/src/content/docs/en/guides/caching.mdx`

## 1. Static prerendering first

Astro's default `output: 'static'` prerenders pages to HTML at build time with zero client-side framework runtime by default. Keep pages static unless a route genuinely requires per-request server rendering.

Do not blindly apply community "always add React islands / always enable `ClientRouter` / always enable global `prefetch: true`" rules:
- UI framework components (React, Vue, Svelte) ship zero JS until a `client:*` directive (`client:load`, `client:idle`, `client:visible`) is attached. Prefer native HTML and small `<script>` modules unless interactive state requires a framework island.
- `prefetch: true` injects a prefetch runtime script across pages. Enable it deliberately only after measuring baseline asset sizes.

## 2. Images (`src/assets/` vs. `public/`)

- **Images in `src/`:** Processed, optimized, resized, and fingerprinted into `/_astro/*` by Astro's Sharp image service when rendered via `<Image />` or `<Picture />` from `astro:assets` (or standard Markdown `![alt](./img.jpg)`). `alt` is required on `<Image />`.
- **Images in `public/`:** Copied untouched to `dist/` with their original filenames and byte sizes. During an initial parity migration where external references or CSS expect stable image URLs, keep those assets in `public/`; move high-weight hero/content images to `src/assets/` in a measured second pass.

## 3. CSS chunking and first-paint correctness

Astro bundles and minifies imported CSS, inlining small stylesheets (under ~4 KB by default unless `build.inlineStylesheets: 'never'` is configured) and emitting hashed `/_astro/*.css` files for larger stylesheets.
- Keep critical dark-mode or layout-stabilizing first-paint styles in `<head>` with `<style is:inline>` so they are never extracted into an external request.
- Build-time markup ordering checks prove structure (`<style is:inline>` before `<script>`), not browser Core Web Vitals (LCP/CLS). Claim speed improvements only after measuring before and after under identical conditions.

## 4. Origin and CDN cache headers

Astro `build` outputs static files; your origin server or CDN (`legends-cloudflare`, `legends-coolify`, Nginx, Caddy, or custom Node handler) sets HTTP `Cache-Control` headers:

| Surface | Recommended `Cache-Control` | Guardrail |
|---|---|---|
| Real fingerprinted asset under `dist/_astro/<name>.<hash>.<ext>` | `public, max-age=31536000, immutable` | Verify the resolved file exists inside `dist/_astro` (prevent `..` path traversal) before returning 200 with `immutable` |
| Missing `/_astro/*` path (404) | `no-store` | Never cache 404 responses as `immutable` |
| HTML pages that vary by cookie, auth, or consent CSP | `no-store` (or `private, no-cache`) | Prevents CDN edge poisoning of personalized or consent-tagged HTML |
| Purely static public HTML | `public, max-age=0, must-revalidate` (or short CDN s-maxage) | Allows instant content updates while caching hashed `/_astro/*` assets long-term |
| Unfingerprinted scripts/files in `public/` | `public, max-age=300` or `no-cache` | Do not mark unhashed filenames `immutable` |
