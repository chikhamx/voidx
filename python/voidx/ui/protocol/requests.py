"""Typed request/response DTOs shared by UI frontends."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from voidx.ui.output.events.schema import PermissionToolDetail


class UiRequestBase(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    prompt: str


class UiChoiceRequest(UiRequestBase):
    kind: Literal["choice"] = "choice"
    choices: list[tuple[str, str, str]] = Field(default_factory=list)


class UiTextRequest(UiRequestBase):
    kind: Literal["text"] = "text"
    default: str = ""
    secret: bool = False


class UiPermissionRequest(UiRequestBase):
    kind: Literal["permission"] = "permission"
    choices: list[tuple[str, str, str]] = Field(default_factory=list)
    tools: list[PermissionToolDetail] = Field(default_factory=list)


UiRequest: TypeAlias = Annotated[
    UiChoiceRequest | UiTextRequest | UiPermissionRequest,
    Field(discriminator="kind"),
]
_REQUEST_ADAPTER: TypeAdapter[UiRequest] = TypeAdapter(UiRequest)


class UiResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    value: str | None = None


def parse_ui_request(value: object) -> UiRequest:
    return _REQUEST_ADAPTER.validate_python(value)
