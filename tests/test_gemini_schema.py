from onebrief.gemini_schema import gemini_compatible_model, restore_nullable_values
from onebrief.runner import _normalize_requirements_payload
from onebrief.generic_development_toolpack import ProjectCodeChangeSet
from onebrief.schemas import RequirementsAnalysis


def test_gemini_transport_schema_removes_provider_incompatible_constraints():
    transport = gemini_compatible_model(RequirementsAnalysis)
    schema = transport.model_json_schema()
    rendered = str(schema)

    for keyword in ("minLength", "maxLength", "minItems", "maxItems", "pattern", "anyOf", "default"):
        assert keyword not in rendered
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["completion_contract"]["$ref"].startswith("#/$defs/")


def test_transport_result_is_still_checked_by_strict_domain_model():
    transport = gemini_compatible_model(RequirementsAnalysis)
    payload = {
        "supported": True,
        "support_reason": "ok",
        "normalized_goal": "valid goal",
        "deliverables": ["artifact"],
        "mandatory_information": [],
        "optional_information": [],
        "acceptance_criteria": ["criterion"],
        "completion_contract": {
            "target_state": "finished",
            "quality_criteria": [{
                "criterion_id": "bad",
                "description": "valid description",
                "evaluation_mode": "deterministic",
                "evidence_required": "test evidence",
                "required": True,
            }],
            "pass_condition": "all pass",
        },
        "sixsense": {
            "standard_profile": "Use a conventional professional standard.",
            "questions": [],
            "interaction_target_seconds": 30,
        },
        "assumptions": [],
        "consolidated_questions": [],
        "ready_for_estimate": True,
    }

    loose = transport.model_validate(payload)
    try:
        RequirementsAnalysis.model_validate(loose.model_dump(mode="json"))
    except ValueError:
        pass
    else:
        raise AssertionError("strict domain validation must reject the bad criterion id")


def test_provider_quality_ids_are_normalized_before_strict_validation():
    payload = {
        "completion_contract": {
            "quality_criteria": [
                {"criterion_id": "unity_compilation"},
                {"criterion_id": "localization_verification"},
            ]
        }
    }

    normalized = _normalize_requirements_payload(payload)
    assert [item["criterion_id"] for item in normalized["completion_contract"]["quality_criteria"]] == ["Q01", "Q02"]


def test_provider_payload_normalization_repairs_bounded_shape_variation():
    payload = {
        "supported": True,
        "support_reason": "x" * 700,
        "normalized_goal": "g" * 1400,
        "deliverables": ["artifact"],
        "mandatory_information": [{
            "key": "Unity-??",
            "request": "r" * 700,
            "reason": "needed",
            "acceptable_evidence": ["evidence"],
        }],
        "optional_information": [],
        "acceptance_criteria": ["works"],
        "completion_contract": {
            "target_state": "done",
            "quality_criteria": [],
            "pass_condition": "pass",
        },
        "assumptions": [],
        "consolidated_questions": [],
        "ready_for_estimate": True,
    }

    normalized = _normalize_requirements_payload(payload)
    result = RequirementsAnalysis.model_validate(normalized)
    assert result.ready_for_estimate is False
    assert result.mandatory_information[0].key == "unity"
    assert result.consolidated_questions



def test_string_null_is_restored_for_nested_optional_hash():
    payload = {
        "schema_version": "onebrief-project-code-change-set-v1",
        "summary": "Add a localization file.",
        "changes": [{
            "path": "Assets/Localization/new.json",
            "base_sha256": "null",
            "content": "{}",
            "reason": "Add the requested translation table.",
        }],
    }

    restored = restore_nullable_values(ProjectCodeChangeSet, payload)
    result = ProjectCodeChangeSet.model_validate(restored)
    assert result.changes[0].base_sha256 is None
