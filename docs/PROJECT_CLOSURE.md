# Project closure and Pack promotion

OneBrief closes a terminal project before publishing its immutable result package.
Closure hashes the retained workspace, excludes temporary files, records the disposition
of credentials and private working memory, and extracts reusable material only as a
candidate. Project-specific memory remains inside the closed project.

The first implemented extractor recognizes a successfully executed ToolPack. It records
the fixed command evidence, hashed files, activation condition, and safety boundary. A
bounded secret scan may pass automatically, but provenance and independent review remain
pending.

Candidate registration is not promotion. `PackRegistry.promote` fails closed unless:

1. privacy review passed;
2. provenance and license review passed;
3. independent review passed; and
4. the candidate succeeded in at least two distinct projects.

Releases are immutable and versioned under `workspace/pack_registry/releases`. This lets
OneBrief learn a standard operating toolkit from completed work without copying an entire
project's private memory into the shared agent library.

