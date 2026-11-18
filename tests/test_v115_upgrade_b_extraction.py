"""Unit tests for Upgrade B: LLM-clean Markdown & Content Extraction Overhaul."""

from __future__ import annotations

import unittest

from smartfetch.cache import build_cache_key
from smartfetch.extract import extract_content


class UpgradeBExtractionTests(unittest.TestCase):
    def test_fenced_code_blocks_preserve_language_tags_and_strip_highlight_spans(self):
        html = """
        <html><body><main>
          <h1>Code Examples</h1>
          <pre><code class="language-python"><span class="k">def</span> <span class="nf">hello</span>():
    <span class="k">return</span> <span class="s2">"world"</span></code></pre>
          <pre class="lang-ts"><code><span class="kw">const</span> x: number = 42;</code></pre>
          <pre data-language="bash"><code>echo "ok"</code></pre>
          <p>Some descriptive article text so quality check passes with plenty of words here.</p>
        </main></body></html>
        """
        res = extract_content(html, "https://docs.example.com/guide")
        md = res["markdown"]
        self.assertIn("```python\ndef hello():\n    return \"world\"\n```", md)
        self.assertIn("```ts\nconst x: number = 42;\n```", md)
        self.assertIn("```bash\necho \"ok\"\n```", md)
        self.assertNotIn("<span", md)

    def test_gfm_pipe_tables_and_layout_table_unwrapping(self):
        html = """
        <html><body><main>
          <h1>Pricing</h1>
          <table>
            <thead><tr><th>Plan</th><th>Price</th><th>Notes</th></tr></thead>
            <tbody>
              <tr>
                <td>Pro\nTier</td>
                <td>$19/mo</td>
                <td>Includes <a href="/api">API</a>\naccess | fast</td>
              </tr>
            </tbody>
          </table>
          <table role="presentation">
            <tr><td>Layout Wrapper Content Paragraph with enough text.</td></tr>
          </table>
        </main></body></html>
        """
        res = extract_content(html, "https://example.com/pricing")
        md = res["markdown"]
        self.assertIn("| Plan | Price | Notes |", md)
        self.assertIn("| --- | --- | --- |", md)
        self.assertIn("| Pro Tier | $19/mo | Includes [API](https://example.com/api) access \\| fast |", md)
        # Layout table should be unwrapped cleanly, not rendered as a pipe table
        self.assertIn("Layout Wrapper Content Paragraph", md)
        self.assertNotIn("| Layout Wrapper Content Paragraph", md)

    def test_absolute_image_resolution_and_pixel_blob_stripping(self):
        html = """
        <html><body><main>
          <h1>Gallery</h1>
          <p>Read our latest architecture overview with diagrams and detailed notes below.</p>
          <img src="/assets/arch.png" alt="Architecture Diagram" width="800" height="450" />
          <img data-src="../img/flow.webp" alt="Flow" />
          <img src="data:image/png;base64,iVBORw0KGgo=" alt="Inline Blob" />
          <img src="https://tracker.example.com/pixel.gif" width="1" height="1" alt="" />
        </main></body></html>
        """
        res = extract_content(html, "https://example.com/blog/post-1")
        md = res["markdown"]
        self.assertIn("![Architecture Diagram](https://example.com/assets/arch.png)", md)
        self.assertIn("![Flow](https://example.com/img/flow.webp)", md)
        self.assertNotIn("data:image/png", md)
        self.assertNotIn("tracker.example.com/pixel.gif", md)

    def test_cookie_banner_removal_preserves_article_about_baking_cookies(self):
        html = """
        <html><body>
          <div id="onetrust-banner-sdk">We use tracking cookies. Accept all cookies.</div>
          <div class="cookie-consent">Cookie consent overlay</div>
          <article class="cookie-recipe">
            <h1>How to Bake Chocolate Chip Cookies</h1>
            <p>Baking cookies at home requires butter, brown sugar, vanilla extract, and chocolate chips.</p>
            <p>Preheat the oven to 350 degrees Fahrenheit and bake for twelve minutes until golden.</p>
          </article>
        </body></html>
        """
        res = extract_content(html, "https://bakery.example.com/cookies")
        self.assertNotIn("We use tracking cookies", res["content"])
        self.assertNotIn("Cookie consent overlay", res["content"])
        self.assertIn("How to Bake Chocolate Chip Cookies", res["content"])
        self.assertIn("butter, brown sugar", res["markdown"])

    def test_only_main_content_true_vs_false_and_cache_key_separation(self):
        html = """
        <html><body>
          <nav><a href="/home">Top Navigation Link</a></nav>
          <main>
            <h1>Main Article Heading</h1>
            <p>Primary article body paragraph with substantial information for the reader.</p>
          </main>
          <footer>Copyright 2026 Footer Notice</footer>
        </body></html>
        """
        res_main = extract_content(html, "https://example.com/doc", only_main_content=True)
        self.assertIn("Main Article Heading", res_main["content"])
        self.assertNotIn("Top Navigation Link", res_main["content"])
        self.assertNotIn("Footer Notice", res_main["content"])

        res_full = extract_content(html, "https://example.com/doc", only_main_content=False)
        self.assertIn("Main Article Heading", res_full["content"])
        self.assertIn("Top Navigation Link", res_full["content"])
        self.assertIn("Footer Notice", res_full["content"])

        key_default = build_cache_key("https://example.com/doc")
        key_main = build_cache_key("https://example.com/doc", only_main_content=True)
        key_full = build_cache_key("https://example.com/doc", only_main_content=False)
        self.assertEqual(key_default, key_main)
        self.assertNotEqual(key_default, key_full)
        self.assertTrue(key_full.endswith(":full"))


if __name__ == "__main__":
    unittest.main()
