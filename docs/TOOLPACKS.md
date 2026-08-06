# Executable ToolPacks

ToolPacks let a goal-specific OneBrief team perform bounded deterministic actions instead of only generating text. A selected pack becomes an explicit `tool_execution` node owned by the maker. Its generated evidence is added to the analyst and critic context and included in the immutable result package.

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
