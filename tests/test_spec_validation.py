from mechatronikers.spec import Check, ValidationReport


def _check(id: str, passed: bool) -> Check:
    return Check(
        id=id, category="V1_geometry", expected=1, measured=2, tolerance=0.5, passed=passed
    )


def test_report_score_and_passed() -> None:
    report = ValidationReport(build_id="b1", checks=[_check("a", True), _check("b", False)])
    assert report.score == 0.5
    assert not report.passed
    assert report.model_dump()["passed"] is False


def test_empty_report_does_not_pass() -> None:
    assert not ValidationReport(build_id="b1").passed


def test_feedback_lists_failed_checks_only() -> None:
    report = ValidationReport(build_id="b1", checks=[_check("ok", True), _check("bad", False)])
    assert report.feedback() == "- bad: expected 1, measured 2 (tol ±0.5)"
    assert ValidationReport(build_id="b2", checks=[_check("ok", True)]).feedback() == (
        "All checks passed."
    )


def test_int_values_keep_their_type() -> None:
    assert _check("a", True).measured == 2
    assert isinstance(_check("a", True).measured, int)
