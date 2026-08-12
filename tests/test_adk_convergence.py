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
    REVERIFY_EXISTING_STATE_KEY,
    EXACT_EDIT_ANCHORS_STATE_KEY,
    REPAIR_CONTRACT_STATE_KEY,
    REPAIR_PLAN_STATE_KEY,
    VERIFICATION_STATE_KEY,
    AdkConvergenceAgent,
    BudgetedAdkLlm,
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
        }

    rendered = instruction(Context())
    assert "onebrief_exact_edit_anchors" not in rendered
    assert "web/app/page.tsx" in rendered
    assert '"criterion_id": "Q02"' in rendered
    assert '"cheapest_probe": "compile only"' in rendered


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
