import hashlib
import io
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import MarkdownConverter
import pypdf

NOISE_TAGS = ['script', 'style', 'noscript', 'iframe', 'svg', 'canvas', 'template', 'form']
ALWAYS_NOISE_SELECTORS = [
    '[hidden]',
    '[aria-hidden="true"]',
    '[style*="display:none" i]',
    '[style*="display: none" i]',
    '[style*="visibility:hidden" i]',
    '[style*="visibility: hidden" i]',
    '#onetrust-banner-sdk',
    '.onetrust-pc-dark-filter',
    '#CybotCookiebotDialog',
    '.cc-window',
    '[class*="cookie-banner" i]',
    '[class*="cookie-consent" i]',
    '[id*="cookie-banner" i]',
    '[id*="cookie-consent" i]',
    '[aria-label*="cookie" i]',
    '.cookie',
    '.cookies',
    '.cookie-banner',
    '.consent',
    '[class*="newsletter" i]',
    '[id*="newsletter" i]',
    '.advertisement',
    '.advertisement-container',
    '.ads',
    '.ad-banner',
    '.social-share',
    '.share-buttons',
]
MAIN_ONLY_NOISE_SELECTORS = [
    'nav',
    'footer',
    'aside',
]

_LANG_CLASS_PATTERNS = (
    re.compile(r'(?:^|\s)(?:language|lang)-([a-zA-Z0-9_+#.-]+)', re.IGNORECASE),
    re.compile(r'(?:^|\s)highlight-source-([a-zA-Z0-9_+#.-]+)', re.IGNORECASE),
    re.compile(r'brush:\s*([a-zA-Z0-9_+#.-]+)', re.IGNORECASE),
)


def _norm(text: str) -> str:
    text = text.replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _norm_markdown(md: str) -> str:
    """Normalizes Markdown whitespace while preserving indentation inside fenced code blocks."""
    md = md.replace('\xa0', ' ')
    parts = re.split(r'(^```[^\n]*\n.*?^```|^````[^\n]*\n.*?^````)', md, flags=re.MULTILINE | re.DOTALL)
    out: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            out.append(part.strip('\r\n'))
        else:
            cleaned = re.sub(r' *\n *', '\n', part)
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
            out.append(cleaned)
    return re.sub(r'\n{3,}', '\n\n', ''.join(out)).strip()


def _extract_code_language(el) -> str:
    """Extracts programming language hint from <pre>, child <code>, or parent wrapper."""
    candidates = [el]
    code_child = el.find('code') if hasattr(el, 'find') else None
    if code_child is not None:
        candidates.insert(0, code_child)
    if getattr(el, 'parent', None) is not None:
        candidates.append(el.parent)

    for node in candidates:
        for attr in ('data-language', 'data-lang', 'lang'):
            val = node.get(attr) if hasattr(node, 'get') else None
            if isinstance(val, str) and val.strip():
                return val.strip().lower()
        classes = node.get('class') if hasattr(node, 'get') else None
        if isinstance(classes, (list, tuple)):
            class_str = ' '.join(str(c) for c in classes)
        elif isinstance(classes, str):
            class_str = classes
        else:
            class_str = ''
        if class_str:
            for pattern in _LANG_CLASS_PATTERNS:
                m = pattern.search(class_str)
                if m:
                    return m.group(1).strip().lower()
    return ''


def _parse_int_attr(val) -> int | None:
    if val is None:
        return None
    s = str(val).strip().lower().removesuffix('px')
    if s.isdigit():
        return int(s)
    return None


class SmartMarkdownConverter(MarkdownConverter):
    """LLM-clean Markdown converter with language-tagged code fences, GFM tables, and absolute images."""

    def __init__(self, *, page_url: str = '', **options):
        self.page_url = page_url
        options.setdefault('heading_style', 'ATX')
        options.setdefault('bullets', '-')
        super().__init__(**options)

    def convert_pre(self, el, text, parent_tags):
        lang = _extract_code_language(el)
        code_el = el.find('code') if hasattr(el, 'find') else None
        target = code_el if code_el is not None else el
        # Preserve <br> as newlines before extracting flattened text from highlight <span>s
        for br in target.find_all('br'):
            br.replace_with('\n')
        raw_code = target.get_text()
        code = raw_code.strip('\r\n')
        if not code:
            return ''
        fence = '````' if '```' in code else '```'
        return f'\n\n{fence}{lang}\n{code}\n{fence}\n\n'

    def _unwrap_table(self, el) -> str:
        """Converts cells of a layout table without pipe-table formatting."""
        parts: list[str] = []
        for cell in el.find_all(['th', 'td']):
            if cell.find_parent('table') is not el:
                continue
            inner_html = ''.join(str(child) for child in cell.contents)
            if '<' in inner_html:
                cell_md = self.convert(inner_html).strip()
            else:
                cell_md = cell.get_text(' ', strip=True)
            if cell_md:
                parts.append(cell_md)
        return '\n\n' + '\n\n'.join(parts) + '\n\n' if parts else ''

    def convert_table(self, el, text, parent_tags):
        role = (el.get('role') or '').strip().lower()
        if role in ('presentation', 'none'):
            return self._unwrap_table(el)

        # Collect direct rows of this table (avoiding nested sub-tables)
        rows = []
        for tr in el.find_all('tr'):
            if tr.find_parent('table') is not el:
                continue
            cells = [c for c in tr.find_all(['th', 'td'], recursive=False)]
            if not cells:
                continue
            rows.append(cells)

        if not rows:
            return ''

        # Unwrap single-cell layout tables or tables wrapping nested tables
        total_cells = sum(len(r) for r in rows)
        if (len(rows) == 1 and total_cells == 1) or el.find('table') is not None:
            return self._unwrap_table(el)

        parsed_rows: list[list[str]] = []
        for cells in rows:
            row_vals: list[str] = []
            for cell in cells:
                inner_html = ''.join(str(child) for child in cell.contents)
                if '<' in inner_html:
                    cell_md = self.convert(inner_html)
                else:
                    cell_md = cell.get_text(' ', strip=True)
                cell_clean = re.sub(r'\s+', ' ', cell_md).strip().replace('|', '\\|')
                row_vals.append(cell_clean)
            if any(v for v in row_vals):
                parsed_rows.append(row_vals)

        if not parsed_rows:
            return ''

        col_count = max(len(r) for r in parsed_rows)
        if col_count == 1 and len(parsed_rows) == 1:
            return f'\n\n{parsed_rows[0][0]}\n\n'

        normalized = [r + [''] * (col_count - len(r)) for r in parsed_rows]
        header = normalized[0]
        sep = ['---'] * col_count
        lines = [
            '| ' + ' | '.join(header) + ' |',
            '| ' + ' | '.join(sep) + ' |',
        ]
        for body_row in normalized[1:]:
            lines.append('| ' + ' | '.join(body_row) + ' |')
        return '\n\n' + '\n'.join(lines) + '\n\n'

    def convert_a(self, el, text, parent_tags):
        href = (el.get('href') or '').strip()
        if href and self.page_url and not href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
            abs_href = urljoin(self.page_url, href)
            if urlparse(abs_href).scheme in ('http', 'https'):
                el['href'] = abs_href
        return super().convert_a(el, text, parent_tags)

    def convert_img(self, el, text, parent_tags):
        raw_src = (el.get('src') or el.get('data-src') or '').strip()
        if not raw_src:
            return ''
        lower_src = raw_src.lower()
        if lower_src.startswith(('data:image/', 'data:', 'javascript:', 'about:')):
            return ''

        w = _parse_int_attr(el.get('width'))
        h = _parse_int_attr(el.get('height'))
        if (w is not None and w <= 2) or (h is not None and h <= 2):
            return ''

        abs_url = urljoin(self.page_url, raw_src) if self.page_url else raw_src
        parsed = urlparse(abs_url)
        if parsed.scheme not in ('http', 'https'):
            return ''

        alt = re.sub(r'\s+', ' ', (el.get('alt') or '').strip())
        return f'![{alt}]({abs_url})'


def _link_density(node) -> float:
    text = _norm(node.get_text(' ', strip=True))
    if not text:
        return 1.0
    link_text = ' '.join(a.get_text(' ', strip=True) for a in node.find_all('a'))
    return min(1.0, len(link_text) / max(1, len(text)))


def _score(node) -> float:
    text = _norm(node.get_text(' ', strip=True))
    if not text:
        return -1
    paragraphs = node.find_all('p')
    headings = node.find_all(['h1', 'h2', 'h3'])
    punctuation = sum(text.count(ch) for ch in '.!?')
    density_penalty = _link_density(node) * 700
    return len(text) + len(paragraphs) * 90 + len(headings) * 35 + punctuation * 4 - density_penalty


def _choose_main(soup):
    preferred = soup.select('article, main, [role="main"]')
    candidates = preferred[:] if preferred else []

    # Preserve multiple non-nested sibling <article> blocks when no single <main> wraps them
    if len(candidates) > 1 and not any(c.name == 'main' or c.get('role') == 'main' for c in candidates):
        top_articles = [
            c for c in candidates
            if not any(other is not c and other in c.parents for other in candidates)
        ]
        if len(top_articles) > 1:
            wrapper = soup.new_tag('div')
            for art in top_articles:
                wrapper.append(art)
            return wrapper

    if not candidates:
        body = soup.body or soup
        # Limit generic candidates to substantial blocks so giant nav wrappers do not dominate.
        for node in body.find_all(['section', 'div'], recursive=True):
            text = _norm(node.get_text(' ', strip=True))
            if len(text) >= 180 and len(node.find_all('p')) >= 1:
                candidates.append(node)
        candidates.append(body)

    return max(candidates, key=_score) if candidates else (soup.body or soup)


def _safe_decompose_matching(soup, selectors: list[str]) -> None:
    """Decomposes noise elements without ever destroying <article>, <main>, <body>, or their wrappers."""
    for selector in selectors:
        for tag in list(soup.select(selector)):
            if getattr(tag, 'attrs', None) is None:
                continue
            if tag.name in ('article', 'main', 'body', 'html'):
                continue
            if tag.find(['article', 'main']) is not None:
                continue
            tag.decompose()


def extract_content(html: str, page_url: str, *, only_main_content: bool = True) -> dict:
    original = BeautifulSoup(html, 'lxml')

    links = []
    seen = set()
    for a in original.find_all('a', href=True):
        try:
            href = urljoin(page_url, a['href'])
            p = urlparse(href)
            if p.scheme not in {'http', 'https'} or href in seen:
                continue
            seen.add(href)
            links.append({'href': href, 'text': _norm(a.get_text(' ', strip=True))[:200]})
            if len(links) >= 100:
                break
        except Exception:
            pass

    soup = BeautifulSoup(html, 'lxml')
    for tag in list(soup.find_all(NOISE_TAGS)):
        if getattr(tag, 'attrs', None) is None:
            continue
        if tag.find(['article', 'main']) is not None:
            continue
        tag.decompose()

    _safe_decompose_matching(soup, ALWAYS_NOISE_SELECTORS)
    if only_main_content:
        _safe_decompose_matching(soup, MAIN_ONLY_NOISE_SELECTORS)
        main = _choose_main(soup)
    else:
        main = soup.body or soup

    title = ''
    h1 = main.find('h1') if hasattr(main, 'find') else None
    if h1:
        title = _norm(h1.get_text(' ', strip=True))
    if not title and original.title:
        title = _norm(original.title.get_text(' ', strip=True))

    text = _norm(main.get_text('\n', strip=True))
    converter = SmartMarkdownConverter(page_url=page_url)
    markdown = _norm_markdown(converter.convert(str(main)))

    lower = text.lower()
    js_signals = ['enable javascript', 'javascript is required', 'please turn javascript on', 'loading...']
    # Short pages can still be perfectly valid (example.com is a good case).
    # Fall back only when content is truly tiny or looks like a JS placeholder.
    low_quality = len(text) < 80 or any(sig in lower for sig in js_signals if len(text) < 800)

    return {
        'title': title,
        'content': text,
        'markdown': markdown,
        'links': links,
        'word_count': len(text.split()),
        'content_hash': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'low_quality': low_quality,
    }


def extract_pdf(
    raw_bytes: bytes,
    page_url: str,
    *,
    max_pages: int = 50,
    max_extracted_chars: int = 100_000,
) -> dict:
    """Extracts text, metadata, and structured Markdown from raw PDF bytes."""
    if not isinstance(raw_bytes, (bytes, bytearray)) or not raw_bytes.startswith(b'%PDF-'):
        raise ValueError("Invalid PDF data: missing %PDF- header")

    reader = pypdf.PdfReader(io.BytesIO(raw_bytes), strict=False)

    if reader.is_encrypted:
        decrypted = False
        try:
            # Attempt empty-password decrypt for unauthenticated read
            res = reader.decrypt("")
            if res:
                decrypted = True
        except Exception:
            decrypted = False
        if not decrypted:
            raise ValueError("PDF is password-protected and cannot be decrypted without a password")

    page_count = len(reader.pages)
    pages_to_extract = min(page_count, max_pages)

    markdown_parts = []
    content_parts = []
    total_chars = 0
    pages_extracted = 0

    for idx in range(pages_to_extract):
        page = reader.pages[idx]
        page_text = page.extract_text() or ""
        page_norm = _norm(page_text)
        if page_norm:
            markdown_parts.append(f"## Page {idx + 1}\n\n{page_norm}")
            content_parts.append(page_norm)
            total_chars += len(page_norm)
        pages_extracted += 1
        if total_chars >= max_extracted_chars:
            break

    content = "\n\n".join(content_parts).strip()
    markdown = "\n\n".join(markdown_parts).strip()

    title = ""
    if reader.metadata and reader.metadata.title:
        title = _norm(str(reader.metadata.title))
    if not title:
        parsed_path = urlparse(page_url).path
        filename = parsed_path.split("/")[-1] if parsed_path else ""
        if filename:
            if filename.lower().endswith(".pdf"):
                filename = filename[:-4]
            title = filename.replace("-", " ").replace("_", " ").strip().title()

    low_quality = len(content) < 20

    return {
        'title': title,
        'content': content,
        'markdown': markdown,
        'links': [],
        'word_count': len(content.split()),
        'content_hash': hashlib.sha256(content.encode('utf-8')).hexdigest(),
        'low_quality': low_quality,
        'page_count': page_count,
        'pages_extracted': pages_extracted,
    }


def extract_plain_document(raw_text: str, page_url: str, content_type: str) -> dict:
    """Extracts clean Markdown and plain text from structured text documents (.md, .json, .csv, .xml)."""
    text = raw_text or ""
    ct = (content_type or "").lower()

    # 1. Derive fallback title from URL filename or domain
    parsed_path = urlparse(page_url).path
    filename = parsed_path.split("/")[-1] if parsed_path else ""
    if filename:
        title = filename.strip()
    else:
        title = urlparse(page_url).netloc or "Document"

    # 2. Pretty-formatting or fencing for markdown
    if "json" in ct or page_url.lower().endswith(".json"):
        try:
            parsed_json = json.loads(text)
            formatted = json.dumps(parsed_json, indent=2, ensure_ascii=False)
            markdown = f"```json\n{formatted}\n```"
        except Exception:
            markdown = f"```json\n{text.strip()}\n```"
        content = text.strip()
    elif "markdown" in ct or page_url.lower().endswith((".md", ".markdown")):
        markdown = text.strip()
        content = text.strip()
    elif "csv" in ct or page_url.lower().endswith(".csv"):
        markdown = f"```csv\n{text.strip()}\n```"
        content = text.strip()
    elif "xml" in ct or page_url.lower().endswith((".xml", ".rss", ".atom")):
        markdown = f"```xml\n{text.strip()}\n```"
        content = text.strip()
    else:
        markdown = text.strip()
        content = text.strip()

    low_quality = len(text.strip()) == 0

    return {
        'title': title,
        'content': content,
        'markdown': markdown,
        'links': [],
        'word_count': len(content.split()),
        'content_hash': hashlib.sha256(content.encode('utf-8')).hexdigest(),
        'low_quality': low_quality,
    }

