"""Runs the gold set and enforces the PRD hard guardrails. Soft targets are reported, not asserted,
so a regression shows up in the measured report instead of being hidden by a tuned threshold."""

import pytest

from tests.evaluation.run_eval import run

pytestmark = pytest.mark.integration


def test_gold_set_hard_guardrails(migrated_engine):
    report = run()
    assert report["dataset"]["total"] == 34
    assert report["metrics"]["abstention"] == 1.0, report["failures"]
    assert report["metrics"]["temporal"] == 1.0, report["failures"]
    by_class = report["dataset"]["by_class"]
    assert by_class["adversarial"]["passed"] == by_class["adversarial"]["total"], report["failures"]
    # No citation may point outside the gold articles on answerable questions.
    assert report["metrics"]["citation_accuracy"] == 1.0, report["failures"]
    assert report["metrics"]["recall_at_5"] >= 0.9
