"""Shared strict base models and identifier constraints (no app import)."""
from typing import Annotated
import uuid


def new_id(prefix):
    """Opaque server-generated identifier; existing supplied IDs remain supported."""
    return prefix + '-' + uuid.uuid4().hex

from pydantic import BaseModel, ConfigDict, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")]
UserName = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}\$?$")]
PROTOCOL = "1.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
