# OneBrief SixSense

## Purpose

SixSense turns a short, novice-friendly goal into an executable completion contract
without requiring the user to write a professional brief. It does not promise to read
unspoken taste. It finds a safe professional standard first, then asks only about the
few unresolved choices that materially change the result.

## Interaction contract

1. The user enters one goal and optionally attaches authoritative sources or an existing
   project.
2. One bounded Gemini inspection reads the goal, sources, and project state. In that same
   response it creates the standard profile and the complete question sequence.
3. The UI presents at most five questions, S02 through S06, one at a time. No model call
   occurs between questions.
4. Every question has two to four short choices and exactly one disclosed recommendation.
   The recommendation is selected by default. Choosing an option advances immediately.
5. The user may change only the decisions they care about, go back, enter a short custom
   answer, or accept all recommendations with one action.
6. One final reinspection merges the confirmed decisions into the canonical goal and
   completion contract. Only then does OneBrief calculate the execution budget and
   authorization package.

The target interaction time after the first inspection is 30 seconds. SixSense must not
become a chat interview. The server produces the whole sequence in one pass and the
browser performs question-to-question navigation locally.

## What may be asked

Questions are limited to choices that materially affect:

- scope and priority;
- intended audience or use context;
- required behavior or non-negotiable constraints;
- subjective direction where several defensible standards exist;
- observable completion level.

SixSense never asks the user to select an agent, model, ToolPack, framework, library,
retry count, or internal execution mechanism. Public facts that can be researched later
are not user questions. Safe ordinary implementation defaults remain internal.

## Default and authority rules

- Uploaded internal information and the selected existing project outrank public norms.
- Explicit user requirements outrank the recommended default.
- Unspecified details use the disclosed professional standard profile.
- A recommendation is not treated as an undisclosed fact or private policy.
- Missing authoritative information that cannot be inferred remains a blocking
  requirement even after SixSense. The product asks for it once rather than inventing it.
- Confirmed choices become part of the canonical goal and are preserved for later
  existing-project improvements.

## Success measures

- one Gemini call to prepare the complete question sequence;
- zero network waits between questions;
- no more than five questions after the goal;
- a one-click path that accepts every recommendation;
- one consolidated reinspection before budget calculation;
- no repeated preference interview in the same stage-one lineage.
