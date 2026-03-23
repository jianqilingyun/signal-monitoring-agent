class MonitorAgentError(Exception):
    """Base exception for the monitoring system."""


class IngestionError(MonitorAgentError):
    """Raised when ingestion fails for all configured sources."""


class ExtractionError(MonitorAgentError):
    """Raised when signal extraction fails."""


class StorageError(MonitorAgentError):
    """Raised when storage operations fail."""


class NotificationError(MonitorAgentError):
    """Raised when all notification channels fail."""
