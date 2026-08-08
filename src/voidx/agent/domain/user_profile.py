"""User prompt preferences owned by the agent domain."""

from pydantic import BaseModel


class UserProfile(BaseModel):
    language: str = ""
    tone: str = ""
