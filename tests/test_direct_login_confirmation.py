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

    async def confirm(**_kwargs):
        state["authenticated"] = True

    button = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock(side_effect=confirm))
    frame = Mock()
    frame.is_detached.return_value = False
    frame.get_by_text.side_effect = lambda text, **kwargs: (
        SimpleNamespace(first=prompt)
        if text == "이미 로그인된 계정입니다."
        else SimpleNamespace(all=AsyncMock(return_value=[hidden_button, button]))
    )
    page.frames = [frame]
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
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0, 0.5, 2])))
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(client_module.KRXAuthError, match="인증 세션.*발급되지"):
        await manager._wait_for_krx_login(page, frame)

    button.click.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prompt", "buttons", "button_visibility", "click"])
async def test_frame_detachment_during_confirmation_waits_for_cookie(client_module, monkeypatch, stage):
    from playwright.async_api import Error

    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    page, frame, state, prompt, button, _ = login_surface()

    async def detach(*_args, **_kwargs):
        state["authenticated"] = True
        frame.is_detached.return_value = True
        page.frames = []
        raise Error("Locator.is_visible: Frame was detached")

    if stage == "prompt":
        prompt.is_visible.side_effect = detach
    elif stage == "buttons":
        original = frame.get_by_text.side_effect
        frame.get_by_text.side_effect = lambda text, **kwargs: (
            SimpleNamespace(all=AsyncMock(side_effect=detach)) if text == "확인" else original(text, **kwargs)
        )
    elif stage == "button_visibility":
        button.is_visible.side_effect = detach
    else:
        button.click.side_effect = detach
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())

    await manager._wait_for_krx_login(page, frame)

    assert page.context.cookies.await_count == 2


@pytest.mark.asyncio
async def test_reacquires_replacement_frame_for_confirmation(client_module, monkeypatch):
    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    page, old_frame, state, _, _, _ = login_surface()
    _, new_frame, new_state, _, button, _ = login_surface()
    old_frame.is_detached.return_value = True
    page.frames = []

    async def replace_frame(*_args):
        page.frames = [new_frame]
        if new_state["authenticated"]:
            state["authenticated"] = True

    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock(side_effect=replace_frame))
    # Keep this bounded even in the broken implementation.
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 0.1, 0.1, 0.2, 999])))

    await manager._wait_for_krx_login(page, old_frame)

    old_frame.get_by_text.assert_not_called()
    button.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigation_context_loss_is_retried(client_module, monkeypatch):
    from playwright.async_api import Error

    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    page, frame, _, prompt, button, _ = login_surface()
    prompt.is_visible.side_effect = [Error("Execution context was destroyed, most likely because of a navigation"), True]
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())

    await manager._wait_for_krx_login(page, frame)

    button.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrelated_browser_error_is_not_hidden(client_module):
    from playwright.async_api import Error

    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    page, frame, _, prompt, _, _ = login_surface()
    prompt.is_visible.side_effect = Error("Target page, context or browser has been closed")

    with pytest.raises(Error, match="browser has been closed"):
        await manager._wait_for_krx_login(page, frame)


@pytest.mark.asyncio
async def test_detached_frame_without_authenticated_cookie_times_out(client_module, monkeypatch):
    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    manager.LOGIN_WAIT_TIMEOUT = 1000
    page, frame, _, _, button, _ = login_surface()
    frame.is_detached.return_value = True
    page.frames = []
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 2])))
    monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(client_module.KRXAuthError, match="인증 세션.*발급되지"):
        await manager._wait_for_krx_login(page, frame)

    button.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_browser_frame_removed_between_lookup_and_visibility(client_module, monkeypatch):
    from playwright.async_api import Error, async_playwright

    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    manager.LOGIN_WAIT_TIMEOUT = 3000
    observed_errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.route("https://data.krx.co.kr/**", lambda route: route.fulfill(
                body='<iframe srcdoc="<p>이미 로그인된 계정입니다.</p><button>확인</button>"></iframe>',
                content_type="text/html",
            ))
            await page.goto("https://data.krx.co.kr/login-test")
            frame = page.frames[1]
            get_by_text = frame.get_by_text

            def racing_locator(text, **kwargs):
                locator = get_by_text(text, **kwargs)
                if text != "이미 로그인된 계정입니다.":
                    return locator

                async def detach_before_read():
                    await page.evaluate("document.querySelector('iframe').remove()")
                    await context.add_cookies([{
                        "name": "mdc.client_session", "value": "test-session",
                        "url": "https://data.krx.co.kr",
                    }])
                    try:
                        return await locator.first.is_visible()
                    except Error as exc:
                        observed_errors.append(str(exc))
                        raise

                return SimpleNamespace(first=SimpleNamespace(is_visible=detach_before_read))

            monkeypatch.setattr(frame, "get_by_text", racing_locator)
            await manager._wait_for_krx_login(page, frame)

            assert frame.is_detached()
            assert any("detached" in message.lower() for message in observed_errors)
        finally:
            await browser.close()
