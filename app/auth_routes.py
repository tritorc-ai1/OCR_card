"""Auth + admin HTTP endpoints, mounted as a router on the main app.

Kept out of ``app/main.py`` so the auth surface is self-contained; guards on
the *existing* scan/event endpoints are applied in main.py as added
``Depends`` params.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from app import auth
from app.storage import repositories, users

router = APIRouter()

# admin_stats() runs 3 full-collection aggregations with no filter (they
# summarize ALL history by design, so no index can narrow them) plus a count
# — cheap today at this data volume, but it's the exact endpoint the original
# perf audit measured at 2.56s, and it will only get heavier as scan_log grows
# since nothing ever prunes it. A short TTL cache (same pattern already used
# in app/storage/mongo_usage.py) means only one request per window actually
# pays for the aggregations; everyone else in that window gets an instant hit.
_ADMIN_STATS_CACHE_TTL_SECONDS = 30.0
_admin_stats_cache: dict[int, tuple[float, dict]] = {}


# --- Request models ---------------------------------------------------------
class LoginIn(BaseModel):
    email: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class CreateUserIn(BaseModel):
    email: str
    password: str
    role: str = "user"


class PatchUserIn(BaseModel):
    active: bool | None = None
    new_password: str | None = None
    role: str | None = None


VALID_ROLES = ("user", "admin", "super_admin")


# --- Auth endpoints ---------------------------------------------------------
@router.post("/auth/login")
async def login(payload: LoginIn, response: Response) -> dict:
    # Uniform error for unknown user / wrong password / inactive so the
    # response never reveals which emails exist.
    invalid = HTTPException(status_code=401, detail="Invalid email or password")
    email = auth.normalize_email(payload.email)
    try:
        user = users.get_user(email)
    except Exception as exc:  # noqa: BLE001 — Mongo unavailable
        raise HTTPException(status_code=503, detail="Sign-in storage is unavailable") from exc
    if not user or not user.get("active", False):
        raise invalid
    # bcrypt.checkpw is CPU-bound (~250ms on a free tier); run it off the event
    # loop so a login never stalls concurrent scans.
    ok = await asyncio.get_event_loop().run_in_executor(
        None, auth.verify_password, payload.password, user.get("password_hash", "")
    )
    if not ok:
        raise invalid
    token = auth.create_session_token(user["email"], int(user.get("token_version", 0)))
    auth.set_session_cookie(response, token)
    return {"email": user["email"], "role": user.get("role", "user")}


@router.post("/auth/logout")
async def logout(response: Response) -> dict:
    auth.clear_session_cookie(response)
    return {"status": "logged_out"}


@router.get("/auth/me")
async def me(user: dict = Depends(auth.require_user)) -> dict:
    return {"email": user["email"], "role": user["role"]}


@router.post("/auth/change-password")
async def change_password(
    payload: ChangePasswordIn, response: Response, user: dict = Depends(auth.require_user)
) -> dict:
    record = users.get_user(user["email"])
    if not record:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not auth.verify_password(payload.current_password, record.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    try:
        new_hash = auth.hash_password(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    users.set_user_password(user["email"], new_hash)
    # The reset bumped token_version, invalidating every cookie — re-issue one
    # for THIS session so the caller isn't logged out of the device they just
    # changed the password on. Other devices are logged out (intended).
    refreshed = users.get_user(user["email"])
    token = auth.create_session_token(user["email"], int(refreshed.get("token_version", 0)))
    auth.set_session_cookie(response, token)
    return {"status": "changed"}


# --- Admin endpoints --------------------------------------------------------
@router.get("/admin/users")
async def admin_list_users(admin: dict = Depends(auth.require_admin)) -> dict:
    return {"users": [users.public_view(u) for u in users.list_users()]}


@router.post("/admin/users")
async def admin_create_user(payload: CreateUserIn, admin: dict = Depends(auth.require_admin)) -> dict:
    if not auth.is_allowed_email(payload.email):
        from app.config import ALLOWED_EMAIL_DOMAIN

        raise HTTPException(status_code=400, detail=f"Email must end with @{ALLOWED_EMAIL_DOMAIN}")
    role = payload.role if payload.role in ("admin", "user") else "user"
    try:
        password_hash = auth.hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        created = users.create_user(
            payload.email, password_hash, role=role, created_by=admin["email"]
        )
    except ValueError as exc:  # duplicate
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return users.public_view(created)


@router.patch("/admin/users/{email}")
async def admin_patch_user(email: str, payload: PatchUserIn, admin: dict = Depends(auth.require_admin)) -> dict:
    from app.config import PROTECTED_ADMIN_EMAIL

    target = auth.normalize_email(email)
    if users.get_user(target) is None:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.active is None and payload.new_password is None and payload.role is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    # Guard against the admin locking themselves out.
    if payload.active is False and target == admin["email"]:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    # The seeded permanent admin can't be deactivated by anyone, not just itself.
    if payload.active is False and target == PROTECTED_ADMIN_EMAIL:
        raise HTTPException(status_code=400, detail="This account is permanent and cannot be deactivated")
    if payload.active is not None:
        users.set_user_active(target, payload.active)
    if payload.new_password is not None:
        try:
            users.set_user_password(target, auth.hash_password(payload.new_password))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.role is not None:
        # Changing roles is a super-admin-only privilege — a regular admin can
        # still activate/deactivate and reset passwords via this same endpoint,
        # but escalating someone (including themselves) to admin/super_admin
        # requires the higher tier.
        if admin.get("role") != "super_admin":
            raise HTTPException(status_code=403, detail="Only a super admin can change roles")
        if payload.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"role must be one of {', '.join(VALID_ROLES)}")
        # The seeded permanent admin's role is as protected as its active
        # status — never demotable, even by another super admin.
        if target == PROTECTED_ADMIN_EMAIL:
            raise HTTPException(status_code=400, detail="This account's role is permanent and cannot be changed")
        users.set_user_role(target, payload.role)
    return users.public_view(users.get_user(target))


def _compute_admin_stats(days: int) -> dict:
    from app.storage.db import utc_now

    return {
        "generated_at": utc_now(),
        "window_days": days,
        "totals": repositories.scan_totals_by_user(),
        "by_event": repositories.scan_counts_by_user_event(),
        "daily": repositories.scan_counts_by_user_day(days),
        "untracked_records": repositories.count_untracked_records(),
    }


@router.get("/admin/stats")
async def admin_stats(days: int = 30, admin: dict = Depends(auth.require_admin)) -> dict:
    days = max(1, min(int(days), 365))
    cached = _admin_stats_cache.get(days)
    if cached is not None and (time.monotonic() - cached[0]) < _ADMIN_STATS_CACHE_TTL_SECONDS:
        return cached[1]
    result = await run_in_threadpool(_compute_admin_stats, days)
    _admin_stats_cache[days] = (time.monotonic(), result)
    return result


@router.get("/me/stats")
async def my_stats(days: int = 30, user: dict = Depends(auth.require_user)) -> dict:
    """A regular user's own card-scanning activity — the non-admin counterpart
    to /admin/stats, scoped to just the caller so it doesn't leak other users'
    numbers. Backs the Home dashboard's per-user tracking card."""
    days = max(1, min(int(days), 365))
    return await run_in_threadpool(repositories.scan_stats_for_user, user["email"], days)
