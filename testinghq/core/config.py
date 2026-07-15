"""Configuration loading for TestingHQ.

Targets are declared explicitly. Secrets come from the environment and are
validated at startup (fail loud on missing or malformed values). This is a
skeleton for M0; the loader is filled in during M1.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class Target:
    """A named, allowed firing target."""

    name: str
    url: str


@dataclass(frozen=True)
class Config:
    """Loaded TestingHQ configuration."""

    targets: Dict[str, Target] = field(default_factory=dict)

    def target_urls(self):
        return [t.url for t in self.targets.values()]
