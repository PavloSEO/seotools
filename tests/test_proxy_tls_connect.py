"""An HTTP proxy CONNECT pins the target IP and verifies its original hostname."""

from __future__ import annotations

import datetime
import select
import socket
import socketserver
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from seohead.recon import net


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, _request, _address):
        pass  # The negative certificate test intentionally aborts one TLS handshake.


def _certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("example.test")]), False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path = tmp_path / "synthetic-cert.pem"
    key_path = tmp_path / "synthetic-key.pem"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert_path), str(key_path))
    client_context = ssl.create_default_context()
    client_context.load_verify_locations(cadata=cert_pem.decode())
    return server_context, client_context


def test_successful_connect_keeps_vetted_ip_but_uses_origin_sni_and_verification(
    monkeypatch, tmp_path
):
    server_context, client_context = _certificate(tmp_path)
    observed = {"connect": [], "sni": [], "host": []}
    server_context.set_servername_callback(
        lambda _socket, name, _context: observed["sni"].append(name)
    )

    class Origin(socketserver.StreamRequestHandler):
        def handle(self):
            self.rfile.readline()
            while line := self.rfile.readline():
                if line in (b"\r\n", b"\n"):
                    break
                if line.lower().startswith(b"host:"):
                    observed["host"].append(line.decode().strip())
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    class TLSOrigin(_Server):
        def get_request(self):
            connection, address = super().get_request()
            return server_context.wrap_socket(connection, server_side=True), address

    with TLSOrigin(("127.0.0.1", 0), Origin) as origin:
        origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
        origin_thread.start()

        class Proxy(socketserver.StreamRequestHandler):
            def handle(self):
                observed["connect"].append(self.rfile.readline().decode().strip())
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                upstream = socket.create_connection(("127.0.0.1", origin.server_address[1]))
                self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.wfile.flush()
                try:
                    sockets = (self.connection, upstream)
                    while readable := select.select(sockets, [], [], 5)[0]:
                        for source in readable:
                            try:
                                payload = source.recv(65536)
                            except ConnectionError:
                                return
                            if not payload:
                                return
                            (upstream if source is self.connection else self.connection).sendall(
                                payload
                            )
                finally:
                    upstream.close()

        with _Server(("127.0.0.1", 0), Proxy) as proxy:
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            original_lookup = socket.getaddrinfo

            def lookup(host, port, *args, **kwargs):
                if host in {"example.test", "other.test"}:
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
                return original_lookup(host, port, *args, **kwargs)

            monkeypatch.setattr(net.socket, "getaddrinfo", lookup)
            route = net.resolve_proxy_route(
                f"http://127.0.0.1:{proxy.server_address[1]}", allow_private=True
            )
            client, http2 = net.http_client(
                5.0, verify=client_context, **net.crawl_transport_options(route)
            )
            try:
                assert http2 is False  # avoid cross-host tunnel reuse on one pinned IP
                assert client.get("https://example.test/").text == "ok"
                with pytest.raises(net.NetworkUnavailable):
                    client.get("https://other.test/")
            finally:
                client.close()
                proxy.shutdown()
                proxy_thread.join(timeout=5)
        origin.shutdown()
        origin_thread.join(timeout=5)

    assert observed["connect"] == [
        "CONNECT 93.184.216.34:443 HTTP/1.1",
        "CONNECT 93.184.216.34:443 HTTP/1.1",
    ]
    assert observed["sni"] == ["example.test", "other.test"]
    assert observed["host"] == ["Host: example.test"]


def test_proxy_tls_stream_fails_closed_if_tunnel_identity_does_not_match():
    stream = net._ProxyTLSStream(object())
    token = net._PROXY_TLS_IDENTITY.set(("93.184.216.34", "example.test"))
    try:
        with pytest.raises(net.NetworkUnavailable, match="identity is unavailable"):
            stream.start_tls(ssl.create_default_context(), server_hostname="127.0.0.1")
    finally:
        net._PROXY_TLS_IDENTITY.reset(token)
