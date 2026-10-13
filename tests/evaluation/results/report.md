# Evaluation report (measured)

- Generated: 2026-10-13T20:18:07+00:00
- Corpus: FICTIONAL fixture corpus (test data) - not the official pilot corpus
- Embeddings: `hash-ngram-1536` · Answer model: `extractive-quote-v1` · Mode: offline
- Cases: 34

| Metric | Measured | PRD target | Met |
|---|---|---|---|
| Retrieval Recall@5 | 100.0% | ≥90% | yes |
| Citation accuracy | 100.0% | ≥95% | yes |
| Auto-checked correctness (proxy) | 96.2% | ≥90% | yes |
| Abstention on out-of-corpus | 100.0% | ≥100% | yes |
| Effective-date scenarios | 100.0% | ≥100% | yes |

Latency (ms): p50 16.0, p95 22, max 26.

| Class | Passed | Total |
|---|---|---|
| direct | 10 | 10 |
| paraphrase | 5 | 6 |
| multi_article | 3 | 3 |
| out_of_corpus | 5 | 5 |
| temporal | 5 | 5 |
| adversarial | 3 | 3 |
| ambiguous | 2 | 2 |

## Failures

- **P02** (paraphrase) `متى تودع رواتب العمال؟` → insufficient_evidence: abstained on an answerable question; missing expected text: اليوم السابع

## Notes

- auto_checked_correctness is a string/citation check, not the human grading required by PRD 15.2.
- Offline mode uses a hashing embedder and a verbatim-quote answer model; it measures the pipeline (filters, lexical retrieval, gating, citations, temporal logic), not LLM answer quality.
- Prompt-injection resistance of a generative model must be measured with --live.
