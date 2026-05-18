---
name: report
description: Report a new GCP gap or missing capability in g-code-mode
allowed-tools: mcp__g-code-mode__report_gap
---

You are helping the user file a gap report for g-code-mode. A gap is something
g-code-mode doesn't handle well: a missing operation, an unhelpful error, a GCP
behaviour the adapters don't warn about, or anything that forced a workaround.

**One gap per report.** If the user mentions multiple problems, handle them one
at a time. File each as a separate issue.

## Step 1 — Gather information

Ask the user these four questions. Ask them together if the gap is obvious from
context, or one at a time if it isn't. Do not proceed until you have answers.

1. **What GCP operation were you trying to perform?**
   *(e.g. "Deploy a Cloud Run revision with a custom service account")*

2. **What was missing or broken in g-code-mode?**
   *(Focus on the tool's gap, not what GCP did wrong.)*
   *(e.g. "deploy_revision has no service_account parameter")*

3. **What workaround did you use, if any?**
   *(e.g. "Fell back to gcloud run deploy --service-account=..." or "Blocked entirely")*

4. **What would have made this easy?**
   *(e.g. "Add a service_account parameter to deploy_revision")*

Also ask: **How severe was the impact?**
- `low` — friction but I worked around it easily
- `medium` — significant workaround needed, took extra time
- `high` — completely blocked, could not complete the task

## Step 2 — Preview the report

Call `mcp__g-code-mode__report_gap` with `submit=False` and these parameters:
- `operation_attempted` — answer to question 1
- `gap_description` — answer to question 2
- `workaround_used` — answer to question 3 (empty string if none)
- `suggestion` — answer to question 4 (empty string if unknown)
- `severity` — low / medium / high
- `llm_model` — your own model name (e.g. "claude-sonnet-4-6")
- `submit` — false

Show the user:
- The full `issue_body` (render it as Markdown)
- The `title`
- Any `duplicate_candidates` with their URLs
- The `privacy_reminder`

## Step 3 — Duplicate check

If `duplicate_candidates` is non-empty, say:

> "I found **N** open issue(s) that might cover this gap. Please review them
> before we create a new one — a comment on an existing issue is more useful
> than a duplicate."

List each candidate with its number, title, and URL. Ask the user:
> "Does any of these cover your issue, or should we proceed with a new report?"

Only proceed if the user confirms the gap is new or sufficiently different.

## Step 4 — Privacy review

Ask the user to check the issue body for:
- Project IDs → replace with `my-project`
- Resource names → replace with `my-service` / `my-resource`
- Service account emails → remove
- API keys, tokens, credentials → remove

Say:
> "The issue body is shown above. Please check the privacy checklist —
> project IDs, resource names, and service accounts should be removed.
> Does anything need to be changed before I submit?"

If the user requests changes, incorporate them and show the updated body.

## Step 5 — Submit

Only call `mcp__g-code-mode__report_gap` with `submit=True` after the user
explicitly says to go ahead ("yes", "submit", "looks good", "go ahead" etc.).

On success, show the `issue_url` so the user can follow the issue.

If submission fails (gh not installed, auth error), show the full `issue_body`
and the manual filing URL so the user can file it themselves.

## Rules

- **Never call submit=True without explicit user approval.**
- **One gap per report.** File them separately.
- If gh is not installed, the dry-run body is still useful — show it so the
  user can copy-paste into a GitHub issue manually.
- Do not add your own commentary to the issue body. The body is what gets filed.
