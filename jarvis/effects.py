"""Light processing that makes a plain voice sound like an AI in a room."""
import numpy as np


def highpass(audio, sr, cutoff):
    # one-pole high-pass, vectorised with scipy so it adds no real latency
    from scipy.signal import lfilter

    rc = 1.0 / (2 * np.pi * cutoff)
    alpha = rc / (rc + 1.0 / sr)
    return lfilter([alpha, -alpha], [1.0, -alpha], audio).astype(np.float32)


def echo(audio, sr, taps_ms, gain):
    max_delay = int(sr * max(taps_ms) / 1000)
    out = np.concatenate([audio, np.zeros(max_delay, dtype=audio.dtype)])
    for ms in taps_ms:
        d = int(sr * ms / 1000)
        out[d : d + len(audio)] += audio * gain
    return out


def apply_effects(audio, sr, cfg):
    if not cfg.get("enabled") or audio.size == 0:
        return audio
    audio = audio.astype(np.float32)
    audio = highpass(audio, sr, cfg.get("highpass_hz", 120))
    audio = echo(audio, sr, cfg.get("echo_ms", [23, 41]), cfg.get("echo_gain", 0.18))
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio / peak * 0.9
    return audio
