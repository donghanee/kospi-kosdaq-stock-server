"""KRX HTTPS transport and mislabeled JSON session-validation regressions."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests


@pytest.fixture
def client_module(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "krx_https_test_module", Path(__file__).resolve().parents[1] / "krx_data_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def response(body, status=200, content_type="text/html; charset=utf-8"):
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Type"] = content_type
    result._content = body.encode()
    result.encoding = "utf-8"
    return result


def manager_with_response(module, result):
    manager = module.KRXAuthManager(krx_id="test-id", krx_pw="test-password", login_method="krx")
    manager.session.post = Mock(return_value=result)
    return manager


@pytest.mark.parametrize("content_type", ["application/json", "text/html; charset=utf-8"])
def test_valid_json_session_uses_https_even_with_html_header(client_module, content_type):
    manager = manager_with_response(client_module, response('{"output": []}', content_type=content_type))

    assert manager._validate_session() is True
    assert manager.session.post.call_args.args[0] == "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
    assert manager.session.headers["Origin"] == "https://data.krx.co.kr"
    assert manager.session.headers["Referer"] == "https://data.krx.co.kr/"


@pytest.mark.parametrize("body,status", [
    ("<html>login</html>", 200),
    ("", 200),
    ('{"RESULT": "LOGOUT"}', 200),
    ('{"error": "unavailable"}', 503),
    ("null", 200),
])
def test_invalid_session_is_rejected(client_module, body, status):
    manager = manager_with_response(client_module, response(body, status=status))
    assert manager._validate_session() is False


def test_market_query_posts_directly_to_https_preserving_parameters(client_module):
    rows = [{"ISU_SRT_CD": "005930", "TDD_CLSPRC": "100,000"}]
    manager = manager_with_response(client_module, response(json.dumps({"OutBlock_1": rows})))
    client = client_module.KRXDataClient.__new__(client_module.KRXDataClient)
    client._auth_manager = manager
    client._ensure_session = Mock()
    params = {"mktId": "ALL", "trdDd": "20261030"}

    assert client._request("dbms/MDC/STAT/standard/MDCSTAT01501", params) == rows
    manager.session.post.assert_called_once_with(
        "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd",
        data={"bld": "dbms/MDC/STAT/standard/MDCSTAT01501", **params},
        timeout=30,
    )


def test_non_json_error_has_transport_details_without_response_body(client_module):
    manager = manager_with_response(client_module, response("<html>private-session</html>"))
    client = client_module.KRXDataClient.__new__(client_module.KRXDataClient)
    client._auth_manager = manager
    client._ensure_session = Mock()

    with pytest.raises(client_module.KRXDataError) as error:
        client._request("test", {})

    assert "status=200" in str(error.value)
    assert "Content-Type=text/html" in str(error.value)
    assert "private-session" not in str(error.value)
