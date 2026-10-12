import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class GuestTokenOut(BaseModel):
    token: str
    guest_id: uuid.UUID


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    # Token of the anonymous session to merge into the account, if any.
    guest_token: str | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    plan: str


class ClaimOut(BaseModel):
    status: str
    conversations_moved: int
    tokens_merged: int


class AuthOut(BaseModel):
    token: str
    user: UserOut
    claim: ClaimOut | None = None


class MeOut(BaseModel):
    kind: str
    id: uuid.UUID
    plan: str
    email: str | None


class UsageOut(BaseModel):
    used: int
    limit: int
    remaining: int
    resets_at: datetime


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    input_tokens: int
    output_tokens: int
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
