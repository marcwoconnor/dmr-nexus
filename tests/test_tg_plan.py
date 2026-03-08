"""Tests for TGPlanStore — mocked asyncpg, no real PG needed."""
import asyncio
import json
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus.tg_plan import TGPlanStore, _hash_token, _verify_token


# ── token hashing ──────────────────────────────────

class TestTokenHashing:
    def test_hash_and_verify(self):
        token = 'test-token-abc123'
        hashed = _hash_token(token)
        assert hashed != token
        assert _verify_token(token, hashed)

    def test_wrong_token_fails(self):
        hashed = _hash_token('correct-token')
        assert not _verify_token('wrong-token', hashed)

    def test_different_hashes_for_same_token(self):
        """bcrypt salts should produce different hashes."""
        h1 = _hash_token('same')
        h2 = _hash_token('same')
        assert h1 != h2
        assert _verify_token('same', h1)
        assert _verify_token('same', h2)


# ── mock helpers ───────────────────────────────────

class _AsyncCtx:
    """Helper: object usable as `async with obj` returning a fixed value."""
    def __init__(self, value):
        self._value = value
    async def __aenter__(self):
        return self._value
    async def __aexit__(self, *args):
        return False


def _mock_pool():
    """Create a mock asyncpg pool with acquire() context manager."""
    pool = MagicMock()
    conn = AsyncMock()

    # pool.acquire() must return an async context manager (not a coroutine)
    pool.acquire.return_value = _AsyncCtx(conn)

    # conn.transaction() is a regular call returning an async ctx manager
    conn.transaction = MagicMock(return_value=_AsyncCtx(conn))

    return pool, conn


def _dict_row(**kwargs):
    """Create a dict-like mock that works with dict() conversion."""
    return kwargs


def _make_store(pool=None, on_change=None):
    """Create a TGPlanStore with mocked pool."""
    store = TGPlanStore('postgresql://test@localhost/test', on_change=on_change)
    if pool:
        store._pool = pool
        store._started = True
    return store


# ── owner CRUD ─────────────────────────────────────

class TestOwnerCRUD:
    @pytest.mark.asyncio
    async def test_create_owner_returns_token(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        token = await store.create_owner('KK4WTI', 'Marc')
        assert isinstance(token, str)
        assert len(token) > 20  # token_urlsafe(32) is ~43 chars

        # Verify INSERT was called with uppercase callsign
        conn.execute.assert_called_once()
        args = conn.execute.call_args
        assert args[0][1] == 'KK4WTI'
        assert args[0][3] == 'Marc'

    @pytest.mark.asyncio
    async def test_create_owner_uppercases_callsign(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        await store.create_owner('kk4wti')
        args = conn.execute.call_args
        assert args[0][1] == 'KK4WTI'

    @pytest.mark.asyncio
    async def test_delete_owner(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.execute.return_value = 'DELETE 1'

        result = await store.delete_owner('KK4WTI')
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_owner_not_found(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.execute.return_value = 'DELETE 0'

        result = await store.delete_owner('NOBODY')
        assert result is False

    @pytest.mark.asyncio
    async def test_list_owners(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        conn.fetch.return_value = [_dict_row(callsign='KK4WTI', name='Marc', created_at=None)]

        owners = await store.list_owners()
        assert len(owners) == 1
        assert owners[0]['callsign'] == 'KK4WTI'

    @pytest.mark.asyncio
    async def test_rotate_token(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.execute.return_value = 'UPDATE 1'

        token = await store.rotate_token('KK4WTI')
        assert isinstance(token, str)
        assert len(token) > 20

    @pytest.mark.asyncio
    async def test_rotate_token_not_found(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.execute.return_value = 'UPDATE 0'

        token = await store.rotate_token('NOBODY')
        assert token is None

    @pytest.mark.asyncio
    async def test_authenticate_valid(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        real_token = 'my-secret-token'
        hashed = _hash_token(real_token)

        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, k: {'callsign': 'KK4WTI', 'api_token': hashed}[k]
        conn.fetch.return_value = [mock_row]

        result = await store.authenticate(real_token)
        assert result == 'KK4WTI'

    @pytest.mark.asyncio
    async def test_authenticate_invalid(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        hashed = _hash_token('correct-token')
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, k: {'callsign': 'KK4WTI', 'api_token': hashed}[k]
        conn.fetch.return_value = [mock_row]

        result = await store.authenticate('wrong-token')
        assert result is None


# ── repeater CRUD ──────────────────────────────────

class TestRepeaterCRUD:
    @pytest.mark.asyncio
    async def test_register_repeater(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        result = await store.register_repeater(311000, 'KK4WTI', 'Downtown')
        assert result is True

    @pytest.mark.asyncio
    async def test_register_duplicate(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        # Simulate unique violation
        import asyncpg as _ap
        conn.execute.side_effect = _ap.UniqueViolationError('')

        result = await store.register_repeater(311000, 'KK4WTI')
        assert result is False

    @pytest.mark.asyncio
    async def test_unregister_repeater_owner_scoped(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.execute.return_value = 'DELETE 1'

        result = await store.unregister_repeater(311000, 'KK4WTI')
        assert result is True
        # Should include owner in WHERE clause
        args = conn.execute.call_args[0]
        assert 'owner_callsign' in args[0]

    @pytest.mark.asyncio
    async def test_unregister_repeater_admin(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.execute.return_value = 'DELETE 1'

        result = await store.unregister_repeater(311000)
        assert result is True
        # Admin path — no owner in WHERE
        args = conn.execute.call_args[0]
        assert 'owner_callsign' not in args[0]

    @pytest.mark.asyncio
    async def test_list_repeaters_all(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetch.return_value = []

        result = await store.list_repeaters()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_repeaters_by_owner(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetch.return_value = []

        await store.list_repeaters('KK4WTI')
        args = conn.fetch.call_args[0]
        assert 'owner_callsign' in args[0]

    @pytest.mark.asyncio
    async def test_get_repeater_owner(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetchval.return_value = 'KK4WTI'

        result = await store.get_repeater_owner(311000)
        assert result == 'KK4WTI'


# ── TG assignment CRUD ─────────────────────────────

class TestTGAssignments:
    @pytest.mark.asyncio
    async def test_get_tgs_empty(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetch.return_value = []

        result = await store.get_tgs(311000)
        assert result == {1: set(), 2: set()}

    @pytest.mark.asyncio
    async def test_get_tgs_populated(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        conn.fetch.return_value = [
            _dict_row(slot=1, talkgroup_id=8),
            _dict_row(slot=1, talkgroup_id=9),
            _dict_row(slot=2, talkgroup_id=3120),
        ]

        result = await store.get_tgs(311000)
        assert result[1] == {8, 9}
        assert result[2] == {3120}

    @pytest.mark.asyncio
    async def test_set_tgs_fires_notify(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        await store.set_tgs(311000, 1, [8, 9])

        # Should have: DELETE, executemany, NOTIFY
        calls = conn.execute.call_args_list
        notify_call = [c for c in calls if 'pg_notify' in str(c)]
        assert len(notify_call) == 1

    @pytest.mark.asyncio
    async def test_set_tgs_owner_check(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetchval.return_value = 'OTHER_GUY'  # different owner

        result = await store.set_tgs(311000, 1, [8], owner_callsign='KK4WTI')
        assert result is False

    @pytest.mark.asyncio
    async def test_set_tgs_owner_match(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetchval.return_value = 'KK4WTI'

        result = await store.set_tgs(311000, 1, [8], owner_callsign='KK4WTI')
        assert result is True

    @pytest.mark.asyncio
    async def test_bulk_assign(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        count = await store.bulk_assign([311000, 311001], 1, [8, 9])
        assert count == 2

    @pytest.mark.asyncio
    async def test_bulk_assign_skips_unowned(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)

        # First call returns owner, second returns different owner
        conn.fetchval.side_effect = ['KK4WTI', 'OTHER']

        count = await store.bulk_assign([311000, 311001], 1, [8],
                                        owner_callsign='KK4WTI')
        assert count == 1

    @pytest.mark.asyncio
    async def test_has_plan_true(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetchval.return_value = 3

        assert await store.has_plan(311000) is True

    @pytest.mark.asyncio
    async def test_has_plan_false(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetchval.return_value = 0

        assert await store.has_plan(311000) is False

    @pytest.mark.asyncio
    async def test_get_repeaters_for_tg(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetch.return_value = []

        result = await store.get_repeaters_for_tg(8)
        assert result == []


# ── health check ───────────────────────────────────

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetchval.return_value = 1

        ok, latency = await store.health_check()
        assert ok is True
        assert latency is not None
        assert latency >= 0

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        pool, conn = _mock_pool()
        store = _make_store(pool)
        conn.fetchval.side_effect = Exception('connection lost')

        ok, latency = await store.health_check()
        assert ok is False
        assert latency is None

    @pytest.mark.asyncio
    async def test_no_pool(self):
        store = _make_store()
        ok, latency = await store.health_check()
        assert ok is False


# ── NOTIFY callback ────────────────────────────────

class TestNotify:
    def test_pg_notify_calls_on_change(self):
        callback = AsyncMock()
        store = _make_store(on_change=callback)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            payload = json.dumps({'repeater_id': 311000})
            store._pg_notify(None, None, 'tg_plan_changed', payload)
            # Run pending tasks
            loop.run_until_complete(asyncio.sleep(0.01))
            callback.assert_called_once_with(311000)
        finally:
            loop.close()

    def test_pg_notify_bad_payload(self):
        callback = AsyncMock()
        store = _make_store(on_change=callback)

        # Should not raise
        store._pg_notify(None, None, 'tg_plan_changed', 'not-json')


# ── properties ─────────────────────────────────────

class TestProperties:
    def test_connected_false_initially(self):
        store = _make_store()
        assert store.connected is False

    def test_connected_true_when_started(self):
        pool, _ = _mock_pool()
        store = _make_store(pool)
        assert store.connected is True

    def test_pg_latency_none_initially(self):
        store = _make_store()
        assert store.pg_latency_ms is None
