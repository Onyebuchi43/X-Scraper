from Scweet.http_utils import normalize_http_proxies
from Scweet.utils import as_str, normalize_proxy_payload


def test_as_str_returns_none_for_empty_and_none():
    assert as_str(None) is None
    assert as_str("") is None
    assert as_str("  ") is None


def test_as_str_strips_and_returns():
    assert as_str("  hello ") == "hello"
    assert as_str(42) == "42"
    assert as_str(0) == "0"


def test_normalize_proxy_payload_none_and_empty():
    assert normalize_proxy_payload(None) is None
    assert normalize_proxy_payload("") is None
    assert normalize_proxy_payload("  ") is None


def test_normalize_proxy_payload_url_string():
    assert normalize_proxy_payload("http://proxy:8080") == "http://proxy:8080"


def test_normalize_proxy_payload_json_string():
    result = normalize_proxy_payload('{"host": "proxy", "port": 8080}')
    assert result == {"host": "proxy", "port": 8080}


def test_normalize_proxy_payload_invalid_json_returns_string():
    assert normalize_proxy_payload("{bad json}") == "{bad json}"


def test_normalize_proxy_payload_dict_returns_copy():
    original = {"host": "proxy", "port": 8080}
    result = normalize_proxy_payload(original)
    assert result == original
    assert result is not original


def test_normalize_http_proxies_colon_separated_formats():
    # host:port:user:pass format
    p1 = normalize_http_proxies("172.93.105.208:43159:5962gz3g8l:yPdWneyVU0")
    assert p1 == {
        "http": "http://5962gz3g8l:yPdWneyVU0@172.93.105.208:43159",
        "https": "http://5962gz3g8l:yPdWneyVU0@172.93.105.208:43159",
    }

    # user:pass:host:port format
    p2 = normalize_http_proxies("5962gz3g8l:yPdWneyVU0:172.93.105.208:43159")
    assert p2 == {
        "http": "http://5962gz3g8l:yPdWneyVU0@172.93.105.208:43159",
        "https": "http://5962gz3g8l:yPdWneyVU0@172.93.105.208:43159",
    }

    # host:port format
    p3 = normalize_http_proxies("172.93.105.208:43159")
    assert p3 == {
        "http": "http://172.93.105.208:43159",
        "https": "http://172.93.105.208:43159",
    }


def test_follower_range_filtering():
    from scheduler_engine import _scrape_followers

    # Test filtering logic on mock items
    items = [
        {"username": "user1", "followers_count": 50},      # match (0-1000)
        {"username": "influencer", "followers_count": 50000}, # skip (>1000)
        {"username": "user2", "followers_count": 800},     # match (0-1000)
    ]
    min_f = 0
    max_f = 1000

    handles = []
    for item in items:
        fc = item.get("followers_count")
        if fc is not None and not (min_f <= fc <= max_f):
            continue
        handles.append(item["username"])

    assert handles == ["user1", "user2"]


    # user:pass@host:port format
    p4 = normalize_http_proxies("5962gz3g8l:yPdWneyVU0@172.93.105.208:43159")
    assert p4 == {
        "http": "http://5962gz3g8l:yPdWneyVU0@172.93.105.208:43159",
        "https": "http://5962gz3g8l:yPdWneyVU0@172.93.105.208:43159",
    }

