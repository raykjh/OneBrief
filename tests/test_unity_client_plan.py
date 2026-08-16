from onebrief.execution_pipeline import (
    bound_unity_client_maker_schema,
    compact_unity_client_planning_sources,
    development_maker_schema_for,
    normalize_unity_client_construction_plan,
)
from onebrief.execution_schemas import VerificationReport, Verdict
from onebrief.generic_development_toolpack import ProjectCodeChangeSet
from onebrief.schemas import ExecutionPhase
from onebrief.unity_client_plan import (
    TRUSTED_UNITY_CLIENT_MARKER,
    UnityClientConstructionPlan,
    bind_unity_client_plan_targets,
    derive_unity_client_evidence_journey,
    render_unity_client_construction,
    validate_unity_client_plan_targets,
)
from onebrief.unity_evidence_plan import candidate_unity_object_names


def _plan(**updates: object) -> UnityClientConstructionPlan:
    payload = {
        "summary": "Build a bounded new client shell.",
        "product_directory": "Assets/JULPAE/NewClient/Runtime",
        "initial_scene": "Login",
        "destination_scene": "Lobby",
        "authentication_account_selector": "DevPanel/TestAccountDropdown",
        "authentication_submit_selector": "DevPanel/DirectEnterButton",
        "brand_title": "JULPAE",
        "login_title": "Enter the Table",
        "login_subtitle": "Choose an approved profile.",
        "login_button_label": "Enter",
        "lobby_title": "Lobby",
        "lobby_subtitle": "Your next match is ready.",
        "settings_button_label": "Settings",
        "settings_title": "Settings",
        "settings_body": "Audio and language controls remain connected to the shipped systems.",
        "close_button_label": "Close",
        "accent_hex": "#35D7FF",
    }
    payload.update(updates)
    return UnityClientConstructionPlan.model_validate(payload)


def _catalog() -> dict[str, object]:
    return {
        "schema_version": "onebrief-unity-scene-catalog-v1",
        "source_revision": "abc123",
        "scenes": [
            {
                "scene_name": "Login",
                "object_names": ["DevPanel", "TestAccountDropdown", "DirectEnterButton"],
                "control_object_names": ["TestAccountDropdown", "DirectEnterButton"],
            },
            {"scene_name": "Lobby", "object_names": ["MainCanvas"]},
        ],
    }


def test_trusted_client_renderer_preserves_authentication_authority() -> None:
    rendered = render_unity_client_construction(_plan())

    assert rendered.runtime_source_path == (
        "Assets/JULPAE/NewClient/Runtime/KhalinosGeneratedClientShell.cs"
    )
    assert TRUSTED_UNITY_CLIENT_MARKER in rendered.runtime_source
    assert 'new GameObject("OneBriefNewClientRoot"' in rendered.runtime_source
    assert 'new GameObject("NewClientLobbyPanel"' not in rendered.runtime_source
    assert 'CreatePanel("NewClientLobbyPanel"' in rendered.runtime_source
    assert "preservedSubmit.onClick.Invoke();" in rendered.runtime_source
    assert "RemoveAllListeners" not in rendered.runtime_source
    assert "DirectEnterWithSelectedDevAccount" not in rendered.runtime_source
    assert "SceneManager.LoadScene" not in rendered.runtime_source
    assert "new TMP_Dropdown.OptionData(item.text, item.image)" not in rendered.runtime_source
    assert rendered.runtime_source.count("new TMP_Dropdown.OptionData(item.text)") == 2
    assert "private void Update()" in rendered.runtime_source
    assert "BindPreservedAuthenticationControls();" in rendered.runtime_source
    assert "!AccountOptionsAreCurrent()" in rendered.runtime_source
    assert "CopyOptions(proxyAccountDropdown);" in rendered.runtime_source
    assert "RefreshProxySubmitAvailability();" in rendered.runtime_source


def test_client_plan_derives_complete_proxy_evidence_journey() -> None:
    plan = _plan(
        product_directory="Assets/JULPAE/Scripts/Presentation",
        initial_scene="LoginScene_All",
        destination_scene="LobbyScene_All",
    )

    journey = derive_unity_client_evidence_journey(plan)

    assert journey.test_directory == "Assets/JULPAE/Tests/PlayMode"
    assert [(step.action, step.target, step.scene_name) for step in journey.steps] == [
        ("load_scene", None, "LoginScene_All"),
        ("wait_frames", None, None),
        ("assert_active", "OneBriefLoginPanel", None),
        ("capture", None, None),
        ("select_dropdown_index", "NewClientTestAccountDropdown", None),
        ("click_button", "NewClientDirectEnterButton", None),
        ("wait_for_scene", None, "LobbyScene_All"),
        ("assert_active", "NewClientLobbyPanel", None),
        ("capture", None, None),
        ("click_button", "NewClientSettingsButton", None),
        ("wait_frames", None, None),
        ("assert_active", "NewClientSettingsPanel", None),
        ("capture", None, None),
    ]


def test_client_plan_preflight_binds_exact_committed_controls() -> None:
    assert validate_unity_client_plan_targets(_plan(), _catalog()) == []

    issues = validate_unity_client_plan_targets(
        _plan(authentication_submit_selector="DevPanel/LoginButton"),
        _catalog(),
    )
    assert len(issues) == 1
    assert "LoginButton" in issues[0]
    assert "DirectEnterButton" in issues[0]


def test_client_plan_canonicalizes_only_paths_whose_stems_are_committed() -> None:
    bound, issues = bind_unity_client_plan_targets(
        _plan(
            initial_scene="Assets/Login.unity",
            destination_scene="Assets/Scenes/Lobby.unity",
        ),
        _catalog(),
    )

    assert issues == []
    assert bound.initial_scene == "Login"
    assert bound.destination_scene == "Lobby"

    unbound, invalid = bind_unity_client_plan_targets(
        _plan(initial_scene="Assets/NewLogin.unity"),
        _catalog(),
    )
    assert unbound.initial_scene == "Assets/NewLogin.unity"
    assert any("NewLogin" in issue for issue in invalid)


def test_client_plan_normalizes_to_one_trusted_new_product_file() -> None:
    normalized = normalize_unity_client_construction_plan(
        _plan().model_dump(mode="json")
    )

    assert isinstance(normalized, ProjectCodeChangeSet)
    assert len(normalized.changes) == 1
    change = normalized.changes[0]
    assert change.base_sha256 is None
    assert str(change.path).endswith("KhalinosGeneratedClientShell.cs")
    assert TRUSTED_UNITY_CLIENT_MARKER in (change.content or "")
    assert {
        "OneBriefLoginPanel",
        "NewClientTestAccountDropdown",
        "NewClientDirectEnterButton",
        "NewClientLobbyPanel",
        "NewClientSettingsButton",
        "NewClientSettingsPanel",
    }.issubset(candidate_unity_object_names(normalized))


def test_trusted_client_product_failure_replans_in_dsl_not_freeform_csharp() -> None:
    candidate = normalize_unity_client_construction_plan(_plan())
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[{
            "criterion": "The generated Unity client compiles",
            "passed": False,
            "evidence": "unity_compile: generated client failed",
        }],
        blocking_issues=["unity_compile: generated client failed"],
        revision_instructions=["Repair the product implementation."],
        missing_information=[],
    )

    selected = development_maker_schema_for(
        report,
        candidate,
        active_phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
    )

    assert selected is UnityClientConstructionPlan


def test_client_plan_preflight_failure_stays_in_dsl_without_a_candidate() -> None:
    report = VerificationReport(
        verdict=Verdict.REVISE,
        criterion_checks=[{
            "criterion": "The declarative client plan uses committed scenes",
            "passed": False,
            "evidence": "Unity client plan preflight rejected an invented scene path.",
        }],
        blocking_issues=[
            "Unity client plan preflight rejected unbound scene or authentication controls."
        ],
        revision_instructions=[
            "Reissue only the UnityClientConstructionPlan with exact scene names."
        ],
        missing_information=[],
    )

    selected = development_maker_schema_for(
        report,
        None,
        active_phase=ExecutionPhase.PRODUCT_IMPLEMENTATION,
    )

    assert selected is UnityClientConstructionPlan


def test_typed_client_schema_binding_survives_product_repairs_only() -> None:
    assert bound_unity_client_maker_schema(
        ExecutionPhase.PRODUCT_IMPLEMENTATION,
        UnityClientConstructionPlan.__name__,
    ) is UnityClientConstructionPlan
    assert bound_unity_client_maker_schema(
        ExecutionPhase.EVIDENCE_CONSTRUCTION,
        UnityClientConstructionPlan.__name__,
    ) is None


def test_client_planner_context_excludes_preserved_implementation_source() -> None:
    sources = [
        {"name": "repository/Assets/Scripts/LoginController.cs", "content": "large"},
        {"name": "repository/toolpack/unity-scene-catalog.json", "content": "scenes"},
        {"name": "onebrief-approved-runtime-authority.json", "content": "auth"},
        {"name": "requirements.md", "content": "duplicated contract"},
    ]

    compact = compact_unity_client_planning_sources(sources)

    assert [item["name"] for item in compact] == [
        "repository/toolpack/unity-scene-catalog.json",
        "onebrief-approved-runtime-authority.json",
    ]
