import hashlib
import hmac
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.deps import (
    Principal,
    client_ip,
    get_ctx,
    get_principal,
    get_session,
    require_member,
)
from app.errors import ApiError
from app.models import Guest, User
from app.schemas import (
    AuthOut,
    ClaimOut,
    Credentials,
    GuestTokenOut,
    MeOut,
    PasswordConfirm,
    UserOut,
)
from app.security import InvalidToken, create_token, decode_token, hash_password, verify_password
from app.services import account
from app.services.claim import ClaimResult, claim_guest, guest_subject, user_subject

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

HOUR = 3600
LOGIN_WINDOW_SECONDS = 15 * 60


def _too_many(ctx: AppContext, message: str, window_seconds: int) -> ApiError:
    retry_after = ctx.quota.window_remaining(window_seconds)
    return ApiError(429, "rate_limited", message, headers={"Retry-After": str(retry_after)})


def _email_bucket(ctx: AppContext, email: str) -> str:
    # Keyed by a keyed digest: a plain hash of an email is reversible with a list of emails.
    secret = ctx.settings.jwt_secret.get_secret_value().encode()
    return "login-fail:" + hmac.new(secret, email.encode(), hashlib.sha256).hexdigest()[:32]


@router.post("/guest", response_model=GuestTokenOut, status_code=201)
async def create_guest(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> GuestTokenOut:
    # Guests are free to create, so cap creations per IP; otherwise a script could mint
    # unlimited guests and with them unlimited daily quota.
    ip = client_ip(request, ctx.settings.trusted_proxy_hops)
    limit = ctx.settings.guest_creations_per_hour_per_ip
    if await ctx.quota.hit_rate_limit(f"guest-create:{ip}", limit, HOUR):
        raise _too_many(ctx, "Too many guest sessions from this address", HOUR)

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
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> AuthOut:
    ip = client_ip(request, ctx.settings.trusted_proxy_hops)
    if await ctx.quota.hit_rate_limit(f"signup:{ip}", ctx.settings.signups_per_hour_per_ip,
                                      HOUR):
        raise _too_many(ctx, "Too many signups from this address", HOUR)
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
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> AuthOut:
    settings = ctx.settings
    # Checked before the password hash: both limits also cap the CPU an attacker can burn.
    ip = client_ip(request, settings.trusted_proxy_hops)
    if await ctx.quota.hit_rate_limit(f"login:{ip}", settings.logins_per_15min_per_ip,
                                      LOGIN_WINDOW_SECONDS):
        raise _too_many(ctx, "Too many login attempts from this address", LOGIN_WINDOW_SECONDS)
    # Emails are stored lowercased, so this hits the unique index.
    email = body.email.lower()
    # Counts every attempt and is cleared on success, so it measures consecutive failures.
    bucket = _email_bucket(ctx, email)
    if await ctx.quota.hit_rate_limit(bucket, settings.login_failures_per_15min_per_email,
                                      LOGIN_WINDOW_SECONDS):
        raise _too_many(ctx, "Too many failed logins for this account", LOGIN_WINDOW_SECONDS)

    user = await session.scalar(select(User).where(User.email == email))
    if not verify_password(body.password, user.password_hash if user else None):
        raise ApiError(401, "invalid_credentials", "Email or password is incorrect")
    await ctx.quota.reset_rate_limit(bucket, LOGIN_WINDOW_SECONDS)
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
        # Cached histories are keyed by owner; the guest's keys are now unreachable.
        await ctx.history.invalidate_owner(guest_subject(claims.subject_id))

    token = create_token("user", user.id, secret, ctx.settings.user_token_ttl_minutes,
                         version=user.token_version)
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


@router.post("/logout-all", status_code=204)
async def logout_all(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Sign out every device, this one included. A stolen token stops working at once."""
    require_member(principal)
    # In SQL, so two concurrent calls both count; either way every older token is dead.
    await session.execute(update(User).where(User.id == principal.id)
                          .values(token_version=User.token_version + 1))
    await session.commit()


@router.delete("/me", status_code=202)
async def delete_account(
    body: PasswordConfirm,
    principal: Principal = Depends(get_principal),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete the account: signed out everywhere and the email freed now, data purged by
    a background task (`services.account`)."""
    require_member(principal)
    # A stolen token alone must not be enough, nor a way to guess the password.
    bucket = _email_bucket(ctx, principal.email)
    if await ctx.quota.hit_rate_limit(bucket, ctx.settings.login_failures_per_15min_per_email,
                                      LOGIN_WINDOW_SECONDS):
        raise _too_many(ctx, "Too many failed attempts for this account", LOGIN_WINDOW_SECONDS)
    user = await session.get(User, principal.id)
    if not verify_password(body.password, user.password_hash):
        raise ApiError(401, "invalid_credentials", "Password is incorrect")
    await ctx.quota.reset_rate_limit(bucket, LOGIN_WINDOW_SECONDS)

    await session.execute(update(User).where(User.id == user.id).values(
        deleted_at=datetime.now(UTC), token_version=User.token_version + 1,
        email=account.tombstone_email(user.id)))
    await session.commit()
    try:
        await ctx.tasks.enqueue(account.TASK_NAME, {"user_id": str(user.id)})
    except Exception:
        # The account is already gone for the user; only the purge is late. The marked
        # row says what's left to purge.
        log.error("could not enqueue account purge", exc_info=True,
                  extra={"user_id": str(user.id)})
