from dataclasses import dataclass


@dataclass
class RetryDecision:
    should_retry: bool
    classification: str  # "transient" | "permanent"
    reason: str
