from typing import Optional

from pydantic import BaseModel


class LoginRequest(BaseModel):
    email:    Optional[str] = None
    password: Optional[str] = None


class ReplyRequest(BaseModel):
    message: str
