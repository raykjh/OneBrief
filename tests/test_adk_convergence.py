import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from pydantic import ConfigDict
from typing_extensions import override

from onebrief.adk_convergence import (
    MAKER_STATE_KEY,
    MAKER_MODEL_BINDING_STATE_KEY,
    MAKER_SCHEMA_BINDING_STATE_KEY,
    MAKER_SCHEMA_FAILURE_STATE_KEY,
    REVERIFY_EXISTING_STATE_KEY,
    EXACT_EDIT_ANCHORS_STATE_KEY,
    MAKER_DIAGNOSTIC_CONTEXT_STATE_KEY,
    REPAIR_CONTRACT_STATE_KEY,
    REPAIR_PLAN_STATE_KEY,
    VERIFIER_CONTEXT_STATE_KEY,
    VERIFICATION_STATE_KEY,
    AdkConvergenceAgent,
    BudgetedAdkLlm,
    InvalidStructuredMakerOutput,
    build_text_convergence_agent,
    run_convergence_agent,
)
from onebrief.execution_schemas import CriterionCheck, DraftArtifact, VerificationReport, Verdict
from onebrief.guarded_gemini import BudgetedGeminiClient


def _report(verdict: Verdict) -> dict[str, Any]:
    revise = verdict == Verdict.REVISE
    return VerificationReport(
        verdict=verdict,
        criterion_checks=[CriterionCheck(
            criterion="Requested result", passed=not revise,
            evidence="Needs repair." if revise else "Observed passing evidence.",
        )],
        blocking_issues=["Repair the result."] if revise else [],
        revision_instructions=["Repair the result."] if revise else [],
        missing_information=[],
    ).model_dump(mode="json")


class FakeMaker(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    calls: int = 0

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        self.calls += 1
        previous_verification = ctx.session.state.get(VERIFICATION_STATE_KEY)
        yield Event(
            author=self.name, invocation_id=ctx.invocation_id, branch=ctx.branch,
            actions=EventActions(state_delta={MAKER_STATE_KEY: {
                "version": self.calls,
                "received_feedback": previous_verification is not None,
            }}),
        )


class FakeVerifier(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    calls: int = 0

    @override
    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        self.calls += 1
        verdict = Verdict.REVISE if self.calls == 1 else Verdict.PASS
        yield Event(
            author=self.name, invocation_id=ctx.invocation_id, branch=ctx.branch,
            actions=EventActions(state_delta={VERIFICATION_STATE_KEY: _report(verdict)}),
        )


def test_adk_loop_reuses_original_maker_and_returns_feedback() -> None:
    maker = FakeMaker(name="maker")
    verifier = FakeVerifier(name="verifier")
    agent = AdkConvergenceAgent(
        name="convergence", sub_agents=[maker, verifier], max_revision_rounds=2,
    )

    state, trace = asyncio.run(
        run_convergence_agent(agent, {"goal": "Complete the work."})
    )

    assert maker.calls == 2
    assert verifier.calls == 2
    assert state[MAKER_STATE_KEY] == {"version": 2, "received_feedback": True}
    assert state[VERIFICATION_STATE_KEY]["verdict"] == Verdict.PASS.value
    assert [item["author"] for item in trace].count("maker") == 2
    assert [item["author"] for item in trace].count("verifier") == 2


def test_reverification_starts_with_existing_candidate_without_maker_call() -> None:
    maker = FakeMaker(name="maker")
    verifier = FakeVerifier(name="verifier", calls=1)
    agent = AdkConvergenceAgent(
        name="convergence", sub_agents=[maker, verifier], max_revision_rounds=1,
    )

    state, trace = asyncio.run(run_convergence_agent(
        agent,
        {"goal": "Reverify the existing candidate."},
        initial_state={
            MAKER_STATE_KEY: {"version": "existing"},
            REVERIFY_EXISTING_STATE_KEY: True,
        },
    ))

    assert maker.calls == 0
    assert verifier.calls == 2
    assert state[MAKER_STATE_KEY] == {"version": "existing"}
    assert [item["author"] for item in trace].count("maker") == 0


def test_adk_llm_retries_max_token_response_as_compact_increment() -> None:
    class Gateway:
        def __init__(self):
            self.calls = []

        def generate_adk_response(self, *, stage, contents, **_kwargs):
            self.calls.append((stage, contents))
            finish = (
                types.FinishReason.MAX_TOKENS
                if len(self.calls) == 1
                else types.FinishReason.STOP
            )
            return types.GenerateContentResponse(
                candidates=[types.Candidate(
                    finish_reason=finish,
                    content=types.Content(
                        role="model", parts=[types.Part(text='{"ok":true}')]
                    ),
                )]
            )

    async def collect():
        gateway = Gateway()
        model = BudgetedAdkLlm(
            model="gemini-3.5-flash", gateway=gateway, stage="long_form_draft"
        )
        request = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="build")])],
            config=types.GenerateContentConfig(max_output_tokens=20_000),
        )
        responses = [item async for item in model.generate_content_async(request)]
        return gateway, responses

    gateway, responses = asyncio.run(collect())
    assert len(responses) == 1
    assert [item[0] for item in gateway.calls] == [
        "long_form_draft", "long_form_draft_compact_retry"
    ]
    retry_text = gateway.calls[1][1][-1].parts[0].text
    assert "exactly one changed path" in retry_text
    assert "obey the selector fields offered by the current schema" in retry_text
    assert "complete existing or candidate-file contents" in retry_text
    assert "under 6000 characters" in retry_text
    assert "narrative artifact" in retry_text
    assert "under 8000 characters" in retry_text


def test_adk_llm_retries_invalid_structured_json_even_when_model_reports_stop() -> None:
    class Gateway:
        def __init__(self):
            self.calls = []

        def generate_adk_response(self, *, stage, contents, **_kwargs):
            self.calls.append((stage, contents))
            text = '{"verdict":"PASS"' if len(self.calls) == 1 else '{"verdict":"PASS"}'
            return types.GenerateContentResponse(
                candidates=[types.Candidate(
                    finish_reason=types.FinishReason.STOP,
                    content=types.Content(role="model", parts=[types.Part(text=text)]),
                )]
            )

    async def collect():
        gateway = Gateway()
        model = BudgetedAdkLlm(
            model="gemini-3.5-flash",
            gateway=gateway,
            stage="independent_verification",
        )
        request = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="verify")])],
            config=types.GenerateContentConfig(
                max_output_tokens=20_000,
                response_mime_type="application/json",
            ),
        )
        responses = [item async for item in model.generate_content_async(request)]
        return gateway, responses

    gateway, responses = asyncio.run(collect())
    assert len(responses) == 1
    assert responses[0].content.parts[0].text == '{"verdict":"PASS"}'
    assert [item[0] for item in gateway.calls] == [
        "independent_verification",
        "independent_verification_compact_retry",
    ]
    assert "truncated or invalid JSON" in gateway.calls[1][1][-1].parts[0].text


def test_verifier_compact_retry_requires_criterion_receipts_without_invented_ids() -> None:
    class Gateway:
        def __init__(self):
            self.calls = []

        def generate_adk_response(self, *, stage, contents, **_kwargs):
            self.calls.append((stage, contents))
            text = (
                '{"verdict":"PASS"'
                if len(self.calls) == 1
                else json.dumps({
                    "verdict": "PASS",
                    "criterion_checks": [{
                        "criterion_id": "Q01",
                        "criterion": "The requested artifact is complete.",
                        "passed": True,
                        "evidence": "The artifact contains the requested section.",
                        "evidence_bindings": [],
                    }],
                    "blocking_issues": [],
                    "revision_instructions": [],
                    "missing_information": [],
                    "temperament_decisions": [],
                })
            )
            return types.GenerateContentResponse(candidates=[types.Candidate(
                finish_reason=types.FinishReason.STOP,
                content=types.Content(role="model", parts=[types.Part(text=text)]),
            )])

    async def collect():
        gateway = Gateway()
        model = BudgetedAdkLlm(
            model="gemini-3.1-pro-preview",
            gateway=gateway,
            stage="independent_verification",
            response_model=VerificationReport,
        )
        request = LlmRequest(
            model="gemini-3.1-pro-preview",
            contents=[types.Content(role="user", parts=[types.Part(text="verify Q01")])],
            config=types.GenerateContentConfig(
                max_output_tokens=2200,
                response_mime_type="application/json",
            ),
        )
        responses = [item async for item in model.generate_content_async(request)]
        return gateway, responses

    gateway, responses = asyncio.run(collect())

    retry_text = gateway.calls[1][1][-1].parts[0].text
    assert "exactly one criterion_check for every Q-prefixed" in retry_text
    assert "set evidence_bindings to an empty list" in retry_text
    VerificationReport.model_validate_json(responses[0].content.parts[0].text)


def test_adk_llm_retries_valid_json_that_fails_active_pydantic_schema() -> None:
    from onebrief.generic_development_toolpack import (
        CompactProposedProjectCodeChangeSet,
    )

    class Gateway:
        def __init__(self):
            self.calls = []

        def generate_adk_response(self, *, stage, contents, **_kwargs):
            self.calls.append((stage, contents))
            payload = (
                {
                    "summary": "Malformed selector.",
                    "changes": [{
                        "path": "Assets/UI/Lobby.cs",
                        "reason": "Repair the product surface.",
                    }],
                }
                if len(self.calls) == 1
                else {
                    "summary": "Bounded new product helper.",
                    "changes": [{
                        "path": "Assets/UI/LobbyLanguageBinder.cs",
                        "content": "public sealed class LobbyLanguageBinder {}",
                        "reason": "Repair the product surface.",
                    }],
                }
            )
            return types.GenerateContentResponse(candidates=[types.Candidate(
                finish_reason=types.FinishReason.STOP,
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
            )])

    async def collect():
        gateway = Gateway()
        model = BudgetedAdkLlm(
            model="gemini-3.5-flash",
            gateway=gateway,
            stage="product_implementation::repair::long_form_draft",
            response_model=CompactProposedProjectCodeChangeSet,
        )
        request = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="repair")])],
            config=types.GenerateContentConfig(
                max_output_tokens=10_000,
                response_mime_type="application/json",
            ),
        )
        responses = [item async for item in model.generate_content_async(request)]
        return gateway, responses

    gateway, responses = asyncio.run(collect())

    assert len(responses) == 1
    assert [call[0] for call in gateway.calls] == [
        "product_implementation::repair::long_form_draft",
        "product_implementation::repair::long_form_draft_compact_retry",
    ]
    CompactProposedProjectCodeChangeSet.model_validate_json(
        responses[0].content.parts[0].text
    )


def test_adk_llm_allows_one_bounded_schema_repair_before_returning() -> None:
    from onebrief.generic_development_toolpack import (
        CompactProposedProjectCodeChangeSet,
    )

    class Gateway:
        def __init__(self):
            self.calls = []

        def generate_adk_response(self, *, stage, contents, **_kwargs):
            self.calls.append((stage, contents))
            change = {
                "path": "Assets/UI/Lobby.cs",
                "reason": "Repair the product surface.",
            }
            if len(self.calls) == 2:
                change.update({"search": "old", "replace": "new"})
            payload = {"summary": "Bounded repair.", "changes": [change]}
            return types.GenerateContentResponse(candidates=[types.Candidate(
                finish_reason=types.FinishReason.STOP,
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
            )])

    async def collect():
        gateway = Gateway()
        model = BudgetedAdkLlm(
            model="gemini-3.5-flash",
            gateway=gateway,
            stage="product_implementation::repair::long_form_draft",
            response_model=CompactProposedProjectCodeChangeSet,
        )
        request = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="repair")])],
            config=types.GenerateContentConfig(
                max_output_tokens=10_000,
                response_mime_type="application/json",
            ),
        )
        responses = [item async for item in model.generate_content_async(request)]
        return gateway, responses

    gateway, responses = asyncio.run(collect())

    assert [call[0] for call in gateway.calls] == [
        "product_implementation::repair::long_form_draft",
        "product_implementation::repair::long_form_draft_compact_retry",
    ]
    retry_text = gateway.calls[-1][1][-1].parts[0].text
    assert "Never return path and reason alone" in retry_text
    CompactProposedProjectCodeChangeSet.model_validate_json(
        responses[0].content.parts[0].text
    )


def test_adk_loop_returns_persisted_schema_failure_to_same_maker() -> None:
    class InvalidThenValidMaker(FakeMaker):
        @override
        async def _run_async_impl(
            self, ctx: InvocationContext
        ) -> AsyncGenerator[Event, None]:
            self.calls += 1
            if self.calls == 1:
                raise InvalidStructuredMakerOutput(
                    "schema validation failed: changes.0 requires one edit mechanism",
                    '{"changes":[{"path":"Assets/UI/Lobby.cs","replace":"```json{"}]}',
                )
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                branch=ctx.branch,
                actions=EventActions(state_delta={MAKER_STATE_KEY: {
                    "version": self.calls,
                    "received_feedback": bool(
                        ctx.session.state.get(VERIFICATION_STATE_KEY)
                    ),
                }}),
            )

    maker = InvalidThenValidMaker(name="maker")
    verifier = FakeVerifier(name="verifier", calls=1)
    agent = AdkConvergenceAgent(
        name="convergence", sub_agents=[maker, verifier], max_revision_rounds=2,
    )

    state, _trace = asyncio.run(
        run_convergence_agent(agent, {"goal": "Complete the work."})
    )

    assert maker.calls == 2
    assert verifier.calls == 2
    assert state[MAKER_STATE_KEY] == {"version": 2, "received_feedback": True}
    assert state[VERIFICATION_STATE_KEY]["verdict"] == Verdict.PASS.value
    assert state[MAKER_SCHEMA_FAILURE_STATE_KEY]["raw_text"].endswith("```json{\"}]}")


def test_adk_llm_rejects_malformed_compact_retry_before_adk_validation() -> None:
    from onebrief.generic_development_toolpack import (
        CompactProposedProjectCodeChangeSet,
    )

    malformed = {
        "summary": "Malformed selector.",
        "changes": [{
            "path": "Assets/UI/Lobby.cs",
            "replace": "```json{",
            "reason": "Repair the product surface.",
        }],
    }

    class Gateway:
        def __init__(self):
            self.calls = []

        def generate_adk_response(self, *, stage, **_kwargs):
            self.calls.append(stage)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                finish_reason=types.FinishReason.STOP,
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(malformed))]
                ),
            )])

    async def collect():
        gateway = Gateway()
        model = BudgetedAdkLlm(
            model="gemini-3.5-flash",
            gateway=gateway,
            stage="product_implementation::repair::long_form_draft",
            response_model=CompactProposedProjectCodeChangeSet,
        )
        request = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="repair")])],
            config=types.GenerateContentConfig(
                max_output_tokens=10_000,
                response_mime_type="application/json",
            ),
        )
        try:
            return gateway, [item async for item in model.generate_content_async(request)]
        except InvalidStructuredMakerOutput as exc:
            return gateway, exc

    gateway, result = asyncio.run(collect())

    assert isinstance(result, InvalidStructuredMakerOutput)
    assert "changes.0" in result.detail
    assert result.raw_text == json.dumps(malformed)
    assert gateway.calls == [
        "product_implementation::repair::long_form_draft",
        "product_implementation::repair::long_form_draft_compact_retry",
    ]


def test_adk_llm_compact_retry_receives_a_larger_structured_output_envelope() -> None:
    class Gateway:
        def __init__(self):
            self.output_caps = []

        def generate_adk_response(self, *, config, **_kwargs):
            self.output_caps.append(config.max_output_tokens)
            text = '{"value":"cut"' if len(self.output_caps) == 1 else '{"value":"complete"}'
            return types.GenerateContentResponse(
                candidates=[types.Candidate(
                    finish_reason=types.FinishReason.STOP,
                    content=types.Content(role="model", parts=[types.Part(text=text)]),
                )]
            )

    async def collect():
        gateway = Gateway()
        model = BudgetedAdkLlm(
            model="gemini-3.5-flash", gateway=gateway, stage="long_form_draft"
        )
        request = LlmRequest(
            model="gemini-3.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text="repair")])],
            config=types.GenerateContentConfig(
                max_output_tokens=3_000,
                response_mime_type="application/json",
            ),
        )
        responses = [item async for item in model.generate_content_async(request)]
        return gateway, responses

    gateway, responses = asyncio.run(collect())

    assert gateway.output_caps == [3_000, 6_000]
    assert responses[0].content.parts[0].text == '{"value":"complete"}'


def test_adk_verifier_compact_retries_when_required_criterion_ids_are_omitted() -> None:
    generic = VerificationReport(
        verdict=Verdict.PASS,
        criterion_checks=[CriterionCheck(
            criterion="Generic review", passed=True, evidence="Looks complete."
        )],
        blocking_issues=[], revision_instructions=[], missing_information=[],
    )
    bound = generic.model_copy(update={
        "criterion_checks": [CriterionCheck(
            criterion_id="Q01", criterion="Required result",
            passed=True, evidence="Q01 was explicitly checked.",
        )],
    })

    class Gateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def generate_adk_response(self, **kwargs):
            self.calls.append(str(kwargs["stage"]))
            payload = generic if len(self.calls) == 1 else bound
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=payload.model_dump_json())],
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    async def collect():
        gateway = Gateway()
        model = BudgetedAdkLlm(
            model="verifier-model", gateway=gateway,
            stage="independent_verification",
            response_model=VerificationReport,
            required_criterion_ids=("Q01",),
        )
        request = LlmRequest(
            model="verifier-model",
            contents=[types.Content(role="user", parts=[types.Part(text="verify")])],
            config=types.GenerateContentConfig(
                response_mime_type="application/json", max_output_tokens=2200,
            ),
        )
        responses = [item async for item in model.generate_content_async(request)]
        return gateway, responses

    gateway, responses = asyncio.run(collect())

    parsed = VerificationReport.model_validate_json(responses[0].content.parts[0].text)
    assert gateway.calls == [
        "independent_verification", "independent_verification_compact_retry"
    ]
    assert parsed.criterion_checks[0].criterion_id == "Q01"


def test_contextual_maker_receives_current_exact_edit_anchors() -> None:
    agent = build_text_convergence_agent(
        gateway=object(),
        maker_model="gemini-3.5-flash",
        verifier_model="gemini-3.5-flash",
        maker_schema=dict,
        max_revision_rounds=1,
        maker_instruction="repair",
        verifier_instruction="verify",
    )
    instruction = agent.maker.instruction

    class Context:
        state = {
            MAKER_STATE_KEY: {"changes": []},
            VERIFICATION_STATE_KEY: {"verdict": "REVISE"},
            REPAIR_PLAN_STATE_KEY: {"tasks": [{"criterion_id": "Q02"}]},
            REPAIR_CONTRACT_STATE_KEY: {
                "hypothesis": {"cheapest_probe": "compile only"},
                "verification_ladder": ["compile", "targeted_test"],
            },
            EXACT_EDIT_ANCHORS_STATE_KEY: [{"path": "web/app/page.tsx"}],
            MAKER_DIAGNOSTIC_CONTEXT_STATE_KEY: [{
                "path": "src/auth/session.ts",
                "source_role": "read_only_diagnostic_context",
                "content_excerpt": "export function createTestSession() {}",
            }],
        }

    rendered = instruction(Context())
    assert "onebrief_exact_edit_anchors" not in rendered
    assert "web/app/page.tsx" in rendered
    assert '"criterion_id": "Q02"' in rendered
    assert '"cheapest_probe": "compile only"' in rendered
    assert '"diagnostic_repository_context"' in rendered
    assert "createTestSession" in rendered


def test_deterministic_gate_overrules_model_pass_and_forces_original_maker_retry() -> None:
    maker = FakeMaker(name="maker")
    verifier = FakeVerifier(name="verifier", calls=1)  # model says PASS immediately

    def gate(
        report: VerificationReport, _ctx: InvocationContext, round_number: int
    ) -> VerificationReport:
        if round_number:
            return report
        return VerificationReport(
            verdict=Verdict.REVISE,
            criterion_checks=[*report.criterion_checks, CriterionCheck(
                criterion="Deterministic gate", passed=False,
                evidence="Runtime evidence failed.",
            )],
            blocking_issues=["Runtime evidence failed."],
            revision_instructions=["Repair the runtime failure."],
            missing_information=[],
        )

    agent = AdkConvergenceAgent(
        name="convergence", sub_agents=[maker, verifier], max_revision_rounds=2,
        verification_gate=gate,
    )
    state, _trace = asyncio.run(
        run_convergence_agent(agent, {"goal": "Complete the work."})
    )

    assert maker.calls == 2
    assert state[MAKER_STATE_KEY]["received_feedback"] is True
    assert state[VERIFICATION_STATE_KEY]["verdict"] == Verdict.PASS.value


def test_needs_information_stops_without_wasting_revision() -> None:
    class NeedsInformationVerifier(FakeVerifier):
        @override
        async def _run_async_impl(
            self, ctx: InvocationContext
        ) -> AsyncGenerator[Event, None]:
            self.calls += 1
            report = VerificationReport(
                verdict=Verdict.NEEDS_INFORMATION,
                criterion_checks=[CriterionCheck(
                    criterion="Authority", passed=False,
                    evidence="An authoritative choice is absent.",
                )],
                blocking_issues=["An authoritative choice is absent."],
                revision_instructions=[],
                missing_information=["Provide the authoritative choice."],
            )
            yield Event(
                author=self.name, invocation_id=ctx.invocation_id, branch=ctx.branch,
                actions=EventActions(state_delta={
                    VERIFICATION_STATE_KEY: report.model_dump(mode="json")
                }),
            )

    maker = FakeMaker(name="maker")
    verifier = NeedsInformationVerifier(name="verifier")
    agent = AdkConvergenceAgent(
        name="convergence", sub_agents=[maker, verifier], max_revision_rounds=4,
    )
    state, _trace = asyncio.run(
        run_convergence_agent(agent, {"goal": "Complete the work."})
    )

    assert maker.calls == 1
    assert verifier.calls == 1
    assert state[VERIFICATION_STATE_KEY]["verdict"] == Verdict.NEEDS_INFORMATION.value


def test_real_adk_llm_agents_exchange_structured_revision_state() -> None:
    class StubBudgetedGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.stages: list[str] = []
            self.outputs = [
                DraftArtifact(
                    title="First draft",
                    body_markdown="This first artifact is long enough to validate but still needs repair.",
                    cited_finding_ids=["F01"], drafting_decisions=[],
                ).model_dump(mode="json"),
                _report(Verdict.REVISE),
                DraftArtifact(
                    title="Revised draft",
                    body_markdown="This revised artifact addresses the verifier feedback and is complete.",
                    cited_finding_ids=["F01"], drafting_decisions=["Applied verifier feedback."],
                ).model_dump(mode="json"),
                _report(Verdict.PASS),
            ]

        def generate_adk_response(self, **kwargs: Any) -> types.GenerateContentResponse:
            self.stages.append(str(kwargs["stage"]))
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    gateway = StubBudgetedGateway()
    agent = build_text_convergence_agent(
        gateway=gateway,
        maker_model="maker-model",
        verifier_model="verifier-model",
        maker_schema=DraftArtifact,
        max_revision_rounds=2,
        maker_instruction="Create the artifact.",
        verifier_instruction="Verify the artifact.",
    )
    state, trace = asyncio.run(run_convergence_agent(agent, {"goal": "Finish it."}))

    assert state[MAKER_STATE_KEY]["title"] == "Revised draft"
    assert state[VERIFICATION_STATE_KEY]["verdict"] == Verdict.PASS.value
    assert gateway.stages == [
        "long_form_draft", "independent_verification",
        "long_form_draft", "independent_verification",
    ]
    assert [event["author"] for event in trace].count("onebrief_maker") == 2


def test_maker_revisions_use_current_state_projection_without_conversation_replay() -> None:
    class InspectingGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[types.Content], str]] = []
            self.outputs = [
                DraftArtifact(
                    title="First draft",
                    body_markdown="A first bounded artifact that still needs one precise repair.",
                    cited_finding_ids=["F01"],
                    drafting_decisions=[],
                ).model_dump(mode="json"),
                _report(Verdict.REVISE),
                DraftArtifact(
                    title="Revised draft",
                    body_markdown="The bounded artifact now contains the precise requested repair.",
                    cited_finding_ids=["F01"],
                    drafting_decisions=["Applied the verification feedback."],
                ).model_dump(mode="json"),
                _report(Verdict.PASS),
            ]

        def generate_adk_response(self, **kwargs: Any) -> types.GenerateContentResponse:
            self.calls.append((
                str(kwargs["stage"]),
                list(kwargs["contents"]),
                str(getattr(kwargs["config"], "system_instruction", "")),
            ))
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    gateway = InspectingGateway()
    agent = build_text_convergence_agent(
        gateway=gateway,
        maker_model="maker-model",
        verifier_model="verifier-model",
        maker_schema=DraftArtifact,
        max_revision_rounds=1,
        maker_instruction="Create the artifact.",
        verifier_instruction="Verify the artifact.",
    )
    asyncio.run(run_convergence_agent(agent, {
        "work_contract": {"goal": "Finish it."},
        "analysis_package": {"findings": [{"finding_id": "F01"}]},
        "authoritative_sources": [{"name": "source", "content": "authority"}],
    }))

    maker_calls = [item for item in gateway.calls if item[0] == "long_form_draft"]
    assert len(maker_calls) == 2
    for _stage, contents, _instruction in maker_calls:
        assert all(content.role != "model" for content in contents)
        assert "First draft" not in json.dumps(
            [content.model_dump(mode="json") for content in contents]
        )
    first_instruction = maker_calls[0][2]
    second_instruction = maker_calls[1][2]
    assert '"work_contract": {"goal": "Finish it."}' in first_instruction
    assert '"authoritative_sources": [{"name": "source", "content": "authority"}]' in first_instruction
    assert '"previous_artifact": null' in first_instruction
    assert '"previous_artifact": {"title": "First draft"' in second_instruction
    assert '"verification_feedback": {"verdict": "REVISE"' in second_instruction


def test_independent_verifier_receives_state_projection_not_repository_conversation() -> None:
    class InspectingGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[types.Content], str]] = []
            self.outputs = [
                DraftArtifact(
                    title="Bounded result",
                    body_markdown="A bounded artifact with enough detail for independent verification.",
                    cited_finding_ids=["F01"],
                    drafting_decisions=[],
                ).model_dump(mode="json"),
                _report(Verdict.PASS),
            ]

        def generate_adk_response(self, **kwargs: Any) -> types.GenerateContentResponse:
            contents = list(kwargs["contents"])
            instruction = str(getattr(kwargs["config"], "system_instruction", ""))
            self.calls.append((str(kwargs["stage"]), contents, instruction))
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    gateway = InspectingGateway()
    agent = build_text_convergence_agent(
        gateway=gateway,
        maker_model="maker-model",
        verifier_model="verifier-model",
        maker_schema=DraftArtifact,
        max_revision_rounds=0,
        maker_instruction="Create the artifact.",
        verifier_instruction="Verify the artifact.",
    )
    huge_repository_payload = "repository-context-should-not-reach-verifier-" * 2000

    asyncio.run(run_convergence_agent(agent, {
        "goal": "Finish it.",
        "work_contract": {
            "completion_contract": {
                "quality_criteria": [{"criterion_id": "Q01"}]
            }
        },
        "analysis_package": {"findings": [{"finding_id": "F01"}]},
        "approved_repository_files": huge_repository_payload,
    }, initial_state={
        VERIFIER_CONTEXT_STATE_KEY: {"commands": [{"command_id": "compile", "exit_code": 0}]}
    }))

    maker_call, verifier_call = gateway.calls
    assert huge_repository_payload in str(maker_call[1])
    assert huge_repository_payload not in str(verifier_call[1])
    assert huge_repository_payload not in verifier_call[2]
    assert "VERIFICATION PAYLOAD" in verifier_call[2]
    assert '"criterion_id": "Q01"' in verifier_call[2]
    assert '"finding_id": "F01"' in verifier_call[2]
    assert "Bounded result" in verifier_call[2]
    assert "compile" in verifier_call[2]


def test_same_adk_maker_changes_model_rung_after_verified_failure() -> None:
    class StubBudgetedGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.outputs = [
                DraftArtifact(
                    title="Initial",
                    body_markdown="The initial artifact is long enough but needs a repair.",
                    cited_finding_ids=["F01"],
                    drafting_decisions=[],
                ).model_dump(mode="json"),
                _report(Verdict.REVISE),
                DraftArtifact(
                    title="Repaired",
                    body_markdown="The repaired artifact preserves context and satisfies the verifier.",
                    cited_finding_ids=["F01"],
                    drafting_decisions=["Used the verified failure."],
                ).model_dump(mode="json"),
                _report(Verdict.PASS),
            ]

        def generate_adk_response(self, **kwargs: Any) -> types.GenerateContentResponse:
            self.calls.append((str(kwargs["stage"]), str(kwargs["model"])))
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    def select_model(report, _ctx, round_number):
        if report is None:
            return None
        return {
            "model": "pro-maker-model",
            "stage": f"long_form_draft_reasoning_escalation_r{round_number}",
            "round_number": round_number,
        }

    gateway = StubBudgetedGateway()
    agent = build_text_convergence_agent(
        gateway=gateway,
        maker_model="flash-maker-model",
        verifier_model="verifier-model",
        maker_schema=DraftArtifact,
        max_revision_rounds=2,
        maker_instruction="Create the artifact.",
        verifier_instruction="Verify the artifact.",
        maker_model_selector=select_model,
    )

    state, trace = asyncio.run(run_convergence_agent(agent, {"goal": "Finish it."}))

    assert gateway.calls == [
        ("long_form_draft", "flash-maker-model"),
        ("independent_verification", "verifier-model"),
        ("long_form_draft_reasoning_escalation_r1", "pro-maker-model"),
        ("independent_verification", "verifier-model"),
    ]
    assert state[MAKER_MODEL_BINDING_STATE_KEY]["model"] == "pro-maker-model"
    assert [event["author"] for event in trace].count("onebrief_maker") == 2


def test_dynamic_maker_binding_persists_execution_phase_before_repair() -> None:
    observed_phases: list[str | None] = []
    observed_after_maker_phases: list[str | None] = []

    class PhaseAwareMaker(FakeMaker):
        model: Any

        @override
        async def _run_async_impl(
            self, ctx: InvocationContext
        ) -> AsyncGenerator[Event, None]:
            observed_phases.append(ctx.session.state.get("onebrief:execution_phase"))
            async for event in super()._run_async_impl(ctx):
                yield event

    maker = PhaseAwareMaker(
        name="maker",
        model=BudgetedAdkLlm(
            model="flash-maker-model",
            gateway=object(),
            stage="evidence_construction::repair::long_form_draft",
        ),
    )
    verifier = FakeVerifier(name="verifier")

    def select_model(report, _ctx, round_number):
        if report is None:
            return None
        return {
            "model": "pro-maker-model",
            "stage": f"product_implementation::repair::long_form_draft_r{round_number}",
            "round_number": round_number,
            "execution_phase": "product_implementation",
            "phase_decision": {"failure_owner": "product"},
        }

    def observe_after_maker(_raw, ctx, _round_number):
        observed_after_maker_phases.append(
            ctx.session.state.get("onebrief:execution_phase")
        )
        return None

    agent = AdkConvergenceAgent(
        name="convergence",
        sub_agents=[maker, verifier],
        max_revision_rounds=2,
        maker_model_selector=select_model,
        after_maker=observe_after_maker,
    )

    state, _trace = asyncio.run(run_convergence_agent(
        agent,
        {"goal": "Repair the shipped product, not its proof."},
        initial_state={"onebrief:execution_phase": "evidence_construction"},
    ))

    assert observed_phases == ["evidence_construction", "product_implementation"]
    assert observed_after_maker_phases == [
        "evidence_construction",
        "product_implementation",
    ]
    assert state["onebrief:execution_phase"] == "product_implementation"
    assert state["onebrief:phase_decision"] == {"failure_owner": "product"}


def test_same_adk_maker_keeps_escalated_rung_until_boundary_passes() -> None:
    class StubBudgetedGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.outputs = [
                DraftArtifact(title="Initial", body_markdown="Initial artifact requiring repair and preserving enough verified context for review.", cited_finding_ids=["F01"], drafting_decisions=[]).model_dump(mode="json"),
                _report(Verdict.REVISE),
                DraftArtifact(title="Pro repair", body_markdown="First reasoned repair preserving the verified context while addressing the active boundary.", cited_finding_ids=["F01"], drafting_decisions=[]).model_dump(mode="json"),
                _report(Verdict.REVISE),
                DraftArtifact(title="Continued", body_markdown="Second reasoned repair remains on the escalated rung while independent verification continues.", cited_finding_ids=["F01"], drafting_decisions=[]).model_dump(mode="json"),
                _report(Verdict.REVISE),
                DraftArtifact(title="Final", body_markdown="Third reasoned repair remains on the escalated rung until independent verification passes.", cited_finding_ids=["F01"], drafting_decisions=[]).model_dump(mode="json"),
                _report(Verdict.PASS),
            ]

        def generate_adk_response(self, **kwargs: Any) -> types.GenerateContentResponse:
            self.calls.append((str(kwargs["stage"]), str(kwargs["model"])))
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    def select_model(report, _ctx, round_number):
        if report is None:
            return None
        escalated = round_number == 1
        return {
            "model": "pro-maker-model" if escalated else "flash-maker-model",
            "stage": f"long_form_draft_r{round_number}",
            "round_number": round_number,
            "decision": {"escalated": escalated},
        }

    gateway = StubBudgetedGateway()
    agent = build_text_convergence_agent(
        gateway=gateway,
        maker_model="flash-maker-model",
        verifier_model="verifier-model",
        maker_schema=DraftArtifact,
        max_revision_rounds=3,
        maker_instruction="Create the artifact.",
        verifier_instruction="Verify the artifact.",
        maker_model_selector=select_model,
    )

    state, _trace = asyncio.run(run_convergence_agent(agent, {"goal": "Finish it."}))

    assert gateway.calls[4][1] == "pro-maker-model"
    assert gateway.calls[6][1] == "pro-maker-model"
    assert state[MAKER_MODEL_BINDING_STATE_KEY]["sticky_escalation"] is True


def test_same_adk_maker_switches_to_an_atomic_output_schema_after_failure() -> None:
    from onebrief.generic_development_toolpack import (
        AtomicUnityEvidenceBundle,
        ProposedProjectCodeChangeSet,
    )

    class StubGateway(BudgetedGeminiClient):
        def __init__(self) -> None:
            self.schemas: list[str] = []
            self.outputs = [
                ProposedProjectCodeChangeSet.model_validate({
                    "summary": "Initial product change.",
                    "changes": [{
                        "path": "Assets/UI/Settings.cs",
                        "base_sha256": "a" * 64,
                        "search": "old",
                        "replace": "new",
                        "reason": "Modernize the product UI.",
                    }],
                }).model_dump(mode="json"),
                _report(Verdict.REVISE),
                AtomicUnityEvidenceBundle.model_validate({
                    "summary": "Atomic test harness.",
                    "playmode_test": {
                        "path": "Assets/Tests/PlayMode/Flow.cs",
                        "content": "namespace OneBrief.Visual { public class Flow {} }",
                        "reason": "Prove the runtime flow.",
                    },
                    "test_assembly": {
                        "path": "Assets/Tests/PlayMode/Flow.asmdef",
                        "content": '{"optionalUnityReferences":["TestAssemblies"]}',
                        "reason": "Discover the runtime test.",
                    },
                }).model_dump(mode="json"),
                _report(Verdict.PASS),
            ]

        def generate_adk_response(self, **kwargs: Any) -> types.GenerateContentResponse:
            schema = getattr(kwargs["config"], "response_schema", None)
            self.schemas.append(getattr(schema, "__name__", str(schema)))
            payload = self.outputs.pop(0)
            return types.GenerateContentResponse(candidates=[types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part(text=json.dumps(payload))]
                ),
                finish_reason=types.FinishReason.STOP,
            )])

    def select_schema(report, _ctx, _round):
        return AtomicUnityEvidenceBundle if report is not None else None

    gateway = StubGateway()
    agent = build_text_convergence_agent(
        gateway=gateway,
        maker_model="maker-model",
        verifier_model="verifier-model",
        maker_schema=ProposedProjectCodeChangeSet,
        max_revision_rounds=2,
        maker_instruction="Create the product, then its proof.",
        verifier_instruction="Verify it.",
        maker_schema_selector=select_schema,
    )

    state, _trace = asyncio.run(run_convergence_agent(agent, {"goal": "Finish it."}))

    assert gateway.schemas == [
        "ProposedProjectCodeChangeSet",
        "VerificationReport",
        "AtomicUnityEvidenceBundle",
        "VerificationReport",
    ]
    assert state[MAKER_SCHEMA_BINDING_STATE_KEY] == "AtomicUnityEvidenceBundle"
