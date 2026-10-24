# Content collections (Astro v7 Content Layer)

Cross-read: [pages-components-styles-scripts.md](pages-components-styles-scripts.md), [migrate-and-wordpress.md](migrate-and-wordpress.md).
Vendored official docs: `vendor/astro-docs/src/content/docs/en/guides/content-collections.mdx`.

## 1. Astro v6/v7 Content Layer vs. legacy Astro v4/v5

Frontier models frequently generate Astro v4/v5 collection code that fails immediately in Astro v6 and v7. Use this exact table:

| Feature | Legacy (Astro v4/v5 - BROKEN in v7) | Current Astro v7 Content Layer (REQUIRED) |
|---|---|---|
| Config file path | `src/content/config.ts` | `src/content.config.ts` (or `.mjs`/`.js` at `src/` root) |
| Collection definition | `defineCollection({ type: 'content', schema })` | `defineCollection({ loader: glob(...), schema })` |
| Loader import | None | `import { glob, file } from "astro/loaders";` |
| Zod import | `import { z } from "astro:content";` | `import { z } from "astro/zod";` |
| Entry identifier | `entry.slug` | `entry.id` |
| Rendering entry | `const { Content } = await entry.render();` | `import { render } from "astro:content"; const { Content } = await render(entry);` |

## 2. Canonical `src/content.config.ts` template

```ts
import { defineCollection } from "astro:content";
import { glob } from "astro/loaders";
import { z } from "astro/zod";

const blog = defineCollection({
  loader: glob({
    pattern: "**/*.{md,mdx}",
    base: "./src/content/blog",
  }),
  schema: z.object({
    title: z.string(),
    description: z.string(),
    pubDate: z.coerce.date(),
    updatedDate: z.coerce.date().optional(),
    canonicalUrl: z.string().url().optional(),
    draft: z.boolean().optional(),
  }),
});

export const collections = { blog };
```

After creating or editing `src/content.config.ts`, generate types with:

```sh
ASTRO_TELEMETRY_DISABLED=1 node ./node_modules/astro/bin/astro.mjs sync
```

## 3. Preventing draft route leaks (`getStaticPaths` + index list)

A common bug in Astro sites is filtering drafts on the blog index page (`getCollection('blog', ({ data }) => !data.draft)`) while forgetting to filter `getStaticPaths()` in `src/pages/blog/[id].astro`. That hides the link on the homepage **but still builds and publishes `dist/blog/secret-draft/index.html`**.

Always share a single predicate in `src/lib/published.ts`:

```ts
export function isPublished(entry: { data: { draft?: boolean } }): boolean {
  if (!import.meta.env.PROD) return true;
  return entry.data.draft !== true;
}
```

Use `isPublished` in both the index listing and dynamic route generation:

```ts
import { getCollection, render } from "astro:content";
import { isPublished } from "../../lib/published";

export async function getStaticPaths() {
  const posts = await getCollection("blog", isPublished);
  return posts.map((post) => ({
    params: { id: post.id },
    props: { post },
  }));
}

const { post } = Astro.props;
const { Content } = await render(post);
```

## 4. Mechanical verification

After building, run `verify-build` with both `--dist` and `--project`:

```sh
python -m legends_astro verify-build --dist ./dist --project .
```

`verify-build` scans `src/content/**/*.(md|mdx)` for `draft: true` entries and verifies that neither a route matching the draft ID nor any sample of the draft body text exists anywhere in `dist/`.
