from textutil import title_case, word_count

def test_hyphen_parts():
    assert word_count("state-of-the-art design") == 5
    assert word_count("-well--known-") == 2
    assert word_count("a b  c") == 3

def test_title_case_unchanged():
    assert title_case("state-of-the-art design") == "State-of-the-art Design"
