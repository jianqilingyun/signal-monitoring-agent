from __future__ import annotations

from typing import Protocol

from monitor_agent.core.models import RawItem


class Ingestor(Protocol):
    def ingest(self) -> list[RawItem]:
        ...
