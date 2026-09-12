"""Tests for JOB-002 — job filtering and pagination on GET /api/v1/jobs."""
from datetime import datetime, timezone, timedelta

import pytest

from app.db.models import JobModel
from app.db.session import get_session
from app.models.job import Job


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_jobs():
    with get_session() as session:
        session.query(JobModel).delete()
    yield
    with get_session() as session:
        session.query(JobModel).delete()


def _job(device="dev1", status="completed", minutes_ago=0):
    """Create a job and set its status and created_at directly in the DB."""
    from app.composition import job_repository

    j = job_repository.add(Job(playbook="test.yml", device=device))
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=j.job_id).first()
        row.status = status
        row.created_at = created
    return j


# ── Response shape ────────────────────────────────────────────────────────────
# GET /jobs/ wraps its payload in the standard ok() envelope
# ({"success": True, "data": {...}}) -- previously this test assumed the
# pagination keys sat at the top level of the JSON body.

def test_list_jobs_returns_paginated_envelope(client):
    resp = client.get("/api/v1/jobs/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert "total" in data
    assert "page" in data
    assert "page_size" in data
    assert "items" in data
    assert isinstance(data["items"], list)


def test_empty_result_returns_zero_total_not_404(client):
    resp = client.get("/api/v1/jobs/?status=running")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 0
    assert data["items"] == []


def test_defaults_are_page1_size50(client):
    resp = client.get("/api/v1/jobs/")
    data = resp.json()["data"]
    assert data["page"] == 1
    assert data["page_size"] == 50


# ── Filter by status ──────────────────────────────────────────────────────────

def test_filter_by_status_completed(client):
    _job(device="d1", status="completed")
    _job(device="d2", status="failed")
    _job(device="d3", status="pending")

    resp = client.get("/api/v1/jobs/?status=completed")
    data = resp.json()["data"]
    assert data["total"] == 1
    assert all(j["status"] == "completed" for j in data["items"])


def test_filter_by_status_failed(client):
    _job(device="d1", status="failed")
    _job(device="d2", status="failed")
    _job(device="d3", status="completed")

    resp = client.get("/api/v1/jobs/?status=failed")
    data = resp.json()["data"]
    assert data["total"] == 2
    assert all(j["status"] == "failed" for j in data["items"])


def test_invalid_status_returns_422(client):
    resp = client.get("/api/v1/jobs/?status=nonsense")
    assert resp.status_code == 422


# ── Filter by device_id ───────────────────────────────────────────────────────

def test_filter_by_device_id(client):
    _job(device="router-a")
    _job(device="router-a")
    _job(device="switch-b")

    resp = client.get("/api/v1/jobs/?device_id=router-a")
    data = resp.json()["data"]
    assert data["total"] == 2
    assert all(j["device"] == "router-a" for j in data["items"])


def test_filter_by_device_id_no_match(client):
    _job(device="router-a")
    resp = client.get("/api/v1/jobs/?device_id=unknown-device")
    assert resp.json()["data"]["total"] == 0


# ── Filter by date range ──────────────────────────────────────────────────────

def test_filter_from_date_excludes_older(client):
    _job(device="old", minutes_ago=120)
    _job(device="new", minutes_ago=10)

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    resp = client.get("/api/v1/jobs/", params={"from_date": cutoff})
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["device"] == "new"


def test_filter_to_date_excludes_newer(client):
    _job(device="old", minutes_ago=120)
    _job(device="new", minutes_ago=10)

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    resp = client.get("/api/v1/jobs/", params={"to_date": cutoff})
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["device"] == "old"


def test_date_range_is_inclusive_on_both_ends(client):
    now = datetime.now(timezone.utc)
    j = _job(device="boundary")
    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=j.job_id).first()
        row.created_at = now

    from_dt = (now - timedelta(seconds=1)).isoformat()
    to_dt = (now + timedelta(seconds=1)).isoformat()
    resp = client.get("/api/v1/jobs/", params={"from_date": from_dt, "to_date": to_dt})
    assert resp.json()["data"]["total"] == 1


def test_from_date_after_to_date_returns_422(client):
    later = datetime.now(timezone.utc)
    earlier = later - timedelta(hours=1)
    resp = client.get("/api/v1/jobs/", params={
        "from_date": later.isoformat(),
        "to_date": earlier.isoformat(),
    })
    assert resp.status_code == 422


# ── Combined filters ──────────────────────────────────────────────────────────

def test_combined_status_and_device_filter(client):
    _job(device="router-a", status="completed")
    _job(device="router-a", status="failed")
    _job(device="switch-b", status="completed")

    resp = client.get("/api/v1/jobs/?status=completed&device_id=router-a")
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["device"] == "router-a"
    assert data["items"][0]["status"] == "completed"


# ── Pagination ────────────────────────────────────────────────────────────────

def test_pagination_page_size(client):
    for i in range(5):
        _job(device=f"dev{i}", minutes_ago=i)

    resp = client.get("/api/v1/jobs/?page=1&page_size=2")
    data = resp.json()["data"]
    assert len(data["items"]) == 2
    assert data["total"] == 5
    assert data["page"] == 1
    assert data["page_size"] == 2


def test_pagination_page_two(client):
    for i in range(4):
        _job(device=f"pgdev{i}", minutes_ago=i)

    resp1 = client.get("/api/v1/jobs/?page=1&page_size=2")
    resp2 = client.get("/api/v1/jobs/?page=2&page_size=2")

    ids1 = {j["job_id"] for j in resp1.json()["data"]["items"]}
    ids2 = {j["job_id"] for j in resp2.json()["data"]["items"]}
    assert ids1.isdisjoint(ids2)
    assert len(ids1) == 2
    assert len(ids2) == 2


def test_pagination_beyond_last_page_returns_empty_items(client):
    _job(device="solo")
    resp = client.get("/api/v1/jobs/?page=99&page_size=50")
    data = resp.json()["data"]
    assert data["items"] == []
    assert data["total"] == 1


def test_results_ordered_newest_first(client):
    _job(device="oldest", minutes_ago=60)
    _job(device="middle", minutes_ago=30)
    _job(device="newest", minutes_ago=5)

    resp = client.get("/api/v1/jobs/")
    items = resp.json()["data"]["items"]
    assert items[0]["device"] == "newest"
    assert items[-1]["device"] == "oldest"
