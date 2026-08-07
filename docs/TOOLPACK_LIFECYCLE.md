# Imported-project ToolPack lifecycle

Imported projects do not receive execution authority from their project manifest. OneBrief
uses a separate four-stage ToolPack lifecycle.

1. **Generate**: derive bounded read/write prefixes, safe text suffixes, and fixed adapter
   IDs from the registered project and its current Git HEAD.
2. **Qualify**: verify manifest identity, root containment, write-path subset rules, immutable
   Git base, isolated snapshot support, and at least one deterministic validation adapter.
3. **Approve**: store a local-user approval for the exact canonical ToolPack hash. A changed
   ToolPack or regenerated repository base cannot reuse an old approval.
4. **Execute gate**: execution remains blocked unless approval is valid and every independent
   execution prerequisite is ready.

## Fixed adapters

The generated profile can select only application-defined adapters:

- repository snapshot;
- Unity batch compilation;
- Unity EditMode tests;
- named `lint`, `test`, or `build` scripts already present in `package.json`;
- project Python tests.

The manifest and the generated profile cannot supply arbitrary commands. Dependency
installation, source-repository writes, deployment, release, Git push, credentials, accounts,
payments, personal data, and destructive Git operations remain outside the boundary.

## Connected runtime boundary

An exact-hash-approved profile becomes execution-ready only while the registered repository
is clean and remains at the approved Git HEAD. OneBrief then:

1. reads bounded committed text from the approved Git commit;
2. gives the maker only canonical paths inside approved write prefixes;
3. verifies every existing file's committed base hash;
4. clones the repository into a disposable directory and writes only there;
5. executes only the enabled generated adapters;
6. returns the patch, complete changed files, command evidence, and safety record.

The source repository is never patched by the runtime. A changed HEAD, dirty worktree,
changed approval hash, missing validation adapter, stale file hash, blocked path, or failed
test/build stops execution before any result is accepted.
