from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.deps import Principal, get_ctx, get_principal, get_session
from app.schemas import UsageOut

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("", response_model=UsageOut)
async def get_usage(
    principal: Principal = Depends(get_principal),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> UsageOut:
    status = await ctx.quota.status(
        session, principal.subject, ctx.settings.daily_token_limit(principal.plan)
    )
    return UsageOut(
        used=status.used, limit=status.limit, remaining=status.remaining,
        resets_at=status.resets_at,
    )
