# Executable ToolPacks

ToolPacks let a goal-specific OneBrief team perform bounded deterministic actions instead of only generating text. A selected pack becomes an explicit `tool_execution` node owned by the maker. Its generated evidence is added to the analyst and critic context and included in the immutable result package.

## Composition model

`ToolPack` is the user-facing name for an approved composition, not one monolithic
project script.

- A **project profile** binds project identity, exact Git revision, read/write prefixes,
  editable suffixes, and project-specific boundaries. A Julpae profile is therefore the
  Julpae-only part of the composition.
- A **capability pack** supplies reusable deterministic control and evidence contracts.
  Current built-ins cover isolated Git control, Unity compile/test control, Unity layout
  diagnostics, Node scripts, local web observation, and Python tests.
- **Knowledge and skills** guide model reasoning but do not grant execution authority.

Each capability pack has a semantic version and canonical digest. The project-profile
approval binds the exact component versions and digests together with the source revision.
Changing a common Unity capability therefore invalidates the old composition approval,
while merely attaching that capability to another project never grants access to either
project.

This separation lets OneBrief reuse verified Unity expertise across projects without
copying Julpae-specific authority, paths, or memory.

## Unity layout diagnostics

The `unity-layout-diagnostics` capability is a fixed read-only observer for visual/UI
work. OneBrief injects its Editor script only into the disposable clone, opens bounded
project scenes in Unity batch mode, and records CanvasScaler and RectTransform hierarchy
facts such as anchors, pivots, sizes, positions, components, and structural risk flags.

The raw hierarchy is bounded and the packaged summary is bound to the exact source
revision and candidate digest. It never edits scenes, prefabs, tests, screenshots, or
product UI. When visual verification fails, the maker receives the smallest relevant
hierarchy evidence so the next repair can target one diagnosed ancestry instead of making
another global scaler guess.

### Verified Julpae diagnostic baseline

On 2026-08-13 the fixed observer compiled and exited with code 0 against an isolated
Julpae candidate clone. The evidence was bound to source revision
`cb3ed328ab5af9bc883b86cb6d660691f5d169be` and candidate digest
`579328c6d60dddf5d317f94f3944f73b02e3f5a9f90668a88c6daad87f4ae22e`.
It observed 2 scenes, 1 Canvas, and the bounded maximum of 800 RectTransforms. The report
identified StorePopup content-height and child-boundary risks plus several abnormal stretch
deltas. The source repository remained unchanged. After capability-pack composition and
diagnostic integration, the complete local regression suite passed 489 tests with no
failures.

## Exchange V1

Exchange is the first executable ToolPack. It is read-only and has no brokerage, account, credential, or order interface.

Allowed commands:

- `npm test`
- `npm run verify:transfer`

Allowed evidence is a fixed six-file set containing project state, data freshness, data quality, BOK/ECB cross-checks, and bounded model-performance results. Every packaged file is hashed. Resume verifies those hashes and reuses the package without rerunning commands.

The requirements preflight receives only a small capability descriptor. The producer reserves 60,000 input tokens for the evidence generated after approval so the hard budget remains conservative.

Local configuration:

```text
ONEBRIEF_EXCHANGE_ROOT=C:\exchange
```

Linux and Cloud Run default to `/opt/onebrief/toolpacks/exchange`. If that immutable pack is not installed, execution fails closed. It never falls back to invented evidence or public search.

## Safety boundary

- ToolPack IDs are an enum, not user-supplied command names.
- The repository root is operator configuration, not a form field.
- Commands use argument arrays with `shell=False`.
- Only a small environment allowlist reaches the child process.
- Symlinked or escaping evidence paths are rejected.
- Automatic trading, personalized buy/sell instructions, and profit guarantees remain outside the product boundary.
