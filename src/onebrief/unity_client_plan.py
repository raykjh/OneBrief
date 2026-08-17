"""Trusted compilation of a bounded Unity client-shell construction plan.

The model chooses product copy, scene identities, and the two existing public
authentication controls.  KHALINOS owns all C# generation.  The generated
surface never calls project-private methods or replaces preserved listeners;
it forwards interaction through the shipped controls that already own those
authority boundaries.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import PurePosixPath
from typing import Mapping, Sequence

from pydantic import BaseModel, Field, field_validator, model_validator

from onebrief.unity_evidence_plan import UnityEvidenceJourneyPlan


TRUSTED_UNITY_CLIENT_MARKER = "KHALINOS_DECLARATIVE_CLIENT_V1"
TRUSTED_UNITY_CLIENT_PLAN_MARKER = "KHALINOS_DECLARATIVE_CLIENT_PLAN_B64"


class VerifiedUnityClientBase(BaseModel):
    """Digest-bound generated client accepted by an earlier milestone."""

    runtime_source_path: str
    runtime_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("runtime_source_path")
    @classmethod
    def validate_runtime_source_path(cls, value: str) -> str:
        normalized = str(PurePosixPath(value.replace("\\", "/"))).strip("/")
        if not normalized.startswith("Assets/") or not normalized.endswith(
            "/KhalinosGeneratedClientShell.cs"
        ):
            raise ValueError("verified base must identify the generated client runtime")
        return normalized


class UnityClientConstructionPlan(BaseModel):
    """Provider-visible plan for a new or incrementally extended Unity client."""

    schema_version: str = "khalinos-unity-client-construction-plan-v2"
    summary: str = Field(min_length=3, max_length=500)
    product_directory: str = Field(
        description=(
            "New approved Assets/... client/presentation directory. KHALINOS chooses a fixed "
            "generated runtime filename below it."
        )
    )
    initial_scene: str = Field(min_length=1, max_length=160)
    destination_scene: str = Field(min_length=1, max_length=160)
    authentication_account_selector: str = Field(
        min_length=1,
        max_length=300,
        description="Exact active shipped account/profile dropdown name or hierarchy path.",
    )
    authentication_submit_selector: str = Field(
        min_length=1,
        max_length=300,
        description="Exact active shipped DirectEnter/SignIn/Login/Authenticate button name or path.",
    )
    brand_title: str = Field(min_length=1, max_length=80)
    login_title: str = Field(min_length=1, max_length=120)
    login_subtitle: str = Field(min_length=1, max_length=240)
    login_button_label: str = Field(min_length=1, max_length=80)
    lobby_title: str = Field(min_length=1, max_length=120)
    lobby_subtitle: str = Field(min_length=1, max_length=240)
    settings_button_label: str = Field(min_length=1, max_length=80)
    settings_title: str = Field(min_length=1, max_length=120)
    settings_body: str = Field(min_length=1, max_length=300)
    close_button_label: str = Field(min_length=1, max_length=80)
    verified_base: VerifiedUnityClientBase | None = Field(
        default=None,
        description=(
            "Exact path and SHA-256 of the previously verified generated client. "
            "Copy only from onebrief-verified-unity-client-base.json."
        ),
    )
    lobby_data_selector: str | None = Field(
        default=None,
        max_length=300,
        description="Exact committed Lobby object/path whose visible text represents preserved data.",
    )
    lobby_navigation_selector: str | None = Field(
        default=None,
        max_length=300,
        description="Exact committed Lobby button/path whose shipped listener owns navigation.",
    )
    lobby_navigation_destination_scene: str | None = Field(
        default=None,
        max_length=160,
        description="Exact committed scene reached by the selected Lobby navigation control.",
    )
    lobby_navigation_label: str | None = Field(default=None, max_length=80)
    accent_hex: str = Field(
        default="#35D7FF",
        pattern=r"^#[0-9A-Fa-f]{6}$",
        description="Restrained six-digit RGB accent color.",
    )

    @field_validator(
        "initial_scene",
        "destination_scene",
        "authentication_account_selector",
        "authentication_submit_selector",
        "brand_title",
        "login_title",
        "login_subtitle",
        "login_button_label",
        "lobby_title",
        "lobby_subtitle",
        "settings_button_label",
        "settings_title",
        "settings_body",
        "close_button_label",
        "lobby_data_selector",
        "lobby_navigation_selector",
        "lobby_navigation_destination_scene",
        "lobby_navigation_label",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> str:
        if value is None:
            return value  # type: ignore[return-value]
        return " ".join(str(value).split()).strip()

    @field_validator("product_directory")
    @classmethod
    def validate_product_directory(cls, value: str) -> str:
        normalized = str(PurePosixPath(value.replace("\\", "/"))).strip("/")
        lowered = f"/{normalized.casefold()}/"
        if not normalized.startswith("Assets/"):
            raise ValueError("product_directory must be below Assets")
        if any(marker in lowered for marker in ("/test/", "/tests/", "/evidence/")):
            raise ValueError("product_directory must be production-owned")
        if not any(marker in lowered for marker in ("client", "presentation", "/ui/")):
            raise ValueError("product_directory must identify a separate client/presentation area")
        return normalized

    @model_validator(mode="after")
    def validate_authority_route(self) -> "UnityClientConstructionPlan":
        if self.initial_scene == self.destination_scene:
            raise ValueError("initial_scene and destination_scene must differ")
        account = self.authentication_account_selector.casefold().replace("_", "").replace("-", "")
        submit = self.authentication_submit_selector.casefold().replace("_", "").replace("-", "")
        if not any(marker in account for marker in ("account", "profile", "auth", "user")):
            raise ValueError("authentication_account_selector must identify an account/profile control")
        if not any(marker in submit for marker in ("directenter", "signin", "login", "authenticate")):
            raise ValueError("authentication_submit_selector must identify an authentication submit control")
        lobby_bindings = (
            self.lobby_data_selector,
            self.lobby_navigation_selector,
            self.lobby_navigation_destination_scene,
            self.lobby_navigation_label,
        )
        if any(item is not None for item in lobby_bindings) and not all(
            isinstance(item, str) and item.strip() for item in lobby_bindings
        ):
            raise ValueError(
                "incremental client plans require complete Lobby data and navigation bindings"
            )
        if self.verified_base is not None and not all(
            isinstance(item, str) and item.strip() for item in lobby_bindings
        ):
            raise ValueError("verified_base requires complete Lobby data and navigation bindings")
        return self


class RenderedUnityClientConstruction(BaseModel):
    runtime_source_path: str
    runtime_source: str


def _cs(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _hex_rgb(value: str) -> tuple[float, float, float]:
    return tuple(int(value[index:index + 2], 16) / 255.0 for index in (1, 3, 5))  # type: ignore[return-value]


def _embedded_plan_payload(plan: UnityClientConstructionPlan) -> str:
    payload = plan.model_dump(mode="json", exclude={"verified_base"})
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return base64.b64encode(encoded).decode("ascii")


def _extract_embedded_plan(source: str) -> dict[str, object] | None:
    prefix = f"// {TRUSTED_UNITY_CLIENT_PLAN_MARKER}:"
    line = next(
        (item.strip() for item in source.splitlines() if item.strip().startswith(prefix)),
        None,
    )
    if line is None:
        return None
    try:
        decoded = base64.b64decode(line[len(prefix):].strip(), validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def verified_unity_client_base_from_sources(
    sources: Sequence[Mapping[str, object]],
) -> tuple[VerifiedUnityClientBase, dict[str, object]] | None:
    """Find one committed generated client and recover its trusted plan metadata."""

    matches: list[tuple[VerifiedUnityClientBase, dict[str, object]]] = []
    for source in sources:
        content = source.get("content")
        path = source.get("repository_path")
        if not isinstance(content, str) or not isinstance(path, str):
            continue
        if TRUSTED_UNITY_CLIENT_MARKER not in content:
            continue
        payload = _extract_embedded_plan(content)
        if payload is None:
            continue
        actual_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        declared_sha256 = source.get("sha256")
        if isinstance(declared_sha256, str) and declared_sha256 != actual_sha256:
            continue
        try:
            base = VerifiedUnityClientBase(
                runtime_source_path=path,
                runtime_source_sha256=actual_sha256,
            )
        except ValueError:
            continue
        matches.append((base, payload))
    if len(matches) != 1:
        return None
    return matches[0]


def verified_unity_client_planning_source(
    sources: Sequence[Mapping[str, object]],
) -> dict[str, object] | None:
    """Expose prior plan facts without exposing generated C# implementation text."""

    discovered = verified_unity_client_base_from_sources(sources)
    if discovered is None:
        return None
    base, prior_plan = discovered
    content = json.dumps(
        {
            "schema_version": "khalinos-verified-unity-client-base-v1",
            "verified_base": base.model_dump(mode="json"),
            "prior_plan": prior_plan,
            "rule": (
                "Preserve prior_plan fields and add exact Lobby data/navigation bindings "
                "from the committed scene catalog."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )
    return {
        "name": "onebrief-verified-unity-client-base.json",
        "priority": "must_use",
        "requirement_keys": ["verified_incremental_client"],
        "content": content,
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "repository_path": None,
        "source_role": "read_only_context",
    }


def bind_unity_client_plan_incremental_base(
    plan: UnityClientConstructionPlan,
    sources: Sequence[Mapping[str, object]],
) -> tuple[UnityClientConstructionPlan, list[str]]:
    """Bind an M02 plan to the sole verified M01 generated artifact."""

    discovered = verified_unity_client_base_from_sources(sources)
    if discovered is None:
        if plan.verified_base is not None:
            return plan, [
                "The declared verified_base is not present as one digest-valid generated client in the approved source snapshot."
            ]
        # No committed generated client means this is the initial construction
        # milestone. Weaker models may eagerly fill optional later-milestone
        # Lobby bindings because the outcome sketch mentions the full client.
        # Keep M01 bounded by dropping those future fields deterministically.
        return UnityClientConstructionPlan.model_validate({
            **plan.model_dump(mode="json"),
            "lobby_data_selector": None,
            "lobby_navigation_selector": None,
            "lobby_navigation_destination_scene": None,
            "lobby_navigation_label": None,
        }), []
    base, prior_payload = discovered
    preserved_fields = (
        "product_directory",
        "initial_scene",
        "destination_scene",
        "authentication_account_selector",
        "authentication_submit_selector",
        "brand_title",
        "login_title",
        "login_subtitle",
        "login_button_label",
        "lobby_title",
        "lobby_subtitle",
        "settings_button_label",
        "settings_title",
        "settings_body",
        "close_button_label",
        "accent_hex",
    )
    trusted_updates = {
        field_name: prior_payload[field_name]
        for field_name in preserved_fields
        if field_name in prior_payload
    }
    bound = UnityClientConstructionPlan.model_validate({
        **plan.model_dump(mode="json"),
        **trusted_updates,
        "verified_base": base.model_dump(mode="json"),
    })
    return bound, []


def validate_unity_client_plan_targets(
    plan: UnityClientConstructionPlan,
    scene_catalog: Mapping[str, object] | None,
    authentication_authority: tuple[str, str] | None = None,
) -> list[str]:
    """Bind scene and preserved-control selections to the committed catalog."""

    _bound, issues = bind_unity_client_plan_targets(
        plan, scene_catalog, authentication_authority
    )
    return issues


def bind_unity_client_plan_targets(
    plan: UnityClientConstructionPlan,
    scene_catalog: Mapping[str, object] | None,
    authentication_authority: tuple[str, str] | None = None,
) -> tuple[UnityClientConstructionPlan, list[str]]:
    """Canonicalize harmless scene-path wrappers and reject invented identities.

    Some weaker models return ``Assets/Login.unity`` even when the schema asks
    for the exact scene identity ``Login``.  The wrapper carries no additional
    authority.  Strip it only when its stem is already an exact committed
    catalog entry; an invented path remains a hard failure.
    """

    by_scene: dict[str, set[str]] = {}
    controls_by_scene: dict[str, set[str]] = {}
    raw_scenes = (
        scene_catalog.get("scenes", [])
        if scene_catalog and isinstance(scene_catalog.get("scenes"), list)
        else []
    )
    for raw in raw_scenes:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("scene_name"), str):
            continue
        names: set[str] = set()
        for key in ("object_names", "control_object_names"):
            values = raw.get(key)
            if isinstance(values, list):
                names.update(str(item) for item in values if isinstance(item, str))
        scene_name = str(raw["scene_name"])
        by_scene[scene_name] = names
        raw_controls = raw.get("control_object_names")
        controls_by_scene[scene_name] = (
            {str(item) for item in raw_controls if isinstance(item, str)}
            if isinstance(raw_controls, list)
            else set()
        )
    updates: dict[str, str] = {}
    for field_name in ("initial_scene", "destination_scene"):
        value = str(getattr(plan, field_name))
        if value in by_scene:
            continue
        stem = PurePosixPath(value.replace("\\", "/")).stem
        if stem in by_scene:
            updates[field_name] = stem
    if updates:
        plan = plan.model_copy(update=updates)
    issues: list[str] = []
    if authentication_authority is not None:
        authority_updates: dict[str, str] = {}
        for field_name, expected in zip(
            ("authentication_account_selector", "authentication_submit_selector"),
            authentication_authority,
            strict=True,
        ):
            supplied = str(getattr(plan, field_name)).replace("\\", "/")
            supplied_leaf = supplied.split("/")[-1]
            expected_leaf = expected.replace("\\", "/").split("/")[-1]
            if supplied_leaf == expected_leaf:
                authority_updates[field_name] = expected
            else:
                issues.append(
                    f"{field_name} {supplied!r} does not match the approved authentication "
                    f"authority {expected!r}."
                )
        if authority_updates:
            plan = plan.model_copy(update=authority_updates)
    if by_scene and plan.initial_scene not in by_scene:
        issues.append(
            f"Initial scene {plan.initial_scene!r} is not committed; exact scenes: "
            + ", ".join(sorted(by_scene))
        )
    if by_scene and plan.destination_scene not in by_scene:
        issues.append(
            f"Destination scene {plan.destination_scene!r} is not committed; exact scenes: "
            + ", ".join(sorted(by_scene))
        )
    initial_names = by_scene.get(plan.initial_scene, set())
    for label, selector in (
        ("account selector", plan.authentication_account_selector),
        ("submit selector", plan.authentication_submit_selector),
    ):
        leaf = selector.replace("\\", "/").split("/")[-1]
        if initial_names and leaf not in initial_names:
            relevant = sorted(
                name for name in initial_names
                if any(token in name.casefold() for token in ("account", "profile", "login", "enter", "dropdown", "button"))
            )[:16]
            issues.append(
                f"Committed scene {plan.initial_scene!r} has no {label} named {leaf!r}; "
                f"relevant exact candidates: {', '.join(relevant) or 'none'}."
            )
    if plan.verified_base is not None:
        destination_names = by_scene.get(plan.destination_scene, set())
        for label, selector in (
            ("Lobby data selector", plan.lobby_data_selector),
            ("Lobby navigation selector", plan.lobby_navigation_selector),
        ):
            leaf = str(selector or "").replace("\\", "/").split("/")[-1]
            if destination_names and leaf not in destination_names:
                issues.append(
                    f"Committed scene {plan.destination_scene!r} has no {label} named {leaf!r}."
                )
        navigation_leaf = str(plan.lobby_navigation_selector or "").replace(
            "\\", "/"
        ).split("/")[-1]
        destination_controls = controls_by_scene.get(plan.destination_scene, set())
        if destination_controls and navigation_leaf not in destination_controls:
            issues.append(
                f"Lobby navigation selector {navigation_leaf!r} is not a committed control in scene {plan.destination_scene!r}."
            )
        navigation_destination = str(plan.lobby_navigation_destination_scene or "")
        if by_scene and navigation_destination not in by_scene:
            issues.append(
                f"Lobby navigation destination {navigation_destination!r} is not committed; exact scenes: "
                + ", ".join(sorted(by_scene))
            )
    return plan, issues


def render_unity_client_construction(
    plan: UnityClientConstructionPlan,
) -> RenderedUnityClientConstruction:
    """Compile the bounded plan into a fixed production runtime component."""

    red, green, blue = _hex_rgb(plan.accent_hex)
    path = str(PurePosixPath(plan.product_directory) / "KhalinosGeneratedClientShell.cs")
    values = {
        "initial": _cs(plan.initial_scene),
        "destination": _cs(plan.destination_scene),
        "account": _cs(plan.authentication_account_selector),
        "submit": _cs(plan.authentication_submit_selector),
        "brand": _cs(plan.brand_title),
        "login_title": _cs(plan.login_title),
        "login_subtitle": _cs(plan.login_subtitle),
        "login_button": _cs(plan.login_button_label),
        "lobby_title": _cs(plan.lobby_title),
        "lobby_subtitle": _cs(plan.lobby_subtitle),
        "settings_button": _cs(plan.settings_button_label),
        "settings_title": _cs(plan.settings_title),
        "settings_body": _cs(plan.settings_body),
        "close_button": _cs(plan.close_button_label),
        "lobby_data": _cs(plan.lobby_data_selector or ""),
        "lobby_navigation": _cs(plan.lobby_navigation_selector or ""),
        "lobby_navigation_destination": _cs(
            plan.lobby_navigation_destination_scene or ""
        ),
        "lobby_navigation_label": _cs(plan.lobby_navigation_label or ""),
    }
    plan_payload = _embedded_plan_payload(plan)
    source = f'''// {TRUSTED_UNITY_CLIENT_MARKER} - generated by KHALINOS; do not hand edit.
// {TRUSTED_UNITY_CLIENT_PLAN_MARKER}: {plan_payload}
using System;
using System.Linq;
using TMPro;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

namespace Khalinos.GeneratedClient
{{
    public sealed class KhalinosGeneratedClientShell : MonoBehaviour
    {{
        private const string InitialScene = {values["initial"]};
        private const string DestinationScene = {values["destination"]};
        private const string AccountSelector = {values["account"]};
        private const string SubmitSelector = {values["submit"]};
        private const string LobbyDataSelector = {values["lobby_data"]};
        private const string LobbyNavigationSelector = {values["lobby_navigation"]};
        private const string LobbyNavigationDestination = {values["lobby_navigation_destination"]};
        private const string LobbyNavigationLabel = {values["lobby_navigation_label"]};
        private static readonly Color Accent = new Color({red:.6f}f, {green:.6f}f, {blue:.6f}f, 1f);
        private GameObject surfaceRoot;
        private TMP_Dropdown preservedTmpDropdown;
        private Dropdown preservedDropdown;
        private Button preservedSubmit;
        private TMP_Dropdown proxyAccountDropdown;
        private Button proxySubmitButton;
        private TMP_Text proxyLobbyData;
        private Button proxyLobbyNavigation;

        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]
        private static void Install()
        {{
            if (FindFirstObjectByType<KhalinosGeneratedClientShell>() != null) return;
            GameObject host = new GameObject("__KhalinosGeneratedClientHost");
            DontDestroyOnLoad(host);
            host.AddComponent<KhalinosGeneratedClientShell>();
        }}

        private void OnEnable()
        {{
            SceneManager.sceneLoaded += OnSceneLoaded;
            BuildFor(SceneManager.GetActiveScene());
        }}

        private void OnDisable()
        {{
            SceneManager.sceneLoaded -= OnSceneLoaded;
        }}

        private void OnSceneLoaded(Scene scene, LoadSceneMode mode)
        {{
            BuildFor(scene);
        }}

        private void Update()
        {{
            if (proxyAccountDropdown != null)
            {{
                BindPreservedAuthenticationControls();
                if (!AccountOptionsAreCurrent())
                    CopyOptions(proxyAccountDropdown);
            }}
            if (proxyLobbyData != null || proxyLobbyNavigation != null)
                RefreshLobbyBindings();
        }}

        private void BuildFor(Scene scene)
        {{
            if (surfaceRoot != null) Destroy(surfaceRoot);
            surfaceRoot = null;
            if (scene.name == InitialScene) BuildLogin();
            else if (scene.name == DestinationScene) BuildLobby();
        }}

        private void BuildLogin()
        {{
            BindPreservedAuthenticationControls();

            Canvas canvas = CreateCanvas();
            GameObject panel = CreatePanel("OneBriefLoginPanel", canvas.transform, new Color(0.035f, 0.047f, 0.075f, 0.96f));
            Place(panel.GetComponent<RectTransform>(), 0.5f, 0.5f, 720f, 540f);
            CreateText("OneBriefBrandTitle", panel.transform, {values["brand"]}, 26f, Accent, 0f, 190f, 620f, 52f);
            CreateText("OneBriefLoginTitle", panel.transform, {values["login_title"]}, 46f, Color.white, 0f, 105f, 620f, 76f);
            CreateText("OneBriefLoginSubtitle", panel.transform, {values["login_subtitle"]}, 22f, new Color(0.78f, 0.84f, 0.92f), 0f, 35f, 620f, 70f);
            proxyAccountDropdown = CreateDropdown("NewClientTestAccountDropdown", panel.transform, 0f, -65f, 500f, 64f);
            CopyOptions(proxyAccountDropdown);
            proxyAccountDropdown.onValueChanged.AddListener(SyncAccountSelection);
            proxySubmitButton = CreateButton("NewClientDirectEnterButton", panel.transform, {values["login_button"]}, 0f, -165f, 500f, 72f, () =>
            {{
                SyncAccountSelection(proxyAccountDropdown.value);
                if (preservedSubmit == null) throw new InvalidOperationException("Approved authentication submit control is unavailable: " + SubmitSelector);
                preservedSubmit.onClick.Invoke();
            }});
            RefreshProxySubmitAvailability();
        }}

        private void BindPreservedAuthenticationControls()
        {{
            GameObject account = FindActive(AccountSelector);
            preservedTmpDropdown = account?.GetComponent<TMP_Dropdown>();
            preservedDropdown = preservedTmpDropdown == null ? account?.GetComponent<Dropdown>() : null;
            preservedSubmit = FindActive(SubmitSelector)?.GetComponent<Button>();
            RefreshProxySubmitAvailability();
        }}

        private void RefreshProxySubmitAvailability()
        {{
            if (proxySubmitButton != null)
                proxySubmitButton.interactable = preservedSubmit != null
                    && (preservedTmpDropdown != null || preservedDropdown != null);
        }}

        private void BuildLobby()
        {{
            Canvas canvas = CreateCanvas();
            GameObject panel = CreatePanel("NewClientLobbyPanel", canvas.transform, new Color(0.025f, 0.035f, 0.06f, 0.97f));
            Stretch(panel.GetComponent<RectTransform>(), 42f);
            CreateText("OneBriefLobbyBrand", panel.transform, {values["brand"]}, 24f, Accent, 0f, 230f, 780f, 50f);
            CreateText("OneBriefLobbyTitle", panel.transform, {values["lobby_title"]}, 50f, Color.white, 0f, 125f, 860f, 80f);
            CreateText("OneBriefLobbySubtitle", panel.transform, {values["lobby_subtitle"]}, 22f, new Color(0.78f, 0.84f, 0.92f), 0f, 50f, 760f, 70f);
            if (!string.IsNullOrEmpty(LobbyDataSelector))
                proxyLobbyData = CreateText("NewClientLobbyData", panel.transform, "Waiting for approved data", 21f, new Color(0.78f, 0.84f, 0.92f), 0f, -40f, 760f, 64f);
            if (!string.IsNullOrEmpty(LobbyNavigationSelector))
                proxyLobbyNavigation = CreateButton("NewClientLobbyNavigationButton", panel.transform, LobbyNavigationLabel, -205f, -145f, 360f, 68f, InvokePreservedLobbyNavigation);
            CreateButton("NewClientSettingsButton", panel.transform, {values["settings_button"]}, string.IsNullOrEmpty(LobbyNavigationSelector) ? 0f : 205f, -145f, 360f, 68f, () => BuildSettings(panel.transform));
            RefreshLobbyBindings();
        }}

        private void RefreshLobbyBindings()
        {{
            if (proxyLobbyData != null)
            {{
                GameObject source = FindActive(LobbyDataSelector);
                TMP_Text tmp = source?.GetComponent<TMP_Text>();
                Text legacy = tmp == null ? source?.GetComponent<Text>() : null;
                string value = tmp != null ? tmp.text : legacy != null ? legacy.text : "";
                if (!string.IsNullOrWhiteSpace(value)) proxyLobbyData.text = value;
            }}
            if (proxyLobbyNavigation != null)
                proxyLobbyNavigation.interactable = FindActive(LobbyNavigationSelector)?.GetComponent<Button>() != null;
        }}

        private void InvokePreservedLobbyNavigation()
        {{
            Button preserved = FindActive(LobbyNavigationSelector)?.GetComponent<Button>();
            if (preserved == null)
                throw new InvalidOperationException("Approved Lobby navigation control is unavailable: " + LobbyNavigationSelector);
            preserved.onClick.Invoke();
        }}

        private void BuildSettings(Transform parent)
        {{
            Transform old = parent.Find("NewClientSettingsPanel");
            if (old != null) Destroy(old.gameObject);
            GameObject panel = CreatePanel("NewClientSettingsPanel", parent, new Color(0.055f, 0.07f, 0.11f, 0.99f));
            Place(panel.GetComponent<RectTransform>(), 0.5f, 0.5f, 680f, 470f);
            CreateText("OneBriefSettingsTitle", panel.transform, {values["settings_title"]}, 42f, Color.white, 0f, 105f, 580f, 70f);
            CreateText("OneBriefSettingsBody", panel.transform, {values["settings_body"]}, 21f, new Color(0.78f, 0.84f, 0.92f), 0f, 5f, 560f, 130f);
            CreateButton("NewClientSettingsCloseButton", panel.transform, {values["close_button"]}, 0f, -140f, 300f, 64f, () => Destroy(panel));
        }}

        private Canvas CreateCanvas()
        {{
            surfaceRoot = new GameObject("OneBriefNewClientRoot", typeof(RectTransform));
            Canvas canvas = surfaceRoot.AddComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            canvas.sortingOrder = 32000;
            CanvasScaler scaler = surfaceRoot.AddComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = new Vector2(1280f, 720f);
            scaler.matchWidthOrHeight = 0.5f;
            surfaceRoot.AddComponent<GraphicRaycaster>();
            return canvas;
        }}

        private static GameObject CreatePanel(string name, Transform parent, Color color)
        {{
            GameObject panel = UiObject(name, parent);
            panel.AddComponent<Image>().color = color;
            return panel;
        }}

        private static TMP_Text CreateText(string name, Transform parent, string value, float size, Color color, float x, float y, float width, float height)
        {{
            GameObject item = UiObject(name, parent);
            TMP_Text text = item.AddComponent<TextMeshProUGUI>();
            text.text = value;
            text.fontSize = size;
            text.color = color;
            text.alignment = TextAlignmentOptions.Center;
            text.enableWordWrapping = true;
            Place(item.GetComponent<RectTransform>(), 0.5f, 0.5f, width, height, x, y);
            return text;
        }}

        private static Button CreateButton(string name, Transform parent, string label, float x, float y, float width, float height, Action action)
        {{
            GameObject item = UiObject(name, parent);
            Image image = item.AddComponent<Image>();
            image.color = Accent;
            Button button = item.AddComponent<Button>();
            button.targetGraphic = image;
            Place(item.GetComponent<RectTransform>(), 0.5f, 0.5f, width, height, x, y);
            CreateText(name + "Label", item.transform, label, 22f, new Color(0.02f, 0.04f, 0.07f), 0f, 0f, width - 24f, height - 12f);
            button.onClick.AddListener(() => action());
            return button;
        }}

        private static TMP_Dropdown CreateDropdown(string name, Transform parent, float x, float y, float width, float height)
        {{
            GameObject item = UiObject(name, parent);
            Image image = item.AddComponent<Image>();
            image.color = new Color(0.10f, 0.13f, 0.19f, 1f);
            TMP_Dropdown dropdown = item.AddComponent<TMP_Dropdown>();
            dropdown.targetGraphic = image;
            TMP_Text caption = CreateText(name + "Caption", item.transform, "Account", 21f, Color.white, 0f, 0f, width - 40f, height - 12f);
            dropdown.captionText = caption;
            Place(item.GetComponent<RectTransform>(), 0.5f, 0.5f, width, height, x, y);
            return dropdown;
        }}

        private void CopyOptions(TMP_Dropdown proxy)
        {{
            proxy.options.Clear();
            if (preservedTmpDropdown != null)
                proxy.options.AddRange(preservedTmpDropdown.options.Select(item => new TMP_Dropdown.OptionData(item.text)));
            else if (preservedDropdown != null)
                proxy.options.AddRange(preservedDropdown.options.Select(item => new TMP_Dropdown.OptionData(item.text)));
            if (proxy.options.Count == 0) proxy.options.Add(new TMP_Dropdown.OptionData("Approved profile"));
            proxy.SetValueWithoutNotify(preservedTmpDropdown != null ? preservedTmpDropdown.value : preservedDropdown != null ? preservedDropdown.value : 0);
            proxy.RefreshShownValue();
        }}

        private bool AccountOptionsAreCurrent()
        {{
            if (proxyAccountDropdown == null) return true;
            if (preservedTmpDropdown != null)
            {{
                if (proxyAccountDropdown.options.Count != preservedTmpDropdown.options.Count) return false;
                for (int index = 0; index < preservedTmpDropdown.options.Count; index++)
                    if (proxyAccountDropdown.options[index].text != preservedTmpDropdown.options[index].text) return false;
                return true;
            }}
            if (preservedDropdown != null)
            {{
                if (proxyAccountDropdown.options.Count != preservedDropdown.options.Count) return false;
                for (int index = 0; index < preservedDropdown.options.Count; index++)
                    if (proxyAccountDropdown.options[index].text != preservedDropdown.options[index].text) return false;
                return true;
            }}
            return proxyAccountDropdown.options.Count == 0;
        }}

        private void SyncAccountSelection(int index)
        {{
            if (preservedTmpDropdown != null && index < preservedTmpDropdown.options.Count)
            {{
                preservedTmpDropdown.SetValueWithoutNotify(index);
                preservedTmpDropdown.onValueChanged.Invoke(index);
            }}
            else if (preservedDropdown != null && index < preservedDropdown.options.Count)
            {{
                preservedDropdown.SetValueWithoutNotify(index);
                preservedDropdown.onValueChanged.Invoke(index);
            }}
        }}

        private static GameObject FindActive(string selector)
        {{
            GameObject found = GameObject.Find(selector);
            if (found != null && found.activeInHierarchy) return found;
            string leaf = selector.Replace('\\\\', '/').Split('/').Last();
            return Resources.FindObjectsOfTypeAll<GameObject>().FirstOrDefault(item =>
                item != null && item.gameObject.scene.IsValid() && item.activeInHierarchy && item.name == leaf);
        }}

        private static GameObject UiObject(string name, Transform parent)
        {{
            GameObject item = new GameObject(name, typeof(RectTransform));
            item.transform.SetParent(parent, false);
            return item;
        }}

        private static void Place(RectTransform rect, float anchorX, float anchorY, float width, float height, float x = 0f, float y = 0f)
        {{
            rect.anchorMin = rect.anchorMax = new Vector2(anchorX, anchorY);
            rect.pivot = new Vector2(0.5f, 0.5f);
            rect.sizeDelta = new Vector2(width, height);
            rect.anchoredPosition = new Vector2(x, y);
        }}

        private static void Stretch(RectTransform rect, float margin)
        {{
            rect.anchorMin = Vector2.zero;
            rect.anchorMax = Vector2.one;
            rect.offsetMin = new Vector2(margin, margin);
            rect.offsetMax = new Vector2(-margin, -margin);
        }}
    }}
}}
'''
    return RenderedUnityClientConstruction(
        runtime_source_path=path,
        runtime_source=source,
    )


def derive_unity_client_evidence_journey(
    plan: UnityClientConstructionPlan,
) -> UnityEvidenceJourneyPlan:
    """Derive the executable proof route from the trusted fixed client topology."""

    product_parts = PurePosixPath(plan.product_directory).parts
    product_root = PurePosixPath(*product_parts[:2])
    test_directory = str(product_root / "Tests" / "PlayMode")
    steps: list[dict[str, object]] = [
        {"action": "load_scene", "scene_name": plan.initial_scene},
        {"action": "wait_frames", "frames": 10},
        {"action": "assert_active", "target": "OneBriefLoginPanel"},
        {"action": "capture", "scenario_id": "login_surface_initial"},
        {
            "action": "select_dropdown_index",
            "target": "NewClientTestAccountDropdown",
            "value_index": 1,
        },
        {"action": "click_button", "target": "NewClientDirectEnterButton"},
        {
            "action": "wait_for_scene",
            "scene_name": plan.destination_scene,
            "timeout_seconds": 20.0,
        },
        {"action": "assert_active", "target": "NewClientLobbyPanel"},
    ]
    if plan.verified_base is not None:
        steps.extend([
            {"action": "assert_active", "target": "NewClientLobbyData"},
            {"action": "assert_active", "target": "NewClientLobbyNavigationButton"},
            {"action": "capture", "scenario_id": "lobby_data_navigation_active"},
            {"action": "click_button", "target": "NewClientLobbyNavigationButton"},
            {
                "action": "wait_for_scene",
                "scene_name": plan.lobby_navigation_destination_scene,
                "timeout_seconds": 20.0,
            },
            {"action": "capture", "scenario_id": "lobby_navigation_destination"},
        ])
    else:
        steps.extend([
            {"action": "capture", "scenario_id": "lobby_surface_initial"},
            {"action": "click_button", "target": "NewClientSettingsButton"},
            {"action": "wait_frames", "frames": 5},
            {"action": "assert_active", "target": "NewClientSettingsPanel"},
            {"action": "capture", "scenario_id": "settings_panel_active"},
        ])
    return UnityEvidenceJourneyPlan.model_validate({
        "summary": "Verify the generated Login, Lobby, and Settings client journey.",
        "test_directory": test_directory,
        "steps": steps,
    })


def is_trusted_unity_client_change_set(value: object | None) -> bool:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = value
    else:
        return False
    changes = payload.get("changes")
    return isinstance(changes, list) and any(
        isinstance(change, Mapping)
        and TRUSTED_UNITY_CLIENT_MARKER in str(change.get("content") or change.get("replace") or "")
        for change in changes
    )
