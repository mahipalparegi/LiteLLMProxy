# Security

Scope: the LiteLLM Proxy deployed by this repository, the credentials it holds,
and the customer virtual keys it issues.

---

## Assets

| Asset | Where it lives | If it leaks |
| --- | --- | --- |
| `LITELLM_MASTER_KEY` | Render env var, operator secret store | Full proxy admin: create/read/modify keys, add models, change config, read all spend |
| `LITELLM_SALT_KEY` | Render env var | Provider credentials stored in the database can be decrypted |
| `AZURE_CLIENT_SECRET` (+ tenant/client id) | Render env var | Direct, unmetered calls to your Azure OpenAI deployments, billed to you |
| `DATABASE_URL` | Render-managed, injected from the Postgres instance | Every key hash, budget, spend row and stored credential |
| `REDIS_URL` | Render-managed, injected from the Key Value instance | Live rate-limit counters and budget reservations; tampering weakens enforcement |
| Customer virtual keys | Operator secret store, then the customer | Model access within that key's budget, RPM, TPM and parallelism |
| Spend and usage records | Postgres | Customer usage patterns; your billing evidence |

---

## Threat model

Threats this deployment is designed against:

1. **A customer escalating to admin.** A virtual key must not reach key
   management, model management, config or admin routes. Verified by
   `scripts/smoke_test.py`.
2. **A customer reaching Azure directly.** Customers never receive Azure
   credentials, the tenant id, the client id or the resource endpoint. The proxy
   is the only path to the Azure OpenAI deployments.
3. **A customer exceeding what they paid for.** Per-key USD budget, RPM, TPM and
   max parallel requests, enforced fail-closed against the database.
4. **One customer affecting another.** Keys are standalone (no shared team), so
   budgets and limits are independent.
5. **Credential leakage through logs, deploy output, Git or error text.**
   `render_start.sh` prints variable names only. Every script routes output
   through a redactor. Prompts and responses are not persisted.
6. **Supply-chain drift.** The image tag is pinned to `v1.99.0`; nothing is
   vendored; upgrades are deliberate.
7. **Silent budget bypass.** No fallbacks, no zero-priced models, no reroute of a
   refused request.
8. **Per-worker limit multiplication.** With 4 worker processes, unshared
   counters would multiply every key's RPM, TPM and budget by 4. Redis holds that
   state, and `render_start.sh` refuses to start more than one worker without it.

Explicitly **not** covered:

- Prompt-injection or content-safety filtering (no guardrails are configured).
- Network isolation. Entra ID is authentication only; the proxy calls the public
  Foundry endpoint. Anyone with the proxy URL can reach it and needs a valid key.
- Customer-side key hygiene once you hand a key over.
- Denial of wallet from a compromised customer key beyond that key's budget
  window.

---

## Customer-key isolation

- Each key is **standalone**: no `team_id`, so no shared budget or shared limit.
- Each key carries its own `max_budget`, `budget_duration`, `rpm_limit`,
  `tpm_limit` and `max_parallel_requests`.
- **Model access is two-layered, and both layers deny by default.** The
  `customer-models` access group is the catalogue of models eligible for customer
  use at all; each key is then issued an explicit subset of that catalogue from
  its profile. A model added to `config.yaml` or the Admin UI without the group
  cannot be granted to anyone, and a model added to the group does not reach any
  existing key whose profile did not name it.
- Enforcement is per request, verified: a key holding
  `["gpt-5.6-sol","gpt-5.6-luna"]` calling `gpt-5.5` receives `403
  key not allowed to access model`, naming only the models it does hold.
- `scripts/create_virtual_keys.py` rejects a profile that requests a model
  outside the catalogue, and rejects unknown profile fields outright so a
  misspelled `max_budget` cannot silently create an unlimited key.
- `all-proxy-models` is a real LiteLLM sentinel that grants **every** model on
  the proxy, from **any** provider, including ones added later. It is
  deliberately unused on customer keys. Do not reintroduce it.
- `scripts/create_virtual_keys.py` refuses to issue keys if no model carries the
  group, and refuses if anything in the group is not Azure-backed (`azure/`) —
  because a non-Azure model in the group means customer traffic on a provider
  billed outside your Azure invoice.
- Adding a model to the group is the privileged act. Verify entitlement, quota, a
  real invocation and pricing first (README sections F and J).
- Keys are stored hashed. `generated-keys.json` is the only plaintext copy and is
  written with owner-only permissions and git-ignored. Move it to your secret
  store and delete the local file.
- Hand each customer only their own key. Never the master key.

## Master-key protection

- Store it only in Render's environment and your secret manager.
- Never in Git, never in a Docker build arg, never in a CI variable that a build
  log could echo, never in a support ticket or chat message.
- It is also the Admin UI credential path — treat it as a root password.
- The operator scripts read it from `LITELLM_MASTER_KEY` and never print it. All
  error text passes through a redactor; unit tests assert the master key cannot
  appear in output or exception messages.
- Rotation: generate a new `sk-` value, set it in Render, redeploy, update your
  own tooling and any admin automation. Existing customer keys are unaffected.

## Salt-key immutability

`LITELLM_SALT_KEY` encrypts provider credentials stored in the database (falling
back to the master key if unset — which is exactly why it must be set). LiteLLM
documents that it must **never change once credentials or models exist**: data
encrypted under the old salt cannot be decrypted under a new one.

- Set it once, before the first deploy.
- Do not put it on a rotation schedule.
- If it must change, that is a planned migration: export what you need, rotate,
  then re-enter every stored provider credential by hand. Expect downtime.

## Entra ID client-secret rotation

Client secrets expire. Rotate before expiry, not after an outage:

1. Entra ID → App registrations → your app → Certificates & secrets → add a
   **second** client secret. Copy the value once.
2. Update `AZURE_CLIENT_SECRET` in Render and redeploy.
3. Confirm `/health/readiness` is `200` and run `scripts/smoke_test.py` with a
   customer key.
4. Only then delete the old secret in Entra ID.
5. Diary the next expiry.

Never reuse the secret anywhere else, and never grant the app more than the
inference role it needs.

## Virtual-key revocation and rotation

**Disable a customer key immediately** (takes effect at once):

```bash
curl -sS -X POST "$LITELLM_BASE_URL/key/block" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "content-type: application/json" \
  -d '{"key": "sk-the-compromised-key"}'
```

Re-enable with `/key/unblock`. Permanently remove with `/key/delete`. Blocking is
the fastest containment step and preserves the spend record; deleting removes the
key row. You can also block a key from the Admin UI.

**Rotate a key** (issue a new value, optionally with a grace period so the
customer can cut over):

```bash
curl -sS -X POST "$LITELLM_BASE_URL/key/sk-the-old-key/regenerate" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "content-type: application/json" \
  -d '{"grace_period": "24h"}'
```

Omit `grace_period` to revoke the old value immediately.

## Datastore protection

Postgres and the Key Value instance are both part of the enforcement path.

- `DATABASE_URL` is injected by Render from the managed instance. Do not copy it
  into a script, a `.env` you share, or a ticket.
- Restrict the database's inbound IP allow list to what you actually need.
- Restrict who in the Render workspace can view the database credentials.
- Back it up and test restores. It holds every key hash, budget and spend row —
  your billing evidence.
- Any local psql session or dump is a copy of that data. Handle and delete
  accordingly.
- The Key Value instance carries `ipAllowList: []`, which blocks every external
  connection: the proxy reaches it over Render's private network only. Do not
  open it to the internet for convenience. It holds live rate-limit and budget
  counters, so write access there is write access to your enforcement.

## Render environment-variable handling

- Every secret is declared `sync: false` in `render.yaml`, so no value is ever
  committed. Render prompts for them during initial Blueprint creation only.
- Later Blueprint updates do **not** re-prompt: add new secrets to the service
  manually and record that you did.
- `sync: false` values are not propagated to preview environments. Do not work
  around this by hardcoding a value.
- Limit who can read Render environment variables and who can trigger deploys.

## No secrets in build args or Git

- The Dockerfile takes exactly one build arg, `LITELLM_VERSION`. Nothing else.
- No secret is `COPY`d, `ARG`d or `ENV`d at build time; the image contains only
  `config.yaml` and `render_start.sh`.
- `.dockerignore` keeps `.env`, `generated-keys.json` and everything else out of
  the build context.
- `.gitignore` covers `.env`, `.env.*` (except `.env.example`) and
  `generated-keys.json`.
- CI fails the build if `generated-keys.json` is tracked, and scans every tracked
  file for key-shaped strings, private-key blocks and credentialed database URLs.
- If a secret ever lands in a commit: rotate it first, then clean history.
  Deleting the file in a later commit does not remove it from history.

## Logging: no prompts, no responses

- `general_settings.store_prompts_in_spend_logs: false` — request and response
  bodies are not persisted on spend rows.
- `litellm_settings.turn_off_message_logging: true` — prompts and completions are
  kept out of logging callbacks.
- `LITELLM_LOG=INFO`, no `set_verbose`, no `detailed_debug`. Debug logging on a
  customer-facing proxy is an exfiltration path for both prompts and secrets.
- If a compliance requirement forces prompt retention, treat those logs as
  customer data: restrict access, set a retention period, and document it in
  your customer agreement first.

## Admin UI protection

- The Admin UI is served by the same service. Confirm it demands admin
  authentication before any customer key exists.
- Do not hand the master key to anyone who only needs read access.
- `allow_public_health_readiness_details: false` keeps the unauthenticated
  readiness payload low-detail; full diagnostics stay behind
  `/health/readiness/details`.
- Options if the UI should not be publicly reachable: set `DISABLE_ADMIN_UI` and
  administer over the API, put the service behind an inbound IP allow list, or
  configure SSO with `UI_USERNAME`/`UI_PASSWORD` or your IdP instead of relying
  on the master key.

## Least-privilege Azure roles

- One dedicated Entra app registration for this proxy. Not a shared app, not a
  human identity.
- Role assignment at **resource scope** on the Foundry / Azure AI Services
  resource: **Cognitive Services OpenAI User** — the least-privilege role that
  permits Entra inference calls and nothing else. Not `Cognitive Services
  Contributor` (cannot do inference), not `Owner`.
- Never Owner, Contributor, User Access Administrator, a subscription or
  management-group scope, or any Entra directory role.
- Review the assignment when you rotate the secret. Remove it when the proxy is
  decommissioned.

---

## Incident response

### A customer key is leaked

1. **Block it now** — `POST /key/block` (command above). Containment before
   investigation.
2. Pull that key's spend and request history from the Admin UI / spend endpoints:
   which models, which windows, how much.
3. Decide whether the budget consumed is refundable or chargeable, and whether
   the leak reached the customer's own systems.
4. Issue a replacement key (a fresh key, or `/key/regenerate`), deliver it over
   your approved channel, then `/key/delete` the old one.
5. Notify the affected customer. Ask them how the key leaked — a key in a public
   repo, a client bundle or a log is a pattern that will repeat.
6. Consider tightening that key's `max_budget`, `rpm_limit` and
   `max_parallel_requests` to shrink the next blast radius.

### The master key is leaked

Treat this as a full compromise of the proxy.

1. **Rotate immediately.** New `sk-` value in Render, redeploy. This locks the
   attacker out of the admin surface.
2. **Assume every key is compromised.** Anyone with the master key could have
   created keys, raised budgets, added models or read all spend.
3. **List every key** and delete or block anything you did not create. Look for
   unexpected aliases, unexpected budgets and unexpected `models` grants.
4. **List every model and every access group.** Remove anything you did not
   configure, and check the `customer-models` membership specifically: adding a
   model to that group is how an attacker with the master key would expose an
   arbitrary provider to every customer key at once.
5. **Check config for tampering:** budgets, rate limits, fallbacks, forwarding
   flags, logging callbacks pointing at an external endpoint.
6. **Rotate the Entra client secret** and review Azure activity for the service
   principal: unexpected volume, unexpected models, unexpected times. Compare
   against LiteLLM spend.
7. **Rotate every customer key** and re-issue.
8. Do **not** rotate the salt key as a reflex — that breaks stored credentials.
   Handle it as the planned migration described above if it is also exposed.
9. Review Render audit/deploy history and GitHub access for how the key escaped.
10. Write it up: what leaked, when, what was reachable, what was rotated, and
    what control stops a repeat.

### General rules

- Never paste a live credential into an issue, a support ticket, a chat or an
  external test service. Reference it by variable name.
- Rotate first, investigate second. Evidence survives rotation; unlimited access
  does not survive delay.
- Share only redacted status and error text when asking for help.

## Reporting

Report suspected vulnerabilities through your organisation's private security
channel. Do not open a public issue containing credentials, prompts, responses,
customer identifiers, database exports or infrastructure access details. Include
reproducible, redacted technical detail.
