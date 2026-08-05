"""Grounded public-research contracts and source conversion."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field

from onebrief.schemas import InternalSource, SourcePriority


class PublicWebSource(BaseModel):
    source_id: str
    title: str
    url: str
    domain: str = ""


class PublicResearchResult(BaseModel):
    query: str
    answer_markdown: str
    sources: list[PublicWebSource] = Field(min_length=1)
    search_queries: list[str] = Field(default_factory=list)
    search_suggestions_html: str = ""

    def as_internal_source(self) -> InternalSource:
        source_lines = ["## 공개 출처"]
        source_lines.extend(
            f"- [{source.source_id}] {source.title} — {source.url}"
            for source in self.sources
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
