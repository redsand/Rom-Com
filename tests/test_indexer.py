from romcom.indexer import rank, clean_query

def test_clean_query_strips_release_noise():
    # region/language/revision groups say which release of the same game, not which
    # game — and the site's search chokes on the parentheses themselves
    assert clean_query("10-Yard Fight (USA, Europe)") == "10-Yard Fight"
    assert clean_query("4 Nin Uchi Mahjong (Japan) (Rev 1)") == "4 Nin Uchi Mahjong"
    assert clean_query("Super Mario Bros. 3 (Europe) (En,Fr,De)") == "Super Mario Bros. 3"

def test_clean_query_keeps_identity_groups():
    # a multicart's board code is the ONLY thing separating it from its siblings;
    # deleting it collapses the query to a generic prefix every neighbour matches
    assert clean_query("4-in-1 (SN 406) (Asia) (En) (Pirate)") == "4-in-1 SN 406 Pirate"
    assert "Hwang Shinwei" in clean_query("3D Block (Taiwan) (En) (Hwang Shinwei) (Pirate)")
    # a status tag names a different artifact — dropping it would fetch the retail
    # ROM for a Demo item and mark it DOWNLOADED
    assert clean_query("Game (Demo)") == "Game Demo"

def test_clean_query_never_returns_empty():
    assert clean_query("(USA)") == "(USA)"

def test_rank_rejects_a_recall_only_near_miss():
    """The real ledger case: "Dragon Ball Z 4 in 1" shares 3 of 8 query tokens with
    "4-in-1 (SN 406) (Asia) (En) (Pirate)" — 37.5 recall, over the 20 floor — and was
    fetched for three different items."""
    rows = [{"title": "dragon ball z 4 in 1", "url": "wrong", "size": 1}]
    assert rank(rows, ["4-in-1 (SN 406) (Asia) (En) (Pirate)"]) == []

def test_rank_requires_the_multicart_number():
    # every other word matches; "4 in 1" is simply not "16-in-1"
    rows = [{"title": "super hik 4 in 1", "url": "wrong", "size": 1}]
    assert rank(rows, ["1997 Super HIK 16-in-1 (Asia) (En) (Pirate)"]) == []
    # ...but a leading year is not identity: sites drop it from the page title
    ok = [{"title": "super hik 16 in 1", "url": "right", "size": 1}]
    assert rank(ok, ["1997 Super HIK 16-in-1 (Asia) (En) (Pirate)"])[0]["url"] == "right"

def test_rank_still_accepts_a_real_match_with_extra_words():
    rows = [{"title": "4 nin uchi mahjong h1", "url": "b", "size": 1}]
    assert rank(rows, ["4 Nin Uchi Mahjong (Japan)"])[0]["score"] >= 20

def test_rank_prefers_title_match():
    rows=[
      {"title":"Random Collection","url":"a","size":100},
      {"title":"Nancy Drew Secrets Can Kill","url":"b","size":100},
    ]
    out=rank(rows,["Nancy Drew Secrets Can Kill"])
    assert out[0]["url"]=="b"

def test_rank_respects_size_bounds():
    rows=[
      {"title":"Archive","url":"small","size":10},
      {"title":"Archive","url":"good","size":1000},
    ]
    out=rank(rows,["Archive"],"volume",min_bytes=100,max_bytes=2000)
    assert [x["url"] for x in out]==["good"]
