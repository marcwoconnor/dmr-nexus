"""TG Plan — centralized talkgroup assignment store backed by PostgreSQL.

Owners register repeaters and assign talkgroups per slot. Core servers
receive real-time updates via PG LISTEN/NOTIFY. Config patterns remain
the security boundary; TG plan operates within that boundary.
"""
import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Callable, Awaitable

try:
    import asyncpg
except ImportError:
    asyncpg = None

try:
    import bcrypt
except ImportError:
    bcrypt = None

logger = logging.getLogger(__name__)

# Schema DDL — runs on startup (idempotent)
_SCHEMA_SQL = (Path(__file__).parent.parent / 'scripts' / 'tg_plan_schema.sql').read_text()


def _hash_token(token: str) -> str:
    """Bcrypt-hash an API token."""
    return bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()


def _verify_token(token: str, hashed: str) -> bool:
    """Verify an API token against its bcrypt hash."""
    return bcrypt.checkpw(token.encode(), hashed.encode())


class TGPlanStore:
    """Async PostgreSQL CRUD for TG plan data.

    Usage:
        store = TGPlanStore(database_url)
        await store.start()       # connect + auto-migrate + start LISTEN
        ...
        await store.stop()
    """

    def __init__(self, database_url: str,
                 on_change: Optional[Callable[[int], Awaitable[None]]] = None):
        """
        Args:
            database_url: PostgreSQL connection string
            on_change: async callback(repeater_id) fired on LISTEN notification
        """
        self._dsn = database_url
        self._pool: Optional[asyncpg.Pool] = None
        self._listen_conn: Optional[asyncpg.Connection] = None
        self._on_change = on_change
        self._started = False
        self._pg_latency_ms: Optional[float] = None

    @property
    def connected(self) -> bool:
        return self._started and self._pool is not None

    @property
    def pg_latency_ms(self) -> Optional[float]:
        return self._pg_latency_ms

    # ── lifecycle ──────────────────────────────────────

    async def start(self):
        """Connect to PG, run auto-migration, start LISTEN."""
        if asyncpg is None:
            raise RuntimeError('asyncpg not installed — pip install asyncpg')
        if bcrypt is None:
            raise RuntimeError('bcrypt not installed — pip install bcrypt')

        self._pool = await asyncpg.create_pool(self._dsn, min_size=2, max_size=10)
        logger.info('TG plan: connected to PostgreSQL')

        # Auto-migrate (idempotent CREATE IF NOT EXISTS)
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA_SQL)
        logger.info('TG plan: schema verified')

        # Dedicated connection for LISTEN
        self._listen_conn = await asyncpg.connect(self._dsn)
        await self._listen_conn.add_listener('tg_plan_changed', self._pg_notify)
        logger.info('TG plan: LISTEN tg_plan_changed active')

        self._started = True

    async def stop(self):
        """Disconnect from PG."""
        self._started = False
        if self._listen_conn:
            await self._listen_conn.remove_listener('tg_plan_changed', self._pg_notify)
            await self._listen_conn.close()
            self._listen_conn = None
        if self._pool:
            await self._pool.close()
            self._pool = None
        logger.info('TG plan: disconnected')

    def _pg_notify(self, conn, pid, channel, payload):
        """Handle PG NOTIFY — schedule async callback."""
        try:
            msg = json.loads(payload)
            repeater_id = msg.get('repeater_id')
            if repeater_id is not None and self._on_change:
                asyncio.get_event_loop().create_task(self._on_change(int(repeater_id)))
        except Exception as e:
            logger.warning(f'TG plan: bad NOTIFY payload: {e}')

    async def health_check(self) -> Tuple[bool, Optional[float]]:
        """Check PG connectivity and measure latency."""
        if not self._pool:
            return False, None
        try:
            t0 = time.monotonic()
            async with self._pool.acquire() as conn:
                await conn.fetchval('SELECT 1')
            ms = (time.monotonic() - t0) * 1000
            self._pg_latency_ms = round(ms, 1)
            return True, self._pg_latency_ms
        except Exception:
            self._pg_latency_ms = None
            return False, None

    # ── owner CRUD ─────────────────────────────────────

    async def create_owner(self, callsign: str, name: str = None) -> str:
        """Create owner, return plaintext API token (shown once)."""
        token = secrets.token_urlsafe(32)
        hashed = _hash_token(token)
        async with self._pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO owners (callsign, api_token, name) VALUES ($1, $2, $3)',
                callsign.upper(), hashed, name
            )
        logger.info(f'TG plan: created owner {callsign.upper()}')
        return token

    async def delete_owner(self, callsign: str) -> bool:
        """Delete owner (cascades repeaters + assignments)."""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                'DELETE FROM owners WHERE callsign = $1', callsign.upper()
            )
        deleted = result == 'DELETE 1'
        if deleted:
            logger.info(f'TG plan: deleted owner {callsign.upper()}')
        return deleted

    async def list_owners(self) -> List[dict]:
        """List all owners (without tokens)."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT callsign, name, created_at FROM owners ORDER BY callsign'
            )
        return [dict(r) for r in rows]

    async def rotate_token(self, callsign: str) -> Optional[str]:
        """Generate new token for owner. Returns plaintext or None if not found."""
        token = secrets.token_urlsafe(32)
        hashed = _hash_token(token)
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                'UPDATE owners SET api_token = $1 WHERE callsign = $2',
                hashed, callsign.upper()
            )
        if result == 'UPDATE 1':
            logger.info(f'TG plan: rotated token for {callsign.upper()}')
            return token
        return None

    async def authenticate(self, token: str) -> Optional[str]:
        """Validate API token, return callsign or None."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch('SELECT callsign, api_token FROM owners')
        for row in rows:
            if _verify_token(token, row['api_token']):
                return row['callsign']
        return None

    # ── repeater CRUD ──────────────────────────────────

    async def register_repeater(self, radio_id: int, owner_callsign: str,
                                name: str = None) -> bool:
        """Register a repeater under an owner."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    'INSERT INTO repeaters (radio_id, owner_callsign, name) '
                    'VALUES ($1, $2, $3)',
                    radio_id, owner_callsign.upper(), name
                )
            return True
        except asyncpg.UniqueViolationError:
            return False

    async def unregister_repeater(self, radio_id: int,
                                  owner_callsign: str = None) -> bool:
        """Unregister repeater. If owner_callsign given, verify ownership."""
        async with self._pool.acquire() as conn:
            if owner_callsign:
                result = await conn.execute(
                    'DELETE FROM repeaters WHERE radio_id = $1 AND owner_callsign = $2',
                    radio_id, owner_callsign.upper()
                )
            else:
                result = await conn.execute(
                    'DELETE FROM repeaters WHERE radio_id = $1', radio_id
                )
        return result == 'DELETE 1'

    async def list_repeaters(self, owner_callsign: str = None) -> List[dict]:
        """List repeaters, optionally filtered by owner."""
        async with self._pool.acquire() as conn:
            if owner_callsign:
                rows = await conn.fetch(
                    'SELECT radio_id, owner_callsign, name, created_at '
                    'FROM repeaters WHERE owner_callsign = $1 ORDER BY radio_id',
                    owner_callsign.upper()
                )
            else:
                rows = await conn.fetch(
                    'SELECT radio_id, owner_callsign, name, created_at '
                    'FROM repeaters ORDER BY radio_id'
                )
        return [dict(r) for r in rows]

    async def get_repeater_owner(self, radio_id: int) -> Optional[str]:
        """Get owner callsign for a repeater, or None."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                'SELECT owner_callsign FROM repeaters WHERE radio_id = $1', radio_id
            )

    # ── TG assignment CRUD ─────────────────────────────

    async def get_tgs(self, radio_id: int) -> Dict[int, Set[int]]:
        """Get TG assignments for a repeater. Returns {slot: {tg_ids}}."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT slot, talkgroup_id FROM tg_assignments WHERE repeater_id = $1',
                radio_id
            )
        result: Dict[int, Set[int]] = {1: set(), 2: set()}
        for row in rows:
            result[row['slot']].add(row['talkgroup_id'])
        return result

    async def set_tgs(self, radio_id: int, slot: int, tg_ids: List[int],
                      owner_callsign: str = None) -> bool:
        """Replace all TGs for a repeater+slot. Optionally verify ownership.
        Fires NOTIFY for live update."""
        async with self._pool.acquire() as conn:
            # Verify ownership if needed
            if owner_callsign:
                owner = await conn.fetchval(
                    'SELECT owner_callsign FROM repeaters WHERE radio_id = $1',
                    radio_id
                )
                if owner != owner_callsign.upper():
                    return False

            async with conn.transaction():
                await conn.execute(
                    'DELETE FROM tg_assignments WHERE repeater_id = $1 AND slot = $2',
                    radio_id, slot
                )
                if tg_ids:
                    await conn.executemany(
                        'INSERT INTO tg_assignments (repeater_id, slot, talkgroup_id) '
                        'VALUES ($1, $2, $3)',
                        [(radio_id, slot, tg) for tg in tg_ids]
                    )
                # Notify listeners
                await conn.execute(
                    "SELECT pg_notify('tg_plan_changed', $1)",
                    json.dumps({'repeater_id': radio_id})
                )
        return True

    async def bulk_assign(self, repeater_ids: List[int], slot: int,
                          tg_ids: List[int],
                          owner_callsign: str = None) -> int:
        """Add TGs to multiple repeaters in one transaction.
        Returns count of affected repeaters. Fires NOTIFY per repeater."""
        affected = 0
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for rid in repeater_ids:
                    # Verify ownership if needed
                    if owner_callsign:
                        owner = await conn.fetchval(
                            'SELECT owner_callsign FROM repeaters WHERE radio_id = $1',
                            rid
                        )
                        if owner != owner_callsign.upper():
                            continue

                    for tg in tg_ids:
                        await conn.execute(
                            'INSERT INTO tg_assignments (repeater_id, slot, talkgroup_id) '
                            'VALUES ($1, $2, $3) ON CONFLICT DO NOTHING',
                            rid, slot, tg
                        )
                    await conn.execute(
                        "SELECT pg_notify('tg_plan_changed', $1)",
                        json.dumps({'repeater_id': rid})
                    )
                    affected += 1
        return affected

    async def get_repeaters_for_tg(self, tg_id: int) -> List[dict]:
        """Which repeaters carry a given TG? Returns [{radio_id, slot, owner}]."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT a.repeater_id, a.slot, r.owner_callsign, r.name '
                'FROM tg_assignments a JOIN repeaters r ON a.repeater_id = r.radio_id '
                'WHERE a.talkgroup_id = $1 ORDER BY a.repeater_id',
                tg_id
            )
        return [dict(r) for r in rows]

    async def has_plan(self, radio_id: int) -> bool:
        """Check if a repeater has any TG plan entries."""
        async with self._pool.acquire() as conn:
            count = await conn.fetchval(
                'SELECT COUNT(*) FROM tg_assignments WHERE repeater_id = $1',
                radio_id
            )
        return count > 0
