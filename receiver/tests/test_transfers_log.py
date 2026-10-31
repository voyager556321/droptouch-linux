import transfers


def test_record_replace_and_failures(tmp_path):
    p = tmp_path / "t.json"
    transfers.record("a.jpg", "interrupted", sha="aa", path=p, now=1)
    transfers.record("a.jpg", "ok", sha="aa", saved_as="a.jpg", path=p, now=2)
    transfers.record("b.pdf", "failed", error="disk full", path=p, now=3)
    items = transfers.load(p)
    assert [e["state"] for e in items] == ["ok", "failed"]          # resume replaced the partial entry
    assert transfers.failed(items)[0]["name"] == "b.pdf"
    assert "b.pdf — disk full" in transfers.failure_body(items, 0)
    assert transfers.failure_body(items, 10) == ""
    transfers.clear(p, only_failed=True)
    assert [e["name"] for e in transfers.load(p)] == ["a.jpg"]       # persisted file survives a reload


def test_keep_bounded(tmp_path):
    p = tmp_path / "t.json"
    for i in range(transfers.KEEP + 5):
        transfers.record(f"{i}", "ok", path=p, now=i)
    assert len(transfers.load(p)) == transfers.KEEP
    assert transfers.load(tmp_path / "missing.json") == []
