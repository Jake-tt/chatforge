from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.deps import Principal, get_ctx, get_principal, get_session
from app.errors import ApiError
from app.models import Guest, User
from app.schemas import AuthOut, ClaimOut, Credentials, GuestTokenOut, MeOut, UserOut
from app.security import InvalidToken, create_token, decode_token, hash_password, verify_password
from app.services.claim import ClaimResult, claim_guest, guest_subject, user_subject

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/guest", response_model=GuestTokenOut, status_code=201)
async def create_guest(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> GuestTokenOut:
    # Guests are free to create, so cap creations per IP; otherwise a script could mint
    # unlimited guests and with them unlimited daily quota.
    ip = request.client.host if request.client else "unknown"
    limit = ctx.settings.guest_creations_per_hour_per_ip
    if await ctx.quota.hit_rate_limit(f"guest-create:{ip}", limit, 3600):
        raise ApiError(429, "rate_limited", "Too many guest sessions from this address")

    guest = Guest()
    session.add(guest)
    await session.commit()
    token = create_token(
        "guest", guest.id, ctx.settings.jwt_secret.get_secret_value(),
        ctx.settings.guest_token_ttl_minutes,
    )
    return GuestTokenOut(token=token, guest_id=guest.id)


@router.post("/signup", response_model=AuthOut, status_code=201)
async def signup(
    body: Credentials,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> AuthOut:
    user = User(email=body.email.lower(), password_hash=hash_password(body.password))
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ApiError(409, "email_taken", "An account with this email already exists") from exc
    return await _finish_auth(ctx, session, user, body.guest_token)


@router.post("/login", response_model=AuthOut)
async def login(
    body: Credentials,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> AuthOut:
    # Emails are stored lowercased, so this hits the unique index.
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    if not verify_password(body.password, user.password_hash if user else None):
        raise ApiError(401, "invalid_credentials", "Email or password is incorrect")
    return await _finish_auth(ctx, session, user, body.guest_token)


async def _finish_auth(
    ctx: AppContext, session: AsyncSession, user: User, guest_token: str | None
) -> AuthOut:
    claim: ClaimResult | None = None
    secret = ctx.settings.jwt_secret.get_secret_value()
    if guest_token:
        try:
            claims = decode_token(guest_token, secret)
        except InvalidToken:
            claims = None  # a stale guest token must never block a login
        if claims and claims.kind == "guest":
            claim = await claim_guest(session, ctx.quota, claims.subject_id, user.id)

    await session.commit()
    if claim and claim.status == "claimed":
        await ctx.quota.invalidate(user_subject(user.id), guest_subject(claims.subject_id))

    token = create_token("user", user.id, secret, ctx.settings.user_token_ttl_minutes)
    return AuthOut(
        token=token,
        user=UserOut.model_validate(user),
        claim=ClaimOut(
            status=claim.status,
            conversations_moved=claim.conversations_moved,
            tokens_merged=claim.tokens_merged,
        ) if claim else None,
    )


@router.get("/me", response_model=MeOut)
async def me(principal: Principal = Depends(get_principal)) -> MeOut:
    return MeOut(kind=principal.kind, id=principal.id, plan=principal.plan, email=principal.email)
