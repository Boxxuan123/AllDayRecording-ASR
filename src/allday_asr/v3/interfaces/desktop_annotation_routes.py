from http import HTTPStatus


class DesktopAnnotationRoutesMixin:
    def _dispatch_annotation_post(self, path, body):
        if path == "/api/v3/voice-audition":
            from .device_reviews import DeviceReviewService
            self._send_json(HTTPStatus.OK, DeviceReviewService(self.application.core).desktop_audio(body))
            return True
        if path in {"/api/v3/annotation-status", "/api/v3/annotation-sample-retry"}:
            from .device_annotations import DeviceAnnotationService
            result = DeviceAnnotationService(self.application.core).execute('desktop', {
                'action': 'status' if path.endswith('status') else 'retry_samples',
                'utterance_ids': body.get('utterance_ids')})
            self._send_json(HTTPStatus.OK, result)
            return True
        if path == "/api/v3/annotation-undo":
            if set(body) != {"selections"}:
                raise ValueError("annotation undo fields are invalid")
            self._send_json(HTTPStatus.OK, self.application.core.corrections.undo_annotations(body["selections"]))
            return True
        if path == "/api/v3/utterance-classifications":
            if set(body) != {"selections", "sound_kind"}:
                raise ValueError("segment classification fields are invalid")
            result = self.application.core.corrections.classify_segments(body["selections"], body["sound_kind"])
            self._send_json(HTTPStatus.OK, result)
            return True
        return False
