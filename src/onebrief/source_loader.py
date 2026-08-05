"""Safe, deterministic ingestion for the first local upload flow."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from onebrief.schemas import InternalSource, SourceRecord, UploadManifest

ALLOWED_SUFFIXES = {".txt", ".md", ".json", ".csv", ".yaml", ".yml"}
MAX_FILE_BYTES = 500_000


def load_uploads(manifest: UploadManifest, manifest_dir: Path) -> list[InternalSource]:
    sources: list[InternalSource] = []
    for entry in manifest.sources:
        path = Path(entry.path)
        if not path.is_absolute():
            path = manifest_dir / path
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"upload is not a file: {path}")
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            raise ValueError(f"unsupported upload type: {path.suffix}")
        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"upload exceeds {MAX_FILE_BYTES} bytes: {path.name}")
        content = raw.decode("utf-8-sig")
        if not content.strip():
            raise ValueError(f"upload is empty: {path.name}")
        media_type = mimetypes.guess_type(path.name)[0] or "text/plain"
        sources.append(
            InternalSource(
                name=path.name,
                priority=entry.priority,
                requirement_keys=entry.requirement_keys,
                summary=entry.summary,
                content=content,
                media_type=media_type,
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    return sources


def source_records(sources: list[InternalSource]) -> list[SourceRecord]:
    return [
        SourceRecord(
            name=source.name,
            requirement_keys=source.requirement_keys,
            priority=source.priority,
            media_type=source.media_type,
            size_bytes=source.size_bytes,
            sha256=source.sha256 or "0" * 64,
        )
        for source in sources
    ]

