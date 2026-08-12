# Imported-project ToolPack lifecycle

Imported projects do not receive execution authority from their project manifest. OneBrief
uses a separate four-stage ToolPack lifecycle.

1. **Generate**: derive bounded read/write prefixes, safe text suffixes, fixed adapter
   IDs, and exact reusable capability-pack references from the registered project and its
   current Git HEAD.
2. **Qualify**: verify manifest identity, root containment, write-path subset rules, immutable
   Git base, isolated snapshot support, and at least one deterministic validation adapter.
3. **Approve**: store a local-user approval for the exact canonical composed ToolPack hash.
   The digest covers the project profile and every capability-pack version and definition
   digest. A changed component, changed ToolPack, or regenerated repository base cannot
   reuse an old approval.
4. **Execute gate**: execution remains blocked unless approval is valid and every independent
   execution prerequisite is ready.

## Fixed adapters

The generated profile can select only application-defined adapters:

- repository snapshot;
- Unity batch compilation;
- Unity EditMode tests;
- bounded Unity Canvas/RectTransform hierarchy diagnostics;
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

For Unity visual work, the fixed layout diagnostic source is injected after patch hygiene
checks and only into the disposable clone. Its evidence records the observed project
hierarchy, the immutable source revision, and a digest of the exact candidate. A failed
semantic visual check may use that evidence to narrow the next repair, but diagnostic
findings are not themselves permission to edit a new path.

The source repository is never patched by the runtime. A changed HEAD, dirty worktree,
changed approval hash, missing validation adapter, stale file hash, blocked path, or failed
test/build stops execution before any result is accepted.
