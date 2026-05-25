from dataclasses import dataclass

logger_name = "app.services.retry_policy"


@dataclass
class RetryDecision:
    should_retry: bool
    classification: str  # "transient" | "permanent"
    reason: str


# Permanent patterns are checked FIRST — a device that says "authentication
# failure: connection timeout" should never be retried.
_PERMANENT_PATTERNS: tuple[str, ...] = (
    "invalid vlan id",
    "incomplete command",
    "syntax error",
    "vlan already exists",
    "permission denied",
    "authentication failure",
    "authentication failed",
    "unsupported command",
    "invalid input",
    "invalid command",
    "authorization failed",
    "access denied",
    "ambiguous command",
    "bad command",
    "error: invalid",
)

_TRANSIENT_PATTERNS: tuple[str, ...] = (
    # Spec-required exact phrases
    "ssh timeout",
    "connection timeout",
    "socket timeout",
    "temporary unreachable",
    "ssh connection failed",
    "network_cli timeout",
    "session reset",
    "connection reset",
    "eof during transport",
    "command timeout",
    "ansible persistent connection timeout",
    # Broader patterns that map to the same transient class
    "timeout",
    "timed out",
    "connection refused",
    "unable to connect",
    "ssh failure",
    "ssh error",
    "ssh connect",
    "network is unreachable",
    "no route to host",
    "broken pipe",
    "host unreachable",
    "transport endpoint",
    "reset by peer",
    "end of file",
)


def classify_error(error: str) -> RetryDecision:
    """Classify a plain-text error string as transient (retryable) or permanent.

    Unknown errors default to permanent — never retry blindly.
    """
    if not error or not error.strip():
        return RetryDecision(
            should_retry=False,
            classification="permanent",
            reason="empty error string",
        )

    lowered = error.lower()

    for pattern in _PERMANENT_PATTERNS:
        if pattern in lowered:
            return RetryDecision(
                should_retry=False,
                classification="permanent",
                reason=pattern,
            )

    for pattern in _TRANSIENT_PATTERNS:
        if pattern in lowered:
            return RetryDecision(
                should_retry=True,
                classification="transient",
                reason=pattern,
            )

    return RetryDecision(
        should_retry=False,
        classification="permanent",
        reason="unknown error — defaulting to permanent",
    )
