"""Generic evidence-quality gates for research-backed narrative artifacts."""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from onebrief.assurance import resolve_assurance_policy
from onebrief.execution_schemas import (
    AnalysisPackage,
    CriterionCheck,
    DraftArtifact,
    VerificationReport,
    Verdict,
)
from onebrief.public_research import PublicResearchResult, PublicWebSource
from onebrief.schemas import IntakeRequest, InternalSource, RequirementsAnalysis


class EvidenceSufficiencyIssueKind(StrEnum):
    MISSING_DIRECT_SOURCE = "missing_direct_source"
    QUANTIFIED_EVIDENCE_SHORTFALL = "quantified_evidence_shortfall"
    UNBOUNDED_ABSENCE_CLAIM = "unbounded_absence_claim"
    UNSUPPORTED_SAFETY_ABSOLUTE = "unsupported_safety_absolute"
    OUT_OF_SCOPE_FOLLOWUP = "out_of_scope_followup"
    UNCITED_MATERIAL_CLAIM = "uncited_material_claim"
    UNGROUNDED_MODEL_URL = "ungrounded_model_url"
    FORBIDDEN_OUTPUT_DISCLOSURE = "forbidden_output_disclosure"
    UNTRACEABLE_FINDING = "untraceable_finding"
    MISSING_PRIMARY_REGULATORY_SOURCE = "missing_primary_regulatory_source"
    UNSUPPORTED_CAUSAL_ABSOLUTE = "unsupported_causal_absolute"


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
    r"유일(?:한|하다)?|독점적(?:인)?\s*(?:지위|시장|제품|서비스|기술)|완전히\s*차별화|"
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
_CAUSAL_ABSOLUTE = re.compile(
    r"(?:교차\s*오염|교차\s*감염|감염|미생물|욕창).{0,30}"
    r"(?:원천(?:적)?(?:으로)?\s*차단|완전(?:히)?\s*(?:제거|차단|예방))|"
    r"(?:eliminates?|completely\s+(?:eliminates?|prevents?|blocks?)).{0,30}"
    r"(?:cross[- ]contamination|infection|microbial|pressure\s+injur)",
    re.IGNORECASE,
)
_REGULATORY_CLAIM = re.compile(
    r"의약외품|위생용품|약사법|품목(?:허가|신고|제조보고)|제조업\s*신고|"
    r"법적으로\s*(?:불가능|가능)|즉시\s*출시|"
    r"\b(?:quasi-drug|regulatory|legally|registration|filing|approval)\b",
    re.IGNORECASE,
)
_PRIMARY_AUTHORITY_HOST = re.compile(
    r"(?:^|\.)(?:law\.go\.kr|mfds\.go\.kr|foodsafetykorea\.go\.kr|gov\.kr|go\.kr|"
    r"iso\.org|who\.int|cdc\.gov|europa\.eu)$",
    re.IGNORECASE,
)
_SCOPE_LIMIT = re.compile(r"까지만|범위에서\s*제외|하지\s*마|\bonly\b|\bdo\s+not\b", re.IGNORECASE)
_SCOPE_LEAKAGE = re.compile(
    r"품목제조신고(?:를)?\s*(?:즉시\s*)?(?:신청|진행|제출|착수)|"
    r"(?:생산|판매|출시)(?:합니다|하겠습니다|한다)|"
    r"(?:production|deployment|sale|launch)[ -]?ready|ready\s+for\s+(?:production|deployment|sale|launch)",
    re.IGNORECASE,
)
_MATERIAL_CLAIM = re.compile(
    r"등록|인정|기능성|효능|개선|감소|증가|증진|억제|완화|도움|안전|위험|"
    r"부작용|효과|입증|확인|점유|가격|비율|"
    r"\b(?:registered|approved|improves?|reduces?|increases?|prevents?|causes?|"
    r"safe|risk|side\s+effects?|effective|proven|confirmed|market\s+share|price)\b",
    re.IGNORECASE,
)
_FINDING_CITATION = re.compile(
    r"\[F\d{2,}(?:\s*,\s*F\d{2,})*\]",
    re.IGNORECASE,
)
_WEB_SOURCE_CITATION = re.compile(r"\[(W\d{2,})\]", re.IGNORECASE)
_MIXED_CITATION_GROUP = re.compile(
    r"\[((?:[FW]\d{2,})(?:\s*,\s*[FW]\d{2,})*)\]",
    re.IGNORECASE,
)
_LABEL_ONLY = re.compile(r"^(?:[-*]\s*)?\*\*[^*]+\*\*:?\s*$")
_ESTIMATE_UNCERTAINTY = re.compile(
    r"(?:추정치|가정|estimate|assumption).*(?:서면\s*견적|확인\s*필요|검증\s*필요|"
    r"written\s+quotation|requires?\s+(?:verification|confirmation))",
    re.IGNORECASE,
)
_HYPOTHESIS_MARKER = re.compile(
    r"가설|추론|가능성|잠재적|예상|검토\s*대상|"
    r"\b(?:hypothesis|inference|may|might|could|potential)\b",
    re.IGNORECASE,
)
_VALIDATION_BOUNDARY = re.compile(
    r"검증|시험|파일럿|측정|확인\s*필요|입증\s*전|주장\s*금지|광고\s*금지|"
    r"\b(?:test|pilot|measure|requires?\s+(?:verification|confirmation)|"
    r"before\s+(?:claiming|marketing))\b",
    re.IGNORECASE,
)
_PROPOSAL_SCOPE_STATEMENT = re.compile(
    r"(?:제안서|계획서).*(?:목표|목적|대상|기획|담고)|"
    r"\b(?:proposal|plan)\b.*\b(?:goal|purpose|intended|scope)\b",
    re.IGNORECASE,
)
_NORMATIVE_TEST_PROCEDURE = re.compile(
    r"(?:테스트|시험|검사|평가).*(?:관찰|측정|확인|검증|중단|설계)|"
    r"\b(?:test|evaluation|inspection).*(?:observe|measure|verify|stop|design)",
    re.IGNORECASE,
)
_NORMATIVE_CHECKLIST = re.compile(
    r"(?:RFQ|견적\s*요청|체크리스트).*(?:확인|요구사항|질문|검토)|"
    r"(?:확인|검토).*(?:RFQ|견적\s*요청|체크리스트)",
    re.IGNORECASE,
)
_EVALUATION_METRIC = re.compile(
    r"^(?:[-*]\s*)?\*\*[^*]+\*\*:?\s*.*(?:정도|여부|평가|점수|관찰)\.?$",
    re.IGNORECASE,
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


def _direct_urls(markdown: str) -> set[str]:
    direct: set[str] = set()
    for match in _URL.finditer(markdown):
        url = match.group(0).rstrip(".,;]")
        parsed = urlparse(url)
        if (parsed.path and parsed.path != "/") or parsed.query:
            # URL hosts are case-insensitive, but paths and query keys may not be.
            # Preserve the observed spelling and normalize only in _normalize_url.
            direct.add(url)
    return direct


def _table_row_direct_urls(markdown: str) -> set[str]:
    """Return direct URLs attached to concrete Markdown table data rows."""

    direct: set[str] = set()
    lines = markdown.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if not (line.startswith("|") and line.endswith("|")):
            continue
        if re.fullmatch(r"[-|: ]+", line):
            continue
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", next_line):
            continue
        direct.update(_direct_urls(line))
    return direct


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.rstrip("/"))
    hostname = (parsed.hostname or "").casefold().removeprefix("www.")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.casefold()}://{hostname}{port}{parsed.path.rstrip('/')}{'?' + parsed.query if parsed.query else ''}"


def _grounded_source_urls(sources: list[InternalSource]) -> set[str]:
    """Return exact URLs preserved in the grounded-source registry only.

    The generated research prose is deliberately excluded: a model-written URL is
    not evidence that Google Search actually grounded that URL.
    """

    grounded: set[str] = set()
    registry_line = re.compile(
        r"^-\s+\[W\d+\]\s+.*?\s+—\s+(?P<url>https?://\S+?)(?:\s+\[HTTP\s+(?P<status>\d{3})\])?(?:\s|$)",
        re.IGNORECASE,
    )
    for source in sources:
        if source.name != "public_research.md" or "## 공개 출처" not in source.content:
            continue
        registry = source.content.rsplit("## 공개 출처", 1)[-1]
        for raw in registry.splitlines():
            match = registry_line.match(raw.strip())
            if not match:
                continue
            status = int(match.group("status")) if match.group("status") else None
            if status is None or 200 <= status < 400:
                grounded.add(_normalize_url(match.group("url")))
            for url_match in _URL.finditer(raw):
                url = url_match.group(0).rstrip(".,;])")
                if (urlparse(url).hostname or "").casefold() == "vertexaisearch.cloud.google.com":
                    grounded.add(_normalize_url(url))
    return grounded


def _grounded_source_ids(sources: list[InternalSource]) -> set[str]:
    """Return only W-prefixed identifiers bound to URLs in the source registry."""

    grounded: set[str] = set()
    registry_line = re.compile(
        r"^-?\s*\[(?P<source_id>W\d{2,})\]\s+.*?\s+—\s+(?P<url>https?://\S+)",
        re.IGNORECASE,
    )
    for source in sources:
        if source.name != "public_research.md" or "## 공개 출처" not in source.content:
            continue
        registry = source.content.rsplit("## 공개 출처", 1)[-1]
        for raw in registry.splitlines():
            match = registry_line.match(raw.strip())
            if match and urlparse(match.group("url")).hostname:
                grounded.add(match.group("source_id").casefold())
    return grounded


def _successful_grounded_source_registry(sources: list[InternalSource]) -> dict[str, str]:
    """Return only inspectable W IDs and their successful resolved public URL."""

    grounded: dict[str, str] = {}
    registry_line = re.compile(
        r"^-?\s*\[(?P<source_id>W\d{2,})\].*?"
        r"(?P<url>https?://[^\s\]]+)(?:\s+\[HTTP\s+(?P<status>\d{3})\])?",
        re.IGNORECASE,
    )
    for source in sources:
        if source.name != "public_research.md" or "## 공개 출처" not in source.content:
            continue
        registry = source.content.rsplit("## 공개 출처", 1)[-1]
        for raw in registry.splitlines():
            match = registry_line.match(raw.strip())
            if not match or not urlparse(match.group("url")).hostname:
                continue
            status = int(match.group("status")) if match.group("status") else None
            if status is not None and not 200 <= status < 400:
                continue
            grounded[match.group("source_id").casefold()] = match.group("url").rstrip(".,;])")
    return grounded


def _grounded_source_urls(sources: list[InternalSource]) -> set[str]:
    return {
        _normalize_url(url)
        for url in _successful_grounded_source_registry(sources).values()
    }


def _grounded_source_ids(sources: list[InternalSource]) -> set[str]:
    return set(_successful_grounded_source_registry(sources))


def append_grounded_public_source_registry(
    sources: list[InternalSource],
    draft: DraftArtifact,
) -> DraftArtifact:
    """Expose the trusted public-source registry without asking the maker to copy it."""

    trusted_lines: list[str] = []
    grounded_ids = _grounded_source_ids(sources)
    for source in sources:
        if source.name != "public_research.md" or "## 공개 출처" not in source.content:
            continue
        registry = source.content.rsplit("## 공개 출처", 1)[-1]
        for raw in registry.splitlines():
            line = raw.strip()
            source_ids = {item.casefold() for item in _WEB_SOURCE_CITATION.findall(line)}
            if source_ids & grounded_ids and _URL.search(line):
                trusted_lines.append(line)
    missing_lines = [
        line for line in dict.fromkeys(trusted_lines)
        if not any(url in draft.body_markdown for url in _URL.findall(line))
    ]
    if not missing_lines:
        return draft
    body = draft.body_markdown.rstrip()
    registry = "\n".join(missing_lines)
    return draft.model_copy(update={
        "body_markdown": f"{body}\n\n## Verified public sources\n\n{registry}\n",
    })


def _unbounded_negative_segments(markdown: str) -> list[str]:
    segments = [
        item.strip()
        for item in re.split(r"\n\s*\n|(?<=[.!?。])\s+", markdown)
        if item.strip()
    ]
    return [
        item for item in segments
        if _NEGATIVE_ABSOLUTE.search(item) and not _BOUNDED_UNCERTAINTY.search(item)
    ]


def _claim_excerpt(segment: str, pattern: re.Pattern[str], *, width: int = 240) -> str:
    normalized = re.sub(r"\s+", " ", segment).strip()
    match = pattern.search(normalized)
    if match is None or len(normalized) <= width:
        return normalized[:width]
    padding = max(20, (width - len(match.group(0))) // 2)
    start = max(0, match.start() - padding)
    end = min(len(normalized), match.end() + padding)
    excerpt = normalized[start:end]
    return f"{'…' if start else ''}{excerpt}{'…' if end < len(normalized) else ''}"


def _uncited_material_claims(
    markdown: str,
    *,
    grounded_source_ids: set[str] | None = None,
    allow_labeled_estimates: bool = False,
    allow_bounded_inference: bool = False,
) -> list[str]:
    claims: list[str] = []
    allowed_source_ids = grounded_source_ids or set()
    lines = markdown.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        is_table_header = (
            "|" in line
            and bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", next_line))
        )
        has_grounded_web_citation = any(
            match.casefold() in allowed_source_ids
            for match in _WEB_SOURCE_CITATION.findall(line)
        )
        has_supported_mixed_citation = any(
            any(
                token.strip().casefold().startswith("f")
                or token.strip().casefold() in allowed_source_ids
                for token in group.split(",")
            )
            for group in _MIXED_CITATION_GROUP.findall(line)
        )
        if (
            not line
            or line.startswith("#")
            or _LABEL_ONLY.fullmatch(line)
            or is_table_header
            or re.fullmatch(r"[-|: ]+", line)
            or not _MATERIAL_CLAIM.search(line)
            or _URL.search(line)
            or _FINDING_CITATION.search(line)
            or has_grounded_web_citation
            or has_supported_mixed_citation
            or (allow_labeled_estimates and _ESTIMATE_UNCERTAINTY.search(line))
            or (
                allow_bounded_inference
                and
                _HYPOTHESIS_MARKER.search(line)
                and _VALIDATION_BOUNDARY.search(line)
            )
            or _PROPOSAL_SCOPE_STATEMENT.search(line)
            or _NORMATIVE_TEST_PROCEDURE.search(line)
            or _NORMATIVE_CHECKLIST.search(line)
            or _EVALUATION_METRIC.fullmatch(line)
        ):
            continue
        claims.append(re.sub(r"\s+", " ", line)[:240])
    return claims


def validate_evidence_sufficiency(
    intake: IntakeRequest,
    requirements: RequirementsAnalysis,
    sources: list[InternalSource],
    draft: DraftArtifact,
    analysis: AnalysisPackage | None = None,
) -> EvidenceSufficiencyVerification:
    """Reject research prose that overclaims or cannot expose its evidence."""

    issues: list[EvidenceSufficiencyIssue] = []
    body = draft.body_markdown
    has_public_research = any(source.name == "public_research.md" for source in sources)
    research_text = _research_text(requirements)
    assurance = resolve_assurance_policy(requirements)
    grounded_registry = _successful_grounded_source_registry(sources)
    grounded_source_ids = set(grounded_registry)

    forbidden_disclosures = [
        term for term in intake.forbidden_output_terms
        if term.casefold() in body.casefold()
    ]
    if forbidden_disclosures:
        issues.append(EvidenceSufficiencyIssue(
            kind=EvidenceSufficiencyIssueKind.FORBIDDEN_OUTPUT_DISCLOSURE,
            message=(
                "The deliverable contains user-configured private decision context. Remove every forbidden "
                "disclosure from the public artifact while preserving the resulting product requirement."
            ),
        ))

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
        requires_table = bool(re.search(r"비교\s*(?:분석\s*)?(?:표|테이블)|\btable\b", research_text, re.IGNORECASE))
        candidate_urls = _table_row_direct_urls(body) if requires_table else _direct_urls(body)
        grounded_source_urls = _grounded_source_urls(sources)
        grounded_urls = {
            _normalize_url(url) for url in candidate_urls
            if not grounded_source_urls or _normalize_url(url) in grounded_source_urls
        }
        ungrounded_urls = {
            url for url in candidate_urls
            if grounded_source_urls and _normalize_url(url) not in grounded_source_urls
        }
        observed = len(grounded_urls)
        if observed < required:
            ungrounded = len(ungrounded_urls)
            issues.append(EvidenceSufficiencyIssue(
                kind=EvidenceSufficiencyIssueKind.QUANTIFIED_EVIDENCE_SHORTFALL,
                message=(
                    f"The completion contract requires at least {required} individually inspectable "
                    f"research items, but only {observed} distinct item-level source URLs were found "
                    "and exactly bound to an observed Google Search source URL"
                    f"{' on comparison-table data rows' if requires_table else ''}. "
                    f"{ungrounded} additional model-written URL(s) were ignored because the exact URLs "
                    "were absent from that registry. Homepage links, categories, or market segments "
                    "do not count as named items."
                ),
            ))
        if ungrounded_urls:
            examples = ", ".join(sorted(ungrounded_urls)[:5])
            issues.append(EvidenceSufficiencyIssue(
                kind=EvidenceSufficiencyIssueKind.UNGROUNDED_MODEL_URL,
                message=(
                    "Comparison evidence contains model-written URLs that were not observed in the "
                    f"Google Search grounding registry. Replace or remove every ungrounded URL: {examples}"
                ),
            ))

    negative_segments = _unbounded_negative_segments(body) if has_public_research else []
    if negative_segments:
        examples = " | ".join(
            _claim_excerpt(segment, _NEGATIVE_ABSOLUTE)
            for segment in negative_segments[:3]
        )
        issues.append(EvidenceSufficiencyIssue(
            kind=EvidenceSufficiencyIssueKind.UNBOUNDED_ABSENCE_CLAIM,
            message=(
                "The artifact makes an absolute absence, uniqueness, or exclusivity claim. Replace it "
                "with a bounded search-scope conclusion, list the compared named candidates and sources, "
                "and preserve uncertainty in the same claim; novelty alone does not prove that no equivalent exists. "
                f"Remove or rewrite every quoted offending segment: {examples}"
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

    if _CAUSAL_ABSOLUTE.search(body):
        issues.append(EvidenceSufficiencyIssue(
            kind=EvidenceSufficiencyIssueKind.UNSUPPORTED_CAUSAL_ABSOLUTE,
            message=(
                "The artifact turns a plausible hygiene or clinical benefit into an absolute causal claim. "
                "Use bounded positive wording, distinguish the proposal rationale from a proven outcome, "
                "and state the pilot or validation boundary needed before public use."
            ),
        ))

    if analysis is not None and has_public_research:
        used_finding_ids = {
            item.casefold() for item in re.findall(r"\bF\d{2,}\b", body, re.IGNORECASE)
        }
        findings = {
            finding.finding_id.casefold(): finding
            for finding in analysis.findings
            if finding.source_name == "public_research.md"
        }
        untraceable: list[str] = []
        for finding_id in sorted(used_finding_ids & findings.keys()):
            refs = {item.casefold() for item in findings[finding_id].source_refs}
            if not refs & grounded_source_ids:
                untraceable.append(findings[finding_id].finding_id)
        if untraceable:
            issues.append(EvidenceSufficiencyIssue(
                kind=EvidenceSufficiencyIssueKind.UNTRACEABLE_FINDING,
                message=(
                    "The artifact cites public-research findings that are not traceable to a successful "
                    "grounded W source: " + ", ".join(untraceable) + ". Re-run research or remove the "
                    "unsupported finding; an F identifier alone is not evidence."
                ),
            ))

        ungrounded_regulatory_lines: list[str] = []
        for raw_line in body.splitlines():
            if not _REGULATORY_CLAIM.search(raw_line):
                continue
            cited_findings = {
                item.casefold() for item in re.findall(r"\bF\d{2,}\b", raw_line, re.IGNORECASE)
            }
            if not cited_findings:
                continue
            referenced_urls = [
                grounded_registry[ref]
                for finding_id in cited_findings
                if finding_id in findings
                for ref in {item.casefold() for item in findings[finding_id].source_refs}
                if ref in grounded_registry
            ]
            if not any(
                _PRIMARY_AUTHORITY_HOST.search((urlparse(url).hostname or "").casefold())
                for url in referenced_urls
            ):
                ungrounded_regulatory_lines.append(re.sub(r"\s+", " ", raw_line).strip()[:200])
        if ungrounded_regulatory_lines:
            issues.append(EvidenceSufficiencyIssue(
                kind=EvidenceSufficiencyIssueKind.MISSING_PRIMARY_REGULATORY_SOURCE,
                message=(
                    "A legal or regulatory conclusion is not bound to a successful primary-authority "
                    "source. Use the governing statute, regulator, or official standard, or present the "
                    "classification as an unresolved question. Examples: "
                    + " | ".join(ungrounded_regulatory_lines[:3])
                ),
            ))

    scope_text = "\n".join(filter(None, [intake.goal, intake.desired_output or ""]))
    if _SCOPE_LIMIT.search(scope_text) and _SCOPE_LEAKAGE.search(body):
        issues.append(EvidenceSufficiencyIssue(
            kind=EvidenceSufficiencyIssueKind.OUT_OF_SCOPE_FOLLOWUP,
            message=(
                "The user set an explicit scope ceiling, but the artifact adds a follow-up, implementation, "
                "production, or next-step section. Remove work outside the approved deliverable."
            ),
        ))

    uncited = (
        _uncited_material_claims(
            body,
            grounded_source_ids=grounded_source_ids,
            allow_labeled_estimates=assurance.allow_labeled_estimates,
            allow_bounded_inference=assurance.allow_bounded_inference,
        )
        if has_public_research else []
    )
    if uncited:
        examples = " | ".join(uncited[:3])
        issues.append(EvidenceSufficiencyIssue(
            kind=EvidenceSufficiencyIssueKind.UNCITED_MATERIAL_CLAIM,
            message=(
                "Research-backed material claims must carry an F-prefixed finding citation, a grounded "
                "W-prefixed source ID, or a source URL on the same row or paragraph. If the supplied findings "
                "do not directly support a claim, that does not prove the claim false. Either remove it, "
                "replace the unsupported value with an explicit RFQ or verification input, or label a plausible "
                "causal idea as a hypothesis with a concrete test/pilot boundary and no pre-validation marketing claim; "
                f"never attach an unrelated citation. Uncited examples: {examples}"
            ),
        ))

    return EvidenceSufficiencyVerification(
        issues=issues,
        verdict_override=Verdict.REVISE if issues else None,
    )


_RESEARCH_REENTRY_KINDS = frozenset({
    EvidenceSufficiencyIssueKind.MISSING_DIRECT_SOURCE,
    EvidenceSufficiencyIssueKind.QUANTIFIED_EVIDENCE_SHORTFALL,
    EvidenceSufficiencyIssueKind.UNBOUNDED_ABSENCE_CLAIM,
    EvidenceSufficiencyIssueKind.UNSUPPORTED_SAFETY_ABSOLUTE,
    EvidenceSufficiencyIssueKind.UNGROUNDED_MODEL_URL,
    EvidenceSufficiencyIssueKind.UNTRACEABLE_FINDING,
    EvidenceSufficiencyIssueKind.MISSING_PRIMARY_REGULATORY_SOURCE,
})

_INVESTIGATOR_REENTRY_KINDS = frozenset({
    EvidenceSufficiencyIssueKind.MISSING_DIRECT_SOURCE,
    EvidenceSufficiencyIssueKind.QUANTIFIED_EVIDENCE_SHORTFALL,
    EvidenceSufficiencyIssueKind.UNGROUNDED_MODEL_URL,
    EvidenceSufficiencyIssueKind.UNTRACEABLE_FINDING,
    EvidenceSufficiencyIssueKind.MISSING_PRIMARY_REGULATORY_SOURCE,
})


def investigator_reentry_issues(
    evidence: EvidenceSufficiencyVerification,
) -> list[EvidenceSufficiencyIssue]:
    """Return defects owned by research/analysis rather than artifact prose."""

    return [
        item for item in evidence.issues
        if item.kind in _INVESTIGATOR_REENTRY_KINDS
    ]


def research_reentry_issues(
    intake: IntakeRequest,
    requirements: RequirementsAnalysis,
    research_markdown: str,
    grounded_sources: list[PublicWebSource] | None = None,
) -> list[EvidenceSufficiencyIssue]:
    """Return evidence defects that require new research rather than maker prose repair."""

    source = (
        PublicResearchResult(
            query=intake.goal,
            answer_markdown=research_markdown,
            sources=grounded_sources,
        ).as_internal_source()
        if grounded_sources else InternalSource(
            name="public_research.md",
            priority="mandatory",
            requirement_keys=["public_research"],
            content=research_markdown,
        )
    )
    probe = DraftArtifact(
        title="Public research evidence probe",
        # At investigator re-entry the grounded registry is part of the
        # research artifact. Validate that complete evidence surface instead
        # of the prose alone, or a valid direct URL in the registry is
        # incorrectly reported as missing.
        body_markdown=source.content if grounded_sources else research_markdown,
        cited_finding_ids=["F00"],
        drafting_decisions=["Synthetic probe used only for deterministic evidence routing."],
    )
    result = validate_evidence_sufficiency(
        intake, requirements, [source], probe
    )
    return [item for item in result.issues if item.kind in _RESEARCH_REENTRY_KINDS]


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
