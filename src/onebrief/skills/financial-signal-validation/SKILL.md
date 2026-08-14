---
name: financial-signal-validation
description: Validate informational market indicators and decision-support UI without presenting unsupported financial advice or pretending unavailable data exists.
license: Apache-2.0
metadata:
  author: onebrief
  version: "1.0"
---

# Financial signal validation

1. Use named, reproducible formulas and state periods and inputs.
2. Test indicators with known fixtures and edge cases such as constant, rising, and falling series.
3. Never synthesize unavailable volume, execution price, spread, tax, or account data.
4. Do not calculate MFI or another volume-dependent indicator without volume data.
5. Separate observed values, technical signals, scenarios, and recommendations.
6. Display the rules and evidence contributing to each signal.
7. Use conservative labels such as review or watch; never imply guaranteed profit.
8. Keep trading and account execution outside the tool boundary.