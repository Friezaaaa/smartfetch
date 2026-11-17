"""Structured Site Intelligence and SEO inspection engine for SmartFetch."""

from __future__ import annotations

import io
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import urljoin, urlparse, urlsplit

from bs4 import BeautifulSoup
import pypdf

from .browser_fetch import browser_fetch
from .cache import build_inspect_cache_key, get_result_cache
from .config import SERVICE_VERSION
from .core import NOSCRIPT_JS_PATTERN, SPA_ROOT_PATTERNS
from .diagnostics import (
    RetrievalDiagnostics,
    RetrievalFailure,
    diagnostics_for_exception,
    normalize_target_host,
)
from .http_fetch import http_fetch
from .security import validate_public_url

_LOGGER = logging.getLogger("smartfetch.inspect")

# Regex detectors for contacts
_EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,7}\b",
    re.IGNORECASE,
)
_PHONE_REGEX = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}",
)
_INVALID_EMAIL_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".css", ".js", ".ts",
    ".mp4", ".mp3", ".woff", ".woff2", ".ttf", ".eot",
)

# Social link domain mapping
_SOCIAL_DOMAINS: list[tuple[str, str, tuple[str, ...]]] = [
    ("github", "github.com", ("github.com/login", "github.com/signup", "github.com/features", "github.com/pricing", "github.com/about")),
    ("x_twitter", "x.com", ("x.com/privacy", "x.com/tos")),
    ("x_twitter", "twitter.com", ("twitter.com/privacy", "twitter.com/tos")),
    ("linkedin", "linkedin.com", ("linkedin.com/legal", "linkedin.com/help")),
    ("youtube", "youtube.com", ("youtube.com/t/", "youtube.com/about")),
    ("youtube", "youtu.be", ()),
    ("discord", "discord.gg", ()),
    ("discord", "discord.com", ("discord.com/terms", "discord.com/privacy")),
    ("telegram", "t.me", ()),
    ("telegram", "telegram.me", ()),
    ("instagram", "instagram.com", ("instagram.com/legal", "instagram.com/about")),
    ("facebook", "facebook.com", ("facebook.com/policies", "facebook.com/privacy")),
]


def _extract_social_links(soup: BeautifulSoup) -> dict[str, str | None]:
    socials: dict[str, str | None] = {
        "github": None,
        "x_twitter": None,
        "linkedin": None,
        "youtube": None,
        "discord": None,
        "telegram": None,
        "instagram": None,
        "facebook": None,
    }
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        href_lower = href.lower()
        if not href_lower.startswith(("http://", "https://")):
            continue
        for key, domain, exclusions in _SOCIAL_DOMAINS:
            if socials[key] is not None:
                continue
            if domain in href_lower:
                if any(ex in href_lower for ex in exclusions):
                    continue
                socials[key] = href
                break
    return socials


def _extract_contacts(soup: BeautifulSoup, html: str) -> dict[str, Any]:
    emails = set()
    phones = set()

    # Scan mailto and tel links
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            email_part = href[7:].split("?")[0].strip()
            if email_part and _EMAIL_REGEX.match(email_part):
                if not any(email_part.lower().endswith(ext) for ext in _INVALID_EMAIL_EXTENSIONS):
                    emails.add(email_part.lower())
        elif href.lower().startswith("tel:"):
            phone_part = href[4:].split("?")[0].strip()
            if phone_part and len(phone_part) >= 7:
                phones.add(phone_part)

    # Regex scan page text for email addresses
    page_text = soup.get_text(" ", strip=True)
    for match in _EMAIL_REGEX.findall(page_text):
        match_clean = match.strip().lower()
        if not any(match_clean.endswith(ext) for ext in _INVALID_EMAIL_EXTENSIONS):
            if "." in match_clean.split("@")[-1]:
                emails.add(match_clean)

    # Regex scan page text for phone numbers
    for match in _PHONE_REGEX.findall(page_text):
        match_clean = match.strip()
        digits = [c for c in match_clean if c.isdigit()]
        if len(digits) >= 10:
            phones.add(match_clean)

    return {
        "emails": sorted(list(emails))[:20],
        "phones": sorted(list(phones))[:10],
        "social_links": _extract_social_links(soup),
    }


def _extract_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    json_ld_list: list[dict[str, Any]] = []
    scripts = soup.find_all("script", type=lambda t: t and "application/ld+json" in t.lower())
    for s in scripts:
        raw = s.string or s.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                json_ld_list.append(parsed)
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        json_ld_list.append(item)
                    if len(json_ld_list) >= 10:
                        break
        except Exception:
            # Safely skip malformed JSON-LD
            continue
        if len(json_ld_list) >= 10:
            break
    return json_ld_list[:10]


def _detect_tech_stack(
    soup: BeautifulSoup,
    html: str,
    headers: dict[str, str],
) -> list[str]:
    stack = set()
    html_lower = html.lower()

    # Meta generator checks
    for meta in soup.find_all("meta", attrs={"name": lambda n: n and n.lower() == "generator"}):
        gen = (meta.get("content") or "").lower()
        if "wordpress" in gen:
            stack.add("WordPress")
        if "next.js" in gen or "nextjs" in gen:
            stack.add("Next.js")
        if "astro" in gen:
            stack.add("Astro")
        if "nuxt" in gen:
            stack.add("Nuxt")
        if "svelte" in gen:
            stack.add("Svelte")
        if "shopify" in gen:
            stack.add("Shopify")
        if "webflow" in gen:
            stack.add("Webflow")

    # Script src and DOM markers
    scripts = [s.get("src", "") for s in soup.find_all("script") if s.get("src")]
    script_str = " ".join(scripts).lower()

    if soup.find("script", id="__NEXT_DATA__") or "/_next/" in script_str:
        stack.add("Next.js")
        stack.add("React")
    if soup.find("div", id="__nuxt") or "/_nuxt/" in script_str:
        stack.add("Nuxt")
        stack.add("Vue")
    if "data-astro-" in html_lower or "<astro-island" in html_lower:
        stack.add("Astro")
    if "svelte-" in html_lower or "__svelte__" in html_lower:
        stack.add("Svelte")
    if "react.production.min.js" in script_str or "data-reactroot" in html_lower or soup.find("div", id="root"):
        stack.add("React")
    if "vue.js" in script_str or "data-v-" in html_lower:
        stack.add("Vue")
    if "/wp-content/" in html_lower or "/wp-includes/" in html_lower:
        stack.add("WordPress")
    if "cdn.shopify.com" in html_lower or "shopify.theme" in html_lower:
        stack.add("Shopify")
    if "data-wf-page" in html_lower or "webflow.js" in script_str:
        stack.add("Webflow")
    if "tailwindcss" in html_lower or "tailwind" in script_str:
        stack.add("Tailwind CSS")
    if "bootstrap" in html_lower:
        stack.add("Bootstrap")
    if "jquery" in script_str:
        stack.add("jQuery")
    if "js.stripe.com" in script_str:
        stack.add("Stripe")
    if "google-analytics.com" in script_str or "googletagmanager.com/gtag" in script_str:
        stack.add("Google Analytics")
    if "googletagmanager.com/gtm.js" in script_str:
        stack.add("Google Tag Manager")

    # Server / HTTP response headers
    server_header = headers.get("server", "").lower()
    if "cloudflare" in server_header or "cf-ray" in headers:
        stack.add("Cloudflare")
    if "vercel" in server_header or "x-vercel-id" in headers:
        stack.add("Vercel")
    if "netlify" in server_header or "x-nf-request-id" in headers:
        stack.add("Netlify")
    if "railway" in server_header or any("railway" in k for k in headers):
        stack.add("Railway")

    return sorted(list(stack))


def _extract_security_headers(headers: dict[str, str], final_url: str) -> dict[str, Any]:
    return {
        "https": final_url.lower().startswith("https://"),
        "strict_transport_security": headers.get("strict-transport-security"),
        "content_security_policy": headers.get("content-security-policy"),
        "x_frame_options": headers.get("x-frame-options"),
        "x_content_type_options": headers.get("x-content-type-options"),
        "referrer_policy": headers.get("referrer-policy"),
        "permissions_policy": headers.get("permissions-policy"),
        "server": headers.get("server"),
    }


def _extract_structure(soup: BeautifulSoup, final_url: str) -> dict[str, Any]:
    h1s = [
        re.sub(r"\s+", " ", h.get_text(" ", strip=True))[:200]
        for h in soup.find_all("h1")
        if h.get_text(" ", strip=True)
    ][:20]

    h2s = [
        re.sub(r"\s+", " ", h.get_text(" ", strip=True))[:200]
        for h in soup.find_all("h2")
        if h.get_text(" ", strip=True)
    ][:30]

    final_parsed = urlparse(final_url)
    target_netloc = final_parsed.netloc.lower()

    total_links = 0
    internal_links = 0
    external_links = 0
    pdf_links = 0
    sitemaps = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        total_links += 1
        resolved = urljoin(final_url, href)
        parsed = urlparse(resolved)
        link_netloc = parsed.netloc.lower()

        if link_netloc == target_netloc or not parsed.netloc:
            internal_links += 1
        else:
            external_links += 1

        if parsed.path.lower().endswith(".pdf") or "pdf" in parsed.query.lower():
            pdf_links += 1

        if "sitemap" in parsed.path.lower():
            sitemaps.add(resolved)

    return {
        "h1": h1s,
        "h2": h2s,
        "total_links": total_links,
        "internal_links": internal_links,
        "external_links": external_links,
        "pdf_links": pdf_links,
        "sitemap_links": sorted(list(sitemaps))[:10],
    }


def _extract_metadata(soup: BeautifulSoup, final_url: str) -> dict[str, Any]:
    # Title
    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
    if not title:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
    if not title:
        tw_title = soup.find("meta", attrs={"name": "twitter:title"})
        if tw_title and tw_title.get("content"):
            title = tw_title["content"].strip()

    # Description
    description = ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    if desc_tag and desc_tag.get("content"):
        description = desc_tag["content"].strip()
    if not description:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            description = og_desc["content"].strip()

    # Canonical URL
    canonical_url = ""
    canon_tag = soup.find("link", rel=lambda r: r and "canonical" in r)
    if canon_tag and canon_tag.get("href"):
        canonical_url = urljoin(final_url, canon_tag["href"].strip())

    # Language
    language = ""
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        language = html_tag["lang"].strip()
    if not language:
        meta_lang = soup.find("meta", attrs={"http-equiv": lambda h: h and h.lower() == "content-language"})
        if meta_lang and meta_lang.get("content"):
            language = meta_lang["content"].strip()

    # Charset
    charset = "utf-8"
    meta_charset = soup.find("meta", charset=True)
    if meta_charset and meta_charset.get("charset"):
        charset = meta_charset["charset"].strip()
    else:
        meta_content_type = soup.find("meta", attrs={"http-equiv": lambda h: h and h.lower() == "content-type"})
        if meta_content_type and meta_content_type.get("content"):
            ct = meta_content_type["content"].lower()
            if "charset=" in ct:
                charset = ct.split("charset=")[-1].split(";")[0].strip()

    # Favicon
    favicon_url = ""
    icon_tag = soup.find("link", rel=lambda r: r and any(x in r for x in ("icon", "shortcut icon", "apple-touch-icon")))
    if icon_tag and icon_tag.get("href"):
        favicon_url = urljoin(final_url, icon_tag["href"].strip())
    if not favicon_url:
        favicon_url = urljoin(final_url, "/favicon.ico")

    # Robots meta
    robots_meta = ""
    robots_tag = soup.find("meta", attrs={"name": lambda n: n and n.lower() == "robots"})
    if robots_tag and robots_tag.get("content"):
        robots_meta = robots_tag["content"].strip()

    # OpenGraph
    open_graph: dict[str, str] = {}
    for meta in soup.find_all("meta", property=lambda p: p and p.lower().startswith("og:")):
        prop = meta["property"].strip().lower()
        key = prop[3:] if prop.startswith("og:") else prop
        if meta.get("content"):
            open_graph[key] = meta["content"].strip()

    # Twitter Card
    twitter_card: dict[str, str] = {}
    for meta in soup.find_all("meta", attrs={"name": lambda n: n and n.lower().startswith("twitter:")}):
        name = meta["name"].strip().lower()
        key = name[8:] if name.startswith("twitter:") else name
        if meta.get("content"):
            twitter_card[key] = meta["content"].strip()

    return {
        "title": title[:500],
        "description": description[:1000],
        "canonical_url": canonical_url,
        "language": language,
        "charset": charset,
        "favicon_url": favicon_url,
        "robots_meta": robots_meta,
        "open_graph": open_graph,
        "twitter_card": twitter_card,
    }


def _handle_pdf_inspection(
    raw_bytes: bytes,
    final_url: str,
    status_code: int,
    headers: dict[str, str],
    started_at: float,
) -> dict[str, Any]:
    title = ""
    page_count = 1
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        page_count = len(reader.pages)
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title).strip()
    except Exception:
        pass

    if not title:
        parsed_path = urlparse(final_url).path
        filename = parsed_path.split("/")[-1] if parsed_path else ""
        if filename.lower().endswith(".pdf"):
            filename = filename[:-4]
        title = filename.replace("-", " ").replace("_", " ").strip().title() or "PDF Document"

    return {
        "success": True,
        "requested_url": final_url,
        "final_url": final_url,
        "status_code": status_code,
        "render_method": "http",
        "retrieval_tier": "http",
        "cache_hit": False,
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
        "request_id": secrets.token_hex(8),
        "service_version": SERVICE_VERSION,
        "metadata": {
            "title": title,
            "description": f"PDF Document ({page_count} pages)",
            "canonical_url": final_url,
            "language": "en",
            "charset": "utf-8",
            "favicon_url": "",
            "robots_meta": "",
            "open_graph": {"title": title, "type": "document"},
            "twitter_card": {"title": title},
        },
        "json_ld": [],
        "contacts": {
            "emails": [],
            "phones": [],
            "social_links": {
                "github": None,
                "x_twitter": None,
                "linkedin": None,
                "youtube": None,
                "discord": None,
                "telegram": None,
                "instagram": None,
                "facebook": None,
            },
        },
        "tech_stack": ["PDF"],
        "security_headers": _extract_security_headers(headers, final_url),
        "structure": {
            "h1": [title],
            "h2": [],
            "total_links": 0,
            "internal_links": 0,
            "external_links": 0,
            "pdf_links": 0,
            "sitemap_links": [],
        },
    }


def inspect_webpage_url(
    url: str,
    *,
    force_browser: bool = False,
    render_mode: str = "auto",
    no_cache: bool = False,
) -> dict[str, Any]:
    """Inspects a public webpage and returns structured metadata, tech stack, and security headers."""
    started_at = time.perf_counter()

    # Validate destination URL and enforce SSRF policy
    validate_public_url(url)

    # Normalize render mode
    mode_lower = (render_mode or "auto").lower()
    if force_browser or mode_lower == "browser":
        resolved_render_mode = "browser"
    elif mode_lower in ("http_only", "http"):
        resolved_render_mode = "http_only"
    else:
        resolved_render_mode = "auto"

    cache_key = build_inspect_cache_key(url, render_mode=resolved_render_mode)

    # 1. Cache lookup
    if not no_cache:
        cached = get_result_cache().get(cache_key)
        if cached is not None and cached.get("success"):
            cached_result = dict(cached)
            cached_result["cache_hit"] = True
            cached_result["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000)
            return cached_result

    html = ""
    final_url = url
    status_code = 200
    headers: dict[str, str] = {}
    render_method = "http"
    retrieval_tier = "http"

    # 2. Forced Browser Mode
    if resolved_render_mode == "browser":
        try:
            browser_result = browser_fetch(url)
            html = browser_result.get("html") or browser_result.get("raw_html") or ""
            final_url = browser_result.get("final_url", url)
            status_code = browser_result.get("status_code", 200)
            render_method = "browser"
            retrieval_tier = "browser"
        except Exception as exc:
            diag = diagnostics_for_exception(exc) or RetrievalDiagnostics(
                target_host=normalize_target_host(url),
                strategy="browser",
                phase="browser_navigate",
                failure_code="browser_failure",
                http_attempted=False,
                http_retry_attempted=False,
                browser_attempted=True,
                fallback_attempted=False,
            )
            raise RetrievalFailure(f"Browser inspection failed: {exc}", diag) from exc

    # 3. HTTP Tier with potential Browser Escalation
    else:
        try:
            http_result = http_fetch(url)
        except Exception as exc:
            # Let diagnostics-bearing failures propagate so they map to 502 without settling
            diag = diagnostics_for_exception(exc) or RetrievalDiagnostics(
                target_host=normalize_target_host(url),
                strategy="http",
                phase="response",
                failure_code="invalid_content",
                http_attempted=True,
                http_retry_attempted=False,
                browser_attempted=False,
                fallback_attempted=False,
            )
            raise RetrievalFailure(f"HTTP inspection failed: {exc}", diag) from exc

        headers = http_result.get("headers", {})
        final_url = http_result.get("final_url", url)
        status_code = http_result.get("status_code", 200)

        # Handle PDF responses without escalating to browser
        if http_result.get("is_pdf") or http_result.get("content_type") == "application/pdf":
            raw_bytes = http_result.get("raw_bytes", b"")
            pdf_result = _handle_pdf_inspection(raw_bytes, final_url, status_code, headers, started_at)
            if not no_cache:
                get_result_cache().set(cache_key, pdf_result, ttl_seconds=60)
            return pdf_result

        html = http_result.get("html") or http_result.get("raw_html") or ""

        # Check escalation heuristics if in auto mode
        if resolved_render_mode == "auto":
            needs_escalation = False
            for pattern in SPA_ROOT_PATTERNS:
                if pattern.search(html):
                    needs_escalation = True
                    break
            if not needs_escalation and NOSCRIPT_JS_PATTERN.search(html):
                needs_escalation = True
            if not needs_escalation and len(html.strip()) < 150 and "<script" in html.lower():
                needs_escalation = True

            if needs_escalation:
                try:
                    browser_res = browser_fetch(url)
                    html = browser_res.get("html") or browser_res.get("raw_html") or html
                    final_url = browser_res.get("final_url", final_url)
                    status_code = browser_res.get("status_code", status_code)
                    render_method = "browser"
                    retrieval_tier = "browser"
                except Exception as b_exc:
                    _LOGGER.warning("Escalation to browser failed for %s, retaining HTTP HTML: %s", url, b_exc)

    # 4. Deterministic HTML Parsing with BeautifulSoup (lxml)
    soup = BeautifulSoup(html, "lxml")

    metadata = _extract_metadata(soup, final_url)
    json_ld = _extract_json_ld(soup)
    contacts = _extract_contacts(soup, html)
    tech_stack = _detect_tech_stack(soup, html, headers)
    security_headers = _extract_security_headers(headers, final_url)
    structure = _extract_structure(soup, final_url)

    result = {
        "success": True,
        "requested_url": url,
        "final_url": final_url,
        "status_code": status_code,
        "render_method": render_method,
        "retrieval_tier": retrieval_tier,
        "cache_hit": False,
        "elapsed_ms": round((time.perf_counter() - started_at) * 1000),
        "request_id": secrets.token_hex(8),
        "service_version": SERVICE_VERSION,
        "metadata": metadata,
        "json_ld": json_ld,
        "contacts": contacts,
        "tech_stack": tech_stack,
        "security_headers": security_headers,
        "structure": structure,
    }

    if not no_cache:
        get_result_cache().set(cache_key, result, ttl_seconds=60)

    return result
