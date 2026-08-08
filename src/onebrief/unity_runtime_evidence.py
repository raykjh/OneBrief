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
    expected_locale: str = Field(min_length=2, max_length=20)
    observed_locale: str = Field(min_length=2, max_length=20)
    changed_visible_text_count: int = Field(ge=0)
    missing_glyph_count: int = Field(ge=0)
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
            "Unity visual verification requires at least one executed OneBrief.Visual PlayMode test"
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
            "Unity visual verification requires onebrief-evidence/runtime-evidence.json"
        )
    try:
        manifest = UnityVisualEvidence.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Unity visual evidence manifest is invalid") from exc

    observed: set[str] = set()
    screenshot_sources: list[tuple[Path, str]] = []
    screenshot_digests: dict[str, str] = {}
    for scenario in manifest.scenarios:
        expected = normalize_locale(scenario.expected_locale)
        actual = normalize_locale(scenario.observed_locale)
        if expected != actual:
            raise RuntimeError(
                f"Unity visual scenario {scenario.scenario_id} observed {actual}, expected {expected}"
            )
        if scenario.changed_visible_text_count < 1:
            raise RuntimeError(
                f"Unity visual scenario {scenario.scenario_id} did not visibly change any text"
            )
        if scenario.missing_glyph_count:
            raise RuntimeError(
                f"Unity visual scenario {scenario.scenario_id} found "
                f"{scenario.missing_glyph_count} missing glyph(s)"
            )
        source = (evidence_root / Path(*PurePosixPath(scenario.screenshot_path).parts)).resolve()
        if not source.is_relative_to(evidence_root) or not source.is_file():
            raise RuntimeError(
                f"Unity visual scenario {scenario.scenario_id} screenshot is unavailable"
            )
        _png_dimensions(source)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        previous_scenario = screenshot_digests.get(digest)
        if previous_scenario is not None:
            raise RuntimeError(
                "Unity visual evidence reused an identical screenshot for "
                f"{previous_scenario} and {scenario.scenario_id}; the capture does not prove "
                "the visible UI changed between locales"
            )
        screenshot_digests[digest] = scenario.scenario_id
        observed.add(actual)
        screenshot_sources.append((source, scenario.screenshot_path))

    required = requested_locales(goal_text)
    if required and not required.issubset(observed):
        missing = ", ".join(sorted(required - observed))
        raise RuntimeError(f"Unity visual evidence did not exercise requested locale(s): {missing}")

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
        screenshot_paths=copied,
    )
    (destination / "summary.json").write_text(
        summary.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return summary
