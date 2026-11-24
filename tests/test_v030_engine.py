"""Hermetic v0.3.0 test suite covering net_safety, capture, vendor-assets, scaffold, upgraded auditor/verify-build, and twin/twin-diff."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from legends_astro.auditor import audit_source, html_to_markdown, verify_build
from legends_astro.capture import capture_site, route_to_capture_slug
from legends_astro.cli import main as cli_main
from legends_astro.docs_nav import OFFICIAL_PACKAGES, lookup_docs
from legends_astro.net_safety import (
    is_forbidden_ip,
    reject_controlled_folder_access,
    safe_fetch_bytes,
    sanitize_rel_path,
    validate_outbound_url,
)
from legends_astro.scaffold import (
    decode_cf_email_hex,
    decode_cloudflare_emails,
    scaffold_astro_project,
    strip_trackers,
)
from legends_astro.twin import diff_twins, run_twin
from legends_astro.vendor import rewrite_html_assets, vendor_assets

MODULE_ROOT = Path(__file__).resolve().parent.parent


def _encode_cf_email(email: str, key: int = 0x5A) -> str:
    return f"{key:02x}" + "".join(f"{b ^ key:02x}" for b in email.encode("utf-8"))


class TestNetSafety(unittest.TestCase):
    def test_ssrf_ip_classification(self) -> None:
        forbidden = [
            "127.0.0.1",
            "127.255.255.254",
            "::1",
            "10.0.0.1",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.1.10",
            "100.64.0.1",
            "100.127.255.254",
            "169.254.169.254",
            "fe80::1",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            "::ffff:10.0.0.5",
            "0.0.0.0",
            "::",
            "224.0.0.1",
        ]
        for ip in forbidden:
            self.assertTrue(is_forbidden_ip(ip), f"Expected {ip} to be forbidden")
        self.assertFalse(is_forbidden_ip("93.184.216.34"))
        self.assertFalse(is_forbidden_ip("2606:2800:220:1:248:1893:25c8:1946"))

    def test_outbound_url_validation_and_manual_redirects(self) -> None:
        pub_resolver = lambda host, _port: ["93.184.216.34"]
        ssrf_resolver = lambda host, _port: ["169.254.169.254"] if host == "evil.example" else ["93.184.216.34"]

        # Reject HTTP without opt-in
        with self.assertRaises(ValueError):
            validate_outbound_url("http://site.example/", allowed_hosts=["site.example"], resolver=pub_resolver)
        # Reject host outside allowlist
        with self.assertRaises(ValueError):
            validate_outbound_url("https://other.example/", allowed_hosts=["site.example"], resolver=pub_resolver)
        # Reject SSRF resolved IP
        with self.assertRaises(ValueError):
            validate_outbound_url("https://evil.example/", allowed_hosts=["evil.example"], resolver=ssrf_resolver)
        # Reject localhost
        with self.assertRaises(ValueError):
            validate_outbound_url("https://localhost/", allowed_hosts=["localhost"], resolver=pub_resolver)

        # Follow safe redirect and strip Authorization header
        seen_headers: list[dict[str, str]] = []

        def transport(url: str, _ips: list[str], headers: dict[str, str], _timeout: float):
            seen_headers.append(headers)
            if url == "https://site.example/old":
                return 301, {"Location": "https://site.example/new"}, b""
            if url == "https://site.example/to-evil":
                return 302, {"Location": "https://evil.example/meta"}, b""
            if url == "https://site.example/big":
                return 200, {"Content-Type": "text/html"}, b"x" * 2048
            return 200, {"Content-Type": "text/html"}, b"<html>ok</html>"

        res = safe_fetch_bytes(
            "https://site.example/old",
            allowed_hosts=["site.example"],
            headers={"Authorization": "Bearer secret"},
            resolver=pub_resolver,
            transport=transport,
        )
        self.assertEqual(res.final_url, "https://site.example/new")
        self.assertIn("Authorization", seen_headers[0])
        self.assertNotIn("Authorization", seen_headers[1])

        # Redirect to SSRF host fails closed
        with self.assertRaises(ValueError):
            safe_fetch_bytes(
                "https://site.example/to-evil",
                allowed_hosts=["site.example", "evil.example"],
                resolver=ssrf_resolver,
                transport=transport,
            )

        # Bounded byte cap
        with self.assertRaises(ValueError):
            safe_fetch_bytes(
                "https://site.example/big",
                allowed_hosts=["site.example"],
                max_bytes=512,
                resolver=pub_resolver,
                transport=transport,
            )

    def test_sanitize_rel_path_and_cfa_guard(self) -> None:
        self.assertEqual(sanitize_rel_path("/wp-content/uploads/my%2520file%20name.png?ver=2"), "wp-content/uploads/my-file-name.png")
        with self.assertRaises(ValueError):
            sanitize_rel_path("../../etc/passwd")
        with self.assertRaises(ValueError):
            sanitize_rel_path("C:/Windows/win.ini")
        with self.assertRaises(ValueError):
            reject_controlled_folder_access(Path.home() / "Documents" / "should-not-write.txt")


class TestCaptureAndFirecrawl(unittest.TestCase):
    def test_live_sitemap_bfs_capture_and_triage(self) -> None:
        pub_resolver = lambda _host, _port: ["93.184.216.34"]
        site_map = {
            "https://demo.example/sitemap_index.xml": (
                200,
                {"Content-Type": "application/xml"},
                b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<sitemap><loc>https://demo.example/page-sitemap.xml</loc></sitemap></sitemapindex>",
            ),
            "https://demo.example/page-sitemap.xml": (
                200,
                {"Content-Type": "application/xml"},
                b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                b"<url><loc>https://demo.example/</loc></url>"
                b"<url><loc>https://demo.example/services/web-design/</loc></url>"
                b"<url><loc>https://demo.example/logo.png</loc></url>"
                b"</urlset>",
            ),
            "https://demo.example/": (
                200,
                {"Content-Type": "text/html; charset=utf-8"},
                (
                    '<!doctype html><html lang="en" data-wf-domain="demo.example" data-wf-site="s1" data-wf-page="p-home">'
                    '<head><title>Demo Home</title><meta name="description" content="Home desc">'
                    '<link rel="stylesheet" href="https://cdn.prod.website-files.com/site.css">'
                    '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC123"></script>'
                    '</head><body class="home">'
                    '<h1>Demo</h1><a href="/about-old">About</a><a href="/cdn-cgi/l/email-protection#1234">Mail</a>'
                    '<form class="w-form" data-turnstile-sitekey="0xAAA"></form>'
                    '</body></html>'
                ).encode("utf-8"),
            ),
            "https://demo.example/services/web-design/": (
                200,
                {"Content-Type": "text/html"},
                b"<!doctype html><html><head><title>Web Design</title></head><body><h1>Web Design</h1></body></html>",
            ),
            "https://demo.example/about-old": (
                301,
                {"Location": "https://demo.example/about/"},
                b"",
            ),
            "https://demo.example/about/": (
                200,
                {"Content-Type": "text/html"},
                b"<!doctype html><html><head><title>About</title></head><body><h1>About Us</h1></body></html>",
            ),
        }

        def transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            return site_map.get(url, (404, {"Content-Type": "text/plain"}, b"not found"))

        with tempfile.TemporaryDirectory() as tmp:
            res = capture_site(
                output_dir=tmp,
                url="https://demo.example/",
                from_sitemap=True,
                crawl=True,
                max_pages=20,
                max_depth=2,
                resolver=pub_resolver,
                transport=transport,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["pageCount"], 3)
            self.assertEqual(res["triage"]["detectedPlatform"], "webflow")
            self.assertIn("google-tag-manager", res["triage"]["trackers"])
            self.assertIn("turnstile", res["triage"]["forms"]["detectedSystems"])
            self.assertIn("cdn.prod.website-files.com", res["triage"]["externalCdns"])
            self.assertEqual(res["triage"]["cloudflareEmailObfuscationCount"], 1)
            self.assertTrue((Path(tmp) / "raw-capture" / "pages" / "services__web-design.html").is_file())
            self.assertTrue((Path(tmp) / "raw-capture" / "pages" / "about.html").is_file())
            self.assertTrue((Path(tmp) / "migration-contract.json").is_file())

    def test_firecrawl_directory_unwrapping_and_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fc_dir = root / "fc-batches"
            fc_dir.mkdir()
            # Envelope 1: response.data (.raw.json)
            (fc_dir / "batch1.raw.json").write_text(
                json.dumps(
                    {
                        "metadata": {"op": "crawl"},
                        "response": {
                            "data": [
                                {
                                    "metadata": {"sourceURL": "https://client.example/", "title": "Home"},
                                    "html": "<!doctype html><html><head><title>Home</title></head><body><h1>Home</h1></body></html>",
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            # Envelope 2: legends-firecrawl-evidence/v1 with richer duplicate + second page
            (fc_dir / "batch2.json").write_text(
                json.dumps(
                    {
                        "schema": "legends-firecrawl-evidence/v1",
                        "records": [
                            {
                                "metadata": {"sourceURL": "https://client.example/", "title": "Home Rich"},
                                "html": "<!doctype html><html><head><title>Home Rich</title></head><body><h1>Home Richer Body</h1><p>Extra</p></body></html>",
                            },
                            {
                                "metadata": {"sourceURL": "https://client.example/pricing/", "title": "Pricing"},
                                "html": "<!doctype html><html><head><title>Pricing</title></head><body><h1>Pricing</h1></body></html>",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            contract = audit_source(fc_dir)
            self.assertEqual(contract["sourceKind"], "firecrawl-json")
            self.assertEqual(contract["summary"]["pageCount"], 2)
            by_route = {p["route"]: p for p in contract["pages"]}
            self.assertEqual(by_route["/"]["title"], "Home Rich")
            self.assertIn("/pricing/", by_route)

            out_cap = root / "cap-out"
            cap_res = capture_site(output_dir=out_cap, from_firecrawl=fc_dir)
            self.assertEqual(cap_res["pageCount"], 2)
            self.assertEqual(route_to_capture_slug("/a/b/c/"), "a__b__c")


class TestVendorAssetsAndScaffold(unittest.TestCase):
    def test_vendor_assets_two_pass_css_dynamic_php_and_sri_strip(self) -> None:
        pub_resolver = lambda _host, _port: ["93.184.216.34"]
        assets_store = {
            "https://origin.example/wp-content/themes/acme/dynamic_css.php?id=7": (
                200,
                {"Content-Type": "text/css; charset=utf-8"},
                b'@import url("./nested.css");\nbody{background:url("../images/bg.svg");}\n',
            ),
            "https://origin.example/wp-content/themes/acme/nested.css": (
                200,
                {"Content-Type": "text/css"},
                b'@font-face{font-family:Acme;src:url("../../fonts/acme.woff2") format("woff2");}\n',
            ),
            "https://origin.example/wp-content/themes/images/bg.svg": (
                200,
                {"Content-Type": "image/svg+xml"},
                b"<svg></svg>",
            ),
            "https://origin.example/wp-content/fonts/acme.woff2": (
                200,
                {"Content-Type": "font/woff2"},
                b"wOF2fakefont",
            ),
            "https://cdn.prod.website-files.com/hero.jpg": (
                200,
                {"Content-Type": "image/jpeg"},
                b"\xff\xd8\xff\xe0jpg",
            ),
            "https://cdn.prod.website-files.com/hero-500.jpg": (
                200,
                {"Content-Type": "image/jpeg"},
                b"\xff\xd8\xff\xe0jpg500",
            ),
            "https://cdn.prod.website-files.com/app.js": (
                200,
                {"Content-Type": "application/javascript"},
                b"console.log('ok');",
            ),
            "https://cdn.prod.website-files.com/poster.jpg": (
                200,
                {"Content-Type": "image/jpeg"},
                b"\xff\xd8\xff\xe0poster",
            ),
            "https://cdn.prod.website-files.com/vid.mp4": (
                200,
                {"Content-Type": "video/mp4"},
                b"mp4data",
            ),
        }

        def transport(url: str, _ips: list[str], _headers: dict[str, str], _timeout: float):
            return assets_store.get(url, (404, {"Content-Type": "text/plain"}, b"missing"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_dir = root / "raw-capture"
            (raw_dir / "pages").mkdir(parents=True)
            sample_html = (
                '<!doctype html><html><head>'
                '<link rel="stylesheet" href="https://origin.example/wp-content/themes/acme/dynamic_css.php?id=7" integrity="sha384-abc" crossorigin="anonymous">'
                '<script src="https://cdn.prod.website-files.com/app.js" integrity="sha384-xyz" crossorigin="anonymous"></script>'
                '<script src="https://unallowed.cdn.example/skip.js" integrity="sha384-keep"></script>'
                '</head><body>'
                '<img src="https://cdn.prod.website-files.com/hero.jpg" srcset="https://cdn.prod.website-files.com/hero-500.jpg 500w, https://cdn.prod.website-files.com/hero.jpg 1000w">'
                '<div data-poster-url="https://cdn.prod.website-files.com/poster.jpg" data-video-urls="https://cdn.prod.website-files.com/vid.mp4"></div>'
                '</body></html>'
            )
            (raw_dir / "pages" / "index.html").write_text(sample_html, encoding="utf-8")

            astro_out = root / "astro-site"
            # Dry run first
            dry = vendor_assets(
                source_dir=raw_dir,
                output_dir=astro_out,
                allow_hosts=["origin.example", "cdn.prod.website-files.com"],
                dry_run=True,
                resolver=pub_resolver,
                transport=transport,
            )
            self.assertTrue(dry["dryRun"])
            self.assertFalse((astro_out / "asset-map.json").exists())
            self.assertIn("unallowed.cdn.example", dry["skippedHosts"])

            # Real run
            run = vendor_assets(
                source_dir=raw_dir,
                output_dir=astro_out,
                allow_hosts=["origin.example", "cdn.prod.website-files.com"],
                origin_url="https://origin.example",
                resolver=pub_resolver,
                transport=transport,
            )
            self.assertTrue(run["ok"])
            self.assertEqual(run["assetCount"], 9)
            self.assertGreaterEqual(run["rewrittenCssCount"], 2)

            amap = json.loads((astro_out / "asset-map.json").read_text(encoding="utf-8"))["map"]
            dyn_local = amap["https://origin.example/wp-content/themes/acme/dynamic_css.php?id=7"]
            self.assertTrue(dyn_local.endswith(".css"))
            # Verify Pass 2 CSS was rewritten locally
            dyn_css_text = (astro_out / "public" / dyn_local.lstrip("/")).read_text(encoding="utf-8")
            self.assertIn("/wp-content/themes/acme/nested.css", dyn_css_text)
            self.assertIn("/wp-content/themes/images/bg.svg", dyn_css_text)
            nested_css_text = (astro_out / "public" / "wp-content" / "themes" / "acme" / "nested.css").read_text(encoding="utf-8")
            self.assertIn("/wp-content/fonts/acme.woff2", nested_css_text)
            self.assertTrue((astro_out / "public" / "wp-content" / "fonts" / "acme.woff2").is_file())

            # Verify HTML rewrite + SRI strip on local tags only
            rewritten = rewrite_html_assets(sample_html, amap)
            self.assertNotIn("cdn.prod.website-files.com", rewritten)
            self.assertNotIn('integrity="sha384-abc"', rewritten)
            self.assertNotIn('integrity="sha384-xyz"', rewritten)
            self.assertIn('integrity="sha384-keep"', rewritten)

    def test_cf_email_decode_tracker_strip_and_scaffold_clone_editorial(self) -> None:
        encoded = _encode_cf_email("hello@acme.example")
        self.assertEqual(decode_cf_email_hex(encoded), "hello@acme.example")

        raw_html = (
            '<!doctype html><html lang="en" data-wf-page="wf123"><head>'
            "<title>Acme {Special}</title>"
            '<meta name="description" content="Acme desc">'
            '<script src="https://www.googletagmanager.com/gtag/js?id=G-1234567"></script>'
            '<script src="/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js"></script>'
            '</head><body class="dark">'
            '<h1>Welcome {Team}</h1>'
            f'<a href="/cdn-cgi/l/email-protection#{encoded}">[email&#160;protected]</a>'
            '<div data-turnstile-sitekey="0xTEST"></div>'
            "</body></html>"
        )
        decoded = decode_cloudflare_emails(raw_html)
        self.assertIn("mailto:hello@acme.example", decoded)
        self.assertNotIn("/cdn-cgi/", decoded)

        stripped = strip_trackers(decoded)
        self.assertNotIn("googletagmanager.com", stripped)
        self.assertNotIn("data-turnstile-sitekey", stripped)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "src-html"
            src_dir.mkdir()
            (src_dir / "index.html").write_text(raw_html, encoding="utf-8")

            clone_out = root / "astro-clone"
            prev = scaffold_astro_project(
                source_path=src_dir,
                output_dir=clone_out,
                mode="clone",
                decode_cf_email=True,
                staging_lock=True,
                strip_trackers_flag=True,
                apply=False,
            )
            self.assertFalse(prev["applied"])
            self.assertFalse((clone_out / "package.json").exists())

            with self.assertRaises(ValueError):
                scaffold_astro_project(
                    source_path=src_dir,
                    output_dir=clone_out,
                    mode="clone",
                    decode_cf_email=True,
                    staging_lock=True,
                    strip_trackers_flag=True,
                    apply=True,
                )

            applied = scaffold_astro_project(
                source_path=src_dir,
                output_dir=clone_out,
                mode="clone",
                decode_cf_email=True,
                staging_lock=True,
                strip_trackers_flag=True,
                apply=True,
                confirm_plan=prev["planSha256"],
            )
            self.assertTrue(applied["applied"])
            pkg = json.loads((clone_out / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(pkg["dependencies"]["astro"], "7.4.13")
            self.assertEqual(pkg["overrides"]["undici"], "^8.13.1")
            self.assertIn("<Fragment set:html={bodyHtml} />", (clone_out / "src" / "layouts" / "LegacyDocument.astro").read_text(encoding="utf-8"))
            self.assertIn("entry.id", (clone_out / "src" / "pages" / "[...slug].astro").read_text(encoding="utf-8"))
            self.assertIn("Disallow: /", (clone_out / "public" / "robots.txt").read_text(encoding="utf-8"))
            self.assertIn("X-Robots-Tag: noindex", (clone_out / "public" / "_headers").read_text(encoding="utf-8"))
            idx_md = (clone_out / "src" / "content" / "pages" / "index.md").read_text(encoding="utf-8")
            self.assertIn("mailto:hello@acme.example", idx_md)
            self.assertNotIn("googletagmanager", idx_md)
            self.assertNotIn("/cdn-cgi/", idx_md)

    def test_wordpress_editorial_yoast_cpt_featured_image_and_rich_markdown(self) -> None:
        md = html_to_markdown(
            '<table><thead><tr><th>Plan</th><th>Price</th></tr></thead>'
            '<tbody><tr><td>Pro</td><td>$99</td></tr></tbody></table>'
            '<figure><img src="/img/hero.webp" alt="Hero"><figcaption>Launch banner</figcaption></figure>'
            '<iframe src="https://www.youtube.com/embed/abc" title="Demo Video"></iframe>'
        )
        self.assertIn("| Plan | Price |", md)
        self.assertIn("| Pro | $99 |", md)
        self.assertIn("![Hero](/img/hero.webp)", md)
        self.assertIn("*Launch banner*", md)
        self.assertIn('<iframe src="https://www.youtube.com/embed/abc" title="Demo Video"></iframe>', md)

        wxr = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0"
    xmlns:excerpt="http://wordpress.org/export/1.2/excerpt/"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
    <item>
        <wp:post_id>901</wp:post_id>
        <wp:post_type>attachment</wp:post_type>
        <wp:attachment_url>https://wp.example/wp-content/uploads/hero.jpg</wp:attachment_url>
    </item>
    <item>
        <title>Raw Service Title</title>
        <link>https://wp.example/services/cloud-audit/</link>
        <dc:creator>alex</dc:creator>
        <category domain="category">Engineering</category>
        <category domain="post_tag">Astro</category>
        <wp:post_name>cloud-audit</wp:post_name>
        <wp:post_type>service</wp:post_type>
        <wp:status>publish</wp:status>
        <content:encoded><![CDATA[<p>Cloud audit details.</p>]]></content:encoded>
        <wp:postmeta>
            <wp:meta_key>_yoast_wpseo_title</wp:meta_key>
            <wp:meta_value>Yoast SEO Cloud Audit Title</wp:meta_value>
        </wp:postmeta>
        <wp:postmeta>
            <wp:meta_key>_thumbnail_id</wp:meta_key>
            <wp:meta_value>901</wp:meta_value>
        </wp:postmeta>
    </item>
</channel>
</rss>"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml_p = root / "wp.xml"
            xml_p.write_text(wxr, encoding="utf-8")
            contract = audit_source(xml_p)
            self.assertEqual(contract["summary"]["pageCount"], 1)
            p = contract["pages"][0]
            self.assertEqual(p["title"], "Yoast SEO Cloud Audit Title")
            self.assertEqual(p["rawTitle"], "Raw Service Title")
            self.assertEqual(p["postType"], "service")
            self.assertEqual(p["author"], "alex")
            self.assertEqual(p["categories"], ["Engineering"])
            self.assertEqual(p["tags"], ["Astro"])
            self.assertEqual(p["featuredImage"], "https://wp.example/wp-content/uploads/hero.jpg")

            ed_out = root / "astro-editorial"
            prev = scaffold_astro_project(source_path=xml_p, output_dir=ed_out, mode="editorial", apply=False)
            scaffold_astro_project(source_path=xml_p, output_dir=ed_out, mode="editorial", apply=True, confirm_plan=prev["planSha256"])
            md_txt = (ed_out / "src" / "content" / "pages" / "cloud-audit.md").read_text(encoding="utf-8")
            self.assertIn("Yoast SEO Cloud Audit Title", md_txt)
            self.assertIn('postType: "service"', md_txt)
            self.assertIn('featuredImage: "https://wp.example/wp-content/uploads/hero.jpg"', md_txt)


class TestUpgradedVerifyBuildAndTwinDiff(unittest.TestCase):
    def test_verify_build_staging_prod_forbid_host_css_sri_sitemap_redirects_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dist = root / "dist"
            (dist / "assets").mkdir(parents=True)
            (dist / "assets" / "app.css").write_text(
                'body{background:url("/assets/missing.png");}\n'
                '.hotlink{background:url("https://cdn.forbidden.example/pic.jpg");}\n',
                encoding="utf-8",
            )
            (dist / "assets" / "app.js").write_text("console.log(1);", encoding="utf-8")
            (dist / "index.html").write_text(
                '<!doctype html><html data-wf-page="wrong-id"><head>'
                "<title>Site</title>"
                '<link rel="stylesheet" href="/assets/app.css" integrity="sha384-stale">'
                '<script src="https://www.googletagmanager.com/gtag/js?id=G-999999"></script>'
                '<script type="application/ld+json">{bad json</script>'
                "</head><body>"
                "<p>Contact via /cdn-cgi/l/email-protection and [contact-form-7 id=\"1\"]</p>"
                "</body></html>",
                encoding="utf-8",
            )
            (dist / "sitemap-index.xml").write_text(
                '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<sitemap><loc>https://site.example/sitemap-0.xml</loc></sitemap></sitemapindex>",
                encoding="utf-8",
            )
            (dist / "sitemap-0.xml").write_text(
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://site.example/does-not-exist/</loc></url></urlset>",
                encoding="utf-8",
            )
            (dist / "_redirects").write_text(
                "/a /b 301\n/b /a 301\n/old /missing-dest 301\n",
                encoding="utf-8",
            )

            contract_p = root / "contract.json"
            contract_p.write_text(
                json.dumps(
                    {
                        "recommendedAstroConfig": {"trailingSlash": "ignore"},
                        "pages": [
                            {
                                "route": "/",
                                "status": "publish",
                                "title": "Site",
                                "canonical": "https://site.example/",
                                "openGraph": {"og:title": "Site"},
                                "jsonLdCount": 1,
                                "jsonLdTypes": ["Organization"],
                                "headings": {"h1": ["Main Heading"]},
                                "dataWfPage": "expected-wf-id",
                                "requiredDomIds": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            res = verify_build(
                dist,
                contract_path=contract_p,
                forbid_hosts=["cdn.forbidden.example"],
                env="staging",
            )
            self.assertFalse(res["ok"])
            codes = {f["code"] for f in res["failures"]}
            for expected in (
                "missing_css_asset",
                "forbidden_host_referenced",
                "local_asset_sri_integrity",
                "invalid_jsonld",
                "cdn_cgi_leak",
                "raw_shortcode_leak",
                "sitemap_broken_route",
                "redirect_loop",
                "redirect_broken_destination",
                "staging_robots_lock_missing",
                "staging_headers_lock_missing",
                "staging_noindex_missing",
                "staging_live_tracker_leaked",
                "contract_canonical_lost",
                "contract_og_lost",
                "contract_jsonld_type_lost",
                "contract_h1_lost",
                "contract_webflow_page_id_mismatch",
            ):
                self.assertIn(expected, codes, f"Missing expected failure code {expected} in {codes}")

            # Verify production env catches leaked staging locks
            prod_dist = root / "prod-dist"
            prod_dist.mkdir()
            (prod_dist / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
            (prod_dist / "_headers").write_text("/*\n  X-Robots-Tag: noindex\n", encoding="utf-8")
            (prod_dist / "index.html").write_text(
                '<!doctype html><html><head><title>Prod</title><meta name="robots" content="noindex"></head><body><h1>Hi</h1></body></html>',
                encoding="utf-8",
            )
            prod_res = verify_build(prod_dist, env="production")
            prod_codes = {f["code"] for f in prod_res["failures"]}
            self.assertIn("production_robots_disallow_all", prod_codes)
            self.assertIn("production_headers_noindex_leaked", prod_codes)
            self.assertIn("production_noindex_leaked", prod_codes)

    def test_twin_diff_and_ssrf_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = {
                "schema": "legends.astro-design-twin/v1",
                "outDir": str(root / "base"),
                "screenshots": [{"file": "home-desktop-1440-viewport.png", "page": "home", "kind": "desktop-viewport"}],
                "pages": [
                    {
                        "id": "home",
                        "route": "/",
                        "desktop": {
                            "status": 200,
                            "errors": {"pageErrors": [], "consoleErrors": [], "requestFailures": []},
                            "extraction": {
                                "horizontalOverflow": {"hasOverflow": False, "overflowPx": 0, "offenders": []},
                                "loadedFonts": ["Inter (400 normal)"],
                                "sections": [{"selector": "header", "y": 0, "height": 80}],
                                "headings": [{"tag": "h1", "fontSize": "48px", "fontFamily": "Inter"}],
                                "borderRadii": [{"value": "4px"}],
                                "buttons": [{"renderedText": "Start"}],
                                "footers": [{"backgroundColor": "rgb(0, 0, 0)"}],
                            },
                        },
                        "mobile": {
                            "status": 200,
                            "errors": {"pageErrors": [], "consoleErrors": [], "requestFailures": []},
                            "extraction": {
                                "horizontalOverflow": {"hasOverflow": False, "overflowPx": 0, "offenders": []},
                                "loadedFonts": ["Inter (400 normal)"],
                            },
                        },
                    }
                ],
            }
            candidate_ok = json.loads(json.dumps(baseline))
            candidate_bad = json.loads(json.dumps(baseline))
            candidate_bad["pages"][0]["desktop"]["errors"]["consoleErrors"].append("TypeError: boom")
            candidate_bad["pages"][0]["desktop"]["extraction"]["horizontalOverflow"] = {"hasOverflow": True, "overflowPx": 32, "offenders": []}
            candidate_bad["pages"][0]["desktop"]["extraction"]["loadedFonts"] = []
            candidate_bad["pages"][0]["desktop"]["extraction"]["sections"][0]["y"] = 200

            b_path = root / "base.json"
            ok_path = root / "cand_ok.json"
            bad_path = root / "cand_bad.json"
            b_path.write_text(json.dumps(baseline), encoding="utf-8")
            ok_path.write_text(json.dumps(candidate_ok), encoding="utf-8")
            bad_path.write_text(json.dumps(candidate_bad), encoding="utf-8")

            res_ok = diff_twins(b_path, ok_path)
            self.assertTrue(res_ok["ok"])
            self.assertEqual(res_ok["regressionCount"], 0)

            res_bad = diff_twins(b_path, bad_path)
            self.assertFalse(res_bad["ok"])
            reg_codes = {r["code"] for r in res_bad["regressions"]}
            self.assertIn("console_error_regression", reg_codes)
            self.assertIn("horizontal_overflow_regression", reg_codes)
            self.assertIn("missing_loaded_font", reg_codes)
            drift_codes = {d["code"] for d in res_bad["drifts"]}
            self.assertIn("section_layout_drift", drift_codes)

            # CLI twin-diff returns non-zero on regression
            rc = cli_main(["twin-diff", "--baseline", str(b_path), "--candidate", str(bad_path)])
            self.assertEqual(rc, 1)

            # run_twin refuses SSRF target URL before invoking Node
            with self.assertRaises(ValueError):
                run_twin(module_root=MODULE_ROOT, output_dir=root / "twin-out", url="http://127.0.0.1:8080/")

    def test_docs_nav_cloudflare_and_new_guides(self) -> None:
        self.assertIn("@astrojs/cloudflare", OFFICIAL_PACKAGES)
        fonts = lookup_docs(query="fonts", module_root=MODULE_ROOT)
        self.assertEqual(fonts["vendoredPageCount"], 45)
        ids = [m["id"] for m in fonts["matches"]]
        self.assertIn("fonts", ids)
        cf = lookup_docs(query="cloudflare", module_root=MODULE_ROOT)
        cf_ids = [m["id"] for m in cf["matches"]]
        self.assertIn("integration-cloudflare", cf_ids)


if __name__ == "__main__":
    unittest.main()
