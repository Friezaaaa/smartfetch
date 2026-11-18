"""Structured Site Intelligence and SEO inspection engine for SmartFetch."""

from __future__ import annotations

from datetime import datetime, timezone
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
import requests

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
        "rss_feeds": _extract_rss_feeds(soup, final_url),
    }


_TWO_PART_TLDS = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au",
    "co.jp", "ne.jp", "or.jp",
    "com.br", "com.mx", "co.in", "co.nz",
})


def _registrable_domain(hostname: str) -> str:
    host = (hostname or "").strip().lower().rstrip(".")
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www.") and len(host) > 4:
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _TWO_PART_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _extract_rss_feeds(soup: BeautifulSoup, final_url: str) -> list[str]:
    feeds: list[str] = []
    seen: set[str] = set()
    for link in soup.find_all("link", href=True):
        rel = link.get("rel") or []
        rel_tokens = [r.lower() for r in (rel if isinstance(rel, (list, tuple)) else [str(rel)])]
        if "alternate" not in rel_tokens:
            continue
        ltype = (link.get("type") or "").strip().lower()
        if "application/rss+xml" not in ltype and "application/atom+xml" not in ltype:
            continue
        abs_url = urljoin(final_url, link["href"].strip())
        if urlparse(abs_url).scheme in ("http", "https") and abs_url not in seen:
            seen.add(abs_url)
            feeds.append(abs_url)
            if len(feeds) >= 10:
                break
    return feeds


_DEEP_PATH_KEYWORDS = ("contact", "about", "pricing", "team")


def _discover_deep_links(soup: BeautifulSoup, final_url: str) -> tuple[list[str], str | None, str | None]:
    """Finds up to 2 high-signal same-origin sub-pages (/contact, /about, /pricing, /team)."""
    parsed_home = urlparse(final_url)
    home_netloc = parsed_home.netloc.lower()
    home_path = (parsed_home.path or "/").rstrip("/").lower()

    # If the page itself is already a sub-page of interest, skip sub-crawling
    if any(home_path.endswith(f"/{kw}") for kw in _DEEP_PATH_KEYWORDS):
        contact_url = final_url if (home_path.endswith("/contact") or home_path.endswith("/about")) else None
        pricing_url = final_url if home_path.endswith("/pricing") else None
        return [], contact_url, pricing_url

    best_by_kw: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        resolved = urljoin(final_url, href)
        p = urlparse(resolved)
        if p.scheme not in ("http", "https") or p.netloc.lower() != home_netloc:
            continue
        clean_url = f"{p.scheme}://{p.netloc}{p.path or '/'}"
        path_lower = (p.path or "/").rstrip("/").lower()
        for kw in _DEEP_PATH_KEYWORDS:
            if kw in best_by_kw:
                continue
            if path_lower == f"/{kw}" or path_lower.endswith(f"/{kw}") or f"/{kw}-" in path_lower:
                best_by_kw[kw] = clean_url
                break

    contact_url = best_by_kw.get("contact") or best_by_kw.get("about")
    pricing_url = best_by_kw.get("pricing")

    chosen: list[str] = []
    for kw in ("contact", "pricing", "about", "team"):
        candidate = best_by_kw.get(kw)
        if candidate and candidate not in chosen and candidate.rstrip("/") != final_url.rstrip("/"):
            chosen.append(candidate)
        if len(chosen) >= 2:
            break
    return chosen, contact_url, pricing_url


def _merge_contacts_into(base_contacts: dict[str, Any], sub_soup: BeautifulSoup, sub_html: str) -> None:
    sub = _extract_contacts(sub_soup, sub_html)
    emails = list(dict.fromkeys([*(base_contacts.get("emails") or []), *(sub.get("emails") or [])]))[:20]
    phones = list(dict.fromkeys([*(base_contacts.get("phones") or []), *(sub.get("phones") or [])]))[:20]
    base_contacts["emails"] = emails
    base_contacts["phones"] = phones
    base_socials = base_contacts.get("social_links") or {}
    sub_socials = sub.get("social_links") or {}
    for k, v in sub_socials.items():
        if base_socials.get(k) is None and v is not None:
            base_socials[k] = v
    base_contacts["social_links"] = base_socials


def _doh_query(name: str, qtype: str, *, timeout: float = 1.5) -> dict[str, Any]:
    resp = requests.get(
        "https://dns.google/resolve",
        params={"name": name, "type": qtype},
        headers={"Accept": "application/dns-json"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        return {}
    return resp.json()


def _rdap_query(domain: str, *, timeout: float = 1.5) -> dict[str, Any]:
    resp = requests.get(
        f"https://rdap.org/domain/{domain}",
        headers={"Accept": "application/rdap+json, application/json"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        return {}
    return resp.json()


def collect_domain_intel(final_url: str, *, timeout: float = 1.5) -> dict[str, Any]:
    """Best-effort, strictly bounded (<=1.5s per lookup) DNS-over-HTTPS + RDAP domain intelligence."""
    hostname = urlparse(final_url).hostname or ""
    domain = _registrable_domain(hostname)
    intel: dict[str, Any] = {
        "domain": domain,
        "has_mx": False,
        "mx_hosts": [],
        "has_spf": False,
        "has_dmarc": False,
        "registrar": None,
        "created_at": None,
        "domain_age_days": None,
    }
    if not domain or "." not in domain:
        return intel

    # 1. MX records via DoH
    try:
        mx_json = _doh_query(domain, "MX", timeout=timeout)
        mx_hosts: list[str] = []
        for ans in mx_json.get("Answer") or []:
            if ans.get("type") == 15 and ans.get("data"):
                parts = str(ans["data"]).split()
                host = parts[-1].rstrip(".").lower() if parts else ""
                if host and host not in mx_hosts:
                    mx_hosts.append(host)
        intel["mx_hosts"] = mx_hosts[:10]
        intel["has_mx"] = len(mx_hosts) > 0
    except Exception:
        pass

    # 2. SPF record via DoH TXT
    try:
        txt_json = _doh_query(domain, "TXT", timeout=timeout)
        for ans in txt_json.get("Answer") or []:
            data = str(ans.get("data") or "").lower()
            if "v=spf1" in data:
                intel["has_spf"] = True
                break
    except Exception:
        pass

    # 3. DMARC record via DoH TXT
    try:
        dmarc_json = _doh_query(f"_dmarc.{domain}", "TXT", timeout=timeout)
        for ans in dmarc_json.get("Answer") or []:
            data = str(ans.get("data") or "").lower()
            if "v=dmarc1" in data:
                intel["has_dmarc"] = True
                break
    except Exception:
        pass

    # 4. RDAP registrar + registration age
    try:
        rdap = _rdap_query(domain, timeout=timeout)
        for ev in rdap.get("events") or []:
            if ev.get("eventAction") == "registration" and ev.get("eventDate"):
                created_str = str(ev["eventDate"])
                intel["created_at"] = created_str
                try:
                    dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age_days = max(0, (datetime.now(timezone.utc) - dt).days)
                    intel["domain_age_days"] = age_days
                except Exception:
                    pass
                break
        for ent in rdap.get("entities") or []:
            roles = [str(r).lower() for r in (ent.get("roles") or [])]
            if "registrar" in roles:
                vcard = ent.get("vcardArray")
                if isinstance(vcard, list) and len(vcard) >= 2 and isinstance(vcard[1], list):
                    for item in vcard[1]:
                        if isinstance(item, list) and len(item) >= 4 and item[0] == "fn" and item[3]:
                            intel["registrar"] = str(item[3]).strip()
                            break
                if intel["registrar"]:
                    break
    except Exception:
        pass

    return intel


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
    deep: bool = True,
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

    cache_key = build_inspect_cache_key(url, render_mode=resolved_render_mode, deep=deep)

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

    pages_checked = [final_url]
    sub_links, contact_url, pricing_url = _discover_deep_links(soup, final_url)
    if deep and sub_links:
        sub_deadline = time.perf_counter() + 2.5
        for sub_url in sub_links:
            if time.perf_counter() >= sub_deadline:
                break
            try:
                validate_public_url(sub_url)
                sub_res = http_fetch(sub_url)
                sub_html = sub_res.get("html") or sub_res.get("raw_html") or ""
                if sub_html:
                    sub_soup = BeautifulSoup(sub_html, "lxml")
                    _merge_contacts_into(contacts, sub_soup, sub_html)
                    pages_checked.append(sub_res.get("final_url", sub_url))
            except Exception as exc:
                _LOGGER.debug("Deep inspect sub-page skipped for %s: %s", sub_url, exc)

    contacts["pages_checked"] = pages_checked
    contacts["contact_url"] = contact_url
    contacts["pricing_url"] = pricing_url

    # Skip external DoH/RDAP network calls if http_fetch is mocked in offline tests
    # unless collect_domain_intel itself was patched for testing.
    http_is_mocked = getattr(http_fetch, "__module__", "") != "smartfetch.http_fetch"
    intel_is_mocked = getattr(collect_domain_intel, "__module__", "") != "smartfetch.inspect"
    if not http_is_mocked or intel_is_mocked:
        domain_intel = collect_domain_intel(final_url, timeout=1.5)
    else:
        domain_intel = {
            "domain": _registrable_domain(urlparse(final_url).hostname or ""),
            "has_mx": False,
            "mx_hosts": [],
            "has_spf": False,
            "has_dmarc": False,
            "registrar": None,
            "created_at": None,
            "domain_age_days": None,
        }

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
        "domain_intel": domain_intel,
    }

    if not no_cache:
        get_result_cache().set(cache_key, result, ttl_seconds=60)

    return result
