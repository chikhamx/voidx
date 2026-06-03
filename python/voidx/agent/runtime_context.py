"""Runtime context shim — thin compat for interaction mode enum."""

from enum import Enum


class InteractionMode(str, Enum):
    AUTO = "auto"
    PLAN = "plan"

    @staticmethod
    def parse(value: str) -> "InteractionMode":
        if value and value.lower() == "plan":
            return InteractionMode.PLAN
        return InteractionMode.AUTO
