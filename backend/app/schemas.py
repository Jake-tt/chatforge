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


class PasswordConfirm(BaseModel):
    password: str = Field(max_length=128)


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


class CitationOut(BaseModel):
    n: int
    title: str
    url: str
    snippet: str
    cited: bool = True  # whether the answer cites it; rows stored before the check: yes


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    kind: str


class AttachmentUploaded(AttachmentOut):
    chars: int  # length of the Markdown it became


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    kind: str = "answer"
    content: str
    # Offered with a clarifying question ([] asks for free text); None for an answer.
    options: list[str] | None = None
    input_tokens: int
    output_tokens: int
    citations: list[CitationOut] | None = None
    attachments: list[AttachmentOut] = []
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class BulkDeleteOut(BaseModel):
    deleted: int


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    # Uploaded to this conversation first (POST .../attachments), each sent once.
    attachment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=4)
