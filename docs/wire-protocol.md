# The agent wire protocol

How a Vigil agent talks to the server: transport, registration, check-in, the
task envelope, signing and the `when:` expression language. The two vector files
under `protocol/conformance/` make it testable. Everything here comes from the
code named in each section; when they disagree, the code wins and this page is
a bug.

- Server side: `server/vigil/signing.py`, `server/apps/hosts/api_schema.py`,
  `server/apps/hosts/views/agent.py`.
- Agent side: `agent/vigil_agent/client.py`, `agent/vigil_agent/verify.py`,
  `agent/vigil_agent/expression.py`.

## 1. Transport

Every call is HTTPS with a JSON body and a JSON response. The agent verifies
the server's TLS certificate on every request; there is no option to turn that
off (`agent/vigil_agent/client.py` module docstring). Agents connect outward —
the server never opens a connection to a host.

**Authentication.** One header, on every call after registration:

```
Authorization: Bearer <agent_token>
```

The server requires the `Bearer ` prefix, looks the token up as a Host's agent
token (`server/apps/hosts/authentication.py:authenticate_agent`), and answers
401 with `{"error": ...}` when the header is missing or unknown. Check-in is the
only authenticated endpoint that accepts a host that has not been approved yet
(a pending host has to check in to reach the enrolment queue); every other
endpoint answers 403 `{"error": "This host has not been approved"}` for a
pending or rejected host.

**Errors.** A 4xx body is `{"error": ...}` or DRF's `{"detail": ...}`; both keys
are optional (`HostsErrorSerializer`), so read whichever is present.

### Registration

`POST /api/v1/register`. Unauthenticated by design — this is how a host gets
into the enrolment queue. Request body (`HostsRegisterSerializer`):

| Field | Type | Required | Meaning |
|---|---|---|---|
| `agent_token` | string | yes | 16-128 letters, digits, `-` or `_`. The credential for every later call. |
| `hostname` | string | yes | What the host calls itself. |
| `machine_id` | string | no | Stable machine fingerprint; a changed value re-binds the token to the new machine. |
| `os` | string | no | e.g. `Linux 6.9.0-15-generic`. |
| `kernel` | string | no | Kernel release. |
| `tags` | list of string | no | Seeds the host's tag list, namespaced under `agent:*`. |

Response (`HostsRegisterResponseSerializer`) — a new host answers 201, an
already-known token answers 200 with the same shape:

| Field | Type | Meaning |
|---|---|---|
| `id` | string | The host's id. |
| `status` | string | `pending` until an admin approves the host. |

### Endpoints the agent calls

All from `agent/vigil_agent/client.py`; `<base>` is the configured server URL.

| Method and path | Function | Purpose |
|---|---|---|
| `POST /api/v1/register` | `register` | Enrol this agent (above). |
| `POST /api/v1/checkin` | `checkin` | Report state, receive tasks (section 2). |
| `POST /api/v1/tasks/result/` | `report_result` | Hand back a finished task's state and output. Body: `task_id`, `state` (a terminal state), `output` (capped at 2,000,000 characters by the agent, saying so in the text when it truncates), and `steps` when the task had steps. The task must belong to the calling host. |
| `POST /api/v1/agent/log-tail/<session>/` | `post_log_lines` | Ship new log lines for a live container tail. Body `{"lines": [...]}`; the response's `continue` tells the agent whether anyone is still watching. |
| `GET /api/v1/agent/stack-env/<ticket>/` | `fetch_stack_env` | Redeem a deploy's one-time ticket for the stack's `.env` text: `{"env": "..."}`. |
| `GET /api/v1/agent/stack-source/<ticket>/` | `fetch_stack_source` | Redeem a source ticket for the packed folder of a Git stack (raw bytes, at most 20 MiB; the caller checks the signed sha256). |
| `POST /api/v1/agent/stack-adopt/<ticket>/` | `post_stack_read` | Hand an adopted stack's files back to the server over the agent's own connection. |
| `GET /api/v1/agent/registry-auth/?registry=<host>` | `fetch_registry_auth` | The private-registry login the server holds for this host; anything but a 200 with a `username` means "pull anonymously". |

## 2. Check-in

`POST /api/v1/checkin`, Bearer auth, both bodies below are JSON objects.

### Request body (`HostsCheckinBodySerializer`)

Every field is optional: an agent too old to report one omits it and the server
keeps what it already knew. That convention runs through the whole body — an
absent key means "this agent cannot tell you", never "the answer is zero or
empty".

| Field | Type | Meaning |
|---|---|---|
| `hostname` | string | Reported hostname. |
| `os` | string | OS name and release. |
| `kernel` | string | Kernel release. |
| `machine_id` | string | Stable machine fingerprint. |
| `mode` | string | The agent's own mode; the agent is authoritative about what it will execute. |
| `vigil_version` | string | Agent version, for the server's feature gating. |
| `key_ack` | string | The next signing key this agent already holds (base64, 44 chars), so a rotation knows who is ready. |
| `allowlist` | list of string | Actions this agent will run. Empty is meaningful ("nothing"). |
| `allow_reprovision` | boolean | Whether this host accepts reprovisioning tasks. |
| `allow_remote_shell` | boolean | Whether it will open a live shell for a human. |
| `allow_remote_desktop` | boolean | Same, for RDP. |
| `runs_as_root` | boolean | A managed agent left unprivileged fails every task, so the server marks such a host. Omitted on Windows. |
| `poller` | boolean | Whether this agent's own `agent.yml` says `poller: true`. Only the check-in writes it; the dashboard never does. A poller is handed device checks (section 4, Device checks). Agents from 2026.17 always send it. Absent keeps the stored value. |
| `check_results` | list of object | Results of the device checks this poller ran since its last check-in: at most 500, each `{"check": "<uuid>", "ok": bool, "latency_ms": number or null, "detail": string, "at": ISO 8601 time, ...}`. Sent only when there are results (agents from 2026.17). Results for a check the server did not hand this poller are dropped. `detail` is kept to 300 characters; other keys are kept as `extra` (scalars only, at most 20). |
| `features` | list of string | Task-language features this agent understands (`signed_v2`, `relevant`, …). The server will not dispatch what the agent cannot read; see section 4 for what `signed_v2` does. |
| `reboot_required` | boolean | Absent means too old to report; an explicit `false` writes. |
| `agent_update` | object | How a self-update ended, sent on each check-in until one carrying it succeeds: `state` (`confirmed`, `rolled_back` or `not_applied`), `from_version`, `to_version`, `running` (the version now running) and `reason`. Absent on every other check-in. The server stores it for display with its own timestamp and decides nothing from it. |
| `tags` | list of string | Namespaced under `agent:*` on the way in. |
| `inventory` | object | Custom inventory columns. |
| `software` | object | Installed-software list; absent means "nothing new to store". |
| `windows_updates` | object | Count of pending Windows updates; absent = cannot count. |
| `windows_update_list` | object | Per-update rows, sent only after a fresh scan. |
| `container_engines` | list of object | Docker/Podman engines found on the host. |
| `docker_containers` | list of object | Absent = Docker unavailable; an empty list means "no containers now". |
| `docker_resources` | object | Container resource usage. |
| `network` | object | Network snapshot for the topology view; absent keeps the previous one. |
| `metrics` | list of object | The metric samples collected this cycle. |

### Response (`HostsCheckinSerializer`)

What an approved agent gets back:

| Field | Type | Meaning |
|---|---|---|
| `status` | string | `"ok"`. |
| `public_key` | string | The task-signing public key, base64 Ed25519. Pinned on first use; see section 4. |
| `key_rotation` | object, optional | `{"next", "sig"}` — present only while a rotation is under way (section 4). |
| `tasks` | list of task envelope | This host's tasks, signed; section 3. |
| `relays` | list of string | Fallback relay URLs, in priority order. |
| `direct` | boolean | Whether the dashboard itself still takes agents. |
| `relays_issued` | integer | Issue time of the relay list, so an agent refuses a replayed older one. |
| `relays_sig` | string | Signature over the relay list, bound to this agent's token fingerprint: a relay that could hand the list unsigned could point every agent at an address it controls. |
| `software_digest` | string | Digest of the software list the server holds; empty when it holds none. The agent resends its list only when this differs from its own. |
| `checks` | list of object, optional | Only when this host is a poller (`agent.yml` `poller: true`): the device checks it must run, sorted by `id`. Each is `{"id", "kind" (icmp, tcp, http or dns), "params", "interval"}` (seconds). Absent for every other host. |
| `checks_issued` | integer, optional | Issue time of the check list, so an agent refuses a replayed older one. Present with `checks`. |
| `checks_sig` | string, optional | Signature over the check list, bound to this agent's token fingerprint, built exactly like `relays_sig`. An agent runs only the checks from a list that verifies. Present with `checks`. |

## 3. Task envelope

A task handed out at check-in (`HostsTaskSerializer`):

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Task id (UUID). Also the `task_id` to report against. |
| `host_id` | string | The host this task was created for; a v2 signature binds it to the receiving agent instead. |
| `action` | string | Action name from the server's action registry. |
| `params` | object | Action parameters. |
| `nonce` | string | Unique per task. Makes a signed payload unrepeatable: replaying an old task is a signature failure. |
| `signature` | string | Base64 Ed25519 over the canonical payload — section 4. An agent rejects a task whose signature does not verify with its pinned key. |
| `sig_v` | integer, optional | `2`, and only for an agent that advertised `signed_v2`. Its absence on a v2-advertising agent means the envelope was not signed as v2 — reject it. |
| `ttl_seconds` | integer | How long the task may sit before the agent must refuse it. |
| `dispatched_at` | date-time | When the signed payload went on the wire. Current agents time the TTL from this, so a task that waited days for a schedule window is still processable. |
| `created_at` | date-time | Kept for agents predating `dispatched_at`; do not use it as a freshness proof. |

## 4. Signing

Task signatures are Ed25519, made with the server's task-signing key and
verified against the public key the agent pinned (`server/vigil/signing.py`
signs; `agent/vigil_agent/verify.py:verify_task_signature` rebuilds the same
bytes to verify).

### Canonical payload — what another language must reproduce

The signed bytes are exactly:

```
json.dumps(fields, sort_keys=True).encode()
```

with Python's **default separators** (`", "` between entries, `": "` between key
and value) and **`ensure_ascii` on**. Concretely: object keys in Unicode
code-point order at every nesting level, one space after each `,` and `:` and no
whitespace elsewhere, numbers in Python's `repr`, `true`/`false`/`null` lower
case, and any non-ASCII character escaped as `\uXXXX`. `"café"` is signed as
`caf\u00e9`, so the bytes are pure ASCII; a signer that emits raw UTF-8 does not
match. `protocol/conformance/signing.json` pins this with a case whose params
carry nested objects, a list, and `"café"`.

### v1 field set

Six fields, all at the top level:

| Field | Source |
|---|---|
| `id` | task id as a string |
| `host_id` | host id as a string |
| `action` | action name |
| `params` | the params object, verbatim |
| `nonce` | task nonce |
| `ttl_seconds` | integer |

### v2 field set (`sign_task_v2`, SEC-4)

The six v1 fields plus three more:

| Field | Source |
|---|---|
| `dispatched_at` | the dispatch timestamp, ISO 8601 with offset — exactly the string the agent received as `dispatched_at` |
| `agent` | lowercase hex SHA-256 of the agent's token (`agent_fingerprint`). The agent knows its token, not its host id, so this is the identity both ends can compute; it is how the token is stored server-side too. |
| `v` | the integer `2` |

v1 left the dispatch time unsigned, so whoever relayed a task could restart its
TTL, and it named a target host the agent never checked. **An agent that
advertises `signed_v2` accepts only v2**: verifying a v2-advertising agent's
task with an `agent_token` requires `sig_v == 2` and a string `dispatched_at`,
and rejects the task otherwise — accepting v1 there would let a relay strip both
bindings. A task with no signature at all is rejected outright.

### Device checks

The check list is signed the same way as the relay list (`vigil.signing`, the
task key, the canonical form above). The payload is
`{"v": 1, "type": "check-list", "agent": <token fingerprint>, "issued": <integer>,
"checks": [{"id", "kind", "params", "interval"}, ...]}`, serialised with
`json.dumps(payload, sort_keys=True)` and signed with Ed25519. The `type` field
keeps this signature from being mistaken for a relay list or a task, and
`agent` stops a poller from replaying a list signed for another agent. The
response carries the payload's `checks`, `issued` (as `checks_issued`) and the
base64 signature (as `checks_sig`); a verifier rebuilds the payload from those
and the agent's own fingerprint.

### Key rotation

While a next key is on offer, the check-in response carries
`key_rotation = {"next": <base64 public key>, "sig": <base64 signature>}`
(`HostsKeyRotationSerializer`, `server/vigil/signing.py:rotation_statement`).
The signed statement is `rotation_payload`:

```
json.dumps({"type": "key-rotation", "v": 1, "next": next_pub}, sort_keys=True)
```

signed by the key currently in use. The agent holds a next key **only when the
key it already trusts verifies that statement**
(`agent/vigil_agent/verify.py:rotation_payload`, `_statement_ok`); it then
acknowledges the key as `key_ack` on later check-ins and moves over when the
server starts presenting it. The `"type"` field keeps a rotation statement from
being mistaken for anything else the task key signs (tasks, relay lists, session
tickets), which never carry a `type`.

## 5. Expression language

A step's `when:` guard is a small Python-style expression the agent evaluates
at run time against its own context; the server validates the same grammar when
a definition is saved, so a broken predicate is rejected before it is ever
signed. Both sides ship byte-identical copies of the module
(`server/apps/tasks/expression.py`, `agent/vigil_agent/expression.py`).

The rules, restated from that module's docstring:

- No function calls. `len()`, `str()`, `int()`, anything custom — nothing. The
  grammar is comparison plus boolean composition over dotted names and literals.
- No attribute writes, no augmented assignment, no comprehensions, no
  subscripts, no lambdas, no f-strings.
- A tuple literal belongs on the right of `in` / `not in`, not anywhere else.
  The whitelist rejects it in every position that needs more than a bare
  comparison operand, so `1 in (1, 2)[0]` is refused as a subscript. List
  literals are accepted as an `in` container too.
- Dotted name access (`agent.os`) is allowed; bracket access (`agent["os"]`) is
  not.
- Ordering comparisons (`<`, `<=`, `>`, `>=`) exist for numbers only. If either
  side is not an `int`/`float` — a string, a bool, or a missing value — the
  comparison is **False**, never an error and never a TypeError. Version
  comparisons belong in hunt params (`version_lt`, …), not expressions.
- `steps` reads an earlier step: exactly `steps.<id>.status` or
  `steps.<id>.result.<field>` — the one root allowed three levels deep. Any
  other shape under `steps` is refused at save time by the step-reference check
  (`referenced_steps`), because a bare `steps.x` resolves to a dict and would
  make every comparison quietly false.

Context roots are `agent`, `inputs`, `host` and `steps`; any other bare name is
an error. Dotted chains are capped at two levels under `agent`, `inputs` and
`host`, three under `steps`. Underscore-prefixed attributes are refused outright,
which keeps `agent.__class__`-style probing out of the grammar.

Missing keys resolve to `None`, so a predicate never raises over a context the
agent has not filled in yet — `agent.os == "linux"` on an agent that reported no
OS is simply False, `inputs.nope == None` is True, and `None in (...)` is False
while `None not in (...)` is True. An untestable membership (`7 in host.count`)
is likewise just False. Booleans are numbers for equality (`agent.flag == 1`
holds when the value is `true`) but never for ordering.

## 6. Conformance

Two vector files, both checked in under `protocol/conformance/`, each run by
both sides:

- **`expressions.json`** — `{"cases": [...]}`, each case one of:
  - `{"name", "expr", "context", "result"}`: `evaluate(expr, context)` must
    return exactly `result`. The same `context` object is a dict keyed by the
    roots.
  - `{"name", "expr", "error": true}`: `validate(expr)` must raise `ExprError`.
    No context is needed — validation never resolves a name.
  - `{"name", "expr", "steps_shape_error": true}`: `validate` accepts it, and
    the step-reference check (`referenced_steps`) must refuse it. These pin the
    `steps` shapes that only the spec-validation layer rejects.

  Case names say what they pin (`ordering_against_string_is_false_not_error`,
  `missing_name_compares_equal_to_none`, …), so a failure reads as a sentence.

- **`signing.json`** — `seed_b64` / `public_key_b64` (a test key that signs
  nothing real), the `agent_token` the v2 cases bind to, and `cases`:
  - `{"name", "version": 1, "task": {"id", "host_id", "action", "params",
    "nonce", "ttl_seconds"}, "canonical", "signature_b64"}`
  - `{"name", "version": 2, "task": {...}, "dispatched_at", "agent_token",
    "canonical", "signature_b64"}`

  `canonical` is the exact JSON text that gets signed, so a change in key
  ordering, separators or non-ASCII escaping fails on the string before it ever
  reaches Ed25519. The signature itself need not be checked by hand: Ed25519 is
  deterministic, so a signer that reproduces `canonical` reproduces
  `signature_b64`.

The two runners are `server/apps/tasks/test_conformance.py` (against
`apps.tasks.expression`, `vigil.signing.sign_task` / `sign_task_v2`) and
`agent/tests/test_conformance.py` (against `vigil_agent.expression` and
`vigil_agent.verify.verify_task_signature`, including that flipping any signed
field — the action, one params value, the nonce, the TTL, and for v2 the
dispatch time — breaks verification, and that a v1 envelope is refused by an
agent advertising v2). A change to signing or to the expression language changes
the vectors in the same commit — if a vector has to move, the protocol moved,
and this page moves with it.
