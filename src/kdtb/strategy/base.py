from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from kdtb.schemas import Disclosure, Extraction, Signal


class Strategy(ABC):
    name: str

    @abstractmethod
    def evaluate(
        self, extraction: Extraction, disclosure: Disclosure
    ) -> Optional[Signal]:
        """Return a candidate Signal, or None if the event doesn't qualify."""
