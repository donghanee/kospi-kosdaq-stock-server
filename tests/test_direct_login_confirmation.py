"""Offline regressions for KRX's HTML existing-session confirmation."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def client_module(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "krx_direct_login_test_module",
        Path(__file__).resolve().parents[1] / "krx_data_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def login_surface():
    state = {"authenticated": False}

    async def cookies(*_args):
        if state["authenticated"]:
            return [{"name": "mdc.client_session", "value": "test-session"}]
        return [{"name": "JSESSIONID", "value": "anonymous-session"}]

    page = SimpleNamespace(
        url="https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd",
        context=SimpleNamespace(cookies=AsyncMock(side_effect=cookies)),
    )
    prompt = SimpleNamespace(is_visible=AsyncMock(return_value=True))
    hidden_button = SimpleNamespace(is_visible=AsyncMock(return_value=False), click=AsyncMock())

    async def confirm():
        state["authenticated"] = True

    button = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock(side_effect=confirm))
    frame = Mock()
    frame.is_detached.return_value = False
    frame.get_by_text.side_effect = lambda text, **kwargs: (
        SimpleNamespace(first=prompt)
        if text == "이미 로그인된 계정입니다."
        else SimpleNamespace(all=AsyncMock(return_value=[hidden_button, button]))
    )
    return page, frame, state, prompt, button, hidden_button


@pytest.mark.asyncio
async def test_accepts_html_confirmation_and_waits_for_authenticated_cookie(client_module, monkeypatch):
    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    page, frame, state, prompt, button, hidden_button = login_surface()
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())

    await manager._wait_for_krx_login(page, frame)

    button.click.assert_awaited_once()
    hidden_button.click.assert_not_awaited()
    assert page.context.cookies.await_count == 2


@pytest.mark.asyncio
async def test_handles_confirmation_that_appears_after_initial_poll(client_module, monkeypatch):
    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    page, frame, state, prompt, button, _ = login_surface()
    prompt.is_visible.side_effect = [False, True]
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())

    await manager._wait_for_krx_login(page, frame)

    button.click.assert_awaited_once()
    assert page.context.cookies.await_count == 3


@pytest.mark.asyncio
async def test_authenticated_cookie_needs_no_confirmation(client_module):
    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    page, frame, state, prompt, button, _ = login_surface()
    state["authenticated"] = True

    await manager._wait_for_krx_login(page, frame)

    frame.get_by_text.assert_not_called()
    button.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_home_and_anonymous_cookie_are_not_login_success(client_module, monkeypatch):
    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    manager.LOGIN_WAIT_TIMEOUT = 1000
    page, frame, state, prompt, button, _ = login_surface()
    prompt.is_visible.return_value = False
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 2])))
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(client_module.KRXAuthError, match="인증 세션.*발급되지"):
        await manager._wait_for_krx_login(page, frame)

    button.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmation_without_session_still_fails_and_is_not_repeated(client_module, monkeypatch):
    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    manager.LOGIN_WAIT_TIMEOUT = 1000
    page, frame, state, prompt, button, _ = login_surface()
    button.click.side_effect = None
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0.5, 2])))
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(client_module.KRXAuthError, match="인증 세션.*발급되지"):
        await manager._wait_for_krx_login(page, frame)

    button.click.assert_awaited_once()
