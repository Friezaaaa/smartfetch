# SmartFetch — Google Maps B2B Leads, Site Intel, Stealth Web Reader & Standby MCP

SmartFetch is an all-in-one **B2B Lead Enrichment**, **Site Intelligence**, **Stealth Web/PDF-to-Markdown**, **Screenshot**, and **Standby MCP Server** Actor built for AI agents, growth teams, and automated workflows.

## Key Capabilities

### 1. Google Maps B2B Lead, Email & Tech-Stack Scraper (`search_leads`)
Search **Google Maps Places** or **Web** for any business niche and location (e.g., `"dentists in Austin TX"`) and automatically enrich every company website in parallel:
- **Verified Contacts**: Emails, phone numbers (merged from Google Maps + website), `/contact`, `/about`, and `/pricing` links
- **Google Maps Pin Data**: Business name, category, star rating, review count, street address, and Maps phone
- **Social Links**: LinkedIn, GitHub, X/Twitter
- **Email & Domain Intel**: DNS `MX` (Google Workspace / Microsoft 365), `SPF`, `DMARC`, and RDAP domain age
- **30+ Tech Stack Signatures**: Next.js, React, Shopify, WordPress, Webflow, Stripe, HubSpot, Cloudflare, and more
- **Ready-to-Use Exports**: Clean Dataset rows + **`LEADS.csv`** in Key-Value Store

### 2. Bulk B2B Domain & Tech-Stack Enrichment (`bulk_inspect`)
Supply up to **50 bare domains or URLs** (e.g., `["stripe.com", "linear.app"]`) and enrich them concurrently into flat, CSV/Excel-ready rows with contacts, social links, security headers (`HTTPS`, `HSTS`, `CSP`), DNS mail records, RDAP age, and tech stack.

### 3. Stealth Web Scraper, PDF-to-Markdown, Screenshots & URL Mapper
- **`fetch`**: Chrome TLS-impersonated HTTP -> PDF extraction -> headless Chromium fallback returning LLM-clean Markdown and plain text
- **`screenshot`**: High-resolution desktop (`1280x800`) or mobile (`390x844`) JPEG/PNG screenshots with optional full-page capture
- **`map`**: Fast `robots.txt`, XML sitemap (`<sitemapindex>`), and internal link discovery (up to 500 URLs)
- **`batch_fetch`**: Parallel retrieval of up to 5 URLs

### 4. Standby MCP Server (`/mcp`)
Connect any MCP client (Claude Desktop, Cursor, custom agents) to `https://smartfetch-dev--smartfetch.apify.actor/mcp` with `Authorization: Bearer <APIFY_API_TOKEN>`.

## Pay-Per-Event (PPE) Pricing
- `actor-start`: `$0.00` (no startup fee)
- `page-fetched`: **`$0.01`** per webpage, PDF, or site map (`fetch`, `batch_fetch`, `map`)
- `site-inspected`: **`$0.01`** per enriched lead or site inspection (`search_leads`, `inspect`, `bulk_inspect`)
- `screenshot-captured`: **`$0.02`** per headless Chromium screenshot (`screenshot`)
- **Zero charge on errors or unconfigured requests.**
