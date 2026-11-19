"""Wake and goodbye phrase matching, tolerant of how Whisper writes things down
("Wake up, Daddy's here!" and "wake up daddy is here" both match)."""
import re
from difflib import SequenceMatcher

DEFAULT_WAKE = [
    "wake up daddy's here", "wake up daddy is here",
    "wake up your daddy's here", "wake up your daddy is here",
    "jarvis wake up", "wake up jarvis", "hey jarvis wake up",
    "wake up jarvis your daddy's here", "wake up jarvis daddy's here",
    "jarvis wake up your daddy's here", "jarvis wake up daddy's here",
    "hey jarvis wake up your daddy's here",
    "daddy's home", "daddy is home", "daddy's here jarvis",
]
DEFAULT_GOODBYE = [
    "call it a day",
    "that's it for today",
    "okay then that's it",
    "okay rest now",
    "bye bye",
]


def normalise(text):
    t = text.lower()
    t = t.replace("’", "'")
    t = re.sub(r"\b(ok|o\.k\.)\b", "okay", t)
    t = re.sub(r"\bdad's\b|\bdaddys\b", "daddy's", t)
    t = re.sub(r"\bthats\b", "that's", t)
    t = re.sub(r"[^a-z' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _fuzzy_contains(text, phrase, threshold):
    """True if `phrase` appears in `text`, allowing small transcription slips."""
    if phrase in text:
        return True
    tw, pw = text.split(), phrase.split()
    n = len(pw)
    for i in range(max(1, len(tw) - n + 1)):
        window = " ".join(tw[i : i + n])
        if SequenceMatcher(None, window, phrase).ratio() >= threshold:
            return True
    return False


class Phrases:
    def __init__(self, cfg):
        self.wake = [normalise(p) for p in cfg.get("wake_phrases", DEFAULT_WAKE)]
        self.goodbye = [normalise(p) for p in cfg.get("goodbye_phrases", DEFAULT_GOODBYE)]

    def is_wake(self, text):
        t = normalise(text)
        return any(_fuzzy_contains(t, p, 0.8) for p in self.wake)

    def is_goodbye(self, text):
        """When the goodbye is basically the whole sentence, or how the sentence ends
        ("and you know what, let's call it a day"), so "bye bye" in the middle of a
        story doesn't send him to sleep."""
        t = normalise(text)
        words = t.split()
        for p in self.goodbye:
            n = len(p.split())
            if _fuzzy_contains(t, p, 0.85) and len(words) <= n + 3:
                return True
            ending = " ".join(words[-n:])                  # the goodbye closes a short-ish sentence
            if len(words) <= n + 8 and SequenceMatcher(None, ending, p).ratio() >= 0.85:
                return True
        return False
