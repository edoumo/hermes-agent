# Governed destructive capability — authority lifecycle

This document describes the authority model behind the `governed_mkfs` tool
and the `hermes grant` CLI: how a one-shot destructive capability is issued,
claimed, fenced to a VM incarnation, executed, and settled.

## Threat model

The model (LLM agent) and the human operator run under the same OS principal
in the common case. A capability file with mode `0600` is therefore NOT a
human/model boundary by itself: any model surface that can read or write the
grant directory could in principle forge or tamper with a grant. The design
treats the persisted grant as *corruption- and clone-resistant* (integrity
binding over the full identity) and the *issuance boundary* as the place
where human authority enters the system.

## Issuance (`hermes grant issue`)

Issuance runs only in the user's own shell, never from a model tool:

1. The CLI captures the live VM incarnation through the structured QGA
   adapter (read-only): `product_uuid` (stable guest identity, survives
   reboots, changes only on VM recreation), `boot_id` (boot generation), and
   `hostname`. The `--hostname` argument must match the observed guest
   hostname.
2. A correlated human approval decision is obtained (approval transport, or
   an explicit interactive TTY confirmation). The evidence
   (`request_id`, `request_digest`, `decision=once`, `principal`, `surface`)
   is bound into the grant.
3. The grant is persisted with a canonical binding over the FULL identity:
   `grant_id`, operation tuple, subject, session, `issued_at`, `expires_at`,
   evidence, and the incarnation triple. Any tamper — TTL extension, clone
   under another UUID, evidence swap, incarnation swap — breaks the
   recomputed binding and is treated as DENY.

## Lifecycle

```text
LIVE
  |  atomic claim (rename, one winner)
  v
CLAIMED(execution_id)
  |  pre-effect failure
  +--> settled:failed_pre_effect   (no mutation can have happened)
  |  execution started
  +--> settled:completed           (postcheck proved the expected result)
  +--> settled:indeterminate       (mutation MAY have happened; no blind retry)
```

Invariants:

- The claim is an atomic rename: exactly one concurrent caller wins; the
  loser is denied before any QGA call.
- The claim is bound to `execution_id`; settlement re-checks it.
- No `qga_create_filesystem` call happens before a successful claim.
- After the claim, the sink re-reads the guest identity and requires
  `product_uuid`, `boot_id`, and `hostname` to match the authorized
  incarnation (stale-actor witness: a grant issued for generation A can
  never mutate generation B, even with the same `vm_id`/device/hostname).
- Once claimed, a grant never returns to the live pool. `indeterminate`
  means a mutation may have happened: replay is denied, and the operator is
  told the outcome is unknown rather than "nothing happened".

## Execution (`governed_mkfs` model tool)

The model tool consumes a grant id and the exact tuple. The handler:

1. claims the grant (atomic reservation);
2. verifies the exact tuple (operation/vm/device/fs/label/session);
3. re-reads the guest incarnation and compares it to the authorized one;
4. runs fail-closed prechecks (device exists, block device, not mounted, no
   existing filesystem/signature, no LVM/mdraid/holders, not used by Docker,
   not in fstab);
5. re-samples the identity immediately before the action (TOCTOU, boot_id
   included);
6. executes via the structured QGA adapter — argv built exclusively from
   allowlisted fields (`FS_TYPE_TO_BINARY` map, validated label, validated
   partition device) — no shell;
7. postchecks (fs type + label + uuid via `blkid -o export`);
8. settles the grant durably.

Any failure after step 6 is settled `indeterminate` and reported as
`INDETERMINATE`, never as a retryable DENY.

## Hardline separation

The generic terminal `mkfs` remains on the unconditional hardline blocklist
(`tools/approval.py`): it cannot be executed by the agent in any mode
(`--yolo`, `approvals.mode=off`, cron approve). The governed path is the
only structured alternative, and it requires a valid, unexpired, unclaimed
grant bound to the exact target incarnation.
