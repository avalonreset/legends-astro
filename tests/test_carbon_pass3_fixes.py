"""Targeted regression tests for Stage 4h Carbon Pass 3 findings (P3-M-01, P3-M-02, P3-N-01)."""

from __future__ import annotations

import time
import unittest

from legends_astro.auditor import _sanitize_markdown_url, html_to_markdown
from legends_astro.capture import (
    _strip_script_style_blocks,
    detect_empty_spa_shell,
    detect_waf_challenge,
    extract_head_and_body,
    extract_root_attrs,
)
from legends_astro.net_safety import extract_html_tag_bodies, replace_html_blocks
from legends_astro.scaffold import decode_cloudflare_emails, strip_trackers
from legends_astro.vendor import (
    _TAG_ATTR_RE,
    _rewrite_html_rel_assets,
    extract_html_asset_urls,
    rewrite_html_assets,
    strip_sri_on_local_tags,
    strip_vendored_preconnect_links,
)


class TestCarbonPass3Fixes(unittest.TestCase):
    # ------------------------------------------------------------------
    # P3-M-01: HTML character reference decoding & backslash escaping in
    #          _sanitize_markdown_url()
    # ------------------------------------------------------------------
    def test_p3_m01_blocks_html_entity_obfuscated_urls_in_markdown(self) -> None:
        html_cases = [
            ('<p><a href="javascript&amp;colon;alert(1)">ClickAmpColon</a></p>', "ClickAmpColon"),
            ('<p><a href="javascript&amp;#58;alert(1)">ClickAmpDec</a></p>', "ClickAmpDec"),
            ('<p><a href="javascript&amp;#x3a;alert(1)">ClickAmpHex</a></p>', "ClickAmpHex"),
            ('<p><a href="java&amp;Tab;script&amp;colon;alert(1)">ClickAmpTabColon</a></p>', "ClickAmpTabColon"),
            (
                '<p><a href="data&amp;colon;text/html,&lt;script&gt;alert(1)&lt;/script&gt;">ClickDataColon</a></p>',
                "ClickDataColon",
            ),
            ('<p><a href="vbscript&amp;colon;msgbox(1)">ClickVbColon</a></p>', "ClickVbColon"),
            ('<p><a href="/&amp;sol;evil.example/pwn">ClickAmpSol</a></p>', "ClickAmpSol"),
            ('<p><a href="/&amp;bsol;evil.example/pwn">ClickAmpBsol</a></p>', "ClickAmpBsol"),
            ('<p><a href="&amp;bsol;evil.example/pwn">ClickLeadBsol</a></p>', "ClickLeadBsol"),
        ]
        for html_in, expected_text in html_cases:
            md = html_to_markdown(html_in)
            self.assertEqual(md, expected_text, f"Failed to block entity URL in {html_in!r}: got {md!r}")
            self.assertNotIn("](", md)

        # Direct _sanitize_markdown_url() checks including multi-layer encoding (>4 passes) and control/space entities
        for bad in (
            "javascript&colon;alert(1)",
            "javascript&#58;alert(1)",
            "javascript&#x3a;alert(1)",
            "java&Tab;script&colon;alert(1)",
            "data&colon;text/html,<script>alert(1)</script>",
            "/&sol;evil.example/pwn",
            "/&bsol;evil.example/pwn",
            "&bsol;evil.example/pwn",
            "&#32;//evil.example/pwn",
            "&#10;javascript:alert(1)",
            "javascript&amp;amp;amp;amp;amp;colon;alert(1)",
        ):
            self.assertIsNone(_sanitize_markdown_url(bad), f"Must reject {bad!r}")

        # Blocked on <img> and <iframe> too
        self.assertEqual(html_to_markdown('<p><img src="javascript&amp;colon;alert(1)" alt="x"></p>'), "")
        self.assertEqual(html_to_markdown('<iframe src="javascript&amp;colon;alert(1)" title="t"></iframe>'), "")

    def test_p3_m01_preserves_query_params_and_escapes_backslashes(self) -> None:
        # Query param ?a=1&copy=2 has no semicolon after &copy and must survive untouched
        self.assertEqual(
            _sanitize_markdown_url("https://example.com/search?a=1&copy=2"),
            "https://example.com/search?a=1&copy=2",
        )
        # In HTML attributes, '&' is serialized as '&amp;'; HTMLParser decodes '&amp;' once
        # into '?a=1&copy=2' and _sanitize_markdown_url() must preserve '&copy=2' untouched.
        md_q = html_to_markdown('<p><a href="https://example.com/search?a=1&amp;copy=2">Search</a></p>')
        self.assertEqual(md_q, "[Search](https://example.com/search?a=1&copy=2)")

        # Valid semicolon-terminated &amp; inside query string decodes cleanly
        self.assertEqual(
            _sanitize_markdown_url("https://example.com/search?a=1&amp;b=2"),
            "https://example.com/search?a=1&b=2",
        )

        # Backslashes are percent-encoded so CommonMark backslash escapes cannot alter destinations
        self.assertEqual(
            _sanitize_markdown_url("https://example.com/docs\\guide(1).html"),
            "https://example.com/docs%5Cguide%281%29.html",
        )
        self.assertEqual(
            _sanitize_markdown_url("docs\\guide.html"),
            "docs%5Cguide.html",
        )

    # ------------------------------------------------------------------
    # P3-M-02: Eliminate all residual O(N^2) HTML/block ReDoS
    # ------------------------------------------------------------------
    def test_p3_m02_linear_time_on_all_pass3_adversarial_payloads(self) -> None:
        payloads = {
            "unclosed_script_open": "<script " * 5000,
            "unclosed_div_open": "<div " * 5000,
            "unclosed_script_block": "<script>" * 5000,
            "unclosed_comment": "<!-- " * 5000,
            "tag_whitespace_non_attr": "<div" + (" " * 5000) + "1>",
            "unclosed_style_block": "<style>" * 5000,
            "unclosed_noscript_block": "<noscript>" * 5000,
            "unclosed_wp_comment": "<!-- wp:" * 5000,
            "unclosed_quoted_div": '<div a="' * 5000,
        }
        rel_map = {"/img/a.png": "/assets/a.png"}
        asset_map = {"https://cdn.example/a.png": "/assets/a.png"}

        for name, payload in payloads.items():
            t0 = time.monotonic()
            # vendor.py
            _rewrite_html_rel_assets(payload, rel_map)
            rewrite_html_assets(payload, asset_map, root_relative_map=rel_map)
            extract_html_asset_urls(payload)
            strip_sri_on_local_tags(payload)
            strip_vendored_preconnect_links(payload, asset_map)
            _TAG_ATTR_RE.findall(payload)
            # scaffold.py
            strip_trackers(payload)
            decode_cloudflare_emails(payload)
            # capture.py
            _strip_script_style_blocks(payload)
            detect_waf_challenge(200, {}, payload)
            detect_empty_spa_shell(payload)
            extract_head_and_body(payload)
            extract_root_attrs(payload)
            # auditor.py
            html_to_markdown(payload)
            elapsed = time.monotonic() - t0
            self.assertLess(
                elapsed,
                0.25,
                f"O(N^2) ReDoS regression for {name!r}: took {elapsed:.3f}s",
            )

    def test_p3_m02_functional_html_scanner_and_block_helpers(self) -> None:
        # Quoted '>' inside opening tags, <script> body untouched, <style> body rewritten, comments untouched
        sample = (
            '<!-- <img src="/img/a.png"> -->'
            '<script src="/js/app.js" data-expr="a>b">const x = "/img/a.png";</script>'
            '<style media="(min-width: 1px)" data-gt="1>0">.hero{background:url("/img/a.png");}</style>'
            '<img data-note="x>y" src="/img/a.png" alt="ok">'
        )
        rel_map = {"/js/app.js": "/assets/app.js", "/img/a.png": "/assets/a.png"}
        rewritten = _rewrite_html_rel_assets(sample, rel_map)
        self.assertIn('<!-- <img src="/img/a.png"> -->', rewritten)
        self.assertIn('<script src="/assets/app.js" data-expr="a>b">const x = "/img/a.png";</script>', rewritten)
        self.assertIn('.hero{background:url("/assets/a.png");}', rewritten)
        self.assertIn('<img data-note="x>y" src="/assets/a.png" alt="ok">', rewritten)

        # extract_html_tag_bodies ignores <stylesheet> (tag boundary check) and extracts real <style>
        bodies = extract_html_tag_bodies('<stylesheet>bad</stylesheet><style type="text/css">ok</style>', "style")
        self.assertEqual(bodies, ["ok"])

        # replace_html_blocks strips <script> & <!-- --> cleanly while respecting tag boundaries
        stripped = replace_html_blocks(
            '<scripting>keep</scripting><script>drop</script><!-- c --><p>hi</p>',
            tags=("script",),
            strip_comments=True,
            default_replacement=" ",
        )
        self.assertIn("<scripting>keep</scripting>", stripped)
        self.assertNotIn("drop", stripped)
        self.assertNotIn("<!--", stripped)
        self.assertIn("<p>hi</p>", stripped)

        # strip_trackers removes GTM script & noscript but preserves app script
        tracked = (
            '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-ABC123"></script>'
            '<noscript><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-ABC123"></iframe></noscript>'
            '<script src="/assets/app.js"></script>'
        )
        cleaned = strip_trackers(tracked)
        self.assertNotIn("googletagmanager", cleaned)
        self.assertIn('<script src="/assets/app.js"></script>', cleaned)

    # ------------------------------------------------------------------
    # P3-N-01: GFM table cell backslash escaping before pipe
    # ------------------------------------------------------------------
    def test_p3_n01_gfm_table_cell_escapes_backslash_before_pipe(self) -> None:
        html_tbl = (
            "<table>"
            "<tr><th>Expr</th><th>Notes</th></tr>"
            "<tr><td>\\| text</td><td>a | b</td></tr>"
            "<tr><td>\\\\| two</td><td>plain \\ slash</td></tr>"
            "</table>"
        )
        md = html_to_markdown(html_tbl)
        lines = md.splitlines()
        self.assertEqual(lines[0], "| Expr | Notes |")
        self.assertEqual(lines[1], "| --- | --- |")
        self.assertEqual(lines[2], r"| \\\| text | a \| b |")
        self.assertEqual(lines[3], r"| \\\\\| two | plain \\ slash |")


if __name__ == "__main__":
    unittest.main()
