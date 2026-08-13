"""Trusted validation for Unity PlayMode visual evidence produced in isolation."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, field_validator


class UnityVisualScenario(BaseModel):
    scenario_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,79}$")
    # Locale measurements remain available for localization work, but a
    # general Unity UI task must not be forced to pretend that every screen is
    # a language-switch scenario.
    expected_locale: str | None = Field(default=None, min_length=2, max_length=20)
    observed_locale: str | None = Field(default=None, min_length=2, max_length=20)
    changed_visible_text_count: int | None = Field(default=None, ge=0)
    missing_glyph_count: int | None = Field(default=None, ge=0)
    observed_state: str | None = Field(default=None, min_length=2, max_length=120)
    interaction: str | None = Field(default=None, min_length=2, max_length=240)
    assertion_count: int | None = Field(default=None, ge=0)
    viewport_width: int | None = Field(default=None, ge=16, le=16384)
    viewport_height: int | None = Field(default=None, ge=16, le=16384)
    screenshot_path: str

    @field_validator("screenshot_path")
    @classmethod
    def validate_screenshot_path(cls, value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix.casefold() != ".png"
        ):
            raise ValueError("screenshot_path must be a safe evidence-relative PNG path")
        # Generated tests commonly write the manifest from the project root
        # and therefore include the trusted evidence directory once.  The
        # validator already resolves relative to that directory, so strip
        # exactly this known prefix rather than rejecting a valid capture or
        # allowing arbitrary path traversal.
        if len(path.parts) > 1 and path.parts[0].casefold() == "onebrief-evidence":
            path = PurePosixPath(*path.parts[1:])
        return path.as_posix()


class UnityVisualEvidence(BaseModel):
    schema_version: str = "onebrief-unity-visual-evidence-v1"
    scenarios: list[UnityVisualScenario] = Field(min_length=1, max_length=20)


class UnityVisualEvidenceSummary(BaseModel):
    test_count: int = Field(gt=0)
    scenario_count: int = Field(gt=0)
    observed_locales: list[str]
    observed_states: list[str] = Field(default_factory=list)
    screenshot_paths: list[str]


_LOCALE_ALIASES = {
    "chinese": "zh-hans",
    "simplified chinese": "zh-hans",
    "중국어": "zh-hans",
    "简体中文": "zh-hans",
    "japanese": "ja",
    "일본어": "ja",
    "日本語": "ja",
    "spanish": "es",
    "스페인어": "es",
    "español": "es",
    "korean": "ko",
    "한국어": "ko",
    "english": "en",
    "영어": "en",
}


def normalize_locale(value: str) -> str:
    normalized = value.strip().casefold().replace("_", "-")
    return _LOCALE_ALIASES.get(normalized, normalized)


def requested_locales(text: str) -> set[str]:
    lowered = text.casefold()
    found = {
        locale
        for label, locale in _LOCALE_ALIASES.items()
        if label.casefold() in lowered
    }
    for token in re.findall(r"(?<![a-z])(?:zh-hans|zh-cn|ja|es|ko|en)(?![a-z])", lowered):
        found.add("zh-hans" if token == "zh-cn" else token)
    return found


def requested_ui_surfaces(text: str) -> set[str]:
    lowered = text.casefold()
    aliases = {
        "login": ("login", "sign in", "로그인"),
        "lobby": ("lobby", "main menu", "로비"),
        "settings": ("settings", "setting", "options", "설정"),
    }
    return {
        surface
        for surface, markers in aliases.items()
        if any(marker in lowered for marker in markers)
    }


def _normalized_ui_surface(value: str) -> str | None:
    lowered = value.casefold()
    aliases = {
        "login": ("login", "sign in", "로그인"),
        "lobby": ("lobby", "main menu", "로비"),
        "settings": ("settings", "setting", "options", "설정"),
    }
    for surface, markers in aliases.items():
        if any(marker in lowered for marker in markers):
            return surface
    return None


def requested_ui_transition(text: str) -> list[str]:
    """Return an explicitly arrow-delimited UI journey, including repeats.

    Surface sets prove coverage, but they cannot distinguish login→lobby→settings
    from login→lobby→settings→lobby.  Preserve duplicate destinations so a
    requested return journey must produce a second, later observation.
    """

    marker_to_surface = {
        "login": "login",
        "sign in": "login",
        "로그인": "login",
        "lobby": "lobby",
        "main menu": "lobby",
        "로비": "lobby",
        "settings": "settings",
        "setting": "settings",
        "options": "settings",
        "설정": "settings",
    }
    surface = "(?:" + "|".join(
        sorted((re.escape(item) for item in marker_to_surface), key=len, reverse=True)
    ) + ")"
    chains = re.findall(
        rf"{surface}(?:\s*(?:→|->)\s*{surface})+",
        text,
        re.IGNORECASE,
    )
    if not chains:
        return []
    normalized_chains = [
        [
            marker_to_surface[item.casefold()]
            for item in re.findall(surface, chain, re.IGNORECASE)
        ]
        for chain in chains
    ]
    # A continuation package can repeat the same goal in several audit files.
    # Select one longest explicit chain rather than concatenating those copies.
    return max(normalized_chains, key=len)


def _contains_ordered_surface_journey(observed: list[str], requested: list[str]) -> bool:
    if not requested:
        return True
    cursor = 0
    for value in observed:
        surface = _normalized_ui_surface(value)
        if surface == requested[cursor]:
            cursor += 1
            if cursor == len(requested):
                return True
    return False


def _test_count(results_path: Path) -> int:
    if not results_path.is_file():
        raise RuntimeError("Unity visual verification did not produce an XML test result")
    try:
        root = ET.parse(results_path).getroot()
    except ET.ParseError as exc:
        raise RuntimeError("Unity visual verification produced invalid XML") from exc
    count = int(root.attrib.get("testcasecount", root.attrib.get("total", "0")) or 0)
    failed = int(root.attrib.get("failed", root.attrib.get("failures", "0")) or 0)
    names = [
        item.attrib.get("fullname", item.attrib.get("name", ""))
        for item in root.iter("test-case")
    ]
    if count < 1 or not any("OneBrief.Visual" in name for name in names):
        raise RuntimeError(
            "Unity visual verification requires at least one executed OneBrief.Visual PlayMode test; "
            "create a discoverable PlayMode test whose namespace/full name begins with OneBrief.Visual "
            "and a sibling test asmdef with optionalUnityReferences containing TestAssemblies"
        )
    if failed:
        raise RuntimeError(f"Unity visual verification reported {failed} failed test(s)")
    return count


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise RuntimeError(f"visual evidence is not a valid PNG: {path.name}")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width < 16 or height < 16:
        raise RuntimeError(f"visual evidence image is too small: {path.name}")
    return width, height


def validate_and_copy_unity_visual_evidence(
    clone: Path,
    results_path: Path,
    destination: Path,
    goal_text: str,
) -> UnityVisualEvidenceSummary:
    test_count = _test_count(results_path)
    evidence_root = (clone / "onebrief-evidence").resolve()
    manifest_path = evidence_root / "runtime-evidence.json"
    if not manifest_path.is_file():
        raise RuntimeError(
            "Unity visual verification requires onebrief-evidence/runtime-evidence.json to be "
            "generated during the executed OneBrief.Visual PlayMode test; edit the approved test "
            "source to create the schema and screenshots at runtime, and do not add a static "
            "onebrief-evidence file to the repository"
        )
    try:
        manifest = UnityVisualEvidence.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        detail = " ".join(str(exc).split())[:2_000]
        raise RuntimeError(
            "Unity visual evidence manifest is invalid: "
            + detail
            + ". Repair the approved executed PlayMode test so it writes "
            + "schema_version='onebrief-unity-visual-evidence-v1' and a non-empty "
            + "scenarios array. Every scenario must include scenario_id and "
            + "screenshot_path; for UI completion evidence also record observed_state, "
            + "interaction, assertion_count, viewport_width, and viewport_height. "
            + "Generate the manifest and PNG captures during the test run rather than "
            + "adding static evidence files to the repository."
        ) from exc

    observed: set[str] = set()
    observed_states: set[str] = set()
    observed_state_sequence: list[str] = []
    screenshot_sources: list[tuple[Path, str]] = []
    screenshot_digests: dict[str, UnityVisualScenario] = {}
    for scenario in manifest.scenarios:
        has_locale_measurement = any(value is not None for value in (
            scenario.expected_locale,
            scenario.observed_locale,
            scenario.changed_visible_text_count,
            scenario.missing_glyph_count,
        ))
        if has_locale_measurement:
            if not scenario.expected_locale or not scenario.observed_locale:
                raise RuntimeError(
                    f"Unity visual scenario {scenario.scenario_id} has an incomplete locale measurement"
                )
            expected = normalize_locale(scenario.expected_locale)
            actual = normalize_locale(scenario.observed_locale)
            if expected != actual:
                raise RuntimeError(
                    f"Unity visual scenario {scenario.scenario_id} observed {actual}, expected {expected}"
                )
            if (scenario.changed_visible_text_count or 0) < 1:
                raise RuntimeError(
                    f"Unity visual scenario {scenario.scenario_id} did not visibly change any text"
                )
            if scenario.missing_glyph_count:
                raise RuntimeError(
                    f"Unity visual scenario {scenario.scenario_id} found "
                    f"{scenario.missing_glyph_count} missing glyph(s)"
                )
            observed.add(actual)
        else:
            if not scenario.observed_state or not scenario.interaction:
                raise RuntimeError(
                    f"Unity visual scenario {scenario.scenario_id} must report the observed real UI state and interaction"
                )
            if (scenario.assertion_count or 0) < 1:
                raise RuntimeError(
                    f"Unity visual scenario {scenario.scenario_id} did not report a passing runtime assertion"
                )
            observed_states.add(scenario.observed_state.casefold())
            observed_state_sequence.append(scenario.observed_state)
        source = (evidence_root / Path(*PurePosixPath(scenario.screenshot_path).parts)).resolve()
        if not source.is_relative_to(evidence_root) or not source.is_file():
            raise RuntimeError(
                f"Unity visual scenario {scenario.scenario_id} screenshot is unavailable"
            )
        width, height = _png_dimensions(source)
        if scenario.viewport_width is not None and scenario.viewport_width != width:
            raise RuntimeError(
                f"Unity visual scenario {scenario.scenario_id} viewport width does not match its PNG"
            )
        if scenario.viewport_height is not None and scenario.viewport_height != height:
            raise RuntimeError(
                f"Unity visual scenario {scenario.scenario_id} viewport height does not match its PNG"
            )
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        previous_scenario = screenshot_digests.get(digest)
        if previous_scenario is not None:
            previous_surface = _normalized_ui_surface(previous_scenario.observed_state or "")
            current_surface = _normalized_ui_surface(scenario.observed_state or "")
            same_return_state = bool(
                previous_surface
                and previous_surface == current_surface
                and previous_scenario.interaction
                and scenario.interaction
                and (previous_scenario.assertion_count or 0) > 0
                and (scenario.assertion_count or 0) > 0
                and not any(value is not None for value in (
                    previous_scenario.expected_locale,
                    scenario.expected_locale,
                ))
            )
            if not same_return_state:
                raise RuntimeError(
                    "Unity visual evidence reused an identical screenshot for "
                    f"{previous_scenario.scenario_id} and {scenario.scenario_id}; the capture does not prove "
                    "the visible UI changed between scenarios"
                )
        else:
            screenshot_digests[digest] = scenario
        screenshot_sources.append((source, scenario.screenshot_path))

    required = requested_locales(goal_text)
    if required and not required.issubset(observed):
        missing = ", ".join(sorted(required - observed))
        raise RuntimeError(f"Unity visual evidence did not exercise requested locale(s): {missing}")

    requested_surfaces = requested_ui_surfaces(goal_text)
    if requested_surfaces:
        scenario_texts = [
            f"{item.scenario_id} {item.observed_state or ''} {item.interaction or ''} "
            f"{item.screenshot_path}".casefold()
            for item in manifest.scenarios
        ]
        # One final-state screenshot named LoginToLobbyToSettings cannot prove
        # three distinct rendered surfaces. Bind each requested surface to a
        # different scenario/screenshot so navigation claims remain observable.
        candidates = {
            surface: [
                index for index, text in enumerate(scenario_texts) if surface in text
            ]
            for surface in requested_surfaces
        }
        assigned: set[int] = set()
        missing_surfaces: list[str] = []
        for surface in sorted(requested_surfaces, key=lambda item: len(candidates[item])):
            available = [index for index in candidates[surface] if index not in assigned]
            if not available:
                missing_surfaces.append(surface)
            else:
                assigned.add(available[0])
        missing_surfaces.sort()
        if missing_surfaces:
            raise RuntimeError(
                "Unity visual evidence requires a distinct rendered scenario for every requested real UI surface: "
                + ", ".join(missing_surfaces)
            )

    requested_transition = requested_ui_transition(goal_text)
    if requested_transition and not _contains_ordered_surface_journey(
        observed_state_sequence, requested_transition
    ):
        raise RuntimeError(
            "Unity visual evidence did not prove the requested ordered UI journey: "
            + " -> ".join(requested_transition)
        )

    responsive_requested = (
        ("mobile" in goal_text.casefold() or "모바일" in goal_text)
        and ("desktop" in goal_text.casefold() or "데스크톱" in goal_text)
    )
    if responsive_requested:
        measured = [
            (item.viewport_width, item.viewport_height)
            for item in manifest.scenarios
            if item.viewport_width is not None and item.viewport_height is not None
        ]
        if not any(width < height for width, height in measured) or not any(
            width >= height for width, height in measured
        ):
            raise RuntimeError(
                "Unity visual evidence must include measured mobile/portrait and desktop/landscape captures"
            )

    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(results_path, destination / "test-results.xml")
    shutil.copy2(manifest_path, destination / "runtime-evidence.json")
    copied: list[str] = []
    for source, relative in screenshot_sources:
        target = destination / "screenshots" / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append((Path("screenshots") / Path(*PurePosixPath(relative).parts)).as_posix())
    summary = UnityVisualEvidenceSummary(
        test_count=test_count,
        scenario_count=len(manifest.scenarios),
        observed_locales=sorted(observed),
        observed_states=sorted(observed_states),
        screenshot_paths=copied,
    )
    (destination / "summary.json").write_text(
        summary.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return summary
