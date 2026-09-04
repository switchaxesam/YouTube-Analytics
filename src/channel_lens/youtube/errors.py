"""Typed failures for the YouTube layer.

Every one of these carries a message written for the person using the app, not
for a log file. The API layer turns them into an HTTP response with the same
text, so a failure reaches the screen explaining what happened and what to do —
"quota exhausted, resets in 4h 12m" rather than a bare 403.
"""

from __future__ import annotations


class YouTubeError(Exception):
    """Base class. ``hint`` is the actionable half, shown under the message."""

    #: HTTP status the API layer should use when reporting this.
    status_code = 502

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, str]:
        return {"error": type(self).__name__, "message": self.message, "hint": self.hint}


class NotConfigured(YouTubeError):
    """No API key set."""

    status_code = 409

    def __init__(self, message: str = "No YouTube Data API key is configured.") -> None:
        super().__init__(
            message,
            "Add a YouTube Data API v3 key in Settings. It is free — see the setup guide for the four steps.",
        )


class QuotaExceeded(YouTubeError):
    """The request would push today's spend past the configured budget.

    Raised by our own ledger *before* the call goes out, so the budget is a
    real guard rather than a report on damage already done.
    """

    status_code = 429

    def __init__(self, message: str, hint: str = "", *, used: int = 0, budget: int = 0,
                 requested: int = 0, resets_at: str = "") -> None:
        super().__init__(message, hint)
        self.used = used
        self.budget = budget
        self.requested = requested
        self.resets_at = resets_at

    def to_dict(self) -> dict[str, str]:
        data = super().to_dict()
        data.update(
            {
                "used": str(self.used),
                "budget": str(self.budget),
                "requested": str(self.requested),
                "resets_at": self.resets_at,
            }
        )
        return data


class OperationTooExpensive(QuotaExceeded):
    """A single operation's estimated cost exceeds the per-operation cap.

    Distinct from plain quota exhaustion because the fix is different: narrow
    the request, don't wait for the reset.
    """

    status_code = 413


class UpstreamQuotaExceeded(YouTubeError):
    """Google itself returned quotaExceeded.

    Means our ledger has drifted from reality — usually because the same API
    key was used elsewhere. The ledger is reconciled to full when this is seen.
    """

    status_code = 429


class InvalidCredentials(YouTubeError):
    """Key rejected, or the Data API isn't enabled on the project."""

    status_code = 401


class NotFound(YouTubeError):
    """Channel, video, or playlist doesn't exist, or is private."""

    status_code = 404


class TransientUpstreamError(YouTubeError):
    """A 5xx or a network failure. Worth retrying; the client already has."""

    status_code = 503
