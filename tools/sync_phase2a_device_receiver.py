"""Isolated real receiver for phone phase 2A. No unauthenticated control endpoint.
Bootstrap is a local public-key file; does not test the Passkey enrollment ceremony.
All network business requests still use production CA/TLS, challenges and signatures.
"""
import argparse
import json
import sys
import time
import threading
import socket
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import test_v34_open_speaker_identity as seed
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.adapters.transfer import TransferDeviceTrustAdapter, V3UploadIngestAdapter
from allday_asr.v3.interfaces.device_gateway import DeviceGateway
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService
from allday_asr.v3.interfaces.transfer.composition import create_transfer_server
from allday_asr.v3.interfaces.transfer.devices import DeviceAuthManager, DeviceCredentialStore, DEVICE_ALGORITHM
from allday_asr.v3.interfaces.transfer.tls import ensure_tls_identity

parser = argparse.ArgumentParser()
parser.add_argument('--public-key', required=True)
parser.add_argument('--bcd', action='store_true')
parser.add_argument('--output', default='outputs/sync-phase2a-device-receiver')
args = parser.parse_args()
root = Path(args.output).resolve()
if not root.is_relative_to(Path('outputs').resolve()):
    parser.error('--output must be an isolated directory inside this repository outputs/')
if (root / 'phone-config.json').exists():
    parser.error('Choose a fresh output directory; existing receiver evidence must be preserved')
root.mkdir(parents=True, exist_ok=True)
seed.TEST_STATE = root / 'synthetic'
fixture = seed.V34OpenSpeakerIdentityTests()
fixture.setUp()
fixture._seed_track(1)
fixture.core.corrections.classify_segments([{'utterance_id': seed._utterance_id(1), 'revision': 1}], 'live_speech')
if args.bcd:
    from tools.sync_bcd_fixtures import add_review, change_evidence
    for number in (2, 3, 4):
        add_review(fixture, number)
def factory():
    return SqliteUnitOfWork(fixture.core.database)
with factory() as uow:
    uow.changes.append('recording_session', seed._session_id(1), 1, 'upsert',
                      {'session_id': seed._session_id(1), 'state': 'ready_for_processing',
                       'captured_start': seed.NOW_TEXT})
identity = ensure_tls_identity(root / 'tls', addresses=['127.0.0.1'])
receiver_id = identity.ca_sha256_fingerprint.replace(':', '').lower()
auth = DeviceAuthManager(DeviceCredentialStore(root / 'devices.json'))
record = auth.store.register(device_name='phase2a synthetic phone', algorithm=DEVICE_ALGORITHM,
                             public_key=Path(args.public_key).read_text().strip(),
                             passkey_credential_id='isolated-local-bootstrap')
trust = TransferDeviceTrustAdapter(auth, factory, receiver_id)
trust.enroll(record)
gateway = DeviceGateway(trust, fixture.core.mobile_sync,
    ingest=V3UploadIngestAdapter(trust, factory, fixture.core.audio_store, fixture.core.artifact_store),
    review_service=DeviceReviewService(fixture.core))
server = create_transfer_server(inbox=root / 'inbox', host='127.0.0.1', port=19100,
    tls_cert=identity.certificate_path, tls_key=identity.private_key_path, device_manager=trust,
    receiver_id=receiver_id, v3_gateway=gateway, max_chunk_bytes=65536)
original_append = server.store.append_chunk
original_sync = gateway.synchronize
evidence = {'upload_bytes': 0, 'upload_complete': False, 'receipts': [], 'audio': [], 'requests': {}}
control = root / 'fault.json'


def fault_mode():
    try:
        return json.loads(control.read_text()).get('mode', '')
    except (FileNotFoundError, json.JSONDecodeError):
        return ''


# Local file only; the isolated server never exposes a fault-control API.
class IsolatedFaultHandler(server.RequestHandlerClass):
    def _handle(self, callback):
        route = self.path.split('?')[0]
        evidence['requests'][route] = evidence['requests'].get(route, 0) + 1
        if fault_mode() == 'offline':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        super()._handle(callback)

    def _send_json(self, status, payload):
        if self.path == '/device/v3/reviews/audio' and fault_mode() == 'audio-interrupt-once':
            control.write_text('{}')
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data[:len(data)//2])
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        super()._send_json(status, payload)


server.RequestHandlerClass = IsolatedFaultHandler
publish_lock = threading.Lock()

def publish():
    with publish_lock:
        uploads = server.store.list_uploads()
        evidence['upload_bytes'] = sum(r.offset for r in uploads if r.kind == 'recording')
        evidence['upload_complete'] = any(r.kind == 'manifest' and r.status == 'completed' for r in uploads)
        temp = root / 'evidence.tmp'
        temp.write_text(json.dumps(evidence, ensure_ascii=False))
        temp.replace(root / 'evidence.json')

def slow_append(*pos, **kw):
    time.sleep(0.65)
    result = original_append(*pos, **kw)
    evidence['upload_bytes'] = sum(r.offset for r in server.store.list_uploads() if r.kind == 'recording')
    evidence['upload_complete'] = any(r.kind == 'manifest' and r.status == 'completed'
                                      for r in server.store.list_uploads())
    publish()
    return result


original_notify = server.notify_upload_completed


def observed_complete(*pos, **kw):
    result = original_notify(*pos, **kw)
    publish()
    return result

def observed_sync(key, payload):
    mode = fault_mode()
    if args.bcd and mode in {'change-evidence', 'new-result'}:
        control.write_text('{}')
        if mode == 'change-evidence':
            change_evidence(fixture)
        else:
            add_review(fixture, 5)
        evidence[mode] = time.time()
    if mode == '500-once':
        control.write_text('{}')
        evidence['injected_500_at'] = time.time()
        publish()
        raise RuntimeError('synthetic recoverable failure')
    result = original_sync(key, payload)
    for receipt in result['receipts']:
        evidence['receipts'].append({'operation_id': receipt['operation_id'], 'status': receipt['status'],
            'at': time.time(), 'upload_bytes': evidence['upload_bytes'],
            'upload_complete': evidence['upload_complete']})
    publish()
    if mode == 'drop-once' and result['receipts']:
        control.write_text('{}')
        evidence['dropped_response_at'] = time.time()
        publish()
        raise ConnectionResetError('synthetic response loss after commit')
    return result

server.store.append_chunk = slow_append
server.notify_upload_completed = observed_complete
gateway.synchronize = observed_sync
original_audio = gateway.review_audio


def observed_audio(key, payload):
    started = time.time()
    if fault_mode() == 'slow-audio':
        time.sleep(3)
    response = original_audio(key, payload)
    evidence['audio'].append({'at': started, 'completed': time.time(), 'key': response['audio_content_key'],
        'cache_hit': response['cache_hit'], 'bytes': response['byte_length'], 'prefetch': payload.get('prefetch', False)})
    publish()
    return response


gateway.review_audio = observed_audio
(root / 'phone-config.json').write_text(json.dumps({
    'baseUrl': 'https://127.0.0.1:19100', 'receiverId': receiver_id, 'deviceId': record.device_id,
    'fingerprint': identity.ca_sha256_fingerprint, 'utteranceId': seed._utterance_id(1)}))
publish()
print('Isolated production TLS receiver ready on loopback:19100', flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
    fixture.tearDown()
