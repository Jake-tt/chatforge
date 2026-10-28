import ipaddress
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.errors import ApiError
from app.models import Guest, User
from app.security import InvalidToken, PrincipalKind, decode_token
from app.services.claim import guest_subject, user_subject


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    id: uuid.UUID
    plan: str  # "guest" | "free" | "pro"
    email: str | None = None

    @property
    def subject(self) -> str:
        return user_subject(self.id) if self.kind == "user" else guest_subject(self.id)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def client_ip(request: Request, trusted_proxy_hops: int) -> str:
    """The client address to rate-limit by: the one the outermost trusted proxy saw.

    Each proxy appends to X-Forwarded-For, so the client controls everything left of the
    entries our proxies added. IPv6 is grouped by /64, the block one subscriber usually
    gets, so rotating addresses inside it doesn't open new buckets."""
    peer = request.client.host if request.client else "unknown"
    address = peer
    if trusted_proxy_hops:
        hops = [h.strip() for v in request.headers.getlist("x-forwarded-for")
                for h in v.split(",")]
        hops = [h for h in hops if h]
        # Fewer entries than proxies: the request didn't take the expected path.
        if len(hops) >= trusted_proxy_hops:
            address = hops[-trusted_proxy_hops]
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return peer
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


async def get_session(ctx: AppContext = Depends(get_ctx)) -> AsyncIterator[AsyncSession]:
    async with ctx.sessionmaker() as session:
        yield session


_bearer = HTTPBearer(auto_error=False)


async def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    if creds is None:
        raise ApiError(401, "unauthenticated", "Missing bearer token")
    try:
        claims = decode_token(creds.credentials, ctx.settings.jwt_secret.get_secret_value())
    except InvalidToken as exc:
        raise ApiError(401, "invalid_token", "Invalid or expired token") from exc

    if claims.kind == "user":
        user = await session.get(User, claims.subject_id)
        if user is None:
            raise ApiError(401, "invalid_token", "User no longer exists")
        return Principal("user", user.id, user.plan, user.email)

    guest = await session.get(Guest, claims.subject_id)
    if guest is None:
        raise ApiError(401, "invalid_token", "Guest no longer exists")
    if guest.claimed_by_user_id is not None:
        # The guest's data now belongs to a member; the client should drop this token.
        raise ApiError(401, "guest_claimed", "This guest session was merged into an account")
    return Principal("guest", guest.id, "guest")
