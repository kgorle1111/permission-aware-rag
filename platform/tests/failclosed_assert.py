"""Shared assertion for the fail-closed HTTP contract (kept out of conftest.py, whose hash is pinned)."""


def assert_failed_closed(response):
    """A failed-closed /query is a 503 with a fixed body, never the canonical empty 200."""
    assert response.status_code == 503
    assert response.json() == {"error": "retrieval unavailable",
                               "request_id": response.headers["X-Request-ID"]}
