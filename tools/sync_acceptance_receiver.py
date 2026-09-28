"""Native phone acceptance receiver. Isolated data, production protocol and worker."""

import argparse
import json
import logging
import sys
import time
import threading
import ipaddress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import test_v34_open_speaker_identity as seed
from tests.annotation_sync_fixture import seed as seed_rows
from tests.test_phase2_samples import audio
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.adapters.transfer import (
    TransferDeviceTrustAdapter,
    V3UploadIngestAdapter,
)
from allday_asr.v3.interfaces.device_gateway import DeviceGateway
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService
from allday_asr.v3.interfaces.transfer.composition import create_transfer_server
from allday_asr.v3.interfaces.transfer.devices import (
    DeviceAuthManager,
    DeviceCredentialStore,
    DEVICE_ALGORITHM,
)
from allday_asr.v3.interfaces.transfer.tls import ensure_tls_identity
from allday_asr.v3.contracts import utterance_dto

p = argparse.ArgumentParser()
p.add_argument("--public-key", required=True)
p.add_argument("--host", required=True)
p.add_argument("--output", required=True)
p.add_argument("--real-model", action="store_true")
p.add_argument("--resume", action="store_true")
p.add_argument("--rows", type=int, default=1800)
args = p.parse_args()
root = Path(args.output).resolve()
if (
    not root.is_relative_to(Path("outputs").resolve())
    or not ipaddress.ip_address(args.host).is_private
):
    raise ValueError("isolated output/private address required")
root.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=root / "timings.log",
    level=logging.DEBUG,
    format="%(asctime)s %(threadName)s %(name)s %(message)s",
    encoding="utf-8",
)
seed.TEST_STATE = root / "synthetic"
f = seed.V34OpenSpeakerIdentityTests()
if args.resume:
    f.root = next((root / "synthetic").glob("v34-people-*"))
    f.paths = seed.V3CorePaths.from_state_dir(f.root)
    f.core = seed.compose_v3_core(
        f.paths,
        codex_settings=seed.CodexReminderSettings(enabled=False),
        speaker_embedding_provider=seed.FakeEmbeddingProvider({}),
    )
    f.core.initialize()
    sid = seed._session_id(2)
else:
    f.setUp()
    f._seed_track(1)
    if not args.real_model:
        synthetic = audio.__wrapped__(f)
        next(synthetic)
    sid, pid, ids = seed_rows(f, args.rows)
model_calls = []
if args.real_model:
    import numpy as np
    import soundfile as sf
    from allday_asr.v3.adapters.models.funasr import FunASRBackend
    from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider

    source = f.core.audio_store.path_for("fixture/2")
    source.parent.mkdir(parents=True, exist_ok=True)
    wave = (np.sin(np.arange(16000) * 2 * np.pi * 220 / 16000) * 0.1).astype(np.float32)
    with sf.SoundFile(
        source, "w", samplerate=16000, channels=1, format="WAV", subtype="PCM_16"
    ) as stream:
        for _ in range(args.rows):
            stream.write(wave)
    backend = FunASRBackend()
    backend.ensure_speaker_loaded()

    class ObservedBackend:
        def extract_speaker_embeddings(self, samples, *, batch_size=8):
            start = time.time()
            result = backend.extract_speaker_embeddings(samples, batch_size=batch_size)
            model_calls.append(
                {"start": start, "end": time.time(), "clips": len(samples)}
            )
            return result

    f.core.people._provider = FunASRSpeakerEmbeddingProvider(
        f.core.audio_store, backend_factory=ObservedBackend, temp_root=f.root / "clips"
    )


def factory():
    return SqliteUnitOfWork(f.core.database)


if not args.resume:
    with factory() as u:
        for row in u.evidence.connection.execute(
            "SELECT utterance_id FROM utterances"
        ).fetchall():
            item = u.evidence.get_utterance(row[0])
            u.changes.append(
                "utterance",
                item.utterance_id,
                item.revision,
                "upsert",
                utterance_dto(
                    item, speaker_label="synthetic", original_speaker_label="synthetic"
                ),
            )
        u.changes.append(
            "recording_session",
            sid,
            1,
            "upsert",
            {
                "session_id": sid,
                "state": "ready_for_processing",
                "captured_start": seed.NOW_TEXT,
            },
        )
identity = ensure_tls_identity(root / "tls", addresses=[args.host])
receiver = identity.ca_sha256_fingerprint.replace(":", "").lower()
auth = DeviceAuthManager(DeviceCredentialStore(root / "devices.json"))
if args.resume:
    record = auth.store.list_devices()[0]
else:
    record = auth.store.register(
        device_name="isolated acceptance phone",
        algorithm=DEVICE_ALGORITHM,
        public_key=Path(args.public_key).read_text().strip(),
        passkey_credential_id="isolated-local-bootstrap",
    )

trust = TransferDeviceTrustAdapter(auth, factory, receiver)
trust.enroll(record)
gateway = DeviceGateway(
    trust,
    f.core.mobile_sync,
    ingest=V3UploadIngestAdapter(
        trust, factory, f.core.audio_store, f.core.artifact_store
    ),
    review_service=DeviceReviewService(f.core),
)
server = create_transfer_server(
    inbox=root / "inbox",
    host=args.host,
    port=19100,
    tls_cert=identity.certificate_path,
    tls_key=identity.private_key_path,
    device_manager=trust,
    receiver_id=receiver,
    v3_gateway=gateway,
    max_chunk_bytes=65536,
)
evidence = {
    "upload_bytes": 0,
    "upload_complete": False,
    "workers": [],
    "model_calls": model_calls,
    "requests": [],
}
lock = threading.Lock()


def publish():
    with lock:
        (root / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")


original = server.store.append_chunk
active = threading.Event()


def append(*a, **kw):
    active.set()
    time.sleep(0.2)
    result = original(*a, **kw)
    evidence["upload_bytes"] = result.offset
    publish()
    return result


server.store.append_chunk = append
original_complete = server.notify_upload_completed


def complete(*a, **kw):
    result = original_complete(*a, **kw)
    if evidence["upload_bytes"] >= 16 * 1024 * 1024:
        evidence["upload_complete"] = True
        active.clear()
    publish()
    return result


server.notify_upload_completed = complete


class Handler(server.RequestHandlerClass):
    def _handle(self, callback):
        start = time.perf_counter()
        self.auth_ms = 0.0
        self.wire = {}
        try:
            super()._handle(callback)
        finally:
            evidence["requests"].append(
                {
                    "client_id": self.client_request_id,
                    "path": self.diagnostic_path,
                    "ms": (time.perf_counter() - start) * 1000,
                    "upload_bytes": evidence["upload_bytes"],
                    "worker_active": active.is_set(),
                    "auth_ms": self.auth_ms,
                    "wire": self.wire,
                }
            )
            publish()

    def _authenticate_device_request(self, binding):
        start = time.perf_counter()
        try:
            return super()._authenticate_device_request(binding)
        finally:
            self.auth_ms += (time.perf_counter() - start) * 1000

    def send_header(self, name, value):
        if name.lower() in {
            "content-length",
            "content-encoding",
            "x-allday-json-bytes",
            "x-allday-connection-id",
        }:
            self.wire[name] = value
        return super().send_header(name, value)


server.RequestHandlerClass = Handler


def worker():
    while True:
        if active.wait(0.2):
            start = time.perf_counter()
            try:
                with factory() as u:
                    u.people.enqueue_samples(sid)
                result = f.core.people.sample_worker.run_pending(1)
                evidence["workers"].append(
                    {
                        "start": time.time(),
                        "ms": (time.perf_counter() - start) * 1000,
                        "result": result,
                    }
                )
                publish()
            except Exception:
                logging.exception("acceptance worker")
            time.sleep(0.02)


threading.Thread(target=worker, daemon=True).start()
(root / "phone-config.json").write_text(
    json.dumps(
        {
            "baseUrl": f"https://{args.host}:19100",
            "receiverId": receiver,
            "deviceId": record.device_id,
            "fingerprint": identity.ca_sha256_fingerprint,
        }
    )
)
publish()
print("Acceptance receiver ready", flush=True)
server.serve_forever()
