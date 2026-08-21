# Durable Workers qualification ledger

This file records the experimental qualification lineage used to build the H6
integration candidate. It is intentionally concise: each phase has a single
purpose and a code SHA that can be inspected independently.

| Phase | Status | Repository / qualified code | Scope |
|---|---|---|---|
| H1 | PASS | `edoumo/hermes-agent` `b4885b2e3fd0343b440616505eedc822b8dc9954` | Durable worker identity, inbox, activation serialization, cold reactivation |
| H2 | PASS | `edoumo/hermes-agent` `453693eacae16a21ec73d4e67c5225ae7bc9a012` | Session-scoped read API |
| H2.1 | PASS | `edoumo/hermes-agent` `bf336decb0ba298e85e5a34cf5b7a596f7dee2dc` | Write/control API, background execution and authenticated SSE |
| H3 | PASS | `edoumo/hermes-webui` `e67722264f109e22f4f7fd2b29ec3898f69083c5` | Standalone Harness UI foundation and server-side BFF |
| H4 | PASS | `edoumo/hermes-agent` `2e14c4f719a9c85bb79b9a44dc72a15cecfb1c39` / `edoumo/hermes-webui` `decc07a86ef109f953e9f13433cd8990dff249ef` | Operator cancel, fail-closed recovery, CAS retry and Harness controls |
| H5 | PASS | `edoumo/hermes-agent` `ea254053c82929fc44646b6cb4c8456498d5deb4` / `edoumo/hermes-webui` `97f614f19cb71439028287ed87bc11679ebe76db` | Operational task DAG, real dispatch, cancel/recover/crash continuity |
| H6 | pending qualification | final-consolidation branches | Formal schema v1, compatibility/hardening, final open-source reviewability |

Post-qualification maintenance commits that only changed tests or documentation
are deliberately not substituted for the behavior SHAs above. For example,
H4 and H5 have later branch heads containing test-hygiene/documentation fixes,
but their real-runtime evidence remains tied to the qualified behavior SHAs.

## Evidence rule

A phase is marked PASS only when the relevant repository tests and required
real-runtime/browser scenarios have passed. Generated code or generated tests
alone are never sufficient evidence.

## Integration rule

H6 must preserve the H1-H5 behavioral contracts. A final integration candidate
may refactor or formalize internal boundaries, but any regression in durable
serialization, session isolation, cancellation, retry/recovery, SSE ownership
or task DAG semantics reopens the responsible phase instead of being waived as
"final cleanup".

See [H6 final consolidation](h6-final-consolidation.md) for the final storage,
rollback and qualification contract.
