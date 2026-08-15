from onebrief.evidence_sufficiency import (
    append_grounded_public_source_registry,
    apply_evidence_sufficiency_override,
    research_reentry_issues,
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


def test_unbounded_absence_issue_quotes_the_exact_offending_segment() -> None:
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(
            "| 제품 | 출처 |\n|---|---|\n"
            "| Alpha | https://example.com/products/a |\n"
            "| Beta | https://example.com/products/b |\n"
            "| Gamma | https://example.com/products/c |\n\n"
            "이 조합은 기존 시장에 유사 제품이 없습니다. [F01]"
        ),
    )

    issue = next(
        item for item in result.issues
        if item.kind.value == "unbounded_absence_claim"
    )
    assert "기존 시장에 유사 제품이 없습니다" in issue.message


def test_model_written_product_urls_do_not_count_without_grounding_binding() -> None:
    source = InternalSource(
        name="public_research.md",
        priority=SourcePriority.MANDATORY,
        content=(
            "Research prose may mention https://invented.example/products/a\n\n"
            "## 공개 출처\n"
            "- [W01] verified.example — https://verified.example/products/a [HTTP 200] "
            "(Google grounding: https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc)"
        ),
    )
    body = """# 결과

| 제품 | 출처 |
|---|---|
| Alpha | https://verified.example/products/a |
| Beta | https://invented.example/products/b |
| Gamma | https://another-fake.example/products/c |
"""
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [source],
        _draft(body),
    )

    issue = next(
        item for item in result.issues
        if item.kind.value == "quantified_evidence_shortfall"
    )
    assert "only 1" in issue.message
    assert "2 additional model-written URL(s) were ignored" in issue.message


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


def test_exact_grounding_preserves_case_sensitive_paths_and_query_keys() -> None:
    source = InternalSource(
        name="public_research.md",
        priority=SourcePriority.MANDATORY,
        content=(
            "Grounded research\n\n## 공개 출처\n"
            "- [W01] Alpha — https://example.com/Product/View?goodsNo=101 [HTTP 200]\n"
            "- [W02] Beta — https://example.com/Product/View?goodsNo=102 [HTTP 200]\n"
            "- [W03] Gamma — https://example.com/Product/View?goodsNo=103 [HTTP 200]"
        ),
    )
    body = """| 제품 | 출처 |
|---|---|
| Alpha | https://example.com/Product/View?goodsNo=101 |
| Beta | https://example.com/Product/View?goodsNo=102 |
| Gamma | https://example.com/Product/View?goodsNo=103 |
"""

    result = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [source],
        _draft(body),
    )

    assert not any(
        item.kind.value in {"quantified_evidence_shortfall", "ungrounded_model_url"}
        for item in result.issues
    )


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

    issue = next(item for item in result.issues if item.kind.value == "uncited_material_claim")
    assert "explicit RFQ or verification input" in issue.message
    assert "unrelated citation" in issue.message


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


def test_grounded_web_source_id_counts_but_invented_id_does_not() -> None:
    source = InternalSource(
        name="public_research.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["research"],
        content=(
            "Research summary.\n\n## 공개 출처\n"
            "- [W01] Example — https://example.com/products/a [HTTP 200]"
        ),
    )
    grounded = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [source],
        _draft(
            "이 제품은 가격 위험을 줄일 수 있습니다. [W01]\n\n"
            "https://example.com/products/a"
        ),
    )
    invented = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [source],
        _draft(
            "이 제품은 가격 위험을 줄일 수 있습니다. [W99]\n\n"
            "https://example.com/products/a"
        ),
    )

    assert not any(item.kind.value == "uncited_material_claim" for item in grounded.issues)
    assert any(item.kind.value == "uncited_material_claim" for item in invented.issues)


def test_explicit_estimate_disclaimer_is_not_itself_an_uncited_price_claim() -> None:
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="시장 가격을 조사해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(
            "아래 가격은 시장 추정치이며 실제 발주 전 서면 견적으로 확인 필요합니다.\n\n"
            "https://example.com/products/a"
        ),
    )

    assert not any(item.kind.value == "uncited_material_claim" for item in result.issues)


def test_trusted_public_source_registry_is_appended_without_model_copying() -> None:
    source = InternalSource(
        name="public_research.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["research"],
        content=(
            "Research summary.\n\n## 공개 출처\n"
            "- [W01] Alpha — https://example.com/products/a [HTTP 200]\n"
            "- [W02] Beta — https://example.com/products/b [HTTP 200]"
        ),
    )
    draft = _draft(
        "# 결과\n\n근거에 따른 결론입니다. [W01] "
        "이 문장은 검증된 공개 출처 레지스트리가 결정론적으로 첨부되는지 확인하기 위한 "
        "충분한 길이의 일반 문서 본문입니다."
    )

    appended = append_grounded_public_source_registry([source], draft)
    appended_twice = append_grounded_public_source_registry([source], appended)

    assert "## 검증된 공개 출처" in appended.body_markdown
    assert "https://example.com/products/a" in appended.body_markdown
    assert "https://example.com/products/b" in appended.body_markdown
    assert appended_twice.body_markdown == appended.body_markdown


def test_proposal_scope_and_test_method_are_not_treated_as_research_claims() -> None:
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="사용성 시험 제안서를 작성해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(
            "본 제안서는 고령 사용자를 대상으로 안전한 건조 경험을 시험하도록 기획했습니다.\n\n"
            "보풀 테스트는 샘플을 문지른 뒤 탈락 섬유를 관찰하여 피부 자극 가능성을 확인합니다.\n\n"
            "https://example.com/products/a"
        ),
    )

    assert not any(item.kind.value == "uncited_material_claim" for item in result.issues)


def test_rfq_checklist_instruction_is_not_a_research_claim() -> None:
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="OEM RFQ 체크리스트를 작성해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(
            "OEM 제조사 선정 및 RFQ 발송 시 확인해야 할 기술적 요구사항입니다.\n\n"
            "https://example.com/products/a"
        ),
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


def test_scope_gate_allows_requested_pilot_and_registration_plan_without_execution() -> None:
    result = validate_evidence_sufficiency(
        IntakeRequest(
            goal=(
                "제품 제안서와 단계별 파일럿 계획을 작성하되 공급사에 연락하거나 "
                "제품을 등록하지 마."
            ),
            desired_output="OEM 견적 요청 체크리스트, 품목제조신고 절차, 단계별 파일럿 계획",
            public_research_allowed=True,
        ),
        _requirements(),
        [_public_source()],
        _draft(
            "# 단계별 파일럿 계획\n\n"
            "1단계는 OEM 견적 요청 체크리스트를 확정한다.\n\n"
            "2단계는 위생용품 품목제조신고 절차와 필요 서류를 검토한다.\n\n"
            "실제 연락, 신청, 생산은 이 제안서 범위에서 수행하지 않는다. [F01]\n\n"
            "| 제품 | 출처 |\n|---|---|\n"
            "| Alpha | https://example.com/products/a |\n"
            "| Beta | https://example.com/products/b |\n"
            "| Gamma | https://example.com/products/c |"
        ),
    )

    assert not any(item.kind.value == "out_of_scope_followup" for item in result.issues)


def test_reference_list_urls_do_not_satisfy_a_comparison_table_contract() -> None:
    body = """# 결과

| 제품 | 기능성 |
|---|---|
| Alpha | 피로 개선 [F01] |
| Beta | 항산화 [F01] |
| Gamma | 눈 건강 [F01] |

## 참고 문헌
- https://example.com/sources/one
- https://example.com/sources/two
- https://example.com/sources/three
"""
    result = validate_evidence_sufficiency(
        IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True),
        _requirements(),
        [_public_source()],
        _draft(body),
    )

    assert any(item.kind.value == "quantified_evidence_shortfall" for item in result.issues)


def test_research_reentry_only_clears_after_item_rows_carry_direct_evidence() -> None:
    unsupported = """| 제품 | 기능성 |
|---|---|
| Alpha | 피로 개선 |
| Beta | 항산화 |
| Gamma | 눈 건강 |

참고: https://example.com/sources/catalog
"""
    supported = """| 제품 | 기능성 | 직접 출처 |
|---|---|---|
| Alpha | 피로 개선 | https://example.com/products/a |
| Beta | 항산화 | https://example.com/products/b |
| Gamma | 눈 건강 | https://example.com/products/c |
"""
    intake = IntakeRequest(goal="후보를 조사해줘.", public_research_allowed=True)

    assert any(
        item.kind.value == "quantified_evidence_shortfall"
        for item in research_reentry_issues(intake, _requirements(), unsupported)
    )
    assert research_reentry_issues(intake, _requirements(), supported) == []
