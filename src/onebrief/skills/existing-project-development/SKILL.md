---
name: existing-project-development
description: Safely continue and improve an existing software project by restoring its goal, preserving behavior, making bounded changes, and proving the result with tests.
license: Apache-2.0
metadata:
  author: onebrief
  version: "1.0"
---

# Existing project development

Use this skill only for an approved existing repository.

1. Read the project goal, current state, commit history, source, and immutable tests before editing.
2. Separate existing behavior, requested changes, incomplete work, and failed attempts.
3. Prefer the smallest complete change that preserves working behavior.
4. Do not claim a feature from a plan or summary. Verify it in the delivered source.
5. Add or strengthen deterministic tests for calculations, constraints, and acceptance criteria.
6. Run the fixed tests and build, then start web deliverables in production mode and verify their HTML, referenced assets, and required data endpoints over HTTP. A build alone does not establish functional correctness.
7. Report every changed file, every test result, and every unimplemented request.
8. Never deploy, push, access accounts, or broaden permissions without explicit approval.