"""Commands sent from interactive frontends to the agent core."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_serializer


class UiSubmitCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["submit"] = "submit"
    text: str
    thread_id: str = ""

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if not self.thread_id:
            data.pop("thread_id", None)
        return data


class UiCancelCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["cancel"] = "cancel"
    thread_id: str = ""

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if not self.thread_id:
            data.pop("thread_id", None)
        return data


UiCommand: TypeAlias = Annotated[
    UiSubmitCommand | UiCancelCommand,
    Field(discriminator="kind"),
]
_COMMAND_ADAPTER: TypeAdapter[UiCommand] = TypeAdapter(UiCommand)


def parse_ui_command(value: object) -> UiCommand:
    return _COMMAND_ADAPTER.validate_python(value)
