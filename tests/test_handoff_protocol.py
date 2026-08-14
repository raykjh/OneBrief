import pytest

from onebrief.handoff_protocol import (
    EvidenceKind,
    EvidenceStatus,
    accept_work_handoff,
    create_evidence_binding,
    create_work_handoff,
    verify_handoff_receipt,
    verify_work_handoff,
)


def handoff():
    binding = create_evidence_binding(
        criterion_id="Q92",
        kind=EvidenceKind.VISUAL,
        status=EvidenceStatus.MISSING,
        summary="The login screenshot was not materialized.",
    )
    return create_work_handoff(
        project_id="julpae",
        milestone_id="M01",
        round_number=3,
        sender_agent_id="critic-01",
        recipient_agent_id="maker-01",
        stage="evidence_construction",
        goal_digest="a" * 64,
        completion_contract_digest="b" * 64,
        source_revision="c" * 40,
        owned_criterion_ids=["Q92"],
        preserve_passed_criterion_ids=["Q01", "Q91"],
        permitted_paths=["Assets/Tests/PlayMode/OneBrief.Visual.LoginTest.cs"],
        forbidden_path_patterns=["product source"],
        input_artifacts=[],
        evidence_bindings=[binding],
        expected_output_schema="UnityEvidenceSourceRepair",
        required_evidence=["artifact_materialization"],
    )


def test_digest_bound_handoff_and_receipt_round_trip() -> None:
    envelope = handoff()
    verify_work_handoff(envelope)
    receipt = accept_work_handoff(envelope, recipient_agent_id="maker-01")
    verify_handoff_receipt(envelope, receipt)
    assert receipt.understood_criterion_ids == ["Q92"]
    assert receipt.understood_permitted_paths == envelope.permitted_paths


def test_changed_handoff_or_wrong_recipient_is_rejected() -> None:
    envelope = handoff()
    with pytest.raises(PermissionError, match="digest"):
        verify_work_handoff(envelope.model_copy(update={"stage": "product_implementation"}))
    with pytest.raises(PermissionError, match="different recipient"):
        accept_work_handoff(envelope, recipient_agent_id="other-maker")


def test_owned_and_preserved_criteria_cannot_overlap() -> None:
    with pytest.raises(ValueError, match="cannot also be frozen"):
        handoff().model_copy(
            update={"preserve_passed_criterion_ids": ["Q01", "Q92"]}
        ).__class__.model_validate(
            handoff().model_copy(
                update={"preserve_passed_criterion_ids": ["Q01", "Q92"]}
            ).model_dump(mode="json")
        )
