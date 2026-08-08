"""Web tool routing configuration."""

from pydantic import BaseModel


class WebToolRoute(BaseModel):
    backend: str = "legacy"
    server: str = ""
    tool: str = ""
