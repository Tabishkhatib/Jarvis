"""Hands-free listening: works out when you start and stop talking.

Uses the loudness of the mic signal (calibrated to your room at startup), so no
button press is needed. While Jarvis is speaking, the mic input is ignored, so
he doesn't hear himself and reply to his own voice.
"""
import collections
import queue
import time

import numpy as np
import sounddevice as sd

BLOCK = 0.03  # seconds per audio block (30 ms)


class Listener:
    def __init__(self, rate, player, cfg, hud=None):
        from jarvis.hud import NullHUD

        self.hud = hud or NullHUD()
        self.rate = rate
        self.player = player
        self.block = int(rate * BLOCK)
        self.silence_end = cfg.get("silence_to_end", 0.8)     # pause that ends a sentence
        self.min_speech = cfg.get("min_speech", 0.35)         # ignore coughs and clicks
        self.max_speech = cfg.get("max_speech", 20.0)
        self.sensitivity = cfg.get("sensitivity", 3.0)        # x room noise to count as speech
        self.fixed_threshold = cfg.get("threshold")           # set a number to skip calibration
        self.echo_tail = cfg.get("echo_tail", 0.4)            # keep ignoring briefly after he speaks
        self.q = queue.Queue()
        self.stream = sd.InputStream(
            samplerate=rate, channels=1, dtype="float32",
            blocksize=self.block, callback=self._callback,
        )
        self.stream.start()
        self.threshold = self.fixed_threshold or self._calibrate()

    def _callback(self, indata, _frames, _time, _status):
        self.q.put(indata[:, 0].copy())

    def _calibrate(self, seconds=1.0):
        print("Calibrating to room noise, stay quiet for a second...")
        levels = []
        end = time.time() + seconds
        while time.time() < end:
            levels.append(float(np.sqrt(np.mean(self.q.get() ** 2))))
        noise = float(np.median(levels)) if levels else 0.002
        threshold = max(noise * self.sensitivity, 0.008)
        print(f"  room noise {noise:.4f}, speech threshold {threshold:.4f}")
        return threshold

    def _jarvis_speaking(self):
        return self.player.busy or (time.time() - self.player.last_spoke) < self.echo_tail

    def next_utterance(self, timeout=None):
        """Block until someone says something. Returns audio (float32), or None
        if nothing was said within `timeout` seconds."""
        pre_roll = collections.deque(maxlen=int(0.3 / BLOCK))  # keep the start of the first word
        frames, speaking, loud_run, quiet_run = [], False, 0, 0
        deadline = time.time() + timeout if timeout else None

        while True:
            if deadline and not speaking and time.time() > deadline:
                return None
            try:
                block = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._jarvis_speaking():
                pre_roll.clear()
                frames, speaking, loud_run, quiet_run = [], False, 0, 0
                if deadline:
                    deadline = time.time() + timeout  # don't count his own talking as idle
                continue

            rms = float(np.sqrt(np.mean(block ** 2)))
            loud = rms > self.threshold
            self._mic_tick = getattr(self, "_mic_tick", 0) + 1
            if self._mic_tick % 2 == 0:  # ~15 updates a second is plenty for the HUD
                self.hud.mic_level(rms / (self.threshold * 6))
            if not speaking:
                pre_roll.append(block)
                loud_run = loud_run + 1 if loud else 0
                if loud_run >= 3:  # ~90 ms of sound: someone started talking
                    speaking = True
                    frames = list(pre_roll)
                    quiet_run = 0
                continue

            frames.append(block)
            quiet_run = 0 if loud else quiet_run + 1
            duration = len(frames) * BLOCK
            if quiet_run * BLOCK >= self.silence_end or duration >= self.max_speech:
                if duration - quiet_run * BLOCK >= self.min_speech:
                    return np.concatenate(frames)
                pre_roll.clear()  # too short, just a noise
                frames, speaking, loud_run, quiet_run = [], False, 0, 0

    def close(self):
        self.stream.stop()
        self.stream.close()
