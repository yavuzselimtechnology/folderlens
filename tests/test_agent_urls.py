import io
import socket
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from agentarm.url_reader import URLReader, normalize_host
from agentarm.workspace import AgentPolicy, Workspace, digest


class Response(io.BytesIO):
    def __init__(self, data=b"example content", status=200, **headers):
        super().__init__(data)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "text/plain; charset=utf-8"
        for name, value in headers.items():
            self.headers.replace_header(
                name, value
            ) if name in self.headers else self.headers.add_header(name, value)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class URLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        policy = AgentPolicy(
            Path(self.temp.name), url_hosts=("example.com", "other.example")
        )
        self.reader = URLReader(Workspace(policy))

    def dns(self, address):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
        ]

    def mock_connection(self, responses):
        connection = unittest.mock.MagicMock()
        connection.getresponse.side_effect = responses
        return patch(
            "agentarm.url_reader.http.client.HTTPSConnection", return_value=connection
        ), connection

    def test_requires_local_optin_and_public_dns_every_time(self):
        for url in [
            "http://example.com",
            "https://example.com:444/x",
            "https://user:password@example.com",
            "https://elsewhere.example",
            "https://example.com/#fragment",
            "https://example.com/\nprivate",
        ]:
            with (
                self.subTest(url=url),
                self.assertRaises((ValueError, PermissionError)),
            ):
                self.reader.validate_url(url)
        for address in [
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "::1",
            "::ffff:127.0.0.1",
            "224.0.0.1",
        ]:
            with (
                self.subTest(address=address),
                patch("socket.getaddrinfo", return_value=self.dns(address)),
                self.assertRaises(PermissionError),
            ):
                self.reader.addresses("example.com")
        with self.assertRaises(ValueError):
            normalize_host("*.example.com")

    def test_connection_uses_validated_ip_without_second_dns_lookup(self):
        with patch("socket.getaddrinfo", return_value=self.dns("93.184.216.34")):
            connection = self.reader.connection("example.com")
        with patch("socket.create_connection") as connect:
            connection._create_connection(("example.com", 443), timeout=7)
            connect.assert_called_once_with(("93.184.216.34", 443), 7, None)
        self.assertEqual(connection.host, "example.com")
        connection.close()

    def test_paginated_text_cache_and_hash_conflict(self):
        context, connection = self.mock_connection(
            [Response("Türkçe content".encode()), Response(b"changed")]
        )
        with (
            context,
            patch("socket.getaddrinfo", return_value=self.dns("93.184.216.34")),
        ):
            first = self.reader.read("https://example.com/data", length=4)
            second = self.reader.read(
                "https://example.com/data", offset=4, expected_sha256=first["sha256"]
            )
            self.assertEqual(first["content"] + second["content"], "Türkçe content")
            self.assertEqual(connection.request.call_count, 1)
            self.assertEqual(first["sha256"], digest("Türkçe content".encode()))
            with self.assertRaisesRegex(ValueError, "changed"):
                self.reader.read(
                    "https://example.com/data",
                    expected_sha256=first["sha256"],
                    refresh=True,
                )

    def test_redirect_validation_and_size_content_type_limits(self):
        context, connection = self.mock_connection(
            [Response(status=302, Location="https://not-allowed.example/data")]
        )
        with (
            context,
            patch("socket.getaddrinfo", return_value=self.dns("93.184.216.34")),
            self.assertRaises(PermissionError),
        ):
            self.reader.read("https://example.com/data")
        self.assertEqual(connection.request.call_count, 1)
        for response in [
            Response(**{"Content-Length": "999999999"}),
            Response(**{"Content-Encoding": "gzip"}),
            Response(**{"Content-Type": "application/octet-stream"}),
            Response(status=500),
        ]:
            context, _ = self.mock_connection([response])
            with (
                context,
                patch("socket.getaddrinfo", return_value=self.dns("93.184.216.34")),
                self.assertRaises(ValueError),
            ):
                self.reader.read("https://example.com/data", refresh=True)

    def test_redirect_to_allowed_host_and_cache_bound(self):
        self.reader.max_cached = 1
        context, connection = self.mock_connection(
            [
                Response(status=302, Location="https://other.example/final"),
                Response(b"ok"),
                Response(b"next"),
            ]
        )
        with (
            context,
            patch("socket.getaddrinfo", return_value=self.dns("93.184.216.34")),
        ):
            result = self.reader.read("https://example.com/data")
            self.assertEqual(result["url"], "https://other.example/final")
            self.reader.read("https://example.com/next")
        self.assertEqual(len(self.reader.cache), 1)
        self.assertEqual(connection.request.call_count, 3)

    def test_deadline_shuts_down_socket_retained_by_close_delimited_response(self):
        callbacks = []
        context, connection = self.mock_connection([])
        active = connection.sock

        class SlowResponse(Response):
            def read(self, size=-1):
                callbacks[0]()
                return super().read(size)

        def get_response():
            # HTTPConnection does this for a Connection: close response; the
            # response's file still owns a socket even though connection.sock is None.
            connection.sock = None
            return SlowResponse()

        def timer(seconds, callback):
            callbacks.append(callback)
            return unittest.mock.MagicMock()

        connection.getresponse.side_effect = get_response
        with (
            context,
            patch("socket.getaddrinfo", return_value=self.dns("93.184.216.34")),
            patch("threading.Timer", side_effect=timer),
            self.assertRaises(TimeoutError),
        ):
            self.reader.read("https://example.com/data")
        active.shutdown.assert_called_once_with(socket.SHUT_RDWR)
