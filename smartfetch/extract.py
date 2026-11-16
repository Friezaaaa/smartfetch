import hashlib
import io
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify as to_markdown
import pypdf

NOISE_TAGS = ['script','style','noscript','iframe','svg','canvas','template','form']
NOISE_SELECTORS = [
    'nav','footer','aside',
    '[aria-hidden="true"]',
    '.cookie','.cookies','.cookie-banner','.consent',
    '.advertisement','.advertisement-container','.ads','.ad-banner',
    '.social-share','.share-buttons'
]


def _norm(text: str) -> str:
    text = text.replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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
    headings = node.find_all(['h1','h2','h3'])
    punctuation = sum(text.count(ch) for ch in '.!?')
    density_penalty = _link_density(node) * 700
    return len(text) + len(paragraphs) * 90 + len(headings) * 35 + punctuation * 4 - density_penalty


def _choose_main(soup):
    preferred = soup.select('article, main, [role="main"]')
    candidates = preferred[:] if preferred else []

    if not candidates:
        body = soup.body or soup
        # Limit generic candidates to substantial blocks so giant nav wrappers do not dominate.
        for node in body.find_all(['section', 'div'], recursive=True):
            text = _norm(node.get_text(' ', strip=True))
            if len(text) >= 180 and len(node.find_all('p')) >= 1:
                candidates.append(node)
        candidates.append(body)

    return max(candidates, key=_score) if candidates else (soup.body or soup)


def extract_content(html: str, page_url: str) -> dict:
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
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    for selector in NOISE_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()

    main = _choose_main(soup)
    title = ''
    h1 = main.find('h1') if hasattr(main, 'find') else None
    if h1:
        title = _norm(h1.get_text(' ', strip=True))
    if not title and original.title:
        title = _norm(original.title.get_text(' ', strip=True))

    text = _norm(main.get_text('\n', strip=True))
    markdown = _norm(to_markdown(str(main), heading_style='ATX', bullets='-'))

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
