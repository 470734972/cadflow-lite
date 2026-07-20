from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Collector(ABC):
    @abstractmethod
    def collect(self) -> dict[str, list[dict[str, Any]]]:
        raise NotImplementedError

