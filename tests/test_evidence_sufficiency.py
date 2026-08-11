from onebrief.evidence_sufficiency import (
    apply_evidence_sufficiency_override,
    validate_evidence_sufficiency,
)
from onebrief.execution_schemas import DraftArtifact, VerificationReport
from onebrief.schemas import (
    CompletionContract,
    IntakeRequest,
    InternalSource,
    QualityCriterion,
    RequirementsAnalysis,
    SourcePriority,
)


def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Supported.",
        normalized_goal="Compare current candidates.",
        deliverables=["Comparison"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Compare at least three named products."],
        completion_contract=CompletionContract(
            target_state="A sourced comparison is complete.",
            quality_criteria=[QualityCriterion(
                criterion_id="Q01",
                description="기존 시장 유사 제품 조사 및 차별성 검증",
                evidence_required="국내 유통 중인 유사 제품 3종 이상과의 비교 분석표",
            )],
        ),
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _public_source() -> InternalSource:
    return InternalSource(
        name="public_research.md",
        priority=SourcePriority.MANDATORY,
        content="Grounded research\n\n[W01] Example — https://example.com/one",
    )


def _draft(body: str) -> DraftArtifact:
    return DraftArtifact(
        title="Comparison",
        body_markdown=body,
        cited_finding_ids=["F01"],
        drafting_decisions=[],
    )


def test_rejects_category_comparison_as_named_product_evidence() -> None:
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="유사 제품이 없으면 후보를 추천해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft("# 결과\n\n| 구분 | 비교 |\n|---|---|\n| 루테인 제품군 | 차별화 |\n\n유사 제품이 없는 독점적 후보입니다."),
    )

    kinds = {item.kind.value for item in result.issues}
    assert "missing_direct_source" in kinds
    assert "quantified_evidence_shortfall" in kinds
    assert "unbounded_absence_claim" in kinds
    assert result.verdict_override == "REVISE"


def test_accepts_three_named_source_linked_rows_with_bounded_wording() -> None:
    body = """# 결과

| 제품 | 출처 |
|---|---|
| Alpha | https://example.com/products/a |
| Beta | https://example.com/products/b |
| Gamma | https://example.com/products/c |

이 검색 범위에서 동등한 조합은 확인되지 않았으며 추가 확인이 필요합니다. [F01]
"""
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(body),
    )

    assert result.issues == []
    assert result.verdict_override is None


def test_scope_and_safety_absolutes_override_model_pass() -> None:
    evidence = validate_evidence_sufficiency(
        IntakeRequest(goal="제품 콘셉트까지만 정해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft("# 콘셉트\n\n부작용 없는 안전한 제품입니다. https://example.com/a\n\n## 후속 작업\n생산합니다."),
    )
    report = apply_evidence_sufficiency_override(
        VerificationReport(
            verdict="PASS",
            criterion_checks=[{"criterion_id": "Q01", "criterion": "Comparison", "passed": True, "evidence": "Looks complete."}],
            blocking_issues=[],
            revision_instructions=[],
            missing_information=[],
        ),
        evidence,
    )

    assert report.verdict == "REVISE"
    assert any("scope ceiling" in item for item in report.revision_instructions)
    assert any("absolute safety" in item for item in report.revision_instructions)


def test_homepages_and_unrelated_uncertainty_do_not_prove_item_level_comparison() -> None:
    body = """# 결과

| 항목 | 제품 A | 제품 B | 제품 C |
|---|---|---|---|
| 출처 | https://shop.example.com | https://compare.example.com/ | https://news.example.com |

다른 수치는 확인 필요입니다.

이 후보는 기존 시장에 유사 제품이 없는 독점적 제안입니다. [F01]
"""
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="유사 제품을 조사하고 콘셉트까지만 정해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(body),
    )

    kinds = {item.kind.value for item in result.issues}
    assert "quantified_evidence_shortfall" in kinds
    assert "unbounded_absence_claim" in kinds


def test_uncited_material_claim_is_rejected_at_row_or_paragraph_level() -> None:
    body = """# 결과

| 제품 | 출처 |
|---|---|
| Alpha | https://example.com/products/a |
| Beta | https://example.com/products/b |
| Gamma | https://example.com/products/c |

이 성분은 피로를 개선하고 위험을 감소시킵니다.
"""
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(body),
    )

    assert any(item.kind.value == "uncited_material_claim" for item in result.issues)


def test_grouped_finding_citations_count_as_same_paragraph_evidence() -> None:
    body = """# 결과

| 제품 | 출처 |
|---|---|
| Alpha | https://example.com/products/a |
| Beta | https://example.com/products/b |
| Gamma | https://example.com/products/c |

이 성분은 피로 개선에 도움을 줄 수 있습니다. [F01, F04]
"""
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(body),
    )

    assert not any(item.kind.value == "uncited_material_claim" for item in result.issues)


def test_markdown_table_header_is_not_treated_as_an_uncited_material_claim() -> None:
    body = """# 결과

| 성분 구분 | 원료명 | 식약처 공시 기능성 내용 |
|---|---|---|
| 주성분 | Alpha | 피로 개선에 도움 [F01] |

| 제품 | 출처 |
|---|---|
| Alpha | https://example.com/products/a |
| Beta | https://example.com/products/b |
| Gamma | https://example.com/products/c |
"""
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(body),
    )

    assert not any(item.kind.value == "uncited_material_claim" for item in result.issues)


def test_scope_gate_allows_filing_classification_but_rejects_filing_action() -> None:
    allowed = validate_evidence_sufficiency(
        IntakeRequest(goal="제품 콘셉트까지만 정해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(
            "# 콘셉트\n\n제품 분류: 건강기능식품(식약처 품목제조신고 대상) [F01]\n\n"
            "| 제품 | 출처 |\n|---|---|\n"
            "| Alpha | https://example.com/products/a |\n"
            "| Beta | https://example.com/products/b |\n"
            "| Gamma | https://example.com/products/c |"
        ),
    )
    rejected = validate_evidence_sufficiency(
        IntakeRequest(goal="제품 콘셉트까지만 정해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(
            "# 콘셉트\n\n품목제조신고를 진행합니다. [F01]\n\n"
            "| 제품 | 출처 |\n|---|---|\n"
            "| Alpha | https://example.com/products/a |\n"
            "| Beta | https://example.com/products/b |\n"
            "| Gamma | https://example.com/products/c |"
        ),
    )

    assert not any(item.kind.value == "out_of_scope_followup" for item in allowed.issues)
    assert any(item.kind.value == "out_of_scope_followup" for item in rejected.issues)
