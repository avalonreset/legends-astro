"""Targeted regression tests for Stage 4f Carbon Pass 2 findings (P2-H-01, P2-M-01, P2-M-02, P2-M-03, P2-L-01)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from legends_astro.auditor import (
    _sanitize_markdown_url,
    html_to_markdown,
    ingest_source_to_astro,
)
from legends_astro.capture import _CSS_URL_HOST_RE, extract_head_and_body
from legends_astro.net_safety import sanitize_rel_path
from legends_astro.scaffold import scaffold_astro_project
from legends_astro.vendor import (
    _CSS_URL_QUOTED_RE,
    _CSS_URL_RE,
    _HTML_SEGMENT_RE,
    extract_css_urls,
    rewrite_css_assets,
    rewrite_html_assets,
    vendor_assets,
)

MODULE_ROOT = Path(__file__).resolve().parent.parent
PUB_RESOLVER = lambda _host, _port: ["93.184.216.34"]


def _write(base: Path, rel: str, content: str) -> Path:
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


class TestCarbonPass2Fixes(unittest.TestCase):
    # ------------------------------------------------------------------
    # P2-H-01: Complete H-02 XSS closure in html_to_markdown, scaffold, ingest
    # ------------------------------------------------------------------
    def test_p2_h01_inline_code_in_link_figcaption_and_unclosed(self) -> None:
        # 1. <code> inside <a>
        md_link = html_to_markdown(
            '<p><a href="https://example.com"><code>&lt;script&gt;window.__XSS1=1&lt;/script&gt;</code></a></p>'
        )
        self.assertEqual(md_link, "[`<script>window.__XSS1=1</script>`](https://example.com)")

        # 2. <code> inside <figcaption> (and nested <a><code>)
        md_fig = html_to_markdown(
            "<figure><figcaption><code>&lt;script&gt;window.__XSS2=1&lt;/script&gt;</code></figcaption></figure>"
        )
        self.assertEqual(md_fig, "*`<script>window.__XSS2=1</script>`*")

        md_fig_link = html_to_markdown(
            '<figure><figcaption><a href="https://example.com"><code>&lt;script&gt;x&lt;/script&gt;</code></a></figcaption></figure>'
        )
        self.assertEqual(md_fig_link, "*[`<script>x</script>`](https://example.com)*")

        # 3. Unclosed <code> inside <p> must NOT leak into subsequent paragraphs
        md_unclosed = html_to_markdown(
            "<p><code>unclosed</p><p>&lt;script&gt;window.__XSS3=1&lt;/script&gt;</p>"
        )
        self.assertEqual(md_unclosed, "`unclosed`\n\n&lt;script&gt;window.__XSS3=1&lt;/script&gt;")

        # 4. Unclosed <code> at EOF
        md_eof = html_to_markdown("<code>&lt;script&gt;eof&lt;/script&gt;")
        self.assertEqual(md_eof, "`<script>eof</script>`")

        # 5. Unclosed <code> inside <a>
        md_a_unclosed = html_to_markdown(
            '<p><a href="https://example.com"><code>&lt;script&gt;a&lt;/script&gt;</a></p>'
        )
        self.assertEqual(md_a_unclosed, "[`<script>a</script>`](https://example.com)")

    def test_p2_h01_backtick_and_pre_fence_breakouts(self) -> None:
        # Inline backtick breakout uses CommonMark N+1 delimiters
        md_bt = html_to_markdown(
            "<p><code>a` &lt;script&gt;window.__XSS_BT=1&lt;/script&gt; `b</code></p>"
        )
        self.assertEqual(md_bt, "``a` <script>window.__XSS_BT=1</script> `b``")

        # Leading/trailing backtick inside <code> gets single-space padding per CommonMark
        md_edge_bt = html_to_markdown("<p><code>`&lt;script&gt;x&lt;/script&gt;`</code></p>")
        self.assertEqual(md_edge_bt, "`` `<script>x</script>` ``")

        # Leading + trailing space around non-whitespace gets padded
        md_edge_sp = html_to_markdown("<p><code> x </code></p>")
        self.assertEqual(md_edge_sp, "`  x  `")

        # Blank line inside inline <code> cannot split paragraph and break code span
        md_nl = html_to_markdown("<p><code>foo\n\n&lt;script&gt;x&lt;/script&gt;</code></p>")
        self.assertNotIn("\n\n", md_nl)
        self.assertEqual(md_nl, "`foo <script>x</script>`")

        # <pre> containing ``` uses 4-backtick fence
        md_pre = html_to_markdown(
            "<pre>```\n&lt;script&gt;window.__XSS_PRE=1&lt;/script&gt;\n```</pre>"
        )
        self.assertTrue(md_pre.startswith("````\n"), f"Expected 4-backtick fence, got: {md_pre!r}")
        self.assertTrue(md_pre.endswith("\n````"), f"Expected 4-backtick closing fence, got: {md_pre!r}")

        # Unclosed <pre> at EOF still gets properly fenced
        md_pre_eof = html_to_markdown("<pre>```\n&lt;script&gt;eof&lt;/script&gt;")
        self.assertTrue(md_pre_eof.startswith("````\n"))
        self.assertTrue(md_pre_eof.endswith("\n````"))

    def test_p2_h01_heading_escaping_in_editorial_scaffold_and_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_html = root / "site"
            # Page without <h1> in body so editorial/ingest synthesizes `# {title}`
            _write(
                src_html,
                "index.html",
                "<!doctype html><html><head><title>Doc &lt;script&gt;window.__XSS_TITLE=1&lt;/script&gt;</title></head>"
                "<body><p>Body paragraph.</p></body></html>",
            )
            # Page with empty body so else-branch synthesizes `# {title}`
            _write(
                src_html,
                "empty.html",
                "<!doctype html><html><head><title>Empty &lt;script&gt;window.__XSS_EMPTY=1&lt;/script&gt;</title></head>"
                "<body></body></html>",
            )

            ed_out = root / "editorial"
            prev = scaffold_astro_project(source_path=src_html, output_dir=ed_out, mode="editorial", apply=False)
            scaffold_astro_project(
                source_path=src_html,
                output_dir=ed_out,
                mode="editorial",
                apply=True,
                confirm_plan=prev["planSha256"],
            )
            for fname in ("index.md", "empty.md"):
                md_text = (ed_out / "src" / "content" / "pages" / fname).read_text(encoding="utf-8")
                body_part = md_text.split("---\n", 2)[2]
                self.assertNotIn("<script>", body_part)
                self.assertIn("&lt;script&gt;", body_part)

            # WordPress WXR with <script> in <title> via ingest and editorial scaffold
            wxr = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:wp="http://wordpress.org/export/1.2/">
<channel>
  <item>
    <title>Post &lt;script&gt;window.__XSS_WP=1&lt;/script&gt;</title>
    <link>https://example.com/post-1/</link>
    <wp:post_name>post-1</wp:post_name>
    <wp:post_type>post</wp:post_type>
    <wp:status>publish</wp:status>
    <content:encoded><![CDATA[<p>Hello</p>]]></content:encoded>
  </item>
</channel>
</rss>"""
            wxr_path = _write(root, "wp.xml", wxr)
            ing_out = root / "ingest"
            ing_prev = ingest_source_to_astro(wxr_path, ing_out, apply=False)
            ingest_source_to_astro(wxr_path, ing_out, apply=True, confirm_plan=ing_prev["planSha256"])
            wp_md = (ing_out / "src" / "content" / "pages" / "post-1.md").read_text(encoding="utf-8")
            wp_body = wp_md.split("---\n", 2)[2]
            self.assertNotIn("<script>", wp_body)
            self.assertIn("# Post &lt;script&gt;window.__XSS_WP=1&lt;/script&gt;", wp_body)

    def test_p2_h01_sanitize_markdown_url_rejects_protocol_relative_and_backslash(self) -> None:
        for bad in (
            "//evil.example/pwn",
            "/\\evil.example/pwn",
            "\\\\evil.example/pwn",
            "\\evil.example/pwn",
            "javascript:alert(1)",
        ):
            self.assertIsNone(_sanitize_markdown_url(bad), f"Must reject {bad!r}")

        md = html_to_markdown('<p><a href="/\\evil.example/pwn">Evil</a></p>')
        self.assertEqual(md, "Evil")
        self.assertNotIn("evil.example", md)

    # ------------------------------------------------------------------
    # P2-M-01: sanitize_rel_path() quote/paren stripping & rewriter escaping
    # ------------------------------------------------------------------
    def test_p2_m01_sanitize_rel_path_and_rewriter_defense_in_depth(self) -> None:
        # Percent-encoded and double-encoded quotes, parens, commas, semicolons, backticks, DEL
        san = sanitize_rel_path("/x%27onerror=%27alert%281%29%27a=%2C%3B%60%7F.png")
        for forbidden in ("'", '"', "(", ")", ",", ";", "`", "\x7f"):
            self.assertNotIn(forbidden, san, f"Forbidden char {forbidden!r} leaked into {san!r}")

        san_double = sanitize_rel_path("/founder%2527s-photo.jpg")
        self.assertNotIn("'", san_double)

        # End-to-end vendor_assets + rewrite_html_assets on single-quoted percent-encoded payload
        evil_url = "https://cdn.example/x%27onerror=%27alert(1)%27a=%27.png"
        html_in = f"<!doctype html><html><body><img src='{evil_url}'></body></html>"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            _write(src, "index.html", html_in)
            out = root / "out"
            res = vendor_assets(
                source_dir=src,
                output_dir=out,
                allow_hosts=["cdn.example"],
                resolver=PUB_RESOLVER,
                transport=lambda u, _ips, _h, _t: (200, {"Content-Type": "image/png"}, b"PNG") if u == evil_url else (404, {}, b""),
            )
            self.assertTrue(res["ok"])
            amap = json.loads((out / "asset-map.json").read_text(encoding="utf-8"))["map"]
            local_path = amap[evil_url]
            self.assertNotIn("'", local_path)
            self.assertNotIn("(", local_path)
            self.assertNotIn(")", local_path)
            rewritten = rewrite_html_assets(html_in, amap)
            self.assertIn(f"<img src='{local_path}'>", rewritten)

        # Defense-in-depth in _sub_attr() for custom asset_map values with quotes/whitespace
        custom_rel_map = {
            "/img/sq.png": "/assets/x'onerror='alert(1).png",
            "/img/dq.png": '/assets/x"onload="alert(1).png',
            "/img/uq.png": "/assets/has space'quote.png",
        }
        rew_sq = rewrite_html_assets("<img src='/img/sq.png'>", {}, root_relative_map=custom_rel_map)
        self.assertEqual(rew_sq, "<img src='/assets/x&#39;onerror=&#39;alert(1).png'>")

        rew_dq = rewrite_html_assets('<img src="/img/dq.png">', {}, root_relative_map=custom_rel_map)
        self.assertEqual(rew_dq, '<img src="/assets/x&quot;onload=&quot;alert(1).png">')

        rew_uq = rewrite_html_assets("<img src=/img/uq.png>", {}, root_relative_map=custom_rel_map)
        self.assertEqual(rew_uq, '<img src="/assets/has space\'quote.png">')

        # Defense-in-depth in _rewrite_css_urls_with_lookup() / rewrite_css_assets()
        css_in = 'body{background:url("https://cdn.example/bg.png");}@import "https://cdn.example/sub.css";'
        css_out = rewrite_css_assets(
            css_in,
            {
                "https://cdn.example/bg.png": "/assets/a'b\"(c) d.png",
                "https://cdn.example/sub.css": "/assets/sub\"(1).css",
            },
        )
        self.assertIn('url("/assets/a%27b%22%28c%29%20d.png")', css_out)
        self.assertIn('@import "/assets/sub%22%281%29.css"', css_out)

    # ------------------------------------------------------------------
    # P2-M-02: Eliminate ReDoS in vendor.py regexes
    # ------------------------------------------------------------------
    def test_p2_m02_no_redos_in_html_segment_and_css_url_regexes(self) -> None:
        adv_tag = "<div " + ("a" * 10000)
        adv_spaces = "<div" + (" " * 10000)
        adv_css = "url(" * 10000
        adv_css_host = "url(https://" * 10000

        t0 = time.monotonic()
        _HTML_SEGMENT_RE.findall(adv_tag)
        _HTML_SEGMENT_RE.findall(adv_spaces)
        rewrite_html_assets(adv_tag, {}, root_relative_map={"/a.png": "/assets/a.png"})
        _CSS_URL_RE.findall(adv_css)
        _CSS_URL_QUOTED_RE.findall(adv_css)
        _CSS_URL_HOST_RE.findall(adv_css_host)
        extract_css_urls(adv_css)
        rewrite_css_assets(adv_css, {"https://cdn.example/x.png": "/assets/x.png"})
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.25, f"ReDoS regression detected: took {elapsed:.3f}s")

        # Verify multi-query & and &amp; still match cleanly
        css_ok = (
            'a{background:url("https://cdn.example/img.jpg?w=800&h=600&fit=crop");}'
            'b{background:url(&quot;https://cdn.example/img2.jpg?x=1&amp;y=2&quot;);}'
        )
        urls = extract_css_urls(css_ok)
        self.assertIn("https://cdn.example/img.jpg?w=800&h=600&fit=crop", urls)
        self.assertIn("https://cdn.example/img2.jpg?x=1&y=2", urls)

    # ------------------------------------------------------------------
    # P2-M-03: _HeadBodySlicer line offsets with \u2028/\u2029/\r
    # ------------------------------------------------------------------
    def test_p2_m03_head_body_slicer_preserves_html_with_unicode_line_separators_and_cr(self) -> None:
        for sep_name, sep_char in (
            ("u2028", "\u2028"),
            ("u2029", "\u2029"),
            ("lone_cr", "\r"),
            ("form_feed", "\x0c"),
            ("vert_tab", "\x0b"),
            ("nel", "\x85"),
        ):
            html_doc = (
                "<!doctype html><html><head>"
                + (sep_char * 30)
                + "\n<title>Test</title></head>"
                + '<body><main id="app"><h1>Intact Body</h1></main></body></html>'
            )
            head_out, body_out = extract_head_and_body(html_doc)
            self.assertIn("<title>Test</title>", head_out, f"Failed head extraction for {sep_name}")
            self.assertEqual(
                body_out,
                '<main id="app"><h1>Intact Body</h1></main>',
                f"Corrupted body extraction for {sep_name}: {body_out!r}",
            )

    # ------------------------------------------------------------------
    # P2-L-01: routeToSlug() deduplication in scripts/twin.mjs
    # ------------------------------------------------------------------
    def test_p2_l01_twin_route_to_slug_deduplication(self) -> None:
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "Node.js is required for twin.mjs routeToSlug test")
        twin_url = (MODULE_ROOT / "scripts" / "twin.mjs").as_uri()
        check_js = f"""
import {{ routeToSlug }} from {json.dumps(twin_url)};
const used = new Set();
const cases = [
  ["/", "home"],
  ["/index.html", "home__2"],
  ["/a/b/", "a__b"],
  ["/a__b/", "a__b__2"],
  ["/a__b__2/", "a__b__2__2"],
  ["/about", "about"],
  ["/about.html", "about__2"],
  ["/About", "About__3"],
  ["/pricing?plan=a", "pricing"],
  ["/pricing?plan=b", "pricing__2"],
];
for (const [route, expected] of cases) {{
  const got = routeToSlug(route, used);
  if (got !== expected) {{
    throw new Error(`routeToSlug(${{JSON.stringify(route)}}) => ${{JSON.stringify(got)}}, expected ${{JSON.stringify(expected)}}`);
  }}
}}
if (routeToSlug("/docs/") !== "docs") throw new Error("Stateless call failed");
console.log("ROUTE_TO_SLUG_OK");
"""
        proc = subprocess.run(
            [node_bin, "--input-type=module", "-e", check_js],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"routeToSlug test failed: {proc.stderr} {proc.stdout}")
        self.assertIn("ROUTE_TO_SLUG_OK", proc.stdout)

        # Verify main() passes usedIds to routeToSlug(route, usedIds)
        twin_src = (MODULE_ROOT / "scripts" / "twin.mjs").read_text(encoding="utf-8")
        self.assertIn("const usedIds = new Set();", twin_src)
        self.assertIn("routeToSlug(route, usedIds)", twin_src)


if __name__ == "__main__":
    unittest.main()
