# LLM Learnings: gcloud and Vertex AI Platform

This page records the concrete snags hit while deploying GapHunter through gcloud, Cloud Run, and Vertex AI Agent Engine. It is written for a future coding agent that needs to continue deployment work without rediscovering the same failures.

## 2026-05-16: Agent Engine and Cloud Run Deployment Snags

### 1. `gcloud auth` and ADC are not the same thing

Agent Engine deployment through the Vertex AI Python SDK uses Application Default Credentials, not only the active `gcloud auth login` account.

What worked locally:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project gaphunter-496315
```

Learning:

- Check both `gcloud auth list` and ADC state before blaming Vertex.
- If a Python SDK call fails while `gcloud` CLI commands work, suspect ADC first.
- Document the ADC config path used in automation notes. For this project it was previously under `/tmp/gcloud-config/application_default_credentials.json`.

### 2. Agent Engine service identity needs Firestore access

The Agent Engine runtime writes progress events and final run state directly to Firestore. Cloud Run creates the queued run, but Agent Engine owns the running/completed updates.

Required binding:

```bash
gcloud projects add-iam-policy-binding gaphunter-496315 \
  --member=serviceAccount:service-519220506089@gcp-sa-aiplatform-re.iam.gserviceaccount.com \
  --role=roles/datastore.user
```

Learning:

- If runs reach Agent Engine but no events appear, verify the Reasoning Engine service agent can write Firestore.
- The service account is not the Cloud Run runtime service account. It is the Vertex AI Reasoning Engine service agent.

### 3. GitHub deploy service account needed three separate permission groups

The GitHub Actions deploy service account is:

```text
gaphunter-github-deploy@gaphunter-496315.iam.gserviceaccount.com
```

It needed separate permissions for separate jobs:

- Cloud Run deployment: already had `roles/run.admin`, `roles/artifactregistry.writer`, and `roles/iam.serviceAccountUser`.
- Agent Engine staging bucket: needed bucket metadata/read plus object write access on `gs://gaphunter-agent-engine-staging-519220506089`.
- Vertex Agent Engine create: needed `roles/aiplatform.user` at project scope.

Failure symptoms:

- Missing bucket metadata access failed with `storage.buckets.get access` denied.
- Missing object write would block staging package upload.
- Missing Vertex permission failed with `aiplatform.reasoningEngines.create` denied.

Commands used:

```bash
gcloud storage buckets add-iam-policy-binding gs://gaphunter-agent-engine-staging-519220506089 \
  --member=serviceAccount:gaphunter-github-deploy@gaphunter-496315.iam.gserviceaccount.com \
  --role=roles/storage.legacyBucketReader \
  --project=gaphunter-496315

gcloud storage buckets add-iam-policy-binding gs://gaphunter-agent-engine-staging-519220506089 \
  --member=serviceAccount:gaphunter-github-deploy@gaphunter-496315.iam.gserviceaccount.com \
  --role=roles/storage.objectAdmin \
  --project=gaphunter-496315

gcloud projects add-iam-policy-binding gaphunter-496315 \
  --member=serviceAccount:gaphunter-github-deploy@gaphunter-496315.iam.gserviceaccount.com \
  --role=roles/aiplatform.user
```

Learning:

- Do not grant broad bucket admin by reflex. Bucket reader plus object admin was enough for Agent Engine staging.
- Agent Engine deployment needs both GCS staging permissions and Vertex create permissions.
- The first permission error may hide later ones; fix and rerun until the deploy reaches a real Vertex long-running operation.

### 4. Shell pipelines can hide failed Agent Engine deploys

The first GitHub Agent Engine workflow wrapped deployment in command substitution and piped output to `tail -n 1`. The Python process failed, but the shell pipeline still produced a final requirements log line. The workflow then treated that log line as the resource name.

Bad pattern:

```bash
resource_name="$(python agent/deploy.py ... | tail -n 1)"
```

Fixed pattern:

```bash
set -o pipefail
python agent/deploy.py ... 2>&1 | tee /tmp/agent-engine-deploy.log
resource_name="$(
  grep -E '^projects/[0-9]+/locations/[^/]+/reasoningEngines/[0-9]+$' \
    /tmp/agent-engine-deploy.log \
    | tail -n 1
)"
test -n "$resource_name"
```

Learning:

- Use `set -o pipefail` for deploy workflows that pipe logs.
- Extract resource names by matching the full expected resource pattern, not by taking the last line.
- Never let a workflow update `AGENT_ENGINE_RESOURCE_NAME` unless the value matches `projects/.../locations/.../reasoningEngines/...`.

### 5. GitHub's default token could not update repo variables

The Agent Engine workflow tried to run:

```bash
gh variable set AGENT_ENGINE_RESOURCE_NAME --body "$resource_name"
```

using `GH_TOKEN=${{ github.token }}`. That failed with:

```text
HTTP 403: Resource not accessible by integration
```

Learning:

- The default GitHub Actions token is not reliable for repo variable writes, even with seemingly related workflow permissions.
- Let the workflow print the exact `gh variable set ...` command, then run it locally with an authenticated maintainer token.
- Alternatively, add a purpose-scoped repository secret/PAT only if automated variable mutation is worth the security cost.

### 6. Agent Engine deploy operations can look stuck or be hard to inspect

One Phase 6b deploy attempt produced a long-running operation:

```text
projects/519220506089/locations/us-central1/reasoningEngines/1327115482527956992/operations/941454658029748224
```

It timed out locally after 900 seconds. A later `gcloud ai operations describe` showed no `done`, no `error`, and no response, and no matching build/runtime logs were found. The resource was not listable afterward.

Learning:

- Treat an Agent Engine deploy as unverified until the resource appears in `agent_engines.list()` or the deploy command prints a valid resource name.
- A timed-out local SDK deploy is not necessarily a runtime failure. It may fail or stall before useful build logs exist.
- Record operation names and resource names immediately; they are the only handles for later investigation.

### 7. Production kept pointing to an old Agent Engine resource

Cloud Run was configured with:

```text
AGENT_ENGINE_RESOURCE_NAME=projects/519220506089/locations/us-central1/reasoningEngines/4105273502662131712
```

That resource returned Phase 6a behavior. The Phase 6b code was implemented locally, but production still said competitor coverage was pending because Cloud Run invoked the older Agent Engine resource.

Final Phase 6b resource:

```text
projects/519220506089/locations/us-central1/reasoningEngines/212861602846736384
```

Learning:

- Code deployment and Agent Engine deployment are separate deployments.
- After creating a new Agent Engine, update GitHub variable `AGENT_ENGINE_RESOURCE_NAME`.
- Then redeploy Cloud Run so the new variable is copied into Cloud Run environment.
- Always verify Cloud Run service metadata after deployment:

```bash
gcloud run services describe gaphunter \
  --region=europe-west6 \
  --project=gaphunter-496315 \
  --format='yaml(spec.template.spec.containers[0].env)'
```

### 8. Cloud Run background threads can die after returning HTTP 202

The `AgentEngineGateway` starts a non-daemon background thread and immediately returns `202 queued`. In production, a smoke test stayed `queued` because Cloud Run accepted the request and then shut down the instance before the background thread invoked Agent Engine.

Mitigation applied:

```bash
gcloud run services update gaphunter \
  --region=europe-west6 \
  --project=gaphunter-496315 \
  --min-instances=1 \
  --no-cpu-throttling
```

Workflow flag added:

```text
--min-instances=1 --no-cpu-throttling
```

Learning:

- Cloud Run returning `202` does not mean the background work was durably handed off.
- If a run stays `queued` with no `parsing_constraints` event, inspect Cloud Run logs for shutdown after request completion.
- `min-instances=1` and CPU always allocated are only a bridge. Phase 6d should replace this with Cloud Tasks so Cloud Run enqueues durable work and a task handler invokes Agent Engine synchronously.

### 9. API keys in Cloud Run env are visible in service metadata

The deploy workflow currently passes:

```yaml
BRAVE_SEARCH_API_KEY=${{ secrets.BRAVE_SEARCH_API_KEY }}
```

as a plain Cloud Run environment variable. `gcloud run services describe` then shows the value in service metadata to anyone with sufficient read access.

Learning:

- GitHub secrets protect values inside GitHub, but plain Cloud Run env vars are not Secret Manager references.
- Move provider keys to Google Secret Manager and use Cloud Run secret env bindings.
- Verify that `gcloud run services describe` shows secret references, not raw key values.

### 10. `gcloud` command surfaces are inconsistent

`gcloud ai operations describe` exists, but `gcloud ai operations list` was not available in this environment. The CLI suggested older `gcloud ai-platform operations list` alternatives.

Learning:

- Do not assume `list` exists for every modern `gcloud ai` subcommand group.
- If a `gcloud ai` command surface is missing, use SDK listing, operation names from logs, or the Cloud Console link printed by Agent Engine.
- Capture operation names in CI logs because later discovery can be awkward.

## Current Known-Good State

- Phase 6b Agent Engine resource:
  `projects/519220506089/locations/us-central1/reasoningEngines/212861602846736384`
- GitHub variable `AGENT_ENGINE_RESOURCE_NAME` points to that resource.
- Cloud Run has `AGENT_BACKEND=agent_engine`.
- Cloud Run has `autoscaling.knative.dev/minScale: '1'`.
- Cloud Run has `run.googleapis.com/cpu-throttling: 'false'`.
- Production smoke test completed with run id:
  `348abfe6d5bd437fb955ce88c6c9e7af`
- The run emitted:
  - `parsing_constraints`
  - `researching_jobs`
  - `checking_competitors`
  - `synthesizing_ideas`
- The run completed in about 92 seconds with three Phase 6b idea briefs.

## Recommended Next Actions

1. Implement Phase 6d Cloud Tasks invocation so Cloud Run no longer relies on background threads.
2. Move `BRAVE_SEARCH_API_KEY` and any future provider keys from plain Cloud Run env vars to Secret Manager references.
3. Add a smoke-test workflow that starts a run and asserts that `checking_competitors` appears before considering deployment healthy.
4. Keep Agent Engine deployment separate from Cloud Run deployment, but document the handoff: deploy Agent Engine, update `AGENT_ENGINE_RESOURCE_NAME`, redeploy Cloud Run, smoke test.
