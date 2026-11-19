"""Semantic router: decides what kind of request something is by its meaning,
not by keywords.

Every route below has example sentences. At startup each example is turned into
an embedding (a vector that captures its meaning) with a small model running in
Ollama (all-minilm, about 45 MB). For each thing you say, your sentence gets an
embedding too, it's compared with all the examples by cosine similarity, and the
route of the closest examples wins. The tool or brain for that route then reads
the details (which city, which title...) out of your sentence.

If nothing is clearly close, it returns None and Jarvis falls back to the old
keyword rules, so nothing gets worse.

Add your own examples in routes.yaml next to config.yaml (it's in .gitignore, so
personal examples like your town, your shows or your routines stay private):
    web:
      - what time does the leisure centre open
    chat:
      - how was your day

Try it:   .venv\\Scripts\\python -m jarvis.semantic "what time does the sun set in Paris"
"""

import hashlib
import json
import os
import threading

import numpy as np
import requests

# what each route does (main.py and router.py act on these names)
TOOL_ROUTES = {"time", "weather", "news", "open", "close_app", "close_windows", "show", "timer", "screen", "memory"}
CLAUDE_ROUTES = {"web", "maths", "deep"}          # need the internet, exact numbers or real thinking
CHAT_ROUTES = {"chat"}                            # small talk: the normal brain choice, no tools

EXAMPLES = {
    "time": [
        "what time is it", "what's the time", "tell me the time", "what time is it now", "time please",
        "what's the date today", "what day is it", "what is today's date", "what's the time in Sydney",
        "what time is it in New York", "time in Tokyo right now", "what's the date", "what day of the week is it",
    ],
    "weather": [
        "what's the weather", "is it going to rain today", "do I need an umbrella", "how cold is it outside",
        "weather in Paris tomorrow", "will it be sunny tomorrow", "how hot is it", "what's the temperature outside",
        "what will the weather be like at 6pm", "is it raining in Berlin", "weather this evening",
        "what's the forecast for tomorrow", "will it snow today", "how windy is it",
    ],
    "news": [
        "what's the news", "any headlines", "latest news", "news about Ukraine", "what's happening in politics",
        "top stories today", "any news on the election", "latest news on America and Iran", "tell me the headlines",
        "what's in the news today", "breaking news", "news about Tesla", "what's the latest in sport news",
    ],
    "open": [
        "open YouTube", "open Netflix", "launch Spotify", "open VS Code", "start Discord", "open Gmail",
        "open the calculator", "search YouTube for lo-fi music", "search Google for flights to Rome",
        "open bbc.co.uk", "bring up Word", "open WhatsApp", "launch Steam", "open file explorer", "open settings",
    ],
    "close_app": [
        "close Spotify", "quit Word", "close Chrome", "exit Discord", "close the calculator", "close VS Code",
        "quit Netflix", "kill Steam",
    ],
    "close_windows": [
        "close all tabs", "close the windows", "hide the pop ups", "close those", "get rid of the windows",
        "close the pop up windows", "close all the sources",
    ],
    "show": [
        "show me the pop ups", "open those windows again", "show me what you found", "bring up the sources",
        "show me the results", "show the news windows", "show me what you found on the internet",
        "open the tabs again", "show me the links",
    ],
    "timer": [
        "set a timer for 5 minutes", "timer for ten minutes", "remind me in 20 minutes", "cancel the timer",
        "stop all timers", "start a 30 second timer", "set an alarm for an hour", "how long is left on my timer",
    ],
    "screen": [
        "click sign in", "click on Stranger Things", "hover over The Crown", "put the mouse over Continue Watching",
        "scroll down", "scroll up a bit", "keep scrolling", "stop scrolling", "scroll to the top", "page down",
        "go back", "press enter", "type hello world", "click the right arrow", "again", "play Inception",
        "play it", "press play", "can you see Inception on the screen", "is there a sign in button",
        "the second one", "scroll right", "click the play button", "hover over my profile", "click it",
        "move the mouse to the search bar", "select the first one", "scroll down a lot", "watch Stranger Things",
        "do you see the play button", "click the left arrow", "next one",
    ],
    "memory": [
        "remember that my exam is on the 3rd", "remember my mum's birthday is in March", "make a note that I like dark mode",
        "what do you remember about me", "what do you know about me", "forget that", "forget about my exam",
        "don't forget I'm meeting Sam on Friday", "keep in mind that I don't like olives", "what's in your memory",
        "delete that from your memory", "note down that my locker code changed",
    ],
    "web": [
        "who won the football last night", "what time is sunrise tomorrow", "when does the sun set in Paris",
        "what's the price of bitcoin", "is there a tennis match on today", "when does the new iPhone come out",
        "what time does the supermarket close", "what's the score in the match", "who is the prime minister now",
        "latest update on the Apple stock price", "when is the next World Cup match", "what time is sunset today",
        "are the trains running today", "what are the opening hours of the Louvre", "what's on at the cinema tonight",
        "is the pharmacy open tonight", "look this up online", "search the internet for", "google who won the election",
        "what's the exchange rate today", "is the shop open now", "who plays in the final",
        "when is the next bank holiday", "how long is the flight from London to New York", "who won the Oscars this year",
        "what's the traffic like on the motorway", "when does the new season of the show come out",
    ],
    "maths": [
        "what's 17 times 23", "how much is 20 percent of 500", "if I invest 2,000 pounds how many shares can I buy",
        "convert 100 dollars to pounds", "what's the square root of 144", "split 87 pounds between 3 people",
        "how many days until Christmas", "calculate my profit", "what's 15 percent tip on 60 pounds",
        "how much interest on 10,000 at 5 percent",
    ],
    "deep": [
        "explain how black holes form", "should I buy an electric car", "help me write an email to my professor",
        "what's the difference between AI and machine learning", "give me advice on my CV", "why is the sky blue",
        "compare the iPhone and the Samsung", "plan a trip to Paris", "how does a neural network learn",
        "what should I cook tonight", "help me prepare for my interview", "summarise the history of the Roman empire",
    ],
    "chat": [
        "how are you", "tell me a joke", "thank you", "good morning", "you're the best", "what's your name",
        "I'm bored", "hello Jarvis", "okay cool", "nice one", "never mind", "how was your day", "that's funny",
        "you're awesome", "good night", "what can you do",
    ],
}


class SemanticRouter:
    def __init__(self, cfg):
        s = cfg.get("semantic") or {}
        self.enabled = s.get("enabled", True)
        self.model = s.get("model", "all-minilm")
        self.min_score = s.get("min_score", 0.55)    # how close the best example must be
        self.margin = s.get("margin", 0.03)          # how far ahead of the next route it must be
        base = cfg["llm"].get("ollama_url", "http://127.0.0.1:11434").rstrip("/").replace("localhost", "127.0.0.1")
        self.base = base
        self.http = requests.Session()
        self.examples = {k: list(v) for k, v in EXAMPLES.items()}
        self._merge_user_examples(s.get("routes_file", "routes.yaml"))
        self.cache_path = os.path.join(cfg["llm"].get("claude_code_workdir", ".jarvis_session"), "route_vectors.json")
        self.labels, self.vecs = [], None
        self.ready = False
        if self.enabled:
            threading.Thread(target=self._build, daemon=True).start()

    def _merge_user_examples(self, path):
        if not os.path.exists(path):
            return
        try:
            import yaml
            extra = yaml.safe_load(open(path, encoding="utf-8")) or {}
            for route, lines in extra.items():
                self.examples.setdefault(route, []).extend(str(x) for x in (lines or []))
        except Exception as e:
            print(f"  (couldn't read {path}: {e})")

    # ---------- embeddings ----------
    def _embed(self, texts):
        """Vectors for a list of sentences, normalised so a dot product is the cosine similarity."""
        try:
            r = self.http.post(self.base + "/api/embed", json={"model": self.model, "input": texts, "keep_alive": "60m"}, timeout=60)
            if r.status_code == 404 and "model" not in r.text.lower():
                raise requests.HTTPError("old Ollama")
            r.raise_for_status()
            vecs = r.json()["embeddings"]
        except requests.HTTPError:
            vecs = []                                 # older Ollama: one sentence at a time
            for t in texts:
                r = self.http.post(self.base + "/api/embeddings", json={"model": self.model, "prompt": t}, timeout=30)
                r.raise_for_status()
                vecs.append(r.json()["embedding"])
        a = np.asarray(vecs, dtype=np.float32)
        return a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)

    def _build(self):
        labels, texts = [], []
        for route, lines in self.examples.items():
            for line in lines:
                labels.append(route)
                texts.append(line)
        key = hashlib.sha1((self.model + "\n" + "\n".join(f"{l}|{t}" for l, t in zip(labels, texts))).encode()).hexdigest()
        try:                                          # reuse last time's vectors if the examples haven't changed
            cached = json.load(open(self.cache_path, encoding="utf-8"))
            if cached.get("key") == key:
                self.labels, self.vecs, self.ready = labels, np.asarray(cached["vecs"], dtype=np.float32), True
                return
        except Exception:
            pass
        try:
            vecs = self._embed(texts)
        except Exception as e:
            print(f"  (semantic routing off: {type(e).__name__}. Run: ollama pull {self.model})")
            return
        self.labels, self.vecs, self.ready = labels, vecs, True
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            json.dump({"key": key, "vecs": vecs.tolist()}, open(self.cache_path, "w", encoding="utf-8"))
        except Exception:
            pass

    # ---------- deciding ----------
    def scores(self, text):
        """Best similarity per route for this sentence, highest first."""
        if not self.ready:
            return []
        q = self._embed([text])[0]
        sims = self.vecs @ q
        best = {}
        for label, s in zip(self.labels, sims):
            if s > best.get(label, -1):
                best[label] = float(s)
        return sorted(best.items(), key=lambda p: -p[1])

    def classify(self, text):
        """(route, score) when one route is clearly closest, else None (use the keyword rules)."""
        if not self.enabled or not self.ready or not text.strip():
            return None
        try:
            ranked = self.scores(text)
        except Exception:
            return None
        if not ranked:
            return None
        (route, top), second = ranked[0], (ranked[1][1] if len(ranked) > 1 else 0.0)
        if top < self.min_score or top - second < self.margin:
            return None
        return route, top


if __name__ == "__main__":
    import sys
    import time

    import yaml

    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    r = SemanticRouter(cfg)
    for _ in range(300):
        if r.ready:
            break
        time.sleep(0.1)
    if not r.ready:
        print("Not ready. Is Ollama running, and have you run: ollama pull all-minilm ?")
        sys.exit(1)
    for sentence in sys.argv[1:] or ["what time does the sun set in Paris, what's the time?"]:
        print(f"\n{sentence}")
        for route, s in r.scores(sentence)[:4]:
            print(f"   {route:14} {s:.3f}")
        print("   ->", r.classify(sentence) or "no clear winner: keyword rules decide")
