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

            # Clone planSha256 includes per-file sha256 and site, and invalidates on same-length TOCTOU mutations
            src = root / "src-html"
            src.mkdir()
            (src / "index.html").write_text(
                "<!doctype html><html><head><title>Hi</title></head><body><h1>AAAA</h1></body></html>",
                encoding="utf-8",
            )
            out = root / "clone-out"
            prev = scaffold_astro_project(
                source_path=src,
                output_dir=out,
                mode="clone",
                site_url="https://site-a.example",
                apply=False,
            )
            self.assertTrue(all("sha256" in item for item in prev["plannedFiles"]))
            expected_canonical = json.dumps(
                {
                    "mode": "clone",
                    "sourcePath": str(src.resolve()),
                    "outputDir": str(out.resolve()),
                    "site": "https://site-a.example",
                    "stagingLock": False,
                    "decodeCfEmail": False,
                    "stripTrackers": False,
                    "plannedFiles": [
                        {
                            "path": item["path"],
                            "bytes": item["bytes"],
                            "sha256": item["sha256"],
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

            # Same-length source HTML mutation invalidates planSha256
            (src / "index.html").write_text(
                "<!doctype html><html><head><title>Hi</title></head><body><h1>BBBB</h1></body></html>",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                scaffold_astro_project(
                    source_path=src,
                    output_dir=out,
                    mode="clone",
                    site_url="https://site-a.example",
                    apply=True,
                    confirm_plan=prev["planSha256"],
                )


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


class TestStage4aBattleTestRegressions(unittest.TestCase):
    """Dedicated regression tests for Stage 3 battle-test findings BT-01 through BT-06."""

    def _init_starter(self, proj: Path, *, preset: str = "agency", trailing_slash: str = "always") -> dict:
        prev = scaffold_astro_project(
            output_dir=proj,
            mode="starter",
            name=f"Acme {preset.title()}",
            preset=preset,
            site_url="https://acme.example",
            trailing_slash=trailing_slash,
            apply=False,
        )
        return scaffold_astro_project(
            output_dir=proj,
            mode="starter",
            name=f"Acme {preset.title()}",
            preset=preset,
            site_url="https://acme.example",
            trailing_slash=trailing_slash,
            apply=True,
            confirm_plan=prev["planSha256"],
        )

    def _write_html(self, path: Path, *, title: str, canonical: str | None = None, body: str = "<p>Body</p>") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        canon = f'<link rel="canonical" href="{canonical}">' if canonical else ""
        path.write_text(
            f'<!doctype html><html><head><title>{title}</title><meta name="description" content="Description text">{canon}</head>'
            f"<body><h1>{title}</h1>{body}</body></html>",
            encoding="utf-8",
        )

    def test_bt01_baseline_redirect_lookup_html_and_extensionless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Case A: trailingSlash: "never" (build.format: "file") -> dist/blog/welcome.html + extensionless _redirects
            proj = root / "site-never"
            self._init_starter(proj, preset="blog", trailing_slash="never")
            dist = proj / "dist"
            manifest = proj / "site-manifest.json"
            self._write_html(dist / "index.html", title="Home", canonical="https://acme.example/")
            self._write_html(dist / "blog.html", title="Blog", canonical="https://acme.example/blog")
            self._write_html(dist / "blog" / "welcome.html", title="Welcome", canonical="https://acme.example/blog/welcome")

            rep0 = verify_build(dist, project_dir=proj, write_manifest_path=manifest)
            self.assertTrue(rep0["ok"], f"Initial verify failed: {rep0['failures']}")

            ren_prev = publish_post(project_dir=proj, rename_from="welcome", slug="welcome-v2", apply=False)
            publish_post(
                project_dir=proj,
                rename_from="welcome",
                slug="welcome-v2",
                apply=True,
                confirm_plan=ren_prev["planSha256"],
            )
            redirects_txt = (proj / "public" / "_redirects").read_text(encoding="utf-8")
            self.assertIn("/blog/welcome /blog/welcome-v2 301", redirects_txt)

            # Simulate rebuilt dist/ under build.format: "file"
            (dist / "blog" / "welcome.html").unlink()
            self._write_html(dist / "blog" / "welcome-v2.html", title="Welcome V2", canonical="https://acme.example/blog/welcome-v2")
            (dist / "_redirects").write_text(redirects_txt, encoding="utf-8")

            rep1 = verify_build(dist, project_dir=proj, baseline_path=manifest)
            self.assertTrue(rep1["ok"], f"BT-01 regression failed: {rep1['failures']}")
            self.assertEqual(
                rep1["baselineDiff"]["redirectedRoutes"],
                [{"from": "/blog/welcome.html", "to": "/blog/welcome-v2", "status": 301}],
            )
            self.assertEqual(rep1["baselineDiff"]["removedRoutes"], [])
            self.assertEqual(rep1["baselineDiff"]["addedRoutes"], ["/blog/welcome-v2.html"])

            # Case B: trailingSlash: "always" baseline route /blog/old/ covered by .html source rule in _redirects
            dist_a = root / "dist-always"
            manifest_a = root / "manifest-always.json"
            self._write_html(dist_a / "index.html", title="Home", canonical="https://acme.example/")
            self._write_html(dist_a / "blog" / "old" / "index.html", title="Old", canonical="https://acme.example/blog/old/")
            self.assertTrue(verify_build(dist_a, write_manifest_path=manifest_a)["ok"])

            (dist_a / "blog" / "old" / "index.html").unlink()
            self._write_html(dist_a / "blog" / "new" / "index.html", title="New", canonical="https://acme.example/blog/new/")
            (dist_a / "_redirects").write_text("/blog/old.html /blog/new/ 301\n", encoding="utf-8")

            rep_a = verify_build(dist_a, baseline_path=manifest_a)
            self.assertTrue(rep_a["ok"], f"Reverse .html redirect lookup failed: {rep_a['failures']}")
            self.assertEqual(
                rep_a["baselineDiff"]["redirectedRoutes"],
                [{"from": "/blog/old/", "to": "/blog/new/", "status": 301}],
            )

    def test_bt02_inline_markdown_image_path_traversal_refused_while_cli_hero_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "site"
            self._init_starter(proj)

            # Secret PNG outside input directory
            outside_png = root / "parent-secret.png"
            outside_png.write_bytes(VALID_PNG)
            # Existing public asset inside project public/
            pub_img = proj / "public" / "images" / "approved.png"
            pub_img.parent.mkdir(parents=True, exist_ok=True)
            pub_img.write_bytes(VALID_PNG)

            inputs_dir = root / "adv-inputs"
            inputs_dir.mkdir()
            valid_desc = "Security regression verification ensuring inline Markdown image localization blocks parent directory traversal and absolute paths."

            traversal_cases = [
                ("parent-rel", "![Parent traversal](../parent-secret.png)\n"),
                ("nested-rel", "![Nested traversal](sub/../../parent-secret.png)\n"),
                ("win-drive", r"![Drive](C:\Windows\secret.png)" + "\n"),
                ("unc-path", r"![UNC](\\server\share\secret.png)" + "\n"),
                ("posix-abs", "![Posix](/etc/passwd)\n"),
                ("root-trav", "![Root traversal](/../parent-secret.png)\n"),
            ]
            for name, md_body in traversal_cases:
                bf = inputs_dir / f"{name}.md"
                bf.write_text(md_body, encoding="utf-8")
                with self.assertRaises(ValueError, msg=f"Expected CWE-22 refusal for case {name}") as ctx:
                    publish_post(
                        project_dir=proj,
                        slug=f"probe-{name}",
                        title="Inline Markdown Image Security Containment Probe",
                        description=valid_desc,
                        body_file=bf,
                        apply=False,
                    )
                self.assertIn("CWE-22", str(ctx.exception))

            # Allowed: root-relative public/ web path + explicit CLI --hero from outside input dir
            safe_bf = inputs_dir / "safe.md"
            safe_bf.write_text("![Approved public image](/images/approved.png)\n", encoding="utf-8")
            prev_ok = publish_post(
                project_dir=proj,
                slug="safe-hero-and-public",
                title="Safe Operator Hero Image and Public Inline Reference",
                description=valid_desc,
                body_file=safe_bf,
                hero=str(outside_png),
                hero_alt="Operator chosen hero image",
                apply=False,
            )
            self.assertTrue(prev_ok["ok"])
            self.assertTrue(prev_ok["lint"]["ok"], f"Unexpected lint errors: {prev_ok['lint']['errors']}")
            self.assertEqual(prev_ok["heroImage"], "/images/blog/safe-hero-and-public/parent-secret.png")

    def test_bt03_verify_build_detects_scheduled_post_leaks_html_sitemap_rss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "site"
            self._init_starter(proj)

            # Add two non-draft future-dated posts (YYYY-MM-DD and ISO-8601)
            (proj / "src" / "content" / "blog" / "future-launch-2099.md").write_text(
                "---\n"
                'title: "Future Launch Announcement 2099"\n'
                'description: "Scheduled launch announcement that must stay out of production builds until 2099."\n'
                'pubDate: "2099-06-15"\n'
                "draft: false\n"
                "---\n\n"
                "CANARY_FUTURE_LEAK_7733 confidential embargoed product details.\n",
                encoding="utf-8",
            )
            (proj / "src" / "content" / "blog" / "future-iso-2099.md").write_text(
                "---\n"
                'title: "Future ISO Timestamp Announcement"\n'
                'description: "Scheduled ISO timestamp post that must stay out of production builds until 2099."\n'
                'pubDate: "2099-12-31T23:59:59Z"\n'
                "draft: false\n"
                "---\n\n"
                "CANARY_FUTURE_ISO_8844 embargoed details.\n",
                encoding="utf-8",
            )

            dist = proj / "dist"
            self._write_html(dist / "index.html", title="Home", canonical="https://acme.example/")
            self._write_html(dist / "blog" / "welcome" / "index.html", title="Welcome", canonical="https://acme.example/blog/welcome/")

            # 1. Clean build with zero leaks passes and reports scheduledEntriesChecked == 2
            rep_clean = verify_build(dist, project_dir=proj)
            self.assertTrue(rep_clean["ok"], f"Clean build failed: {rep_clean['failures']}")
            self.assertEqual(rep_clean["scheduledEntriesChecked"], 2)

            # 2. Leak scheduled HTML route + body + sitemap entry
            leaked_html = dist / "blog" / "future-launch-2099" / "index.html"
            self._write_html(
                leaked_html,
                title="Future Launch Announcement 2099",
                canonical="https://acme.example/blog/future-launch-2099/",
                body="<p>CANARY_FUTURE_LEAK_7733 confidential embargoed product details.</p>",
            )
            (dist / "sitemap-0.xml").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                "  <url><loc>https://acme.example/</loc></url>\n"
                "  <url><loc>https://acme.example/blog/future-launch-2099/</loc></url>\n"
                "</urlset>\n",
                encoding="utf-8",
            )
            rep_leaked = verify_build(dist, project_dir=proj)
            self.assertFalse(rep_leaked["ok"])
            codes = {f["code"] for f in rep_leaked["failures"]}
            self.assertIn("scheduled_post_route_leaked", codes)
            self.assertIn("scheduled_post_body_leaked", codes)
            self.assertIn("scheduled_post_in_sitemap", codes)

            # 3. Remove HTML + sitemap leak, leak ONLY via dist/rss.xml
            leaked_html.unlink()
            (dist / "sitemap-0.xml").unlink()
            (dist / "rss.xml").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<rss version="2.0"><channel><title>Acme</title><link>https://acme.example/</link><description>Feed</description>'
                "<item><title>Future ISO</title><link>https://acme.example/blog/future-iso-2099/</link>"
                "<description>CANARY_FUTURE_ISO_8844 embargoed details.</description></item>"
                "</channel></rss>\n",
                encoding="utf-8",
            )
            rep_rss = verify_build(dist, project_dir=proj)
            self.assertFalse(rep_rss["ok"])
            rss_codes = {f["code"] for f in rep_rss["failures"]}
            self.assertIn("scheduled_post_route_leaked", rss_codes)
            self.assertIn("scheduled_post_body_leaked", rss_codes)

    def test_bt04_starter_includes_pages_gitkeep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "starter-pages"
            applied = self._init_starter(proj)
            planned_paths = {item["path"] for item in applied["plannedFiles"]}
            self.assertIn("src/content/pages/.gitkeep", planned_paths)
            gitkeep = proj / "src" / "content" / "pages" / ".gitkeep"
            self.assertTrue(gitkeep.is_file())
            self.assertEqual(gitkeep.read_text(encoding="utf-8"), "")

    def test_bt05_starter_welcome_description_length_and_404_no_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for preset in ("agency", "blog", "local-business"):
                proj = root / f"site-{preset}"
                self._init_starter(proj, preset=preset)
                inv = publish_post(project_dir=proj, list_only=True)
                self.assertEqual(inv["entryCount"], 1)
                welcome = inv["entries"][0]
                desc = welcome["description"] or ""
                self.assertGreaterEqual(len(desc), 120, f"{preset} welcome description too short ({len(desc)}): {desc}")
                self.assertLessEqual(len(desc), 150, f"{preset} welcome description too long ({len(desc)}): {desc}")
                warn_codes = {w["code"] for w in welcome["lint"]["warnings"]}
                self.assertNotIn("description_length", warn_codes, f"{preset} emitted description_length warning")

                layout = (proj / "src" / "layouts" / "BaseLayout.astro").read_text(encoding="utf-8")
                self.assertIn("suppressCanonical", layout)
                self.assertIn("!suppressCanonical && resolvedCanonical", layout)
                page_404 = (proj / "src" / "pages" / "404.astro").read_text(encoding="utf-8")
                self.assertIn('robots="noindex, nofollow"', page_404)

    def test_bt06_custom_collection_image_dir_and_svg_card_verify_build_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "custom-col"
            (proj / "src" / "content" / "articles").mkdir(parents=True)
            (proj / "src" / "content.config.ts").write_text(
                'import { defineCollection } from "astro:content";\n'
                'import { glob } from "astro/loaders";\n'
                'import { z } from "astro/zod";\n'
                "const articles = defineCollection({\n"
                '  loader: glob({ pattern: "**/*.md", base: "./src/content/articles" }),\n'
                "  schema: z.object({ title: z.string(), description: z.string(), pubDate: z.coerce.date(), heroImage: z.string().optional(), heroAlt: z.string().optional(), draft: z.boolean().optional() }),\n"
                "});\n"
                "export const collections = { articles };\n",
                encoding="utf-8",
            )
            in_dir = root / "in"
            in_dir.mkdir()
            (in_dir / "hero.png").write_bytes(VALID_PNG)
            (in_dir / "diagram.png").write_bytes(VALID_PNG)
            body_f = in_dir / "body.md"
            body_f.write_text("## Diagram\n\n![Inline diagram](diagram.png)\n", encoding="utf-8")

            prev = publish_post(
                project_dir=proj,
                collection="articles",
                slug="custom-article",
                title="Custom Collection Localized Asset Directory Test",
                description="Verifying that non-blog collections localize hero and inline images under public/images/<collection>/<slug>/ cleanly.",
                body_file=body_f,
                hero=str(in_dir / "hero.png"),
                hero_alt="Custom collection hero",
                publish_flag=True,
                apply=False,
            )
            publish_post(
                project_dir=proj,
                collection="articles",
                slug="custom-article",
                title="Custom Collection Localized Asset Directory Test",
                description="Verifying that non-blog collections localize hero and inline images under public/images/<collection>/<slug>/ cleanly.",
                body_file=body_f,
                hero=str(in_dir / "hero.png"),
                hero_alt="Custom collection hero",
                publish_flag=True,
                apply=True,
                confirm_plan=prev["planSha256"],
            )
            self.assertTrue((proj / "public" / "images" / "articles" / "custom-article" / "hero.png").is_file())
            self.assertTrue((proj / "public" / "images" / "articles" / "custom-article" / "diagram.png").is_file())
            written = (proj / "src" / "content" / "articles" / "custom-article.md").read_text(encoding="utf-8")
            self.assertIn('heroImage: "/images/articles/custom-article/hero.png"', written)
            self.assertIn("![Inline diagram](/images/articles/custom-article/diagram.png)", written)

        # Verify SVG card row 03 label and assets/manifest.json integrity
        render_py = (MODULE_ROOT / "scripts" / "render_cards.py").read_text(encoding="utf-8")
        self.assertIn("verify-build --baseline", render_py)
        manifest = json.loads((MODULE_ROOT / "assets" / "manifest.json").read_text(encoding="utf-8"))
        slot3 = next(f for f in manifest["files"] if f["name"] == "cards/r1/slot-3-plain-english-chains.svg")
        svg_bytes = (MODULE_ROOT / "assets" / "cards" / "r1" / "slot-3-plain-english-chains.svg").read_bytes()
        self.assertEqual(slot3["bytes"], len(svg_bytes))
        self.assertEqual(slot3["sha256"], hashlib.sha256(svg_bytes).hexdigest())


class TestCarbonPass1Remediations(unittest.TestCase):
    """Dedicated regression tests for Carbon Pass 1 findings C1-01 through C1-10."""

    def _init_starter(self, proj: Path, *, name: str = "Acme Agency", trailing_slash: str = "always") -> dict:
        prev = scaffold_astro_project(
            output_dir=proj,
            mode="starter",
            name=name,
            site_url="https://acme.example",
            trailing_slash=trailing_slash,
            apply=False,
        )
        return scaffold_astro_project(
            output_dir=proj,
            mode="starter",
            name=name,
            site_url="https://acme.example",
            trailing_slash=trailing_slash,
            apply=True,
            confirm_plan=prev["planSha256"],
        )

    def _write_html(
        self,
        path: Path,
        *,
        title: str = "Page",
        canonical: str | None = None,
        body: str = "<p>Body content here.</p>",
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        canon = f'<link rel="canonical" href="{canonical}">' if canonical else ""
        path.write_text(
            f'<!doctype html><html><head><title>{title}</title><meta name="description" content="Description text">{canon}</head>'
            f"<body><h1>{title}</h1>{body}</body></html>",
            encoding="utf-8",
        )

    def test_c1_01_starter_name_braces_rendered_via_site_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "brace-site"
            self._init_starter(proj, name="Studio {Labs} & {process.exit(1)}")
            site_ts = (proj / "src" / "lib" / "site.ts").read_text(encoding="utf-8")
            index_astro = (proj / "src" / "pages" / "index.astro").read_text(encoding="utf-8")

            self.assertIn("heroKicker:", site_ts)
            self.assertIn("heroTitle:", site_ts)
            self.assertIn("heroLead:", site_ts)
            self.assertIn('"Studio {Labs} & {process.exit(1)} builds fast, verifiable websites"', site_ts)

            self.assertIn("<p class=\"kicker\">{SITE_CONFIG.heroKicker}</p>", index_astro)
            self.assertIn("<h1>{SITE_CONFIG.heroTitle}</h1>", index_astro)
            self.assertIn("<p class=\"lead\">{SITE_CONFIG.heroLead}</p>", index_astro)
            self.assertNotIn("{Labs}", index_astro)
            self.assertNotIn("process.exit", index_astro)

    def test_c1_02_svg_xml_parser_blocks_all_xss_bypasses_and_allows_clean_svg(self) -> None:
        # Clean styled SVG with leading <?xml ...?> and internal #fragment references passes
        clean_svg = (
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 20 20">'
            b'<defs><linearGradient id="g"><stop offset="0%" stop-color="#111"/></linearGradient>'
            b'<rect id="box" width="10" height="10"/></defs>'
            b'<style>.shape { fill: url(#g); stroke: #222; }</style>'
            b'<use xlink:href="#box" class="shape" style="fill: url(#g);"/>'
            b"</svg>"
        )
        validate_image_bytes(".svg", clean_svg)

        malicious_svgs = [
            ("xml-stylesheet-pi", b'<?xml-stylesheet type="text/xsl" href="evil.xsl"?><svg xmlns="http://www.w3.org/2000/svg"/>'),
            (
                "second-pi-after-xml",
                b'<?xml version="1.0"?><?xml-stylesheet href="evil.xsl"?><svg xmlns="http://www.w3.org/2000/svg"/>',
            ),
            (
                "xhtml-script",
                b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xhtml"><x:script>alert(1)</x:script></svg>',
            ),
            (
                "svg-prefixed-script",
                b'<svg:svg xmlns:svg="http://www.w3.org/2000/svg"><svg:script>alert(1)</svg:script></svg:svg>',
            ),
            (
                "entity-obfuscated-href",
                b'<svg xmlns="http://www.w3.org/2000/svg"><a href="&#x6a;avascript:void(0)"><rect width="5" height="5"/></a></svg>',
            ),
            (
                "entity-obfuscated-xlink",
                b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><a xlink:href="&#106;avascript:alert(1)"/></svg>',
            ),
            (
                "smil-set",
                b'<svg xmlns="http://www.w3.org/2000/svg"><set attributeName="onload" to="alert(1)"/></svg>',
            ),
            (
                "smil-animate",
                b'<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="href" to="javascript:alert(1)"/></svg>',
            ),
            (
                "use-data-uri",
                b'<svg xmlns="http://www.w3.org/2000/svg"><use href="data:image/svg+xml;base64,PHN2Zz4="/></svg>',
            ),
            (
                "image-external-uri",
                b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.example/x.png"/></svg>',
            ),
            (
                "feimage-tag",
                b'<svg xmlns="http://www.w3.org/2000/svg"><filter id="f"><feImage href="#a"/></filter></svg>',
            ),
            (
                "iframe-tag",
                b'<svg xmlns="http://www.w3.org/2000/svg"><iframe src="https://evil.example/"/></svg>',
            ),
            (
                "object-tag",
                b'<svg xmlns="http://www.w3.org/2000/svg"><object data="https://evil.example/"/></svg>',
            ),
            (
                "embed-tag",
                b'<svg xmlns="http://www.w3.org/2000/svg"><embed src="https://evil.example/"/></svg>',
            ),
            (
                "style-import",
                b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url("https://evil.example/a.css");</style></svg>',
            ),
            (
                "inline-style-js",
                b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill: url(javascript:alert(1))"/></svg>',
            ),
            (
                "malformed-comment-split",
                b"<svg><scr<!--x-->ipt>alert(1)</script></svg>",
            ),
            (
                "doctype-xxe",
                b'<!DOCTYPE svg [<!ENTITY xxe "pwn">]><svg xmlns="http://www.w3.org/2000/svg">&xxe;</svg>',
            ),
        ]
        for label, payload in malicious_svgs:
            with self.assertRaises(ValueError, msg=f"Expected SVG bypass '{label}' to be rejected"):
                validate_image_bytes(".svg", payload, source_label=f"{label}.svg")

    def test_c1_03_hero_image_confinement_dual_roots_and_content_dir_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "site"
            self._init_starter(proj)

            outside_img = root / "host-secret.png"
            outside_img.write_bytes(VALID_PNG)

            # 1. Existing frontmatter heroImage with parent traversal MUST fail CWE-22 on --update and --rename-from
            bad_post = proj / "src" / "content" / "blog" / "evil-hero.md"
            bad_post.write_text(
                "---\n"
                'title: "Existing Post With Traversal Hero Image Reference"\n'
                'description: "Regression check verifying that existing frontmatter heroImage paths are strictly confined to project roots."\n'
                'pubDate: "2026-10-01"\n'
                'heroImage: "../../../../host-secret.png"\n'
                'heroAlt: "Traversal hero"\n'
                "draft: false\n"
                "---\n\n"
                "Body content here.\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx_upd:
                publish_post(project_dir=proj, update=True, slug="evil-hero", title="Updated Hero Post Title Length Check")
            self.assertIn("CWE-22", str(ctx_upd.exception))

            with self.assertRaises(ValueError) as ctx_ren:
                publish_post(project_dir=proj, rename_from="evil-hero", slug="evil-hero-v2")
            self.assertIn("CWE-22", str(ctx_ren.exception))

            # 2. Both --input (in dir_in with relative heroImage) and --body-file (in dir_body with relative inline img) work together
            dir_in = root / "dir_in"
            dir_body = root / "dir_body"
            dir_in.mkdir()
            dir_body.mkdir()
            (dir_in / "cover.png").write_bytes(VALID_PNG)
            (dir_body / "inline.png").write_bytes(VALID_PNG)
            in_md = dir_in / "meta.md"
            in_md.write_text(
                "---\n"
                'title: "Dual Input and Body File Confinement Verification"\n'
                'description: "Ensures --input heroImage resolves inside input dir while --body-file inline images resolve inside body dir."\n'
                'heroImage: "cover.png"\n'
                'heroAlt: "Input dir cover"\n'
                "---\n",
                encoding="utf-8",
            )
            bf_md = dir_body / "body.md"
            bf_md.write_text("## Section\n\n![Inline diagram](inline.png)\n", encoding="utf-8")
            prev_dual = publish_post(
                project_dir=proj,
                slug="dual-root-post",
                input_path=in_md,
                body_file=bf_md,
                publish_flag=True,
                apply=False,
            )
            self.assertTrue(prev_dual["lint"]["ok"], f"Unexpected lint errors: {prev_dual['lint']['errors']}")
            self.assertEqual(prev_dual["heroImage"], "/images/blog/dual-root-post/cover.png")
            self.assertEqual(len(prev_dual["plannedAssets"]), 2)

            # 3. --content-dir . / ./ / public/blog MUST be refused
            for bad_cdir in (".", "./", "public", "public/blog"):
                with self.assertRaises(ValueError, msg=f"Expected --content-dir {bad_cdir!r} to be refused"):
                    publish_post(project_dir=proj, content_dir_override=bad_cdir, list_only=True)

    def test_c1_04_verify_build_rejects_symlink_project_before_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            proj = root / "proj"
            proj.mkdir()
            self._write_html(dist / "index.html", title="Home")

            seen_paths: list[Path] = []

            def _fake_reject(p: Path | str) -> None:
                raw = Path(p)
                seen_paths.append(raw)
                if raw.name == "proj-link":
                    raise ValueError(f"Refusing symlink or reparse-point path (CWE-59): {raw}")

            with mock.patch("legends_astro.auditor.reject_symlink_chain", side_effect=_fake_reject):
                with self.assertRaises(ValueError) as ctx:
                    verify_build(dist, project_dir=root / "proj-link")
            self.assertIn("CWE-59", str(ctx.exception))
            self.assertTrue(any(p.name == "proj-link" for p in seen_paths))

    def test_c1_05_linear_md_image_regex_and_auditor_frontmatter_hardening(self) -> None:
        import time
        from legends_astro.auditor import _extract_fm_scalar
        from legends_astro.publisher import _MD_IMAGE_RE

        # 1. O(N) ReDoS check on 30,000 unclosed '![' tokens completes well under 100ms
        payload = "![" * 30000
        t0 = time.perf_counter()
        self.assertEqual(_MD_IMAGE_RE.findall(payload), [])
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(elapsed_ms, 100.0, f"_MD_IMAGE_RE took {elapsed_ms:.1f}ms on 30k '![' tokens")

        # 2. Cross-line \s* capture fixed in _extract_fm_scalar
        fm = 'pubDate:\nupdatedDate: "2099-01-01"\ntitle: "Clean" # inline comment\n'
        self.assertIsNone(_extract_fm_scalar(fm, "pubDate"))
        self.assertEqual(_extract_fm_scalar(fm, "updatedDate"), "2099-01-01")
        self.assertEqual(_extract_fm_scalar(fm, "title"), "Clean")

        # 3. UTF-8 BOM, inline YAML comment, EOF after closing ---, isDraft: true, and custom collectionBase
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "site"
            (proj / "custom_articles").mkdir(parents=True)
            (proj / "src" / "content" / "blog").mkdir(parents=True)
            (proj / "src" / "content.config.ts").write_text(
                'import { defineCollection } from "astro:content";\n'
                'import { glob } from "astro/loaders";\n'
                'import { z } from "astro/zod";\n'
                "const articles = defineCollection({\n"
                '  loader: glob({ pattern: "**/*.md", base: "./custom_articles" }),\n'
                "  schema: z.object({ title: z.string(), isDraft: z.boolean().optional() }),\n"
                "});\n"
                "export const collections = { articles };\n",
                encoding="utf-8",
            )
            # BOM + inline comment + EOF right after closing ---
            (proj / "src" / "content" / "blog" / "bom-draft.md").write_text(
                "\ufeff---\ntitle: \"BOM Draft\"\ndraft: true # hold until review\n---",
                encoding="utf-8",
            )
            # Custom collectionBase outside src/content/ with isDraft: true
            (proj / "custom_articles" / "secret-custom.md").write_text(
                "---\ntitle: \"Secret Custom\"\nisDraft: true\n---\nExclusive custom collection secret prose line over thirty two characters.\n",
                encoding="utf-8",
            )
            dist = root / "dist"
            self._write_html(dist / "index.html", title="Home")
            self._write_html(dist / "blog" / "bom-draft" / "index.html", title="BOM Draft")
            self._write_html(dist / "articles" / "secret-custom" / "index.html", title="Secret Custom")

            rep = verify_build(dist, project_dir=proj)
            self.assertFalse(rep["ok"])
            self.assertEqual(rep["draftEntriesChecked"], 2)
            leaked_files = {f.get("draftFile") for f in rep["failures"] if f["code"] == "draft_route_leaked"}
            self.assertIn("src/content/blog/bom-draft.md", leaked_files)
            self.assertIn("custom_articles/secret-custom.md", leaked_files)

    def test_c1_06_plan_sha256_invalidates_same_length_mutations_across_commands(self) -> None:
        from legends_astro.auditor import ingest_source_to_astro

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1. ingest_source_to_astro same-length mutation invalidates planSha256
            src_html = root / "src"
            src_html.mkdir()
            (src_html / "index.html").write_text(
                "<!doctype html><html><head><title>Home</title></head><body><h1>AAAA</h1><p>Body 1111</p></body></html>",
                encoding="utf-8",
            )
            ing_out = root / "ing"
            ing_prev = ingest_source_to_astro(src_html, ing_out, apply=False)
            self.assertTrue(all("sha256" in f for f in ing_prev["plannedFiles"]))
            (src_html / "index.html").write_text(
                "<!doctype html><html><head><title>Home</title></head><body><h1>BBBB</h1><p>Body 2222</p></body></html>",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                ingest_source_to_astro(src_html, ing_out, apply=True, confirm_plan=ing_prev["planSha256"])

            # 2. publisher --rename-from: same-length mutation of file to be unlinked invalidates planSha256
            proj = root / "site"
            self._init_starter(proj)
            old_p = proj / "src" / "content" / "blog" / "welcome.md"
            ren_prev = publish_post(project_dir=proj, rename_from="welcome", slug="welcome-new", apply=False)
            orig_txt = old_p.read_text(encoding="utf-8")
            self.assertIn("Astro", orig_txt)
            # Same byte length mutation
            mutated_txt = orig_txt.replace("Astro", "Bstro", 1)
            self.assertEqual(len(mutated_txt.encode("utf-8")), len(orig_txt.encode("utf-8")))
            old_p.write_text(mutated_txt, encoding="utf-8")
            with self.assertRaises(ValueError):
                publish_post(
                    project_dir=proj,
                    rename_from="welcome",
                    slug="welcome-new",
                    apply=True,
                    confirm_plan=ren_prev["planSha256"],
                )

    def test_c1_07_yaml_block_scalars_and_wrapped_strings_preserved_on_update(self) -> None:
        raw_folded = (
            "---\n"
            'title: "CMS Folded Description Post For Update Round Trip"\n'
            "description: >-\n"
            "  This long SEO meta description was written by a Git-backed CMS\n"
            "  using a standard YAML folded block scalar across two lines.\n"
            "notes: |\n"
            "  First literal line\n"
            "  Second literal line\n"
            'wrapped: "Wrapped quoted\n'
            '  scalar value"\n'
            'pubDate: "2026-10-05"\n'
            "draft: false\n"
            "---\n\n"
            "Body paragraph.\n"
        )
        fm, _, _ = parse_frontmatter(raw_folded)
        self.assertEqual(
            fm["description"],
            "This long SEO meta description was written by a Git-backed CMS using a standard YAML folded block scalar across two lines.",
        )
        self.assertEqual(fm["notes"], "First literal line\nSecond literal line")
        self.assertEqual(fm["wrapped"], "Wrapped quoted scalar value")

        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "site"
            self._init_starter(proj)
            (proj / "src" / "content" / "blog" / "cms-post.md").write_text(raw_folded, encoding="utf-8")
            prev = publish_post(
                project_dir=proj,
                update=True,
                slug="cms-post",
                title="Revised Headline After CMS Folded Description Edit",
                apply=False,
            )
            publish_post(
                project_dir=proj,
                update=True,
                slug="cms-post",
                title="Revised Headline After CMS Folded Description Edit",
                apply=True,
                confirm_plan=prev["planSha256"],
            )
            updated = (proj / "src" / "content" / "blog" / "cms-post.md").read_text(encoding="utf-8")
            fm_after, _, _ = parse_frontmatter(updated)
            self.assertEqual(
                fm_after["description"],
                "This long SEO meta description was written by a Git-backed CMS using a standard YAML folded block scalar across two lines.",
            )
            self.assertNotEqual(fm_after["description"], ">-")

    def test_c1_08_field_map_collisions_comment_safe_ts_parser_and_mdx_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "site"
            (proj / "src" / "content" / "articles").mkdir(parents=True)
            (proj / "src" / "content.config.ts").write_text(
                'import { defineCollection } from "astro:content";\n'
                'import { glob } from "astro/loaders";\n'
                'import { z } from "astro/zod";\n'
                "const articles = defineCollection({\n"
                "  // Don't allow empty slugs or unquoted apostrophes to break static parsing\n"
                "  /* Block comment with 'single' and \"double\" quotes and { unmatched brace */\n"
                '  loader: glob({ pattern: "**/*.{md,mdx}", base: "./src/content/articles" }),\n'
                "  schema: z.object({\n"
                "    title: z.string(),\n"
                "    summary: z.string(),\n"
                "    date: z.coerce.date(),\n"
                "    hero: z.object({\n"
                "      image: z.string(),\n"
                "      alt: z.string(),\n"
                "    }).optional(),\n"
                "    draft: z.boolean().optional(),\n"
                "  }),\n"
                "});\n"
                "export const collections = { articles };\n",
                encoding="utf-8",
            )
            cfg = inspect_content_config(proj, "articles")
            self.assertEqual(cfg["collectionBase"], "src/content/articles")
            self.assertEqual(cfg["fieldMap"]["description"], "summary")
            self.assertEqual(cfg["fieldMap"]["pubDate"], "date")
            # Nested z.object({ image, alt }) keys MUST NOT leak into top-level detectedFields
            self.assertIn("hero", cfg["detectedFields"])
            self.assertNotIn("image", cfg["detectedFields"])
            self.assertNotIn("alt", cfg["detectedFields"])

            # --field-map collision refused
            with self.assertRaises(ValueError) as ctx_col:
                publish_post(
                    project_dir=proj,
                    collection="articles",
                    title="Collision Check Title Over Thirty Characters Long",
                    field_map_overrides=["updatedDate=date"],
                    apply=False,
                )
            self.assertIn("collision", str(ctx_col.exception).lower())

            # Cross-extension .md / .mdx collision refused even with overwrite=True
            (proj / "src" / "content" / "articles" / "existing-mdx.mdx").write_text(
                '---\ntitle: "Existing MDX"\nsummary: "Summary"\ndate: "2026-10-01"\n---\nBody\n',
                encoding="utf-8",
            )
            with self.assertRaises(FileExistsError):
                publish_post(
                    project_dir=proj,
                    collection="articles",
                    slug="existing-mdx",
                    title="Attempting Duplicate Stem MD File Next To MDX",
                    overwrite=True,
                    apply=False,
                )

    def test_c1_09_rss_fail_closed_timezone_sampletext_and_redirects_precedence(self) -> None:
        from legends_astro.auditor import _is_future_pub_date, ingest_source_to_astro
        from legends_astro.publisher import _is_scheduled_date, _validate_date_str

        # 1. Naive sub-day timestamps rejected; explicit Z/z/offset accepted & parity-matched
        for bad_ts in ("2026-11-25T15:00:00", "2026-11-25 15:00:00"):
            with self.assertRaises(ValueError):
                _validate_date_str(bad_ts)
        for good_ts in ("2099-11-25", "2099-11-25 23:59:00z", "2099-11-25T15:00:00+02:00"):
            self.assertEqual(_validate_date_str(good_ts), good_ts)
            self.assertTrue(_is_scheduled_date(good_ts))
            self.assertTrue(_is_future_pub_date(good_ts))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 2. editorial and ingest emit pubDate-aware isPublished
            src_html = root / "src"
            src_html.mkdir()
            (src_html / "index.html").write_text(
                "<!doctype html><html><head><title>Home</title></head><body><h1>Home</h1><p>Hi</p></body></html>",
                encoding="utf-8",
            )
            ed_out = root / "ed"
            ed_prev = scaffold_astro_project(source_path=src_html, output_dir=ed_out, mode="editorial", apply=False)
            scaffold_astro_project(source_path=src_html, output_dir=ed_out, mode="editorial", apply=True, confirm_plan=ed_prev["planSha256"])
            self.assertIn("Date.now()", (ed_out / "src" / "lib" / "published.ts").read_text(encoding="utf-8"))

            ing_out = root / "ing"
            ing_prev = ingest_source_to_astro(src_html, ing_out, apply=False)
            ingest_source_to_astro(src_html, ing_out, apply=True, confirm_plan=ing_prev["planSha256"])
            self.assertIn("Date.now()", (ing_out / "src" / "lib" / "published.ts").read_text(encoding="utf-8"))

            # 3. Malformed dist/rss.xml fails closed with rss_invalid_xml even without --project
            dist = root / "dist"
            self._write_html(dist / "index.html", title="Home", canonical="https://acme.example/")
            (dist / "rss.xml").write_text("<rss><unclosed></rss>", encoding="utf-8")
            rep_rss_bad = verify_build(dist)
            self.assertFalse(rep_rss_bad["ok"])
            self.assertIn("rss_invalid_xml", {f["code"] for f in rep_rss_bad["failures"]})

            (dist / "rss.xml").write_text('<!DOCTYPE rss [<!ENTITY x "y">]><rss/>', encoding="utf-8")
            rep_rss_xxe = verify_build(dist)
            self.assertFalse(rep_rss_xxe["ok"])
            self.assertIn("rss_invalid_xml", {f["code"] for f in rep_rss_xxe["failures"]})
            (dist / "rss.xml").unlink()

            # 4. Boilerplate / MDX import / shared published prose in draft does NOT false-positive
            proj = root / "site"
            self._init_starter(proj)
            shared_line = "This exact editorial disclaimer appears across all published guides on our site."
            (proj / "src" / "content" / "blog" / "published-guide.md").write_text(
                f'---\ntitle: "Published Guide"\ndescription: "Published"\npubDate: "2026-10-01"\ndraft: false\n---\n\n{shared_line}\n',
                encoding="utf-8",
            )
            (proj / "src" / "content" / "blog" / "draft-mdx.mdx").write_text(
                f'---\ntitle: "Draft MDX"\ndescription: "Draft"\npubDate: "2026-10-01"\ndraft: true\n---\n\n'
                f'import Callout from "../../components/Callout.astro";\n'
                f'<div class="note">Key takeaways:</div>\n'
                f"{shared_line}\n",
                encoding="utf-8",
            )
            self._write_html(
                dist / "blog" / "published-guide" / "index.html",
                title="Published Guide",
                canonical="https://acme.example/blog/published-guide/",
                body=f'<div class="note">Key takeaways:</div><p>{shared_line}</p>',
            )
            rep_no_fp = verify_build(dist, project_dir=proj)
            self.assertTrue(rep_no_fp["ok"], f"Unexpected boilerplate false positive: {rep_no_fp['failures']}")

            # 5. Normalized redirect cycle detection + first-rule-wins + non-404 baseline coverage
            dist_r = root / "dist-r"
            manifest_r = root / "manifest-r.json"
            self._write_html(dist_r / "index.html", title="Home", canonical="https://acme.example/")
            self._write_html(dist_r / "404.html", title="404")
            self._write_html(dist_r / "blog" / "a" / "index.html", title="A", canonical="https://acme.example/blog/a/")
            self._write_html(dist_r / "blog" / "b" / "index.html", title="B", canonical="https://acme.example/blog/b/")
            self.assertTrue(verify_build(dist_r, write_manifest_path=manifest_r)["ok"])

            # Trailing-slash mismatch cycle: /blog/a/ -> /blog/b and /blog/b/ -> /blog/a
            (dist_r / "_redirects").write_text("/blog/a/ /blog/b 301\n/blog/b/ /blog/a 301\n", encoding="utf-8")
            rep_cyc = verify_build(dist_r)
            self.assertIn("redirect_loop", {f["code"] for f in rep_cyc["failures"]})

            # Remove /blog/a/; first rule points to /404.html (must NOT cover removed route even if rule 2 is valid!)
            (dist_r / "blog" / "a" / "index.html").unlink()
            (dist_r / "_redirects").write_text(
                "/blog/a/ /404.html 301\n"
                "/blog/a/ /blog/b/ 301\n",
                encoding="utf-8",
            )
            rep_404_redir = verify_build(dist_r, baseline_path=manifest_r)
            self.assertFalse(rep_404_redir["ok"])
            self.assertIn("baseline_route_missing", {f["code"] for f in rep_404_redir["failures"]})

    def test_c1_10_inline_image_query_fragment_and_doc_cli_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "site"
            self._init_starter(proj)

            in_dir = root / "in"
            in_dir.mkdir()
            (in_dir / "diagram.png").write_bytes(VALID_PNG)
            bf = in_dir / "body.md"
            bf.write_text("## Visual\n\n![System diagram](diagram.png?v=2#dark)\n", encoding="utf-8")

            # Omitting --draft/--publish defaults to draft: true in create mode
            prev = publish_post(
                project_dir=proj,
                slug="query-frag-post",
                title="Inline Image Query and Fragment Suffix Preservation",
                description="Verifies relative inline Markdown images with query and fragment suffixes localize cleanly and keep their suffix.",
                body_file=bf,
                apply=False,
            )
            self.assertTrue(prev["draft"])
            self.assertTrue(prev["lint"]["ok"], f"Unexpected lint errors: {prev['lint']['errors']}")
            publish_post(
                project_dir=proj,
                slug="query-frag-post",
                title="Inline Image Query and Fragment Suffix Preservation",
                description="Verifies relative inline Markdown images with query and fragment suffixes localize cleanly and keep their suffix.",
                body_file=bf,
                apply=True,
                confirm_plan=prev["planSha256"],
            )
            self.assertTrue((proj / "public" / "images" / "blog" / "query-frag-post" / "diagram.png").is_file())
            written = (proj / "src" / "content" / "blog" / "query-frag-post.md").read_text(encoding="utf-8")
            self.assertIn("![System diagram](/images/blog/query-frag-post/diagram.png?v=2#dark)", written)

        # Verify doc-to-CLI sync points
        self.assertIn("publisher.py", (MODULE_ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("scheduledEntriesChecked", (MODULE_ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("scheduledEntriesChecked", (MODULE_ROOT / "CAPABILITIES.md").read_text(encoding="utf-8"))
        changelog = (MODULE_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("public/images/<collection>/<slug>/", changelog)
        self.assertIn("scheduled_post_in_sitemap", changelog)
        self.assertIn("draft: true", (MODULE_ROOT / "wiki" / "publishing-and-content-ops.md").read_text(encoding="utf-8"))
        self.assertIn("draft: true", (MODULE_ROOT / "docs" / "AGENT-RECIPE.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
