"""Real TLS admission tests; every identity and socket is synthetic and local."""
import http.client
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
import shutil
import json
import logging

from allday_asr.v3.interfaces.transfer.composition import create_transfer_server
from allday_asr.v3.interfaces.transfer.tls import ensure_tls_identity
from allday_asr.v3.interfaces.transfer.http_handler import TransferRequestHandler


@contextmanager
def running_server():
    root = Path('outputs') / ('tls-admission-' + uuid4().hex)
    root.mkdir(parents=True)
    identity = ensure_tls_identity(root / 'tls', addresses=['127.0.0.1'])
    server = create_transfer_server(inbox=root / 'inbox', host='127.0.0.1', port=0,
                                    tls_cert=identity.certificate_path,
                                    tls_key=identity.private_key_path)
    server.handshake_timeout = 0.4
    context = ssl.create_default_context(cafile=str(identity.ca_certificate_path))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
    thread.start()
    try:
        yield server, context
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()
        shutil.rmtree(root)


def test_stalled_handshake_does_not_block_https():
    with running_server() as (server, context):
        slow = socket.create_connection(('127.0.0.1', server.port), timeout=2)
        try:
            # Accept the first TCP connection before starting the normal TLS client.
            time.sleep(0.08)
            client = http.client.HTTPSConnection('127.0.0.1', server.port, context=context, timeout=1)
            try:
                client.request('GET', '/synthetic-nonexistent')
                response = client.getresponse()
                assert response.status == 404
                response.read()
            finally:
                client.close()
        finally:
            slow.close()


def test_handshake_timeout_and_shutdown_release_sockets():
    with running_server() as (server, _):
        for _ in range(3):
            slow = socket.create_connection(('127.0.0.1', server.port), timeout=2)
            try:
                assert slow.recv(1) == b''
            finally:
                slow.close()
        deadline = time.monotonic() + 2
        while server.active_connection_count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.active_connection_count == 0


def test_admission_is_bounded_and_close_interrupts_handshakes():
    with running_server() as (server, _):
        server.handshake_timeout = 10
        clients = []
        try:
            for _ in range(server.max_connections):
                clients.append(socket.create_connection(('127.0.0.1', server.port), timeout=2))
            deadline = time.monotonic() + 2
            while server.active_connection_count < server.max_connections and time.monotonic() < deadline:
                time.sleep(0.01)
            extra = socket.create_connection(('127.0.0.1', server.port), timeout=2)
            try:
                assert extra.recv(1) == b''
            finally:
                extra.close()
            server.shutdown()
            server.server_close()
            assert server.active_connection_count == 0
        finally:
            for client in clients:
                client.close()


def test_internal_error_has_safe_correlated_stack(monkeypatch, caplog):
    def fail(_handler):
        raise RuntimeError('PRIVATE token and transcript C:/sensitive/audio.wav')
    monkeypatch.setattr(TransferRequestHandler, '_dispatch_get', fail)
    with caplog.at_level(logging.INFO), running_server() as (server, context):
        client = http.client.HTTPSConnection('127.0.0.1', server.port, context=context, timeout=2)
        try:
            client.request('GET', '/device/v3/status', headers={'XAllDayRequestId':'synthetic-attempt-2'})
            response = client.getresponse()
            body = json.loads(response.read())
            assert response.status == 500
            assert body['code'] == 'INTERNAL_SERVER_ERROR'
            assert len(body['request_id']) == 26
            assert response.getheader('X-AllDay-Request-ID') == body['request_id']
            assert body['request_id'] in caplog.text
            assert 'synthetic-attempt-2' in caplog.text
            assert 'RuntimeError' in caplog.text and 'fail:' in caplog.text
            assert 'PRIVATE' not in caplog.text + json.dumps(body)
            assert 'sensitive' not in caplog.text + json.dumps(body)
        finally:
            client.close()


def test_successful_polling_is_quiet_at_info(monkeypatch, caplog):
    monkeypatch.setattr(TransferRequestHandler, '_dispatch_get', lambda handler: handler._send_json(200, {'ok': True}))
    with caplog.at_level(logging.INFO), running_server() as (server, context):
        for _ in range(12):
            client = http.client.HTTPSConnection('127.0.0.1', server.port, context=context, timeout=2)
            try:
                client.request('GET', '/healthz')
                response = client.getresponse()
                response.read()
                assert response.status == 200
            finally:
                client.close()
        assert not [r for r in caplog.records if r.levelno == logging.INFO and 'transfer id=' in r.message]
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
