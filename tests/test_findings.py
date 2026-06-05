from mtk_rescue.core.findings import Finding, Severity


def test_finding_is_immutable():
    f = Finding(check_id="x", title="t", severity=Severity.OK, summary="s")
    try:
        f.title = "other"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Finding should be frozen")


def test_severity_ordering_is_string_valued():
    assert Severity.OK.value == "ok"
    assert Severity.CRITICAL.value == "critical"
