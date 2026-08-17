"""Product-wide delivery defaults that protect first-pass convergence."""

from __future__ import annotations

import re

from onebrief.schemas import IntakeRequest, OutputTarget, RequirementsAnalysis, ToolPackId


_EXPLICIT_DESIGN_DIRECTION = re.compile(
    r"독창|고유한\s*(?:디자인|시각)|새로운\s*(?:디자인|시각)|디자인\s*(?:개선|변경|개편)|"
    r"original\s+design|unique\s+(?:design|visual)|distinctive\s+(?:design|visual)|"
    r"redesign|visual\s+direction|design\s+system",
    re.IGNORECASE,
)


def apply_standard_first_delivery_policy(
    intake: IntakeRequest, requirements: RequirementsAnalysis
) -> RequirementsAnalysis:
    """Default unspecified product visuals to a testable professional baseline.

    This is a delivery strategy, not a hidden taste decision. Explicit visual work
    remains supported, while ordinary first-pass product creation spends its loop
    budget on function, evidence, and usability before originality.
    """

    is_product = (
        intake.output_target in {
            OutputTarget.EXISTING_PROJECT,
            OutputTarget.WEB_APP,
            OutputTarget.UNITY_APP,
        }
        or any(
            item in intake.toolpack_ids
            for item in {
                ToolPackId.EXCHANGE_DEVELOPMENT,
                ToolPackId.PROJECT_DEVELOPMENT,
                ToolPackId.GREENFIELD_WEB_DEVELOPMENT,
            }
        )
    )
    request_text = "\n".join(
        filter(
            None,
            [
                intake.goal,
                intake.desired_output or "",
                *(
                    f"{source.summary}\n{source.content}"
                    for source in intake.internal_sources
                ),
            ],
        )
    )
    if not is_product or _EXPLICIT_DESIGN_DIRECTION.search(request_text):
        return requirements
    assumption = (
        "The first result uses an accessible conventional commercial design; "
        "distinctive visual redesign is deferred to a later improvement goal."
    )
    assumptions = list(dict.fromkeys([*requirements.assumptions, assumption]))[:10]
    sixsense = requirements.sixsense
    if sixsense is not None and assumption not in sixsense.standard_profile:
        sixsense = sixsense.model_copy(update={
            "standard_profile": f"{sixsense.standard_profile.rstrip()} {assumption}"[:800]
        })
    return requirements.model_copy(update={
        "assumptions": assumptions,
        "sixsense": sixsense,
    })
