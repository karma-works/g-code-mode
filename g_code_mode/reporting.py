"""
Gap reporting for g-code-mode.

Lets the calling LLM file a structured GitHub issue when it encounters a
capability gap — something g-code-mode doesn't handle, an unhelpful error,
or a GCP behaviour the adapters don't warn about.

Always call with submit=False first (dry run) so the user can review the
formatted body, check duplicate candidates, and strip PII before the issue
is created.
"""

from __future__ import annotations

import importlib.metadata
import json
import subprocess
from datetime import datetime, timezone
from typing import Any

_REPO = "karma-works/g-code-mode"
_LABELS = "llm-report,gap"
_SEVERITY_LABELS = {"high": "priority:high", "medium": "priority:medium", "low": "priority:low"}
_DUPLICATE_LIMIT = 5
_GH_TIMEOUT = 15


def get_version() -> str:
    try:
        return importlib.metadata.version("g-code-mode")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def search_duplicates(operation: str, gap: str) -> list[dict[str, Any]]:
    """Search open GitHub issues for likely duplicates. Returns [] on any failure."""
    query = f"{operation[:40]} {gap[:40]}".strip()
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", _REPO,
                "--search", query,
                "--state", "open",
                "--json", "number,title,url",
                "--limit", str(_DUPLICATE_LIMIT),
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def _format_severity_label(severity: str) -> str:
    return _SEVERITY_LABELS.get(severity.lower(), _SEVERITY_LABELS["medium"])


def format_issue_body(
    *,
    operation_attempted: str,
    gap_description: str,
    workaround_used: str,
    suggestion: str,
    severity: str,
    llm_model: str,
    version: str,
    report_time: str,
) -> str:
    workaround_text = workaround_used.strip() or "_None — blocked entirely._"
    suggestion_text = suggestion.strip() or "_No specific suggestion._"
    severity_display = severity.lower()

    return f"""\
## Operation Attempted

{operation_attempted.strip()}

## Gap / Missing Capability

{gap_description.strip()}

## Workaround Used

{workaround_text}

## Suggestion

{suggestion_text}

## Metadata

| Field | Value |
|---|---|
| g-code-mode version | `{version}` |
| Report time (UTC) | `{report_time}` |
| LLM model | `{llm_model.strip() or "not reported"}` |
| Severity | `{severity_display}` |

## Privacy Checklist

<!-- Complete before submitting -->
- [ ] Project IDs removed or replaced with `my-project`
- [ ] Resource names removed or anonymized
- [ ] Service account emails removed
- [ ] No API keys, tokens, or credentials included

---
*Reported via g-code-mode `/report` skill*
"""


def _derive_title(operation_attempted: str, gap_description: str) -> str:
    # Prefer gap_description for the title since it's more specific than the operation
    base = gap_description.strip()
    if len(base) > 72:
        base = base[:69] + "..."
    return f"[gap] {base}"


def report_gap(
    operation_attempted: str,
    gap_description: str,
    workaround_used: str = "",
    suggestion: str = "",
    severity: str = "medium",
    llm_model: str = "",
    submit: bool = False,
) -> dict[str, Any]:
    """
    Core reporting logic. Returns a dict with:
      - title, issue_body, duplicate_candidates
      - version, report_time
      - dry_run, privacy_reminder
      - issue_url, submitted (only when submit=True)
    """
    if severity.lower() not in ("low", "medium", "high"):
        severity = "medium"

    version = get_version()
    report_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = _derive_title(operation_attempted, gap_description)
    body = format_issue_body(
        operation_attempted=operation_attempted,
        gap_description=gap_description,
        workaround_used=workaround_used,
        suggestion=suggestion,
        severity=severity,
        llm_model=llm_model,
        version=version,
        report_time=report_time,
    )
    duplicates = search_duplicates(operation_attempted, gap_description)

    result: dict[str, Any] = {
        "title": title,
        "issue_body": body,
        "duplicate_candidates": duplicates,
        "version": version,
        "report_time": report_time,
        "dry_run": not submit,
        "privacy_reminder": (
            "Before approving: scan the issue body for project IDs, resource names, "
            "service account emails, and API keys. Replace with placeholders like "
            "'my-project' or 'my-service'. Only submit after the privacy checklist "
            "in the body is complete."
        ),
    }

    if duplicates:
        result["duplicate_warning"] = (
            f"{len(duplicates)} possible duplicate(s) found. Review them before "
            "submitting — commenting on an existing issue is more useful than a duplicate."
        )

    if submit:
        labels = f"{_LABELS},{_format_severity_label(severity)}"
        try:
            proc = subprocess.run(
                [
                    "gh", "issue", "create",
                    "--repo", _REPO,
                    "--title", title,
                    "--body", body,
                    "--label", labels,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                result["issue_url"] = proc.stdout.strip()
                result["submitted"] = True
            else:
                result["submitted"] = False
                result["submit_error"] = proc.stderr.strip() or "gh issue create failed"
        except subprocess.TimeoutExpired:
            result["submitted"] = False
            result["submit_error"] = "gh CLI timed out — check your network and try again"
        except FileNotFoundError:
            result["submitted"] = False
            result["submit_error"] = (
                "gh CLI not found. Install it: https://cli.github.com/\n"
                "Then authenticate: gh auth login\n\n"
                "You can also file the issue manually at:\n"
                f"https://github.com/{_REPO}/issues/new?template=llm-report.yml"
            )
    else:
        result["next_step"] = (
            "Review the issue body and duplicate candidates above. "
            "When ready, call report_gap again with submit=True."
        )

    return result
