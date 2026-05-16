---
name: gcloud-execute
description: Execute a Google Cloud mutation via g-code-mode with full safety stack (undo, snapshot, retry).
disable-model-invocation: true
allowed-tools: mcp__g-code-mode__code mcp__g-code-mode__list_in_flight_operations
argument-hint: "[describe the operation, e.g. 'deploy agent at ./dist to us-central1']"
---

## Active GCP context

- Project: !`gcloud config get-value project 2>/dev/null || echo "not set"`
- ADC:     !`gcloud auth application-default print-access-token >/dev/null 2>&1 && echo "configured" || echo "NOT CONFIGURED"`

## Task

Execute the following Google Cloud operation safely:

$ARGUMENTS

Use the `code` MCP tool. Write `async def run()` that calls the appropriate
adapter function. Return the full result dict.

After the operation succeeds:
1. Show the `undo_recipe` to the user.
2. Show any `warnings` from the result.
3. If a resource_name changed, remind the user to update downstream config.

If ADC is NOT CONFIGURED above, stop immediately and ask the user to run:
  gcloud auth application-default login
