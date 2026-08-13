"""ADK-native maker -> verifier -> original-maker convergence workflow."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing_extensions import override

from onebrief.execution_schemas import VerificationReport, Verdict
MAKER_STATE_KEY = "onebrief_maker_artifact"
VERIFICATION_STATE_KEY = "onebrief_verification"
ROUND_STATE_KEY = "onebrief_convergence_round"
VERIFIER_CONTEXT_STATE_KEY = "onebrief_verifier_context"
SKIP_VERIFIER_STATE_KEY = "onebrief_skip_verifier"
REVERIFY_EXISTING_STATE_KEY = "onebrief_reverify_existing_candidate"
EXACT_EDIT_ANCHORS_STATE_KEY = "onebrief_exact_edit_anchors"
REPAIR_PLAN_STATE_KEY = "onebrief_repair_plan"
REPAIR_CONTRACT_STATE_KEY = "onebrief_repair_contract"
MAKER_MODEL_BINDING_STATE_KEY = "onebrief_maker_model_binding"
MAKER_SCHEMA_BINDING_STATE_KEY = "onebrief_maker_schema_binding"


class BudgetedAdkLlm(BaseLlm):
    """ADK BaseLlm that preserves OneBrief's hard budget gateway."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    gateway: Any
    stage: str
    response_model: Any = None

    @override
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if stream:
            raise ValueError("OneBrief ADK convergence uses non-streaming bounded turns")
        self._maybe_append_user_content(llm_request)
        response = await asyncio.to_thread(
            self.gateway.generate_adk_response,
            stage=self.stage,
            model=self.model,
            contents=llm_request.contents,
            config=llm_request.config,
        )
        def structured_failure(candidate_response) -> tuple[bool, str]:
            candidates = list(getattr(candidate_response, "candidates", None) or [])
            finish_reason = (
                getattr(candidates[0], "finish_reason", None) if candidates else None
            )
            if "MAX_TOKENS" in str(finish_reason).upper():
                return True, "the response reached MAX_TOKENS"
            if getattr(llm_request.config, "response_mime_type", None) != "application/json":
                return False, ""
            try:
                parsed = json.loads(getattr(candidate_response, "text", "") or "")
                if (
                    isinstance(self.response_model, type)
                    and issubclass(self.response_model, BaseModel)
                ):
                    self.response_model.model_validate(parsed)
            except (TypeError, json.JSONDecodeError) as exc:
                return True, "invalid JSON: " + str(exc)
            except ValidationError as exc:
                return True, "schema validation failed: " + " ".join(str(exc).split())[:1200]
            return False, ""

        invalid, failure_detail = structured_failure(response)
        for compact_attempt in range(1, 3):
            if not invalid:
                break
            # A truncated structured response is not useful evidence and cannot be
            # parsed by ADK. Retry twice at most as a deliberately small incremental edit.
            # Later convergence rounds can add the next increment after deterministic
            # verification, avoiding a fragile whole-project JSON blob.
            compact_contents = list(llm_request.contents) + [types.Content(
                role="user",
                parts=[types.Part(text=(
                    "The previous structured response was truncated or invalid JSON, or it was invalid "
                    "under the active Pydantic schema, and was discarded. "
                    f"Validation detail: {failure_detail}. "
                    "Return a valid, much smaller response in the required schema. If the schema is a "
                    "code change set, return exactly one changed path unless the current verification "
                    "contract explicitly requires the atomic Unity evidence pair (one PlayMode .cs and "
                    "one sibling test .asmdef); obey the selector fields offered by "
                    "the current schema. Every change object must include exactly one complete mechanism: "
                    "content for a bounded new file, anchor_id plus replace, search plus replace, or "
                    "start_anchor plus end_anchor plus replace. Never return path and reason alone. Replace "
                    "only one coherent range under 6000 characters; do not "
                    "return complete existing or candidate-file contents. If the schema is a narrative artifact, "
                    "keep its body under 8000 characters while covering every acceptance criterion with "
                    "concise evidence. Implement the highest-priority verified slice now; later maker "
                    "rounds can add remaining detail. Return only the required schema."
                ))],
            )]
            retry_config = llm_request.config.model_copy(deep=True)
            retry_config.max_output_tokens = min(
                20_000,
                max(6_000, int(retry_config.max_output_tokens or 0) * 2),
            )
            response = await asyncio.to_thread(
                self.gateway.generate_adk_response,
                stage=f"{self.stage}_compact_retry",
                model=self.model,
                contents=compact_contents,
                config=retry_config,
            )
            invalid, failure_detail = structured_failure(response)
        yield LlmResponse.create(response)


GateHook = Callable[
    [VerificationReport, InvocationContext, int],
    VerificationReport | Awaitable[VerificationReport],
]
MakerHook = Callable[
    [Any, InvocationContext, int],
    dict[str, Any] | Awaitable[dict[str, Any] | None] | None,
]
MakerModelHook = Callable[
    [VerificationReport | None, InvocationContext, int],
    dict[str, Any] | Awaitable[dict[str, Any] | None] | None,
]
MakerSchemaHook = Callable[
    [VerificationReport | None, InvocationContext, int],
    type | None,
]


class AdkConvergenceAgent(BaseAgent):
    """Run one accountable maker and one independent verifier until convergence.

    The same maker object is reused on every revision. Deterministic OneBrief
    gates run after the verifier through ``verification_gate`` and therefore
    remain authoritative over an LLM-issued PASS.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    maker_state_key: str = MAKER_STATE_KEY
    verification_state_key: str = VERIFICATION_STATE_KEY
    max_revision_rounds: int = Field(default=2, ge=0, le=12)
    maker_model_selector: MakerModelHook | None = None
    maker_schema_selector: MakerSchemaHook | None = None
    after_maker: MakerHook | None = None
    verification_gate: GateHook | None = None

    @property
    def maker(self) -> BaseAgent:
        return self.sub_agents[0]

    @property
    def verifier(self) -> BaseAgent:
        return self.sub_agents[1]

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        if len(self.sub_agents) != 2:
            raise ValueError("ADK convergence requires exactly maker and verifier sub-agents")
        for round_number in range(self.max_revision_rounds + 1):
            round_event = Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                branch=ctx.branch,
                actions=EventActions(state_delta={
                    ROUND_STATE_KEY: round_number,
                    SKIP_VERIFIER_STATE_KEY: False,
                }),
            )
            yield round_event
            reverify_existing = (
                round_number == 0
                and bool(ctx.session.state.get(REVERIFY_EXISTING_STATE_KEY))
                and ctx.session.state.get(self.maker_state_key) is not None
            )
            if not reverify_existing:
                if self.maker_schema_selector is not None:
                    raw_verification = ctx.session.state.get(self.verification_state_key)
                    current_report = (
                        VerificationReport.model_validate(raw_verification)
                        if raw_verification is not None
                        else None
                    )
                    selected_schema = self.maker_schema_selector(
                        current_report, ctx, round_number
                    )
                    if selected_schema is not None:
                        if not issubclass(selected_schema, __import__("pydantic").BaseModel):
                            raise TypeError("dynamic maker schema must be a Pydantic model")
                        self.maker.output_schema = selected_schema
                        budgeted_model = getattr(self.maker, "model", None)
                        if isinstance(budgeted_model, BudgetedAdkLlm):
                            budgeted_model.response_model = selected_schema
                        yield Event(
                            author=self.name,
                            invocation_id=ctx.invocation_id,
                            branch=ctx.branch,
                            actions=EventActions(state_delta={
                                MAKER_SCHEMA_BINDING_STATE_KEY: selected_schema.__name__
                            }),
                        )
                if self.maker_model_selector is not None:
                    raw_verification = ctx.session.state.get(self.verification_state_key)
                    current_report = (
                        VerificationReport.model_validate(raw_verification)
                        if raw_verification is not None
                        else None
                    )
                    binding = self.maker_model_selector(
                        current_report, ctx, round_number
                    )
                    if isinstance(binding, Awaitable):
                        binding = await binding
                    if binding:
                        previous_binding = ctx.session.state.get(
                            MAKER_MODEL_BINDING_STATE_KEY
                        )
                        previous_decision = (
                            previous_binding.get("decision", {})
                            if isinstance(previous_binding, dict) else {}
                        )
                        next_decision = (
                            binding.get("decision", {})
                            if isinstance(binding, dict) else {}
                        )
                        if (
                            isinstance(previous_binding, dict)
                            and (
                                bool(previous_decision.get("escalated"))
                                or bool(previous_binding.get("sticky_escalation"))
                            )
                            and not bool(next_decision.get("escalated"))
                        ):
                            # Escalation belongs to this persistent maker and
                            # unresolved failure boundary. Do not silently
                            # demote when the latest normalized message no
                            # longer repeats the classifier's original phrase.
                            binding = {
                                **dict(binding),
                                "model": previous_binding.get("model"),
                                "stage": previous_binding.get(
                                    "stage", "long_form_draft"
                                ),
                                "sticky_escalation": True,
                                "prior_binding": dict(previous_binding),
                                "decision": previous_decision,
                            }
                        model = str(binding.get("model", "")).strip()
                        stage = str(binding.get("stage", "")).strip()
                        if not model or not stage:
                            raise ValueError(
                                "dynamic maker model binding requires model and stage"
                            )
                        budgeted_model = getattr(self.maker, "model", None)
                        if not isinstance(budgeted_model, BudgetedAdkLlm):
                            raise TypeError(
                                "dynamic maker model binding requires BudgetedAdkLlm"
                            )
                        # Keep the exact same LlmAgent instance, conversation, role,
                        # and artifact state. Only the pre-approved model rung and
                        # policy-bound call stage change for this revision turn.
                        budgeted_model.model = model
                        budgeted_model.stage = stage
                        binding_state_delta: dict[str, Any] = {
                            MAKER_MODEL_BINDING_STATE_KEY: dict(binding)
                        }
                        execution_phase = str(
                            binding.get("execution_phase", "")
                        ).strip()
                        if execution_phase:
                            binding_state_delta[
                                "onebrief:execution_phase"
                            ] = execution_phase
                        phase_decision = binding.get("phase_decision")
                        if isinstance(phase_decision, dict):
                            binding_state_delta[
                                "onebrief:phase_decision"
                            ] = dict(phase_decision)
                        yield Event(
                            author=self.name,
                            invocation_id=ctx.invocation_id,
                            branch=ctx.branch,
                            actions=EventActions(state_delta=binding_state_delta),
                        )
                async for event in self.maker.run_async(ctx):
                    yield event
            maker_output = ctx.session.state.get(self.maker_state_key)
            if maker_output is None:
                raise RuntimeError("ADK maker produced no structured state output")
            hook_delta: dict[str, Any] = {}
            if self.after_maker is not None:
                delta = self.after_maker(maker_output, ctx, round_number)
                if isinstance(delta, Awaitable):
                    delta = await delta
                if delta:
                    hook_delta = delta
                    yield Event(
                        author=self.name,
                        invocation_id=ctx.invocation_id,
                        branch=ctx.branch,
                        actions=EventActions(state_delta=delta),
                    )
            skip_verifier = bool(hook_delta.get(SKIP_VERIFIER_STATE_KEY, False))
            if not skip_verifier:
                async for event in self.verifier.run_async(ctx):
                    yield event
            raw_report = hook_delta.get(
                self.verification_state_key,
                ctx.session.state.get(self.verification_state_key),
            )
            if raw_report is None:
                raise RuntimeError("ADK verifier produced no structured state output")
            report = VerificationReport.model_validate(raw_report)
            if self.verification_gate is not None:
                gated = self.verification_gate(report, ctx, round_number)
                report = await gated if isinstance(gated, Awaitable) else gated
                yield Event(
                    author=self.name,
                    invocation_id=ctx.invocation_id,
                    branch=ctx.branch,
                    actions=EventActions(
                        state_delta={self.verification_state_key: report.model_dump(mode="json")}
                    ),
                )
            if report.verdict != Verdict.REVISE:
                return


def build_text_convergence_agent(
    *,
    gateway: Any,
    maker_model: str,
    verifier_model: str,
    maker_schema: type,
    max_revision_rounds: int,
    maker_instruction: str,
    verifier_instruction: str,
    maker_stage: str = "long_form_draft",
    verifier_stage: str = "independent_verification",
    maker_output_tokens: int = 6000,
    verifier_output_tokens: int = 2200,
    maker_model_selector: MakerModelHook | None = None,
    maker_schema_selector: MakerSchemaHook | None = None,
    after_maker: MakerHook | None = None,
    verification_gate: GateHook | None = None,
) -> AdkConvergenceAgent:
    """Build the production ADK agent tree with one persistent maker identity."""

    def contextual_maker_instruction(ctx) -> str:
        previous = ctx.state.get(MAKER_STATE_KEY)
        verification = ctx.state.get(VERIFICATION_STATE_KEY)
        return maker_instruction + "\n\nCURRENT REVISION CONTEXT:\n" + json.dumps(
            {
                "previous_artifact": previous,
                "verification_feedback": verification,
                "repair_plan": ctx.state.get(REPAIR_PLAN_STATE_KEY),
                "repair_contract": ctx.state.get(REPAIR_CONTRACT_STATE_KEY),
                "exact_edit_anchors": ctx.state.get(EXACT_EDIT_ANCHORS_STATE_KEY, []),
                "execution_phase": ctx.state.get("onebrief:execution_phase"),
            },
            ensure_ascii=False,
        )

    def contextual_verifier_instruction(ctx) -> str:
        return verifier_instruction + "\n\nVERIFICATION PAYLOAD:\n" + json.dumps({
            "artifact": ctx.state.get(MAKER_STATE_KEY),
            "implementation_evidence": ctx.state.get(VERIFIER_CONTEXT_STATE_KEY),
        }, ensure_ascii=False)

    maker = LlmAgent(
        name="onebrief_maker",
        description="Accountable maker that creates and revises its own artifact.",
        model=BudgetedAdkLlm(
            model=maker_model, gateway=gateway, stage=maker_stage,
            response_model=maker_schema,
        ),
        instruction=contextual_maker_instruction,
        output_schema=maker_schema,
        output_key=MAKER_STATE_KEY,
        include_contents="default",
        mode="single_turn",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.1, max_output_tokens=maker_output_tokens
        ),
    )
    verifier = LlmAgent(
        name="onebrief_independent_verifier",
        description="Independent verifier that cannot modify or approve its own work.",
        model=BudgetedAdkLlm(
            model=verifier_model, gateway=gateway, stage=verifier_stage,
            response_model=VerificationReport,
        ),
        instruction=contextual_verifier_instruction,
        output_schema=VerificationReport,
        output_key=VERIFICATION_STATE_KEY,
        # The verifier receives the current artifact and trusted implementation
        # evidence through contextual_verifier_instruction.  Replaying the
        # original repository payload and every maker turn duplicates tens of
        # thousands of tokens, weakens role separation, and can make an
        # otherwise valid Vertex request exceed provider limits.
        include_contents="none",
        mode="single_turn",
        generate_content_config=types.GenerateContentConfig(
            temperature=0.0, max_output_tokens=verifier_output_tokens
        ),
    )
    return AdkConvergenceAgent(
        name="onebrief_quality_convergence",
        description="ADK-native maker, verifier, and original-maker revision loop.",
        sub_agents=[maker, verifier],
        max_revision_rounds=max_revision_rounds,
        maker_model_selector=maker_model_selector,
        maker_schema_selector=maker_schema_selector,
        after_maker=after_maker,
        verification_gate=verification_gate,
    )


async def run_convergence_agent(
    agent: AdkConvergenceAgent,
    payload: dict[str, Any],
    *,
    app_name: str = "onebrief-convergence",
    initial_state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run and return final ADK state plus a durable, JSON-safe event trace."""

    user_id = "onebrief-worker"
    session_id = __import__("uuid").uuid4().hex
    sessions = InMemorySessionService()
    await sessions.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
        state=initial_state or {},
    )
    runner = Runner(agent=agent, app_name=app_name, session_service=sessions)
    trace: list[dict[str, Any]] = []
    message = types.Content(
        role="user",
        parts=[types.Part(text=json.dumps(payload, ensure_ascii=False, indent=2))],
    )
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=message
    ):
        trace.append({
            "author": event.author,
            "invocation_id": event.invocation_id,
            "state_delta_keys": sorted(event.actions.state_delta),
            "final_response": event.is_final_response(),
        })
    session = await sessions.get_session(
        app_name=app_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        raise RuntimeError("ADK convergence session disappeared")
    return dict(session.state), trace
