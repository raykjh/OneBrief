"""Generic evidence-quality gates for research-backed narrative artifacts."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field

from onebrief.execution_schemas import CriterionCheck, DraftArtifact, VerificationReport, Verdict
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis


class EvidenceSufficiencyIssueKind(StrEnum):
    MISSING_DIRECT_SOURCE = "missing_direct_source"
    QUANTIFIED_EVIDENCE_SHORTFALL = "quantified_evidence_shortfall"
    UNBOUNDED_ABSENCE_CLAIM = "unbounded_absence_claim"
    UNSUPPORTED_SAFETY_ABSOLUTE = "unsupported_safety_absolute"
    OUT_OF_SCOPE_FOLLOWUP = "out_of_scope_followup"


class EvidenceSufficiencyIssue(BaseModel):
    kind: EvidenceSufficiencyIssueKind
    message: str


class EvidenceSufficiencyVerification(BaseModel):
    issues: list[EvidenceSufficiencyIssue] = Field(default_factory=list)
    verdict_override: Verdict | None = None


_URL = re.compile(r"https?://[^\s)>|]+", re.IGNORECASE)
_RESEARCH_CRITERION = re.compile(
    r"시장|유사|비교|공개|현재|최신|검색|조사|market|similar|compare|public|current|latest|research|search",
    re.IGNORECASE,
)
_QUANTIFIED_ITEMS_KO = re.compile(
    r"(?P<count>\d+)\s*(?:종|개|명|건)\s*이상",
    re.IGNORECASE,
)
_QUANTIFIED_ITEMS_EN = re.compile(
    r"at\s+least\s+(?P<count>\d+)\s+(?:products?|items?|candidates?|examples?|listings?|sources?)",
    re.IGNORECASE,
)
_NEGATIVE_ABSOLUTE = re.compile(
    r"유사\s*(?:제품|서비스|사례)?\s*(?:이|가)?\s*없|기존\s*시장에\s*없|"
    r"유일(?:한|하다)?|독점적|완전히\s*차별화|"
    r"\b(?:no\s+(?:similar|equivalent|competing)|unique|only\s+one|first\s+ever)\b",
    re.IGNORECASE,
)
_BOUNDED_UNCERTAINTY = re.compile(
    r"검색\s*범위|조사\s*범위|확인되지\s*않|확인\s*필요|단정할\s*수\s*없|"
    r"within\s+the\s+(?:search|review)\s+scope|not\s+identified|not\s+established|requires?\s+confirmation",
    re.IGNORECASE,
)
_SAFETY_ABSOLUTE = re.compile(
    r"부작용\s*없|완전히\s*안전|안전한\s+(?:제품|원료|소재)|위험\s*없|"
    r"\b(?:no\s+side\s+effects?|completely\s+safe|risk[- ]free|zero\s+risk)\b",
    re.IGNORECASE,
)
_SCOPE_LIMIT = re.compile(r"까지만|범위에서\s*제외|하지\s*마|\bonly\b|\bdo\s+not\b", re.IGNORECASE)
_FOLLOWUP_HEADING = re.compile(
    r"^#{1,6}\s+.*(?:후속\s*작업|다음\s*단계|생산\s*단계|판매\s*단계|"
    r"next\s+steps?|follow[- ]?up|implementation\s+guide|production\s+stage)",
    re.IGNORECASE | re.MULTILINE,
)


def _research_text(requirements: RequirementsAnalysis) -> str:
    contract = requirements.completion_contract
    if contract is None:
        return ""
    return "\n".join(
        f"{item.description}\n{item.evidence_required}"
        for item in contract.quality_criteria
        if _RESEARCH_CRITERION.search(item.description + " " + item.evidence_required)
    )


def _linked_rows(markdown: str) -> int:
    rows = set()
    for raw in markdown.splitlines():
        line = raw.strip()
        if _URL.search(line) and (line.startswith("|") or re.match(r"^[-*]\s+", line)):
            rows.add(re.sub(r"\s+", " ", line).casefold())
    return len(rows)


def validate_evidence_sufficiency(
    intake: IntakeRequest,
    requirements: RequirementsAnalysis,
    sources: list[InternalSource],
    draft: DraftArtifact,
) -> EvidenceSufficiencyVerification:
    """Reject research prose that overclaims or cannot expose its evidence."""

    issues: list[EvidenceSufficiencyIssue] = []
    body = draft.body_markdown
    has_public_research = any(source.name == "public_research.md" for source in sources)
    research_text = _research_text(requirements)

    if has_public_research and research_text and not _URL.search(body):
        issues.append(EvidenceSufficiencyIssue(
            kind=EvidenceSufficiencyIssueKind.MISSING_DIRECT_SOURCE,
            message=(
                "Public-research conclusions must expose direct source URLs in the artifact; "
                "finding IDs alone do not let an independent verifier inspect the evidence."
            ),
        ))

    required_counts = [
        int(match.group("count"))
        for pattern in (_QUANTIFIED_ITEMS_KO, _QUANTIFIED_ITEMS_EN)
        for match in pattern.finditer(research_text)
    ]
    if has_public_research and required_counts:
        required = max(required_counts)
        observed = _linked_rows(body)
        if observed < required:
            issues.append(EvidenceSufficiencyIssue(
                kind=EvidenceSufficiencyIssueKind.QUANTIFIED_EVIDENCE_SHORTFALL,
                message=(
                    f"The completion contract requires at least {required} individually inspectable "
                    f"research items, but only {observed} source-linked table or list rows were found. "
                    "Categories or market segments do not count as named items."
                ),
            ))

    if has_public_research and _NEGATIVE_ABSOLUTE.search(body) and not _BOUNDED_UNCERTAINTY.search(body):
        issues.append(EvidenceSufficiencyIssue(
            kind=EvidenceSufficiencyIssueKind.UNBOUNDED_ABSENCE_CLAIM,
            message=(
                "The artifact makes an absolute absence, uniqueness, or exclusivity claim. Replace it "
                "with a bounded search-scope conclusion, list the compared named candidates and sources, "
                "and preserve uncertainty; novelty alone does not prove that no equivalent exists."
            ),
        ))

    if _SAFETY_ABSOLUTE.search(body):
        issues.append(EvidenceSufficiencyIssue(
            kind=EvidenceSufficiencyIssueKind.UNSUPPORTED_SAFETY_ABSOLUTE,
            message=(
                "The artifact uses an absolute safety or no-side-effect claim. Remove the absolute wording "
                "and state only the bounded, cited evidence and remaining verification needs."
            ),
        ))

    scope_text = "\n".join(filter(None, [intake.goal, intake.desired_output or ""]))
    if _SCOPE_LIMIT.search(scope_text) and _FOLLOWUP_HEADING.search(body):
        issues.append(EvidenceSufficiencyIssue(
            kind=EvidenceSufficiencyIssueKind.OUT_OF_SCOPE_FOLLOWUP,
            message=(
                "The user set an explicit scope ceiling, but the artifact adds a follow-up, implementation, "
                "production, or next-step section. Remove work outside the approved deliverable."
            ),
        ))

    return EvidenceSufficiencyVerification(
        issues=issues,
        verdict_override=Verdict.REVISE if issues else None,
    )


def apply_evidence_sufficiency_override(
    report: VerificationReport,
    evidence: EvidenceSufficiencyVerification,
) -> VerificationReport:
    if evidence.verdict_override is None:
        return report
    messages = [item.message for item in evidence.issues]
    checks = [*report.criterion_checks, *(
        CriterionCheck(
            criterion=f"Evidence sufficiency: {item.kind.value}",
            passed=False,
            evidence=item.message,
        )
        for item in evidence.issues
    )]
    return VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=checks,
        blocking_issues=list(dict.fromkeys([*report.blocking_issues, *messages])),
        revision_instructions=list(dict.fromkeys([*report.revision_instructions, *messages])),
        missing_information=report.missing_information,
        temperament_decisions=report.temperament_decisions,
    )
