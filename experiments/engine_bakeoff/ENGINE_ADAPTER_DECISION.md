# Engine Adapter Decision

Status: accepted for the PUZZLE TELOS test; the Unity adapter remains supported for
existing Unity repositories.

## Decision

Use Godot 4.7 as the primary greenfield game adapter for PUZZLE TELOS. Retain Unity as
the existing-project adapter for JULPAE and as a regression case. This is an adapter
choice, not a change to KHALINOS's product identity or Quest semantics.

## Evidence

Both engines received the exact same digest-bound five-region topology:

`title -> chamber_select -> gameplay -> pause -> mission_result`

The final clean measurement required each engine to materialize five native scenes,
traverse the full route in a real runtime, capture five PNG files, and export a Windows
desktop build.

| Engine | Final contract | Measured time | Total build size |
|---|---:|---:|---:|
| Unity 6000.3.11f1 | PASS | 64.425 s | 88,411,020 bytes |
| Godot 4.7.1 | PASS | 3.952 s | 109,086,024 bytes |

Godot loaded and traversed the generated text scenes on the first runtime attempt. Its
initial export failure was an external template/path preparation issue. Unity required
a capability-complete built-in module manifest and one generated C# escaping repair
before its native scenes and build could be produced. The repaired final generators
both pass, so this decision is based on failure surface and iteration speed rather than
claiming that Unity cannot be generated safely.

## Product consequence

- KHALINOS keeps one engine-neutral Outcome, Quest, Receipt, authority, budget, and
  structural-cause policy.
- `GodotTopologyPlan` is the first trusted greenfield topology compiler.
- The existing Unity adapter is not deleted or weakened.
- No paid model or cloud run is authorized until the Godot compiler passes an unrelated
  holdout, a real headless engine probe, and the KHALINOS regression suite.
- A second structural variant requires a generalization review. A third variant blocks
  another project-specific patch; a separately reviewed trusted-system repair may still
  proceed.
