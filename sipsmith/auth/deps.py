"""FastAPI dependency injection for authentication."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sipsmith.auth.service import decode_access_token, hash_pat
from sipsmith.database import get_db
from sipsmith.models.user import PersonalAccessToken, Role, User


async def _user_from_token(token: str, db: AsyncSession) -> User | None:
    username = decode_access_token(token)
    if not username:
        return None
    result = await db.execute(select(User).where(User.username == username, User.is_active))
    return result.scalar_one_or_none()


async def _user_from_pat(raw_token: str, db: AsyncSession) -> User | None:
    hashed = hash_pat(raw_token)
    result = await db.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.token_hash == hashed)
    )
    pat = result.scalar_one_or_none()
    if pat is None:
        return None
    if pat.expires_at and pat.expires_at < datetime.now(UTC):
        return None
    # Update last_used
    pat.last_used_at = datetime.now(UTC)
    await db.commit()
    user_result = await db.execute(select(User).where(User.id == pat.user_id, User.is_active))
    return user_result.scalar_one_or_none()


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    session_token: Annotated[str | None, Cookie(alias="sipsmith_session")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    user: User | None = None

    if session_token:
        user = await _user_from_token(session_token, db)

    if user is None and authorization:
        scheme, _, cred = authorization.partition(" ")
        if scheme.lower() == "bearer":
            user = await _user_from_token(cred, db) or await _user_from_pat(cred, db)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


# §16.1 — roles form a partial order (admin > operator > readonly).
# require_role(operator) must accept admin as well; using set-membership for
# the check makes admin-issued requests get rejected by operator endpoints.
_ROLE_RANK: dict[Role, int] = {
    Role.readonly: 1,
    Role.operator: 2,
    Role.admin: 3,
}


def require_role(*roles: Role):
    """Dependency factory that enforces a MINIMUM rank across `roles`.

    Pass the lowest role allowed; any role at or above that rank passes."""
    required = min(_ROLE_RANK[r] for r in roles)

    async def _check(user: Annotated[User, Depends(get_current_user)]) -> User:
        if _ROLE_RANK.get(user.role, 0) < required:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user

    return _check
