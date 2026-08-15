"""Grounded public-research contracts and source conversion."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse
from urllib.parse import urljoin

import httpx

from pydantic import BaseModel, Field

from onebrief.schemas import InternalSource, SourcePriority


class PublicWebSource(BaseModel):
    source_id: str
    title: str
    url: str
    domain: str = ""
    resolved_url: str | None = None
    http_status: int | None = None


class PublicResearchResult(BaseModel):
    query: str
    answer_markdown: str
    sources: list[PublicWebSource] = Field(min_length=1)
    search_queries: list[str] = Field(default_factory=list)
    search_suggestions_html: str = ""

    def as_internal_source(self) -> InternalSource:
        source_lines = ["## 공개 출처"]
        for source in self.sources:
            inspectable_url = source.resolved_url or source.url
            status = (
                f" [HTTP {source.http_status}]"
                if source.http_status is not None else ""
            )
            grounding = (
                f" (Google grounding: {source.url})"
                if source.resolved_url and source.resolved_url != source.url else ""
            )
            source_lines.append(
                f"- [{source.source_id}] {source.title} — {inspectable_url}{status}{grounding}"
            )
        content = self.answer_markdown.rstrip() + "\n\n" + "\n".join(source_lines)
        return InternalSource(
            name="public_research.md",
            priority=SourcePriority.MANDATORY,
            requirement_keys=["public_research"],
            summary="Gemini grounded public-web research with preserved source URLs.",
            content=content,
            media_type="text/markdown",
            size_bytes=len(content.encode("utf-8")),
        )


def web_source(source_id: str, title: str | None, url: str) -> PublicWebSource:
    return PublicWebSource(
        source_id=source_id,
        title=(title or url)[:500],
        url=url,
        domain=urlparse(url).netloc,
    )


def _public_host(hostname: str) -> bool:
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        }
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(item).is_global for item in addresses)


def resolve_public_source(
    source: PublicWebSource,
    *,
    timeout_seconds: float = 8.0,
    max_redirects: int = 6,
) -> PublicWebSource:
    """Resolve one Google grounding redirect without following private-network hops."""

    current = source.url
    headers = {"User-Agent": "OneBriefEvidenceObserver/1.0"}
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout_seconds,
            headers=headers,
        ) as client:
            for _ in range(max_redirects + 1):
                parsed = urlparse(current)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    return source
                if not _public_host(parsed.hostname):
                    return source
                response = client.head(current)
                status_code = response.status_code
                response_url = str(response.url)
                response_headers = response.headers
                # A number of inspectable public sites (including government
                # registries) do not implement HEAD consistently: they return
                # 404/500 for HEAD while serving the same URL with GET. Treat
                # HEAD as the cheap probe, not as final evidence of failure.
                if not 200 <= status_code < 400:
                    # Do not add a Range header: some legacy government sites
                    # reject ranged GETs too. Streaming lets us inspect the
                    # response status and close it without consuming the body.
                    with client.stream("GET", current) as fallback:
                        status_code = fallback.status_code
                        response_url = str(fallback.url)
                        response_headers = fallback.headers
                if status_code in {301, 302, 303, 307, 308}:
                    location = response_headers.get("location")
                    if not location:
                        return source
                    current = urljoin(current, location)
                    continue
                return source.model_copy(update={
                    "resolved_url": response_url,
                    "http_status": status_code,
                })
    except (httpx.HTTPError, OSError):
        return source
    return source


def merge_public_sources(
    *source_groups: list[PublicWebSource],
) -> list[PublicWebSource]:
    """Keep grounded evidence discovered in any refinement round."""

    merged: list[PublicWebSource] = []
    seen: dict[str, int] = {}
    for group in source_groups:
        for raw_source in group:
            source = PublicWebSource.model_validate(raw_source)
            key = (source.resolved_url or source.url).casefold()
            if key in seen:
                existing_index = seen[key]
                existing = merged[existing_index]
                existing_ok = (
                    existing.http_status is not None
                    and 200 <= existing.http_status < 400
                )
                source_ok = (
                    source.http_status is not None
                    and 200 <= source.http_status < 400
                )
                if source_ok and not existing_ok:
                    merged[existing_index] = source.model_copy(update={
                        "source_id": existing.source_id,
                    })
                continue
            seen[key] = len(merged)
            merged.append(source.model_copy(update={
                "source_id": f"W{len(merged) + 1:02d}",
            }))
    return merged
