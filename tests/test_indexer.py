from romcom.indexer import rank

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
