"""TG Plan REST API — FastAPI router for centralized talkgroup management.

Mounted on the dashboard app. Two auth tiers:
  - Admin token (from config): owner CRUD
  - Owner API token (bcrypt in PG): repeater + TG assignment CRUD, scoped to own data

Admin can also access owner-scoped endpoints for any owner's resources.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tg-plan", tags=["tg-plan"])

# These are set by mount_tg_plan_api() when the router is added to the app
_store = None       # TGPlanStore instance
_admin_token = None  # plaintext admin token from config


# ── request/response models ───────────────────────

class CreateOwnerRequest(BaseModel):
    callsign: str
    name: Optional[str] = None

class RegisterRepeaterRequest(BaseModel):
    radio_id: int
    name: Optional[str] = None

class SetTalkgroupsRequest(BaseModel):
    slot1: Optional[List[int]] = None
    slot2: Optional[List[int]] = None

class BulkAssignRequest(BaseModel):
    repeater_ids: List[int]
    slot: int
    talkgroup_ids: List[int]


# ── auth dependencies ─────────────────────────────

def _get_bearer(authorization: str = Header(None)) -> str:
    """Extract bearer token from Authorization header."""
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Missing or invalid Authorization header')
    return authorization[7:]


async def get_current_owner(token: str = Depends(_get_bearer)) -> str:
    """Authenticate and return owner callsign. Admin token returns '__admin__'."""
    if token == _admin_token:
        return '__admin__'
    callsign = await _store.authenticate(token)
    if not callsign:
        raise HTTPException(401, 'Invalid API token')
    return callsign


async def require_admin(token: str = Depends(_get_bearer)) -> str:
    """Require admin token."""
    if token != _admin_token:
        raise HTTPException(403, 'Admin access required')
    return '__admin__'


def _is_admin(owner: str) -> bool:
    return owner == '__admin__'


# ── owner management (admin-only) ─────────────────

@router.post("/owners")
async def create_owner(req: CreateOwnerRequest, _=Depends(require_admin)):
    """Create a new owner. Returns the API token (shown once)."""
    try:
        token = await _store.create_owner(req.callsign, req.name)
        return {"ok": True, "callsign": req.callsign.upper(), "api_token": token}
    except Exception as e:
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            raise HTTPException(409, f'Owner {req.callsign.upper()} already exists')
        raise HTTPException(500, str(e))


@router.get("/owners")
async def list_owners(_=Depends(require_admin)):
    """List all owners (tokens hidden)."""
    owners = await _store.list_owners()
    return {"ok": True, "owners": owners}


@router.delete("/owners/{callsign}")
async def delete_owner(callsign: str, _=Depends(require_admin)):
    """Delete an owner (cascades repeaters + TGs)."""
    deleted = await _store.delete_owner(callsign)
    if not deleted:
        raise HTTPException(404, f'Owner {callsign.upper()} not found')
    return {"ok": True, "deleted": callsign.upper()}


@router.post("/owners/{callsign}/rotate-token")
async def rotate_token(callsign: str, _=Depends(require_admin)):
    """Generate a new API token for an owner. Returns token (shown once)."""
    token = await _store.rotate_token(callsign)
    if not token:
        raise HTTPException(404, f'Owner {callsign.upper()} not found')
    return {"ok": True, "callsign": callsign.upper(), "api_token": token}


# ── repeater registration (owner-scoped) ───────────

@router.post("/repeaters")
async def register_repeater(req: RegisterRepeaterRequest,
                             owner: str = Depends(get_current_owner)):
    """Register a repeater under the authenticated owner."""
    callsign = owner if not _is_admin(owner) else None
    if _is_admin(owner):
        raise HTTPException(400, 'Admin must specify owner — use owner token instead')

    ok = await _store.register_repeater(req.radio_id, callsign, req.name)
    if not ok:
        raise HTTPException(409, f'Repeater {req.radio_id} already registered')
    return {"ok": True, "radio_id": req.radio_id, "owner": callsign}


@router.get("/repeaters")
async def list_repeaters(owner: str = Depends(get_current_owner)):
    """List repeaters. Admin sees all; owners see their own."""
    if _is_admin(owner):
        repeaters = await _store.list_repeaters()
    else:
        repeaters = await _store.list_repeaters(owner)
    return {"ok": True, "repeaters": repeaters}


@router.delete("/repeaters/{radio_id}")
async def unregister_repeater(radio_id: int,
                               owner: str = Depends(get_current_owner)):
    """Unregister a repeater. Admin can delete any; owners only their own."""
    if _is_admin(owner):
        deleted = await _store.unregister_repeater(radio_id)
    else:
        deleted = await _store.unregister_repeater(radio_id, owner)
    if not deleted:
        raise HTTPException(404, f'Repeater {radio_id} not found or not owned by you')
    return {"ok": True, "deleted": radio_id}


# ── TG assignments (owner-scoped) ─────────────────

@router.get("/repeaters/{radio_id}/talkgroups")
async def get_talkgroups(radio_id: int,
                          owner: str = Depends(get_current_owner)):
    """Get TG assignments for a repeater."""
    if not _is_admin(owner):
        rpt_owner = await _store.get_repeater_owner(radio_id)
        if rpt_owner != owner:
            raise HTTPException(403, 'Not your repeater')

    tgs = await _store.get_tgs(radio_id)
    return {"ok": True, "radio_id": radio_id,
            "slot1": sorted(tgs.get(1, set())),
            "slot2": sorted(tgs.get(2, set()))}


@router.put("/repeaters/{radio_id}/talkgroups")
async def set_talkgroups(radio_id: int, req: SetTalkgroupsRequest,
                          owner: str = Depends(get_current_owner)):
    """Set TGs for a repeater (replaces existing)."""
    owner_cs = None if _is_admin(owner) else owner

    if req.slot1 is not None:
        ok = await _store.set_tgs(radio_id, 1, req.slot1, owner_callsign=owner_cs)
        if not ok:
            raise HTTPException(403, 'Not your repeater')
    if req.slot2 is not None:
        ok = await _store.set_tgs(radio_id, 2, req.slot2, owner_callsign=owner_cs)
        if not ok:
            raise HTTPException(403, 'Not your repeater')

    tgs = await _store.get_tgs(radio_id)
    return {"ok": True, "radio_id": radio_id,
            "slot1": sorted(tgs.get(1, set())),
            "slot2": sorted(tgs.get(2, set()))}


@router.post("/bulk-assign")
async def bulk_assign(req: BulkAssignRequest,
                       owner: str = Depends(get_current_owner)):
    """Assign TGs to multiple repeaters in one transaction."""
    if req.slot not in (1, 2):
        raise HTTPException(400, 'slot must be 1 or 2')

    owner_cs = None if _is_admin(owner) else owner
    affected = await _store.bulk_assign(
        req.repeater_ids, req.slot, req.talkgroup_ids, owner_callsign=owner_cs
    )
    return {"ok": True, "affected_repeaters": affected}


# ── read-only lookups ──────────────────────────────

@router.get("/talkgroups/{tg_id}/repeaters")
async def get_tg_repeaters(tg_id: int, _owner: str = Depends(get_current_owner)):
    """Which repeaters carry this TG?"""
    repeaters = await _store.get_repeaters_for_tg(tg_id)
    return {"ok": True, "talkgroup_id": tg_id, "repeaters": repeaters}


# ── health ─────────────────────────────────────────

@router.get("/health")
async def tg_plan_health():
    """TG plan PG health check (no auth required)."""
    if not _store:
        return {"ok": False, "error": "TG plan not configured"}
    ok, latency = await _store.health_check()
    return {"ok": ok, "pg_latency_ms": latency}


# ── mount helper ───────────────────────────────────

def mount_tg_plan_api(app, store, admin_token: str):
    """Mount the TG plan router on a FastAPI app."""
    global _store, _admin_token
    _store = store
    _admin_token = admin_token
    app.include_router(router)
    logger.info('TG plan API mounted at /api/tg-plan/')
