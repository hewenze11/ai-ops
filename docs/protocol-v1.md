# Execution protocol 1.0 — preview contract

Authority: this repository. Agent compatibility: `0.1.0.dev1`, exact protocol `1.0` only. No compatibility promise for undeclared versions. Control and worker release numbers are independent.

## Trust boundaries

Administrative API Bearer credential authorizes role/asset creation and task submission. It must NOT be available to an LLM tool. The model tool receives server-owned round context and can choose only among its selected native accounts. Public POST /tasks remains a trusted human/admin endpoint; model tools create child commands internally without receiving an admin credential.

Asset provisioning is an admin operation, usable from deployment CLI/API, not a Web registration wizard. Returns one random token once; server stores SHA-256 token hash. Agent authenticates with that token and matching asset ID. Account allowlists exist both at server and worker. Native operating-system permissions remain authoritative.

## Task

`POST /api/v1/tasks` (administrator):

```json
{
  "role_id": "ops",
  "asset_id": "test-linux",
  "execution_users": ["ops_read", "ops_admin"],
  "run_as": "ops_read",
  "command": "id -un",
  "timeout_seconds": 60,
  "mode": "direct",
  "idempotency_key": "caller-generated-unique-request-id"
}
```

All selected users are stored as immutable task payload. The actual run_as must be among them and configured for this asset. Missing user fails; never try another account. One Agent task contains one shell operation. Since dev3, control-side tasks have a distinct parent turn_id. Multi-operation role turns, manual commands and trigger inputs share one parent FIFO per role; see model-turns.md. Agent wire format remains 1.0.

`mode=confirm` starts awaiting_approval. Administrative `POST /tasks/{id}/approve` is a separate control request, not a queued role message. Cancellation presently supports only awaiting_approval/queued.

## Worker handoff

`POST /api/v1/agents/{asset_id}/claim`, body `{"protocol_version":"1.0"}`.

Empty response: `{"protocol_version":"1.0","task":null}`. Otherwise task contains id (UUID), claim_id (random delivery nonce), and original task fields. Server atomically transitions queued → claimed and appends audit, then responds. Different roles may advance independently. Commands cannot bypass an earlier unfinished parent role turn, even across different assets. A later command within the CURRENT multi-step turn runs before commands belonging to later turns; command insertion order alone no longer defines the role queue.

`POST /api/v1/agents/{asset_id}/tasks/{task_id}/result`:

```json
{
  "claim_id": "claim-nonce-from-server",
  "status": "succeeded",
  "exit_code": 0,
  "stdout": "ops_read\n",
  "stderr": "",
  "error_code": null,
  "output_truncated": false
}
```

Statuses: succeeded, failed, unknown. succeeded requires exit_code=0. Exact result retries are idempotent; changed result is a conflict. Agent token alone cannot overwrite another asset's result; claim nonce must match. Responses do not echo credentials.

## Crash semantics

Worker persists `started` before launching a process, persists `result_ready` before reporting, persists `acknowledged` only after server acknowledgement. On restart, result_ready is resent; started becomes unknown and is NOT rerun. Losing claim response can leave a claimed task with no local journal. This deliberately requires operator investigation rather than unsafe redelivery. No exactly-once claim is made.

`unknown` blocks the role. Operator resolution endpoint, leases, heartbeat reconciliation and running-task cancellation remain future milestones. No editing/deleting production DB rows as an official recovery method.

## Transport and storage

HTTPS required off localhost; Agent rejects credential-bearing redirects and environment proxies. Local explicit loopback HTTP allowed for isolated preview tests. Request/response credentials are excluded from application logs. Secret-bearing command output is not generically sanitized in this preview; avoid sensitive workloads until that feature is implemented.

Linux Agent configuration must be owned by the execution service user with mode 0600 (or stricter). Journal directory private; one process per journal. stdout/stderr each currently retain at most 64KiB, with truncation flag. This is not final complete audit output storage.
