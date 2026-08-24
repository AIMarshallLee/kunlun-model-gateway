"""Public exceptions shared by provider adapters and the API layer."""

from __future__ import annotations


class ProviderError(RuntimeError):
    """A sanitized upstream failure; response bodies and prompts are never retained."""

    def __init__(
        self,
        status_code: int,
        *,
        category: str | None = None,
        safe_to_failover: bool | None = None,
        request_may_be_billable: bool | None = None,
    ) -> None:
        self.status_code = int(status_code)
        self.category = category or f"provider_http_{status_code}"
        if safe_to_failover is None:
            # A provider 5xx can be returned after the request has already
            # been accepted and billed. Only an explicit adapter contract may
            # override this conservative default. Rate-limit rejection is the
            # sole generic HTTP response considered safe to route elsewhere.
            safe_to_failover = status_code == 429
        if request_may_be_billable is None:
            request_may_be_billable = status_code >= 500
        self.safe_to_failover = bool(safe_to_failover)
        self.request_may_be_billable = bool(request_may_be_billable)
        super().__init__(self.category)
