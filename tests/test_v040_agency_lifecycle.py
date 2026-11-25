"""Hermetic v0.4.0 test suite covering greenfield starter (init / scaffold --mode starter), Day-2 publisher (post), regression manifest gate (verify-build --write-manifest / --baseline), and refreshed vendored docs."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from legends_astro.auditor import verify_build
from legends_astro.cli import main as cli_main
from legends_astro.docs_nav import lookup_docs
from legends_astro.publisher import (
    inspect_content_config,
    parse_frontmatter,
    publish_post,
    validate_image_bytes,
    validate_slug,
)
from legends_astro.scaffold import scaffold_astro_project

MODULE_ROOT = Path(__file__).resolve().parent.parent

# Minimal valid magic-byte payloads for hermetic image tests
VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
VALID_JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 16
VALID_GIF = b"GIF89a" + b"\x00" * 16
VALID_WEBP = b"RIFF\x20\x00\x00\x00WEBP" + b"\x00" * 24
VALID_AVIF = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 16
VALID_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'


def _run_cli_json(argv: list[str]) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(argv)
    return rc, json.loads(buf.getvalue())


class TestVendoredDocsRefresh(unittest.TestCase):
    def test_53_vendored_guides_and_new_topics_indexed(self) -> None:
        base = lookup_docs(list_all=True, module_root=MODULE_ROOT)
        self.assertEqual(base["vendoredPageCount"], 53)
        self.assertEqual(base["snapshotCommit"], "7f32508fbe63450b15ff4503a7062c847e1cd0e5")

        all_ids = {item["id"] for item in base["vendoredOfficialDocs"]}
        for new_id in (
            "recipe-rss",
            "integration-mdx",
            "markdown-content",
            "endpoints",
            "cms-index",
            "deploy-index",
            "deploy-netlify",
            "deploy-vercel",
        ):
            self.assertIn(new_id, all_ids)

        for query, expected_id in (
            ("rss", "recipe-rss"),
            ("mdx", "integration-mdx"),
            ("cms", "cms-index"),
            ("deploy", "deploy-index"),
            ("markdown", "markdown-content"),
        ):
            res = lookup_docs(query=query, module_root=MODULE_ROOT)
            ids = [m["id"] for m in res["matches"]]
            self.assertIn(expected_id, ids, f"Expected {expected_id} in query={query!r} matches {ids}")


class TestPillar1GreenfieldStarter(unittest.TestCase):
    def test_init_preview_confirm_and_all_three_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for preset, expected_schema in (
                ("agency", '"@type": "Organization"'),
                ("blog", '"@type": "Organization"'),
                ("local-business", '"@type": "LocalBusiness"'),
            ):
                proj = root / f"site-{preset}"
                prev = scaffold_astro_project(
                    output_dir=proj,
                    mode="starter",
                    name=f"Acme {preset.title()}",
                    preset=preset,
                    site_url="https://acme.example",
                    apply=False,
                )
                self.assertTrue(prev["ok"])
                self.assertFalse(prev["applied"])
                self.assertEqual(prev["preset"], preset)
                self.assertTrue(prev["withSitemap"])
                self.assertFalse((proj / "package.json").exists())

                # Apply without confirm_plan fails closed
                with self.assertRaises(ValueError):
                    scaffold_astro_project(
                        output_dir=proj,
                        mode="starter",
                        name=f"Acme {preset.title()}",
                        preset=preset,
                        site_url="https://acme.example",
                        apply=True,
                    )

                applied = scaffold_astro_project(
                    output_dir=proj,
                    mode="starter",
                    name=f"Acme {preset.title()}",
                    preset=preset,
                    site_url="https://acme.example",
                    apply=True,
                    confirm_plan=prev["planSha256"],
                )
                self.assertTrue(applied["applied"])

                pkg = json.loads((proj / "package.json").read_text(encoding="utf-8"))
                self.assertEqual(pkg["dependencies"]["astro"], "7.4.13")
                self.assertEqual(pkg["dependencies"]["@astrojs/sitemap"], "^3.7.6")
                self.assertEqual(pkg["overrides"]["sharp"], "^0.35.5")
                self.assertEqual(pkg["overrides"]["undici"], "^8.13.1")
                self.assertEqual(pkg["engines"]["node"], ">=22.23.4")

                site_ts = (proj / "src" / "lib" / "site.ts").read_text(encoding="utf-8")
                self.assertIn(expected_schema, site_ts)
                self.assertIn("buildArticleSchema", site_ts)
                self.assertIn("buildBreadcrumbSchema", site_ts)

                pub_ts = (proj / "src" / "lib" / "published.ts").read_text(encoding="utf-8")
                self.assertIn("draft === true", pub_ts)
                self.assertIn("Date.now()", pub_ts)

                base_layout = (proj / "src" / "layouts" / "BaseLayout.astro").read_text(encoding="utf-8")
                self.assertIn(r'\u003c', base_layout)
                self.assertIn("application/rss+xml", base_layout)
                self.assertIn('rel="sitemap"', base_layout)

                rss_ts = (proj / "src" / "pages" / "rss.xml.ts").read_text(encoding="utf-8")
                self.assertIn("context.site ??", rss_ts)
                self.assertIn("escapeXml", rss_ts)
                self.assertIn("isPublished", rss_ts)
                self.assertIn("formatRoute", rss_ts)

                welcome_md = (proj / "src" / "content" / "blog" / "welcome.md").read_text(encoding="utf-8")
                self.assertNotIn("\u2014", welcome_md)
                self.assertNotIn("\u2013", welcome_md)
                _, _, w_body = parse_frontmatter(welcome_md)
                self.assertFalse(w_body.lstrip().startswith("# "))

                guidelines = (proj / "ASTRO-BUILD-GUIDELINES.md").read_text(encoding="utf-8")
                self.assertNotIn("\u2014", guidelines)
                self.assertNotIn("\u2013", guidelines)
                self.assertIn("ASTRO_TELEMETRY_DISABLED=1", guidelines)

                robots = (proj / "public" / "robots.txt").read_text(encoding="utf-8")
                self.assertIn("Sitemap: https://acme.example/sitemap-index.xml", robots)

    def test_trailing_slash_sitemap_flags_staging_lock_and_cli_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # trailingSlash=never + --no-sitemap + --staging-lock via CLI `init`
            proj = root / "staging-never"
            rc_prev, prev = _run_cli_json(
                [
                    "init",
                    "--output",
                    str(proj),
                    "--name",
                    "Staging Lab",
                    "--site",
                    "https://staging.example",
                    "--trailing-slash",
                    "never",
                    "--no-sitemap",
                    "--staging-lock",
                    "--json",
                ]
            )
            self.assertEqual(rc_prev, 0)
            self.assertFalse(prev["applied"])
            self.assertFalse(prev["withSitemap"])
            self.assertEqual(prev["recommendedAstroConfig"]["build.format"], "file")

            rc_apply, applied = _run_cli_json(
                [
                    "init",
                    "--output",
                    str(proj),
                    "--name",
                    "Staging Lab",
                    "--site",
                    "https://staging.example",
                    "--trailing-slash",
                    "never",
                    "--no-sitemap",
                    "--staging-lock",
                    "--apply",
                    "--confirm-plan",
                    prev["planSha256"],
                    "--json",
                ]
            )
            self.assertEqual(rc_apply, 0)
            self.assertTrue(applied["applied"])

            pkg = json.loads((proj / "package.json").read_text(encoding="utf-8"))
            self.assertNotIn("@astrojs/sitemap", pkg["dependencies"])

            cfg = (proj / "astro.config.mjs").read_text(encoding="utf-8")
            self.assertNotIn("@astrojs/sitemap", cfg)
            self.assertIn('trailingSlash: "never"', cfg)
            self.assertIn('format: "file"', cfg)

            self.assertIn("Disallow: /", (proj / "public" / "robots.txt").read_text(encoding="utf-8"))
            self.assertIn("X-Robots-Tag: noindex", (proj / "public" / "_headers").read_text(encoding="utf-8"))

            # trailingSlash=ignore + --with-sitemap without --site via `scaffold --mode starter`
            proj_ig = root / "ignore-sm"
            p_ig = scaffold_astro_project(
                output_dir=proj_ig,
                mode="starter",
                trailing_slash="ignore",
                with_sitemap=True,
                apply=False,
            )
            self.assertEqual(p_ig["recommendedAstroConfig"]["build.format"], "preserve")
            self.assertTrue(p_ig["withSitemap"])
            scaffold_astro_project(
                output_dir=proj_ig,
                mode="starter",
                trailing_slash="ignore",
                with_sitemap=True,
                apply=True,
                confirm_plan=p_ig["planSha256"],
            )
            self.assertIn('format: "preserve"', (proj_ig / "astro.config.mjs").read_text(encoding="utf-8"))
            self.assertIn("sitemap()", (proj_ig / "astro.config.mjs").read_text(encoding="utf-8"))

    def test_clone_editorial_backward_compatibility_and_source_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Missing --source for clone/editorial fails cleanly
            with self.assertRaises(ValueError):
                scaffold_astro_project(output_dir=root / "out", mode="clone")
            with self.assertRaises(ValueError):
                scaffold_astro_project(output_dir=root / "out", mode="editorial")

            # Clone planSha256 matches legacy canonical structure exactly
            src = root / "src-html"
            src.mkdir()
            (src / "index.html").write_text(
                "<!doctype html><html><head><title>Hi</title></head><body><h1>Hi</h1></body></html>",
                encoding="utf-8",
            )
            out = root / "clone-out"
            prev = scaffold_astro_project(source_path=src, output_dir=out, mode="clone", apply=False)
            expected_canonical = json.dumps(
                {
                    "mode": "clone",
                    "sourcePath": str(src.resolve()),
                    "outputDir": str(out.resolve()),
                    "stagingLock": False,
                    "decodeCfEmail": False,
                    "stripTrackers": False,
                    "plannedFiles": [
                        {
                            "path": item["path"],
                            "bytes": item["bytes"],
                            "draft": item.get("draft"),
                            "legacyRoute": item.get("legacyRoute"),
                        }
                        for item in prev["plannedFiles"]
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            expected_sha = hashlib.sha256(expected_canonical.encode("utf-8")).hexdigest()
            self.assertEqual(prev["planSha256"], expected_sha)


class TestPillar2Day2Publisher(unittest.TestCase):
    def _init_starter(self, proj: Path, trailing_slash: str = "always") -> None:
        prev = scaffold_astro_project(
            output_dir=proj,
            mode="starter",
            name="Studio North",
            site_url="https://studionorth.example",
            trailing_slash=trailing_slash,
            apply=False,
        )
        scaffold_astro_project(
            output_dir=proj,
            mode="starter",
            name="Studio North",
            site_url="https://studionorth.example",
            trailing_slash=trailing_slash,
            apply=True,
            confirm_plan=prev["planSha256"],
        )

    def test_create_list_update_custom_frontmatter_and_strip_h1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "site"
            self._init_starter(proj)

            draft_dir = root / "incoming"
            draft_dir.mkdir()
            hero_img = draft_dir / "cover.png"
            hero_img.write_bytes(VALID_PNG)
            inline_img = draft_dir / "diagram.webp"
            inline_img.write_bytes(VALID_WEBP)

            draft_md = draft_dir / "launch-guide.md"
            draft_md.write_text(
                "---\n"
                'title: "Production Launch Guide for Modern Astro Teams"\n'
                'description: "Step-by-step release checklist covering Astro v7 Content Layer builds, asset optimization, and post-build verification gates."\n'
                'pubDate: "2026-10-15"\n'
                'customBadge: "featured-tier-1"\n'
                "seoScore: 98\n"
                "---\n\n"
                "# Duplicate Body Heading\n\n"
                "## Architecture Overview\n\n"
                "![System architecture diagram](diagram.webp)\n",
                encoding="utf-8",
            )

            # Preview create (without --strip-body-h1 -> emits duplicate_h1 warning)
            prev_warn = publish_post(
                project_dir=proj,
                input_path=draft_md,
                slug="launch-guide",
                hero=str(hero_img),
                hero_alt="Cover artwork for launch guide",
                publish_flag=True,
                apply=False,
            )
            self.assertFalse(prev_warn["applied"])
            self.assertTrue(prev_warn["lint"]["ok"])
            self.assertIn("duplicate_h1", {w["code"] for w in prev_warn["lint"]["warnings"]})
            self.assertFalse((proj / "src" / "content" / "blog" / "launch-guide.md").exists())

            # Preview & apply with --strip-body-h1
            prev = publish_post(
                project_dir=proj,
                input_path=draft_md,
                slug="launch-guide",
                hero=str(hero_img),
                hero_alt="Cover artwork for launch guide",
                tags=["astro", "launch"],
                publish_flag=True,
                strip_body_h1=True,
                apply=False,
            )
            self.assertTrue(prev["h1Stripped"])
            self.assertNotIn("duplicate_h1", {w["code"] for w in prev["lint"]["warnings"]})

            applied = publish_post(
                project_dir=proj,
                input_path=draft_md,
                slug="launch-guide",
                hero=str(hero_img),
                hero_alt="Cover artwork for launch guide",
                tags=["astro", "launch"],
                publish_flag=True,
                strip_body_h1=True,
                apply=True,
                confirm_plan=prev["planSha256"],
            )
            self.assertTrue(applied["applied"])
            self.assertTrue((proj / "public" / "images" / "blog" / "launch-guide" / "cover.png").is_file())
            self.assertTrue((proj / "public" / "images" / "blog" / "launch-guide" / "diagram.webp").is_file())

            post_txt = (proj / "src" / "content" / "blog" / "launch-guide.md").read_text(encoding="utf-8")
            self.assertIn('heroImage: "/images/blog/launch-guide/cover.png"', post_txt)
            self.assertIn("![System architecture diagram](/images/blog/launch-guide/diagram.webp)", post_txt)
            self.assertNotIn("# Duplicate Body Heading", post_txt)
            self.assertIn('customBadge: "featured-tier-1"', post_txt)

            # Inventory (--list) via CLI
            rc_list, inv = _run_cli_json(["post", "--project", str(proj), "--list", "--json"])
            self.assertEqual(rc_list, 0)
            self.assertEqual(inv["entryCount"], 2)
            by_slug = {e["slug"]: e for e in inv["entries"]}
            self.assertIn("launch-guide", by_slug)
            self.assertEqual(by_slug["launch-guide"]["pubDate"], "2026-10-15")
            self.assertFalse(by_slug["launch-guide"]["draft"])
            self.assertFalse(by_slug["launch-guide"]["scheduled"])
            self.assertGreater(by_slug["launch-guide"]["wordCount"], 0)

            # Update mode: preserves pubDate, stamps updatedDate, preserves custom frontmatter keys
            upd_prev = publish_post(
                project_dir=proj,
                update=True,
                slug="launch-guide",
                title="Production Launch Guide for Modern Astro Teams (2026)",
                apply=False,
            )
            upd_done = publish_post(
                project_dir=proj,
                update=True,
                slug="launch-guide",
                title="Production Launch Guide for Modern Astro Teams (2026)",
                apply=True,
                confirm_plan=upd_prev["planSha256"],
            )
            self.assertTrue(upd_done["applied"])
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.assertEqual(upd_done["pubDate"], "2026-10-15")
            self.assertEqual(upd_done["updatedDate"], today_utc)
            updated_txt = (proj / "src" / "content" / "blog" / "launch-guide.md").read_text(encoding="utf-8")
            self.assertIn('pubDate: "2026-10-15"', updated_txt)
            self.assertIn(f'updatedDate: "{today_utc}"', updated_txt)
            self.assertIn('customBadge: "featured-tier-1"', updated_txt)
            self.assertIn("seoScore: 98", updated_txt)

    def test_rename_from_redirects_dedup_chain_flatten_and_cycle_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "site"
            self._init_starter(proj, trailing_slash="always")

            # Seed existing _redirects with a prior hop pointing to welcome + duplicate rule
            (proj / "public" / "_redirects").write_text(
                "# Legacy hops\n"
                "/blog/ancient/ /blog/welcome/ 301\n"
                "/blog/welcome/ /blog/old-dest/ 301\n",
                encoding="utf-8",
            )

            prev = publish_post(
                project_dir=proj,
                rename_from="welcome",
                slug="getting-started",
                apply=False,
            )
            self.assertEqual(prev["plannedRemovals"], ["src/content/blog/welcome.md"])
            # Preview did NOT remove old file or modify _redirects
            self.assertTrue((proj / "src" / "content" / "blog" / "welcome.md").is_file())
            self.assertFalse((proj / "src" / "content" / "blog" / "getting-started.md").exists())

            applied = publish_post(
                project_dir=proj,
                rename_from="welcome",
                slug="getting-started",
                apply=True,
                confirm_plan=prev["planSha256"],
            )
            self.assertTrue(applied["applied"])
            self.assertFalse((proj / "src" / "content" / "blog" / "welcome.md").exists())
            self.assertTrue((proj / "src" / "content" / "blog" / "getting-started.md").is_file())

            redirects_txt = (proj / "public" / "_redirects").read_text(encoding="utf-8")
            # Flattened chain: /blog/ancient/ now points directly to /blog/getting-started/
            self.assertIn("/blog/ancient/ /blog/getting-started/ 301", redirects_txt)
            # Deduplicated: old /blog/welcome/ -> /blog/old-dest/ replaced by /blog/getting-started/
            self.assertNotIn("/blog/old-dest/", redirects_txt)
            self.assertIn("/blog/welcome/ /blog/getting-started/ 301", redirects_txt)

            # Cycle refusal: trying to rename getting-started back to welcome would create cycle!
            with self.assertRaises(ValueError):
                publish_post(
                    project_dir=proj,
                    rename_from="getting-started",
                    slug="welcome",
                    apply=False,
                )

    def test_custom_content_config_and_field_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "custom-site"
            (proj / "src" / "content" / "articles").mkdir(parents=True)
            (proj / "src" / "content.config.ts").write_text(
                'import { defineCollection } from "astro:content";\n'
                'import { glob } from "astro/loaders";\n'
                'import { z } from "astro/zod";\n\n'
                "const articles = defineCollection({\n"
                '  loader: glob({ pattern: "**/*.md", base: "./src/content/articles" }),\n'
                "  schema: z.object({\n"
                "    title: z.string(),\n"
                "    excerpt: z.string(),\n"
                "    publishedAt: z.coerce.date(),\n"
                "    updatedAt: z.coerce.date().optional(),\n"
                "    featuredImage: z.string().optional(),\n"
                "    imageAlt: z.string().optional(),\n"
                "    draft: z.boolean().optional(),\n"
                "  }),\n"
                "});\n"
                "export const collections = { articles };\n",
                encoding="utf-8",
            )
            info = inspect_content_config(proj, "articles")
            self.assertEqual(info["collectionBase"], "src/content/articles")
            self.assertEqual(info["fieldMap"]["description"], "excerpt")
            self.assertEqual(info["fieldMap"]["pubDate"], "publishedAt")
            self.assertEqual(info["fieldMap"]["heroImage"], "featuredImage")

            body_f = root / "body.md"
            body_f.write_text("## Section\n\nClean content paragraph.\n", encoding="utf-8")
            img_f = root / "photo.jpg"
            img_f.write_bytes(VALID_JPG)

            prev = publish_post(
                project_dir=proj,
                collection="articles",
                title="Custom Consumer Schema Compatibility Verification",
                description="Ensuring static content.config.ts parsing resolves custom base folders and field aliases without executing JS.",
                body_file=body_f,
                hero=str(img_f),
                hero_alt="Custom schema photo",
                publish_flag=True,
                apply=False,
            )
            publish_post(
                project_dir=proj,
                collection="articles",
                title="Custom Consumer Schema Compatibility Verification",
                description="Ensuring static content.config.ts parsing resolves custom base folders and field aliases without executing JS.",
                body_file=body_f,
                hero=str(img_f),
                hero_alt="Custom schema photo",
                publish_flag=True,
                apply=True,
                confirm_plan=prev["planSha256"],
            )
            written = (proj / "src" / "content" / "articles" / f"{prev['slug']}.md").read_text(encoding="utf-8")
            self.assertIn("excerpt:", written)
            self.assertIn("publishedAt:", written)
            self.assertIn("featuredImage:", written)
            self.assertIn("imageAlt:", written)

    def test_security_magic_bytes_svg_xss_slug_traversal_symlink_yaml_injection_and_lint(self) -> None:
        # Magic bytes: valid formats pass
        validate_image_bytes(".png", VALID_PNG)
        validate_image_bytes(".jpg", VALID_JPG)
        validate_image_bytes(".gif", VALID_GIF)
        validate_image_bytes(".webp", VALID_WEBP)
        validate_image_bytes(".avif", VALID_AVIF)
        validate_image_bytes(".svg", VALID_SVG)

        # Disguised HTML as PNG refused
        with self.assertRaises(ValueError):
            validate_image_bytes(".png", b"<html><script>alert(1)</script></html>")

        # SVG XSS refused (CWE-79)
        with self.assertRaises(ValueError):
            validate_image_bytes(".svg", b"<svg><script>alert(1)</script></svg>")
        with self.assertRaises(ValueError):
            validate_image_bytes(".svg", b'<svg onload="alert(1)"></svg>')

        # CWE-22 slug traversal & Windows reserved names refused
        for bad_slug in ("../etc/passwd", "a/b", "a\\b", "bad\x00slug", "con", "nul", "COM1", "Bad_Upper"):
            with self.assertRaises(ValueError, msg=f"Expected bad slug to fail: {bad_slug!r}"):
                validate_slug(bad_slug)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "site"
            self._init_starter(proj)

            # Mutual exclusion --draft + --publish
            with self.assertRaises(ValueError):
                publish_post(project_dir=proj, title="Both Flags", draft_flag=True, publish_flag=True)

            # CWE-59 symlink refusal
            with mock.patch("legends_astro.net_safety.is_reparse_or_symlink", return_value=True):
                with self.assertRaises(ValueError):
                    publish_post(project_dir=proj, list_only=True)

            # CWE-74 YAML injection escaped cleanly onto single line
            body_ok = root / "clean.md"
            body_ok.write_text("## Valid Body\n\nAll clean here.\n", encoding="utf-8")
            inj_prev = publish_post(
                project_dir=proj,
                slug="yaml-safe",
                title="Safe Title\ndraft: false",
                description="A complete description that avoids any frontmatter injection issues when serialized via json dumps for YAML safety.",
                body_file=body_ok,
                draft_flag=True,
                apply=False,
            )
            publish_post(
                project_dir=proj,
                slug="yaml-safe",
                title="Safe Title\ndraft: false",
                description="A complete description that avoids any frontmatter injection issues when serialized via json dumps for YAML safety.",
                body_file=body_ok,
                draft_flag=True,
                apply=True,
                confirm_plan=inj_prev["planSha256"],
            )
            yaml_txt = (proj / "src" / "content" / "blog" / "yaml-safe.md").read_text(encoding="utf-8")
            fm_parsed, _, _ = parse_frontmatter(yaml_txt)
            self.assertEqual(fm_parsed["title"], "Safe Title draft: false")
            self.assertIs(fm_parsed["draft"], True)

            # Editorial SEO lint errors block --apply
            bad_body = root / "bad.md"
            bad_body.write_text("Intro with TODO and ![](missing-local.png)\n", encoding="utf-8")
            lint_prev = publish_post(
                project_dir=proj,
                slug="lint-fail",
                title="Short",
                body_file=bad_body,
                hero="/images/does-not-exist.png",
                strict_typography=True,
                apply=False,
            )
            self.assertFalse(lint_prev["lint"]["ok"])
            err_codes = {e["code"] for e in lint_prev["lint"]["errors"]}
            self.assertIn("missing_hero_alt", err_codes)
            self.assertIn("missing_inline_image_alt", err_codes)
            self.assertIn("broken_local_image", err_codes)
            self.assertIn("placeholder_content", err_codes)
            warn_codes = {w["code"] for w in lint_prev["lint"]["warnings"]}
            self.assertIn("title_length", warn_codes)
            self.assertIn("missing_description", warn_codes)

            with self.assertRaises(ValueError):
                publish_post(
                    project_dir=proj,
                    slug="lint-fail",
                    title="Short",
                    body_file=bad_body,
                    hero="/images/does-not-exist.png",
                    apply=True,
                    confirm_plan=lint_prev["planSha256"],
                )


class TestPillar3BaselineManifestGate(unittest.TestCase):
    def _write_page(
        self,
        dist: Path,
        rel_path: str,
        *,
        title: str | None = "Page Title",
        desc: str | None = "Meta description here.",
        canonical: str | None = "https://site.example/",
        h1: str | None = "Main Heading",
        jsonld_type: str | None = "Organization",
    ) -> None:
        target = dist / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        head_parts = ['<meta charset="utf-8">']
        if title:
            head_parts.append(f"<title>{title}</title>")
        if desc:
            head_parts.append(f'<meta name="description" content="{desc}">')
        if canonical:
            head_parts.append(f'<link rel="canonical" href="{canonical}">')
        if jsonld_type:
            head_parts.append(
                f'<script type="application/ld+json">{{"@context":"https://schema.org","@type":"{jsonld_type}"}}</script>'
            )
        body_inner = f"<h1>{h1}</h1><p>Content</p>" if h1 else "<p>Content</p>"
        target.write_text(
            f"<!doctype html><html><head>{''.join(head_parts)}</head><body>{body_inner}</body></html>",
            encoding="utf-8",
        )

    def test_write_manifest_baseline_add_rename_and_regression_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            manifest_path = root / "site-manifest.json"

            self._write_page(dist, "index.html", title="Home", canonical="https://site.example/", jsonld_type="Organization")
            self._write_page(
                dist,
                "blog/hello/index.html",
                title="Hello",
                canonical="https://site.example/blog/hello/",
                jsonld_type="BlogPosting",
            )
            self._write_page(dist, "404.html", title="404", canonical=None, jsonld_type=None)
            (dist / "assets").mkdir(parents=True)
            (dist / "assets" / "app.css").write_text("body{color:#111;}\n", encoding="utf-8")

            # 1. Write initial clean manifest via CLI
            rc, rep = _run_cli_json(
                [
                    "verify-build",
                    "--dist",
                    str(dist),
                    "--write-manifest",
                    str(manifest_path),
                    "--json",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(rep["ok"])
            self.assertEqual(rep["manifestWrittenTo"], str(manifest_path.resolve()))
            mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(mdata["schema"], "legends.astro-site-manifest/v1")
            self.assertEqual(mdata["summary"]["routeCount"], 3)
            self.assertEqual(mdata["summary"]["assetCount"], 1)
            self.assertEqual(mdata["summary"]["assetsByType"][".css"], 1)

            # 2. Clean Day-2 addition + asset passes --baseline
            self._write_page(
                dist,
                "blog/new-post/index.html",
                title="New Post",
                canonical="https://site.example/blog/new-post/",
                jsonld_type="BlogPosting",
            )
            (dist / "assets" / "cover.png").write_bytes(VALID_PNG)
            rep_add = verify_build(dist, baseline_path=manifest_path)
            self.assertTrue(rep_add["ok"])
            self.assertEqual(rep_add["baselineDiff"]["addedRoutes"], ["/blog/new-post/"])
            self.assertEqual(rep_add["baselineDiff"]["assetCountDelta"], 1)

            # 3. Unredirected removal fails closed with baseline_route_missing AND refuses --write-manifest overwrite
            (dist / "blog" / "hello" / "index.html").unlink()
            orig_manifest_bytes = manifest_path.read_bytes()
            rep_removed = verify_build(
                dist,
                baseline_path=manifest_path,
                write_manifest_path=manifest_path,
                overwrite=True,
            )
            self.assertFalse(rep_removed["ok"])
            self.assertIn("baseline_route_missing", {f["code"] for f in rep_removed["failures"]})
            self.assertEqual(rep_removed["baselineDiff"]["removedRoutes"], ["/blog/hello/"])
            self.assertIsNone(rep_removed["manifestWrittenTo"])
            self.assertEqual(rep_removed["manifestSkippedReason"], "verification_failed")
            self.assertEqual(manifest_path.read_bytes(), orig_manifest_bytes)

            # 4. Covering the removed route with a valid 301 redirect in _redirects passes!
            (dist / "_redirects").write_text("/blog/hello/ /blog/new-post/ 301\n", encoding="utf-8")
            rep_redir = verify_build(dist, baseline_path=manifest_path)
            self.assertTrue(rep_redir["ok"], f"Unexpected failures: {rep_redir['failures']}")
            self.assertEqual(
                rep_redir["baselineDiff"]["redirectedRoutes"],
                [{"from": "/blog/hello/", "to": "/blog/new-post/", "status": 301}],
            )

            # 5. Surviving route metadata/schema/canonical regressions fail closed
            self._write_page(
                dist,
                "index.html",
                title=None,
                desc=None,
                canonical="https://other.example/",
                h1=None,
                jsonld_type=None,
            )
            rep_reg = verify_build(dist, baseline_path=manifest_path)
            self.assertFalse(rep_reg["ok"])
            reg_codes = {f["code"] for f in rep_reg["failures"]}
            for expected_code in (
                "baseline_canonical_changed",
                "baseline_title_lost",
                "baseline_description_lost",
                "baseline_h1_lost",
                "baseline_jsonld_type_lost",
            ):
                self.assertIn(expected_code, reg_codes)

            # 6. --allow-canonical-change suppresses baseline_canonical_changed (while still catching canonical_lost)
            rep_allow_canon = verify_build(dist, baseline_path=manifest_path, allow_canonical_change=True)
            self.assertNotIn("baseline_canonical_changed", {f["code"] for f in rep_allow_canon["failures"]})

            self._write_page(dist, "index.html", title="Home", desc="Desc", canonical=None, h1="Home", jsonld_type="Organization")
            rep_lost_canon = verify_build(dist, baseline_path=manifest_path)
            self.assertIn("baseline_canonical_lost", {f["code"] for f in rep_lost_canon["failures"]})


if __name__ == "__main__":
    unittest.main()
