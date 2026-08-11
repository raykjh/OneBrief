"""One bounded Gemini 3.5 Flash request grounded with Google Search."""

from __future__ import annotations

import json
from math import ceil

from google.genai import types

from onebrief.execution_limits import PUBLIC_RESEARCH_OUTPUT_CAP
from onebrief.producer import approximate_tokens
from onebrief.public_research import PublicResearchResult, web_source


def run_grounded_research(
    gateway: object,
    *,
    goal: str,
    desired_output: str | None,
    completion_contract: dict[str, object] | None = None,
    stage: str = "public_research",
    prior_research: str | None = None,
    blocking_issues: list[str] | None = None,
) -> PublicResearchResult:
    """Run one grounded research or evidence-repair prompt under a fixed reservation."""
    model = (
        gateway.model_for(stage)
        if hasattr(gateway, "model_for")
        else "gemini-3.5-flash"
    )
    fixed_cost_cap = 0.035
    system_instruction = (
        "You are OneBrief's public research agent. Use Google Search for current public facts. "
        "Never invent a listing, price, fee, date, or URL. Distinguish an explicitly advertised "
        "value from an estimate. Satisfy the supplied completion contract's evidence requirements, "
        "not merely the broad goal. For lists, return one Markdown table with one individually named "
        "item per row and include a direct source URL, checked date, and uncertainty columns. A category "
        "or market segment is not a named item. Never infer that no equivalent exists merely because a "
        "candidate is new; search named comparisons and use bounded wording such as 'not identified within "
        "this search scope'. Never use absolute safety or no-side-effect language. When a requested value is "
        "absent, write '확인 필요' rather than guessing. Keep high-impact decisions with the user."
    )
    contents = json.dumps(
        {
            "goal": goal,
            "desired_output": desired_output,
            "completion_contract": completion_contract,
            "instructions": (
                "Research enough current candidates to answer the goal. Apply every numeric and "
                "geographic condition exactly. Preserve direct source links in the report."
            ),
            "prior_research": prior_research,
            "blocking_evidence_issues": blocking_issues or [],
            "repair_instructions": (
                "When prior research and blocking issues are supplied, replace unsupported evidence "
                "rather than merely rewriting its wording. Put every required named comparison item "
                "and its directly inspectable URL on the same Markdown table row. Do not count a "
                "bibliography, provider homepage, category, or market segment as item-level evidence."
                if prior_research else None
            ),
        },
        ensure_ascii=False,
    )
    tool = types.Tool(google_search=types.GoogleSearch())
    generation = types.GenerationConfig(
        max_output_tokens=PUBLIC_RESEARCH_OUTPUT_CAP,
        temperature=0.0,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    count = gateway.client.models.count_tokens(
        model=model,
        contents=contents,
        config=types.CountTokensConfig(
            system_instruction=system_instruction,
            tools=[tool],
            generation_config=generation,
        ),
    )
    observed = int(count.total_tokens or 0)
    input_cap = ceil((observed + approximate_tokens(system_instruction)) * 1.15) + 256
    reservation = gateway.store.reserve_call(
        stage=stage,
        model=model,
        input_token_cap=input_cap,
        output_token_cap=PUBLIC_RESEARCH_OUTPUT_CAP,
        fixed_cost_usd=fixed_cost_cap,
    )
    try:
        response = gateway.client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[tool],
                max_output_tokens=PUBLIC_RESEARCH_OUTPUT_CAP,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception as exc:
        gateway.store.release_call(
            reservation.call_id, f"provider call failed: {type(exc).__name__}"
        )
        raise

    metadata = response.candidates[0].grounding_metadata if response.candidates else None
    sources = []
    seen_urls: set[str] = set()
    for chunk in (getattr(metadata, "grounding_chunks", None) or []):
        web = getattr(chunk, "web", None)
        url = getattr(web, "uri", None) if web else None
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append(
            web_source(f"W{len(sources) + 1:02d}", getattr(web, "title", None), url)
        )

    usage = response.usage_metadata
    actual_input = int(getattr(usage, "prompt_token_count", 0) or observed)
    candidate_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    thought_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
    actual_output = candidate_tokens + thought_tokens
    if actual_output == 0:
        total = int(getattr(usage, "total_token_count", 0) or actual_input)
        actual_output = max(0, total - actual_input)
    gateway.store.settle_call(
        reservation.call_id,
        input_tokens=actual_input,
        output_tokens=actual_output,
        fixed_cost_usd=fixed_cost_cap if sources else 0.0,
    )
    if not sources:
        raise ValueError("Google Search returned no grounded source URLs.")
    entry = getattr(metadata, "search_entry_point", None)
    return PublicResearchResult(
        query=goal,
        answer_markdown=response.text or "",
        sources=sources,
        search_queries=list(getattr(metadata, "web_search_queries", None) or []),
        search_suggestions_html=getattr(entry, "rendered_content", "") or "",
    )
