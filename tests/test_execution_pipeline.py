import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import types
from pydantic import ValidationError

from onebrief.development_toolpack import CodeChangeSet, DevelopmentCommandResult, DevelopmentRun
from onebrief.generic_development_toolpack import (
    AtomicUnityEvidenceBundle,
    AnchoredRangeRepairProjectCodeChangeSet,
    CompactProposedProjectCodeChangeSet, ExactRepairProjectCodeChangeSet,
    CatalogAnchoredProductRepair,
    CatalogAnchoredEvidenceRepair,
    ProjectCodeChangeSet, ProposedProjectCodeChangeSet,
    UnityEvidenceSourceRepair,
    UnityEvidenceAnchoredSourceRepair,
    UnityRenderTextureEvidenceRepair,
    UnityEvidenceAssemblyRepair,
)
from onebrief.budget_guard import BudgetExceeded, BudgetStore, RunStatus
from onebrief.execution_pipeline import (
    ExecutionPipeline,
    development_proposal_changes,
    filter_development_proposal_for_phase,
    phase_owned_handoff_paths,
    paths_outside_active_repair_contract,
    development_toolpack_focus_text,
    development_repair_requires_anchored_range,
    evidence_repair_has_bounded_uncommitted_candidate,
    development_repair_difficulty,
    development_maker_schema_for,
    visual_repair_production_candidate,
    visual_repair_has_uncommitted_product_candidate,
    visual_repair_production_target_allowed,
    relevant_product_repair_sources,
    product_failure_edit_anchors,
    active_exact_edit_anchors,
    candidate_first_edit_anchors,
    is_unity_evidence_contract_feedback,
    is_development_product_target_failure,
    missing_unity_evidence_bundle_paths,
    normalize_atomic_unity_evidence_bundle,
    rollback_detached_development_changes,
    should_preserve_unity_evidence_checkpoint,
    is_unity_localization_product_failure,
    approved_runtime_authority_source,
    quest_initial_execution_phase,
    unity_evidence_contract_target_allowed,
)
from onebrief.execution_agents import DeveloperAgent
from onebrief.execution_limits import DEVELOPER_OUTPUT_CAP
from onebrief.guarded_gemini import BudgetedGeminiClient
from onebrief.unity_evidence_plan import (
    UnityEvidenceJourneyPlan,
    render_unity_evidence_journey,
)
from onebrief.convergence_policy import RepairContract
from onebrief.execution_schemas import (
    AnalysisPackage,
    DraftArtifact,
    PipelineStatus,
    VerificationReport,
    Verdict,
)
from onebrief.producer import estimate_budget
from onebrief.toolpack_lifecycle import ApprovedRuntimeArgument
from onebrief.schemas import (
    CompletionContract,
    IntakeRequest,
    InternalSource,
    OutputTarget,
    QualityCriterion,
    RequirementsAnalysis,
    SourcePriority,
    ToolPackId,
    ExecutionPhase,
)


def test_dict_development_proposal_cannot_bypass_phase_authority() -> None:
    proposal = {
        "summary": "mixed repair",
        "changes": [
            {"path": "Assets/Scripts/LoginView.cs", "content": "product"},
            {
                "path": "Assets/Tests/PlayMode/OneBriefVisualTests.cs",
                "content": "evidence",
            },
        ],
    }

    assert len(development_proposal_changes(proposal)) == 2
    filtered, deferred = filter_development_proposal_for_phase(
        proposal, ExecutionPhase.PRODUCT_IMPLEMENTATION
    )

    assert isinstance(filtered, dict)
    assert [item["path"] for item in filtered["changes"]] == [
        "Assets/Scripts/LoginView.cs"
    ]
    assert deferred == ["Assets/Tests/PlayMode/OneBriefVisualTests.cs"]


def test_active_quest_binds_initial_evidence_phase() -> None:
    payload = json.dumps({
        "schema_version": "onebrief-quest-contract-v1",
        "initial_execution_phase": "evidence_construction",
    })
    source = InternalSource(
        name="onebrief-active-quest-QC-0123456789abcdef.json",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["active_quest"],
        summary="Authorized current Quest.",
        content=payload,
        media_type="application/json",
        size_bytes=len(payload.encode("utf-8")),
        sha256="0" * 64,
    )

    assert quest_initial_execution_phase([source]) == ExecutionPhase.EVIDENCE_CONSTRUCTION


def test_missing_active_quest_keeps_legacy_product_phase() -> None:
    assert quest_initial_execution_phase([]) == ExecutionPhase.PRODUCT_IMPLEMENTATION


def test_approved_runtime_authentication_is_projected_as_mandatory_context() -> None:
    binding = ApprovedRuntimeArgument(
        adapter_id="unity_playmode_visual_tests",
        argument="--julpae-recording-profile",
        value="onebrief-evidence",
        source_paths=[
            "Assets/JULPAE/Scripts/Common/JulpaeRecordingProfile.cs",
            "Assets/JULPAE/Scripts/Login/LoginSceneController.cs",
        ],
        source_digest_sha256="1" * 64,
        authentication_selector="DevPanel/TestAccountDropdown",
        authentication_submit="DevPanel/DirectEnterButton",
    )
    pack = SimpleNamespace(_profile=lambda: SimpleNamespace(
        runtime_arguments=[binding], sha256="2" * 64,
    ))

    source = approved_runtime_authority_source(pack)

    assert source is not None
    assert source.priority == SourcePriority.MANDATORY
    payload = json.loads(source.content)
    assert payload["toolpack_sha256"] == "2" * 64
    assert payload["bindings"][0]["authentication_selector"] == (
        "DevPanel/TestAccountDropdown"
    )
    assert payload["bindings"][0]["authentication_submit"] == (
        "DevPanel/DirectEnterButton"
    )


def test_dict_development_proposal_counts_atomic_evidence_members() -> None:
    feedback = (
        "add a discoverable Unity PlayMode test and add a Unity test .asmdef"
    )
    proposal = {
        "changes": [
            {"path": "Assets/Tests/PlayMode/OneBriefVisualTests.cs"},
            {"path": "Assets/Tests/PlayMode/OneBrief.Visual.Tests.asmdef"},
        ]
    }

    assert missing_unity_evidence_bundle_paths(feedback, proposal) == []


def test_verification_report_discards_provider_artifact_digest_placeholder() -> None:
    report = VerificationReport.model_validate({
        "verdict": "PASS",
        "criterion_checks": [{
            "criterion_id": "Q01",
            "criterion": "Unity compile and PlayMode pass",
            "passed": True,
            "evidence": "Observed the trusted command receipt.",
            "evidence_bindings": [{
                "binding_id": "EB-0123456789abcdef",
                "criterion_id": "Q01",
                "kind": "compile",
                "status": "passed",
                "summary": "Provider observation only.",
                "artifact": {
                    "artifact_type": "log",
                    "path": "onebrief-compile.log",
                    "sha256": "dummy_hash",
                },
                "command_id": "unity_compile",
            }],
        }],
        "blocking_issues": [],
        "revision_instructions": [],
        "missing_information": [],
    })

    assert report.criterion_checks[0].evidence_bindings[0].artifact is None
    assert report.criterion_checks[0].evidence_bindings[0].command_id == "unity_compile"


def test_analysis_package_assigns_unique_ids_to_duplicate_provider_findings() -> None:
    analysis = AnalysisPackage.model_validate({
        "objective": "Inspect the approved Unity baseline.",
        "findings": [
            {
                "finding_id": "F01",
                "source_name": "a.cs",
                "evidence": "Login exists.",
                "implication": "Verify Login.",
            },
            {
                "finding_id": "F01",
                "source_name": "b.cs",
                "evidence": "Lobby exists.",
                "implication": "Verify Lobby.",
            },
            {
                "finding_id": "F99",
                "source_name": "c.cs",
                "evidence": "Settings exists.",
                "implication": "Preserve Settings.",
            },
        ],
        "recommended_structure": ["Verify the active Quest."],
        "constraints": ["Do not modify the original."],
        "risks": [],
    })

    assert [item.finding_id for item in analysis.findings] == ["F001", "F002", "F99"]


def test_declarative_unity_journey_compiles_trusted_harness() -> None:
    plan = UnityEvidenceJourneyPlan.model_validate({
        "summary": "Prove the preserved login journey.",
        "test_directory": "Assets/JULPAE/Tests/PlayMode",
        "steps": [
            {"action": "load_scene", "scene_name": "LoginScene_All"},
            {"action": "capture", "scenario_id": "login"},
            {
                "action": "select_dropdown_index",
                "target": "TestAccountDropdown",
                "value_index": 1,
            },
            {"action": "click_button", "target": "DirectEnterButton"},
            {"action": "wait_for_scene", "scene_name": "LobbyScene_All"},
            {"action": "capture", "scenario_id": "lobby"},
        ],
    })

    rendered = render_unity_evidence_journey(plan)

    assert "ONEBRIEF_DECLARATIVE_EVIDENCE_V1" in rendered.playmode_test_source
    assert "ONEBRIEF_AUTHENTICATION_PRECONDITION_V1" in rendered.playmode_test_source
    assert 'RequireActive("TestAccountDropdown")' in rendered.playmode_test_source
    assert 'RequireActive("DirectEnterButton")' in rendered.playmode_test_source
    assert "ScenarioReceipt" in rendered.playmode_test_source
    assert "WriteManifestAtomically(receipts.ToArray())" in rendered.playmode_test_source
    assert "TestAssemblies" in rendered.test_assembly_source
    assert "item.gameObject.scene.IsValid()" in rendered.playmode_test_source
    assert "item.scene.IsValid() && item.isActiveAndEnabled" not in rendered.playmode_test_source
    assert len(rendered.playmode_test_source.encode("utf-8")) < 8_000


def test_declarative_unity_journey_canonicalizes_exact_assets_root_shorthand() -> None:
    plan = UnityEvidenceJourneyPlan.model_validate({
        "summary": "Use the fixed generated PlayMode evidence directory.",
        "test_directory": "Tests/PlayMode",
        "steps": [
            {"action": "load_scene", "scene_name": "LoginScene_All"},
            {"action": "assert_active", "target": "StartButton"},
            {"action": "capture", "scenario_id": "login"},
        ],
    })

    assert plan.test_directory == "Assets/Tests/PlayMode"


def test_declarative_unity_journey_compiles_complete_onboarding_auth_contract() -> None:
    plan = UnityEvidenceJourneyPlan.model_validate({
        "summary": "Prove the complete first-run authentication journey.",
        "test_directory": "Assets/JULPAE/Tests/PlayMode",
        "steps": [
            {"action": "load_scene", "scene_name": "LoginScene_All"},
            {"action": "click_button", "target": "StartButton"},
            {"action": "set_toggle_on", "target": "TermsPanel/AgreeToggle"},
            {"action": "click_button", "target": "TermsPanel/ConfirmButton"},
            {
                "action": "set_input_text",
                "target": "NicknameSetupPanel/NicknameInputField",
                "text_value": "TestUser",
            },
            {"action": "click_button", "target": "NicknameSetupPanel/ConfirmButton"},
            {"action": "wait_for_scene", "scene_name": "LobbyScene_All"},
            {"action": "capture", "scenario_id": "lobby"},
        ],
    })

    rendered = render_unity_evidence_journey(plan)

    assert "ONEBRIEF_AUTHENTICATION_PRECONDITION_V1" in rendered.playmode_test_source
    assert 'SetToggleOn(RequireActive("TermsPanel/AgreeToggle"))' in rendered.playmode_test_source
    assert "toggle.SetIsOnWithoutNotify(true)" in rendered.playmode_test_source


def test_declarative_unity_journey_canonicalizes_named_toggle_before_playmode() -> None:
    plan = UnityEvidenceJourneyPlan.model_validate({
        "summary": "Normalize a provider-authored terms interaction.",
        "test_directory": "Assets/JULPAE/Tests/PlayMode",
        "steps": [
            {"action": "load_scene", "scene_name": "LoginScene_All"},
            {"action": "click_button", "target": "TermsPanel/AgreeToggle"},
            {"action": "assert_active", "target": "TermsPanel/ConfirmButton"},
            {"action": "capture", "scenario_id": "terms"},
        ],
    })

    assert plan.steps[1].action == "set_toggle_on"


def test_declarative_unity_journey_does_not_certify_start_click_as_authentication() -> None:
    plan = UnityEvidenceJourneyPlan.model_validate({
        "summary": "Observe an unauthenticated start click without claiming a protected scene.",
        "test_directory": "Assets/JULPAE/Tests/PlayMode",
        "steps": [
            {"action": "load_scene", "scene_name": "LoginScene_All"},
            {"action": "click_button", "target": "StartButton"},
            {"action": "assert_active", "target": "StartButton"},
            {"action": "capture", "scenario_id": "login_after_start"},
        ],
    })

    rendered = render_unity_evidence_journey(plan)

    assert "ONEBRIEF_AUTHENTICATION_PRECONDITION_V1" not in rendered.playmode_test_source


@pytest.mark.parametrize(
    "steps",
    [
        [
            {"action": "load_scene", "scene_name": "LoginScene_All"},
            {"action": "click_button", "target": "StartButton"},
            {"action": "wait_for_scene", "scene_name": "LobbyScene_All"},
            {"action": "capture", "scenario_id": "lobby"},
        ],
        [
            {"action": "load_scene", "scene_name": "LoginScene_All"},
            {
                "action": "select_dropdown_index",
                "target": "TestAccountDropdown",
                "value_index": 1,
            },
            {"action": "click_button", "target": "StartButton"},
            {"action": "wait_for_scene", "scene_name": "LobbyScene_All"},
            {"action": "capture", "scenario_id": "lobby"},
        ],
    ],
)
def test_declarative_unity_journey_leaves_incomplete_authentication_for_preflight(
    steps: list[dict[str, object]],
) -> None:
    plan = UnityEvidenceJourneyPlan.model_validate({
        "summary": "Do not mistake Start for authentication.",
        "test_directory": "Assets/JULPAE/Tests/PlayMode",
        "steps": steps,
    })

    rendered = render_unity_evidence_journey(plan)

    assert "ONEBRIEF_AUTHENTICATION_PRECONDITION_V1" not in rendered.playmode_test_source


def test_declarative_unity_journey_does_not_mark_direct_enter_without_account() -> None:
    plan = UnityEvidenceJourneyPlan.model_validate({
        "summary": "Incomplete direct-enter authentication proof.",
        "test_directory": "Assets/JULPAE/Tests/PlayMode",
        "steps": [
            {"action": "load_scene", "scene_name": "LoginScene_All"},
            {"action": "capture", "scenario_id": "login"},
            {"action": "click_button", "target": "DirectEnterButton"},
            {"action": "wait_for_scene", "scene_name": "LobbyScene_All"},
            {"action": "capture", "scenario_id": "lobby"},
        ],
    })

    rendered = render_unity_evidence_journey(plan)

    assert "ONEBRIEF_AUTHENTICATION_PRECONDITION_V1" not in rendered.playmode_test_source


def test_declarative_unity_journey_rejects_direct_destination_load() -> None:
    with pytest.raises(ValidationError, match="only the initial scene"):
        UnityEvidenceJourneyPlan.model_validate({
            "summary": "Invalid direct navigation proof.",
            "test_directory": "Assets/Tests/PlayMode",
            "steps": [
                {"action": "load_scene", "scene_name": "Login"},
                {"action": "assert_active", "target": "StartButton"},
                {"action": "load_scene", "scene_name": "Lobby"},
                {"action": "capture", "scenario_id": "lobby"},
            ],
        })


def test_declarative_unity_journey_restores_transport_placeholders() -> None:
    plan = UnityEvidenceJourneyPlan.model_validate({
        "schema_version": "onebrief-unity-evidence-journey-plan-v1",
        "summary": "Transport-safe login proof.",
        "test_directory": "Assets/Tests/PlayMode",
        "steps": [
            {
                "action": "load_scene", "target": "", "scene_name": "Login",
                "value_index": 0, "text_value": "", "frames": 0,
                "timeout_seconds": 0, "scenario_id": "", "viewport_width": 0,
                "viewport_height": 0,
            },
            {
                "action": "assert_active", "target": "StartButton", "scene_name": "",
                "value_index": 0, "text_value": "", "frames": 0,
                "timeout_seconds": 0, "scenario_id": "", "viewport_width": 0,
                "viewport_height": 0,
            },
            {
                "action": "capture", "target": "", "scene_name": "",
                "value_index": 0, "text_value": "", "frames": 0,
                "timeout_seconds": 0, "scenario_id": "login", "viewport_width": 0,
                "viewport_height": 0,
            },
        ],
    })

    assert plan.steps[0].target is None
    assert plan.steps[0].frames == 2
    assert plan.steps[0].timeout_seconds == 10
    assert plan.steps[2].viewport_width == 1280
    assert plan.steps[2].viewport_height == 720


def test_declarative_unity_journey_discards_irrelevant_transport_operands() -> None:
    plan = UnityEvidenceJourneyPlan.model_validate({
        "summary": "Normalize all-required provider transport fields.",
        "test_directory": "Assets/Tests/PlayMode",
        "steps": [
            {
                "action": "load_scene", "target": "InventedTarget",
                "scene_name": "LoginScene_All", "value_index": 7,
                "text_value": "invented", "scenario_id": "M01_Login_To_Lobby",
            },
            {
                "action": "assert_active", "target": "StartButton",
                "scene_name": "InventedScene", "value_index": 7,
                "text_value": "invented", "scenario_id": "M01_Login_To_Lobby",
            },
            {
                "action": "capture", "target": "InventedTarget",
                "scene_name": "InventedScene", "value_index": 7,
                "text_value": "invented", "scenario_id": "M01_Login_To_Lobby",
            },
        ],
    })

    assert plan.steps[0].target is None
    assert plan.steps[0].scenario_id is None
    assert plan.steps[1].scene_name is None
    assert plan.steps[1].scenario_id is None
    assert plan.steps[2].target is None
    assert plan.steps[2].scene_name is None
    assert plan.steps[2].scenario_id == "m01_login_to_lobby"


def test_declarative_unity_journey_reuses_existing_evidence_paths() -> None:
    raw = {
        "schema_version": "onebrief-unity-evidence-journey-plan-v1",
        "summary": "Repair the journey plan.",
        "test_directory": "Assets/Other/Tests/PlayMode",
        "steps": [
            {"action": "load_scene", "scene_name": "Login"},
            {"action": "assert_active", "target": "StartButton"},
            {"action": "capture", "scenario_id": "login"},
        ],
    }
    previous = {
        "changes": [
            {"path": "Assets/Scripts/LoginView.cs"},
            {"path": "Assets/JULPAE/Tests/PlayMode/ExistingJourney.cs"},
            {"path": "Assets/JULPAE/Tests/PlayMode/ExistingJourney.asmdef"},
        ]
    }

    normalized = normalize_atomic_unity_evidence_bundle(raw, previous)

    assert [item.path for item in normalized.changes] == [
        "Assets/JULPAE/Tests/PlayMode/ExistingJourney.cs",
        "Assets/JULPAE/Tests/PlayMode/ExistingJourney.asmdef",
    ]


def test_declarative_unity_journey_recovers_compatible_transport_shape() -> None:
    raw = {
        "schema_version": "onebrief-unity-journey-plan-v1",
        "summary": "Transport-compatible login proof.",
        "test_directory": "Assets/JULPAE/Tests/PlayMode",
        "steps": [
            {"action": "load_scene", "scene_name": "LoginScene_All"},
            {"action": "assert_active", "target": "StartButton"},
            {"action": "capture", "scenario_id": "login"},
        ],
        "changes": [],
    }

    normalized = normalize_atomic_unity_evidence_bundle(raw)

    assert isinstance(normalized, ProjectCodeChangeSet)
    assert [item.path for item in normalized.changes] == [
        "Assets/JULPAE/Tests/PlayMode/OneBriefGeneratedJourneyTest.cs",
        "Assets/JULPAE/Tests/PlayMode/OneBrief.Generated.Visual.Tests.asmdef",
    ]


def test_trusted_declarative_journey_can_compile_beyond_provider_transport_cap() -> None:
    steps = [
        {"action": "load_scene", "scene_name": "LoginScene_All"},
        *(
            {"action": "assert_active", "target": f"LoginControl{index}"}
            for index in range(20)
        ),
        {"action": "capture", "scenario_id": "login_complete"},
        {"action": "wait_frames", "frames": 4},
    ]
    plan = UnityEvidenceJourneyPlan.model_validate({
        "summary": "Compile a detailed but bounded login observation journey.",
        "test_directory": "Assets/Tests/PlayMode",
        "steps": steps,
    })

    normalized = normalize_atomic_unity_evidence_bundle(plan)

    assert isinstance(normalized, ProjectCodeChangeSet)
    source = next(item.content for item in normalized.changes if item.path.endswith(".cs"))
    assert len(source) > 8_000
    assert "ONEBRIEF_DECLARATIVE_EVIDENCE_V1" in source


def test_evidence_phase_selects_declarative_plan_for_runtime_failure() -> None:
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[{
            "criterion": "Preserved login reaches Lobby",
            "passed": False,
            "evidence": "The executable journey remained in Login.",
        }],
        blocking_issues=[
            "unity_playmode_visual_tests: login destination was not reached"
        ],
        revision_instructions=["Establish the existing test account first."],
        missing_information=[],
    )

    selected = development_maker_schema_for(
        report,
        current_payload=None,
        active_phase=ExecutionPhase.EVIDENCE_CONSTRUCTION,
    )

    assert selected is UnityEvidenceJourneyPlan


def test_product_handoff_never_inherits_failing_test_path() -> None:
    paths = phase_owned_handoff_paths(
        phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
        proposed_paths=["Assets/Tests/PlayMode/OneBriefVisualTests.cs"],
        previous_change_set=None,
    )

    assert paths == []


def test_evidence_handoff_falls_back_only_to_prior_evidence_paths() -> None:
    previous = {
        "changes": [
            {"path": "Assets/Scripts/LoginView.cs"},
            {"path": "Assets/Tests/PlayMode/OneBriefVisualTests.cs"},
        ]
    }

    paths = phase_owned_handoff_paths(
        phase=ExecutionPhase.EVIDENCE_CONSTRUCTION,
        proposed_paths=["Assets/Scripts/LoginView.cs"],
        previous_change_set=previous,
    )

    assert paths == ["Assets/Tests/PlayMode/OneBriefVisualTests.cs"]


def test_verifier_only_replay_does_not_reauthorize_cumulative_candidate_paths() -> None:
    contract = RepairContract.model_validate({
        "contract_id": "RC-0123456789abcdef",
        "observation_id": "FO-0123456789abcdef",
        "progress_kind": "new_hypothesis",
        "occurrence": 1,
        "hypothesis": {
            "hypothesis_id": "RH-0123456789abcdef",
            "suspected_cause": "One product runtime behavior remains incomplete.",
            "cheapest_probe": "Replay the already-built candidate.",
            "expected_signal": "The unchanged trusted verifier produces a receipt.",
            "repair_boundary": "One product source file.",
            "requires_model_reasoning": False,
        },
        "permitted_paths": ["Assets/Scripts/LoginBinder.cs"],
        "verification_ladder": ["compile", "targeted_test"],
        "execution_allowed": True,
        "escalation_required": False,
        "rationale": "Verify before buying another repair turn.",
    })
    cumulative_paths = [
        "Assets/Scripts/LoginBinder.cs",
        "Assets/Tests/PlayMode/OneBriefVisualLoginLobbyTest.cs",
        "Assets/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
    ]

    assert paths_outside_active_repair_contract(
        cumulative_paths, contract, reverify_existing=True
    ) == []
    assert paths_outside_active_repair_contract(
        cumulative_paths, contract, reverify_existing=False
    ) == cumulative_paths[1:]


def test_generated_unity_evidence_pair_is_one_causal_repair_bundle() -> None:
    contract = RepairContract.model_validate({
        "contract_id": "RC-1123456789abcdef",
        "observation_id": "FO-1123456789abcdef",
        "progress_kind": "new_hypothesis",
        "occurrence": 1,
        "hypothesis": {
            "hypothesis_id": "RH-1123456789abcdef",
            "suspected_cause": "The generated PlayMode journey needs its test assembly.",
            "cheapest_probe": "Compile the generated evidence bundle.",
            "expected_signal": "Unity discovers and compiles the PlayMode test.",
            "repair_boundary": "One generated Unity evidence bundle.",
            "requires_model_reasoning": False,
        },
        "permitted_paths": [
            "Assets/JULPAE/Tests/PlayMode/OneBriefGeneratedJourneyTest.cs"
        ],
        "verification_ladder": ["compile", "targeted_test"],
        "execution_allowed": True,
        "escalation_required": False,
        "rationale": "The trusted compiler emits an indivisible test and asmdef pair.",
    })
    proposed = [
        "Assets/JULPAE/Tests/PlayMode/OneBriefGeneratedJourneyTest.cs",
        "Assets/JULPAE/Tests/PlayMode/OneBrief.Generated.Visual.Tests.asmdef",
    ]

    assert paths_outside_active_repair_contract(
        proposed, contract, reverify_existing=False
    ) == []


class FakeGateway:
    def __init__(self, outputs: list[object]):
        self.outputs = outputs
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict[str, object]] = []

    def generate_json(self, *, stage: str, model: str, schema: type, **kwargs: object):
        self.calls.append((stage, model))
        self.payloads.append(kwargs)
        value = self.outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, schema)
        return value


class BlockingGateway:
    def generate_json(self, **_: object):
        raise BudgetExceeded("blocked before generation")


class FakeBudgetedGateway(BudgetedGeminiClient):
    """Budgeted gateway type marker without a live Vertex client."""

    def __init__(self, outputs: list[object]):
        self.outputs = outputs
        self.calls: list[tuple[str, str]] = []

    def generate_json(self, *, stage: str, model: str, schema: type, **_: object):
        self.calls.append((stage, model))
        value = self.outputs.pop(0)
        assert isinstance(value, schema)
        return value


@pytest.mark.parametrize("path", [
    "Assets/JULPAE/Tests/PlayMode/Flow.cs",
    "independent_observations/unity_ui_observation.json",
    "evidence/screenshots/LobbyMobile.png",
    "web/src/page.test.tsx",
])
def test_visual_product_repair_cannot_target_tests_or_evidence(path: str) -> None:
    assert visual_repair_production_target_allowed(path) is False


@pytest.mark.parametrize("path", [
    "Assets/JULPAE/Scripts/Localization/JulpaeLanguageDropdown.cs",
    "Assets/JULPAE/Scenes/Main.unity",
    "web/src/app/page.tsx",
])
def test_visual_product_repair_allows_shipped_product_sources(path: str) -> None:
    assert visual_repair_production_target_allowed(path) is True


def test_development_toolpack_focus_includes_the_full_completion_contract() -> None:
    intake = IntakeRequest(
        goal="Modernize the Unity client.",
        desired_output="A verified patch.",
    )
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="The repository can be inspected.",
        normalized_goal="Modernize the existing Unity client.",
        deliverables=["Updated client source"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Spanish locale changes visible Settings text."],
        completion_contract=CompletionContract(
            target_state="The modernized client is usable.",
            quality_criteria=[QualityCriterion(
                criterion_id="Q01",
                description="Language selection visibly updates the active screen.",
                evidence_required="A real PlayMode screenshot after selecting Spanish.",
            )],
        ),
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )

    focus = development_toolpack_focus_text(intake, requirements)

    assert "Modernize the Unity client." in focus
    assert "Spanish locale changes visible Settings text." in focus
    assert "Language selection visibly updates the active screen." in focus
    assert "real PlayMode screenshot" in focus


def test_unchanged_unity_locale_is_a_product_failure_not_an_evidence_gap() -> None:
    feedback = "Unity visual scenario locale_es did not visibly change any text"

    assert is_unity_localization_product_failure(feedback) is True
    assert is_unity_evidence_contract_feedback(feedback) is False

    report = ExecutionPipeline._development_failure_report(feedback)
    instruction = report.revision_instructions[0]
    assert "production path" in instruction
    assert "Do not edit the PlayMode test" in instruction


def test_visual_repair_maker_candidate_hides_proof_and_retains_product_code() -> None:
    candidate = ProjectCodeChangeSet(
        summary="Mixed prior candidate.",
        changes=[
            {
                "path": "Assets/JULPAE/Tests/PlayMode/Flow.cs",
                "base_sha256": None,
                "content": "assert layout",
                "reason": "Proof only.",
            },
            {
                "path": "Assets/JULPAE/Scripts/LobbyLayout.cs",
                "base_sha256": None,
                "content": "repair layout",
                "reason": "Shipped product.",
            },
        ],
    )

    visible = visual_repair_production_candidate(candidate)

    assert [change.path for change in visible.changes] == [
        "Assets/JULPAE/Scripts/LobbyLayout.cs"
    ]
    assert len(candidate.changes) == 2
    assert visual_repair_has_uncommitted_product_candidate(candidate) is True


def test_visual_repair_committed_source_does_not_use_candidate_file_mode() -> None:
    candidate = ProjectCodeChangeSet(
        summary="Committed product repair.",
        changes=[{
            "path": "Assets/JULPAE/Scripts/Localization/JulpaeLanguageDropdown.cs",
            "base_sha256": "a" * 64,
            "content": "public class JulpaeLanguageDropdown {}",
            "reason": "Repair a committed source.",
        }],
    )

    assert visual_repair_has_uncommitted_product_candidate(candidate) is False


def test_small_generated_unity_evidence_file_uses_candidate_file_mode() -> None:
    candidate = ProjectCodeChangeSet(
        summary="Generated PlayMode evidence.",
        changes=[{
            "path": "Assets/Tests/PlayMode/VisualFlowTest.cs",
            "base_sha256": None,
            "content": "namespace OneBrief.Visual { class Flow {} }\n",
            "reason": "Current generated evidence candidate.",
        }],
    )

    assert evidence_repair_has_bounded_uncommitted_candidate(candidate) is True


def test_committed_or_large_unity_evidence_file_stays_anchored() -> None:
    committed = ProjectCodeChangeSet(
        summary="Committed PlayMode evidence.",
        changes=[{
            "path": "Assets/Tests/PlayMode/VisualFlowTest.cs",
            "base_sha256": "a" * 64,
            "content": "namespace OneBrief.Visual { class Flow {} }\n",
            "reason": "Committed evidence candidate.",
        }],
    )
    large = committed.model_copy(update={
        "changes": [committed.changes[0].model_copy(update={
            "base_sha256": None,
            "content": "x" * 8_001,
        })],
    })

    assert evidence_repair_has_bounded_uncommitted_candidate(committed) is False
    assert evidence_repair_has_bounded_uncommitted_candidate(large) is False


def test_existing_unity_multi_surface_repair_is_classified_complex() -> None:
    requirements = _requirements().model_copy(update={
        "acceptance_criteria": [
            "Login works.", "Lobby works.", "Settings works.", "Mobile works."
        ],
        "completion_contract": None,
    })
    # Re-validate so the derived completion contract reflects the four criteria.
    requirements = RequirementsAnalysis.model_validate(
        requirements.model_dump(mode="json")
    )
    intake = IntakeRequest(
        goal=(
            "Preserve the server protocol while modernizing login, lobby, settings, mobile and desktop UI."
        ),
        output_target=OutputTarget.EXISTING_PROJECT,
    )

    assert development_repair_difficulty(
        intake,
        requirements,
        ["Assets/JULPAE/Scripts/Login.cs", "Assets/JULPAE/Scripts/Lobby.cs"],
    ) == "complex"


def test_resumed_development_resolves_revalidation_evidence_under_development(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    evidence_dir = output_dir / "development" / "unity_visual_evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "runtime.json").write_text(
        '{"scenario":"LoginDesktop"}', encoding="utf-8"
    )
    run = DevelopmentRun(
        status="verified",
        repository_name="julpae",
        base_head_sha="a" * 40,
        summary="Verified",
        changed_paths=[],
        commands=[],
        patch_path="development_revalidation_v8/changes.patch",
        evidence_paths=["development_revalidation_v8/unity_visual_evidence"],
        safety_boundary=["isolated"],
    )
    (output_dir / "development" / "development_run.json").write_text(
        run.model_dump_json(), encoding="utf-8"
    )
    (output_dir / "code_change_set.json").write_text(
        '{"summary":"candidate","changes":[]}', encoding="utf-8"
    )

    evidence = ExecutionPipeline(tmp_path / "run", gateway=object())._development_evidence(
        output_dir
    )

    assert evidence is not None
    assert evidence["runtime_evidence"] == [{
        "path": "development/unity_visual_evidence/runtime.json",
        "content": '{"scenario":"LoginDesktop"}',
    }]

def _requirements() -> RequirementsAnalysis:
    return RequirementsAnalysis(
        supported=True,
        support_reason="Complete internal inputs were supplied.",
        normalized_goal="Create a grounded guide.",
        deliverables=["Grounded guide"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Every material claim cites supplied evidence."],
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )


def _source() -> InternalSource:
    return InternalSource(
        name="policy.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["policy"],
        content="The policy requires manager approval for remote work.",
    )


def _analysis() -> AnalysisPackage:
    return AnalysisPackage(
        objective="Create a grounded guide.",
        findings=[
            {
                "finding_id": "F01",
                "source_name": "policy.md",
                "evidence": "Manager approval is required.",
                "implication": "The guide must instruct employees to request approval.",
            }
        ],
        recommended_structure=["Rule", "Procedure"],
        constraints=["Use only the policy."],
        risks=[],
    )


def _draft(text: str = "Employees must request manager approval before remote work. [F01]") -> DraftArtifact:
    return DraftArtifact(
        title="Remote Work Guide",
        body_markdown=text,
        cited_finding_ids=["F01"],
        drafting_decisions=["Used the mandatory policy."],
    )


def _verification(verdict: str) -> VerificationReport:
    revise = verdict == "REVISE"
    return VerificationReport(
        verdict=verdict,
        criterion_checks=[
            {
                "criterion": "Every material claim cites evidence.",
                "passed": not revise,
                "evidence": "F01 is present." if not revise else "The procedure is incomplete.",
            }
        ],
        blocking_issues=["Add the approval request procedure."] if revise else [],
        revision_instructions=["Add a clear approval request step."] if revise else [],
        missing_information=[],
    )


def _approve(tmp_path: Path, intake: IntakeRequest, source: InternalSource) -> Path:
    run_dir = tmp_path / "run"
    estimate = estimate_budget(
        intake.model_copy(update={"internal_sources": [source]}),
        _requirements(),
    )
    BudgetStore(run_dir).approve(estimate, estimate.recommended_approval_usd)
    return run_dir


def test_revision_is_always_reverified_through_same_gateway(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.", max_revision_rounds=2)
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    gateway = FakeGateway(
        [
            _analysis(),
            _draft(),
            _verification("REVISE"),
            DraftArtifact(
                title="Remote Work Guide",
                body_markdown=(
                    "Employees must submit a request and receive manager approval before remote work. [F01]"
                ),
                drafting_decisions=["Added the approval request procedure."],
                cited_finding_ids=["F01"],
                temperament_decisions=[
                    {
                        "agent": "writer",
                        "agent_type": "TNL",
                        "options": ["targeted correction", "full rewrite"],
                        "selected": "targeted correction",
                        "deciding_axis": "scope",
                        "reason": "Both met the contract; Local favored the smaller correction.",
                    }
                ],
            ),
            _verification("PASS"),
        ]
    )
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        output_dir=tmp_path / "output",
    )
    assert result.status == PipelineStatus.COMPLETE
    assert [stage for stage, _ in gateway.calls] == [
        "evidence_analysis",
        "long_form_draft",
        "independent_verification_r0",
        "long_form_draft_revision_r1",
        "independent_verification_r1",
    ]
    assert BudgetStore(run_dir).read().status == RunStatus.COMPLETE
    audit = json.loads(
        (tmp_path / "output" / "temperament_decisions.json").read_text(encoding="utf-8")
    )
    assert len(audit) == 1
    assert audit[0]["agent_type"] == "TNL"
    retry_payload = json.loads(gateway.payloads[3]["contents"])
    assert retry_payload["previous_draft"]["title"] == "Remote Work Guide"
    assert retry_payload["verification_feedback"]["verdict"] == "REVISE"


def test_milestone_pipeline_leaves_shared_budget_open_for_next_slice(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.", max_revision_rounds=1)
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    gateway = FakeGateway([_analysis(), _draft(), _verification("PASS")])

    result = ExecutionPipeline(
        run_dir,
        gateway=gateway,
        finalize_budget_on_finish=False,
    ).run(
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        output_dir=tmp_path / "milestone-output",
    )

    assert result.status == PipelineStatus.COMPLETE
    assert BudgetStore(run_dir).read().status in {RunStatus.APPROVED, RunStatus.RUNNING}


def test_production_gateway_selects_adk_convergence_without_legacy_writer_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = IntakeRequest(goal="Create a guide.", max_revision_rounds=2)
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    gateway = FakeBudgetedGateway([_analysis()])
    output_dir = tmp_path / "adk-output"
    calls: list[str] = []

    def fake_adk(self, **_: object):
        calls.append("adk")
        draft = _draft()
        report = _verification("PASS")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "draft_r1.json").write_text(
            draft.model_dump_json(indent=2), encoding="utf-8"
        )
        (output_dir / "verification_r1.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        (output_dir / "adk_convergence_trace.json").write_text(
            json.dumps({"same_maker_reused": True}), encoding="utf-8"
        )
        return draft, report, 1

    monkeypatch.setattr(ExecutionPipeline, "_run_adk_document_convergence", fake_adk)
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        output_dir=output_dir,
    )

    assert result.status == PipelineStatus.COMPLETE
    assert calls == ["adk"]
    assert [stage for stage, _ in gateway.calls] == ["evidence_analysis"]
    assert (output_dir / "final.md").is_file()


def test_cloud_document_continuation_reenters_adk_convergence_with_restored_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = IntakeRequest(goal="Create a guide.", max_revision_rounds=2)
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    gateway = FakeBudgetedGateway([])
    output_dir = tmp_path / "adk-continuation"
    output_dir.mkdir()
    (output_dir / "analysis.json").write_text(
        _analysis().model_dump_json(indent=2), encoding="utf-8"
    )
    (output_dir / "draft_r0.json").write_text(
        _draft(
            "Restored incomplete candidate still requires a grounded procedure before it can pass. [F01]"
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    (output_dir / "continuation_manifest.json").write_text(
        json.dumps({"schema_version": "onebrief-cloud-continuation-v1"}),
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_adk(self, **_: object):
        calls.append("restored-adk")
        assert (output_dir / "draft_r0.json").is_file()
        draft = _draft(
            "Restored candidate was repaired by the same accountable maker and now includes the grounded procedure. [F01]"
        )
        report = _verification("PASS")
        (output_dir / "draft_r1.json").write_text(
            draft.model_dump_json(indent=2), encoding="utf-8"
        )
        (output_dir / "verification_r1.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        (output_dir / "adk_convergence_trace.json").write_text(
            json.dumps({"same_maker_reused": True}), encoding="utf-8"
        )
        return draft, report, 1

    monkeypatch.setattr(ExecutionPipeline, "_run_adk_document_convergence", fake_adk)

    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        output_dir=output_dir,
    )

    assert result.status == PipelineStatus.COMPLETE
    assert calls == ["restored-adk"]
    assert gateway.calls == []
    assert "same accountable maker" in (output_dir / "final.md").read_text("utf-8")


def test_adk_software_loop_repairs_failed_isolated_test_before_independent_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = CodeChangeSet(summary="Initial implementation", changes=[{
        "path": "web/src/status.ts", "base_sha256": None,
        "content": "export const status = 'broken';\n", "reason": "Implement status.",
    }])
    corrected = CodeChangeSet(summary="Corrected implementation", changes=[{
        "path": "web/src/status.ts", "base_sha256": None,
        "content": "export const status = 'ready';\n", "reason": "Repair failed test.",
    }])

    class AdkSoftwareGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.stages: list[str] = []
            self.outputs = [
                initial.model_dump(mode="json"),
                corrected.model_dump(mode="json"),
                _verification("PASS").model_dump(mode="json"),
            ]

        def generate_adk_response(self, **kwargs: object) -> types.GenerateContentResponse:
            self.stages.append(str(kwargs["stage"]))
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    attempts: list[CodeChangeSet] = []

    def fake_apply(
        _self: ExecutionPipeline, _intake: IntakeRequest, _pack: object, supplied: CodeChangeSet,
        development_dir: Path, _contract: dict[str, object],
    ) -> DevelopmentRun:
        attempts.append(supplied)
        if len(attempts) == 1:
            raise RuntimeError("development verification failed: web_tests expected ready status")
        changed = development_dir / "changed_files" / "web" / "src" / "status.ts"
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text(supplied.changes[0].content, encoding="utf-8")
        (development_dir / "changes.patch").write_text("patch\n", encoding="utf-8")
        run = DevelopmentRun(
            status="verified", repository_name="exchange", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=["web/src/status.ts"],
            commands=[DevelopmentCommandResult(
                command_id="web_tests", argv=["npm", "test"], exit_code=0,
                duration_seconds=0.1, output_tail="passed",
            )],
            patch_path="development/changes.patch", safety_boundary=["isolated clone only"],
        )
        (development_dir / "development_run.json").write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        return run

    monkeypatch.setattr(ExecutionPipeline, "_apply_development_change_set", fake_apply)
    intake = IntakeRequest(
        goal="Repair the existing Exchange status module.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
        max_revision_rounds=2,
    )
    gateway = AdkSoftwareGateway()
    output_dir = tmp_path / "software-adk"
    pipeline = ExecutionPipeline(tmp_path / "run", gateway=gateway)
    draft, report, revision_round = pipeline._run_adk_development_convergence(
        intake=intake, requirements=_requirements(), sources=[_source()],
        source_payload=[{
            "name": "exchange-source/web/src/status.ts", "priority": "mandatory",
            "requirement_keys": ["status"], "content": "export const status = 'old';",
            "sha256": "b" * 64,
        }],
        contract={"goal": intake.goal, "acceptance_criteria": ["Tests pass."]},
        analysis=_analysis(), output_dir=output_dir,
    )

    assert revision_round == 1
    assert report.verdict == Verdict.PASS
    assert "격리 빌드" in draft.title
    assert [item.changes[0].content for item in attempts] == [
        initial.changes[0].content, corrected.changes[0].content,
    ]
    assert gateway.stages == [
        "product_implementation::long_form_draft",
        "product_implementation::repair::long_form_draft",
        "final_verification::independent_verification",
    ]
    trace = json.loads((output_dir / "adk_convergence_trace.json").read_text(encoding="utf-8"))
    assert trace["agent_tree"]["same_maker_reused"] is True


def test_adk_software_continuation_restores_previous_change_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = CodeChangeSet(summary="Prior candidate", changes=[{
        "path": "web/src/status.ts", "base_sha256": None,
        "content": "export const status = 'almost-ready';\n",
        "reason": "Preserve the prior Cloud candidate.",
    }])
    repaired = CodeChangeSet(summary="Targeted repair", changes=[{
        "path": "web/src/status.ts", "base_sha256": None,
        "content": "export const status = 'ready';\n",
        "reason": "Repair the one remaining criterion.",
    }])

    class ResumeGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.outputs = [
                repaired.model_dump(mode="json"),
                _verification("PASS").model_dump(mode="json"),
            ]
            self.output_caps: list[int | None] = []

        def generate_adk_response(self, **kwargs: object) -> types.GenerateContentResponse:
            config = kwargs.get("config")
            self.output_caps.append(getattr(config, "max_output_tokens", None))
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    observed_previous: list[CodeChangeSet | None] = []
    original_promote = DeveloperAgent.promote_candidate

    def capture_previous(
        self: DeveloperAgent, raw: object, sources: list[dict[str, object]],
        previous_change_set: CodeChangeSet | None = None,
        approved_anchor_catalog: list[dict[str, object]] | None = None,
    ) -> CodeChangeSet:
        observed_previous.append(previous_change_set)
        return original_promote(
            self, raw, sources, previous_change_set, approved_anchor_catalog
        )

    def fake_apply(
        _self: ExecutionPipeline, _intake: IntakeRequest, _pack: object,
        supplied: CodeChangeSet, development_dir: Path, _contract: dict[str, object],
    ) -> DevelopmentRun:
        assert supplied.changes[0].content == "export const status = 'ready';\n"
        return DevelopmentRun(
            status="verified", repository_name="exchange", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=["web/src/status.ts"],
            commands=[], patch_path="development/changes.patch",
            safety_boundary=["isolated clone only"],
        )

    monkeypatch.setattr(DeveloperAgent, "promote_candidate", capture_previous)
    monkeypatch.setattr(ExecutionPipeline, "_apply_development_change_set", fake_apply)
    intake = IntakeRequest(
        goal="Finish the prior Exchange repair.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
    )
    output_dir = tmp_path / "continued"
    output_dir.mkdir()
    (output_dir / "code_change_set.json").write_text(
        previous.model_dump_json(indent=2), encoding="utf-8"
    )
    (output_dir / "development_verification_failure.txt").write_text(
        "Visible text did not change after the language control was activated.",
        encoding="utf-8",
    )

    gateway = ResumeGateway()
    _, report, _ = ExecutionPipeline(
        tmp_path / "run", gateway=gateway
    )._run_adk_development_convergence(
        intake=intake, requirements=_requirements(), sources=[_source()],
        source_payload=[{
            "name": "exchange-source/web/src/status.ts", "priority": "mandatory",
            "requirement_keys": ["status"], "content": "export const status = 'old';",
            "sha256": "b" * 64,
        }],
        contract={"goal": intake.goal, "acceptance_criteria": ["Tests pass."]},
        analysis=_analysis(), output_dir=output_dir,
    )

    assert report.verdict == Verdict.PASS
    assert observed_previous == [previous]
    assert gateway.output_caps[0] == 10_000


def test_adk_software_failure_keeps_most_progressed_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def candidate(label: str) -> CodeChangeSet:
        return CodeChangeSet(summary=label, changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": f"export const status = '{label}';\n",
            "reason": label,
        }])

    initial = candidate("initial")
    improved = candidate("improved")
    regressed = candidate("regressed")
    class RegressionGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.outputs = [
                initial.model_dump(mode="json"),
                improved.model_dump(mode="json"),
                regressed.model_dump(mode="json"),
                regressed.model_dump(mode="json"),
                regressed.model_dump(mode="json"),
            ]

        def generate_adk_response(self, **_kwargs: object) -> types.GenerateContentResponse:
            payload = self.outputs.pop(0)
            config = _kwargs.get("config")
            schema = getattr(config, "response_schema", None)
            if getattr(schema, "__name__", "").startswith("CatalogBoundProductRepair_"):
                def enum_values(value: object) -> list[str]:
                    if isinstance(value, dict):
                        found = [
                            item for item in value.get("enum", [])
                            if isinstance(item, str) and item.startswith("A")
                        ]
                        constant = value.get("const")
                        if isinstance(constant, str) and constant.startswith("A"):
                            found.append(constant)
                        return found or next(
                            (items for nested in value.values() if (items := enum_values(nested))),
                            [],
                        )
                    if isinstance(value, list):
                        return next(
                            (items for nested in value if (items := enum_values(nested))),
                            [],
                        )
                    return []

                anchor_id = enum_values(schema.model_json_schema())[0]
                label = str(payload["summary"])
                payload = {
                    "summary": label,
                    "changes": [{
                        "path": "web/src/status.ts",
                        "base_sha256": None,
                        "anchor_id": anchor_id,
                        "replace": f"export const status = '{label}';\n",
                        "reason": label,
                    }],
                }
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    gateway = RegressionGateway()
    failures = iter([
        "web observation failed: no control | text same | lang same | one state",
        "web observation failed: cjk leaked | labels identical",
        "web observation failed: no control | text same | lang same | one state",
        "web observation failed: no control | text same | lang same | one state",
    ])

    def fake_apply(*_args: object, **_kwargs: object) -> DevelopmentRun:
        raise RuntimeError(next(failures))

    monkeypatch.setattr(ExecutionPipeline, "_apply_development_change_set", fake_apply)
    intake = IntakeRequest(
        goal="Add a working language control.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
    )
    output_dir = tmp_path / "regression-output"
    output_dir.mkdir()

    with pytest.raises(RuntimeError, match="no new evidence"):
        ExecutionPipeline(tmp_path / "run", gateway=gateway)._run_adk_development_convergence(
            intake=intake, requirements=_requirements(), sources=[_source()],
            source_payload=[{
                "name": "exchange-source/web/src/status.ts", "priority": "mandatory",
                "requirement_keys": ["language"], "content": "export const status = 'old';",
                "sha256": "b" * 64,
            }],
            contract={"goal": intake.goal, "acceptance_criteria": ["Languages switch."]},
            analysis=_analysis(), output_dir=output_dir,
        )

    restored = CodeChangeSet.model_validate_json(
        (output_dir / "code_change_set.json").read_text("utf-8")
    )
    assert restored.summary == "improved"
    assert CodeChangeSet.model_validate_json(
        (output_dir / "development_best_candidate.json").read_text("utf-8")
    ).summary == "improved"
    preserved_feedback = (
        output_dir / "development_verification_failure.txt"
    ).read_text("utf-8").strip()
    assert preserved_feedback.startswith(
        "web observation failed: cjk leaked | labels identical"
    )
    assert "Regression guard from the rejected attempt" in preserved_feedback
    assert "no control | text same | lang same | one state" in preserved_feedback


def test_compact_repair_schema_allows_small_test_and_assembly_pair() -> None:
    repair = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Complete the executable evidence topology.",
        "changes": [
            {
                "path": "Assets/Tests/PlayMode/OneBriefVisualTests.cs",
                "base_sha256": None,
                "content": "namespace OneBrief.Visual { public class Flow {} }\n",
                "reason": "Add the bounded PlayMode evidence test.",
            },
            {
                "path": "Assets/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
                "base_sha256": None,
                "content": '{"optionalUnityReferences":["TestAssemblies"]}\n',
                "reason": "Make the PlayMode evidence test discoverable.",
            },
        ],
    })

    assert len(repair.changes) == 2


def test_exact_repair_schema_allows_one_bounded_coherent_search_range() -> None:
    repair = ExactRepairProjectCodeChangeSet.model_validate({
        "summary": "Repair one coherent evidence function.",
        "changes": [{
            "path": "Assets/Tests/PlayMode/VisualFlowTest.cs",
            "base_sha256": None,
            "search": "x" * 6_000,
            "replace": "y" * 6_500,
            "reason": "Bind measured capture dimensions to each scenario.",
        }],
    })

    assert len(repair.changes[0].search or "") == 6_000


def test_png_viewport_mismatch_requests_measured_capture_dimensions() -> None:
    report = ExecutionPipeline._development_failure_report(
        "Unity visual scenario login_desktop viewport width does not match its PNG"
    )

    assert report.verdict.value == "REVISE"
    assert "Do not add more frame delays" in report.revision_instructions[0]
    assert "Texture2D/PNG width and height actually written" in report.revision_instructions[0]


def test_adk_software_continuation_returns_bad_edit_anchor_to_same_maker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = ProjectCodeChangeSet(
        summary="Prior candidate",
        changes=[{
            "path": "web/app/page.tsx", "base_sha256": "a" * 64,
            "content": "export const label = 'almost-ready';\n",
            "reason": "Preserve prior progress.",
        }],
    )
    bad = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Bad anchor",
        "changes": [{
            "path": "web/app/page.tsx", "base_sha256": "a" * 64,
            "search": "text that is not present", "replace": "ready",
            "reason": "First repair attempt.",
        }],
    })
    good = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Exact repair",
        "changes": [{
            "path": "web/app/page.tsx", "base_sha256": "a" * 64,
            "search": "'almost-ready'", "replace": "'ready'",
            "reason": "Use exact prior text.",
        }],
    })

    class AnchorRetryGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.outputs = [
                bad.model_dump(mode="json"), good.model_dump(mode="json"),
                _verification("PASS").model_dump(mode="json"),
            ]

        def generate_adk_response(self, **_kwargs: object) -> types.GenerateContentResponse:
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    def fake_apply(
        _self: ExecutionPipeline, _intake: IntakeRequest, _pack: object,
        supplied: ProjectCodeChangeSet, _development_dir: Path,
        _contract: dict[str, object],
    ) -> DevelopmentRun:
        assert supplied.changes[0].content == "export const label = 'ready';\n"
        return DevelopmentRun(
            status="verified", repository_name="project", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=["web/app/page.tsx"],
            commands=[], patch_path="development/changes.patch",
            safety_boundary=["isolated clone only"],
        )

    monkeypatch.setattr(ExecutionPipeline, "_apply_development_change_set", fake_apply)
    monkeypatch.setattr(
        ExecutionPipeline,
        "_bind_project_change_set",
        lambda _self, _intake, _pack, change_set, _output: change_set,
    )
    intake = IntakeRequest(
        goal="Finish the existing project.", output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.PROJECT_DEVELOPMENT], existing_project_id="project",
    )
    output_dir = tmp_path / "anchor-retry"
    output_dir.mkdir()
    (output_dir / "code_change_set.json").write_text(
        previous.model_dump_json(indent=2), encoding="utf-8"
    )
    (output_dir / "development_verification_failure.txt").write_text(
        "One exact source edit remains.", encoding="utf-8"
    )

    pipeline = ExecutionPipeline(tmp_path / "run", gateway=AnchorRetryGateway())
    monkeypatch.setattr(
        pipeline,
        "_development_components",
        lambda _intake, _output=None: (
            ProjectCodeChangeSet,
            object(),
            DeveloperAgent(
                pipeline.gateway, change_set_schema=ProjectCodeChangeSet,
                source_prefix="project-source/", path_approver=lambda path: path,
            ),
        ),
    )
    _, report, _ = pipeline._run_adk_development_convergence(
        intake=intake, requirements=_requirements(), sources=[_source()],
        source_payload=[{
            "name": "project-source/web/app/page.tsx", "priority": "mandatory",
            "requirement_keys": ["repair"], "content": previous.changes[0].content,
            "sha256": "a" * 64,
        }],
        contract={"goal": intake.goal, "acceptance_criteria": ["Tests pass."]},
        analysis=_analysis(), output_dir=output_dir,
    )

    assert report.verdict == Verdict.PASS
    assert (output_dir / "development_candidate_promotion_failure_r0.txt").is_file()
    convergence = json.loads(
        (output_dir / "convergence_ledger.json").read_text(encoding="utf-8")
    )
    assert convergence["observations"][-1]["layer"] == "source_binding"
    assert convergence["repair_contracts"][-1]["execution_allowed"] is True
    assert (
        convergence["repair_contracts"][-1]["hypothesis"]["requires_model_reasoning"]
        is False
    )


def test_initial_unsafe_promotion_returns_to_same_maker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = ProposedProjectCodeChangeSet.model_validate({
        "summary": "Unsafe initial proposal.",
        "changes": [{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "search": "'old'",
            "replace": "process.env.SECRET",
            "reason": "This unsafe capability must be rejected.",
        }],
    })
    corrected = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Safe bounded repair.",
        "changes": [{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "search": "'old'",
            "replace": "'ready'",
            "reason": "Use a normal in-app value.",
        }],
    })

    class SafetyRetryGateway:
        def __init__(self) -> None:
            self.outputs = [
                bad.model_dump(mode="json"),
                corrected.model_dump(mode="json"),
                _verification("PASS").model_dump(mode="json"),
            ]

        def generate_adk_response(self, **_kwargs: object) -> types.GenerateContentResponse:
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    def fake_apply(
        _self: ExecutionPipeline, _intake: IntakeRequest, _pack: object,
        supplied: ProjectCodeChangeSet, _development_dir: Path,
        _contract: dict[str, object],
    ) -> DevelopmentRun:
        assert supplied.changes[0].content == "export const label = 'ready';\n"
        return DevelopmentRun(
            status="verified", repository_name="project", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=["web/app/page.tsx"],
            commands=[], patch_path="development/changes.patch",
            safety_boundary=["isolated clone only"],
        )

    monkeypatch.setattr(ExecutionPipeline, "_apply_development_change_set", fake_apply)
    monkeypatch.setattr(
        ExecutionPipeline, "_bind_project_change_set",
        lambda _self, _intake, _pack, change_set, _output: change_set,
    )
    intake = IntakeRequest(
        goal="Finish the existing project.", output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.PROJECT_DEVELOPMENT], existing_project_id="project",
    )
    output_dir = tmp_path / "unsafe-initial"
    output_dir.mkdir()
    pipeline = ExecutionPipeline(tmp_path / "run", gateway=SafetyRetryGateway())
    monkeypatch.setattr(
        pipeline, "_development_components",
        lambda _intake, _output=None: (
            ProjectCodeChangeSet,
            object(),
            DeveloperAgent(
                pipeline.gateway, change_set_schema=ProjectCodeChangeSet,
                source_prefix="project-source/", path_approver=lambda path: path,
            ),
        ),
    )
    _, report, _ = pipeline._run_adk_development_convergence(
        intake=intake, requirements=_requirements(), sources=[_source()],
        source_payload=[{
            "name": "project-source/web/app/page.tsx",
            "repository_path": "web/app/page.tsx",
            "priority": "mandatory", "requirement_keys": ["repair"],
            "content": "export const label = 'old';\n", "sha256": "a" * 64,
        }],
        contract={"goal": intake.goal, "acceptance_criteria": ["Tests pass."]},
        analysis=_analysis(), output_dir=output_dir,
    )

    assert report.verdict == Verdict.PASS
    assert (output_dir / "development_candidate_promotion_failure_r0.txt").is_file()
    assert not (output_dir / "development_pending_promotion.json").exists()


def test_source_binding_failure_rebuilds_approved_anchor_and_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_content = (
        "private void ApplyLoginStaticTexts()\n"
        "{\n"
        "    if (GameObject.Find(\"Canvas/CurrentPanel\") == null)\n"
        "        return;\n"
        "}\n"
    )
    bad = ProposedProjectCodeChangeSet.model_validate({
        "summary": "Update the current login guard.",
        "changes": [{
            "path": "Assets/UI/LoginBinder.cs", "base_sha256": "a" * 64,
            "search": (
                "private void ApplyLoginStaticTexts()\n{\n"
                "    if (GameObject.Find(\"Canvas/StalePanel\") == null)\n"
                "        return;\n}"
            ),
            "replace": "stale proposal",
            "reason": "The model reconstructed one stale line.",
        }],
    })
    source_payload = [{
        "name": "project-source/Assets/UI/LoginBinder.cs",
        "repository_path": "Assets/UI/LoginBinder.cs",
        "priority": "mandatory", "requirement_keys": ["repair"],
        "content": source_content, "sha256": "a" * 64,
    }]
    anchors = DeveloperAgent.source_binding_anchors(
        bad.model_dump(mode="json"), source_payload
    )
    assert anchors and "Canvas/CurrentPanel" in anchors[0]["anchors"][0]["text"]
    anchor_id = str(anchors[0]["anchors"][0]["anchor_id"])
    corrected = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Use the approved current source window.",
        "changes": [{
            "path": "Assets/UI/LoginBinder.cs", "base_sha256": "a" * 64,
            "anchor_id": anchor_id,
            "replace": (
                "private void ApplyLoginStaticTexts()\n{\n"
                "    if (GameObject.Find(\"Canvas/CurrentPanel\") == null && !enabled)\n"
                "        return;"
            ),
            "reason": "Select the digest-bound approved source window.",
        }],
    })

    class SourceBindingGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.outputs = [
                bad.model_dump(mode="json"), corrected.model_dump(mode="json"),
                _verification("PASS").model_dump(mode="json"),
            ]

        def generate_adk_response(self, **_kwargs: object) -> types.GenerateContentResponse:
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload))]),
                finish_reason=types.FinishReason.STOP,
            )])

    def fake_apply(
        _self: ExecutionPipeline, _intake: IntakeRequest, _pack: object,
        supplied: ProjectCodeChangeSet, _development_dir: Path,
        _contract: dict[str, object],
    ) -> DevelopmentRun:
        assert "Canvas/CurrentPanel" in supplied.changes[0].content
        assert "!enabled" in supplied.changes[0].content
        return DevelopmentRun(
            status="verified", repository_name="project", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=["Assets/UI/LoginBinder.cs"],
            commands=[], patch_path="development/changes.patch",
            safety_boundary=["isolated clone only"],
        )

    monkeypatch.setattr(ExecutionPipeline, "_apply_development_change_set", fake_apply)
    monkeypatch.setattr(
        ExecutionPipeline, "_bind_project_change_set",
        lambda _self, _intake, _pack, change_set, _output: change_set,
    )
    intake = IntakeRequest(
        goal="Finish the existing project.", output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.PROJECT_DEVELOPMENT], existing_project_id="project",
    )
    output_dir = tmp_path / "source-binding-anchor-recovery"
    output_dir.mkdir()
    pipeline = ExecutionPipeline(tmp_path / "run", gateway=SourceBindingGateway())
    monkeypatch.setattr(
        pipeline, "_development_components",
        lambda _intake, _output=None: (
            ProjectCodeChangeSet, object(),
            DeveloperAgent(
                pipeline.gateway, change_set_schema=ProjectCodeChangeSet,
                source_prefix="project-source/", path_approver=lambda path: path,
            ),
        ),
    )
    _, report, _ = pipeline._run_adk_development_convergence(
        intake=intake, requirements=_requirements(), sources=[_source()],
        source_payload=source_payload,
        contract={"goal": intake.goal, "acceptance_criteria": ["Tests pass."]},
        analysis=_analysis(), output_dir=output_dir,
    )

    assert report.verdict == Verdict.PASS
    assert (output_dir / "development_candidate_promotion_failure_r0.txt").is_file()


def test_continuation_repromotes_paid_pending_proposal_before_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = ProjectCodeChangeSet(
        summary="Prior candidate",
        changes=[{
            "path": "Assets/UI/Status.cs", "base_sha256": None,
            "content": "void Bind() { label.text = \"almost-ready\"; }\n",
            "reason": "Preserve prior progress.",
        }],
    )
    feedback = "Expected ready instead of almost-ready in Assets/UI/Status.cs."
    anchors = DeveloperAgent.exact_edit_anchors(previous, feedback)
    anchor_id = str(anchors[0]["anchors"][0]["anchor_id"])
    pending = AnchoredRangeRepairProjectCodeChangeSet.model_validate({
        "summary": "Finish the status label.",
        "changes": [{
            "path": "Assets/UI/Status.cs", "base_sha256": None,
            "start_anchor": anchor_id, "end_anchor": anchor_id,
            "replace": "void Bind() { label.text = \"ready\"; }",
            "reason": "Use the approved catalog window.",
        }],
    })

    class VerifierOnlyGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def generate_adk_response(self, **kwargs: object) -> types.GenerateContentResponse:
            self.calls.append(str(kwargs["stage"]))
            payload = _verification("PASS").model_dump(mode="json")
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    def fake_apply(
        _self: ExecutionPipeline, _intake: IntakeRequest, _pack: object,
        supplied: ProjectCodeChangeSet, _development_dir: Path,
        _contract: dict[str, object],
    ) -> DevelopmentRun:
        assert '"ready"' in supplied.changes[0].content
        return DevelopmentRun(
            status="verified", repository_name="project", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=["Assets/UI/Status.cs"],
            commands=[], patch_path="development/changes.patch",
            safety_boundary=["isolated clone only"],
        )

    gateway = VerifierOnlyGateway()
    pipeline = ExecutionPipeline(tmp_path / "run", gateway=gateway)
    monkeypatch.setattr(ExecutionPipeline, "_apply_development_change_set", fake_apply)
    monkeypatch.setattr(
        ExecutionPipeline, "_bind_project_change_set",
        lambda _self, _intake, _pack, change_set, _output: change_set,
    )
    monkeypatch.setattr(
        pipeline, "_development_components",
        lambda _intake, _output=None: (
            ProjectCodeChangeSet, object(),
            DeveloperAgent(
                gateway, change_set_schema=ProjectCodeChangeSet,
                source_prefix="project-source/", path_approver=lambda path: path,
            ),
        ),
    )
    output_dir = tmp_path / "pending-promotion"
    output_dir.mkdir()
    (output_dir / "code_change_set.json").write_text(
        previous.model_dump_json(indent=2), encoding="utf-8"
    )
    (output_dir / "development_verification_failure.txt").write_text(
        feedback, encoding="utf-8"
    )
    (output_dir / "development_pending_promotion.json").write_text(
        pending.model_dump_json(indent=2), encoding="utf-8"
    )
    intake = IntakeRequest(
        goal="Finish the existing project.", output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.PROJECT_DEVELOPMENT], existing_project_id="project",
    )

    _, report, _ = pipeline._run_adk_development_convergence(
        intake=intake, requirements=_requirements(), sources=[_source()],
        source_payload=[{
            "name": "project-source/Assets/UI/Status.cs", "priority": "mandatory",
            "requirement_keys": ["repair"], "content": previous.changes[0].content,
            "sha256": "a" * 64,
        }],
        contract={"goal": intake.goal, "acceptance_criteria": ["Tests pass."]},
        analysis=_analysis(), output_dir=output_dir,
    )

    assert report.verdict == Verdict.PASS
    assert gateway.calls == ["final_verification::independent_verification"]
    assert (output_dir / "development_consumed_promotion.json").is_file()
    receipt = json.loads(
        (output_dir / "development_pending_promotion_receipt.json").read_text("utf-8")
    )
    assert receipt["model_call_avoided"] is True


def test_budget_block_writes_resumable_checkpoint(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.")
    source = _source()
    run_dir = _approve(tmp_path, intake, source)


    output_dir = tmp_path / "blocked"
    with pytest.raises(BudgetExceeded):
        ExecutionPipeline(run_dir, gateway=BlockingGateway()).run(
            intake=intake,
            requirements=_requirements(),
            sources=[source],
            output_dir=output_dir,
        )
    checkpoint = (output_dir / "execution_checkpoint.json").read_text(encoding="utf-8")
    assert '"status": "needs_budget"' in checkpoint

def test_exchange_development_returns_verified_web_changes_without_replacing_them_with_excel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = IntakeRequest(
        goal="Improve the existing Exchange web application.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
    )
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    change_set = CodeChangeSet(
        summary="Improve the web application status panel.",
        changes=[{
            "path": "web/src/status.ts",
            "base_sha256": None,
            "content": "export const status = 'verified';\n",
            "reason": "Expose a verified status in the existing web application.",
        }],
    )
    gateway = FakeGateway([_analysis(), change_set, _verification("PASS")])

    monkeypatch.setattr(
        "onebrief.execution_pipeline.execute_toolpacks",
        lambda toolpack_ids, output_dir: ([], []),
    )

    def fake_apply(_self, supplied: CodeChangeSet, output_dir: Path) -> DevelopmentRun:
        assert supplied == change_set
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "changes.patch").write_text("verified patch\n", encoding="utf-8")
        run = DevelopmentRun(
            status="verified",
            repository_name="exchange",
            base_head_sha="a" * 40,
            summary=supplied.summary,
            changed_paths=[item.path for item in supplied.changes],
            commands=[
                DevelopmentCommandResult(
                    command_id="web_build", argv=["npm", "run", "build"],
                    exit_code=0, duration_seconds=0.1, output_tail="passed",
                ),
                DevelopmentCommandResult(
                    command_id="web_tests", argv=["npm", "test"],
                    exit_code=0, duration_seconds=0.1, output_tail="passed",
                ),
            ],
            patch_path="development/changes.patch",
            safety_boundary=["isolated clone only"],
        )
        (output_dir / "development_run.json").write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        return run

    monkeypatch.setattr(
        "onebrief.execution_pipeline.ExchangeDevelopmentToolPack.apply_and_verify", fake_apply
    )
    output_dir = tmp_path / "development-output"
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake, requirements=_requirements(), sources=[source], output_dir=output_dir
    )
    assert result.status == PipelineStatus.COMPLETE
    assert (output_dir / "development" / "changes.patch").is_file()
    assert "web/src/status.ts" in (output_dir / "final.md").read_text(encoding="utf-8")
    assert not (output_dir / "result.xlsx").exists()
    assert [stage for stage, _ in gateway.calls] == [
        "evidence_analysis", "long_form_draft", "independent_verification_r0"
    ]

def test_exchange_development_repairs_two_distinct_failed_regression_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = IntakeRequest(
        goal="Improve the existing Exchange web application.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
    )
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    initial = CodeChangeSet(
        summary="Initial implementation.",
        changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": "export const status = 'initial';\n",
            "reason": "Implement the first attempt.",
        }],
    )
    repaired = CodeChangeSet(
        summary="Regression-safe implementation.",
        changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": "export const status = 'repaired';\n",
            "reason": "Repair the reported regression.",
        }],
    )
    final_repair = CodeChangeSet(
        summary="Compile-safe implementation.",
        changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": "export const status = 'verified';\n",
            "reason": "Repair the second deterministic failure.",
        }],
    )
    gateway = FakeGateway([
        _analysis(), initial, repaired, final_repair, _verification("PASS")
    ])
    monkeypatch.setattr(
        "onebrief.execution_pipeline.execute_toolpacks",
        lambda toolpack_ids, output_dir: ([], []),
    )
    attempts: list[CodeChangeSet] = []

    def fake_apply(_self, supplied: CodeChangeSet, output_dir: Path) -> DevelopmentRun:
        attempts.append(supplied)
        if len(attempts) <= 2:
            raise RuntimeError(
                f"development verification failed: web_tests\nattempt {len(attempts)} failed"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "changes.patch").write_text("repaired patch\n", encoding="utf-8")
        run = DevelopmentRun(
            status="verified", repository_name="exchange", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=[item.path for item in supplied.changes],
            commands=[DevelopmentCommandResult(
                command_id="web_tests", argv=["npm", "test"], exit_code=0,
                duration_seconds=0.1, output_tail="passed",
            )],
            patch_path="development/changes.patch", safety_boundary=["isolated clone only"],
        )
        (output_dir / "development_run.json").write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        return run

    monkeypatch.setattr(
        "onebrief.execution_pipeline.ExchangeDevelopmentToolPack.apply_and_verify", fake_apply
    )
    output_dir = tmp_path / "repair-output"
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake, requirements=_requirements(), sources=[source], output_dir=output_dir
    )

    assert result.status == PipelineStatus.COMPLETE
    assert attempts == [initial, repaired, final_repair]
    assert (output_dir / "development_verification_failure_r0.txt").is_file()
    assert (output_dir / "development_verification_failure_r1.txt").is_file()
    assert (output_dir / "code_change_set_retry_r1.json").is_file()
    assert (output_dir / "code_change_set_retry_r2.json").is_file()
    recovery = json.loads((output_dir / "recovery_decisions.json").read_text(encoding="utf-8"))
    assert recovery["decisions"][0]["error_class"] == "artifact_validation"
    assert recovery["decisions"][0]["action"] == "return_to_agent"
    assert recovery["decisions"][0]["responsible_party"] == "maker"
    assert [stage for stage, _ in gateway.calls] == [
        "evidence_analysis", "long_form_draft", "long_form_draft_verification_retry",
        "long_form_draft_verification_retry",
        "independent_verification_r0",
    ]




def test_developer_retries_truncated_json_with_a_larger_bounded_output() -> None:
    truncated = CodeChangeSet.model_validate_json
    try:
        truncated('{"schema_version":"onebrief-code-change-set-v1","summary":"cut","changes":[{"path":"web/src/status.ts","content":"')
    except Exception as exc:
        parse_error = exc
    change_set = CodeChangeSet(
        summary="Compact verified implementation.",
        changes=[{
            "path": "web/src/status.ts",
            "base_sha256": None,
            "content": "export const status = 'verified';\n",
            "reason": "Implement the requested status.",
        }],
    )
    gateway = FakeGateway([parse_error, change_set])

    result = DeveloperAgent(gateway).run({}, _analysis(), [])

    assert result == change_set
    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft",
        "long_form_draft_compact_retry",
    ]


def test_developer_stops_after_repeated_truncated_output() -> None:
    errors = []
    for _ in range(2):
        try:
            CodeChangeSet.model_validate_json(
                '{"summary":"cut","changes":[{"path":"web/app/page.tsx","content":"'
            )
        except Exception as exc:
            errors.append(exc)
    gateway = FakeGateway(errors)
    developer = DeveloperAgent(gateway)

    with pytest.raises(Exception, match="Invalid JSON"):
        developer.run({}, _analysis(), [])

    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft", "long_form_draft_compact_retry"
    ]
    assert [item.action.value for item in developer.last_recovery_decisions] == [
        "auto_retry", "stop"
    ]

def test_developer_retries_provenance_name_used_as_repository_path() -> None:
    try:
        CodeChangeSet.model_validate({
            "summary": "Invalid provenance path.",
            "changes": [{
                "path": "exchange-source/web/app/page.tsx",
                "base_sha256": "a" * 64,
                "content": "export default function Page(){return null}\n",
                "reason": "Attempted page update.",
            }],
        })
    except Exception as exc:
        path_error = exc
    corrected = CodeChangeSet(
        summary="Correct repository path.",
        changes=[{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "content": "export default function Page(){return null}\n",
            "reason": "Update the approved page.",
        }],
    )
    gateway = FakeGateway([path_error, corrected])

    approved_source = {
        "name": "exchange-source/web/app/page.tsx",
        "sha256": "a" * 64,
        "content": "export default function Page(){return null}\n",
    }
    assert DeveloperAgent(gateway).run({}, _analysis(), [approved_source]) == corrected
    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft", "long_form_draft_compact_retry"
    ]


def test_developer_preserves_new_file_provenance_during_verification_retry() -> None:
    previous = CodeChangeSet(
        summary="Add a bounded test assembly.",
        changes=[{
            "path": "web/app/new-visual-test.tsx",
            "base_sha256": None,
            "content": "export const visualTest = 'initial';\n",
            "reason": "Add the visual test assembly.",
        }],
    )
    mistaken_retry = CodeChangeSet(
        summary="Repair the bounded test assembly.",
        changes=[{
            "path": "web/app/new-visual-test.tsx",
            "base_sha256": "a" * 64,
            "content": "export const visualTest = 'repaired';\n",
            "reason": "Repair the visual test assembly.",
        }],
    )
    gateway = FakeGateway([mistaken_retry])

    result = DeveloperAgent(gateway).run(
        {}, _analysis(), [], verification_feedback="compile failed", previous_change_set=previous
    )

    assert result.changes[0].base_sha256 is None
    assert [stage for stage, _ in gateway.calls] == ["long_form_draft_verification_retry"]


def test_developer_supplies_exact_small_anchors_for_language_repair() -> None:
    previous = ProjectCodeChangeSet(
        summary="Existing localized candidate.",
        changes=[{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "content": (
                "export default function Page() {\n"
                "  return <main><h1>환율 분석</h1><p>시장 요약</p></main>;\n"
                "}\n"
            ),
            "reason": "Current candidate.",
        }],
    )
    repaired = ProposedProjectCodeChangeSet.model_validate({
        "summary": "Translate one exact visible fragment.",
        "changes": [{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "search": "  return <main><h1>환율 분석</h1><p>시장 요약</p></main>;",
            "replace": "  return <main><h1>FX analysis</h1><p>Market summary</p></main>;",
            "reason": "Remove the unexpected CJK fragment.",
        }],
    })

    class AnchorGateway:
        contents = ""

        def generate_json(self, *, contents: str, **_: object):
            self.contents = contents
            return repaired

    gateway = AnchorGateway()
    developer = DeveloperAgent(
        gateway,
        change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/",
        path_approver=lambda path: path,
    )
    result = developer.run(
        {}, _analysis(), [{
            "name": "project-source/web/app/page.tsx",
            "repository_path": "web/app/page.tsx",
            "sha256": "a" * 64,
            "content": previous.changes[0].content,
        }],
        verification_feedback="English locale contains an unexpected CJK fragment.",
        previous_change_set=previous,
    )

    payload = json.loads(gateway.contents)
    assert payload["exact_edit_anchors"][0]["path"] == "web/app/page.tsx"
    assert "환율 분석" in payload["exact_edit_anchors"][0]["anchors"][0]["text"]
    assert result.changes[0].content is not None

    direct = DeveloperAgent.exact_edit_anchors(
        previous, "English locale contains an unexpected CJK fragment."
    )
    assert direct == payload["exact_edit_anchors"]


def test_synthetic_unity_feedback_targets_generated_fallback_block() -> None:
    previous = ProjectCodeChangeSet(
        summary="Candidate with an unsafe test fallback.",
        changes=[{
            "path": "Assets/Tests/PlayMode/Flow.cs", "base_sha256": None,
            "content": (
                "if (dropdown == null) {\n"
                "  GameObject fallbackGo = new GameObject(\"FallbackLanguageDropdown\");\n"
                "  fallbackGo.AddComponent<TMP_Dropdown>();\n"
                "}\n"
            ),
            "reason": "Prior candidate.",
        }],
    )

    anchors = DeveloperAgent.exact_edit_anchors(
        previous,
        "Unity visual test contract: the test must not construct synthetic UI",
    )

    assert anchors
    assert "FallbackLanguageDropdown" in anchors[0]["anchors"][0]["text"]
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: Unity visual test contract: "
        "the test must not construct synthetic UI"
    )
    assert "Delete the generated fallback" in report.revision_instructions[0]


def test_unity_evidence_mutation_feedback_demands_removal_not_replacement() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: "
        "Unity visual test contract: a runtime evidence test must observe product text, "
        "not rewrite visible labels or dropdown option text to hide a localization or glyph defect | "
        "Unity visual test contract: a runtime evidence test must observe the shipped responsive layout, "
        "not configure CanvasScaler properties during verification; repair the production UI instead"
    )

    instructions = " ".join(report.revision_instructions)
    assert "Remove the verification-code block" in instructions
    assert "assigns or replaces TMP_Dropdown option text" in instructions
    assert "assigns CanvasScaler" in instructions
    assert "Do not substitute a different test-side label repair" in instructions


def test_protected_unity_destination_feedback_requires_existing_auth_fixture() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: Unity visual test contract: protected destination "
        "evidence must establish an approved authenticated/test state or exercise the complete "
        "authentication UI; a precondition-free click test must not drive product navigation repairs"
    )

    instruction = " ".join(report.revision_instructions)
    assert "deterministic authentication fixture" in instruction
    assert "test or development account selector" in instruction
    assert "shipped authentication" in instruction
    assert "Do not use a precondition-free Start click" in instruction
    assert "report that missing fixture" in instruction


def test_unity_missing_surface_feedback_requires_separate_executed_captures() -> None:
    report = ExecutionPipeline._development_failure_report(
        "Unity visual evidence requires a distinct rendered scenario for every requested real UI surface: settings"
    )

    instruction = " ".join(report.revision_instructions)
    assert "For Login, capture before starting" in instruction
    assert "for Lobby, capture after the real login/start transition" in instruction
    assert "for Settings, capture only after invoking the real Settings button" in instruction
    assert "LoginToLobbyToSettings is not distinct evidence" in instruction
    assert is_unity_evidence_contract_feedback(
        "Unity visual evidence requires a distinct rendered scenario for settings"
    )
    assert unity_evidence_contract_target_allowed(
        "Assets/JULPAE/Tests/PlayMode/JulpaePlayModeVerification.cs"
    )
    assert not unity_evidence_contract_target_allowed(
        "Assets/JULPAE/Scripts/UI/ModernizedSettingsUI.cs"
    )
    assert is_unity_evidence_contract_feedback(
        "Unity visual test contract: responsive Unity visual evidence must define "
        "and capture both a measured mobile viewport and a desktop viewport"
    )
    assert is_unity_evidence_contract_feedback(
        "Unity visual evidence did not exercise requested locale(s): es"
    )


def test_missing_unity_locale_feedback_requires_measured_locale_scenario() -> None:
    report = ExecutionPipeline._development_failure_report(
        "Unity visual evidence did not exercise requested locale(s): es"
    )

    instruction = " ".join(report.revision_instructions)
    assert "dedicated locale scenario" in instruction
    assert "expected_locale" in instruction
    assert "observed_locale" in instruction
    assert "changed_visible_text_count" in instruction
    assert "missing_glyph_count" in instruction
    assert "exact missing locale" in instruction
    assert "real visible language dropdown" in instruction


def test_missing_unity_harness_is_an_atomic_test_and_asmdef_pair() -> None:
    feedback = (
        "development verification failed: Unity visual test contract: add a discoverable "
        "Unity PlayMode test whose namespace/full name begins with OneBrief.Visual | "
        "Unity visual test contract: add a Unity test .asmdef with "
        "optionalUnityReferences containing TestAssemblies"
    )
    product = ProjectCodeChangeSet(
        summary="Product repair.",
        changes=[{
            "path": "Assets/JULPAE/Scripts/UI/Settings.cs",
            "base_sha256": "a" * 64,
            "content": "public class Settings {}\n",
            "reason": "Repair active product source.",
        }],
    )
    incomplete = CompactProposedProjectCodeChangeSet(
        summary="Only half the harness.",
        changes=[{
            "path": "Assets/Tests/PlayMode/OneBriefVisualFlowTest.cs",
            "base_sha256": None,
            "content": "namespace OneBrief.Visual { public class Flow {} }\n",
            "reason": "Add the test.",
        }],
    )
    complete = CompactProposedProjectCodeChangeSet(
        summary="Complete harness.",
        changes=[
            {
                "path": "Assets/Tests/PlayMode/OneBriefVisualFlowTest.cs",
                "base_sha256": None,
                "content": "namespace OneBrief.Visual { public class Flow {} }\n",
                "reason": "Add the test.",
            },
            {
                "path": "Assets/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
                "base_sha256": None,
                "content": '{"optionalUnityReferences":["TestAssemblies"]}\n',
                "reason": "Add the test assembly.",
            },
        ],
    )

    assert missing_unity_evidence_bundle_paths(
        feedback, product, incomplete
    ) == ["test assembly definition"]
    assert missing_unity_evidence_bundle_paths(feedback, product, complete) == []
    instruction = " ".join(
        ExecutionPipeline._development_failure_report(feedback).revision_instructions
    )
    assert "one atomic two-file repair" in instruction
    assert "Do not submit or retain only one half" in instruction


def test_atomic_unity_evidence_schema_requires_sibling_test_and_asmdef() -> None:
    bundle = AtomicUnityEvidenceBundle.model_validate({
        "summary": "Executable visual evidence pair.",
        "playmode_test": {
            "path": "Assets/Tests/PlayMode/OneBriefVisualFlowTest.cs",
            "content": "namespace OneBrief.Visual { public class Flow {} }\n",
            "reason": "Exercise the real UI flow.",
        },
        "test_assembly": {
            "path": "Assets/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
            "content": '{"optionalUnityReferences":["TestAssemblies"]}\n',
            "reason": "Make the PlayMode test discoverable.",
        },
    })

    promoted = bundle.as_change_set()
    assert [item.path for item in promoted.changes] == [
        "Assets/Tests/PlayMode/OneBriefVisualFlowTest.cs",
        "Assets/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
    ]
    with pytest.raises(ValidationError):
        AtomicUnityEvidenceBundle.model_validate({
            "summary": "Incomplete pair.",
            "playmode_test": bundle.playmode_test.model_dump(),
        })
    with pytest.raises(ValidationError, match="siblings"):
        AtomicUnityEvidenceBundle.model_validate({
            "summary": "Split pair.",
            "playmode_test": bundle.playmode_test.model_dump(),
            "test_assembly": {
                **bundle.test_assembly.model_dump(),
                "path": "Assets/Other/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
            },
        })


def test_atomic_unity_evidence_rehydrates_from_adk_state_dictionary() -> None:
    raw = {
        "schema_version": "onebrief-atomic-unity-evidence-bundle-v1",
        "summary": "Executable visual evidence pair.",
        "playmode_test": {
            "path": "Assets/Tests/PlayMode/OneBriefVisualFlowTest.cs",
            "content": "namespace OneBrief.Visual { public class Flow {} }\n",
            "reason": "Exercise the real UI flow.",
        },
        "test_assembly": {
            "path": "Assets/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
            "content": '{"optionalUnityReferences":["TestAssemblies"]}\n',
            "reason": "Make the PlayMode test discoverable.",
        },
    }

    restored = normalize_atomic_unity_evidence_bundle(raw)

    assert isinstance(restored, CompactProposedProjectCodeChangeSet)
    assert [item.path for item in restored.changes] == [
        "Assets/Tests/PlayMode/OneBriefVisualFlowTest.cs",
        "Assets/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
    ]


def test_unity_evidence_source_repair_rehydrates_to_one_cs_change() -> None:
    raw = {
        "schema_version": "onebrief-unity-evidence-source-repair-v1",
        "summary": "Refine the executable visual evidence source only",
        "playmode_test": {
            "path": "Assets/Tests/PlayMode/OneBrief.Visual.Tests.cs",
            "content": "namespace OneBrief.Visual { public class Tests {} }",
            "reason": "Address the next trusted evidence blocker",
        },
    }

    restored = normalize_atomic_unity_evidence_bundle(raw)

    assert isinstance(restored, CompactProposedProjectCodeChangeSet)
    assert [item.path for item in restored.changes] == [
        "Assets/Tests/PlayMode/OneBrief.Visual.Tests.cs"
    ]


def test_unity_evidence_source_repair_rejects_asmdef() -> None:
    with pytest.raises(ValidationError, match="playmode_test must be a C# source"):
        UnityEvidenceSourceRepair.model_validate({
            "summary": "Do not rewrite the accepted assembly checkpoint",
            "playmode_test": {
                "path": "Assets/Tests/PlayMode/Tests.PlayMode.asmdef",
                "content": "{}",
                "reason": "Invalid post-pair target",
            },
        })


def test_trusted_unity_test_compile_reference_failure_unlocks_only_asmdef() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: unity_compile (exit_code=1) "
        "Assets\\Tests\\PlayMode\\OneBriefVisualTest.cs(8,7): error CS0246: "
        "The type or namespace name 'TMPro' could not be found"
    )

    assert development_maker_schema_for(report, None) is UnityEvidenceAssemblyRepair
    repair = UnityEvidenceAssemblyRepair.model_validate({
        "summary": "Add the compiler-proven test assembly reference",
        "test_assembly": {
            "path": "Assets/Tests/PlayMode/Tests.PlayMode.asmdef",
            "content": '{"references":["Unity.TextMeshPro"],"optionalUnityReferences":["TestAssemblies"]}',
            "reason": "Resolve the trusted CS0246 test assembly failure",
        },
    })
    restored = normalize_atomic_unity_evidence_bundle(
        repair.model_dump(mode="json")
    )

    assert [item.path for item in restored.changes] == [
        "Assets/Tests/PlayMode/Tests.PlayMode.asmdef"
    ]


@pytest.mark.parametrize("feedback", [
    "Unity visual test contract: runtime-evidence.json must use schema_version "
    "onebrief-unity-visual-evidence-v1 and contain a scenarios array",
    "Unity visual test contract: a camera RenderTexture does not capture "
    "ScreenSpaceOverlay UI; temporarily route the real active Canvas",
    "development verification failed: unity_playmode_visual_tests (exit_code=2) "
    "UNITY TEST FAILURES OneBrief.Visual.OneBriefVisualTest.TransitionTest: "
    "LanguageDropdown must exist in the scene. Expected: not null But was: null",
])
def test_all_unity_visual_test_contract_failures_route_to_evidence(feedback: str) -> None:
    assert is_unity_evidence_contract_feedback(feedback) is True


def test_executable_atomic_evidence_is_preserved_when_deeper_checks_fail() -> None:
    candidate = ProjectCodeChangeSet(
        summary="Product and executable evidence candidate.",
        changes=[
            {
                "path": "Assets/UI/Settings.cs",
                "base_sha256": "a" * 64,
                "content": "public class Settings {}\n",
                "reason": "Modernize product UI.",
            },
            {
                "path": "Assets/Tests/PlayMode/Flow.cs",
                "base_sha256": None,
                "content": "namespace OneBrief.Visual { class Flow {} }\n",
                "reason": "Execute the UI flow.",
            },
            {
                "path": "Assets/Tests/PlayMode/Flow.asmdef",
                "base_sha256": None,
                "content": '{"optionalUnityReferences":["TestAssemblies"]}\n',
                "reason": "Discover the test.",
            },
        ],
    )

    assert should_preserve_unity_evidence_checkpoint(
        candidate,
        "Unity visual test contract: the OneBrief.Visual test must write runtime-evidence.json",
    ) is True
    assert should_preserve_unity_evidence_checkpoint(
        candidate,
        "Unity visual test contract: add a discoverable Unity PlayMode test",
    ) is False


def test_atomic_schema_returns_to_bounded_repair_after_pair_exists() -> None:
    feedback = (
        "Unity visual test contract: add a discoverable Unity PlayMode test | "
        "Unity visual test contract: add a Unity test .asmdef"
    )
    report = ExecutionPipeline._development_failure_report(feedback)
    product_only = ProjectCodeChangeSet(
        summary="Product only.",
        changes=[{
            "path": "Assets/UI/Settings.cs",
            "base_sha256": "a" * 64,
            "content": "public class Settings {}\n",
            "reason": "Modernize product UI.",
        }],
    )
    with_pair = ProjectCodeChangeSet(
        summary="Product plus evidence.",
        changes=[
            *product_only.changes,
            ProjectCodeChangeSet.model_validate({
                "summary": "Test.",
                "changes": [{
                    "path": "Assets/Tests/PlayMode/Flow.cs",
                    "base_sha256": None,
                    "content": "namespace OneBrief.Visual { class Flow {} }\n",
                    "reason": "Execute flow.",
                }],
            }).changes[0],
            ProjectCodeChangeSet.model_validate({
                "summary": "Assembly.",
                "changes": [{
                    "path": "Assets/Tests/PlayMode/Flow.asmdef",
                    "base_sha256": None,
                    "content": '{"optionalUnityReferences":["TestAssemblies"]}\n',
                    "reason": "Discover test.",
                }],
            }).changes[0],
        ],
    )

    assert development_maker_schema_for(
        report, product_only.model_dump(mode="json")
    ) is UnityEvidenceJourneyPlan
    assert development_maker_schema_for(
        report, with_pair.model_dump(mode="json")
    ) is UnityEvidenceAnchoredSourceRepair

    detailed_report = ExecutionPipeline._development_failure_report(
        "Unity visual test contract: responsive Unity visual evidence must define and capture "
        "both a measured mobile/portrait viewport and a desktop/landscape viewport before "
        "PlayMode execution"
    )
    assert development_maker_schema_for(
        detailed_report, with_pair.model_dump(mode="json")
    ) is UnityEvidenceAnchoredSourceRepair

    bounded = UnityEvidenceAnchoredSourceRepair.model_validate({
        "summary": "Replace only the batch-safe capture helper.",
        "changes": [{
            "path": "Assets/Tests/PlayMode/Flow.cs",
            "base_sha256": None,
            "start_anchor": "private CapturedImage CaptureScreenshot(",
            "end_anchor": "return image;\n        }",
            "replace": "x" * 10_000,
            "reason": "Repair the established evidence source without echoing the full file.",
        }],
    })
    assert len(bounded.changes[0].replace) == 10_000

    render_texture_report = ExecutionPipeline._development_failure_report(
        "Unity visual test contract: a camera RenderTexture does not capture "
        "ScreenSpaceOverlay UI; temporarily route the real active Canvas through "
        "ScreenSpaceCamera/worldCamera, render it, then restore its prior state | "
        "responsive Unity batchmode evidence must pass each requested viewport width and height "
        "directly into the synchronous RenderTexture capture"
    )
    assert development_maker_schema_for(
        render_texture_report, with_pair.model_dump(mode="json")
    ) is UnityRenderTextureEvidenceRepair

    two_range_repair = UnityRenderTextureEvidenceRepair.model_validate({
        "summary": "Repair viewport calls and the batch-safe capture helper together.",
        "changes": [{
            "path": "Assets/Tests/PlayMode/Flow.cs",
            "base_sha256": None,
            "start_anchor": "var desktop = CaptureScreenshot",
            "end_anchor": "var mobile = CaptureScreenshot",
            "replace": (
                'var desktop = CaptureScreenshot("desktop.png", 1920, 1080);\n'
                'var mobile = CaptureScreenshot("mobile.png", 1080, 2340);'
            ),
            "reason": "Pass the requested viewport directly.",
        }, {
            "path": "Assets/Tests/PlayMode/Flow.cs",
            "base_sha256": None,
            "start_anchor": "private CapturedImage CaptureScreenshot",
            "end_anchor": "return image;",
            "replace": (
                "private CapturedImage CaptureScreenshot(string path, int width, int height) { "
                "RenderTexture rt = new RenderTexture(width, height, 24); "
                "Canvas canvas = Object.FindObjectOfType<Canvas>(); "
                "var oldMode = canvas.renderMode; var oldCamera = canvas.worldCamera; "
                "canvas.renderMode = RenderMode.ScreenSpaceCamera; canvas.worldCamera = camera; "
                "camera.targetTexture = rt; camera.Render(); RenderTexture.active = rt; "
                "texture.ReadPixels(new Rect(0, 0, width, height), 0, 0); "
                "canvas.renderMode = oldMode; canvas.worldCamera = oldCamera; return image; }"
            ),
            "reason": "Route overlay UI through the render camera and restore state.",
        }],
    })
    assert len(two_range_repair.changes) == 2

    with pytest.raises(ValidationError, match="missing required mechanism"):
        UnityRenderTextureEvidenceRepair.model_validate({
            "summary": "Repeat the ineffective camera-only repair.",
            "changes": [{
                "path": "Assets/Tests/PlayMode/Flow.cs",
                "base_sha256": None,
                "start_anchor": "private CapturedImage CaptureScreenshot",
                "end_anchor": "return image;",
                "replace": (
                    "private CapturedImage CaptureScreenshot(string path, int width, int height) { "
                    "RenderTexture rt = new RenderTexture(width, height, 24); "
                    "camera.targetTexture = rt; camera.Render(); RenderTexture.active = rt; "
                    "texture.ReadPixels(new Rect(0, 0, width, height), 0, 0); return image; }"
                ),
                "reason": "Still omits overlay Canvas routing.",
            }],
        })

    with pytest.raises(ValidationError, match="below Tests/PlayMode"):
        UnityEvidenceAnchoredSourceRepair.model_validate({
            "summary": "Do not cross the evidence boundary.",
            "changes": [{
                "path": "Assets/JULPAE/Scripts/Lobby/LobbyResponsiveLayout.cs",
                "base_sha256": "a" * 64,
                "start_anchor": "class LobbyResponsiveLayout",
                "end_anchor": "}",
                "replace": "class LobbyResponsiveLayout {}",
                "reason": "Invalid product-source target.",
            }],
        })

    mixed_product_report = ExecutionPipeline._development_failure_report(
        "Unity visual test contract: changed Unity UI MonoBehaviour "
        "JulpaeLobbySettingsPopupLocalizationBinder is not reachable from any committed "
        ".unity/.prefab script GUID, runtime initialization entrypoint, or other production "
        "source reference; repair or attach the active component instead | "
        "Unity visual test contract: the OneBrief.Visual test must select a real "
        "LanguageDropdown value and dispatch its change"
    )
    assert development_maker_schema_for(
        mixed_product_report, with_pair.model_dump(mode="json")
    ) is ExactRepairProjectCodeChangeSet


def test_detached_product_edit_is_rolled_back_without_losing_evidence() -> None:
    candidate = ProjectCodeChangeSet(
        summary="Detached product edit plus executable evidence.",
        changes=[
            {
                "path": "Assets/JULPAE/Scripts/Localization/JulpaeLobbySettingsPopupLocalizationBinder.cs",
                "base_sha256": "a" * 64,
                "content": "public class JulpaeLobbySettingsPopupLocalizationBinder {}",
                "reason": "Attempted settings localization.",
            },
            {
                "path": "Assets/JULPAE/Tests/PlayMode/OneBrief.Visual.SettingsTest.cs",
                "base_sha256": None,
                "content": "namespace OneBrief.Visual { class SettingsTest {} }",
                "reason": "Execute the active UI.",
            },
            {
                "path": "Assets/JULPAE/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
                "base_sha256": None,
                "content": '{"optionalUnityReferences":["TestAssemblies"]}',
                "reason": "Discover the PlayMode test.",
            },
        ],
    )
    feedback = (
        "changed Unity UI MonoBehaviour JulpaeLobbySettingsPopupLocalizationBinder "
        "is not reachable from any committed .unity/.prefab script GUID; "
        "repair or attach the active component instead"
    )

    assert is_development_product_target_failure(feedback) is True
    rolled_back = rollback_detached_development_changes(candidate, feedback)

    assert [item.path for item in rolled_back.changes] == [
        "Assets/JULPAE/Tests/PlayMode/OneBrief.Visual.SettingsTest.cs",
        "Assets/JULPAE/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
    ]


def test_runtime_product_failure_uses_one_bounded_exact_repair() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: unity_playmode_visual_tests | "
        "UNITY TEST FAILURES OneBrief.Visual.LoginJourney: Must transition to a "
        "Lobby scene after clicking Start Game. Expected True But was False"
    )

    assert is_development_product_target_failure(
        " | ".join([*report.blocking_issues, *report.revision_instructions])
    ) is True
    assert development_maker_schema_for(report, None) is ExactRepairProjectCodeChangeSet

    anchors = [{
        "path": "Assets/Scripts/LoginBinder.cs",
        "anchors": [{
            "anchor_id": "A0123456789ab",
            "text": "button.onClick.AddListener(UnsafeTransition);",
        }],
    }]
    selected = development_maker_schema_for(report, None, anchors)
    assert issubclass(selected, CatalogAnchoredProductRepair)


def test_unseen_large_source_binding_uses_catalog_anchored_repair() -> None:
    report = ExecutionPipeline._development_failure_report(
        "existing file was not included in approved model context: "
        "Assets/JULPAE/Scripts/Lobby/LobbyPopupController.cs"
    )
    anchors = [{
        "path": "Assets/JULPAE/Scripts/Lobby/LobbyPopupController.cs",
        "anchors": [{
            "anchor_id": "A0123456789ab",
            "text": "private void BindSettings() {}",
        }],
    }]

    selected = development_maker_schema_for(report, None, anchors)

    assert issubclass(selected, CatalogAnchoredProductRepair)


def test_unseen_evidence_source_binding_stays_in_evidence_catalog() -> None:
    report = ExecutionPipeline._development_failure_report(
        "existing file was not included in approved model context: "
        "Assets/Tests/PlayMode/OneBriefGeneratedJourneyTest.cs"
    )
    anchors = [{
        "path": "Assets/Tests/PlayMode/OneBriefGeneratedJourneyTest.cs",
        "anchors": [{
            "anchor_id": "A0123456789ab",
            "text": "SelectDropdown(RequireActive(\"LanguageDropdown\"), 1);",
        }],
    }]

    selected = development_maker_schema_for(
        report, None, anchors, ExecutionPhase.EVIDENCE_CONSTRUCTION
    )

    assert issubclass(selected, CatalogAnchoredEvidenceRepair)
    selected.model_validate({
        "summary": "Repair the bounded PlayMode evidence source.",
        "changes": [{
            "path": "Assets/Tests/PlayMode/OneBriefGeneratedJourneyTest.cs",
            "base_sha256": None,
            "anchor_id": "A0123456789ab",
            "replace": "SelectDropdown(RequireActive(\"LanguageDropdown\"), 2);",
            "reason": "Exercise the committed language control.",
        }],
    })


def test_prohibited_runtime_repair_with_anchors_stays_catalog_bounded() -> None:
    report = ExecutionPipeline._development_failure_report(
        "change content requests a prohibited host-runtime capability"
    )
    anchors = [{
        "path": "Assets/JULPAE/Scripts/Localization/JulpaeLanguageSelectorGroup.cs",
        "anchors": [{
            "anchor_id": "Aabcdef012345",
            "text": "private void OnDropdownChanged(int index) {}",
        }],
    }]

    selected = development_maker_schema_for(report, None, anchors)

    assert issubclass(selected, CatalogAnchoredProductRepair)


def test_runtime_product_source_discovery_is_bounded_and_phase_safe() -> None:
    sources = [{
        "repository_path": "Assets/Game/Scripts/Common/SceneRouter.cs",
        "content": "void OpenLobby() { LoadScene(\"Lobby\"); }",
    }, {
        "repository_path": "Assets/Game/Scripts/Login/LoginController.cs",
        "content": "void StartGame() { router.OpenLobby(); }",
    }, {
        "repository_path": "Assets/Game/Scripts/Shop/Catalog.cs",
        "content": "void OpenShop() {}",
    }, {
        "repository_path": "Assets/Game/Tests/PlayMode/LoginJourney.cs",
        "content": "Assert.That(activeScene, Is.EqualTo(\"Lobby\"));",
    }]

    selected = relevant_product_repair_sources(
        sources,
        "LoginJourney failed to transition to Lobby after clicking Start Game",
        limit=2,
    )

    assert [item["repository_path"] for item in selected] == [
        "Assets/Game/Scripts/Login/LoginController.cs",
        "Assets/Game/Scripts/Common/SceneRouter.cs",
    ]
    assert all("/Tests/" not in str(item["repository_path"]) for item in selected)

    anchors = product_failure_edit_anchors(
        sources,
        "LoginJourney failed to transition to Lobby after clicking Start Game",
        source_limit=2,
        anchors_per_source=2,
    )
    assert [item["path"] for item in anchors] == [
        "Assets/Game/Scripts/Login/LoginController.cs",
        "Assets/Game/Scripts/Common/SceneRouter.cs",
    ]
    assert all(
        anchor["anchor_id"].startswith("A") and anchor["text"]
        for group in anchors for anchor in group["anchors"]
    )

    assert active_exact_edit_anchors([], anchors) == anchors
    assert active_exact_edit_anchors(anchors, [{
        "path": "Assets/Game/Scripts/Login/LoginController.cs",
        "anchors": [{"anchor_id": "invented", "text": "x"}],
    }]) == anchors

    candidate = [{
        "path": "Assets/Game/Scripts/Login/LoginController.cs",
        "anchors": [{"anchor_id": "A111111111111", "text": "unsafe candidate"}],
    }]
    merged = candidate_first_edit_anchors(candidate, anchors)
    assert merged[0] == candidate[0]
    assert [item["path"] for item in merged] == [
        "Assets/Game/Scripts/Login/LoginController.cs",
        "Assets/Game/Scripts/Common/SceneRouter.cs",
    ]


def test_duplicate_unity_screenshot_feedback_requires_capture_at_each_real_state() -> None:
    report = ExecutionPipeline._development_failure_report(
        "Unity visual evidence reused an identical screenshot for LoginDesktop and LobbyDesktop; "
        "the capture does not prove the visible UI changed between scenarios"
    )

    instruction = " ".join(report.revision_instructions)
    assert "writes a unique PNG immediately" in instruction
    assert "Capture Login before" in instruction
    assert "Lobby after" in instruction
    assert "Settings after" in instruction
    assert "do not relabel one final-state PNG" in instruction


def test_duplicate_responsive_screenshot_feedback_repairs_render_target_dimensions() -> None:
    report = ExecutionPipeline._development_failure_report(
        "Unity visual evidence reused an identical screenshot for lobby_desktop and lobby_mobile; "
        "the capture does not prove the visible UI changed between scenarios"
    )

    instruction = " ".join(report.revision_instructions)
    assert "accept the requested width and height" in instruction
    assert "RenderTexture, Texture2D, and ReadPixels" in instruction
    assert "Do not rely on Screen.SetResolution" in instruction


def test_project_developer_promotes_dedicated_anchored_range_repair() -> None:
    previous = ProjectCodeChangeSet(
        summary="Existing generated verification source.",
        changes=[{
            "path": "Assets/JULPAE/Tests/PlayMode/Flow.cs",
            "base_sha256": None,
            "content": "before\nBEGIN CAPTURE\nold rows\nEND CAPTURE\nafter\n",
            "reason": "Prior candidate.",
        }],
    )
    proposal = AnchoredRangeRepairProjectCodeChangeSet(
        summary="Capture unique real states.",
        changes=[{
            "path": "Assets/JULPAE/Tests/PlayMode/Flow.cs",
            "base_sha256": None,
            "start_anchor": "BEGIN CAPTURE",
            "end_anchor": "END CAPTURE",
            "replace": "BEGIN CAPTURE\nlogin.png\nlobby.png\nsettings.png\nEND CAPTURE",
            "reason": "Replace one coherent capture region.",
        }],
    )
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(proposal, [], previous, [])

    assert "login.png" in result.changes[0].content
    assert "old rows" not in result.changes[0].content


def test_project_developer_composes_two_unity_evidence_ranges_in_one_file() -> None:
    previous = ProjectCodeChangeSet(
        summary="Existing generated verification source.",
        changes=[{
            "path": "Assets/Tests/PlayMode/Flow.cs",
            "base_sha256": None,
            "content": (
                "CALLS_START\nold desktop call\nold mobile call\nCALLS_END\n"
                "HELPER_START\nold helper\nHELPER_END\n"
            ),
            "reason": "Prior candidate.",
        }],
    )
    proposal = UnityRenderTextureEvidenceRepair(
        summary="Repair two coherent evidence ranges.",
        changes=[{
            "path": "Assets/Tests/PlayMode/Flow.cs",
            "base_sha256": None,
            "start_anchor": "CALLS_START",
            "end_anchor": "CALLS_END",
            "replace": (
                'CALLS_START\nCaptureScreenshot("desktop.png", 1920, 1080);\n'
                'CaptureScreenshot("mobile.png", 1080, 2340);\nCALLS_END'
            ),
            "reason": "Pass explicit viewport sizes.",
        }, {
            "path": "Assets/Tests/PlayMode/Flow.cs",
            "base_sha256": None,
            "start_anchor": "HELPER_START",
            "end_anchor": "HELPER_END",
            "replace": (
                "HELPER_START\nRenderTexture rt = new RenderTexture(width, height, 24); "
                "var oldMode = canvas.renderMode; var oldCamera = canvas.worldCamera; "
                "canvas.renderMode = RenderMode.ScreenSpaceCamera; canvas.worldCamera = camera; "
                "camera.targetTexture = rt; camera.Render(); RenderTexture.active = rt; "
                "texture.ReadPixels(rect, 0, 0); canvas.renderMode = oldMode; "
                "canvas.worldCamera = oldCamera;\nHELPER_END"
            ),
            "reason": "Route and restore overlay Canvas state.",
        }],
    )
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(proposal, [], previous, [])

    assert len(result.changes) == 1
    assert 'CaptureScreenshot("desktop.png", 1920, 1080)' in result.changes[0].content
    assert "RenderMode.ScreenSpaceCamera" in result.changes[0].content


def test_project_developer_anchored_range_tolerates_formatting_only_reflow() -> None:
    previous = ProjectCodeChangeSet(
        summary="Existing generated UI source.",
        changes=[{
            "path": "Assets/JULPAE/Scripts/UI/Lobby.cs",
            "base_sha256": None,
            "content": (
                "before\n    private void ApplyLayout()\n    {\n"
                "        old();\n    }\nafter\n"
            ),
            "reason": "Prior candidate.",
        }],
    )
    proposal = AnchoredRangeRepairProjectCodeChangeSet(
        summary="Repair the layout range.",
        changes=[{
            "path": "Assets/JULPAE/Scripts/UI/Lobby.cs",
            "base_sha256": None,
            "start_anchor": "private void ApplyLayout() {",
            "end_anchor": "old(); }",
            "replace": "private void ApplyLayout()\n    {\n        fixedLayout();\n    }",
            "reason": "Preserve tokens while normalizing copied indentation.",
        }],
    )
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(proposal, [], previous, [])

    assert "fixedLayout();" in result.changes[0].content
    assert "old();" not in result.changes[0].content


def test_project_developer_consumes_an_already_applied_anchored_repair() -> None:
    current = (
        "private void Apply()\n"
        "{\n"
        "    var dropdown = FindDropdown();\n"
        "    bool isEs = dropdown.value == 4;\n"
        "    foreach (var text in texts)\n"
        "    {\n"
        "        text.text = isEs ? \"Ajustes\" : \"Settings\";\n"
        "    }\n"
        "}\n"
    )
    previous = ProjectCodeChangeSet(
        summary="Already promoted candidate.",
        changes=[{
            "path": "Assets/UI/SettingsBinder.cs",
            "base_sha256": "a" * 64,
            "content": current,
            "reason": "Prior candidate.",
        }],
    )
    stale = AnchoredRangeRepairProjectCodeChangeSet(
        summary="Previously paid bounded repair.",
        changes=[{
            "path": "Assets/UI/SettingsBinder.cs",
            "base_sha256": "a" * 64,
            "start_anchor": "    {\n        text.text = \"Settings\";",
            "end_anchor": "        Save();\n    }",
            "replace": (
                "    var dropdown = FindDropdown();\n"
                "    bool isEs = dropdown.value == 4;\n"
                "    {\n"
                "        text.text = isEs ? \"Ajustes\" : \"Settings\";\n"
                "    }"
            ),
            "reason": "Bind the requested locale.",
        }],
    )
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(stale, [], previous, [])

    assert result.changes[0].content == current


def test_development_failure_report_redirects_detached_unity_binding() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: changed Unity UI MonoBehaviour SettingsBinder "
        "is not reachable from any committed .unity/.prefab script GUID, runtime initialization "
        "entrypoint, or other production source reference"
    )

    assert report.verdict == Verdict.REVISE
    assert "attached_script_paths" in report.revision_instructions[0]
    assert "Do not revise that file again" in report.revision_instructions[0]


def test_project_developer_does_not_consume_partially_applied_repair() -> None:
    previous = ProjectCodeChangeSet(
        summary="Partially changed candidate.",
        changes=[{
            "path": "Assets/UI/SettingsBinder.cs",
            "base_sha256": "a" * 64,
            "content": "var dropdown = FindDropdown();\nbool isEs = false;\n",
            "reason": "Prior candidate.",
        }],
    )
    stale = AnchoredRangeRepairProjectCodeChangeSet(
        summary="Unproven repair.",
        changes=[{
            "path": "Assets/UI/SettingsBinder.cs",
            "base_sha256": "a" * 64,
            "start_anchor": "old start",
            "end_anchor": "old end",
            "replace": (
                "var dropdown = FindDropdown();\n"
                "bool isEs = dropdown.value == 4;\n"
                "text.text = isEs ? \"Ajustes\" : \"Settings\";"
            ),
            "reason": "Bind the requested locale.",
        }],
    )
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    with pytest.raises(ValueError, match="edit anchors could not rediscover"):
        developer.promote_candidate(stale, [], previous, [])


def test_playmode_anchor_normalizes_javascript_style_csharp_interpolation() -> None:
    previous = ProjectCodeChangeSet(
        summary="Existing generated evidence source.",
        changes=[{
            "path": "Assets/Tests/PlayMode/VisualFlowTest.cs",
            "base_sha256": None,
            "content": (
                "before\n            var scenarios = new List<string>();\n"
                "            string json = $\"{evidence}\";\n"
                "            File.WriteAllText(\"runtime-evidence.json\", json);\nafter\n"
            ),
            "reason": "Prior generated candidate.",
        }],
    )
    proposal = AnchoredRangeRepairProjectCodeChangeSet(
        summary="Repair the generated evidence range.",
        changes=[{
            "path": "Assets/Tests/PlayMode/VisualFlowTest.cs",
            "base_sha256": None,
            "start_anchor": "            var scenarios = new List<string>();",
            "end_anchor": (
                "            string json = ${\"{evidence}\";\n"
                "            File.WriteAllText(\"runtime-evidence.json\", json);"
            ),
            "replace": "            WriteVerifiedEvidence();",
            "reason": "Use the exact candidate range after safe selector normalization.",
        }],
    )
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(proposal, [], previous, [])

    assert "WriteVerifiedEvidence();" in result.changes[0].content
    assert "var scenarios" not in result.changes[0].content


def test_project_developer_normalizes_complete_javascript_wrapped_csharp_json_selector() -> None:
    current = (
        'before\n            string json = $"{{\\"schema_version\\":'
        '\\"onebrief-unity-visual-evidence-v1\\",\\"scenarios\\":['
        '{string.Join(",", scenarios)}],\\"missing_glyph_count\\":'
        '{missingGlyphs}}}";\n'
        '            File.WriteAllText("onebrief-evidence/runtime-evidence.json", json);\n'
        '        }\nafter\n'
    )
    javascript_selector = (
        '            string json = ${"{\\"schema_version\\":'
        '\\"onebrief-unity-visual-evidence-v1\\",\\"scenarios\\":['
        '{string.Join(",", scenarios)}],\\"missing_glyph_count\\":'
        '{missingGlyphs}}"};\n'
        '            File.WriteAllText("onebrief-evidence/runtime-evidence.json", json);\n'
        '        }'
    )
    previous = ProjectCodeChangeSet(
        summary="Existing generated evidence source.",
        changes=[{
            "path": "Assets/Tests/PlayMode/VisualFlowTest.cs",
            "base_sha256": None,
            "content": current,
            "reason": "Prior generated candidate.",
        }],
    )
    proposal = AnchoredRangeRepairProjectCodeChangeSet(
        summary="Repair locale evidence.",
        changes=[{
            "path": "Assets/Tests/PlayMode/VisualFlowTest.cs",
            "base_sha256": None,
            "start_anchor": javascript_selector,
            "end_anchor": javascript_selector,
            "replace": "            WriteMeasuredLocaleEvidence();",
            "reason": "Promote a uniquely rediscovered generated C# range.",
        }],
    )
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(proposal, [], previous, [])

    assert "WriteMeasuredLocaleEvidence();" in result.changes[0].content
    assert "schema_version" not in result.changes[0].content


def test_semantic_visual_candidate_repairs_use_bounded_ranges() -> None:
    assert development_repair_requires_anchored_range(
        multi_state_evidence_repair=False,
        unity_evidence_topology_repair=False,
        semantic_visual_repair=True,
    )
    assert not development_repair_requires_anchored_range(
        multi_state_evidence_repair=False,
        unity_evidence_topology_repair=False,
        semantic_visual_repair=False,
    )


def test_project_developer_compact_retry_keeps_existing_files_as_exact_edits() -> None:
    previous = ProjectCodeChangeSet(
        summary="Existing candidate.",
        changes=[{
            "path": "web/app/page.tsx", "base_sha256": "a" * 64,
            "content": "export const label = '한국어';\n",
            "reason": "Current candidate.",
        }],
    )
    compact = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Translate the remaining label.",
        "changes": [{
            "path": "web/app/page.tsx", "base_sha256": "a" * 64,
            "search": "'한국어'", "replace": "'English'",
            "reason": "Remove the remaining CJK label.",
        }],
    })
    try:
        ProposedProjectCodeChangeSet.model_validate_json(
            '{"summary":"truncated","changes":[{"path":"web/app/page.tsx","search":"'
        )
    except Exception as exc:
        truncated = exc

    class CompactGateway:
        schemas: list[type] = []
        calls = 0

        def generate_json(self, *, schema: type, **_: object):
            self.schemas.append(schema)
            self.calls += 1
            if self.calls == 1:
                raise truncated
            return compact

    gateway = CompactGateway()
    developer = DeveloperAgent(
        gateway,
        change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/",
        path_approver=lambda path: path,
    )
    result = developer.run(
        {}, _analysis(), [{
            "name": "project-source/web/app/page.tsx",
            "repository_path": "web/app/page.tsx",
            "sha256": "a" * 64,
            "content": previous.changes[0].content,
        }],
        verification_feedback="English locale contains an unexpected CJK fragment.",
        previous_change_set=previous,
    )

    assert gateway.schemas == [
        CompactProposedProjectCodeChangeSet,
        CompactProposedProjectCodeChangeSet,
    ]
    assert result.changes[0].content == "export const label = 'English';\n"


def test_project_developer_compact_retry_can_add_bounded_new_evidence_files() -> None:
    proposal = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Add the missing Unity test contract.",
        "changes": [{
            "path": "Assets/Tests/PlayMode/OneBrief.Visual.Tests.asmdef",
            "base_sha256": None,
            "content": '{"optionalUnityReferences":["TestAssemblies"]}\n',
            "reason": "The verifier explicitly requires a discoverable PlayMode test assembly.",
        }],
    })
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(proposal, [])

    assert result.changes[0].path.endswith("OneBrief.Visual.Tests.asmdef")
    assert "TestAssemblies" in result.changes[0].content


def test_project_developer_rejects_static_runtime_evidence_with_actionable_boundary() -> None:
    proposal = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Add runtime evidence.",
        "changes": [{
            "path": "onebrief-evidence/runtime-evidence.json",
            "base_sha256": None,
            "content": '{"visual_verification":"passed"}\n',
            "reason": "Satisfy the missing runtime evidence blocker.",
        }],
    })
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda _path: None,
    )

    with pytest.raises(ValueError, match="generated by editing an approved test source"):
        developer.promote_candidate(proposal, [])


def test_compact_repair_schema_allows_only_two_coherent_changed_paths() -> None:
    item = {
        "path": "Assets/Tests/One.cs",
        "base_sha256": None,
        "content": "class One {}\n",
        "reason": "One bounded repair.",
    }
    with pytest.raises(Exception):
        CompactProposedProjectCodeChangeSet.model_validate({
            "summary": "Too many repair paths.",
            "changes": [
                item,
                {**item, "path": "Assets/Tests/Two.cs"},
                {**item, "path": "Assets/Tests/Three.cs"},
            ],
        })


def test_anchored_repair_bounds_copied_whole_range_before_validation() -> None:
    copied_range = "BEGIN\n" + ("x" * 2_400) + "\nEND"

    proposal = AnchoredRangeRepairProjectCodeChangeSet.model_validate({
        "summary": "Repair one coherent range.",
        "changes": [{
            "path": "Assets/Tests/PlayMode/Flow.cs",
            "base_sha256": None,
            "start_anchor": copied_range,
            "end_anchor": copied_range,
            "replace": "BEGIN\nfixed\nEND",
            "reason": "The provider copied the entire selected range as both boundaries.",
        }],
    })

    change = proposal.changes[0]
    assert change.start_anchor == copied_range[:1000]
    assert change.end_anchor == copied_range[-1000:]


def test_duplicate_removal_uses_the_nearest_repeated_start_before_unique_end() -> None:
    repeated = "            var closeButton = FindClose();\n            Close();\n"
    ending = "            WriteEvidence();\n"
    candidate = "void Flow() {\n" + repeated + repeated + ending + "}\n"
    previous = ProjectCodeChangeSet(
        summary="Candidate with one duplicated navigation block.",
        changes=[{
            "path": "Assets/Tests/PlayMode/VisualFlowTest.cs",
            "base_sha256": None,
            "content": candidate,
            "reason": "Prior candidate.",
        }],
    )
    proposal = AnchoredRangeRepairProjectCodeChangeSet.model_validate({
        "summary": "Remove the duplicate navigation.",
        "changes": [{
            "path": "Assets/Tests/PlayMode/VisualFlowTest.cs",
            "base_sha256": None,
            "start_anchor": repeated.splitlines()[0],
            "end_anchor": ending.strip(),
            "replace": ending,
            "reason": "Remove the second identical duplicate navigation block.",
        }],
    })
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(proposal, [], previous, [])

    content = result.changes[0].content
    assert content.count("var closeButton = FindClose();") == 1
    assert content.count("WriteEvidence();") == 1


def test_project_developer_compact_retry_rejects_full_existing_file_replacement() -> None:
    source = "export const label = 'old';\n"
    proposal = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Unsafe broad replacement.",
        "changes": [{
            "path": "web/app/page.tsx",
            "base_sha256": None,
            "content": "export const label = 'new';\n",
            "reason": "Attempt a broad replacement.",
        }],
    })
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    with pytest.raises(ValueError, match="may only add a new file"):
        developer.promote_candidate(proposal, [{
            "repository_path": "web/app/page.tsx",
            "sha256": "a" * 64,
            "content": source,
        }])


def test_exact_repair_can_replace_a_bounded_generated_candidate_file() -> None:
    from onebrief.generic_development_toolpack import ExactRepairProjectCodeChangeSet

    previous = ProjectCodeChangeSet(
        summary="Generated test candidate.",
        changes=[{
            "path": "Assets/Tests/PlayMode/OneBriefVisualTests.cs",
            "base_sha256": None,
            "content": "namespace Old { public class Test {} }\n",
            "reason": "Initial generated test.",
        }],
    )
    proposal = ExactRepairProjectCodeChangeSet.model_validate({
        "summary": "Correct the generated test.",
        "changes": [{
            "path": "Assets/Tests/PlayMode/OneBriefVisualTests.cs",
            "base_sha256": None,
            "content": "namespace OneBrief.Visual { public class Test {} }\n",
            "reason": "Repair only the bounded generated candidate file.",
        }],
    })
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(proposal, [], previous)

    assert result.changes[0].content.startswith("namespace OneBrief.Visual")


def test_deterministic_development_failures_become_independent_repair_slices() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: missing PNG | missing scene interaction | missing glyph check"
    )

    assert report.verdict == Verdict.REVISE
    assert [item.evidence for item in report.criterion_checks] == [
        "missing PNG", "missing scene interaction", "missing glyph check"
    ]


def test_unity_png_failure_explains_direct_test_evidence_contract() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: Unity visual test contract: must capture PNG runtime evidence"
    )

    instruction = report.revision_instructions[0]
    assert "OneBrief.Visual" in instruction
    assert "OneBriefAtomicScreenshot.Capture" in instruction
    assert "WriteManifestAtomically" in instruction


def test_fake_unity_evidence_helper_failure_requires_the_supplied_exact_api() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: the PlayMode test must not declare, duplicate, or replace "
        "OneBriefAtomicScreenshot | OneBriefAtomicScreenshot.Capture requires exactly the evidence-relative "
        ".png path | OneBriefAtomicScreenshot.WriteManifestAtomically requires a complete schema; a "
        "zero-argument log-only call cannot publish durable evidence"
    )

    instructions = "\n".join(report.revision_instructions)
    assert "Delete the test-side OneBriefAtomicScreenshot class completely" in instructions
    assert "CaptureScenario" in instructions
    assert "Do not create a fallback or wrapper" in instructions
    assert "WriteManifestAtomically(scenarioReceipt)" in instructions


def test_scenario_receipt_compile_error_gets_exact_evidence_only_repair() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: unity_compile: "
        "Assets/Tests/PlayMode/OneBriefVisualLoginTest.cs(48,30): error CS0029: "
        "Cannot implicitly convert type 'OneBrief.Visual.OneBriefAtomicScreenshot.ScenarioReceipt' "
        "to 'string'"
    )

    instruction = report.revision_instructions[0]
    assert "CaptureScenario returns" in instruction
    assert "ScenarioReceipt, not string" in instruction
    assert "Do not edit product source" in instruction


def test_atomic_unity_harness_instruction_publishes_exact_helper_contract() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: Unity visual test contract: add a discoverable "
        "Unity PlayMode test whose namespace/full name begins with OneBrief.Visual | "
        "Unity visual test contract: add a Unity test .asmdef with "
        "optionalUnityReferences containing TestAssemblies"
    )

    instructions = "\n".join(report.revision_instructions)
    assert "atomic two-file repair" in instructions
    assert "runner supplies immutable OneBriefAtomicScreenshot" in instructions
    assert "CaptureScenario(scenarioId, observedState, interaction, assertionCount" in instructions
    assert "WriteManifestAtomically(scenarioReceipt)" in instructions


def test_related_unity_contract_failures_remain_one_coherent_repair_scope() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: "
        "Unity visual test contract: must capture PNG runtime evidence | "
        "Unity visual test contract: must find visible UI | "
        "Unity visual test contract: add a test asmdef"
    )

    assert len(report.criterion_checks) == 1
    assert len(report.revision_instructions) >= 2
    assert "coherent evidence-harness repair" in report.revision_instructions[-1]
    assert "Do not edit product UI" in report.revision_instructions[-1]


def test_unity_discovery_failure_targets_test_namespace_not_asmdef() -> None:
    report = ExecutionPipeline._development_failure_report(
        "development verification failed: Unity visual test contract: add a discoverable "
        "Unity PlayMode test whose namespace/full name begins with OneBrief.Visual"
    )

    instruction = report.revision_instructions[0]
    assert "namespace begins exactly with OneBrief.Visual" in instruction
    assert "Do not change or re-emit the asmdef" in instruction


def test_project_developer_resolves_verified_anchor_id_without_retyping_source() -> None:
    previous = ProjectCodeChangeSet(
        summary="Existing candidate.",
        changes=[{
            "path": "web/app/page.tsx", "base_sha256": "a" * 64,
            "content": (
                "export default function Page() {\n"
                "  return <h2>Historical Model Performance</h2>;\n"
                "}\n"
            ),
            "reason": "Current candidate.",
        }],
    )
    anchors = DeveloperAgent.exact_edit_anchors(
        previous, "Expected 과거 시점별 모델 성능 instead of Historical Model Performance."
    )
    anchor = anchors[0]["anchors"][0]
    proposal = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Restore the Korean heading.",
        "changes": [{
            "path": "web/app/page.tsx", "base_sha256": "a" * 64,
            "anchor_id": anchor["anchor_id"],
            "replace": (
                "export default function Page() {\n"
                "  return <h2>과거 시점별 모델 성능</h2>;\n"
                "}"
            ),
            "reason": "Replace the verified source window by ID.",
        }],
    })
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )
    result = developer.promote_candidate(
        proposal,
        [{
            "repository_path": "web/app/page.tsx", "sha256": "a" * 64,
            "content": previous.changes[0].content,
        }],
        previous,
        anchors,
    )

    assert "과거 시점별 모델 성능" in result.changes[0].content
    assert "Historical Model Performance" not in result.changes[0].content


def test_project_developer_normalizes_catalog_id_misplaced_in_range_fields() -> None:
    previous = ProjectCodeChangeSet(
        summary="Existing Unity candidate.",
        changes=[{
            "path": "Assets/UI/ModernizedSettingsUI.cs", "base_sha256": None,
            "content": (
                "void Bind() {\n"
                "    languageDropdown.SetOptions(new [] { \"English\", \"Español\" });\n"
                "}\n"
            ),
            "reason": "Current generated production candidate.",
        }],
    )
    anchors = DeveloperAgent.exact_edit_anchors(
        previous, "Missing glyphs are visible in Español language dropdown."
    )
    anchor_id = str(anchors[0]["anchors"][0]["anchor_id"])
    misplaced = AnchoredRangeRepairProjectCodeChangeSet.model_validate({
        "summary": "Use a supported Spanish label.",
        "changes": [{
            "path": "Assets/UI/ModernizedSettingsUI.cs",
            "base_sha256": None,
            "start_anchor": anchor_id,
            "end_anchor": anchor_id,
            "replace": (
                "void Bind() {\n"
                "    languageDropdown.SetOptions(new [] { \"English\", \"Spanish\" });\n"
                "}"
            ),
            "reason": "Replace the approved catalog window without another maker call.",
        }],
    })
    developer = DeveloperAgent(
        object(), change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/", path_approver=lambda path: path,
    )

    result = developer.promote_candidate(misplaced, [], previous, anchors)

    assert "Spanish" in result.changes[0].content
    assert "Español" not in result.changes[0].content


def test_developer_rejects_blind_existing_file_replacement_and_uses_sidecar() -> None:
    blind = CodeChangeSet(
        summary="Blind replacement.",
        changes=[{
            "path": "web/src/unseen.ts",
            "base_sha256": "b" * 64,
            "content": "export const unseen = true;\n",
            "reason": "Attempt to replace an uninspected file.",
        }],
    )
    sidecar = CodeChangeSet(
        summary="Safe additive implementation.",
        changes=[{
            "path": "web/src/localization-sidecar.ts",
            "base_sha256": None,
            "content": "export const locales = ['ja'];\n",
            "reason": "Add a bounded sidecar without replacing unseen source.",
        }],
    )
    gateway = FakeGateway([blind, sidecar])

    result = DeveloperAgent(gateway).run({}, _analysis(), [{
        "name": "exchange-source/web/app/page.tsx",
        "sha256": "a" * 64,
        "content": "export default function Page(){return null}\n",
    }])

    assert result == sidecar
    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft", "long_form_draft_compact_retry"
    ]



def test_developer_separates_repository_path_from_source_provenance_name() -> None:
    change_set = CodeChangeSet(
        summary="Use the actual repository path.",
        changes=[{
            "path": "web/app/page.tsx",
            "base_sha256": "a" * 64,
            "content": "export default function Page(){return <main>FX</main>}\n",
            "reason": "Improve the approved page.",
        }],
    )

    class CapturingGateway:
        contents = ""

        def generate_json(self, *, contents: str, **_: object):
            self.contents = contents
            return change_set

    gateway = CapturingGateway()
    result = DeveloperAgent(gateway).run({}, _analysis(), [
        {"name": "exchange-source/web/app/page.tsx", "sha256": "a" * 64,
         "content": "export default function Page(){return null}\n"},
        {"name": "exchange-source/docs/product.md", "sha256": "b" * 64,
         "content": "Evidence only."},
    ])

    payload = json.loads(gateway.contents)
    source = payload["approved_repository_files"][0]
    assert result == change_set
    assert source["name"] == "exchange-source/web/app/page.tsx"
    assert source["repository_path"] == "web/app/page.tsx"
    assert payload["approved_repository_files"][1]["repository_path"] is None

    assert source["source_role"] == "editable_source"
    assert payload["approved_repository_files"][1]["source_role"] == "read_only_context"


def test_project_developer_discards_unapproved_infrastructure_proposals() -> None:
    proposal = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Build the approved product page.",
        "changes": [
                {
                    "path": "src/index.html",
                    "base_sha256": "a" * 64,
                    "search": "<main>placeholder</main>",
                    "replace": "<main>JULPAE</main>",
                    "reason": "Implement the approved product surface.",
                },
            {
                "path": "scripts/server.mjs",
                "base_sha256": None,
                "content": "process.env.PORT; import('node:http');\n",
                "reason": "Attempt to replace fixed infrastructure.",
            },
        ],
    })
    gateway = FakeGateway([proposal])
    developer = DeveloperAgent(
        gateway,
        change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/",
        path_approver=lambda path: path if path.startswith(("src/", "tests/")) else None,
    )

    result = developer.run({}, _analysis(), [{
        "name": "project-source/src/index.html",
        "sha256": "a" * 64,
        "content": "<!doctype html><main>placeholder</main>\n",
    }])

    assert [change.path for change in result.changes] == ["src/index.html"]
    assert result.changes[0].content.endswith("JULPAE</main>\n")


def test_project_developer_promotes_exact_search_replace_without_rewriting_file() -> None:
    original = '<select id="langSelect">\n<option>한국어</option>\n</select>\n'
    proposal = CompactProposedProjectCodeChangeSet.model_validate({
        "summary": "Add one accessible label.",
        "changes": [{
            "path": "src/index.html",
            "base_sha256": "a" * 64,
            "search": '<select id="langSelect">',
            "replace": '<select id="langSelect" aria-label="언어 선택 / Select Language">',
            "reason": "Expose the control name to assistive technology.",
        }],
    })
    developer = DeveloperAgent(
        FakeGateway([proposal]),
        change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/",
        path_approver=lambda path: path if path == "src/index.html" else None,
    )

    result = developer.run({}, _analysis(), [{
        "name": "project-source/src/index.html",
        "sha256": "a" * 64,
        "content": original,
    }])

    assert result.changes[0].content == original.replace(
        '<select id="langSelect">',
        '<select id="langSelect" aria-label="언어 선택 / Select Language">',
    )
    assert "<option>한국어</option>" in result.changes[0].content


def test_project_developer_composes_multiple_exact_edits_for_one_path() -> None:
    original = "<main><h1>Old</h1><p>Draft</p></main>"
    proposal = ProposedProjectCodeChangeSet.model_validate({
        "summary": "Complete two bounded edits in one approved file.",
        "changes": [
            {
                "path": "src/index.html",
                "base_sha256": "a" * 64,
                "search": "<h1>Old</h1>",
                "replace": "<h1>Ready</h1>",
                "reason": "Update the primary heading.",
            },
            {
                "path": "src/index.html",
                "base_sha256": "a" * 64,
                "search": "<p>Draft</p>",
                "replace": "<p>Verified</p>",
                "reason": "Update the supporting status.",
            },
        ],
    })
    developer = DeveloperAgent(
        FakeGateway([]),
        change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/",
        path_approver=lambda path: path if path == "src/index.html" else None,
    )

    result = developer.promote_candidate(proposal, [{
        "repository_path": "src/index.html",
        "sha256": "a" * 64,
        "content": original,
    }])

    assert len(result.changes) == 1
    assert result.changes[0].content == (
        "<main><h1>Ready</h1><p>Verified</p></main>"
    )


def test_exact_retry_falls_back_to_approved_source_after_corrupt_full_file() -> None:
    original = '<select id="langSelect">\n<option>한국어</option>\n</select>\n'
    proposal = ProposedProjectCodeChangeSet.model_validate({
        "summary": "Repair with a bounded exact edit.",
        "changes": [{
            "path": "src/index.html",
            "search": '<select id="langSelect">',
            "replace": '<select id="langSelect" aria-label="언어 선택 / Select Language">',
            "reason": "Discard the rejected rewrite and use the approved baseline.",
        }],
    })
    developer = DeveloperAgent(
        FakeGateway([]),
        change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/",
        path_approver=lambda path: path if path == "src/index.html" else None,
    )
    rejected = ProjectCodeChangeSet(
        summary="Rejected rewrite",
        changes=[{
            "path": "src/index.html",
            "base_sha256": "a" * 64,
            "content": "corrupted full-file output",
            "reason": "Rejected by verification.",
        }],
    )

    result = developer.promote_candidate(proposal, [{
        "repository_path": "src/index.html",
        "sha256": "a" * 64,
        "content": original,
    }], rejected)

    assert "corrupted" not in result.changes[0].content
    assert "aria-label" in result.changes[0].content
    assert "<option>한국어</option>" in result.changes[0].content

def test_exact_retry_accepts_unique_jsx_whitespace_reflow() -> None:
    original = '<header>\n  <select id="lang-select">\n    <option value="ko">KO</option>\n  </select>\n</header>\n'
    proposal = ProposedProjectCodeChangeSet.model_validate({
        "summary": "Repair the language control.",
        "changes": [{
            "path": "web/app/page.tsx",
            "search": '<select id="lang-select"> <option value="ko">KO</option> </select>',
            "replace": '<select id="lang-select"><option value="en">English</option></select>',
            "reason": "Apply the unique bounded JSX repair despite formatting-only drift.",
        }],
    })
    developer = DeveloperAgent(
        FakeGateway([]),
        change_set_schema=ProjectCodeChangeSet,
        source_prefix="project-source/",
        path_approver=lambda path: path if path == "web/app/page.tsx" else None,
    )

    result = developer.promote_candidate(proposal, [{
        "repository_path": "web/app/page.tsx",
        "sha256": "a" * 64,
        "content": original,
    }])

    assert '<option value="en">English</option>' in result.changes[0].content
    assert result.changes[0].content.startswith("<header>")


def test_exchange_development_budget_includes_compact_retry_capacity() -> None:
    intake = IntakeRequest(goal="Improve Exchange.", toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT])
    estimate = estimate_budget(intake, _requirements())
    draft = next(stage for stage in estimate.stages if stage.stage == "long_form_draft")
    assert draft.output_tokens_per_call == DEVELOPER_OUTPUT_CAP
    assert DEVELOPER_OUTPUT_CAP >= 20_000
    assert (draft.minimum_calls, draft.recommended_calls, draft.maximum_calls) == (1, 3, 3)


def test_resume_reuses_completed_analysis_without_a_new_model_call(tmp_path: Path) -> None:
    intake = IntakeRequest(goal="Create a guide.")
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    output_dir = tmp_path / "resumed"
    output_dir.mkdir()
    (output_dir / "analysis.json").write_text(
        _analysis().model_dump_json(indent=2),
        encoding="utf-8",
    )
    gateway = FakeGateway([_draft(), _verification("PASS")])

    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=_requirements(),
        sources=[source],
        output_dir=output_dir,
    )

    assert result.status == PipelineStatus.COMPLETE
    assert [stage for stage, _ in gateway.calls] == [
        "long_form_draft",
        "independent_verification_r0",
    ]
    final_text = (output_dir / "final.md").read_text(encoding="utf-8")
    assert "Remote Work Guide" in final_text
    assert json.loads((output_dir / "temperament_decisions.json").read_text(encoding="utf-8")) == []


def test_reused_research_draft_cannot_bypass_evidence_sufficiency_gate(
    tmp_path: Path,
) -> None:
    intake = IntakeRequest(
        goal="유사 제품이 없으면 제품 콘셉트까지만 정해줘.",
        public_research_allowed=True,
        max_revision_rounds=0,
    )
    source = InternalSource(
        name="public_research.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["market"],
        content="[W01] Product — https://example.com/products/a",
    )
    requirements = RequirementsAnalysis(
        supported=True,
        support_reason="Supported.",
        normalized_goal="Select a differentiated product concept.",
        deliverables=["Product concept"],
        mandatory_information=[],
        optional_information=[],
        acceptance_criteria=["Compare current products."],
        completion_contract=CompletionContract(
            target_state="A sourced product concept is complete.",
            quality_criteria=[QualityCriterion(
                criterion_id="Q01",
                description="현재 시장의 유사 제품 조사 및 차별성 검증",
                evidence_required="비교한 제품과 직접 출처를 제시한다.",
            )],
        ),
        assumptions=[],
        consolidated_questions=[],
        ready_for_estimate=True,
    )
    run_dir = _approve(tmp_path, intake, source)
    output_dir = tmp_path / "reused-research"
    output_dir.mkdir()
    (output_dir / "analysis.json").write_text(
        _analysis().model_dump_json(indent=2), encoding="utf-8"
    )
    (output_dir / "public_research.json").write_text(
        json.dumps({
            "query": intake.goal,
            "answer_markdown": "Product evidence.",
            "sources": [{
                "source_id": "W01",
                "title": "Product",
                "url": "https://example.com/products/a",
                "domain": "example.com",
            }],
            "search_queries": ["product"],
            "search_suggestions_html": "",
        }),
        encoding="utf-8",
    )
    (output_dir / "draft_r0.json").write_text(
        _draft(
            "이 원료는 시장에 유사 제품이 없는 독점적 후보입니다. [F01]\n\n"
            "https://example.com/products/a\n\n"
            "## 생산 단계\n\n품목제조신고를 즉시 진행합니다."
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    gateway = FakeGateway([_verification("PASS")])

    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake,
        requirements=requirements,
        sources=[source],
        output_dir=output_dir,
    )

    assert result.status == PipelineStatus.PARTIAL
    evidence = json.loads(
        (output_dir / "evidence_sufficiency_r0.json").read_text(encoding="utf-8")
    )
    kinds = {item["kind"] for item in evidence["issues"]}
    assert "unbounded_absence_claim" in kinds
    assert "out_of_scope_followup" in kinds
    verification = json.loads(
        (output_dir / "verification_r0.json").read_text(encoding="utf-8")
    )
    assert verification["verdict"] == "REVISE"


def test_exchange_development_returns_failed_acceptance_to_the_software_maker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    intake = IntakeRequest(
        goal="Improve the existing Exchange web application.",
        output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
        max_revision_rounds=1,
    )
    source = _source()
    run_dir = _approve(tmp_path, intake, source)
    initial = CodeChangeSet(
        summary="Claims a feature without implementing it.",
        changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": "export const status = 'initial';\n",
            "reason": "Initial incomplete implementation.",
        }],
    )
    corrected = CodeChangeSet(
        summary="Implements the verified feature.",
        changes=[{
            "path": "web/src/status.ts", "base_sha256": None,
            "content": "export const status = 'corrected';\n",
            "reason": "Address the independent implementation review.",
        }],
    )
    gateway = FakeGateway([
        _analysis(), initial, _verification("REVISE"), corrected, _verification("PASS")
    ])
    monkeypatch.setattr(
        "onebrief.execution_pipeline.execute_toolpacks",
        lambda toolpack_ids, output_dir: ([], []),
    )
    attempts: list[CodeChangeSet] = []

    def fake_apply(_self, supplied: CodeChangeSet, output_dir: Path) -> DevelopmentRun:
        attempts.append(supplied)
        output_dir.mkdir(parents=True, exist_ok=True)
        changed = output_dir / "changed_files" / "web" / "src" / "status.ts"
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text(supplied.changes[0].content, encoding="utf-8")
        (output_dir / "changes.patch").write_text("patch\n", encoding="utf-8")
        run = DevelopmentRun(
            status="verified", repository_name="exchange", base_head_sha="a" * 40,
            summary=supplied.summary, changed_paths=["web/src/status.ts"],
            commands=[DevelopmentCommandResult(
                command_id="web_tests", argv=["npm", "test"], exit_code=0,
                duration_seconds=0.1, output_tail="passed",
            )],
            patch_path="development/changes.patch", safety_boundary=["isolated clone only"],
        )
        (output_dir / "development_run.json").write_text(
            run.model_dump_json(indent=2), encoding="utf-8"
        )
        return run

    monkeypatch.setattr(
        "onebrief.execution_pipeline.ExchangeDevelopmentToolPack.apply_and_verify", fake_apply
    )
    output_dir = tmp_path / "acceptance-revision-output"
    result = ExecutionPipeline(run_dir, gateway=gateway).run(
        intake=intake, requirements=_requirements(), sources=[source], output_dir=output_dir
    )

    assert result.status == PipelineStatus.COMPLETE
    assert attempts == [initial, corrected]
    assert (output_dir / "code_change_set_revision_r1.json").is_file()
    assert "corrected" in (
        output_dir / "development" / "changed_files" / "web" / "src" / "status.ts"
    ).read_text(encoding="utf-8")
    verifier_payloads = [
        json.loads(payload["contents"])
        for (stage, _), payload in zip(gateway.calls, gateway.payloads)
        if stage.startswith("independent_verification")
    ]
    assert verifier_payloads[0]["implementation_evidence"]["changed_files"][0]["content"]
    assert [stage for stage, _ in gateway.calls] == [
        "evidence_analysis",
        "long_form_draft",
        "independent_verification_r0",
        "long_form_draft_verification_retry",
        "independent_verification_r1",
    ]

def test_development_retry_overlays_prior_changes_and_removes_new_duplicate(tmp_path: Path) -> None:
    pipeline = ExecutionPipeline(tmp_path)
    previous = ProjectCodeChangeSet(
        summary="Initial multilingual implementation.",
        changes=[
            {
                "path": "Assets/Game/Localization.cs",
                "base_sha256": "a" * 64,
                "content": "class Localization {}\n",
                "reason": "Update localization.",
            },
            {
                "path": "Assets/Game/Duplicate.cs",
                "base_sha256": None,
                "content": "class Duplicate {}\n",
                "reason": "Add a helper.",
            },
        ],
    )
    delta = ProjectCodeChangeSet(
        summary="Remove the duplicate helper.",
        changes=[{
            "path": "Assets/Game/Duplicate.cs",
            "base_sha256": None,
            "content": "",
            "reason": "Remove the duplicate file.",
        }],
    )

    merged = pipeline._merge_development_retry(previous, delta)

    assert [item.path for item in merged.changes] == ["Assets/Game/Localization.cs"]
    assert merged.changes[0].content == "class Localization {}\n"


def test_development_retry_preserves_prior_scope_when_updating_one_file(tmp_path: Path) -> None:
    pipeline = ExecutionPipeline(tmp_path)
    previous = ProjectCodeChangeSet(
        summary="Initial change.",
        changes=[
            {"path": "Assets/A.cs", "base_sha256": "a" * 64, "content": "A1", "reason": "Update A."},
            {"path": "Assets/B.cs", "base_sha256": "b" * 64, "content": "B1", "reason": "Update B."},
        ],
    )
    delta = ProjectCodeChangeSet(
        summary="Correct A.",
        changes=[{
            "path": "Assets/A.cs", "base_sha256": "a" * 64,
            "content": "A2", "reason": "Fix A."
        }],
    )

    merged = pipeline._merge_development_retry(previous, delta)

    assert [(item.path, item.content) for item in merged.changes] == [
        ("Assets/A.cs", "A2"), ("Assets/B.cs", "B1")
    ]


def test_development_change_comparison_ignores_only_narrative_metadata(tmp_path: Path) -> None:
    pipeline = ExecutionPipeline(tmp_path)
    previous = ProjectCodeChangeSet(
        summary="First explanation.",
        changes=[{
            "path": "Assets/A.cs",
            "base_sha256": "a" * 64,
            "content": "same executable content",
            "reason": "First reason.",
        }],
    )
    repeated = ProjectCodeChangeSet(
        summary="Different explanation.",
        changes=[{
            "path": "Assets/A.cs",
            "base_sha256": "a" * 64,
            "content": "same executable content",
            "reason": "Different reason.",
        }],
    )
    changed = repeated.model_copy(update={
        "changes": [repeated.changes[0].model_copy(update={"content": "new content"})]
    })

    assert pipeline._same_development_changes(previous, repeated) is True
    assert pipeline._same_development_changes(previous, changed) is False


def test_unity_playmode_failure_report_removes_shutdown_noise(tmp_path: Path) -> None:
    feedback = (
        "development verification failed: unity_playmode_visual_tests (exit_code=2) "
        + "package shutdown noise " * 500
        + "UNITY TEST FAILURES OneBrief.Visual.Flow: LanguageDropdown Expected: not null But was: null"
    )

    report = ExecutionPipeline._development_failure_report(feedback)

    assert report.verdict == Verdict.REVISE
    assert "LanguageDropdown" in report.blocking_issues[0]
    assert "package shutdown noise" not in report.blocking_issues[0]
    assert len(report.blocking_issues[0]) < 4_200
    assert "real requested screen transition" in report.revision_instructions[0]


def test_unity_compile_failure_report_keeps_only_compiler_signals() -> None:
    feedback = (
        "development verification failed: unity_compile (exit_code=1) "
        + "warning CS0414 noisy warning " * 200
        + "VERIFICATION SIGNALS Assets/Test.cs(109,1): error CS1529: using clause misplaced "
        + "BEE COMPILER DIAGNOSTICS enormous backend json"
    )

    report = ExecutionPipeline._development_failure_report(feedback)

    assert "error CS1529" in report.blocking_issues[0]
    assert "noisy warning" not in report.blocking_issues[0]
    assert "backend json" not in report.blocking_issues[0]


def test_two_identical_development_repairs_stop_without_more_verification_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = CodeChangeSet(summary="same", changes=[{
        "path": "web/src/status.ts", "base_sha256": None,
        "content": "export const status = 'same';\n", "reason": "same",
    }])

    class RepeatingGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.outputs = [candidate.model_dump(mode="json") for _ in range(3)]

        def generate_adk_response(self, **_kwargs: object) -> types.GenerateContentResponse:
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=json.dumps(payload))]),
                finish_reason=types.FinishReason.STOP,
            )])

    apply_count = 0

    def fail_once(*_args: object, **_kwargs: object) -> DevelopmentRun:
        nonlocal apply_count
        apply_count += 1
        raise RuntimeError("development verification failed: node_tests assertion failed")

    monkeypatch.setattr(ExecutionPipeline, "_apply_development_change_set", fail_once)
    intake = IntakeRequest(
        goal="Repair the executable.", output_target=OutputTarget.EXISTING_PROJECT,
        toolpack_ids=[ToolPackId.EXCHANGE_DEVELOPMENT],
    )
    output_dir = tmp_path / "identical-output"
    output_dir.mkdir()

    with pytest.raises(RuntimeError, match="stalled after two identical candidates"):
        ExecutionPipeline(tmp_path / "run", gateway=RepeatingGateway())._run_adk_development_convergence(
            intake=intake, requirements=_requirements(), sources=[_source()],
            source_payload=[{
                "name": "exchange-source/web/src/status.ts", "priority": "mandatory",
                "requirement_keys": ["repair"], "content": "export const status = 'old';",
                "sha256": "b" * 64,
            }],
            contract={"goal": intake.goal, "acceptance_criteria": ["Executable passes tests."]},
            analysis=_analysis(), output_dir=output_dir,
        )

    assert apply_count == 1


def test_retry_summary_can_remove_named_new_duplicate_without_dropping_scope(tmp_path: Path) -> None:
    pipeline = ExecutionPipeline(tmp_path)
    previous = ProjectCodeChangeSet(
        summary="Initial change.",
        changes=[
            {"path": "Assets/Localization.cs", "base_sha256": "a" * 64, "content": "L1", "reason": "Update localization."},
            {"path": "Assets/JulpaeRuntimeSession.cs", "base_sha256": None, "content": "duplicate", "reason": "Add helper."},
        ],
    )
    delta = ProjectCodeChangeSet(
        summary="Remove duplicate JulpaeRuntimeSession and correct localization.",
        changes=[{
            "path": "Assets/Localization.cs", "base_sha256": "a" * 64,
            "content": "L2", "reason": "Correct localization after deleting duplicate."
        }],
    )

    merged = pipeline._merge_development_retry(previous, delta)

    assert [(item.path, item.content) for item in merged.changes] == [
        ("Assets/Localization.cs", "L2")
    ]
