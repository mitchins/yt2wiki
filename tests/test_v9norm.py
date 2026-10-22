from yt2wiki.v9.publish import normalize_timestamps as N

def test_range_seconds_hint():
    out = N("see [7:83-7:86] end", "https://youtube.com/watch?v=A", duration_s=887.0, hint_s=785.0)
    assert "[13:03-13:06]" in out, out

def test_decimal_colon():
    out = N("see [159:0](z)", None, duration_s=775.0, hint_s=160.0)
    assert out.strip() == "see 2:39", out

def test_none_target_and_past_eof():
    out = N("see [450:00](None) and [20:00](https://youtube.com/watch?v=A)", "https://youtube.com/watch?v=A", duration_s=438.0, hint_s=420.0)
    assert "(None)" not in out and "t=1200" not in out, out

def test_bare_decimal_and_assets_untouched():
    out = N("at [203.7] x ![a](assets/f.jpg)", "https://youtube.com/watch?v=A", duration_s=438.0, hint_s=200.0)
    assert "[3:24](https://youtube.com/watch?v=A&t=203)" in out, out
    assert "assets/f.jpg" in out

def test_descending_ranges_are_ordered():
    from yt2wiki.v9.publish import normalize_timestamps as N
    out = N("see [11:39-7:04](u) end", "https://youtube.com/watch?v=A", duration_s=777.0, hint_s=700.0)
    assert "[7:04-11:39]" in out, out
