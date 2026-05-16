"""Tests for ADC preflight checks."""

import pytest

from g_code_mode.preflight import check_adc, require_adc


def test_require_adc_raises_when_missing(monkeypatch):
    """Simulate missing ADC credentials."""

    def _raise(*_a, **_kw):
        raise Exception("no credentials")

    monkeypatch.setattr("google.auth.default", _raise)
    with pytest.raises(ValueError, match="Application Default Credentials"):
        require_adc()


def test_check_adc_returns_false_when_missing(monkeypatch):
    def _raise(*_a, **_kw):
        raise Exception("no credentials")

    monkeypatch.setattr("google.auth.default", _raise)
    ok, msg = check_adc()
    assert not ok
    assert "gcloud auth application-default login" in msg
