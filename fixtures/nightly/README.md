# Nightly synchronization fixtures (TEST DATA)

Every file here is fictional and exists only to test the autonomous nightly synchronization
(PRD v2 section 16). None of it is a real Saudi law, decision or gazette publication.

| Feed | Scenario |
|---|---|
| `feeds/nightly/day1_new_law.xml` | T01 new instrument; T07 draft and press release are excluded |
| `feeds/nightly/day2_amendment.xml` | T02 unchanged items are a no-op; T03-T05 amendment of two articles |
| `feeds/nightly/repeal.xml` | Repeal of an instrument from an evidenced date |
| `feeds/nightly/ambiguous.xml` | T08 missing official text, competing dates, uncertain article mapping |
| `feeds/nightly/hostile.xml` | T09 PDF with instructions addressed to an AI system |
| `feeds/simulated_feed.xml` | T06 amendment that takes effect next month |

Regenerate with `python scripts/make_nightly_fixtures.py` (the generator is kept in-repo).
