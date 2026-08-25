from .config import DEFAULT_MAX_OUTPUT_CHARS, MAX_LINKS, MAX_OUTPUT_CHARS


def normalize_max_chars(value) -> int:
    if value is None:
        return DEFAULT_MAX_OUTPUT_CHARS
    if isinstance(value, bool):
        raise ValueError('max_chars must be an integer')
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('max_chars must be an integer') from exc
    if value < 1000:
        raise ValueError('max_chars must be at least 1000')
    return min(value, MAX_OUTPUT_CHARS)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    # Prefer a clean boundary rather than cutting in the middle of a word.
    cut = text[:limit]
    boundary = max(cut.rfind('\n\n'), cut.rfind('. '), cut.rfind('\n'), cut.rfind(' '))
    if boundary >= int(limit * 0.75):
        cut = cut[:boundary + (1 if cut[boundary:boundary + 1] == '.' else 0)]
    return cut.rstrip() + '\n\n[truncated]', True


def shape_output(result: dict, max_chars: int) -> dict:
    full_content = result.get('content', '')
    full_markdown = result.get('markdown', '')
    content, c_truncated = _truncate(full_content, max_chars)
    markdown, m_truncated = _truncate(full_markdown, max_chars)
    links = result.get('links', [])[:MAX_LINKS]

    shaped = dict(result)
    shaped['content'] = content
    shaped['markdown'] = markdown
    shaped['links'] = links
    shaped['truncated'] = bool(c_truncated or m_truncated or len(result.get('links', [])) > MAX_LINKS)
    shaped['original_content_chars'] = len(full_content)
    shaped['original_markdown_chars'] = len(full_markdown)
    shaped['returned_content_chars'] = len(content)
    shaped['returned_markdown_chars'] = len(markdown)
    shaped['links_returned'] = len(links)
    shaped['max_chars'] = max_chars
    return shaped
