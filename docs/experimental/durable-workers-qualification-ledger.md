# Durable Workers qualification ledger

This file records the qualification lineage used to build the H6 clean integration candidate. Each phase has a single purpose and a behavior-qualified code SHA that can be inspected independently.

| Phase | Status | Repository / qualified code | Scope |
|---|---|---|---|
| H1 | PASS | `edoumo/hermes-agent` `b4885b2e3fd0343b440616505eedc822b8dc9954` | Durable worker identity, inbox, activation serialization, cold reactivation |
| H2 | PASS | `edoumo/hermes-agent` `453693eacae16a21ec73d4e67c5225ae7bc9a012` | Session-scoped read API |
| H2.1 | PASS | `edoumo/hermes-agent` `bf336decb0ba298e85e5a34cf5b7a596f7dee2dc` | Write/control API, background execution and authenticated SSE |
| H3 | PASS | `edoumo/hermes-webui` `e67722264f109e22f4f7fd2b29ec3898f69083c5` | Standalone Harness UI foundation and server-side BFF |
| H4 | PASS | `edoumo/hermes-agent` `2e14c4f719a9c85bb79b9a44dc72a15cecfb1c39` / `edoumo/hermes-webui` `decc07a86ef109f953e9f13433cd8990dff249ef` | Operator cancel, fail-closed recovery, CAS retry and Harness controls |
| H5 | PASS | `edoumo/hermes-agent` `ea254053c82929fc44646b6cb4c8456498d5deb4` / `edoumo/hermes-webui` `97f614f19cb71439028287ed87bc11679ebe76db` | Operational task DAG, real dispatch, cancel/recover/crash continuity |
| H6 | READY FOR QUALIFICATION | `experimental/durable-workers-h6-upstream-main` + `experimental/hermes-harness-ui-h6-upstream-master` | Current-upstream clean integration, schema v1, compatibility/hardening and public reviewability |

## H6 upstream bases

- Hermes Agent: `NousResearch/hermes-agent@fcbd1076a93841fa88855acce810e342a5b78101`
- Hermes WebUI: `nesquena/hermes-webui@cfcc39194a4cfbb6c78fe8114695a70737e17bbf`

The H6 candidates are rebuilt directly on those upstream commits instead of carrying the historical experimental branch ancestry. At the qualification boundary, both clean branches must remain additive and must be zero commits behind their recorded upstream base.

Post-qualification maintenance commits that changed only tests or documentation are deliberately not substituted for the behavior SHAs above. H4 and H5 therefore retain their real-runtime behavior-qualified SHAs even though their historical branch heads later received test-hygiene/documentation commits.

## Evidence rule

A phase is marked PASS only when the relevant repository tests and required real-runtime/browser scenarios have passed. Generated code or generated tests alone are never sufficient evidence.

## Integration rule

H6 must preserve the H1-H5 behavioral contracts. A final integration candidate may formalize internal boundaries, but any regression in durable serialization, session isolation, cancellation, retry/recovery, SSE ownership or task-DAG semantics reopens the responsible phase rather than being waived as final cleanup.

See [H6 final consolidation](h6-final-consolidation.md) for storage, rollback and final qualification requirements.
