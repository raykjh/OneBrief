# Guarded Multi-Agent Execution

## Call graph

```text
Approved budget
  -> Analyst
  -> Writer
  -> Independent Verifier
  -> Deterministic Grounding Gate (authoritative CSV + scoring rules)
       -> PASS -> final package
       -> NEEDS_INFORMATION -> user checkpoint
       -> REVISE -> Revision Agent -> Independent Verifier
                                      (maximum two rounds)
```

All four role implementations receive only a `BudgetedGeminiClient`. They cannot call
the Google model client directly. Every structured call therefore performs input token
counting, worst-case reservation, provider generation, and actual-usage settlement.

## Role boundaries

- Analyst extracts traceable `F01`-style findings and never drafts.
- Writer creates the artifact from the contract, findings, and authoritative source
  payload but cannot approve it.
- Verifier is independent, checks every acceptance criterion, and returns `PASS`,
  `REVISE`, or `NEEDS_INFORMATION`.
- Revision Agent receives the authoritative source payload, applies only the verifier's
  blocking instructions, and always returns to verification before completion.

## Deterministic grounding gate

The model verifier cannot overrule this gate. For every authoritative CSV, the gate
locates every first-column record identifier in the result table and compares each
subsequent source field with the corresponding result cell. A missing row or changed
value forces `REVISE`.

The gate also extracts threshold and category-to-score mappings. If a generated scoring
conversion cannot be matched to one authoritative source line, the result becomes
`NEEDS_INFORMATION`; the system asks for an approved conversion table or formula rather
than allowing an agent to invent one.

Each handoff is a Pydantic schema rather than chat prose. Every stage and revision is
written as a checkpoint so a crash or budget block leaves inspectable state.

## Outputs

- `analysis.json`
- `draft_r0.json`
- `model_verification_r0.json`
- `deterministic_verification_r0.json`
- `verification_r0.json`
- optional `revision_rN.json` and `verification_rN.json`
- `final.md`
- `final_verification.json`
- `execution_checkpoint.json`

## Commands

Recalculate the revised four-agent budget without another Gemini call:

```powershell
onebrief estimate INPUT REQUIREMENTS UPLOAD_MANIFEST --output BUDGET_JSON
```

After explicitly approving that estimate:

```powershell
onebrief execute INPUT REQUIREMENTS UPLOAD_MANIFEST `
  --run-dir RUN_DIRECTORY `
  --output-dir OUTPUT_DIRECTORY
```

