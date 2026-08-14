"""Declarative Unity journey evidence compiled by OneBrief-owned code.

The model selects observable scenes, controls, and actions.  It never authors
the PlayMode C# harness or the evidence manifest API calls.  This keeps proof
construction inside a small typed language while the trusted runner owns code
generation, assertions, screenshots, and atomic publication.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


UnityJourneyAction = Literal[
    "load_scene",
    "wait_frames",
    "assert_active",
    "select_dropdown_index",
    "set_toggle_on",
    "set_input_text",
    "click_button",
    "wait_for_scene",
    "capture",
]


class UnityEvidenceJourneyStep(BaseModel):
    """One bounded UI operation in an executable evidence journey."""

    action: UnityJourneyAction
    target: str | None = Field(
        default=None,
        max_length=300,
        description=(
            "Exact active GameObject name or hierarchy path. Required for "
            "assert_active, select_dropdown_index, set_toggle_on, set_input_text, and "
            "click_button. Use set_toggle_on for a Unity Toggle; never treat a Toggle as a Button."
        ),
    )
    scene_name: str | None = Field(
        default=None,
        max_length=160,
        description="Exact committed Unity scene name for load_scene or wait_for_scene.",
    )
    value_index: int | None = Field(
        default=None,
        ge=0,
        le=128,
        description="Existing dropdown option index selected through the shipped control.",
    )
    text_value: str | None = Field(
        default=None,
        max_length=300,
        description="Deterministic non-secret text entered through the shipped input control.",
    )
    frames: int = Field(default=2, ge=1, le=120)
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    scenario_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$",
        description="Stable evidence row ID, required only for capture.",
    )
    viewport_width: int = Field(default=1280, ge=320, le=3840)
    viewport_height: int = Field(default=720, ge=240, le=2160)

    @model_validator(mode="before")
    @classmethod
    def canonicalize_named_toggle_action(cls, value: object) -> object:
        """Compile an obvious Unity Toggle selector to the typed toggle operation.

        Provider plans have repeatedly described ``AgreeToggle`` correctly but selected the
        generic click action.  A Unity Toggle is not a Button, so accepting that representation
        only defers a deterministic type error to an expensive PlayMode run.
        """

        if not isinstance(value, dict) or value.get("action") != "click_button":
            return value
        target = str(value.get("target") or "").casefold()
        if "toggle" not in target:
            return value
        normalized = dict(value)
        normalized["action"] = "set_toggle_on"
        return normalized

    @field_validator("target", "scene_name", "text_value", "scenario_id", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split()).strip()
        return text or None

    @field_validator("frames", mode="before")
    @classmethod
    def restore_frames_default(cls, value: object) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 2
        return parsed if parsed > 0 else 2

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def restore_timeout_default(cls, value: object) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 10.0
        return parsed if parsed > 0 else 10.0

    @field_validator("viewport_width", mode="before")
    @classmethod
    def restore_width_default(cls, value: object) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 1280
        return parsed if parsed > 0 else 1280

    @field_validator("viewport_height", mode="before")
    @classmethod
    def restore_height_default(cls, value: object) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 720
        return parsed if parsed > 0 else 720

    @model_validator(mode="after")
    def fields_match_action(self) -> "UnityEvidenceJourneyStep":
        target_actions = {
            "assert_active", "select_dropdown_index", "set_toggle_on", "set_input_text",
            "click_button",
        }
        if self.action in target_actions and not self.target:
            raise ValueError(f"{self.action} requires target")
        if self.action in {"load_scene", "wait_for_scene"} and not self.scene_name:
            raise ValueError(f"{self.action} requires scene_name")
        if self.action == "select_dropdown_index" and self.value_index is None:
            raise ValueError("select_dropdown_index requires value_index")
        if self.action == "set_input_text" and self.text_value is None:
            raise ValueError("set_input_text requires text_value")
        if self.action == "capture" and not self.scenario_id:
            raise ValueError("capture requires scenario_id")
        return self


class UnityEvidenceJourneyPlan(BaseModel):
    """Provider-visible proof plan; OneBrief compiles it into trusted C#."""

    schema_version: str = "onebrief-unity-evidence-journey-plan-v1"
    summary: str = Field(min_length=3, max_length=500)
    test_directory: str = Field(
        description=(
            "Approved Assets/.../Tests/PlayMode directory. OneBrief chooses fixed generated "
            "filenames below this directory and does not trust provider-authored C#."
        )
    )
    steps: list[UnityEvidenceJourneyStep] = Field(
        min_length=3,
        max_length=24,
        description=(
            "Ordered real UI journey. Load only the initial scene; later screens must be reached "
            "through shipped controls. Establish any approved test/authentication precondition "
            "before clicking into a protected destination, wait for that destination, then capture. "
            "Use set_toggle_on for terms/consent Toggle controls instead of click_button."
        ),
    )

    @field_validator("test_directory")
    @classmethod
    def validate_test_directory(cls, value: str) -> str:
        normalized = str(PurePosixPath(value.replace("\\", "/"))).strip("/")
        lowered = f"/{normalized.casefold()}/"
        if not normalized.startswith("Assets/") or "/tests/playmode/" not in lowered:
            raise ValueError("test_directory must be below Assets/.../Tests/PlayMode")
        return normalized

    @model_validator(mode="after")
    def validate_journey(self) -> "UnityEvidenceJourneyPlan":
        if self.steps[0].action != "load_scene":
            raise ValueError("the journey must begin with load_scene")
        if any(step.action == "load_scene" for step in self.steps[1:]):
            raise ValueError(
                "only the initial scene may be loaded directly; later destinations require UI actions"
            )
        captures = [step for step in self.steps if step.action == "capture"]
        if not captures:
            raise ValueError("the journey must capture at least one executed state")
        scenario_ids = [step.scenario_id for step in captures]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("capture scenario_id values must be unique")
        if not any(step.action in {"assert_active", "wait_for_scene"} for step in self.steps):
            raise ValueError("the journey must contain an executable state assertion")
        for index, step in enumerate(self.steps):
            if step.action != "wait_for_scene" or index == 1:
                continue
            if not any(
                prior.action in {
                    "click_button", "select_dropdown_index", "set_toggle_on", "set_input_text",
                }
                for prior in self.steps[:index]
            ):
                raise ValueError(
                    "a later destination must follow a real shipped-control interaction"
                )
        return self


class RenderedUnityEvidenceJourney(BaseModel):
    playmode_test_path: str
    playmode_test_source: str
    test_assembly_path: str
    test_assembly_source: str


def _cs(value: str) -> str:
    """JSON string syntax is a valid deterministic C# string literal here."""

    return json.dumps(value, ensure_ascii=False)


AUTHENTICATION_CONTRACT_MARKER = "ONEBRIEF_AUTHENTICATION_PRECONDITION_V1"


def _target_contains(step: UnityEvidenceJourneyStep, markers: tuple[str, ...]) -> bool:
    target = (step.target or "").casefold().replace("_", "").replace("-", "")
    return any(marker in target for marker in markers)


def journey_establishes_authentication_precondition(
    plan: UnityEvidenceJourneyPlan,
) -> bool:
    """Recognize only executable authentication paths expressible by the trusted DSL.

    The marker produced from this decision is consumed by deterministic preflight.  It is
    deliberately based on typed actions rather than provider-authored prose or C# tokens.
    Runtime verification still has to prove that every referenced control exists and that
    the shipped flow reaches the asserted destination.
    """

    account_selector = any(
        step.action == "select_dropdown_index"
        and _target_contains(step, ("testaccount", "account", "profile", "auth", "session", "user"))
        for step in plan.steps
    )
    authenticated_submit = any(
        step.action == "click_button"
        and _target_contains(step, ("directenter", "signin", "login", "authenticate"))
        for step in plan.steps
    )
    if account_selector and authenticated_submit:
        return True

    terms_acceptance = any(
        step.action == "set_toggle_on"
        and _target_contains(step, ("terms", "agree", "consent", "privacy"))
        for step in plan.steps
    )
    identity_input = any(
        step.action == "set_input_text"
        and _target_contains(step, ("nickname", "username", "email", "password", "account"))
        for step in plan.steps
    )
    return terms_acceptance and identity_input


def _step_source(step: UnityEvidenceJourneyStep) -> list[str]:
    if step.action == "load_scene":
        return [
            f"SceneManager.LoadScene({_cs(step.scene_name or '')});",
            f"yield return WaitForScene({_cs(step.scene_name or '')}, {step.timeout_seconds:g}f);",
            "assertions++; trace.Add(\"load_scene:\" + SceneManager.GetActiveScene().name);",
        ]
    if step.action == "wait_frames":
        return [f"yield return WaitFrames({step.frames});"]
    if step.action == "assert_active":
        return [
            f"RequireActive({_cs(step.target or '')});",
            f"assertions++; trace.Add({_cs('assert_active:' + (step.target or ''))});",
        ]
    if step.action == "select_dropdown_index":
        return [
            f"SelectDropdown(RequireActive({_cs(step.target or '')}), {step.value_index});",
            f"assertions++; trace.Add({_cs('select_dropdown:' + (step.target or '') + ':' + str(step.value_index))});",
            f"yield return WaitFrames({step.frames});",
        ]
    if step.action == "set_toggle_on":
        return [
            f"SetToggleOn(RequireActive({_cs(step.target or '')}));",
            f"assertions++; trace.Add({_cs('set_toggle_on:' + (step.target or ''))});",
            f"yield return WaitFrames({step.frames});",
        ]
    if step.action == "set_input_text":
        return [
            f"SetInputText(RequireActive({_cs(step.target or '')}), {_cs(step.text_value or '')});",
            f"assertions++; trace.Add({_cs('set_input:' + (step.target or ''))});",
            f"yield return WaitFrames({step.frames});",
        ]
    if step.action == "click_button":
        return [
            f"ClickButton(RequireActive({_cs(step.target or '')}));",
            f"assertions++; trace.Add({_cs('click:' + (step.target or ''))});",
        ]
    if step.action == "wait_for_scene":
        return [
            f"yield return WaitForScene({_cs(step.scene_name or '')}, {step.timeout_seconds:g}f);",
            f"assertions++; trace.Add({_cs('wait_for_scene:' + (step.scene_name or ''))});",
        ]
    if step.action == "capture":
        scenario = step.scenario_id or "scenario"
        return [
            "receipts.Add(OneBriefAtomicScreenshot.CaptureScenario(",
            f"    {_cs(scenario)}, SceneManager.GetActiveScene().name,",
            "    trace.Count == 0 ? \"observe\" : string.Join(\" -> \", trace),",
            f"    Math.Max(1, assertions), {_cs('Screenshots/' + scenario + '.png')},",
            f"    RequireCamera(), {step.viewport_width}, {step.viewport_height}, ActiveCanvases()));",
            "trace.Clear(); assertions = 0;",
        ]
    raise ValueError(f"unsupported Unity journey action: {step.action}")


def render_unity_evidence_journey(
    plan: UnityEvidenceJourneyPlan,
    *,
    existing_test_path: str | None = None,
    existing_assembly_path: str | None = None,
) -> RenderedUnityEvidenceJourney:
    """Compile a bounded plan into the only C# evidence harness the runner accepts."""

    directory = PurePosixPath(plan.test_directory)
    test_path = existing_test_path or str(directory / "OneBriefGeneratedJourneyTest.cs")
    assembly_path = existing_assembly_path or str(
        directory / "OneBrief.Generated.Visual.Tests.asmdef"
    )
    body: list[str] = []
    for step in plan.steps:
        body.extend("            " + line for line in _step_source(step))
    authentication_contract = (
        [
            "        private const string OneBriefAuthenticationContract =",
            f"            \"{AUTHENTICATION_CONTRACT_MARKER}\";",
            "",
        ]
        if journey_establishes_authentication_precondition(plan)
        else []
    )
    source = "\n".join([
        "// ONEBRIEF_DECLARATIVE_EVIDENCE_V1 - generated by trusted runner; do not hand edit.",
        "using System;", "using System.Collections;", "using System.Collections.Generic;",
        "using System.Linq;", "using System.Reflection;", "using NUnit.Framework;",
        "using UnityEngine;", "using UnityEngine.SceneManagement;", "using UnityEngine.TestTools;",
        "using UnityEngine.UI;", "", "namespace OneBrief.Visual", "{",
        "    public sealed class OneBriefGeneratedJourneyTest", "    {",
        *authentication_contract,
        "        [UnityTest]", "        public IEnumerator ExecuteApprovedJourney()", "        {",
        "            var receipts = new List<OneBriefAtomicScreenshot.ScenarioReceipt>();",
        "            var trace = new List<string>();", "            int assertions = 0;",
        *body,
        "            OneBriefAtomicScreenshot.WriteManifestAtomically(receipts.ToArray());",
        "        }", "",
        "        private static IEnumerator WaitFrames(int count)", "        {",
        "            for (int i = 0; i < count; i++) yield return null;", "        }", "",
        "        private static IEnumerator WaitForScene(string expected, float seconds)", "        {",
        "            float until = Time.realtimeSinceStartup + seconds;",
        "            while (SceneManager.GetActiveScene().name != expected && Time.realtimeSinceStartup < until)",
        "                yield return null;",
        "            Assert.AreEqual(expected, SceneManager.GetActiveScene().name, \"Expected destination scene was not reached.\");",
        "            yield return WaitFrames(2);", "        }", "",
        "        private static GameObject RequireActive(string selector)", "        {",
        "            GameObject found = GameObject.Find(selector);",
        "            string leaf = selector.Split('/').Last();",
        "            if (found == null) found = Resources.FindObjectsOfTypeAll<GameObject>()",
        "                .FirstOrDefault(item => item != null && item.scene.IsValid()",
        "                    && item.activeInHierarchy && item.name == leaf);",
        "            Assert.IsNotNull(found, \"Active shipped UI object is unavailable: \" + selector);",
        "            Assert.IsTrue(found.activeInHierarchy, \"UI object is inactive: \" + selector);",
        "            return found;", "        }", "",
        "        private static Camera RequireCamera()", "        {",
        "            Camera camera = Camera.main ?? Resources.FindObjectsOfTypeAll<Camera>()",
        "                .FirstOrDefault(item => item != null && item.gameObject.scene.IsValid() && item.isActiveAndEnabled);",
        "            Assert.IsNotNull(camera, \"Active scene camera is unavailable.\");", "            return camera;",
        "        }", "",
        "        private static Canvas[] ActiveCanvases()", "        {",
        "            return Resources.FindObjectsOfTypeAll<Canvas>()",
        "                .Where(item => item != null && item.gameObject.scene.IsValid() && item.isActiveAndEnabled",
        "                    && item.gameObject.activeInHierarchy).ToArray();", "        }", "",
        "        private static void ClickButton(GameObject target)", "        {",
        "            Button button = target.GetComponent<Button>();",
        "            Assert.IsNotNull(button, \"Target is not a Button: \" + target.name);",
        "            Assert.IsTrue(button.interactable, \"Button is not interactable: \" + target.name);",
        "            button.onClick.Invoke();", "        }", "",
        "        private static Component ValueComponent(GameObject target, string property)", "        {",
        "            Component component = target.GetComponents<Component>().FirstOrDefault(item =>",
        "                item != null && item.GetType().GetProperty(property, BindingFlags.Instance | BindingFlags.Public) != null);",
        "            Assert.IsNotNull(component, \"Control property is unavailable: \" + property);",
        "            return component;", "        }", "",
        "        private static void SelectDropdown(GameObject target, int index)", "        {",
        "            Component control = ValueComponent(target, \"value\"); Type type = control.GetType();",
        "            PropertyInfo options = type.GetProperty(\"options\");",
        "            var list = options == null ? null : options.GetValue(control) as System.Collections.IList;",
        "            Assert.IsNotNull(list, \"Dropdown options are unavailable.\");",
        "            Assert.Greater(list.Count, index, \"Dropdown option index is unavailable.\");",
        "            MethodInfo silent = type.GetMethod(\"SetValueWithoutNotify\", new[] { typeof(int) });",
        "            if (silent != null) silent.Invoke(control, new object[] { index });",
        "            else type.GetProperty(\"value\").SetValue(control, index);",
        "            object changed = type.GetProperty(\"onValueChanged\")?.GetValue(control);",
        "            changed?.GetType().GetMethod(\"Invoke\", new[] { typeof(int) })?.Invoke(changed, new object[] { index });",
        "        }", "",
        "        private static void SetToggleOn(GameObject target)", "        {",
        "            Toggle toggle = target.GetComponent<Toggle>();",
        "            Assert.IsNotNull(toggle, \"Target is not a Toggle: \" + target.name);",
        "            Assert.IsTrue(toggle.interactable, \"Toggle is not interactable: \" + target.name);",
        "            toggle.SetIsOnWithoutNotify(true);",
        "            toggle.onValueChanged.Invoke(true);",
        "        }", "",
        "        private static void SetInputText(GameObject target, string value)", "        {",
        "            Component control = ValueComponent(target, \"text\"); Type type = control.GetType();",
        "            MethodInfo silent = type.GetMethod(\"SetTextWithoutNotify\", new[] { typeof(string) });",
        "            if (silent != null) silent.Invoke(control, new object[] { value });",
        "            else type.GetProperty(\"text\").SetValue(control, value);",
        "            object changed = type.GetProperty(\"onValueChanged\")?.GetValue(control);",
        "            changed?.GetType().GetMethod(\"Invoke\", new[] { typeof(string) })?.Invoke(changed, new object[] { value });",
        "        }", "    }", "}", "",
    ])
    assembly = json.dumps({
        "name": "OneBrief.Generated.Visual.Tests",
        "references": ["UnityEngine.TestRunner", "UnityEditor.TestRunner"],
        "includePlatforms": [], "excludePlatforms": [], "allowUnsafeCode": False,
        "overrideReferences": True, "precompiledReferences": ["nunit.framework.dll"],
        "autoReferenced": False, "defineConstraints": ["UNITY_INCLUDE_TESTS"],
        "versionDefines": [], "noEngineReferences": False,
        "optionalUnityReferences": ["TestAssemblies"],
    }, ensure_ascii=False, indent=2) + "\n"
    return RenderedUnityEvidenceJourney(
        playmode_test_path=test_path,
        playmode_test_source=source,
        test_assembly_path=assembly_path,
        test_assembly_source=assembly,
    )
