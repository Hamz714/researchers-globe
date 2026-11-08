

def test_shifted_event_date_is_seeded_and_avoids_gdelt_gaps():
    from datetime import date, timedelta

    from eval.metrics import MIN_VALID_RECENT, shifted_event_date
    from pipeline.fetch_gdelt import in_gap

    real_event, real_end = date(2026, 11, 3), date(2026, 11, 8)
    d = shifted_event_date(real_event, real_end)
    assert d == shifted_event_date(real_event, real_end)  # seeded
    end = d + (real_end - real_event)
    assert end < real_end - timedelta(days=7)  # no overlap with the real recent window
    assert sum(not in_gap(end - timedelta(days=i)) for i in range(7)) >= MIN_VALID_RECENT


def test_run_query_criteria_override_moves_the_event():
    from reasoning.run_query import _merge

    base = {"event": {"name": "x", "date": "2026-11-03", "terms": ["a"]}, "window": {"recent_days": 7}}
    out = _merge(base, {"event": {"date": "2026-10-15"}})
    assert out["event"] == {"name": "x", "date": "2026-10-15", "terms": ["a"]} and base["event"]["date"] == "2026-11-03"
