"""Tests for v1.13 Step 1: Waterfall Fix and Local PDF Support via pypdf."""

import io
import os
import unittest
from unittest.mock import MagicMock, patch

from pypdf import PdfReader, PdfWriter

from smartfetch.cache import get_cache_provider
from smartfetch.core import smart_fetch
from smartfetch.diagnostics import RetrievalFailure, diagnostics_for_exception
from smartfetch.extract import extract_pdf
from smartfetch.http_fetch import http_fetch
from smartfetch.server import create_app


def make_test_pdf(
    text: str = "Quarterly financial report content for tests.",
    title: str | None = None,
    num_pages: int = 1,
    password: str | None = None,
) -> bytes:
    """Helper to synthesize valid PDF bytes with text and metadata."""
    clean_text = text.replace("(", "").replace(")", "")
    stream_content = f"BT\n/F1 24 Tf\n100 700 Td\n({clean_text}) Tj\nET"
    stream_bytes = stream_content.encode("latin1")
    length = len(stream_bytes)
    base_template = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 612 792] /Contents 5 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
        b"5 0 obj\n<< /Length " + str(length).encode("ascii") + b" >>\nstream\n"
        + stream_bytes + b"\nendstream\nendobj\n"
        b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000234 00000 n \n0000000305 00000 n \n"
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n450\n%%EOF\n"
    )
    reader = PdfReader(io.BytesIO(base_template))
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.append(reader)
    if title:
        writer.add_metadata({"/Title": title})
    if password:
        writer.encrypt(password)
    bio = io.BytesIO()
    writer.write(bio)
    return bio.getvalue()


def make_blank_pdf(num_pages: int = 1) -> bytes:
    """Helper to synthesize a blank/image-only PDF with zero extracted text."""
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    bio = io.BytesIO()
    writer.write(bio)
    return bio.getvalue()


class TestWaterfallBypassFix(unittest.IsolatedAsyncioTestCase):
    """Verifies that BROWSER_WORKER_REQUIRED does not force force_browser=True in server.py."""

    async def test_server_does_not_force_browser_when_worker_required(self):
        with patch.dict(os.environ, {
            "BROWSER_WORKER_REQUIRED": "true",
            "BROWSER_WORKER_URL": "https://159.89.40.174:8443",
            "BROWSER_WORKER_HMAC_KEY": "fake_key",
        }):
            with patch("smartfetch.server.smart_fetch") as mock_fetch:
                mock_fetch.return_value = {
                    "success": True,
                    "render_method": "http",
                    "retrieval_tier": "http",
                    "content": "test",
                }
                app = create_app()
                current = app
                while hasattr(current, "app"):
                    if hasattr(current, "state") and hasattr(current.state, "smartfetch_mcp"):
                        break
                    current = current.app
                mcp = current.state.smartfetch_mcp
                fetch_tool = mcp.fastmcp._tool_manager._tools["fetch_webpage"]

                # Request with force_browser=False
                res = await fetch_tool.fn(url="https://example.com/page", force_browser=False)
                self.assertFalse(res.isError)
                mock_fetch.assert_called_once()
                args, kwargs = mock_fetch.call_args
                # args: (url, should_force, max_chars)
                # should_force MUST be False despite BROWSER_WORKER_REQUIRED=true!
                self.assertEqual(args[0], "https://example.com/page")
                self.assertFalse(args[1], "should_force must be False when force_browser=False!")


class TestHttpFetchPdfSupport(unittest.TestCase):
    """Verifies Content-Type handling and PDF byte streaming in http_fetch."""

    @patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.http_fetch.install_dns_rebinding_protection")
    @patch("smartfetch.http_fetch.SESSION.send")
    def test_http_fetch_accepts_valid_application_pdf(self, mock_send, mock_pin, mock_validate):
        pdf_bytes = make_test_pdf("Document body text", title="Sample PDF")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "application/pdf",
            "Content-Length": str(len(pdf_bytes)),
        }
        mock_resp.iter_content.return_value = [pdf_bytes]
        mock_send.return_value = mock_resp

        result = http_fetch("https://example.com/report.pdf")
        self.assertTrue(result.get("is_pdf"))
        self.assertEqual(result.get("content_type"), "application/pdf")
        self.assertEqual(result.get("raw_bytes"), pdf_bytes)
        self.assertEqual(result.get("status_code"), 200)

    @patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.http_fetch.install_dns_rebinding_protection")
    @patch("smartfetch.http_fetch.SESSION.send")
    def test_http_fetch_accepts_octet_stream_with_pdf_extension(self, mock_send, mock_pin, mock_validate):
        pdf_bytes = make_test_pdf("Octet stream PDF text")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(pdf_bytes)),
        }
        mock_resp.iter_content.return_value = [pdf_bytes]
        mock_send.return_value = mock_resp

        result = http_fetch("https://example.com/downloads/manual.PDF")
        self.assertTrue(result.get("is_pdf"))
        self.assertEqual(result.get("content_type"), "application/pdf")
        self.assertEqual(result.get("raw_bytes"), pdf_bytes)

    @patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.http_fetch.install_dns_rebinding_protection")
    @patch("smartfetch.http_fetch.SESSION.send")
    def test_http_fetch_rejects_spoofed_non_pdf_bytes(self, mock_send, mock_pin, mock_validate):
        fake_bytes = b"<html><body>Not a PDF</body></html>"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "application/pdf",
            "Content-Length": str(len(fake_bytes)),
        }
        mock_resp.iter_content.return_value = [fake_bytes]
        mock_send.return_value = mock_resp

        with self.assertRaises(RuntimeError) as ctx:
            http_fetch("https://example.com/spoofed.pdf")
        diag = diagnostics_for_exception(ctx.exception)
        self.assertIsNotNone(diag)
        self.assertEqual(diag.failure_code, "invalid_content")

    @patch("smartfetch.http_fetch.validate_public_url", side_effect=lambda u: u)
    @patch("smartfetch.http_fetch.install_dns_rebinding_protection")
    @patch("smartfetch.http_fetch.SESSION.send")
    def test_http_fetch_rejects_octet_stream_without_pdf_extension(self, mock_send, mock_pin, mock_validate):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "Content-Type": "application/octet-stream",
        }
        mock_send.return_value = mock_resp

        with self.assertRaises(RuntimeError) as ctx:
            http_fetch("https://example.com/binary.bin")
        diag = diagnostics_for_exception(ctx.exception)
        self.assertIsNotNone(diag)
        self.assertEqual(diag.failure_code, "invalid_content")


class TestExtractPdf(unittest.TestCase):
    """Verifies extract_pdf text, markdown, title, pagination, and decryption checks."""

    def test_extract_pdf_success_with_metadata_title(self):
        pdf_bytes = make_test_pdf(
            "Executive summary paragraph with financial metrics.",
            title="Q3 2026 Shareholder Letter",
            num_pages=2,
        )
        result = extract_pdf(pdf_bytes, "https://example.com/reports/q3.pdf")
        self.assertEqual(result["title"], "Q3 2026 Shareholder Letter")
        self.assertIn("Executive summary paragraph", result["content"])
        self.assertIn("## Page 1", result["markdown"])
        self.assertIn("## Page 2", result["markdown"])
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["pages_extracted"], 2)
        self.assertGreater(result["word_count"], 5)
        self.assertEqual(result["links"], [])
        self.assertFalse(result["low_quality"])

    def test_extract_pdf_fallback_title_from_url_filename(self):
        pdf_bytes = make_test_pdf("Some informative technical documentation content.")
        result = extract_pdf(pdf_bytes, "https://example.com/docs/api_developer_handbook.pdf")
        self.assertEqual(result["title"], "Api Developer Handbook")

    def test_extract_pdf_respects_max_pages(self):
        pdf_bytes = make_test_pdf("Page content line.", num_pages=5)
        result = extract_pdf(pdf_bytes, "https://example.com/multi.pdf", max_pages=3)
        self.assertEqual(result["page_count"], 5)
        self.assertEqual(result["pages_extracted"], 3)
        self.assertIn("## Page 3", result["markdown"])
        self.assertNotIn("## Page 4", result["markdown"])

    def test_extract_pdf_rejects_password_locked_pdf(self):
        locked_bytes = make_test_pdf("Secret content", password="super_secret_pwd")
        with self.assertRaises(ValueError) as ctx:
            extract_pdf(locked_bytes, "https://example.com/locked.pdf")
        self.assertIn("password-protected", str(ctx.exception).lower())

    def test_extract_pdf_rejects_invalid_header(self):
        with self.assertRaises(ValueError):
            extract_pdf(b"not a valid pdf", "https://example.com/bad.pdf")


class TestSmartFetchPdfIntegration(unittest.TestCase):
    """Verifies smart_fetch Tier 1 PDF handling, caching, and absence of browser escalation."""

    def setUp(self):
        get_cache_provider().clear()

    @patch("smartfetch.core.browser_fetch")
    @patch("smartfetch.core.http_fetch")
    def test_smart_fetch_on_valid_pdf_returns_http_and_never_calls_browser(
        self, mock_http, mock_browser
    ):
        pdf_bytes = make_test_pdf("Comprehensive market research report for enterprise AI.", title="Market Report")
        mock_http.return_value = {
            "raw_bytes": pdf_bytes,
            "final_url": "https://example.com/report.pdf",
            "status_code": 200,
            "content_type": "application/pdf",
            "is_pdf": True,
        }

        # First call: fetched and extracted via Tier 1
        res1 = smart_fetch("https://example.com/report.pdf")
        self.assertTrue(res1["success"])
        self.assertEqual(res1["render_method"], "http")
        self.assertEqual(res1["retrieval_tier"], "http")
        self.assertEqual(res1["content_type"], "application/pdf")
        self.assertEqual(res1["title"], "Market Report")
        self.assertIn("Comprehensive market research", res1["content"])
        self.assertFalse(res1["cache_hit"])
        mock_http.assert_called_once()
        mock_browser.assert_not_called()

        # Second call: hits cache, neither HTTP nor browser is called again
        res2 = smart_fetch("https://example.com/report.pdf")
        self.assertTrue(res2["success"])
        self.assertEqual(res2["retrieval_tier"], "cache")
        self.assertTrue(res2["cache_hit"])
        self.assertEqual(res2["content_type"], "application/pdf")
        self.assertEqual(mock_http.call_count, 1)
        mock_browser.assert_not_called()

    @patch("smartfetch.core.browser_fetch")
    @patch("smartfetch.core.http_fetch")
    def test_smart_fetch_on_blank_or_scanned_pdf_raises_retrieval_failure_without_browser(
        self, mock_http, mock_browser
    ):
        blank_bytes = make_blank_pdf(num_pages=2)
        mock_http.return_value = {
            "raw_bytes": blank_bytes,
            "final_url": "https://example.com/scanned_receipt.pdf",
            "status_code": 200,
            "content_type": "application/pdf",
            "is_pdf": True,
        }

        with self.assertRaises(RetrievalFailure) as ctx:
            smart_fetch("https://example.com/scanned_receipt.pdf")

        diag = ctx.exception.retrieval_diagnostics
        self.assertEqual(diag.failure_code, "invalid_content")
        self.assertEqual(diag.strategy, "http")
        self.assertEqual(diag.phase, "extract")
        # Browser must NEVER have been called!
        mock_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
