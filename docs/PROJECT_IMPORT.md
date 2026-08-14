# External project import

OneBrief treats any project with a validated project manifest as an existing project,
whether or not OneBrief originally created it.

## Entry file

Place a file named `ONEBRIEF_PROJECT.json` in the project root. Upload that exact
resident file from the existing-project screen.

Required fields:

- `schema_version`: always `onebrief-project-v1`
- `project_id`: stable lowercase ID
- `name`: user-facing project name
- `project_type`: short type such as `unity_game` or `web_application`
- `project_root`: absolute local project path
- `canonical_goal`: the single goal restored on future improvement runs

Optional fields:

- `summary`
- `authoritative_documents`: relative paths only

The manifest is identity, not authority. It cannot declare commands, ToolPacks,
secrets, environment variables, deployments, or write permissions.

## Import sequence

1. Verify schema and exact resident-file proof.
2. Reject broad roots, command fields, path escapes, and conflicting project IDs.
3. Inspect repository metadata and technology markers read-only.
4. Register a sidecar workspace outside the source repository.
5. Create memory, knowledge, skills, ToolPack-preparation, evidence, and run folders.
6. Mark the project `needs_generation`; source editing remains disabled.
7. A later ToolPack Builder must generate and qualify bounded capabilities before use.

## Sidecar layout

```text
<OneBrief registry>/<project-id>/
  project.json
  inventory.json
  memory/
    canonical_goal.md
    project_state.json
  knowledge/
  skills/
  toolpacks/
    preparation.json
  evidence/
  runs/
```

The original repository is not modified during import.

