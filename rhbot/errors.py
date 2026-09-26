"""Exceptions. None of these represent a live-broker call."""


class ConfigError(ValueError):
    """Config is missing or tries to loosen a hard cap."""


class DataError(RuntimeError):
    """Market data was missing, malformed, or unusable."""


class OrderRejected(Exception):
    """Risk or the paper broker refused a simulated order."""

    def __init__(self, reasons: list[str], kill: bool = False):
        self.reasons = list(reasons)
        self.kill = kill
        super().__init__("; ".join(self.reasons) or "rejected")


class LiveTradingDisabled(RuntimeError):
    """Real orders are not implemented. Paper mode is the only mode."""


class ReadOnlyViolation(RuntimeError):
    """The quote client was asked to do something other than a read."""


class RateLimitError(RuntimeError):
    """The read budget (100 requests/minute) is exhausted."""


class ClockSkewError(RuntimeError):
    """Local clock is too far from the server to sign a read safely."""
