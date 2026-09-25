"""Run actual built desktop, SQLite, FFmpeg and browser using synthetic audio only."""
import subprocess
import threading
from pathlib import Path

from tests.test_review_audio_boundaries import actual_audio, pending_sample, people
from tests.test_phase2_samples import short_rows
from allday_asr.v3.interfaces.desktop_server import V3DesktopApplication, V3DesktopHTTPServer
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork


def main():
    fixture = people.__wrapped__()
    f = next(fixture)
    server = None
    try:
        f, _, provider = actual_audio.__wrapped__(f)
        pid, candidate = pending_sample(f, short_rows(f))
        application = V3DesktopApplication(f.core, token='synthetic-browser-only')
        server = V3DesktopHTTPServer(('127.0.0.1', 0), application)
        application.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        script = Path(__file__).parents[1]/'all_day_recording_front/tests/review-audio-live-browser.mjs'
        subprocess.run(['node', str(script), application.base_url], check=True)
        with SqliteUnitOfWork(f.core.database) as u:
            assert not u.people.person_vectors(provider.model, provider.model_version)
        assert not f.core.people.list_review_candidates(pid, 'confirmed')
        print('PASS browser confirmed then independently withdrew actual grant; actual matching input empty')
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.close()


if __name__ == '__main__':
    main()
