import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

PrincipalKind = Literal["user", "guest"]

_hasher = PasswordHasher()
# Verified against when the email doesn't exist, so login timing doesn't reveal which emails exist.
_DUMMY_HASH = _hasher.hash("timing-equaliser")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


@dataclass(frozen=True)
class TokenClaims:
    kind: PrincipalKind
    subject_id: uuid.UUID
    version: int = 0  # users.token_version when issued; tokens from before versioning: 0


class InvalidToken(Exception):
    pass


def create_token(kind: PrincipalKind, subject_id: uuid.UUID, secret: str, ttl_minutes: int,
                 *, version: int = 0) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(subject_id),
        "kind": kind,
        "ver": version,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp"]})
        kind = payload["kind"]
        if kind not in ("user", "guest"):
            raise InvalidToken("unknown token kind")
        return TokenClaims(kind=kind, subject_id=uuid.UUID(payload["sub"]),
                           version=int(payload.get("ver", 0)))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidToken(str(exc)) from exc
