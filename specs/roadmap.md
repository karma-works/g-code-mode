# Roadmap: g-code-mode Adapters

**Status:** Living document — updated as adapters ship  
**Last updated:** 2026-05-18

---

## Guiding principle

10 services, 10x deep. Each adapter must have:
- A curated, LLM-legible operation surface (not a raw API mirror)
- Known traps and failure modes baked into implementation
- Full five-layer safety stack on every mutating operation
- Undo action registered before every mutation executes

Breadth without depth loses to the official Google MCP server. Depth with safety wins.

---

## Adapter status

| # | Service | Status | Version | Key risk mitigated |
|---|---|---|---|---|
| 1 | Cloud Run | ✅ Shipped | v0.2 | Traffic splits, env var exposure, region mismatch |
| 2 | Firestore | ✅ Shipped | v0.3 | Document overwrites, index cost, rules deployment |
| 3 | Vertex AI (Agent Engine) | ✅ Shipped | v0.1 | Agent deploy/delete, two-deployment trap |
| 4 | Cloud Storage (GCS) | 🔲 Planned | — | Silent overwrites, retention locks, public access |
| 5 | Pub/Sub | 🔲 Planned | — | Message loss, ordering, dead-letter misconfiguration |
| 6 | BigQuery | 🔲 Planned | — | Cost explosions, partition expiry, dataset deletion |
| 7 | Cloud SQL | 🔲 Planned | — | Instance deletion, backup gaps, connection exhaustion |
| 8 | Secret Manager | 🔲 Planned | — | Version destruction, IAM over-grant, rotation gaps |
| 9 | Cloud Scheduler | 🔲 Planned | — | Timezone traps, idempotency failures, missed runs |
| 10 | IAM | 🔲 Planned | — | Over-grant, privilege escalation, inherited bindings |

---

## Next up: Cloud Storage (GCS)

See `implementation-plan-gcs-adapter.md` for the full plan.

Top pain points driving scope:
- Silent overwrites (`blob.upload_from_filename` with no precondition guard)
- `gsutil rsync -d` wiping destination on source error
- Retention policy lock (permanent, no grace period)
- `allUsers` IAM grant (public exposure, bot-scanned within minutes)
- Uniform bucket-level access silently breaking legacy IAM

---

## Services deliberately out of scope

- **Kubernetes Engine (GKE)**: Too deep, too many footguns in cluster-level ops. Scope creep risk.
- **Cloud Functions**: Overlaps heavily with Cloud Run. Add only if real demand surfaces.
- **Artifact Registry / Container Registry**: Build tooling, not runtime ops. Out of LLM agent scope.
- **VPC / Networking**: Config changes affect all services simultaneously. Too high blast radius for v1.
- **Billing / Cost Management**: Read-only advisory only — no mutations warranted.
