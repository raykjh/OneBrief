# Unity reference-completion playbook

This playbook records only project-independent lessons learned from a Codex
reference completion in an isolated clone. It does not contain or copy the
reference product implementation. OneBrief must still create its own solution
from the approved source snapshot, completion contract, and ToolPack authority.

## General rules

1. **Prove journey preconditions before repairing the product.** A test that
   clicks a login/start control and immediately demands a protected destination
   is invalid unless it establishes an approved authenticated test state or
   exercises the complete authentication UI. This is an evidence-topology
   defect, not permission to add a navigation bypass to product code.
2. **Capture synchronously in Unity batch mode.** Render the real scene camera
   to an explicitly sized `RenderTexture`, temporarily route active
   `ScreenSpaceOverlay` canvases through that camera, read pixels, write the PNG
   durably, and restore camera/canvas state. Do not rely on
   `WaitForEndOfFrame`, the system framebuffer, or `Screen.SetResolution`.
3. **Reject blank evidence before semantic review.** The trusted capture helper
   samples the rendered texture and rejects uniform/no-signal frames before a
   manifest can be published. Independent semantic visual review still owns
   higher-level layout, clipping, branding, and glyph judgments.
4. **Treat mobile orientation as a product contract.** Mobile Unity products
   can be landscape. Unless the goal explicitly requests an orientation,
   responsive proof requires two materially different measured mobile/desktop
   viewport shapes rather than a hard-coded portrait/landscape pair.
5. **Keep product and evidence repair wallets separate.** Invalid capture,
   missing preconditions, or incomplete scenario topology returns to the
   evidence maker. Only observed shipped behavior or rendering defects may open
   product repair authority.

## Acceptance tests

- A precondition-free protected-destination test is rejected as evidence
  topology; the corresponding product source remains outside the repair scope.
- An existing approved test-account/authentication path is accepted.
- A newly wired product `onClick -> SceneManager.LoadScene` bypass remains a
  product defect and is rejected.
- A uniform screenshot is rejected inside the trusted atomic capture helper.
- Desktop plus mobile-landscape evidence passes when aspect shapes are
  materially different; two resolutions with the same aspect shape do not.
- All prior ToolPack, execution, convergence, and Unity evidence regressions
  remain green.

## Reuse boundary

The isolated reference result is an oracle for diagnosing the system, not a
template to copy. A valid independent replay starts from the clean approved
source revision and receives only the rules above through OneBrief's normal
maker/verifier workflow.
