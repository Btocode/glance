from glance.daemon.cache import LruCache, cache_key


def test_lru_evicts_oldest_and_keys_are_hashes():
    c = LruCache(2)
    c.put(cache_key("a"), 1)
    c.put(cache_key("b"), 2)
    c.get(cache_key("a"))
    c.put(cache_key("c"), 3)
    assert c.get(cache_key("b")) is None
    assert c.get(cache_key("a")) == 1
    assert c.get(cache_key("c")) == 3
    assert "a" != cache_key("a")
    assert len(cache_key("a")) == 64


def test_put_again_replaces_and_refreshes():
    c = LruCache(2)
    c.put("k1", 1)
    c.put("k2", 2)
    c.put("k1", 10)  # k2 is now the oldest
    c.put("k3", 3)
    assert c.get("k1") == 10 and c.get("k2") is None and c.get("k3") == 3


def test_clear_and_missing_key():
    c = LruCache()
    c.put("k", 1)
    c.clear()
    assert c.get("k") is None


def test_cache_key_separates_its_parts():
    assert cache_key("ab", "c") != cache_key("a", "bc")
    assert cache_key("ab", "c") != cache_key("abc")
    assert cache_key("a", "b") == cache_key("a", "b")
    assert len(cache_key("\ud800 lone surrogate")) == 64  # never raises on odd text


def test_cache_never_touches_the_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = LruCache(4)
    for n in range(10):
        c.put(cache_key(f"typed text {n}"), n)
    assert list(tmp_path.iterdir()) == []
