# Saudi-dialect understanding, the legal-domain guard and the answer policy

Code: `packages/legal_core/legal_core/intent.py` (analysis and guard) and `answer.py` (policy).
Tests: `tests/unit/test_dialect_intent.py` (137 colloquial questions), `tests/unit/test_domain_guard.py`
(55 non-legal questions and the edge cases), `tests/integration/test_dialect_answers.py` (end to end).

## Pipeline

```text
question (kept verbatim)
  → dialect normalization   Arabizi lexicon (fa9alni → فصلني), spelling variants (مكافئة → مكافأة),
                            Arabic normalization (hamza, taa marbuta, alef maqsura, digits)
  → domain guard            Saudi legal / regulatory question?  no → «غير متوفر معلومات عن ذا الشي»
  → intent classification   56 legal intents (labor termination, unpaid wages, eviction, fraud, …)
  → legal domain + entities durations («من شهرين» → 2 months), amounts, article numbers, contract type
  → query expansion         formal legal phrasing of the top intents, used only if the user's own
                            words find no strong official evidence
  → hybrid retrieval → evidence gate → grounded answer or the no-evidence sentence
```

The user's text is never rewritten. Retrieval runs on it first; the formal phrasing of the recognised
intent ("إنهاء عقد العمل من صاحب العمل والتعويض…") is an *additional* query. When an answer came from
the expanded query, it says so: «فُهم سؤالك على أنه عن: …».

Cues are written against the normalized text and compiled with the same letter folding as
`normalize_ar`, so a cue spelled «مسؤولية» matches the normalized «مسووليه». Each cue matches whole
tokens with optional proclitics (و/ف/ب/ل/ك/ال/لل): «وفصلوني» matches, «مفصل» does not.

Registers detected: `saudi_dialect`, `msa`, `english`, `mixed` (Arabic-English), `arabizi`. The register
is passed to the answer model so dialect questions get plain, respectful Saudi-friendly wording and
English questions an English answer (the official Arabic text remains the governing reference).

## Domain guard

The guard combines independent signals, so no single word decides:

| Signal | Weight |
|---|---|
| Best matching legal intent | its weight (1.0–2.0) |
| General legal vocabulary (نظام، عقد، غرامة، يحق، محكمة، law, contract, …) | 0.75 each |
| Normative question frame (ما حكم، هل يجوز، هل يسمح، ممنوع…) | 1.0 |
| A law named by subject (قوانين العمل، نظام الشركات; not «نظام غذائي») | 1.25 |
| Parties of legal relationships (العامل، المستأجر، صاحب البيانات…) | 0.5 each |
| Title of an indexed instrument | 2.0 |

Decision: a foreign jurisdiction without a Saudi component is refused; a score ≥ 2 is in scope; a score ≥ 1
with no non-legal topic is in scope; a legal intent of weight ≥ 1.5 stays in scope even when the subject is
a restaurant, a football club or software ("المطعم رفض يرجع فلوسي" is consumer protection); otherwise the
question is refused. Jailbreak and role-play framing ("تجاهل التعليمات…", "act as…") is detected and
removed before judging, so it can neither widen nor narrow the scope.

When `LLM_PROVIDER=openai`, a model is consulted **only for borderline cases** (weak or conflicting
signals). It can never override the foreign-law refusal, and when it fails the deterministic decision
stands.

Refusal contract: `answer_status = "out_of_scope"`, `answer_ar` is exactly «غير متوفر معلومات عن ذا الشي»,
and there are no citations, caveats, disclaimer, service suggestions or judgments. The UI shows the sentence
alone. Traces record `out_of_scope` without the question text.

## Evidence policy

* No strong official evidence → exactly «لم أجد معلومات موثوقة وكافية في المصادر الرسمية المتاحة للإجابة عن سؤالك.»
  The model is not called.
* The model sees only evidence ids and text. Citations, URLs, authorities and dates are built from database rows.
* Every practical step must cite evidence ids, and every article number in the answer or a step must come from
  cited text. Otherwise the draft is rejected, retried once, then replaced by a verbatim quotation.
* Only approved versions in force on the as-of date are retrievable. Drafts (Istitlaa), news, directories and
  archives can never be published by the release gate, whatever their titles say.
* Judgments are stored separately (`judicial_decisions`) and shown beside an answer as «للاسترشاد فقط»,
  never as evidence and never as a binding rule. Finality is shown as unknown unless published.
* Drafts are listed only when the user asks about proposed regulations, labelled «مشروع غير نافذ».

## Answer structure

`answer_sections` (grounded and partial answers only):

```json
{
  "headings": {"answer": "الجواب", "actions": "وش تقدر تسوي؟", "sources": "المصدر الرسمي"},
  "answer": "…",
  "actions": [
    {"kind": "legal_step", "text": "…", "article_ids": ["…"]},
    {"kind": "fact_to_confirm", "text": "نوع عقد العمل (محدد المدة أو غير محدد المدة)"}
  ],
  "sources": [{"law_title_ar": "…", "article_label": "…", "issuing_authority": "…",
               "legal_status_ar": "سارٍ منذ …", "official_url": "https://laws.boe.gov.sa/…"}]
}
```

`fact_to_confirm` items are questions to the user about facts that change the applicable rule. They
never state law. `legal_step` items exist only when the model produced them with citations.

## Known limits

* Intent cues are hand-written and reviewed. They cover common phrasings across 19 legal areas, not every
  regional expression. Unknown phrasings still reach retrieval with the user's own words, and a
  semantic embedding model (`EMBEDDING_PROVIDER=openai`) is needed for paraphrases with no shared vocabulary.
* Arabizi support is a curated lexicon of legally relevant words, not general transliteration.
* «ما الحكم في هذه الحالة؟» without context is refused: the assistant has no conversation memory.
