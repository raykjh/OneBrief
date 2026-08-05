# Intake Preparation Workflow

## Gate sequence

1. `analyze` turns the goal into a requirements contract.
2. The user supplies every mandatory item through an upload manifest.
3. OneBrief hashes and records each source, then sends its actual text for reinspection.
4. Gemini resolves a gap only when the uploaded content is semantically sufficient.
5. The deterministic Producer runs only when `ready_for_estimate` is `true`.
6. The user approves the recommended amount before any long-form execution begins.

Merely matching a filename or `requirement_key` never passes the gate. Empty,
unrelated, incomplete, or contradictory material remains `NEEDS_INFORMATION`.

## Outputs

- `source_manifest.json`: source name, requirement mapping, byte size, media type, SHA-256
- `requirements_reinspection.json`: revised contract and remaining questions
- `budget_estimate.json`: created only after the requirements gate passes

Source contents are not copied into the output directory.

## Producer estimate

The estimate includes evidence analysis, drafting, standards review, independent
verification, and up to two revision rounds. It reports minimum, recommended, and
maximum token/cost/time bounds. The recommended and maximum estimates include 20%
and 25% contingency.

The current price card is versioned `2026-08-05` and uses the Google global Standard
rates for Gemini 3.5 Flash and Gemini 3.5 Flash-Lite. It must be refreshed when Google
changes pricing. Intake and reinspection calls already completed are intentionally
excluded from the future-work approval amount.

## Example

```powershell
onebrief prepare `
  samples\intake_missing_information.json `
  output\requirements_analysis_ko.json `
  samples\hiring_uploads.json `
  --output-dir output\prepared_hiring
```

