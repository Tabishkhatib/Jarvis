import queue
import random
import threading
import time

import numpy as np
import sounddevice as sd


def record_until_enter(rate=16000):
    """Record from the default mic until the user presses Enter."""
    frames = []

    def callback(indata, _frames, _time, status):
        if status:
            print(status)
        frames.append(indata.copy())

    with sd.InputStream(samplerate=rate, channels=1, dtype="float32", callback=callback):
        input("  listening... [Enter] to stop ")

    if not frames:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(frames).flatten()


def load_breath(path):
    """Load an optional breath sound. Returns (audio, sr) or None."""
    if not path:
        return None
    import soundfile as sf

    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    peak = np.max(np.abs(audio)) or 1.0
    return audio / peak * 0.35, sr  # breaths should be quiet


class Player:
    """Plays sentences in order on a background thread, so the next sentence
    can be synthesized while the current one is speaking. Adds a short pause
    after each sentence and, sometimes, a breath before longer ones."""

    def __init__(self, pause_s=0.3, breath=None, breath_chance=0.0, hud=None):
        from jarvis.hud import NullHUD

        self.hud = hud or NullHUD()
        self.pause_s = pause_s
        self.breath = breath
        self.breath_chance = breath_chance
        self.last_spoke = 0.0  # when he last finished speaking (for echo protection)
        self.q = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            audio, sr = self.q.get()
            try:
                if audio.size:
                    long_sentence = audio.size > sr * 2.0
                    if self.breath and long_sentence and random.random() < self.breath_chance:
                        b, bsr = self.breath
                        sd.play(b, bsr)
                        sd.wait()
                    self.hud.speaking(True)
                    sd.play(audio, sr)
                    self._animate(audio, sr)
                    sd.wait()
                    if self.pause_s:
                        sd.sleep(int(self.pause_s * 1000))
            finally:
                self.last_spoke = time.time()
                if self.q.unfinished_tasks <= 1:  # nothing else queued: he's done talking
                    self.hud.speaking(False)
                self.q.task_done()

    def _animate(self, audio, sr, fps=60):
        """Send his voice loudness to the HUD in time with playback."""
        from jarvis.hud import NullHUD

        if isinstance(self.hud, NullHUD):
            return
        step = max(1, sr // fps)
        start = time.time()
        for i in range(0, len(audio), step):
            level = float(np.sqrt(np.mean(audio[i : i + step] ** 2)))
            self.hud.voice_level(level * 4.0)
            delay = start + (i + step) / sr - time.time()
            if delay > 0:
                time.sleep(delay)

    @property
    def busy(self):
        """True while anything is queued or playing."""
        return self.q.unfinished_tasks > 0

    def enqueue(self, audio, sr):
        self.q.put((audio, sr))

    def wait(self):
        self.q.join()
