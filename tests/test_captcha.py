from __future__ import annotations

import httpx
import pytest

from app.services.captcha import CaptchaError, CaptchaVerifier


def verifier(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return CaptchaVerifier(
        endpoint="https://captcha.example.test/siteverify",
        secret="super-secret",
        allowed_hosts={"captcha.example.test"},
        transport=transport,
        **kwargs,
    )


@pytest.mark.anyio
async def test_verifies_on_server_and_sends_secret_without_exposing_it():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://captcha.example.test/siteverify"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.content == b"secret=super-secret&response=answer&remoteip=192.0.2.1"
        return httpx.Response(200, json={"success": True})

    assert await verifier(handler).verify("answer", remote_ip="192.0.2.1") is True


@pytest.mark.anyio
async def test_success_is_bound_to_expected_hostname_and_action():
    async def allowed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "success": True,
            "hostname": "gateway.example",
            "action": "register",
        })

    check = verifier(allowed, expected_hostname="gateway.example")
    assert await check.verify("answer", expected_action="register") is True

    async def wrong_host(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "success": True,
            "hostname": "evil.example",
            "action": "register",
        })

    assert await verifier(wrong_host, expected_hostname="gateway.example").verify(
        "answer", expected_action="register",
    ) is False

    async def wrong_action(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "success": True,
            "hostname": "gateway.example",
            "action": "password_reset",
        })

    assert await verifier(wrong_action, expected_hostname="gateway.example").verify(
        "answer", expected_action="register",
    ) is False


@pytest.mark.anyio
async def test_success_missing_required_binding_fails_closed():
    async def missing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True})

    assert await verifier(missing, expected_hostname="gateway.example").verify(
        "answer", expected_action="register",
    ) is False


@pytest.mark.anyio
async def test_invalid_or_unusable_response_fails_closed_without_leaking_body():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"secret=super-secret internal stack trace")

    check = verifier(handler)
    with pytest.raises(CaptchaError) as exc_info:
        await check.verify("answer")
    assert str(exc_info.value) == "验证码服务不可用"
    assert "super-secret" not in str(exc_info.value)
    assert "stack trace" not in str(exc_info.value)


@pytest.mark.anyio
async def test_redirects_are_not_followed_and_oversized_response_fails_closed():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    with pytest.raises(CaptchaError):
        await verifier(handler).verify("answer")
    assert calls == 1

    async def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 129)

    with pytest.raises(CaptchaError):
        await verifier(oversized, max_response_bytes=128).verify("answer")


def test_endpoint_requires_https_or_loopback_and_exact_allowed_host():
    with pytest.raises(ValueError):
        CaptchaVerifier(endpoint="http://captcha.example.test/siteverify", secret="s", allowed_hosts={"captcha.example.test"})
    with pytest.raises(ValueError):
        CaptchaVerifier(endpoint="https://sub.captcha.example.test/siteverify", secret="s", allowed_hosts={"captcha.example.test"})
    assert CaptchaVerifier(endpoint="http://127.0.0.1:8080/siteverify", secret="s", allowed_hosts={"127.0.0.1"})


@pytest.mark.anyio
async def test_network_exception_is_sanitized_and_empty_token_is_rejected():
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret=super-secret", request=_request)

    with pytest.raises(CaptchaError) as exc_info:
        await verifier(handler).verify("answer")
    assert str(exc_info.value) == "验证码服务不可用"
    with pytest.raises(CaptchaError):
        await verifier(handler).verify("")
    with pytest.raises(CaptchaError):
        await verifier(handler).verify("x" * 2049)
