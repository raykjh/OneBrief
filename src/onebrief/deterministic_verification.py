"""Deterministic grounding checks that outrank model verification."""

from __future__ import annotations

import csv
import io
import re
from enum import StrEnum

from pydantic import BaseModel, Field

from onebrief.execution_schemas import (
    CriterionCheck,
    DraftArtifact,
    VerificationReport,
    Verdict,
)
from onebrief.schemas import InternalSource


class GroundingIssueKind(StrEnum):
    CSV_ROW_MISSING = "csv_row_missing"
    CSV_VALUE_MISMATCH = "csv_value_mismatch"
    UNSUPPORTED_SCORING_RULE = "unsupported_scoring_rule"


class GroundingIssue(BaseModel):
    kind: GroundingIssueKind
    source_name: str
    record_id: str | None = None
    field: str | None = None
    expected: str | None = None
    observed: str | None = None
    message: str


class DeterministicVerification(BaseModel):
    checked_csv_sources: list[str] = Field(default_factory=list)
    checked_rows: int = 0
    issues: list[GroundingIssue] = Field(default_factory=list)
    verdict_override: Verdict | None = None


_MARKDOWN_DECORATION = re.compile(r"[*_`]")
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_NUMBER = re.compile(r"(?<![\d.])(-?\d+(?:\.\d+)?)(?![\d.])")
_SCORE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:점|points?)", re.IGNORECASE)
_KOREAN_THRESHOLD = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(년|개|회|명|점|%)?\s*(이상|이하|미만|초과)"
)
_SYMBOL_THRESHOLD = re.compile(
    r"(>=|<=|>|<)\s*(\d+(?:\.\d+)?)\s*(년|개|회|명|점|%|years?|items?|projects?)?",
    re.IGNORECASE,
)
_ENGLISH_THRESHOLD = re.compile(
    r"(?:at\s+least|at\s+most|more\s+than|less\s+than)\s+(\d+(?:\.\d+)?)\s*"
    r"(years?|items?|projects?|points?|%)?",
    re.IGNORECASE,
)
_RULE_SEPARATOR = re.compile(r"[:=]|->|=>|→")
_LABEL_TOKEN = re.compile(r"[가-힣]{2,}|[A-Za-z]{2,}")
_LABEL_STOPWORDS = {
    "배점",
    "점수",
    "산출",
    "방식",
    "기준",
    "총점",
    "만점",
    "score",
    "scores",
    "point",
    "points",
    "method",
    "criteria",
}

_SOURCE_APPENDIX_MARKER = "<!-- onebrief-authoritative-csv-appendix -->"


def _clean(value: str) -> str:
    return _MARKDOWN_DECORATION.sub("", value).strip()


def append_authoritative_csv_tables(
    sources: list[InternalSource], draft: DraftArtifact
) -> DraftArtifact:
    """Attach immutable CSV rows to spreadsheet artifacts for export and verification.

    The model may describe a workbook without reproducing its source rows. A real
    spreadsheet deliverable must retain those rows, so the pipeline appends them
    deterministically rather than asking the model to copy data token by token.
    """
    body = draft.body_markdown.split(_SOURCE_APPENDIX_MARKER, 1)[0].rstrip()
    sections: list[str] = []
    for source in sources:
        if not (
            source.name.casefold().endswith(".csv")
            or source.media_type.casefold() == "text/csv"
        ):
            continue
        reader = csv.DictReader(io.StringIO(source.content))
        fields = list(reader.fieldnames or [])
        records = list(reader)
        if not fields or not records:
            continue

        def cell(value: object) -> str:
            return str(value or "").replace("|", "\\|").replace("\n", " ").strip()

        lines = [
            f"### 원자료 · {source.name}",
            "| " + " | ".join(cell(field) for field in fields) + " |",
            "| " + " | ".join("---" for _ in fields) + " |",
        ]
        lines.extend(
            "| " + " | ".join(cell(record.get(field)) for field in fields) + " |"
            for record in records
        )
        sections.append("\n".join(lines))
    if not sections:
        return draft
    return draft.model_copy(update={
        "body_markdown": (
            body
            + "\n\n"
            + _SOURCE_APPENDIX_MARKER
            + "\n\n## 검증용 원자료\n\n"
            + "\n\n".join(sections)
        )
    })


def _markdown_rows(markdown: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in markdown.splitlines():
        if line.count("|") < 2:
            continue
        cells = [_clean(cell) for cell in line.strip().strip("|").split("|")]
        if not cells or any(_TABLE_SEPARATOR.fullmatch(cell.replace(" ", "")) for cell in cells):
            continue
        rows.append(cells)
    return rows


def _cell_contains(cell: str, expected: str) -> bool:
    expected = expected.strip()
    if not expected:
        return True
    if re.fullmatch(r"-?\d+(?:\.\d+)?", expected):
        return any(match.group(1) == expected for match in _NUMBER.finditer(cell))
    normalized_cell = re.sub(r"[^0-9A-Za-z가-힣]+", "", cell).casefold()
    normalized_expected = re.sub(r"[^0-9A-Za-z가-힣]+", "", expected).casefold()
    return bool(normalized_expected) and normalized_expected in normalized_cell


def _csv_issues(source: InternalSource, markdown: str) -> tuple[int, list[GroundingIssue]]:
    try:
        reader = csv.DictReader(io.StringIO(source.content))
        fields = list(reader.fieldnames or [])
        records = list(reader)
    except csv.Error:
        return 0, []
    if len(fields) < 2 or not records:
        return 0, []

    table_rows = _markdown_rows(markdown)
    issues: list[GroundingIssue] = []
    key_field = fields[0]
    for record in records:
        record_id = (record.get(key_field) or "").strip()
        if not record_id:
            continue
        matches: list[tuple[list[str], int]] = []
        for row in table_rows:
            for index, cell in enumerate(row):
                if _clean(cell).casefold() == record_id.casefold():
                    matches.append((row, index))
        if not matches:
            issues.append(
                GroundingIssue(
                    kind=GroundingIssueKind.CSV_ROW_MISSING,
                    source_name=source.name,
                    record_id=record_id,
                    field=key_field,
                    expected=record_id,
                    message=f"{source.name} record {record_id} is missing from the result table.",
                )
            )
            continue

        row, key_index = max(matches, key=lambda item: len(item[0]) - item[1])
        value_cells = row[key_index + 1 :]
        for offset, field in enumerate(fields[1:]):
            expected = (record.get(field) or "").strip()
            observed = value_cells[offset] if offset < len(value_cells) else "<missing column>"
            if not _cell_contains(observed, expected):
                issues.append(
                    GroundingIssue(
                        kind=GroundingIssueKind.CSV_VALUE_MISMATCH,
                        source_name=source.name,
                        record_id=record_id,
                        field=field,
                        expected=expected,
                        observed=observed,
                        message=(
                            f"{source.name} record {record_id} field {field} must preserve "
                            f"{expected!r}, but the result row contains {observed!r}."
                        ),
                    )
                )
    return len(records), issues


def _thresholds(text: str) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for number, unit, operator in _KOREAN_THRESHOLD.findall(text):
        found.add((number, (unit or "").casefold(), operator))
    for operator, number, unit in _SYMBOL_THRESHOLD.findall(text):
        found.add((number, (unit or "").casefold(), operator))
    for match in _ENGLISH_THRESHOLD.finditer(text):
        phrase = match.group(0).casefold()
        operator = next(
            item for item in ("at least", "at most", "more than", "less than") if item in phrase
        )
        found.add((match.group(1), (match.group(2) or "").casefold(), operator))
    return found


def _score_values(text: str) -> set[str]:
    return {match.group(1) for match in _SCORE.finditer(text)}


def _rule_supported(rule_line: str, source_lines: list[str]) -> bool:
    scores = _score_values(rule_line)
    if not scores:
        return True
    thresholds = _thresholds(rule_line)
    if thresholds:
        return any(
            scores.issubset(_score_values(source_line))
            and thresholds.issubset(_thresholds(source_line))
            for source_line in source_lines
        )

    separator = _RULE_SEPARATOR.search(rule_line)
    if not separator:
        return True
    label_text = rule_line[: separator.start()]
    labels = {
        token.casefold()
        for token in _LABEL_TOKEN.findall(label_text)
        if token.casefold() not in _LABEL_STOPWORDS
    }
    if not labels:
        return False
    return any(
        scores.issubset(_score_values(source_line))
        and any(label in source_line.casefold() for label in labels)
        for source_line in source_lines
    )


def _unsupported_rule_issues(
    sources: list[InternalSource], markdown: str
) -> list[GroundingIssue]:
    source_lines = [
        _clean(line)
        for source in sources
        for line in source.content.splitlines()
        if line.strip()
    ]
    issues: list[GroundingIssue] = []
    seen: set[str] = set()
    for raw_line in markdown.splitlines():
        line = _clean(raw_line)
        if not line or line.startswith("|") or not _score_values(line):
            continue
        thresholds = _thresholds(line)
        separator = _RULE_SEPARATOR.search(line)
        if not thresholds and not separator:
            continue
        if not thresholds and separator:
            right_side = line[separator.end() :]
            if not re.match(
                r"\s*\(?\s*\d+(?:\.\d+)?\s*(?:점|points?)", right_side, re.IGNORECASE
            ):
                continue
        normalized = re.sub(r"\s+", " ", line).strip().casefold()
        if normalized in seen or _rule_supported(line, source_lines):
            continue
        seen.add(normalized)
        issues.append(
            GroundingIssue(
                kind=GroundingIssueKind.UNSUPPORTED_SCORING_RULE,
                source_name="authoritative_sources",
                observed=line,
                message=(
                    "The result introduces a scoring conversion rule not present in any "
                    f"authoritative source: {line}"
                ),
            )
        )
    return issues


def validate_draft_grounding(
    sources: list[InternalSource], draft: DraftArtifact, *,
    require_full_csv_preservation: bool = True,
) -> DeterministicVerification:
    checked_sources: list[str] = []
    checked_rows = 0
    issues: list[GroundingIssue] = []
    for source in sources:
        if (
            require_full_csv_preservation
            and (source.name.casefold().endswith(".csv") or source.media_type.casefold() == "text/csv")
        ):
            rows, csv_issues = _csv_issues(source, draft.body_markdown)
            if rows:
                checked_sources.append(source.name)
                checked_rows += rows
                issues.extend(csv_issues)
    issues.extend(_unsupported_rule_issues(sources, draft.body_markdown))

    kinds = {issue.kind for issue in issues}
    if GroundingIssueKind.UNSUPPORTED_SCORING_RULE in kinds:
        override = Verdict.NEEDS_INFORMATION
    elif kinds:
        override = Verdict.REVISE
    else:
        override = None
    return DeterministicVerification(
        checked_csv_sources=checked_sources,
        checked_rows=checked_rows,
        issues=issues,
        verdict_override=override,
    )


def apply_deterministic_override(
    model_report: VerificationReport,
    deterministic: DeterministicVerification,
) -> VerificationReport:
    if deterministic.verdict_override is None:
        return model_report

    data_issues = [
        issue
        for issue in deterministic.issues
        if issue.kind != GroundingIssueKind.UNSUPPORTED_SCORING_RULE
    ]
    rule_issues = [
        issue
        for issue in deterministic.issues
        if issue.kind == GroundingIssueKind.UNSUPPORTED_SCORING_RULE
    ]
    checks = [*model_report.criterion_checks]
    if data_issues:
        checks.append(
            CriterionCheck(
                criterion="Every CSV record and field value must be preserved in the result.",
                passed=False,
                evidence=f"Deterministic comparison found {len(data_issues)} row-level issue(s).",
            )
        )
    if rule_issues:
        checks.append(
            CriterionCheck(
                criterion="Scoring conversions must be explicitly supported by authoritative sources.",
                passed=False,
                evidence=f"Deterministic comparison found {len(rule_issues)} unsupported rule(s).",
            )
        )

    blocking = list(
        dict.fromkeys(
            [*model_report.blocking_issues, *(item.message for item in deterministic.issues)]
        )
    )
    if rule_issues or model_report.verdict == Verdict.NEEDS_INFORMATION:
        missing = list(
            dict.fromkeys(
                [
                    *model_report.missing_information,
                    *(
                        "Provide an authoritative scoring conversion table or formula for: "
                        + (item.observed or item.message)
                        for item in rule_issues
                    ),
                ]
            )
        )
        return VerificationReport(
            verdict=Verdict.NEEDS_INFORMATION,
            criterion_checks=checks,
            blocking_issues=blocking,
            revision_instructions=[],
            missing_information=missing,
            temperament_decisions=model_report.temperament_decisions,
        )

    instructions = list(
        dict.fromkeys(
            [
                *model_report.revision_instructions,
                *(item.message for item in data_issues),
            ]
        )
    )
    return VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=checks,
        blocking_issues=blocking,
        revision_instructions=instructions,
        missing_information=model_report.missing_information,
        temperament_decisions=model_report.temperament_decisions,
    )
