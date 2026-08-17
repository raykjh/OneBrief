"""Versioned, reusable capability components for project-bound ToolPacks.

A project pack owns identity, source revision, and read/write authority.  A
capability pack owns reusable deterministic expertise such as Unity control or
layout diagnosis.  Project approval binds both layers by digest; a reusable
component never grants access to a project by itself.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, Field


class CapabilityPackId(StrEnum):
    REPOSITORY_CONTROL = "repository-control"
    UNITY_CONTROL = "unity-control"
    UNITY_LAYOUT_DIAGNOSTICS = "unity-layout-diagnostics"
    GODOT_CONTROL = "godot-control"
    NODE_CONTROL = "node-control"
    WEB_OBSERVATION = "web-observation"
    PYTHON_CONTROL = "python-control"


class ExecutionPlacement(StrEnum):
    """Where deterministic adapters may execute.

    The placement is part of a capability pack's versioned semantics.  Host
    bound packs rely on an explicitly approved local runtime (for example the
    project's Unity Editor) and must not be moved to a generic Cloud worker.
    """

    PORTABLE = "portable"
    APPROVED_HOST = "approved_host"


class CapabilityPackDefinition(BaseModel):
    schema_version: str = "onebrief-capability-pack-v1"
    pack_id: CapabilityPackId
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    title: str = Field(min_length=3, max_length=120)
    ecosystems: list[str] = Field(min_length=1, max_length=8)
    adapter_ids: list[str] = Field(min_length=1, max_length=12)
    capabilities: list[str] = Field(min_length=1, max_length=16)
    evidence_contracts: list[str] = Field(min_length=1, max_length=16)
    blocked_boundaries: list[str] = Field(min_length=1, max_length=16)
    readonly_by_default: bool = True

    @property
    def sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class CapabilityPackRef(BaseModel):
    pack_id: CapabilityPackId
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    definition_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


_COMMON_BLOCKS = [
    "does not grant project identity or source access by itself",
    "does not grant credentials, deployment, publishing, Git push, or arbitrary commands",
]


BUILTIN_CAPABILITY_PACKS: dict[CapabilityPackId, CapabilityPackDefinition] = {
    CapabilityPackId.REPOSITORY_CONTROL: CapabilityPackDefinition(
        pack_id=CapabilityPackId.REPOSITORY_CONTROL,
        version="1.1.0",
        title="Isolated repository control",
        ecosystems=["git"],
        adapter_ids=["repository_snapshot"],
        capabilities=[
            "bind execution to one immutable Git revision",
            "create a disposable repository snapshot",
            "enforce exact source hashes and bounded paths",
            "transfer a bounded medium-project snapshot for remote deterministic verification",
        ],
        evidence_contracts=["repository identity, clean worktree, and source revision receipt"],
        blocked_boundaries=_COMMON_BLOCKS,
    ),
    CapabilityPackId.UNITY_CONTROL: CapabilityPackDefinition(
        pack_id=CapabilityPackId.UNITY_CONTROL,
        version="1.1.0",
        title="Unity compile and runtime control",
        ecosystems=["unity"],
        adapter_ids=[
            "unity_compile",
            "unity_editmode_tests",
            "unity_playmode_visual_tests",
        ],
        capabilities=[
            "bind the project-declared Unity Editor",
            "compile an isolated Unity project in batch mode",
            "run approved EditMode and PlayMode verification",
        ],
        evidence_contracts=[
            "Unity command receipts and test XML",
            "runtime screenshot manifest when visual verification is requested",
        ],
        blocked_boundaries=_COMMON_BLOCKS,
    ),
    CapabilityPackId.UNITY_LAYOUT_DIAGNOSTICS: CapabilityPackDefinition(
        pack_id=CapabilityPackId.UNITY_LAYOUT_DIAGNOSTICS,
        version="1.1.0",
        title="Unity Canvas and RectTransform diagnostics",
        ecosystems=["unity"],
        adapter_ids=["unity_layout_diagnostics"],
        capabilities=[
            "inspect committed scene Canvas and RectTransform hierarchy read-only",
            "record anchors, pivots, sizes, CanvasScaler settings, and structural risk flags",
            "bind diagnostic evidence to the exact candidate revision",
        ],
        evidence_contracts=[
            "bounded machine-readable hierarchy report",
            "risk summary tied to source revision and candidate digest",
        ],
        blocked_boundaries=[
            *_COMMON_BLOCKS,
            "does not edit scenes, prefabs, tests, screenshots, or product UI",
        ],
    ),
    CapabilityPackId.GODOT_CONTROL: CapabilityPackDefinition(
        pack_id=CapabilityPackId.GODOT_CONTROL,
        version="1.1.0",
        title="Godot trusted incremental construction and verification",
        ecosystems=["godot"],
        adapter_ids=["godot_headless_probe"],
        capabilities=[
            "bind one exact Godot executable by path and SHA-256",
            "materialize a trusted declarative topology inside an isolated snapshot",
            "run the committed headless topology probe with a bounded receipt path",
            "increment a verified predecessor with a trusted gameplay compiler",
            "capture an actual runtime frame and export a Windows artifact with fixed argv",
        ],
        evidence_contracts=[
            "plan, bundle, source revision, executable digest, and probe receipt lineage",
            "independent file-hash and visited-region verification",
            "quest-unique runtime PNG and exported binary evidence",
        ],
        blocked_boundaries=[
            *_COMMON_BLOCKS,
            "does not permit model-authored commands, engine arguments, or executable paths",
        ],
    ),
    CapabilityPackId.NODE_CONTROL: CapabilityPackDefinition(
        pack_id=CapabilityPackId.NODE_CONTROL,
        version="1.0.0",
        title="Node package-script control",
        ecosystems=["node"],
        adapter_ids=["node_script"],
        capabilities=["run only committed named lint, test, and build scripts"],
        evidence_contracts=["fixed command receipt and exit status"],
        blocked_boundaries=_COMMON_BLOCKS,
    ),
    CapabilityPackId.WEB_OBSERVATION: CapabilityPackDefinition(
        pack_id=CapabilityPackId.WEB_OBSERVATION,
        version="1.0.0",
        title="Local web runtime observation",
        ecosystems=["node", "web"],
        adapter_ids=["node_web_observation"],
        capabilities=["launch a verified local build and observe it with headless Chrome"],
        evidence_contracts=["HTTP receipt, screenshots, and browser observation record"],
        blocked_boundaries=_COMMON_BLOCKS,
    ),
    CapabilityPackId.PYTHON_CONTROL: CapabilityPackDefinition(
        pack_id=CapabilityPackId.PYTHON_CONTROL,
        version="1.0.0",
        title="Python test control",
        ecosystems=["python"],
        adapter_ids=["python_tests"],
        capabilities=["run the project test suite with the approved Python runtime"],
        evidence_contracts=["fixed pytest command receipt and exit status"],
        blocked_boundaries=_COMMON_BLOCKS,
    ),
}


_HOST_BOUND_PACKS = frozenset({
    CapabilityPackId.UNITY_CONTROL,
    CapabilityPackId.UNITY_LAYOUT_DIAGNOSTICS,
    CapabilityPackId.GODOT_CONTROL,
})


def capability_pack_execution_placement(pack_id: CapabilityPackId) -> ExecutionPlacement:
    """Return the versioned execution boundary for a reusable capability."""

    if pack_id in _HOST_BOUND_PACKS:
        return ExecutionPlacement.APPROVED_HOST
    return ExecutionPlacement.PORTABLE


def capability_pack_refs_require_approved_host(refs: list[CapabilityPackRef]) -> bool:
    return any(
        capability_pack_execution_placement(ref.pack_id) is ExecutionPlacement.APPROVED_HOST
        for ref in refs
    )


def capability_pack_refs_for_adapter_ids(adapter_ids: list[str]) -> list[CapabilityPackRef]:
    selected = set(adapter_ids)
    refs: list[CapabilityPackRef] = []
    for pack_id, definition in BUILTIN_CAPABILITY_PACKS.items():
        if selected.intersection(definition.adapter_ids):
            refs.append(CapabilityPackRef(
                pack_id=pack_id,
                version=definition.version,
                definition_sha256=definition.sha256,
            ))
    return refs


def validate_capability_pack_refs(
    refs: list[CapabilityPackRef], adapter_ids: list[str]
) -> list[str]:
    """Return deterministic integrity errors for one project composition."""

    errors: list[str] = []
    seen: set[CapabilityPackId] = set()
    covered: set[str] = set()
    for ref in refs:
        if ref.pack_id in seen:
            errors.append(f"duplicate capability pack: {ref.pack_id.value}")
            continue
        seen.add(ref.pack_id)
        definition = BUILTIN_CAPABILITY_PACKS.get(ref.pack_id)
        if definition is None:
            errors.append(f"unknown capability pack: {ref.pack_id.value}")
            continue
        if ref.version != definition.version or ref.definition_sha256 != definition.sha256:
            errors.append(f"stale capability pack binding: {ref.pack_id.value}")
            continue
        covered.update(definition.adapter_ids)
    missing = sorted(set(adapter_ids) - covered)
    if missing:
        errors.append("adapters without a capability pack: " + ", ".join(missing))
    return errors
