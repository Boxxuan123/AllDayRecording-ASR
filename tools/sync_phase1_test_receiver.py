"""Loopback-only synthetic slow TLS receiver, NEVER a production management API.

Use hdc rport tcp:19099 tcp:19099. The phone test entry uploads zero bytes only.
Run with the project's Python 3.12. Generated CA/key and progress stay in outputs.
"""
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from allday_asr.v3.interfaces.transfer.tls import ensure_tls_identity

root = Path('outputs/sync-phase1-test-receiver')
root.mkdir(parents=True, exist_ok=True)
identity = ensure_tls_identity(root / 'tls', addresses=['127.0.0.1'])
state = {'received': 0, 'total': 0, 'started': 0, 'ended': 0}
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def do_PUT(self):
        total = int(self.headers.get('Content-Length', '0'))
        if self.path != '/synthetic' or total != 8 * 1024 * 1024:
            self.send_error(400)
            return
        with lock:
            state.update(received=0, total=total, started=time.time(), ended=0)
        self.connection.settimeout(10)
        try:
            while state['received'] < total:
                data = self.rfile.read(min(8192, total - state['received']))
                if not data:
                    break
                if any(data):
                    raise ValueError('only synthetic zeros are accepted')
                with lock:
                    state['received'] += len(data)
                time.sleep(0.08)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')
        except (OSError, ValueError):
            pass
        finally:
            with lock:
                state['ended'] = time.time()
                (root / 'last-transfer.json').write_text(json.dumps(state))

    def do_GET(self):
        with lock:
            data = json.dumps(state).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


server = ThreadingHTTPServer(('127.0.0.1', 19099), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(identity.certificate_path, identity.private_key_path)
server.socket = context.wrap_socket(server.socket, server_side=True)
print('SYNTHETIC TLS receiver ready on loopback:19099', flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
