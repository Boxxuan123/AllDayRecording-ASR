"""Companion-sensitive fake model reproduces the failure at our API boundary."""

import numpy as np
import pytest

from allday_asr.v3.adapters.models.funasr import FunASRBackend


class PaddingSensitiveModel:
    def generate(self, input, batch_size):
        results = []
        for offset in range(0, len(input), batch_size):
            group = input[offset : offset + batch_size]
            padded = max(len(x) for x in group)
            for wave in group:
                results.append(
                    {
                        "spk_embedding": np.array(
                            [wave.sum(), len(wave), padded], dtype=np.float32
                        )
                    }
                )
        return results


@pytest.mark.parametrize("requested", [1, 2, 4, 8])
def test_same_audio_independent_of_short_long_and_mixed_companions(requested):
    backend = FunASRBackend(device="cpu")
    backend._speaker_model = PaddingSensitiveModel()
    target = np.array([1, 2, 3], dtype=np.float32)
    alone = backend.extract_speaker_embeddings([target], batch_size=1)[0]
    mixed = [
        target,
        np.ones(1),
        np.ones(100),
        np.ones(17),
        target,
        np.ones(43),
        np.ones(2),
        np.ones(77),
    ]
    result = backend.extract_speaker_embeddings(mixed, batch_size=requested)
    np.testing.assert_array_equal(result[0], alone)
    np.testing.assert_array_equal(result[4], alone)
    assert result.shape == (8, 3)


def test_invalid_batch_size_rejected_and_empty_input_stays_empty():
    backend = FunASRBackend(device="cpu")
    with pytest.raises(ValueError, match="positive"):
        backend.extract_speaker_embeddings([], batch_size=0)
    assert backend.extract_speaker_embeddings([]).shape == (0, 192)


def test_missing_embedding_fails_instead_of_misaligning_audio():
    class Missing:
        def generate(self, **kwargs):
            return [{"spk_embedding": np.ones(3)}]

    backend = FunASRBackend(device="cpu")
    backend._speaker_model = Missing()
    with pytest.raises(RuntimeError, match="数量不一致"):
        backend.extract_speaker_embeddings([np.ones(10), np.ones(11)])
