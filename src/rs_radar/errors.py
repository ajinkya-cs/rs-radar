"""Exception hierarchy. Tools surface these messages to the user verbatim, so keep them actionable."""

from __future__ import annotations


class RsRadarError(Exception):
    """Base class for all rs-radar errors."""


class DataSourceError(RsRadarError):
    """An upstream market-data API failed after retries."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UnknownSymbolError(RsRadarError):
    """A symbol could not be resolved to any supported venue."""


class UnsupportedIntervalError(RsRadarError):
    """The provider has no data at the requested interval."""
