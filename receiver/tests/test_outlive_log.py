import logging
from outbox import Outbox


def test_outlive_logs_start_and_end_even_without_a_phone(caplog, tmp_path):
    ob = Outbox.__new__(Outbox)
    ob._link = lambda: None
    with caplog.at_level(logging.INFO):
        ob.out_live("top", 0.5, 0.2)
        ob.out_live("top", 0.5, 0.3)
        ob.out_live_end(True)
    text = caplog.text
    assert "OutLive start edge=top" in text and "NO PHONE CONNECTED" in text
    assert "after 2 OutLive frame(s)" in text
