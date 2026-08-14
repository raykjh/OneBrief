# Folder-first project import

The normal OneBrief import path does not require a user-authored JSON file.

## User flow

1. Choose **Existing project improvement**.
2. Select **Choose project folder**.
3. OneBrief inspects the folder read-only and detects Git state, technology markers,
   project documents, and likely validation adapters.
4. Confirm only the project name and long-term goal.
5. OneBrief creates `ONEBRIEF_PROJECT.json` in the selected root and registers a
   separate project workspace.
6. ToolPack generation, qualification, and approval remain a later boundary. Import
   alone grants no command or write capability.

If the folder already contains a valid manifest, OneBrief shows it unchanged and
registers it without overwriting it. Manual manifest upload remains available as an
advanced recovery path.

## Local boundary

Native folder selection is intentionally available only in the local Windows service.
A Cloud Run deployment cannot browse or write a user's computer. External repositories
used by a hosted OneBrief service need a separate Git-provider connection and explicit
repository authorization.

## Generated draft

The deterministic discovery pass derives:

- a stable project ID from the folder name;
- a user-facing name;
- a project type from Unity, Node, Python, .NET, Rust, or Go markers;
- a conservative long-term goal that preserves existing behavior;
- candidate authoritative technical documents;
- Git branch, head, and clean/modified state;
- ToolPack capability and validation candidates.

The generated manifest remains identity metadata, not execution authority.
