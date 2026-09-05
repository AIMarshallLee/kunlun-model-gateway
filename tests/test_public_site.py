from sqlalchemy import select

from app.models import ModelPrice
from test_managed_gateway import managed


def test_managed_home_and_guide_are_public_but_console_keeps_identity_routes(managed):
    client = managed[0]
    home = client.get("/")
    assert home.status_code == 200 and 'lang="en"' in home.text
    assert 'data-page="home"' in home.text and '/assets/site.js' in home.text
    for route in ("/api-guide", "/service-info"):
        response = client.get(route)
        assert response.status_code == 200 and 'data-page=' in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    for route in ("/console", "/verify-email", "/reset-password"):
        assert 'id="login-form"' in client.get(route).text


def test_public_catalog_uses_current_db_selling_prices_and_excludes_supply_secrets(managed):
    client = managed[0]
    response = client.get("/public/catalog")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["environment"] == "test" and not body["purchasing_enabled"]
    assert body["registration_enabled"]
    assert body["models"][0]["input_microusd_per_million"] == 1000000
    assert not any(word in response.text for word in ("secret", "api_key", "upstream_cost", "platform_budget", "vault", "500000"))
    with client.app.state.SessionLocal() as db:
        price = db.scalar(select(ModelPrice))
        price.input_microusd_per_million = 2000000
        db.commit()
    assert client.get("/public/catalog").json()["models"][0]["input_microusd_per_million"] == 2000000
    with client.app.state.SessionLocal() as db:
        db.scalar(select(ModelPrice)).active = False
        db.commit()
    assert client.get("/public/catalog").json()["models"] == []


def test_legacy_mode_does_not_publish_commercial_catalog(client):
    assert 'id="login-form"' in client.get("/").text
    assert client.get("/public/catalog").status_code == 404


def test_site_javascript_does_not_use_html_injection_or_persist_credentials(managed):
    response = managed[0].get("/assets/site.js")
    assert response.status_code == 200
    for forbidden in ("innerHTML", "localStorage", "sessionStorage", "eval("):
        assert forbidden not in response.text


def test_public_policy_links_reject_non_https_and_embedded_credentials(managed):
    client = managed[0]
    client.app.state.settings.terms_url = "javascript:alert(1)"
    client.app.state.settings.privacy_url = "https://private:password@example.test/privacy"
    body = client.get("/public/catalog").json()
    assert body["terms_url"] is None and body["privacy_url"] is None
    client.app.state.settings.terms_url = "https://example.test/terms"
    assert client.get("/public/catalog").json()["terms_url"] == "https://example.test/terms"


def test_checkout_returns_to_console_not_marketing_home(managed):
    script = managed[0].get("/assets/app.js").text
    assert 'return_url: `${window.location.origin}/console`' in script
