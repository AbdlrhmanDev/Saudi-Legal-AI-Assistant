"""Text extraction and article-aware structure parsing (FR-02).

Headings recognised: "المادة الأولى", "المادة الحادية والعشرون بعد المائة",
"المادة (5)", "المادة ٥:", "مادة 5 مكرر", with chapter lines "الباب ..." / "الفصل ...".
A line only counts as a heading when the ordinal parses and is followed by
end-of-line or a separator, so in-body references ("المادة الخامسة من هذا النظام")
do not split an article.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlsplit

from legal_core.normalize import normalize_ar

_UNITS = {
    "اول": 1, "اولي": 1, "حادي": 1, "حاديه": 1, "ثاني": 2, "ثانيه": 2, "ثالث": 3, "ثالثه": 3,
    "رابع": 4, "رابعه": 4, "خامس": 5, "خامسه": 5, "سادس": 6, "سادسه": 6, "سابع": 7, "سابعه": 7,
    "ثامن": 8, "ثامنه": 8, "تاسع": 9, "تاسعه": 9, "عاشر": 10, "عاشره": 10,
}  # fmt: skip
_TEEN = {"عشر", "عشره"}
_TENS = {
    "عشرون": 20, "عشرين": 20, "ثلاثون": 30, "ثلاثين": 30, "اربعون": 40, "اربعين": 40,
    "خمسون": 50, "خمسين": 50, "ستون": 60, "ستين": 60, "سبعون": 70, "سبعين": 70,
    "ثمانون": 80, "ثمانين": 80, "تسعون": 90, "تسعين": 90,
}  # fmt: skip
_HUNDREDS = {
    "مايه": 100, "ميه": 100, "مايتان": 200, "مايتين": 200, "ميتان": 200, "ميتين": 200,
    "ثلاثمايه": 300, "ثلاثميه": 300, "اربعمايه": 400, "اربعميه": 400,
}  # fmt: skip

_DIGITS = str.maketrans(
    {**{chr(0x0660 + i): str(i) for i in range(10)}, **{chr(0x06F0 + i): str(i) for i in range(10)}}
)
_HEADING = re.compile(r"^\s*(?:ال)?ماد[ةه]\s*(?P<rest>.+?)\s*$")
_NUMERIC = re.compile(r"^[\(\[]?\s*(?P<num>\d{1,4})\s*[\)\]]?(?P<tail>.*)$")
_SEPARATOR_CHARS = ":：-–—."
_SEPARATOR = re.compile(r"^\s*[:：\-–—.]\s*")
_CHAPTER = re.compile(r"^\s*(?:الباب|الفصل|القسم)\s+\S+.{0,120}$")
_MUKARRAR = re.compile(r"^\s*مكرر[ةه]?\b")
_BLANKS = re.compile(r"\n{3,}")
_PAGE_CHROME_LINES = frozenset(
    {
        "الرئيسية",
        "القرارات",
        "الديوان الملكي",
        "قرارات مجلس الوزراء",
        "قرارات وزارية",
        "أوامر ملكية",
        "هيئة الخبراء بمجلس الوزراء",
        "لوائح وأنظمة",
        "مراسيم ملكية",
        "هيئات",
        "خريطة الموقع",
        "تابعنا على",
        "اشترك في نشرتنا الإخبارية",
        "Facebook",
        "X",
        "WhatsApp",
        "نسخ الرابط",
        "طباعة",
        "تم نسخ الرابط",
        "تحميل...",
        "نسخة تجريبية",
        "مكة المكرمة",
    }
)

INJECTION_PATTERNS = re.compile(
    r"تجاهل\s+(?:جميع\s+|كل\s+)?التعليمات|انس\s+التعليمات|ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions"
    r"|system\s+prompt|تعليمات\s+النظام|أنت\s+الآن\s+مساعد",
    re.IGNORECASE,
)


def _key(token: str) -> str:
    token = normalize_ar(token)
    return token[2:] if token.startswith("ال") else token


def parse_ordinal(words: list[str]) -> tuple[int | None, int]:
    """Parse an Arabic ordinal from the start of `words`. Returns (value, words_consumed)."""
    keys = [_key(w) for w in words[:6]]
    if not keys:
        return None, 0
    value: int | None = None
    used = 0
    first = keys[0]
    if first in _UNITS:
        unit = _UNITS[first]
        nxt = keys[1] if len(keys) > 1 else ""
        if nxt in _TEEN:
            value, used = 10 + unit, 2
        elif nxt.startswith("و") and _key(nxt[1:]) in _TENS:
            value, used = _TENS[_key(nxt[1:])] + unit, 2
        elif first in ("حادي", "حاديه"):
            return None, 0  # "الحادي" never stands alone
        else:
            value, used = unit, 1
    elif first in _TENS:
        value, used = _TENS[first], 1
    elif first in _HUNDREDS:
        return _HUNDREDS[first], 1
    if value is None:
        return None, 0
    if len(keys) > used + 1 and keys[used] == "بعد" and keys[used + 1] in _HUNDREDS:
        value += _HUNDREDS[keys[used + 1]]
        used += 2
    return value, used


@dataclass
class ArticleHeading:
    number: str
    label: str
    inline_body: str


def match_article_heading(line: str) -> ArticleHeading | None:
    m = _HEADING.match(line)
    if not m:
        return None
    rest = m.group("rest")
    num = _NUMERIC.match(rest.translate(_DIGITS))  # translate is length-preserving
    separated = False
    if num:
        number = num.group("num").lstrip("0") or "0"
        tail = rest[num.start("tail") :]
    else:
        words = rest.split()
        # A separator glued to a word ("والسبعون:") ends the ordinal there.
        cut = next((i for i, w in enumerate(words) if w[-1] in _SEPARATOR_CHARS), None)
        value, used = parse_ordinal(words if cut is None else words[: cut + 1])
        if value is None:
            return None
        number, tail = str(value), " ".join(words[used:])
        separated = cut is not None and used == cut + 1
    tail = tail.strip()
    mk = _MUKARRAR.match(tail)
    if mk:
        number += " مكرر"
        tail = tail[mk.end() :].strip()
    if tail and not separated:
        sep = _SEPARATOR.match(tail)
        if not sep:
            return None  # "المادة الخامسة من هذا النظام ..." is body text, not a heading
        tail = tail[sep.end() :].strip()
    label = line.strip()
    if tail and label.endswith(tail):
        label = label[: -len(tail)]
    label = label.strip().rstrip(":：-–—. ").strip()
    return ArticleHeading(number=number, label=label, inline_body=tail)


def _is_chapter_heading(line: str) -> bool:
    if len(line) > 120 or not _CHAPTER.match(line):
        return False
    words = line.split()[1:]
    return parse_ordinal(words)[0] is not None or bool(_NUMERIC.match(words[0].translate(_DIGITS)))


@dataclass
class ParsedArticle:
    article_number: str | None
    article_label: str | None
    chapter: str | None
    text: str
    ordering: int


@dataclass
class ParsedDocument:
    title: str | None
    articles: list[ParsedArticle] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("‏", "").replace("‎", "")
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def strip_page_chrome(text: str) -> str:
    """Remove standalone navigation/footer labels without rewriting legal prose."""
    lines = []
    for line in clean_text(text).splitlines():
        value = line.strip()
        if not value:
            continue
        if value in _PAGE_CHROME_LINES:
            continue
        if value.startswith(("جميع الحقوق محفوظة", "تاريخ آخر تعديل:")):
            continue
        if re.fullmatch(r"-+°C", value):
            continue
        lines.append(value)
    return clean_text("\n".join(lines))


def parse_legal_text(text: str) -> ParsedDocument:
    text = clean_text(text)
    lines = text.split("\n")
    title = next((ln for ln in lines if ln), None)
    doc = ParsedDocument(title=title)

    chapter: str | None = None
    current: ArticleHeading | None = None
    current_chapter: str | None = None
    body: list[str] = []
    preamble: list[str] = []

    def flush() -> None:
        if current is None:
            return
        content = "\n".join(body).strip()
        if not content:
            doc.warnings.append(f"empty_article:{current.number}")
        doc.articles.append(
            ParsedArticle(current.number, current.label, current_chapter, content, len(doc.articles))
        )

    for line in lines:
        heading = match_article_heading(line) if line else None
        if heading:
            flush()
            current, current_chapter = heading, chapter
            body = [heading.inline_body] if heading.inline_body else []
        elif line and _is_chapter_heading(line):
            chapter = line.rstrip(":： ")
        elif current is None:
            preamble.append(line)
        else:
            body.append(line)
    flush()

    preamble_text = "\n".join(preamble).strip()
    if not doc.articles:
        doc.warnings.append("no_articles_detected")
        doc.articles.append(ParsedArticle(None, None, None, text, 0))
    elif len(preamble_text) > len(title or "") + 40:
        doc.articles.insert(0, ParsedArticle(None, "تمهيد", None, preamble_text, 0))
        for i, art in enumerate(doc.articles):
            art.ordering = i

    doc.warnings.extend(_structure_warnings(doc.articles))
    if INJECTION_PATTERNS.search(text):
        doc.warnings.append("possible_prompt_injection")
    return doc


def _structure_warnings(articles: list[ParsedArticle]) -> list[str]:
    """Article-count sanity checks surfaced to the reviewer (risk: extraction misses an article)."""
    warnings: list[str] = []
    numbers = [a.article_number for a in articles if a.article_number]
    seen: set[str] = set()
    for n in numbers:
        if n in seen:
            warnings.append(f"duplicate_article_number:{n}")
        seen.add(n)
    plain = [int(n) for n in numbers if n.isdigit()]
    for prev, cur in zip(plain, plain[1:], strict=False):
        if cur > prev + 1:
            warnings.append(f"article_number_gap:{prev}->{cur}")
        elif cur < prev:
            warnings.append(f"article_number_out_of_order:{prev}->{cur}")
    return warnings


# --- raw content extraction -------------------------------------------------

ALLOWED_MIME = {
    "application/pdf": "pdf",
    "text/plain": "text",
    "text/html": "html",
}


class ExtractionError(ValueError):
    pass


def sniff_kind(content: bytes, filename: str | None, declared_mime: str | None) -> str:
    """Decide the parser from magic bytes first; the declared type must not contradict it."""
    head = content[:1024].lstrip()
    if head.startswith(b"%PDF-"):
        kind = "pdf"
    elif head[:1] == b"<" and re.search(rb"<(?:!doctype|html|body|main|article|div|p|head)\b", head, re.IGNORECASE):
        kind = "html"
    elif head.startswith((b"PK\x03\x04", b"Rar!", b"\x1f\x8b", b"7z\xbc\xaf", b"MZ")):
        raise ExtractionError("archives and executables are not accepted")
    else:
        kind = "text"
    declared = ALLOWED_MIME.get((declared_mime or "").split(";")[0].strip().lower())
    if declared_mime and declared is None and declared_mime != "application/octet-stream":
        raise ExtractionError(f"unsupported content type: {declared_mime}")
    if declared == "pdf" and kind != "pdf":
        raise ExtractionError("file is declared as PDF but is not a PDF")
    return kind


def extract_text(content: bytes, kind: str) -> str:
    if kind == "pdf":
        import pymupdf

        try:
            with pymupdf.open(stream=content, filetype="pdf") as pdf:
                pages = [page.get_text("text") for page in pdf]
        except Exception as exc:  # pymupdf raises several unrelated error types
            raise ExtractionError(f"could not read PDF: {exc}") from exc
        text = "\n".join(pages)
        if len(text.strip()) < 20:
            raise ExtractionError("PDF has no extractable text (scanned PDFs need OCR, which is out of MVP scope)")
        return clean_text(text)
    if kind == "html":
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe", "object", "embed", "template", "svg"]):
            tag.decompose()
        root = soup.select_one("article") or soup.select_one("main") or soup.body or soup
        for tag in root(["nav", "footer", "aside", "form", "button"]):
            tag.decompose()
        for tag in root.select(
            ".share, .sharing, .social-share, .social-media, .breadcrumb, .breadcrumbs, "
            ".newsletter, .related-content, .related-articles, [aria-label='breadcrumb']"
        ):
            tag.decompose()
        return strip_page_chrome(root.get_text("\n"))
    try:
        return clean_text(content.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ExtractionError("text files must be UTF-8") from exc


# --- source adapters ----------------------------------------------------------


def parse_boe_html(content: bytes) -> ParsedDocument | None:
    """Source adapter for the Bureau of Experts portal (laws.boe.gov.sa).

    That page marks every article as `div.article_item` inside `#divLawText`, next to a table of
    contents, a report form and pop-ups with amendment history. Reading the markup directly avoids
    the duplicate/empty articles that plain-text parsing of the whole page produces.
    Returns None when the page does not have this structure.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    root = soup.select_one("#divLawText")
    items = root.select("div.article_item") if root else []
    if not items:
        return None
    for tag in root(["script", "style", "noscript", "iframe", "object", "embed", "template", "svg"]):
        tag.decompose()
    has_injection = bool(INJECTION_PATTERNS.search(root.get_text(" ")))

    first_heading = root.find(["h3", "h4"])
    doc = ParsedDocument(title=first_heading.get_text(" ", strip=True) if first_heading else None)
    amended = 0
    chapter: str | None = None
    preamble: list[str] = []
    for node in root.find_all(["h3", "h4", "div", "p"]):
        if node.decomposed:  # removed below as part of an article already handled
            continue
        classes = node.get("class") or []
        if node.find_parent("div", class_="article_item") is not None:
            continue
        if node.name in ("h3", "h4"):
            heading_text = node.get_text(" ", strip=True)
            if _is_chapter_heading(heading_text):
                chapter = heading_text.rstrip(":： ")
            continue
        if "article_item" not in classes:
            if not doc.articles and "HTMLContainer" in classes:
                preamble.append(node.get_text("\n", strip=True))
            continue

        title_node = node.find("h3")
        label = title_node.get_text(" ", strip=True) if title_node else ""
        # Amendment history lives in buttons/pop-ups; it is not part of the article's text.
        if "changed-article" in classes or node.select_one(".popup-list, .article_item_popup"):
            amended += 1
        for extra in node.select(".article_btns, .popup-list, .article_item_popup, .hidden-print"):
            extra.decompose()
        if title_node:
            title_node.decompose()
        heading = match_article_heading(label)
        if heading is None:
            doc.warnings.append(f"unrecognised_article_heading:{label[:40]}")
        body = clean_text(node.get_text("\n"))
        if not body:
            doc.warnings.append(f"empty_article:{heading.number if heading else label[:20]}")
        doc.articles.append(
            ParsedArticle(heading.number if heading else None, label.rstrip(":： ") or None, chapter, body, len(doc.articles))
        )

    issuing = clean_text("\n".join(p for p in preamble if p))
    if len(issuing) > 40:
        doc.articles.insert(0, ParsedArticle(None, "أداة الإصدار", None, issuing, 0))
        for i, art in enumerate(doc.articles):
            art.ordering = i
    doc.warnings.extend(_structure_warnings(doc.articles))
    if amended:
        # The portal shows one text per article plus amendment notes. Whether that text is the
        # consolidated wording must be confirmed by the reviewer; it is not assumed here.
        doc.warnings.append(f"amended_articles_on_source_page:{amended}")
    if has_injection:
        doc.warnings.append("possible_prompt_injection")
    return doc


@dataclass
class BoeMetadata:
    status: str | None  # e.g. "ساري", as published by the Bureau of Experts
    issued_at: date | None
    published_at: date | None


_BOE_GREGORIAN = re.compile(r"الموافق\s*:\s*(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_boe_metadata(content: bytes) -> BoeMetadata | None:
    """The official summary block of a Bureau of Experts law page: status (الحالة), issue and
    publication dates with their Gregorian equivalents as printed by the Bureau. None if absent."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    if soup.select_one("#divLawText") is None:
        return None
    lines = [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]

    def after(label: str) -> str | None:
        for index, line in enumerate(lines[:-1]):
            if line == label:
                return lines[index + 1]
        return None

    def gregorian(value: str | None) -> date | None:
        match = _BOE_GREGORIAN.search(value or "")
        if not match:
            return None
        day, month, year = (int(g) for g in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None

    status = after("الحالة")
    return BoeMetadata(
        status=status.strip() if status else None,
        issued_at=gregorian(after("تاريخ الإصدار")),
        published_at=gregorian(after("تاريخ النشر")),
    )


def parse_uqn_html(content: bytes) -> ParsedDocument | None:
    """Source adapter for Umm Al-Qura newspaper article pages (uqn.gov.sa).

    The generic page text also contains navigation, sharing controls, the issue
    archive and the site footer.  Only ``.article-desc`` is the official article
    body shown on the page, so keep that element and its adjacent title.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    root = soup.select_one(".ummalqura-article-details article")
    body_node = root.select_one(".article-desc") if root else None
    if root is None or body_node is None:
        return None

    for tag in body_node(["script", "style", "noscript", "iframe", "object", "embed", "template", "svg"]):
        tag.decompose()
    title_node = root.select_one(".image-wrapper h1, .image-wrapper h2, .image-wrapper h3, .image-wrapper")
    title = clean_text(title_node.get_text(" ", strip=True)) if title_node else None
    body = clean_text(body_node.get_text("\n"))
    doc = ParsedDocument(title=title)
    if body:
        doc.articles.append(ParsedArticle(None, "نص القرار", None, body, 0))
    else:
        doc.warnings.append("empty_official_text")
    if INJECTION_PATTERNS.search(body):
        doc.warnings.append("possible_prompt_injection")
    return doc


def clean_source_text(text: str, source_url: str | None = None, title: str | None = None) -> str:
    """Clean legacy source text at retrieval time without changing its audit copy.

    Older Umm Al-Qura imports used the whole web page.  They remain immutable in
    storage, but navigation/footer text must never be quoted as legal evidence.
    """
    cleaned = strip_page_chrome(text)
    hostname = (urlsplit(source_url).hostname or "").lower() if source_url else ""
    if hostname != "uqn.gov.sa" and not hostname.endswith(".uqn.gov.sa"):
        return cleaned

    if title:
        starts = [match.start() for match in re.finditer(re.escape(title), cleaned)]
        if len(starts) >= 2:
            cleaned = cleaned[starts[1] + len(title) :].lstrip(" \n|-")

    # Legacy snapshots do not all repeat the page title consistently. Start at
    # the first formal enactment line instead of quoting menus or breadcrumbs.
    legal_start = re.compile(
        r"^(?:الصادر(?:ة)?\s+بقرار|قرار\s+(?:مجلس|رقم)|مرسوم\s+ملكي|أمر\s+ملكي|إن\s+مجلس|"
        r"أول[ًاا]:|أولاً:)"
    )
    legacy_lines = cleaned.splitlines()
    start_index = next(
        (
            index
            for index, line in enumerate(legacy_lines)
            if line.strip() != (title or "").strip() and legal_start.match(line.strip())
        ),
        None,
    )
    if start_index is not None:
        cleaned = "\n".join(legacy_lines[start_index:])

    end_markers = (
        "{{ art.hijri_date }}",
        "{{ relatedArticle.title }}",
        "اقرأ أيضاً",
        "جميع الحقوق محفوظة",
        "اشترك في نشرتنا الإخبارية",
        "سياسة الخصوصية",
    )
    cuts = [cleaned.find(marker) for marker in end_markers if marker in cleaned]
    if cuts:
        cleaned = cleaned[: min(cuts)]
    return strip_page_chrome(cleaned)


def document_text(doc: ParsedDocument) -> str:
    parts = [doc.title or ""]
    for art in doc.articles:
        parts.append("\n".join(p for p in (art.article_label, art.text) if p))
    return clean_text("\n\n".join(parts))
