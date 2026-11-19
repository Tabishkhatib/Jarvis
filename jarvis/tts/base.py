from abc import ABC, abstractmethod

import numpy as np


class TTSProvider(ABC):
    """Every voice backend implements one method.

    synthesize(text) -> (audio, sample_rate)
    audio is a mono float32 numpy array in the range [-1, 1].
    """

    @abstractmethod
    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        ...


def pcm16_to_float(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
