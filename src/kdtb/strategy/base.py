from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from kdtb.schemas import Extraction, Signal


class Strategy(ABC):
    name: str

    @abstractmethod
    def evaluate(self, extraction: Extraction) -> Optional[Signal]:
        """Return a candidate Signal, or None if no signal."""
