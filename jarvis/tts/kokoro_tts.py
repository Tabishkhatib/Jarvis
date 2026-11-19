import numpy as np

from jarvis.tts.base import TTSProvider


class KokoroTTS(TTSProvider):
    """Free, local, natural-sounding. Voices starting with 'b' are British."""

    SAMPLE_RATE = 24000

    def __init__(self, cfg):
        from kokoro import KPipeline

        self.voice = cfg.get("kokoro_voice", "bm_george")
        self.pipe = KPipeline(lang_code=self.voice[0])  # 'b' = British, 'a' = American

    def synthesize(self, text):
        chunks = []
        for _, _, audio in self.pipe(text, voice=self.voice):
            if hasattr(audio, "numpy"):
                audio = audio.numpy()
            chunks.append(np.asarray(audio, dtype=np.float32))
        if not chunks:
            return np.zeros(0, dtype=np.float32), self.SAMPLE_RATE
        return np.concatenate(chunks), self.SAMPLE_RATE
