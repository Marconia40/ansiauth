"""Unit tests for the retry policy engine (error classification only)."""
import pytest

from app.services.retry_policy import RetryDecision, classify_error


# ── Return type ───────────────────────────────────────────────────────────────

def test_returns_retry_decision():
    result = classify_error("ssh timeout")
    assert isinstance(result, RetryDecision)
    assert hasattr(result, "should_retry")
    assert hasattr(result, "classification")
    assert hasattr(result, "reason")


# ── Transient (retryable) patterns ────────────────────────────────────────────

@pytest.mark.parametrize("error", [
    "SSH timeout connecting to device",
    "connection timeout after 30s",
    "socket timeout waiting for response",
    "Host is temporary unreachable",
    "SSH connection failed: refused",
    "network_cli timeout waiting for command",
    "Session reset by remote peer",
    "Connection reset by peer",
    "EOF during transport layer negotiation",
    "command timeout: no response within 60s",
    "ansible persistent connection timeout",
    # broader catches
    "Read timed out",
    "Connection refused on port 22",
    "Unable to connect to switch-01",
    "SSH failure during handshake",
    "SSH error: key exchange failed",
    "ssh connect: no route to host",
    "network is unreachable (101)",
    "No route to host",
    "Broken pipe",
    "Host unreachable",
    "Transport endpoint is not connected",
    "Reset by peer",
    "End of file on stdin",
])
def test_transient_errors(error):
    decision = classify_error(error)
    assert decision.should_retry is True, f"Expected transient for: {error!r}"
    assert decision.classification == "transient"
    assert decision.reason  # must give a non-empty reason


# ── Permanent (non-retryable) patterns ───────────────────────────────────────

@pytest.mark.parametrize("error", [
    "invalid vlan id: 5000",
    "Incomplete command at '^' marker",
    "Syntax error detected",
    "VLAN already exists on this device",
    "Permission denied (publickey)",
    "Authentication failure for user admin",
    "Authentication failed: bad credentials",
    "Unsupported command in this context",
    "Invalid input detected at '^'",
    "Authorization failed for user",
    "Access denied by ACL",
    "Ambiguous command: 'sh'",
    "Bad command or filename",
    "error: invalid parameter",
])
def test_permanent_errors(error):
    decision = classify_error(error)
    assert decision.should_retry is False, f"Expected permanent for: {error!r}"
    assert decision.classification == "permanent"
    assert decision.reason


# ── Unknown errors default to permanent ──────────────────────────────────────

@pytest.mark.parametrize("error", [
    "something completely unknown happened",
    "unexpected output from device",
    "module not found",
    "segmentation fault",
])
def test_unknown_errors_default_to_permanent(error):
    decision = classify_error(error)
    assert decision.should_retry is False
    assert decision.classification == "permanent"


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_string_is_permanent():
    assert classify_error("").classification == "permanent"
    assert classify_error("").should_retry is False


def test_whitespace_only_is_permanent():
    assert classify_error("   ").classification == "permanent"
    assert classify_error("   ").should_retry is False


def test_case_insensitive_transient():
    assert classify_error("SSH TIMEOUT").should_retry is True
    assert classify_error("Connection Reset").should_retry is True
    assert classify_error("TIMED OUT").should_retry is True


def test_case_insensitive_permanent():
    assert classify_error("SYNTAX ERROR").should_retry is False
    assert classify_error("PERMISSION DENIED").should_retry is False
    assert classify_error("INVALID INPUT").should_retry is False


# ── Permanent takes priority over transient when both match ──────────────────

def test_permanent_overrides_transient():
    """An error mentioning both 'authentication failure' and 'timeout' must
    be classified as permanent — authentication errors should not be retried."""
    error = "authentication failure: connection timeout exceeded"
    decision = classify_error(error)
    assert decision.should_retry is False
    assert decision.classification == "permanent"


# ── reason field is always populated ─────────────────────────────────────────

def test_reason_populated_for_transient():
    decision = classify_error("ssh timeout")
    assert "ssh timeout" in decision.reason


def test_reason_populated_for_permanent():
    decision = classify_error("syntax error at line 3")
    assert "syntax error" in decision.reason


def test_reason_populated_for_unknown():
    decision = classify_error("totally unknown problem")
    assert decision.reason  # non-empty, mentions defaulting behaviour
