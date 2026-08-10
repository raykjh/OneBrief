# Parallel test campaigns

OneBrief uses three isolated test lanes to find failures quickly and one integration
lane to decide what may change shared product code. Parallelism increases evidence
throughput; it does not grant several workers permission to edit the same baseline.

## Fixed topology

1. Freeze one Git commit, tree hash, container image digest, model policy, case contract,
   and total budget in `campaign.json`.
2. Run exactly three lanes from that baseline. Each lane has a unique output root and
   budget cap and may only publish evidence.
3. The integration lane clusters failure fingerprints. A failure seen in two or more
   distinct lanes is eligible for a common OneBrief fix. A one-lane issue remains in
   that project's ToolPack or knowledge pack.
4. Only the integration lane changes common code. The candidate must then pass all
   three lanes again before promotion.
5. Safe apply is serialized. Test lanes never apply changes to a source project, and
   two jobs never apply to the same project concurrently.

## Pilot lanes

- `exchange-existing`: a larger existing software improvement with external data,
  degraded modes, bilingual UI, build tests, HTTP checks, and safe-apply requirements.
- `public-data-greenfield`: a new, executable public-data briefing application.
- `operations-workbook`: a structured Excel deliverable with row-level traceability and
  deterministic workbook verification.

This spread is intentional. A homepage-only suite could prove web generation but would
not reveal whether completion contracts, artifact verification, recovery, and packaging
generalize to other work.

## Evidence and promotion

Every lane receipt records its frozen commit and case hash, actual cost and elapsed
time, explicit user questions and approvals, repeated requests, recovery attempts and
successes, normalized failure fingerprints, and safe-apply outcome. Evidence is
append-only and guarded by a per-lane file lock.

The integration receipt reports completion rate, total cost, median and p95 duration,
user interventions, recovery rate, common failures, project-specific failures, and
promotion blockers. A candidate image or commit is promotable only when the latest
revalidation contains one passing result for every lane.

## Commands

Creating a campaign only writes the immutable assignments; it does not launch Cloud
jobs or spend model credits.

```powershell
.venv\Scripts\python scripts\parallel_campaign.py create `
  campaign_configs\parallel_pilot_20260810.json `
  benchmarks\campaigns\onebrief-pilot-20260810 `
  --repo-root .
```

Workers submit lane evidence with `record`. The accountable integration process then
runs `integrate`, makes one reviewed common patch if warranted, reruns all three lanes,
and records the final three-lane receipt with `revalidate`.

The pilot configuration reserves at most USD 18: USD 8 for Exchange, USD 6 for the new
web application, and USD 4 for the workbook. This is a configuration ceiling, not
permission to spend. Cloud execution still requires the user's explicit campaign
approval.
