from onebrief.deterministic_verification import (
    GroundingIssueKind,
    append_authoritative_csv_tables,
    apply_deterministic_override,
    validate_draft_grounding,
)
from onebrief.execution_schemas import DraftArtifact, VerificationReport, Verdict
from onebrief.schemas import InternalSource, SourcePriority


def _source(name: str, content: str, media_type: str = "text/plain") -> InternalSource:
    return InternalSource(
        name=name,
        priority=SourcePriority.MANDATORY,
        requirement_keys=["evaluation_rules"],
        content=content,
        media_type=media_type,
    )


def _draft(markdown: str) -> DraftArtifact:
    return DraftArtifact(
        title="Evaluation",
        body_markdown=markdown,
        cited_finding_ids=["F01"],
        drafting_decisions=[],
    )


def _model_pass() -> VerificationReport:
    return VerificationReport(
        verdict=Verdict.PASS,
        criterion_checks=[
            {"criterion": "Preserve supplied data.", "passed": True, "evidence": "Model said yes."}
        ],
        blocking_issues=[],
        revision_instructions=[],
        missing_information=[],
    )


def _csv() -> InternalSource:
    return _source(
        "records.csv",
        "record_id,years,level\nA1,3,strong\nA2,5,medium\n",
        "text/csv",
    )


def _supported_rules() -> InternalSource:
    return _source(
        "rules.md",
        "# Rules\n- at least 5 years: 40 points\n- strong: 30 points\n- medium: 20 points\n",
    )


def test_all_csv_rows_and_fields_pass_when_preserved() -> None:
    draft = _draft(
        "# Result\n\n"
        "- at least 5 years: 40 points\n"
        "- strong: 30 points\n"
        "- medium: 20 points\n\n"
        "| record_id | years | level | score |\n"
        "|---|---:|---|---:|\n"
        "| A1 | 3 years | strong | 30 points |\n"
        "| A2 | 5 years | medium | 60 points |"
    )

    result = validate_draft_grounding([_csv(), _supported_rules()], draft)

    assert result.checked_rows == 2
    assert result.issues == []
    assert result.verdict_override is None


def test_changed_csv_value_forces_revise_even_when_model_passes() -> None:
    draft = _draft(
        "# Result\n\n"
        "- at least 5 years: 40 points\n"
        "- strong: 30 points\n"
        "- medium: 20 points\n\n"
        "| record_id | years | level | score |\n"
        "|---|---:|---|---:|\n"
        "| A1 | 6 years | strong | 30 points |\n"
        "| A2 | 5 years | medium | 60 points |"
    )

    deterministic = validate_draft_grounding([_csv(), _supported_rules()], draft)
    final = apply_deterministic_override(_model_pass(), deterministic)

    assert deterministic.verdict_override == Verdict.REVISE
    assert any(
        issue.kind == GroundingIssueKind.CSV_VALUE_MISMATCH
        and issue.record_id == "A1"
        and issue.field == "years"
        for issue in deterministic.issues
    )
    assert final.verdict == Verdict.REVISE
    assert final.revision_instructions


def test_missing_csv_row_forces_revise() -> None:
    draft = _draft(
        "# Result\n\n"
        "- at least 5 years: 40 points\n"
        "- strong: 30 points\n"
        "- medium: 20 points\n\n"
        "| record_id | years | level | score |\n"
        "|---|---:|---|---:|\n"
        "| A1 | 3 years | strong | 30 points |"
    )

    result = validate_draft_grounding([_csv(), _supported_rules()], draft)

    assert result.verdict_override == Verdict.REVISE
    assert any(
        issue.kind == GroundingIssueKind.CSV_ROW_MISSING and issue.record_id == "A2"
        for issue in result.issues
    )


def test_spreadsheet_csv_appendix_is_idempotent_and_preserves_all_rows() -> None:
    first = append_authoritative_csv_tables(
        [_csv()], _draft("# Workbook\n\nA verified operational workbook result.")
    )
    second = append_authoritative_csv_tables([_csv()], first)

    assert first.body_markdown == second.body_markdown
    assert first.body_markdown.count("<!-- onebrief-authoritative-csv-appendix -->") == 1
    result = validate_draft_grounding([_csv()], second)
    assert result.checked_rows == 2
    assert result.issues == []


def test_unsupported_scoring_conversion_forces_needs_information() -> None:
    incomplete_rules = _source(
        "rules.md",
        "Experience has a maximum weight of 40 points. No conversion table is supplied.",
    )
    draft = _draft(
        "# Result\n\n"
        "- at least 5 years: 40 points\n\n"
        "| record_id | years | level |\n"
        "|---|---:|---|\n"
        "| A1 | 3 years | strong |\n"
        "| A2 | 5 years | medium |"
    )

    deterministic = validate_draft_grounding([_csv(), incomplete_rules], draft)
    final = apply_deterministic_override(_model_pass(), deterministic)

    assert deterministic.verdict_override == Verdict.NEEDS_INFORMATION
    assert any(
        issue.kind == GroundingIssueKind.UNSUPPORTED_SCORING_RULE
        for issue in deterministic.issues
    )
    assert final.verdict == Verdict.NEEDS_INFORMATION
    assert final.missing_information
    assert final.revision_instructions == []
