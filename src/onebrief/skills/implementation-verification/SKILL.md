---
name: implementation-verification
description: Independently compare an executable implementation with its acceptance criteria, source changes, tests, and claimed summary before release.
license: Apache-2.0
metadata:
  author: onebrief
  version: "1.0"
---

# Implementation verification

Do not review only the maker's summary.

1. Map every acceptance criterion to concrete source evidence and an executable test.
2. Compare claimed features with changed files. Missing, partial, or differently named behavior is blocking.
3. Check formulas and decision rules against an independent reference or known fixture.
4. Reject arbitrary thresholds or scores unless their source, purpose, and visible explanation are documented.
5. Treat passing legacy tests as regression evidence only; require new tests for new behavior.
9. For web programs, start the production server and verify the rendered HTML, every referenced JavaScript/CSS asset, and required data endpoints over HTTP.
6. Confirm that safety disclaimers do not contradict visible recommendations.
7. Return REVISE when code can correct the problem and NEEDS_INFORMATION only when required evidence is absent.
8. Include exact file, behavior, and test changes required for every blocking issue.