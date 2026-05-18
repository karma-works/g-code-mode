"""Unit tests for the gap reporting module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from g_code_mode.reporting import (
    _derive_title,
    format_issue_body,
    get_version,
    report_gap,
    search_duplicates,
)


# ── get_version ───────────────────────────────────────────────────────────────


def test_get_version_installed():
    with patch("importlib.metadata.version", return_value="0.3.0"):
        assert get_version() == "0.3.0"


def test_get_version_not_installed():
    import importlib.metadata

    with patch("importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError):
        assert get_version() == "unknown"


# ── _derive_title ─────────────────────────────────────────────────────────────


def test_derive_title_short():
    title = _derive_title("Deploy Cloud Run", "No service_account parameter")
    assert title == "[gap] No service_account parameter"


def test_derive_title_long_gap_truncated():
    long_gap = "A" * 100
    title = _derive_title("Deploy Cloud Run", long_gap)
    assert title.startswith("[gap] ")
    assert len(title) <= len("[gap] ") + 72


def test_derive_title_uses_gap_not_operation():
    title = _derive_title("Operation text", "Gap text")
    assert "Gap text" in title
    assert "Operation text" not in title


# ── format_issue_body ─────────────────────────────────────────────────────────


def test_format_issue_body_contains_all_fields():
    body = format_issue_body(
        operation_attempted="Deploy Cloud Run revision",
        gap_description="No service_account parameter",
        workaround_used="Used gcloud directly",
        suggestion="Add service_account param",
        severity="medium",
        llm_model="claude-sonnet-4-6",
        version="0.3.0",
        report_time="2026-05-18T10:00:00Z",
    )
    assert "Deploy Cloud Run revision" in body
    assert "No service_account parameter" in body
    assert "Used gcloud directly" in body
    assert "Add service_account param" in body
    assert "medium" in body
    assert "claude-sonnet-4-6" in body
    assert "0.3.0" in body
    assert "2026-05-18T10:00:00Z" in body


def test_format_issue_body_empty_workaround():
    body = format_issue_body(
        operation_attempted="op",
        gap_description="gap",
        workaround_used="",
        suggestion="",
        severity="high",
        llm_model="",
        version="0.1.0",
        report_time="2026-01-01T00:00:00Z",
    )
    assert "_None — blocked entirely._" in body


def test_format_issue_body_empty_suggestion():
    body = format_issue_body(
        operation_attempted="op",
        gap_description="gap",
        workaround_used="",
        suggestion="",
        severity="low",
        llm_model="",
        version="0.1.0",
        report_time="2026-01-01T00:00:00Z",
    )
    assert "_No specific suggestion._" in body


def test_format_issue_body_contains_privacy_checklist():
    body = format_issue_body(
        operation_attempted="op",
        gap_description="gap",
        workaround_used="",
        suggestion="",
        severity="low",
        llm_model="",
        version="0.1.0",
        report_time="2026-01-01T00:00:00Z",
    )
    assert "Privacy Checklist" in body
    assert "Project IDs" in body


def test_format_issue_body_unknown_model():
    body = format_issue_body(
        operation_attempted="op",
        gap_description="gap",
        workaround_used="",
        suggestion="",
        severity="low",
        llm_model="",
        version="0.1.0",
        report_time="2026-01-01T00:00:00Z",
    )
    assert "not reported" in body


# ── search_duplicates ─────────────────────────────────────────────────────────


def test_search_duplicates_success():
    fake_output = json.dumps([
        {"number": 1, "title": "No service_account support", "url": "https://github.com/..."},
    ])
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = fake_output

    with patch("subprocess.run", return_value=mock_proc):
        results = search_duplicates("Deploy Cloud Run", "No service_account")

    assert len(results) == 1
    assert results[0]["number"] == 1


def test_search_duplicates_gh_not_installed():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        results = search_duplicates("op", "gap")
    assert results == []


def test_search_duplicates_timeout():
    import subprocess

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 15)):
        results = search_duplicates("op", "gap")
    assert results == []


def test_search_duplicates_bad_json():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "not json"

    with patch("subprocess.run", return_value=mock_proc):
        results = search_duplicates("op", "gap")
    assert results == []


def test_search_duplicates_empty_output():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = ""

    with patch("subprocess.run", return_value=mock_proc):
        results = search_duplicates("op", "gap")
    assert results == []


def test_search_duplicates_nonzero_exit():
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""

    with patch("subprocess.run", return_value=mock_proc):
        results = search_duplicates("op", "gap")
    assert results == []


# ── report_gap — dry run ──────────────────────────────────────────────────────


def test_report_gap_dry_run_returns_expected_keys():
    with patch("g_code_mode.reporting.get_version", return_value="0.3.0"), \
         patch("g_code_mode.reporting.search_duplicates", return_value=[]):
        result = report_gap(
            operation_attempted="Deploy Cloud Run with custom SA",
            gap_description="No service_account parameter in deploy_revision",
            workaround_used="gcloud CLI",
            suggestion="Add service_account param",
            severity="medium",
            llm_model="claude-sonnet-4-6",
            submit=False,
        )

    assert result["dry_run"] is True
    assert "title" in result
    assert "issue_body" in result
    assert "duplicate_candidates" in result
    assert "version" in result
    assert "report_time" in result
    assert "privacy_reminder" in result
    assert "next_step" in result
    assert "issue_url" not in result


def test_report_gap_dry_run_contains_version():
    with patch("g_code_mode.reporting.get_version", return_value="1.2.3"), \
         patch("g_code_mode.reporting.search_duplicates", return_value=[]):
        result = report_gap(
            operation_attempted="op",
            gap_description="gap",
            submit=False,
        )
    assert result["version"] == "1.2.3"
    assert "1.2.3" in result["issue_body"]


def test_report_gap_dry_run_with_duplicates_adds_warning():
    duplicates = [{"number": 7, "title": "Similar gap", "url": "https://github.com/..."}]
    with patch("g_code_mode.reporting.get_version", return_value="0.3.0"), \
         patch("g_code_mode.reporting.search_duplicates", return_value=duplicates):
        result = report_gap(
            operation_attempted="op",
            gap_description="gap",
            submit=False,
        )
    assert "duplicate_warning" in result
    assert result["duplicate_candidates"] == duplicates


def test_report_gap_invalid_severity_coerced():
    with patch("g_code_mode.reporting.get_version", return_value="0.3.0"), \
         patch("g_code_mode.reporting.search_duplicates", return_value=[]):
        result = report_gap(
            operation_attempted="op",
            gap_description="gap",
            severity="INVALID",
            submit=False,
        )
    assert "medium" in result["issue_body"]


# ── report_gap — submit ───────────────────────────────────────────────────────


def test_report_gap_submit_success():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "https://github.com/karma-works/g-code-mode/issues/42\n"

    with patch("g_code_mode.reporting.get_version", return_value="0.3.0"), \
         patch("g_code_mode.reporting.search_duplicates", return_value=[]), \
         patch("subprocess.run", return_value=mock_proc):
        result = report_gap(
            operation_attempted="op",
            gap_description="gap",
            submit=True,
        )

    assert result["submitted"] is True
    assert result["dry_run"] is False
    assert "github.com" in result["issue_url"]


def test_report_gap_submit_gh_not_installed():
    with patch("g_code_mode.reporting.get_version", return_value="0.3.0"), \
         patch("g_code_mode.reporting.search_duplicates", return_value=[]), \
         patch("subprocess.run", side_effect=FileNotFoundError):
        result = report_gap(
            operation_attempted="op",
            gap_description="gap",
            submit=True,
        )

    assert result["submitted"] is False
    assert "gh CLI not found" in result["submit_error"]
    assert "cli.github.com" in result["submit_error"]


def test_report_gap_submit_gh_fails():
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = ""
    mock_proc.stderr = "gh auth error: token not found"

    with patch("g_code_mode.reporting.get_version", return_value="0.3.0"), \
         patch("g_code_mode.reporting.search_duplicates", return_value=[]), \
         patch("subprocess.run", return_value=mock_proc):
        result = report_gap(
            operation_attempted="op",
            gap_description="gap",
            submit=True,
        )

    assert result["submitted"] is False
    assert "token not found" in result["submit_error"]


def test_report_gap_submit_timeout():
    import subprocess

    with patch("g_code_mode.reporting.get_version", return_value="0.3.0"), \
         patch("g_code_mode.reporting.search_duplicates", return_value=[]), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 30)):
        result = report_gap(
            operation_attempted="op",
            gap_description="gap",
            submit=True,
        )

    assert result["submitted"] is False
    assert "timed out" in result["submit_error"]
