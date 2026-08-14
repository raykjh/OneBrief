# JULPAE Homepage — OneBrief End-to-End Demo

## Outcome

OneBrief completed an existing-project workflow from bounded planning through verified safe apply.
The final source is in `C:\memory R\JULPAE-homepage-onebrief-demo` at commit `2df845a`.

Final factual-correction session:

- Session: `3f40ba3d-dd19-46f3-ae83-1e77fc3d1e67`
- Job: `5cccc593-a02f-4672-9aed-1d15ef52c234`
- ToolPack hash: `8049bc4c2b5232310c450801f8c47f3c04d3f301b2928ca4098b72730bc54edd`
- Safe-apply package: `d76762868aeb34b89c91abe243cc0db0293c0a667726ac4b91b783f91f9e4861`
- Required criteria: `5 / 5` passed
- Approved ceiling: `$3.2472`
- Actual model cost: `$0.192663`
- Model calls: `6`

## Stage 1 — completion contract and authorization

The user supplied one goal and two first-party game-guide documents. OneBrief produced:

- a completion contract;
- an exact-hash ToolPack;
- bounded write access to `src/` and `public/` only;
- read-only access to fixed tests, build scripts, and server scripts;
- a recommended budget and immutable execution ceiling;
- deterministic build, HTTP, asset, responsive, localization, and factual-grounding criteria.

Public research was disabled for the final factual pass. Gameplay claims were limited to the supplied Korean and English guides plus the project README.

## Stage 2 — make, observe, reject, revise, and apply

The pipeline used Gemini agents for analysis, implementation, independent review, and final approval. Fixed code executed the approved build and tests. A pipeline-owned web observer then:

- launched the isolated result using the approved `start` script;
- rendered a 1200px desktop page and exact 375px mobile page in headless Chrome;
- activated the language control appropriate to the current document locale;
- confirmed visible text and `html lang` changed from Korean to English;
- checked image load state, replacement characters, horizontal overflow, and visible element geometry;
- stored desktop/mobile PNG evidence and an independent semantic-observation receipt.

Only after all gates passed did safe apply back up the original file, update `src/index.html`, and rerun the fixed build and tests against the source repository.

## Failures that improved the product

The real run exposed and corrected reusable OneBrief defects rather than hiding them:

1. Background jobs previously changed a process-global project registry. Jobs now receive an explicit snapshot registry.
2. Fixed build/server scripts were omitted from snapshots. They are now readable but not writable.
3. Node tests ran asynchronously after the verifier ended. The fixed verifier now waits for the test process.
4. A build-only result lacked runtime and visual proof. A generic web observation ToolPack was added.
5. Headless Chrome's nominal 375px window did not equal its CSS viewport. The observer now hosts the app in an exact-width iframe.
6. The first language-control observer clicked the already-active `KO` button. It now reads the current locale and selects the opposite control.
7. Web observation failures were unclassified. They now return to the same maker for bounded revision.
8. Generated changes could attempt to modify acceptance tests. Node tests and scripts are now immutable ToolPack inputs.
9. A visually correct draft still included unsupported marketing claims. A final internal-source-only pass removed ungrounded claims before safe apply.

## Cost context

Across the complete JULPAE homepage development history retained in the local job store, 20 runs used `$3.472318` in recorded model cost across 95 calls. This includes deliberate troubleshooting and repeated observer hardening. The final factual-correction run itself used `$0.192663`.

## Final evidence

- Final desktop screenshot: `work/development/web_observation_evidence/desktop-ko.png`
- Final mobile screenshot: `work/development/web_observation_evidence/mobile-en.png`
- Independent receipt: `work/independent_observations/web_ui_observation.json`
- Safe-apply backup: `C:\Users\SEVER\AppData\Local\OneBrief\apply-backups\julpae-homepage-onebrief-demo\d76762868aeb34b89c91abe243cc0db0293c0a667726ac4b91b783f91f9e4861`

The local browser verification after safe apply confirmed the exact Google Play and Steam links, live Korean-to-English switching, `html lang="en"`, and zero browser warnings or errors.
