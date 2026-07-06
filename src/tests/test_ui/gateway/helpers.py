"""Shared helpers for gateway v2 tests."""

import json


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_text(self, text: str) -> None:
        self.messages.append(text)


def _parse(msg: str) -> dict:
    return json.loads(msg)


def _method(msg: str) -> str:
    return _parse(msg)["method"]


def _params(msg: str) -> dict:
    return _parse(msg)["params"]
