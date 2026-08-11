# Five-lane stabilization matrix — 2026-08-11

## Frozen baseline

- Product commit: `8deb1977f6fb8d2ba809d9d9c0a818d3745e7415`
- Worker image: `sha256:87c307cb079b52cadb754778d1a25457f48b65fa548a92903f162b5b088f8f6b`
- Campaign ceiling: `$18.00`
- Actual worker model cost: `$2.269806`
- Test lanes were evidence-only and had no safe-apply authority.

The Agent Platform router initially started work but delayed its client receipt while
requesting a redundant second model summary. Commit `4aeef89` changed only the managed
router to return the ADK tool receipt directly. The frozen worker image, case contracts,
and approved work orders did not change.

## Results

| Lane | Size | Result | Cost | Model calls | Evidence |
|---|---|---:|---:|---:|---|
| incident-playbook | small document | rejected | `$0.708803` | 18 | Revision limit reached before verification passed. |
| operations-workbook | small workbook | **verified complete** | `$0.179481` | 6 | Workbook artifact and required verification converged. |
| service-dashboard | small web app | budget stop | `$0.759174` | 16 + 1 denied | The next `$0.137821` call was denied with `$0.051226` remaining. |
| public-data-greenfield | medium new web app | rejected | `$0.282104` | 8 | Required independent runnable HTTP/browser observation was unavailable. |
| exchange-existing | medium existing software | rejected | `$0.340244` | 5 | An exact edit anchor no longer occurred once in `web/app/page.tsx`. |

Aggregate verified completion was `1/5` (`20%`). No result was silently promoted and
no source project was modified. The evidence therefore proves cost isolation and
fail-closed behavior, but it does not yet prove broad completion reliability.

## Integration interpretation

The normalized fingerprints are different, so the first automatic clustering pass
correctly produced no cross-lane common-fix candidate. Human integration review should
still examine whether three symptoms share a deeper platform cause:

1. document revision exhaustion and small-web budget exhaustion may indicate inefficient
   convergence prompts or overly broad criteria for small artifacts;
2. new web applications currently lack a qualified runnable development/observation pack;
3. exact string replacement remains brittle for existing software after prior edits;
4. the budget estimator did not reserve enough room for one final small-web repair call.

The next iteration must patch only these shared mechanisms, re-run the affected cheap
lanes first, and then revalidate all five lanes before promotion.
