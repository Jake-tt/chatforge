"""What the assistant remembers about a member, and the member's controls over it."""

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import Principal, get_principal, get_session, require_member
from app.errors import ApiError
from app.schemas import MemoryFactOut, MemoryOut, MemorySettings
from app.services import memory

router = APIRouter(prefix="/memory", tags=["memory"])


async def _member(principal: Principal = Depends(get_principal)) -> Principal:
    require_member(principal)  # guests keep no memory
    return principal


@router.get("", response_model=MemoryOut)
async def get_memory(principal: Principal = Depends(_member),
                     session: AsyncSession = Depends(get_session)) -> MemoryOut:
    facts = await memory.list_facts(session, principal.id)
    return MemoryOut(enabled=await memory.is_enabled(session, principal.id),
                     facts=[MemoryFactOut(id=f.id, subject=f.subject, predicate=f.predicate,
                                          object=f.object, updated_at=f.updated_at)
                            for f in facts])


@router.delete("/{fact_id}", status_code=204)
async def delete_fact(fact_id: uuid.UUID, principal: Principal = Depends(_member),
                      session: AsyncSession = Depends(get_session)) -> Response:
    if not await memory.delete_fact(session, principal.id, fact_id):
        raise ApiError(404, "not_found", "Memory not found")
    await session.commit()
    return Response(status_code=204)


@router.delete("", status_code=204)
async def delete_all(principal: Principal = Depends(_member),
                     session: AsyncSession = Depends(get_session)) -> Response:
    await memory.delete_all(session, principal.id)
    await session.commit()
    return Response(status_code=204)


@router.put("/settings", response_model=MemorySettings)
async def put_settings(body: MemorySettings, principal: Principal = Depends(_member),
                       session: AsyncSession = Depends(get_session)) -> MemorySettings:
    # Turning it off stops new memories and their use; what's stored stays until deleted.
    await memory.set_enabled(session, principal.id, body.enabled)
    await session.commit()
    return body
