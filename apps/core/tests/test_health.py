def test_health_needs_no_login(db, anon):
    res = anon.get("/api/health/")
    assert res.status_code == 200 and res.json() == {"status": "ok"}
