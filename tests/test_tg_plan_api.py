"""Tests for TG Plan REST API endpoints."""
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from fastapi import FastAPI
from dashboard.tg_plan_api import router, mount_tg_plan_api

ADMIN_TOKEN = 'test-admin-token'
OWNER_TOKEN = 'test-owner-token'
OWNER_CALLSIGN = 'KK4WTI'


def _make_app():
    """Create a test FastAPI app with TG plan router mounted."""
    app = FastAPI()
    store = AsyncMock()

    # Default mock returns
    store.authenticate = AsyncMock(return_value=None)
    store.health_check = AsyncMock(return_value=(True, 1.5))

    mount_tg_plan_api(app, store, ADMIN_TOKEN)
    return app, store


def _admin_headers():
    return {'Authorization': f'Bearer {ADMIN_TOKEN}'}


def _owner_headers():
    return {'Authorization': f'Bearer {OWNER_TOKEN}'}


# ── auth tests ─────────────────────────────────────

class TestAuth:
    def test_no_auth_returns_401(self):
        app, _ = _make_app()
        client = TestClient(app)
        r = client.get('/api/tg-plan/owners')
        assert r.status_code == 401

    def test_bad_token_returns_401(self):
        app, store = _make_app()
        store.authenticate.return_value = None
        client = TestClient(app)
        r = client.get('/api/tg-plan/repeaters',
                       headers={'Authorization': 'Bearer bad-token'})
        assert r.status_code == 401

    def test_owner_cannot_access_admin_endpoints(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        client = TestClient(app)
        r = client.get('/api/tg-plan/owners', headers=_owner_headers())
        assert r.status_code == 403

    def test_admin_can_access_admin_endpoints(self):
        app, store = _make_app()
        store.list_owners.return_value = []
        client = TestClient(app)
        r = client.get('/api/tg-plan/owners', headers=_admin_headers())
        assert r.status_code == 200


# ── owner CRUD ─────────────────────────────────────

class TestOwnerAPI:
    def test_create_owner(self):
        app, store = _make_app()
        store.create_owner.return_value = 'generated-token'
        client = TestClient(app)
        r = client.post('/api/tg-plan/owners',
                        json={'callsign': 'KK4WTI', 'name': 'Marc'},
                        headers=_admin_headers())
        assert r.status_code == 200
        data = r.json()
        assert data['api_token'] == 'generated-token'
        assert data['callsign'] == 'KK4WTI'

    def test_list_owners(self):
        app, store = _make_app()
        store.list_owners.return_value = [{'callsign': 'KK4WTI', 'name': 'Marc'}]
        client = TestClient(app)
        r = client.get('/api/tg-plan/owners', headers=_admin_headers())
        assert r.status_code == 200
        assert len(r.json()['owners']) == 1

    def test_delete_owner(self):
        app, store = _make_app()
        store.delete_owner.return_value = True
        client = TestClient(app)
        r = client.delete('/api/tg-plan/owners/KK4WTI', headers=_admin_headers())
        assert r.status_code == 200

    def test_delete_owner_not_found(self):
        app, store = _make_app()
        store.delete_owner.return_value = False
        client = TestClient(app)
        r = client.delete('/api/tg-plan/owners/NOBODY', headers=_admin_headers())
        assert r.status_code == 404

    def test_rotate_token(self):
        app, store = _make_app()
        store.rotate_token.return_value = 'new-token'
        client = TestClient(app)
        r = client.post('/api/tg-plan/owners/KK4WTI/rotate-token',
                        headers=_admin_headers())
        assert r.status_code == 200
        assert r.json()['api_token'] == 'new-token'


# ── repeater CRUD ──────────────────────────────────

class TestRepeaterAPI:
    def test_register_repeater(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.register_repeater.return_value = True
        client = TestClient(app)
        r = client.post('/api/tg-plan/repeaters',
                        json={'radio_id': 311000, 'name': 'Downtown'},
                        headers=_owner_headers())
        assert r.status_code == 200
        assert r.json()['radio_id'] == 311000

    def test_register_duplicate(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.register_repeater.return_value = False
        client = TestClient(app)
        r = client.post('/api/tg-plan/repeaters',
                        json={'radio_id': 311000},
                        headers=_owner_headers())
        assert r.status_code == 409

    def test_list_repeaters_owner_scoped(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.list_repeaters.return_value = [{'radio_id': 311000}]
        client = TestClient(app)
        r = client.get('/api/tg-plan/repeaters', headers=_owner_headers())
        assert r.status_code == 200
        # Should call list_repeaters with owner filter
        store.list_repeaters.assert_called_with(OWNER_CALLSIGN)

    def test_list_repeaters_admin_sees_all(self):
        app, store = _make_app()
        store.list_repeaters.return_value = []
        client = TestClient(app)
        r = client.get('/api/tg-plan/repeaters', headers=_admin_headers())
        assert r.status_code == 200
        store.list_repeaters.assert_called_with()

    def test_unregister_repeater_owner(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.unregister_repeater.return_value = True
        client = TestClient(app)
        r = client.delete('/api/tg-plan/repeaters/311000', headers=_owner_headers())
        assert r.status_code == 200
        store.unregister_repeater.assert_called_with(311000, OWNER_CALLSIGN)

    def test_unregister_repeater_admin(self):
        app, store = _make_app()
        store.unregister_repeater.return_value = True
        client = TestClient(app)
        r = client.delete('/api/tg-plan/repeaters/311000', headers=_admin_headers())
        assert r.status_code == 200
        store.unregister_repeater.assert_called_with(311000)


# ── TG assignment CRUD ─────────────────────────────

class TestTGAssignmentAPI:
    def test_get_talkgroups(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.get_repeater_owner.return_value = OWNER_CALLSIGN
        store.get_tgs.return_value = {1: {8, 9}, 2: {3120}}
        client = TestClient(app)
        r = client.get('/api/tg-plan/repeaters/311000/talkgroups',
                       headers=_owner_headers())
        assert r.status_code == 200
        data = r.json()
        assert data['slot1'] == [8, 9]
        assert data['slot2'] == [3120]

    def test_get_talkgroups_not_your_repeater(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.get_repeater_owner.return_value = 'OTHER_GUY'
        client = TestClient(app)
        r = client.get('/api/tg-plan/repeaters/311000/talkgroups',
                       headers=_owner_headers())
        assert r.status_code == 403

    def test_set_talkgroups(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.set_tgs.return_value = True
        store.get_tgs.return_value = {1: {8, 9}, 2: set()}
        client = TestClient(app)
        r = client.put('/api/tg-plan/repeaters/311000/talkgroups',
                       json={'slot1': [8, 9]},
                       headers=_owner_headers())
        assert r.status_code == 200

    def test_set_talkgroups_not_your_repeater(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.set_tgs.return_value = False
        client = TestClient(app)
        r = client.put('/api/tg-plan/repeaters/311000/talkgroups',
                       json={'slot1': [8]},
                       headers=_owner_headers())
        assert r.status_code == 403

    def test_bulk_assign(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.bulk_assign.return_value = 3
        client = TestClient(app)
        r = client.post('/api/tg-plan/bulk-assign',
                        json={'repeater_ids': [1, 2, 3], 'slot': 1,
                              'talkgroup_ids': [8]},
                        headers=_owner_headers())
        assert r.status_code == 200
        assert r.json()['affected_repeaters'] == 3

    def test_bulk_assign_bad_slot(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        client = TestClient(app)
        r = client.post('/api/tg-plan/bulk-assign',
                        json={'repeater_ids': [1], 'slot': 3,
                              'talkgroup_ids': [8]},
                        headers=_owner_headers())
        assert r.status_code == 400


# ── read-only lookups ──────────────────────────────

class TestLookups:
    def test_get_tg_repeaters(self):
        app, store = _make_app()
        store.authenticate.return_value = OWNER_CALLSIGN
        store.get_repeaters_for_tg.return_value = [
            {'repeater_id': 311000, 'slot': 1}
        ]
        client = TestClient(app)
        r = client.get('/api/tg-plan/talkgroups/8/repeaters',
                       headers=_owner_headers())
        assert r.status_code == 200
        assert len(r.json()['repeaters']) == 1


# ── health ─────────────────────────────────────────

class TestHealth:
    def test_health_ok(self):
        app, store = _make_app()
        store.health_check.return_value = (True, 1.5)
        client = TestClient(app)
        r = client.get('/api/tg-plan/health')
        assert r.status_code == 200
        data = r.json()
        assert data['ok'] is True
        assert data['pg_latency_ms'] == 1.5

    def test_health_down(self):
        app, store = _make_app()
        store.health_check.return_value = (False, None)
        client = TestClient(app)
        r = client.get('/api/tg-plan/health')
        assert r.status_code == 200
        assert r.json()['ok'] is False
