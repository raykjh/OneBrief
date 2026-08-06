"""Deterministic, authority-bounded executable ToolPacks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

from onebrief.schemas import InternalSource, SourcePriority, ToolPackId


class ToolCommandResult(BaseModel):
    command_id: str
    argv: list[str]
    exit_code: int
    duration_seconds: float = Field(ge=0)
    output_tail: str


class ToolEvidence(BaseModel):
    source_path: str
    packaged_path: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ToolPackRun(BaseModel):
    schema_version: str = "onebrief-toolpack-run-v1"
    toolpack_id: ToolPackId
    readonly: bool = True
    status: str
    commands: list[ToolCommandResult]
    evidence: list[ToolEvidence]
    safety_boundary: list[str]


CommandRunner = Callable[[list[str], Path, int], ToolCommandResult]


EXCHANGE_EVIDENCE = (
    "docs/PROJECT_CURRENT.md",
    "docs/ACTIVE_CHECKPOINT.md",
    "reports/2026-07-26_ecb_data_quality.json",
    "reports/2026-07-26_bok_ecb_cross_check.json",
    "web/public/data-status.json",
    "web/public/model-performance.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _command_runner(argv: list[str], cwd: Path, timeout_seconds: int) -> ToolCommandResult:
    started = time.monotonic()
    allowed_environment = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in {"path", "systemroot", "temp", "tmp", "comspec", "pathext"}
    }
    allowed_environment.update({"CI": "1", "NO_COLOR": "1"})
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=allowed_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        shell=False,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    result = ToolCommandResult(
        command_id="pending",
        argv=argv,
        exit_code=completed.returncode,
        duration_seconds=round(time.monotonic() - started, 3),
        output_tail=output[-20_000:],
    )
    if completed.returncode != 0:
        raise RuntimeError(f"ToolPack command failed ({completed.returncode}): {' '.join(argv)}")
    return result


def toolpack_descriptor(toolpack_id: ToolPackId) -> InternalSource:
    if toolpack_id != ToolPackId.EXCHANGE:
        raise ValueError(f"unsupported ToolPack: {toolpack_id}")
    content = (
        "Exchange is an approved read-only executable ToolPack. It provides official-source FX "
        "data status, data-quality checks, BOK/ECB cross-check evidence, walk-forward model "
        "performance, and reproducible repository tests. It cannot place trades, connect accounts, "
        "handle credentials, issue personalized buy/sell instructions, or promise profit. During "
        "execution OneBrief runs only the fixed integrity commands and packages fixed evidence files."
    )
    return InternalSource(
        name="toolpack-exchange-capability.md",
        priority=SourcePriority.MANDATORY,
        requirement_keys=["exchange_toolpack"],
        summary="Approved Exchange read-only data and verification capability.",
        content=content,
        media_type="text/markdown",
        size_bytes=len(content.encode("utf-8")),
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def attach_toolpack_descriptors(intake):
    """Attach bounded capability evidence before requirements inspection."""
    existing = {source.name for source in intake.internal_sources}
    additions = [
        toolpack_descriptor(toolpack_id)
        for toolpack_id in intake.toolpack_ids
        if f"toolpack-{toolpack_id.value}-capability.md" not in existing
    ]
    return intake.model_copy(update={"internal_sources": [*intake.internal_sources, *additions]})


class ExchangeToolPack:
    """Read-only adapter over the existing Exchange repository."""

    def __init__(self, root: Path | None = None, runner: CommandRunner | None = None):
        configured = root or Path(
            os.environ.get(
                "ONEBRIEF_EXCHANGE_ROOT",
                r"C:\exchange" if os.name == "nt" else "/opt/onebrief/toolpacks/exchange",
            )
        )
        self.root = configured.resolve()
        self.runner = runner or _command_runner

    def _validate_root(self) -> None:
        required = [self.root / "package.json", *(self.root / item for item in EXCHANGE_EVIDENCE)]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Exchange ToolPack is unavailable or incomplete: " + ", ".join(missing[:3])
            )

    def _run_command(self, command_id: str, args: list[str]) -> ToolCommandResult:
        npm = "npm.cmd" if os.name == "nt" else "npm"
        result = self.runner([npm, *args], self.root, 180)
        return result.model_copy(update={"command_id": command_id})

    def execute(self, output_dir: Path) -> tuple[ToolPackRun, list[InternalSource]]:
        self._validate_root()
        commands = [
            self._run_command("repository_tests", ["test"]),
            self._run_command("transfer_integrity", ["run", "verify:transfer"]),
        ]
        pack_dir = output_dir / ToolPackId.EXCHANGE.value
        evidence_dir = pack_dir / "evidence"
        evidence: list[ToolEvidence] = []
        sources: list[InternalSource] = []
        for relative in EXCHANGE_EVIDENCE:
            source = (self.root / relative).resolve()
            if not source.is_relative_to(self.root) or source.is_symlink():
                raise PermissionError(f"unsafe Exchange evidence path: {relative}")
            destination = evidence_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            digest = _sha256(destination)
            evidence.append(ToolEvidence(
                source_path=relative,
                packaged_path=destination.relative_to(output_dir.parent).as_posix(),
                size_bytes=destination.stat().st_size,
                sha256=digest,
            ))
            if destination.stat().st_size <= 120_000:
                content = destination.read_text(encoding="utf-8")
                sources.append(InternalSource(
                    name=f"exchange/{relative}",
                    priority=SourcePriority.MANDATORY,
                    requirement_keys=["exchange_toolpack"],
                    summary="Evidence produced by the approved read-only Exchange ToolPack.",
                    content=content,
                    media_type="application/json" if destination.suffix == ".json" else "text/markdown",
                    size_bytes=destination.stat().st_size,
                    sha256=digest,
                ))
        run = ToolPackRun(
            toolpack_id=ToolPackId.EXCHANGE,
            status="passed",
            commands=commands,
            evidence=evidence,
            safety_boundary=[
                "read-only fixed commands and fixed evidence paths",
                "no trading, account connection, credentials, or personal portfolio data",
                "no personalized buy/sell instruction or profit guarantee",
            ],
        )
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / "toolpack_run.json").write_text(
            run.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        return run, sources


def execute_toolpacks(
    toolpack_ids: list[ToolPackId], output_dir: Path
) -> tuple[list[ToolPackRun], list[InternalSource]]:
    existing_manifest = output_dir / "toolpack_execution.json"
    if existing_manifest.exists():
        payload = json.loads(existing_manifest.read_text(encoding="utf-8"))
        runs = [ToolPackRun.model_validate(item) for item in payload.get("runs", [])]
        sources: list[InternalSource] = []
        for run in runs:
            for evidence in run.evidence:
                path = output_dir / run.toolpack_id.value / "evidence" / evidence.source_path
                if not path.is_file():
                    raise FileNotFoundError(f"packaged ToolPack evidence is missing: {evidence.source_path}")
                if _sha256(path) != evidence.sha256:
                    raise RuntimeError(f"packaged ToolPack evidence changed: {evidence.source_path}")
                if path.is_file() and path.stat().st_size <= 120_000:
                    content = path.read_text(encoding="utf-8")
                    sources.append(InternalSource(
                        name=f"{run.toolpack_id.value}/{evidence.source_path}", priority=SourcePriority.MANDATORY,
                        requirement_keys=[f"{run.toolpack_id.value}_toolpack"], content=content,
                        media_type="application/json" if path.suffix == ".json" else "text/markdown",
                        size_bytes=path.stat().st_size, sha256=_sha256(path),
                    ))
        return runs, sources
    runs: list[ToolPackRun] = []
    sources: list[InternalSource] = []
    for toolpack_id in toolpack_ids:
        if toolpack_id != ToolPackId.EXCHANGE:
            raise ValueError(f"unsupported ToolPack: {toolpack_id}")
        run, generated = ExchangeToolPack().execute(output_dir)
        runs.append(run)
        sources.extend(generated)
    manifest = {
        "schema_version": "onebrief-toolpack-execution-v1",
        "runs": [run.model_dump(mode="json") for run in runs],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "toolpack_execution.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return runs, sources
