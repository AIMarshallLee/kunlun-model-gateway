import pytest

from scripts.customer_invite import validate_origin


@pytest.mark.parametrize("value", ["http://example.com", "https://user:pass@example.com", "https://example.com/path",
                                   "https://example.com?token=secret", "https://example.com#token=secret", "https://"])
def test_rejects_unsafe_origin(value):
    with pytest.raises(ValueError):
        validate_origin(value)


def test_accepts_https_origin():
    assert validate_origin("https://example.com/") == "https://example.com"
