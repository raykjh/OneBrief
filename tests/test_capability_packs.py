from onebrief.capability_packs import (
    BUILTIN_CAPABILITY_PACKS,
    CapabilityPackId,
    ExecutionPlacement,
    capability_pack_execution_placement,
    capability_pack_refs_require_approved_host,
    capability_pack_refs_for_adapter_ids,
    validate_capability_pack_refs,
)


def test_unity_capabilities_are_versioned_reusable_components() -> None:
    adapters = [
        "repository_snapshot",
        "unity_compile",
        "unity_editmode_tests",
        "unity_playmode_visual_tests",
        "unity_layout_diagnostics",
    ]

    refs = capability_pack_refs_for_adapter_ids(adapters)

    assert [item.pack_id for item in refs] == [
        CapabilityPackId.REPOSITORY_CONTROL,
        CapabilityPackId.UNITY_CONTROL,
        CapabilityPackId.UNITY_LAYOUT_DIAGNOSTICS,
    ]
    assert validate_capability_pack_refs(refs, adapters) == []
    assert all(
        item.definition_sha256 == BUILTIN_CAPABILITY_PACKS[item.pack_id].sha256
        for item in refs
    )


def test_changed_or_missing_capability_component_invalidates_composition() -> None:
    adapters = ["repository_snapshot", "unity_compile"]
    refs = capability_pack_refs_for_adapter_ids(adapters)
    stale = refs[0].model_copy(update={"definition_sha256": "0" * 64})

    errors = validate_capability_pack_refs([stale, *refs[1:]], adapters)
    missing = validate_capability_pack_refs(refs[:1], adapters)

    assert errors == [
        "stale capability pack binding: repository-control",
        "adapters without a capability pack: repository_snapshot",
    ]
    assert missing == ["adapters without a capability pack: unity_compile"]


def test_unity_capabilities_are_bound_to_the_approved_host() -> None:
    refs = capability_pack_refs_for_adapter_ids([
        "repository_snapshot",
        "unity_compile",
        "unity_layout_diagnostics",
    ])

    assert capability_pack_execution_placement(
        CapabilityPackId.REPOSITORY_CONTROL
    ) is ExecutionPlacement.PORTABLE
    assert capability_pack_execution_placement(
        CapabilityPackId.UNITY_CONTROL
    ) is ExecutionPlacement.APPROVED_HOST
    assert capability_pack_refs_require_approved_host(refs) is True
