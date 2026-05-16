"""Pre-flight checks run before any GCP mutating operation."""

from __future__ import annotations


def check_adc() -> tuple[bool, str]:
    """Return (ok, error_message). Verifies Application Default Credentials are configured."""
    try:
        import google.auth  # type: ignore[import-untyped]
        import google.auth.exceptions  # type: ignore[import-untyped]

        google.auth.default()
        return True, ""
    except Exception:
        try:
            import google.auth.exceptions as _exc  # type: ignore[import-untyped]

            raise
        except Exception as e:
            return False, (
                f"No Application Default Credentials found ({e}).\n"
                "Run:\n"
                "  gcloud auth application-default login\n"
                "  gcloud auth application-default set-quota-project YOUR_PROJECT_ID"
            )


def require_adc() -> None:
    """Raise ValueError with remediation steps if ADC is not configured."""
    ok, msg = check_adc()
    if not ok:
        raise ValueError(msg)
