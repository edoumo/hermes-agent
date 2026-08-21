# Durable Workers qualification ledger

This file records the qualification lineage of the Durable Workers and Harness workstream. Each phase has a single purpose and a behavior-qualified SHA that can be inspected independently.

| Phase | Status | Repository / qualified code | Scope |
|---|---|---|---|
| H1 | PASS | `edoumo/hermes-agent` `b4885b2e3fd0343b440616505eedc822b8dc9954` | Durable worker identity, inbox, activation serialization, cold reactivation |
| H2 | PASS | `edoumo/hermes-agent` `453693eacae16a21ec73d4e67c5225ae7bc9a012` | Session-scoped read API |
| H2.1 | PASS | `edoumo/hermes-agent` `bf336decb0ba298e85e5a34cf5b7a596f7dee2dc` | Write/control API, background execution and authenticated SSE |
| H3 | PASS | `edoumo/hermes-webui` `e67722264f109e22f4f7fd2b29ec3898f69083c5` | Standalone Harness UI foundation and server-side BFF |
| H4 | PASS | `edoumo/hermes-agent` `2e14c4f719a9c85bb79b9a44dc72a15cecfb1c39` / `edoumo/hermes-webui` `decc07a86ef109f953e9f13433cd8990dff249ef` | Operator cancel, fail-closed recovery, CAS retry and Harness controls |
| H4 hygiene | PASS | post-qualification test maintenance | Static/non-regression test hygiene |
| H5 | PASS | `edoumo/hermes-agent` `ea254053c82929fc44646b6cb4c8456498d5deb4` / `edoumo/hermes-webui` `97f614f19cb71439028287ed87bc11679ebe76db` | Operational task DAG, real dispatch, cancel/recover/crash continuity |
| H5 hygiene | PASS | `edoumo/hermes-webui` `76ed367cffed37f3abe3aaa881f6a6172c0d8c37` | H4/H5 static test layering cleanup |
| H6 | PASS | backend behavior `011fe3c2fb20385c97ecad450ded02d0982ae3db` / Harness `9a6ea47e969489149c3964c6de8bdb9923acd3cc` | Current-upstream clean integration, schema v1, compatibility/hardening and real-runtime qualification |
| H6 hygiene | PASS | backend `07dd7bd93411f3486e405b55dda48892395ea637` | AST-based plugin test hygiene; no production-code change |

## H6 upstream bases

- Hermes Agent: `NousResearch/hermes-agent@fcbd1076a93841fa88855acce810e342a5b78101`
- Hermes WebUI: `nesquena/hermes-webui@cfcc39194a4cfbb6c78fe8114695a70737e17bbf`

The H6 candidates were rebuilt directly on those upstream commits instead of carrying historical experimental branch ancestry. Both contributions were additive and zero commits behind their recorded bases at behavior qualification.

## H6 final evidence

Final technical status:

- `H6_FINAL_CONSOLIDATION_STATUS=PASS`
- `H6_TEST_HYGIENE_STATUS=PASS`
- `HERMES_DURABLE_WORKERS_INTEGRATION_READINESS=PASS`
- backend consolidated tests: `91/91 PASS`
- Harness tests: `36/36 PASS`
- real DeepSeek E2E, cancel/recovery/crash, isolation, SSE, capacity and soak: PASS
- H6 evidence archive SHA-256: `e7ac0b0d89e13055b94046d7aefed9123e1b40a9454bdd8a56300120a7755d91`

Post-qualification test/documentation commits are deliberately distinguished from behavior-qualified SHAs. Real-runtime evidence remains tied to the code that actually ran during qualification.

## Evidence rule

A phase is marked PASS only when repository tests and required real-runtime/browser scenarios pass. Generated code or generated tests alone are not sufficient evidence.

## Integration rule

H6 preserves the H1-H5 behavioral contracts. Any future regression in durable serialization, session isolation, cancellation, retry/recovery, SSE ownership or task-DAG semantics reopens the responsible contract rather than being waived as cleanup.

Technical readiness does not itself authorize an upstream PR. A maintainer hands-on acceptance pass remains the final product gate before PR preparation.

See [H6 final consolidation](h6-final-consolidation.md) for storage, rollback, qualification evidence and release gating.
