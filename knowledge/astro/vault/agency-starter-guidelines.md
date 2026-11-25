# Agency starter guidelines (Day-1 greenfield)

Cross-read: [content-collections.md](content-collections.md), [publishing-and-content-ops.md](publishing-and-content-ops.md), [migrate-and-wordpress.md](migrate-and-wordpress.md).
Vendored official docs: `vendor/astro-docs/src/content/docs/en/guides/content-collections.mdx`, `vendor/astro-docs/src/content/docs/en/recipes/rss.mdx`, `vendor/astro-docs/src/content/docs/en/guides/deploy/index.mdx`.

Use this runbook when a team standardizes on Astro v7.4 across client websites and starts a new project instead of migrating an old one. The goal is a boring, buildable baseline: one Content Layer shape, one publishing filter, one layout shell, and one post-build gate on every client repo.

## 1. Start a project (init or scaffold --mode starter)

Both entry points generate the same starter. `init` is the ergonomic alias. `scaffold --mode starter` fits scripts that already call `scaffold` for clone or editorial work.

```sh
# Preview first: nothing is written. Copy planSha256 from the JSON output.
legends-astro init --output ./client-site --name "Client Legal" --site https://client.example --preset agency --trailing-slash always
legends-astro init --output ./client-site --name "Client Legal" --site https://client.example --preset agency --trailing-slash always --apply --confirm-plan <planSha256>
```

Equivalent form:

```sh
legends-astro scaffold --output ./client-site --mode starter --name "Client Legal" --site https://client.example --preset agency --trailing-slash always
legends-astro scaffold --output ./client-site --mode starter --name "Client Legal" --site https://client.example --preset agency --trailing-slash always --apply --confirm-plan <planSha256>
```

Rules that protect you:

- Preview-first safety: without `--apply --confirm-plan <planSha256>` the command only reports `plannedFiles` and `planSha256`. A stale or edited plan fails closed.
- `--overwrite` is required to replace existing files. Do not pass it casually on a live client repo.
- `--name` defaults to the `--output` directory name when omitted.
- `--preset agency|blog|local-business` (default `agency`) controls starter copy and which JSON-LD business schema is emitted (see Section 6).
- `--trailing-slash always|never|ignore` (default `always`) sets Astro `trailingSlash` and matching `build.format` (`always` to `directory`, `never` to `file`, `ignore` to `preserve`). Pick one per client and never mix shapes later. Internal links must route through `formatRoute()` from `src/lib/site.ts`.
- `--site` sets the canonical origin in `astro.config.mjs`. It powers canonical URLs, RSS links, and sitemap emission. Pass the production origin, not a staging URL.
- `--with-sitemap` / `--no-sitemap` are mutually exclusive. By default `@astrojs/sitemap` is wired only when `--site` is supplied and `--staging-lock` is off. Use `--with-sitemap` to force it and `--no-sitemap` for minimal offline builds.
- `--staging-lock` injects Triple-Lock staging protection: `public/robots.txt` with `Disallow: /`, a `noindex` robots meta in the layout, and `public/_headers` with `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet`. Never launch with it still on.
- `--source` is required only for `--mode clone|editorial`. Do not pass a source for a greenfield starter.

Then install and build with the project-local Astro binary only:

```sh
cd client-site
npm install
set ASTRO_TELEMETRY_DISABLED=1
node ./node_modules/astro/bin/astro.mjs build
```

Never run a global `astro` command. On Windows it can resolve to an unrelated Airflow CLI.

## 2. Canonical folder structure

Keep every client repo on this shape so any engineer or coding agent can navigate without hunting:

```text
package.json
astro.config.mjs
ASTRO-BUILD-GUIDELINES.md
public/
  robots.txt
  _headers            (staging lock or host headers)
  _redirects          (created when a slug is renamed)
  images/blog/<slug>/ (localized hero and inline images)
src/
  content.config.ts
  content/
    blog/             (Markdown and MDX posts)
    pages/            (evergreen pages where used)
  lib/
    published.ts      (isPublished filter)
    site.ts           (SITE_CONFIG, formatRoute, JSON-LD builders)
  layouts/
    BaseLayout.astro
    BlogPostLayout.astro
  pages/
    index.astro
    blog/index.astro
    blog/[...id].astro
    rss.xml.ts
    404.astro
  styles/
    global.css
```

`src/pages/blog/[...id].astro` uses a rest parameter so flat posts (`launch-notes.md`) and nested posts (`2026/launch-notes.md`) both resolve through `entry.id`.

## 3. Define collections (src/content.config.ts)

Astro v7 Content Layer config lives at `src/content.config.ts`, never `src/content/config.ts`. Import `glob` from `astro/loaders` and `z` from `astro/zod`. The starter defines `blog` and `pages` collections. The `blog` schema is the contract every publisher and CMS must respect:

```ts
import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

const blog = defineCollection({
  loader: glob({ pattern: "**/*.{md,mdx}", base: "./src/content/blog" }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    pubDate: z.coerce.date(),
    updatedDate: z.coerce.date().optional(),
    author: z.string().optional(),
    categories: z.array(z.string()).default([]),
    tags: z.array(z.string()).default([]),
    heroImage: z.string().optional(),
    heroAlt: z.string().optional(),
    canonicalUrl: z.string().optional(),
    draft: z.boolean().optional(),
  }),
});
```

House rules:

- Use `entry.id`, never the deprecated `entry.slug`.
- Render with `import { render } from "astro:content"` and `const { Content } = await render(entry)`.
- After editing the schema run `node ./node_modules/astro/bin/astro.mjs sync` so TypeScript types regenerate.
- If a client repo renames fields (`featuredImage`, `publishedAt`, `excerpt`), keep the mapping explicit. The Day-2 publisher detects common aliases statically without executing the config, but new code should prefer the canonical names above.

## 4. Gate visibility with isPublished

`src/lib/published.ts` holds a single `isPublished` predicate. In development everything is visible so writers can preview. In production (`import.meta.env.PROD`) an entry is hidden when either condition is true:

- `draft: true` in frontmatter.
- `pubDate` is in the future (scheduled post).

Use the same predicate in every consumer or you will leak content:

- `src/pages/blog/index.astro` listing: `getCollection("blog", isPublished)`.
- `src/pages/blog/[...id].astro` route generation: `getStaticPaths()` maps only `getCollection("blog", isPublished)` to `params: { id: post.id }`.
- `src/pages/index.astro` latest-posts section and `src/pages/rss.xml.ts` feed.

Filtering only the listing while leaving `getStaticPaths()` unfiltered still publishes `dist/blog/secret-draft/index.html`. After every build, `legends-astro verify-build --dist ./dist --project .` scans `src/content/` for drafts and fails if a draft route or body sample appears in `dist/`. Scheduled posts go live only on a later rebuild (see [publishing-and-content-ops.md](publishing-and-content-ops.md)).

## 5. Serve RSS (zero-dep endpoint vs @astrojs/rss)

The starter ships a zero-dependency `src/pages/rss.xml.ts` (`APIRoute` `GET` handler) on purpose.

| Concern | Zero-dep `rss.xml.ts` (default) | `@astrojs/rss` |
|---|---|---|
| Extra packages | None | Adds `@astrojs/rss` plus its dependencies |
| Build without `site` | Succeeds with a fallback origin | Fails the build (see below) |
| Draft and schedule filter | Uses `isPublished` and `formatRoute()` directly | You must filter and map `link` yourself |
| Full HTML feed bodies or XSL | Emits title, description, link, and date items | Better fit via `stylesheet` and sanitized content helpers |

Why `@astrojs/rss` fails when `site` is unset: its options validator requires `site` to be a valid URL string. Calling `rss({ site: context.site, ... })` with no `site` in `astro.config.mjs` throws at build time and `astro build` exits non-zero. Agencies routinely scaffold before the client domain is final, so the default endpoint must never crash pre-launch. It falls back gracefully, XML-escapes output, respects `trailingSlash` through `formatRoute()`, and filters with `isPublished`.

Adopt `@astrojs/rss` deliberately when a project needs an XSL stylesheet or full sanitized HTML in the feed, the production `site` is set, and someone owns the extra dependency. Read the vendored recipe at `vendor/astro-docs/src/content/docs/en/recipes/rss.mdx` and pass `trailingSlash: false` when the site uses `--trailing-slash never`. Note that `@astrojs/sitemap` behaves differently without `site`: it logs a skip message and lets the build succeed.

## 6. One layout shell (BaseLayout and BlogPostLayout)

`src/layouts/BaseLayout.astro` is the only `<head>` owner. It emits charset, viewport, `<title>`, meta description, canonical link, OpenGraph and Twitter tags, the RSS alternate link, an optional sitemap link, global CSS, and staging `noindex` when locked. JSON-LD is serialized with `<` escaped so a title containing markup cannot break out of the script block.

`src/layouts/BlogPostLayout.astro` wraps `BaseLayout`, renders the single `<h1>{title}</h1>`, publication and update dates, author, tags, an optional hero image with alt text, and the article slot. Because the layout owns the H1, Markdown bodies must not start with `# Title`. The sample `src/content/blog/welcome.md` follows this rule.

Structured data comes from builders in `src/lib/site.ts`:

- `Organization` for agency and blog presets, `LocalBusiness` for the local-business preset.
- `BlogPosting` on every post (headline, dates, author, image).
- `BreadcrumbList` on posts (Home to Blog to post title).

`src/lib/site.ts` also exports `SITE_CONFIG` and `formatRoute(path)`. All internal post links use `formatRoute("/blog/" + post.id)` so trailing-slash policy stays consistent across listings, RSS, and breadcrumbs.

## 7. Commit the operating rules (ASTRO-BUILD-GUIDELINES.md)

Every generated repo includes `ASTRO-BUILD-GUIDELINES.md` at the root. Treat it as the contract for downstream engineers and coding agents: project-local Astro invocation, Content Layer file placement, `isPublished` usage, the RSS and sitemap `site` requirement, and the mandatory `verify-build` gate after `astro build`. Keep it committed. When a client repo diverges (custom collection folder, renamed frontmatter field, adapter for server output), record the divergence there in the same pull request.

## 8. Day-1 acceptance checklist

```sh
legends-astro doctor --project ./client-site --json
node ./node_modules/astro/bin/astro.mjs build
legends-astro verify-build --dist ./client-site/dist --project ./client-site --env staging
```

- Build passes with zero leaked drafts and no future-dated post in `dist/`.
- `dist/rss.xml` exists and lists only published posts.
- Sitemap files exist only when `--site` and sitemap wiring are active and staging lock is off.
- Record the first production baseline before launch: `legends-astro verify-build --dist ./dist --project . --write-manifest site-manifest.json --env production`, then commit `site-manifest.json`.

Next: publish and update safely with [publishing-and-content-ops.md](publishing-and-content-ops.md).
