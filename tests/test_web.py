from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from codelite.tools.base import ToolError
from codelite.tools.web import _PinnedHTTPConnection, _PinnedHTTPSConnection, _public_addresses


def test_public_resolution_rejects_private_addresses() -> None:
    with patch(
        "codelite.tools.web.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("127.0.0.1", 80))],
    ):
        with pytest.raises(ToolError, match="Private"):
            _public_addresses("http://rebind.example/")


def test_http_connection_uses_the_validated_ip_not_the_hostname() -> None:
    socket = Mock()
    with patch("codelite.tools.web.socket.create_connection", return_value=socket) as connect:
        connection = _PinnedHTTPConnection(
            "rebind.example", addresses=("93.184.216.34",), timeout=3
        )
        connection.connect()

    connect.assert_called_once_with(("93.184.216.34", 80), 3, None)
    assert connection.sock is socket


def test_https_connection_preserves_the_hostname_for_tls() -> None:
    socket = Mock()
    context = Mock()
    wrapped = Mock()
    context.wrap_socket.return_value = wrapped
    with patch("codelite.tools.web.socket.create_connection", return_value=socket) as connect:
        connection = _PinnedHTTPSConnection(
            "rebind.example", addresses=("93.184.216.34",), timeout=3, context=context
        )
        connection.connect()

    connect.assert_called_once_with(("93.184.216.34", 443), 3, None)
    context.wrap_socket.assert_called_once_with(socket, server_hostname="rebind.example")
    assert connection.sock is wrapped
