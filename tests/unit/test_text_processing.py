import hashlib
import json
from pathlib import Path

import pytest

from legal_core.chunking import chunk_article
from legal_core.normalize import detect_language, lexical_tokens, normalize_ar
from legal_core.parser import (
    ExtractionError,
    extract_text,
    match_article_heading,
    parse_legal_text,
    parse_ordinal,
    sniff_kind,
)

CORPUS = Path("fixtures/corpus")


def test_normalization_unifies_search_variants():
    assert normalize_ar("الإِجَازَةُ السَّنَوِيَّة") == normalize_ar("الاجازه السنويه")
    assert normalize_ar("المادة ١٢") == "الماده 12"
    assert normalize_ar("مـســؤوليّة") == normalize_ar("مسووليه")


def test_light_stemming_matches_attached_particles():
    assert lexical_tokens("والعامل") == lexical_tokens("للعامل") == lexical_tokens("العامل") == ["عامل"]
    assert lexical_tokens("ما هو نظام العمل؟") == ["عمل"]  # question words and generic terms dropped


def test_language_detection():
    assert detect_language("ما مدة الإجازة؟") == "ar"
    assert detect_language("What is the probation period?") == "en"


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("الأولى", 1),
        ("العاشرة", 10),
        ("الحادية عشرة", 11),
        ("الثانية عشرة", 12),
        ("العشرون", 20),
        ("الحادية والعشرون", 21),
        ("التاسعة والتسعون", 99),
        ("المائة", 100),
        ("الخامسة بعد المائة", 105),
        ("الحادية والعشرون بعد المائة", 121),
        ("الثالثة والثلاثون بعد المائتين", 233),
    ],
)
def test_arabic_ordinals(words, expected):
    assert parse_ordinal(words.split())[0] == expected


@pytest.mark.parametrize(
    ("line", "number", "body"),
    [
        ("المادة الأولى:", "1", ""),
        ("المادة الخامسة", "5", ""),
        ("المادة (7)", "7", ""),
        ("المادة ١٢ :", "12", ""),
        ("مادة 3 - يلتزم صاحب العمل", "3", "يلتزم صاحب العمل"),
        ("المادة الخامسة مكرر:", "5 مكرر", ""),
        ("المادة السابعة والسبعون: نص المادة", "77", "نص المادة"),
    ],
)
def test_article_headings(line, number, body):
    heading = match_article_heading(line)
    assert heading is not None
    assert (heading.number, heading.inline_body) == (number, body)


@pytest.mark.parametrize(
    "line",
    [
        "المادة الخامسة من هذا النظام تسري على جميع العقود",  # in-body reference
        "المادة المشار إليها أعلاه",
        "مادة كيميائية خطرة",
        "وفقاً للمادة الثالثة",
    ],
)
def test_in_body_references_are_not_headings(line):
    assert match_article_heading(line) is None


@pytest.mark.parametrize(
    ("file", "expected_numbers"),
    [
        ("labor_v1.txt", [str(n) for n in range(1, 11)]),
        ("companies_v1.txt", [str(n) for n in range(1, 10)]),
        ("pdpl_v1.txt", [str(n) for n in range(1, 13)]),
        ("pdpl_regulation_v1.txt", ["1", "2", "3", "4"]),
        ("hostile_notice.txt", ["1", "2"]),
    ],
)
def test_fixture_corpus_article_extraction_is_exact(file, expected_numbers):
    """FR-02 on the fixture corpus: every article id is extracted, in order, with a body."""
    doc = parse_legal_text((CORPUS / file).read_text(encoding="utf-8"))
    assert [a.article_number for a in doc.articles] == expected_numbers
    assert all(a.text for a in doc.articles)
    assert not [w for w in doc.warnings if w != "possible_prompt_injection"]


def test_parser_keeps_original_text_and_chapters():
    doc = parse_legal_text((CORPUS / "labor_v1.txt").read_text(encoding="utf-8"))
    article5 = doc.articles[4]
    assert article5.chapter == "الباب الثاني: عقد العمل"
    assert "تسعين يوماً" in article5.text  # diacritics/punctuation preserved verbatim
    assert doc.title.startswith("نظام العمل التجريبي")


def test_parser_flags_structure_problems_and_injection():
    doc = parse_legal_text("عنوان\nالمادة الأولى:\nنص\nالمادة الثالثة:\nنص\nالمادة الثالثة:\nنص")
    assert "article_number_gap:1->3" in doc.warnings
    assert "duplicate_article_number:3" in doc.warnings
    hostile = parse_legal_text((CORPUS / "hostile_notice.txt").read_text(encoding="utf-8"))
    assert "possible_prompt_injection" in hostile.warnings
    assert "no_articles_detected" in parse_legal_text("إعلان قصير دون مواد").warnings


def test_upload_sniffing_rejects_archives_and_mismatches():
    assert sniff_kind(b"%PDF-1.7 ...", "a.pdf", "application/pdf") == "pdf"
    assert sniff_kind("نص".encode(), "a.txt", "text/plain") == "text"
    assert sniff_kind(b"<html><body>x</body></html>", "a.html", "text/html") == "html"
    with pytest.raises(ExtractionError):
        sniff_kind(b"PK\x03\x04zipdata", "a.pdf", "application/pdf")
    with pytest.raises(ExtractionError):
        sniff_kind(b"MZ\x90\x00", "a.txt", "text/plain")
    with pytest.raises(ExtractionError):
        sniff_kind("نص".encode(), "a.pdf", "application/pdf")
    with pytest.raises(ExtractionError):
        sniff_kind(b"data", "a.docx", "application/msword")


def test_html_extraction_drops_active_content():
    html = "<html><body><script>alert(1)</script><p>المادة الأولى:</p><p>نص المادة</p><style>p{}</style></body></html>"
    text = extract_text(html.encode(), "html")
    assert "alert" not in text and "p{}" not in text
    assert parse_legal_text(text).articles[0].article_number == "1"


def test_html_extraction_prefers_article_and_drops_page_chrome():
    html = """<html><body><header>اسم الموقع</header><nav>الرئيسية</nav>
    <main><article><h1>قرار تجريبي</h1><div class="social-share">Facebook X WhatsApp</div>
    <p>يقرر اعتماد النص الرسمي.</p></article><aside>محتوى مرتبط</aside></main>
    <footer>جميع الحقوق محفوظة 2026</footer></body></html>"""
    text = extract_text(html.encode(), "html")
    assert text == "قرار تجريبي\nيقرر اعتماد النص الرسمي."


def test_pdf_extraction_roundtrip():
    import pymupdf

    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Article text for extraction test, long enough to pass.")
    text = extract_text(pdf.tobytes(), "pdf")
    assert "extraction test" in text
    with pytest.raises(ExtractionError):
        extract_text(b"%PDF-1.4 broken", "pdf")


def test_long_articles_split_only_on_sentence_boundaries():
    sentences = [f"الفقرة رقم {i} تنص على حكم مستقل وواضح." for i in range(40)]
    text = " ".join(sentences)
    chunks = chunk_article(text, 300)
    assert len(chunks) > 1
    for offset, chunk in chunks:
        assert len(chunk) <= 300
        assert text[offset : offset + len(chunk)] == chunk  # offsets point into the original
        assert chunk.endswith(".")
    assert chunk_article("نص قصير", 300) == [(0, "نص قصير")]


def test_manifest_checksums_match_fixture_files():
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    for doc in manifest["documents"]:
        assert hashlib.sha256((CORPUS / doc["file"]).read_bytes()).hexdigest() == doc["sha256"], doc["file"]


def test_bureau_of_experts_page_is_read_from_its_article_markup():
    from legal_core.parser import parse_boe_html

    html = """<html><body><select id="ddlArticle"><option>المادة الأولى</option><option>المادة الثانية</option></select>
    <div id="divLawText"><div><h3 class="center">نظام تجريبي</h3><h4 class="center">مرسوم ملكي رقم (م/1)</h4>
    <div class="HTMLContainer"><p>رسمنا بما هو آت: الموافقة على النظام بالصيغة المرافقة، ويعمل به بعد تسعين يوماً من نشره.</p></div>
    <h3 class="center">الباب الأول: أحكام عامة</h3>
    <div class="article_item no_alternate"><h3 class="center">المادة الأولى</h3><div class="HTMLContainer"><p>نص المادة الأولى.</p></div><div class="article_btns"></div></div>
    <div class="article_item changed-article"><h3 class="center">المادة الثانية</h3><div class="article_btns">تعديلات المادة</div>
      <div class="popup-list"><div class="article_item_popup"><h3>المادة الثانية</h3><p>عدلت بموجب المرسوم (م/2) لتكون: نص قديم.</p></div></div>
      <p>نص المادة الثانية وفق المادة الأولى من هذا النظام.</p></div>
    </div></div></body></html>"""
    doc = parse_boe_html(html.encode())
    assert doc.title == "نظام تجريبي"
    assert [(a.article_number, a.article_label) for a in doc.articles] == [(None, "أداة الإصدار"), ("1", "المادة الأولى"), ("2", "المادة الثانية")]
    assert doc.articles[1].text == "نص المادة الأولى." and doc.articles[1].chapter == "الباب الأول: أحكام عامة"
    assert doc.articles[2].text == "نص المادة الثانية وفق المادة الأولى من هذا النظام."  # no amendment pop-up text
    assert "تسعين يوماً" in doc.articles[0].text
    assert doc.warnings == ["amended_articles_on_source_page:1"]
    assert parse_boe_html(b"<html><body><p>plain page</p></body></html>") is None


def test_umm_al_qura_page_excludes_navigation_and_footer():
    from legal_core.parser import clean_source_text, document_text, parse_uqn_html

    html = """<html><body><nav>خريطة الموقع Facebook WhatsApp</nav>
    <div class="ummalqura-article-details"><article>
      <div class="image-wrapper"><h1>قرار رسمي تجريبي</h1></div>
      <div class="meta-wrapper">13 أكتوبر 2026</div>
      <div class="article-desc"><p>إن مجلس الإدارة يقرر ما يلي:</p><p>أولاً: اعتماد الإجراء.</p></div>
      <div>اقرأ أيضاً {{ relatedArticle.title }}</div>
    </article></div><footer>جميع الحقوق محفوظة</footer></body></html>"""
    doc = parse_uqn_html(html.encode())
    assert doc is not None
    assert doc.title == "قرار رسمي تجريبي"
    assert [(a.article_label, a.text) for a in doc.articles] == [
        ("نص القرار", "إن مجلس الإدارة يقرر ما يلي:\nأولاً: اعتماد الإجراء.")
    ]
    assert "Facebook" not in document_text(doc)
    assert "جميع الحقوق" not in document_text(doc)

    legacy = "قرار رسمي تجريبي\nخريطة الموقع\nقرار رسمي تجريبي\n13 أكتوبر 2026\nنص القرار الصحيح.\n{{ art.hijri_date }}\nجميع الحقوق محفوظة"
    assert clean_source_text(legacy, "https://uqn.gov.sa/details", "قرار رسمي تجريبي") == "13 أكتوبر 2026\nنص القرار الصحيح."

    single_title_legacy = "قرار تجريبي\nمذكرات تفاهم\nإعلانات\nالصادرة بقرار مجلس الإدارة رقم (1)\nأولًا: النص الرسمي.\nجميع الحقوق محفوظة"
    assert clean_source_text(single_title_legacy, "https://uqn.gov.sa/details", "قرار تجريبي") == (
        "الصادرة بقرار مجلس الإدارة رقم (1)\nأولًا: النص الرسمي."
    )
