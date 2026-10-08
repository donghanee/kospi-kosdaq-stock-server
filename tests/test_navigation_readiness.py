"""KRX documents must become usable even when background requests remain open."""

import importlib.util
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def client_module(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "krx_navigation_test_module",
        Path(__file__).resolve().parents[1] / "krx_data_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def streaming_page():
    release = threading.Event()
    requested = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == "/stream":
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "1000000")
                self.end_headers()
                self.wfile.write(b"still loading")
                self.wfile.flush()
                requested.set()
                release.wait(10)
            else:
                body = b'<input id="mbrId"><script>fetch("/stream").then(r => r.text());</script>'
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/login", requested
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_usable_login_document_does_not_wait_for_background_network(
    client_module, streaming_page
):
    from playwright.async_api import TimeoutError, async_playwright

    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    manager.PAGE_LOAD_TIMEOUT = 2000
    url, requested = streaming_page
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            response = await manager._goto_ready_document(page, url)
            assert response.status == 200
            await page.locator("#mbrId").fill("test-user")
            assert await page.locator("#mbrId").input_value() == "test-user"
            assert requested.wait(1)
            # The old readiness condition still fails on this same usable page.
            with pytest.raises(TimeoutError):
                await page.wait_for_load_state("networkidle", timeout=750)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_real_navigation_failure_is_preserved(client_module):
    from playwright.async_api import TimeoutError

    manager = client_module.KRXAuthManager.__new__(client_module.KRXAuthManager)
    error = TimeoutError("main document unavailable")
    page = SimpleNamespace(goto=AsyncMock(side_effect=error))
    with pytest.raises(TimeoutError) as caught:
        await manager._goto_ready_document(page, "https://data.krx.co.kr/login")
    assert caught.value is error
