"""Voice control of whatever app is in front: click things by name, scroll, go
back and forward, press keys and type. Windows only. Everything stays on the
laptop: no screenshots are sent anywhere.

How a click finds its target:
  1. Windows' accessibility list (UI Automation): every button, link, tab and
     menu item an app reports, with its name and where it is. Instant and exact.
  2. If nothing there matches, Windows' own text recognition reads the words on
     screen (optional, needs the "winocr" package).
If several things match about equally well, Jarvis asks which one instead of
guessing. Anything that looks like buying, paying, sending or deleting needs a
yes first.
"""

import difflib
import os
import re
import threading
import time

# names that mean money or something you can't take back: always confirm first
RISKY = re.compile(
    r"\b(buy|purchase|pay|payment|checkout|check out|place order|order now|confirm order|subscribe|"
    r"send|post|publish|delete|remove|uninstall|erase|transfer|donate|book now|confirm and pay)\b",
    re.I,
)
CLICKABLE = {
    "ButtonControl", "HyperlinkControl", "ListItemControl", "MenuItemControl", "TabItemControl",
    "TreeItemControl", "CheckBoxControl", "RadioButtonControl", "ImageControl", "TextControl",
    "EditControl", "ComboBoxControl", "DataItemControl", "SplitButtonControl", "GroupControl",
    "CustomControl", "MenuBarControl", "HeaderItemControl", "DocumentControl",
}
# what row arrows are called inside apps (Netflix: "See more titles"), for "click the right arrow"
ARROW_NAMES = {
    "right": ["see more titles", "see more", "more titles", "next", "next page", "scroll right", "right arrow",
              "forward", "show more", "next slide", "next item"],
    "left": ["see previous titles", "previous titles", "previous", "previous page", "scroll left", "left arrow",
             "back", "prev", "previous slide", "previous item"],
}
# the browser's own buttons and tabs: never picked unless asked for by their exact name
BROWSER_BITS = {
    "view site information", "close tab", "close", "minimise", "minimize", "maximise", "maximize", "restore",
    "new tab", "address and search bar", "reload", "refresh", "extensions", "settings and more", "downloads",
    "favorites", "favourites", "bookmark this tab", "split screen", "copilot", "tab actions menu", "sidebar",
    "browser essentials", "collections", "search tabs", "profile", "read aloud", "translate this page",
}
ORDINALS = {
    "first": 0, "1st": 0, "one": 0, "1": 0, "second": 1, "2nd": 1, "two": 1, "2": 1,
    "third": 2, "3rd": 2, "three": 2, "3": 2, "fourth": 3, "4th": 3, "four": 3, "4": 3,
    "fifth": 4, "5th": 4, "five": 4, "5": 4,
}


def _norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _foreground():
    """The window you're working in (skipping Jarvis's own HUD), as (hwnd, rect)."""
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()

    def title(h):
        n = u.GetWindowTextLengthW(h)
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        return b.value

    if title(hwnd) == "HUD" or not hwnd:          # the HUD has focus: use the window just below it
        h = u.GetWindow(hwnd, 2)                  # GW_HWNDNEXT, down the stack
        while h:
            if u.IsWindowVisible(h) and title(h) and title(h) != "HUD" and not u.IsIconic(h):
                hwnd = h
                break
            h = u.GetWindow(h, 2)
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return hwnd, (r.left, r.top, r.right, r.bottom)


def _where(cx, cy, rect):
    """'top left', 'middle', 'bottom right'... so similar items can be told apart."""
    l, t, r, b = rect
    fx = (cx - l) / max(1, r - l)
    fy = (cy - t) / max(1, b - t)
    v = "top" if fy < 0.33 else "bottom" if fy > 0.66 else ""
    h = "left" if fx < 0.33 else "right" if fx > 0.66 else ""
    return (f"{v} {h}".strip() or "middle")


class Screen:
    def __init__(self, cfg=None):
        self.scrolling = False
        self.scroll_thread = None
        self.last_hover = None      # where the pointer was last parked by "hover over ..."
        self.last_arrow = None      # the arrow last clicked, for "again"

    # ---------- finding things ----------
    def _ui_items(self, hwnd, rect, budget=1.6):
        """Named, visible things in the window from the accessibility list."""
        import uiautomation as auto

        items = []
        root = auto.ControlFromHandle(hwnd)
        if not root:
            return items
        end = time.time() + budget                 # big web pages have huge lists: stop in time
        l, t, r, b = rect
        for ctrl, _depth in auto.WalkControl(root, maxDepth=40):
            if time.time() > end or len(items) > 3000:
                break
            try:
                name = ctrl.Name
                if not name or ctrl.ControlTypeName not in CLICKABLE or ctrl.IsOffscreen:
                    continue
                br = ctrl.BoundingRectangle
                w, h = br.right - br.left, br.bottom - br.top
                if w <= 2 or h <= 2:
                    continue
                cx, cy = (br.left + br.right) // 2, (br.top + br.bottom) // 2
                if not (l <= cx <= r and t <= cy <= b):
                    continue
                items.append({"name": name.strip(), "x": cx, "y": cy, "area": w * h, "kind": ctrl.ControlTypeName,
                              "box": (br.left, br.top, br.right, br.bottom)})
            except Exception:
                continue
        return items

    def _ocr_items(self, rect):
        """Words read off the screen with Windows' text recognition (if installed)."""
        try:
            import winocr
            from PIL import ImageGrab
        except ImportError:
            return []
        l, t, r, b = rect
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
        try:
            res = winocr.recognize_pil_sync(img, "en")
        except Exception:
            return []
        lines = res.get("lines", []) if isinstance(res, dict) else getattr(res, "lines", [])
        items = []
        for line in lines:
            words = line.get("words", []) if isinstance(line, dict) else getattr(line, "words", [])
            boxes = []
            for w in words:
                br = w.get("bounding_rect") if isinstance(w, dict) else getattr(w, "bounding_rect", None)
                if br is None:
                    continue
                g = (lambda k: br.get(k) if isinstance(br, dict) else getattr(br, k))
                boxes.append((g("x"), g("y"), g("width"), g("height")))
            text = line.get("text") if isinstance(line, dict) else getattr(line, "text", "")
            if not boxes or not text:
                continue
            x0 = min(bx for bx, _, _, _ in boxes); y0 = min(by for _, by, _, _ in boxes)
            x1 = max(bx + bw for bx, _, bw, _ in boxes); y1 = max(by + bh for _, by, _, bh in boxes)
            items.append({"name": text, "x": int(l + (x0 + x1) / 2), "y": int(t + (y0 + y1) / 2),
                          "area": (x1 - x0) * (y1 - y0), "kind": "text"})
        return items

    @staticmethod
    def _score(want, name):
        n = _norm(name)
        if not n:
            return 0.0
        if n == want:
            return 1.0
        if n.startswith(want + " ") or n.endswith(" " + want):
            return 0.9
        wt = want.split()
        if all(w in n.split() for w in wt):
            return 0.85 - min(0.25, 0.01 * (len(n.split()) - len(wt)))
        if len(want) >= 3 and re.search(r"\b" + re.escape(want) + r"\b", n):   # whole words only ("it" won't match "site")
            return 0.75
        fuzzy = difflib.SequenceMatcher(None, want, n).ratio()
        # misheard names ("Inter Stellar" for "Interstellar"): a shared longer word plus a close sound-alike
        shared = [w for w in wt if len(w) >= 4 and w in n.split()]
        if shared and fuzzy >= 0.6:
            return 0.74
        return fuzzy * 0.85

    def find(self, target):
        """Things on screen that match what you said, best first, with near-duplicates
        (a link and the text inside it, at the same spot) merged."""
        hwnd, rect = _foreground()
        want = _norm(target)
        want = re.sub(r"\s+(?:in|on|from)\s+(?:netflix|youtube|prime(?: video)?|disney(?: plus)?|spotify|the (?:screen|page|browser|site|app))$", "", want)
        hint = ""
        m = re.search(r"\s+(?:in|at|on|near)\s+the\s+((?:top|bottom|middle)?\s*(?:left|right)?)(?:\s+(?:corner|side|of the screen))?$", want)
        if m and m.group(1).strip():
            hint, want = m.group(1).strip(), want[: m.start()]      # "play button in the left corner"
        want = re.sub(r"\s+(?:button|link|icon|tab|option|thumbnail|poster|tile)$", "", want)
        scored = []
        def ui():
            items = self._ui_items(hwnd, rect)
            if len(items) < 15:                       # browsers only fill in the page's list once asked: look again
                time.sleep(0.4)
                items = self._ui_items(hwnd, rect)
            return items

        for source in (ui, lambda: self._ocr_items(rect)):
            items = source()
            scored = [(self._score(want, it["name"]), it) for it in items
                      if _norm(it["name"]) not in BROWSER_BITS or _norm(it["name"]) == want]
            scored = [(s, it) for s, it in scored if s >= 0.72]
            if scored:
                break                                   # found some with the accessibility list: no need to read the screen
        scored.sort(key=lambda p: (-p[0], -p[1]["area"]))
        out = []
        for s, it in scored:
            dup = False
            for _, o in out:
                if abs(it["x"] - o["x"]) < 25 and abs(it["y"] - o["y"]) < 25:
                    dup = True                          # same spot: a link and the text inside it
                elif _norm(it["name"]) == _norm(o["name"]):
                    l, t, r, b = o.get("box", (o["x"], o["y"], o["x"], o["y"]))
                    inside = l - 30 <= it["x"] <= r + 30 and t - 30 <= it["y"] <= b + 30
                    stacked = abs(it["x"] - o["x"]) < 60 and abs(it["y"] - o["y"]) < 200
                    dup = inside or stacked             # same name, same tile: a profile picture and its label
                if dup:
                    break
            if not dup:
                out.append((s, it))
        for _, it in out:
            it["where"] = _where(it["x"], it["y"], rect)
        if hint:                                        # prefer the ones in the corner you named
            out.sort(key=lambda p: (not all(w in p[1]["where"] for w in hint.split()), -p[0]))
            out = [p for p in out if all(w in p[1]["where"] for w in hint.split())] or out
        return out

    # ---------- doing things ----------
    @staticmethod
    def click(item):
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.click(item["x"], item["y"])

    def hover(self, item):
        """Park the pointer on something without clicking (shows hover menus and row arrows)."""
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(item["x"], item["y"], duration=0.15)
        self.last_hover = (item["x"], item["y"])

    def find_arrow(self, direction):
        """Arrows of that direction on screen, nearest the hovered row first."""
        hwnd, rect = _foreground()
        names = ARROW_NAMES[direction]
        found = []
        for it in self._ui_items(hwnd, rect):
            n = _norm(it["name"])
            if any(n == a or n.startswith(a + " ") for a in names) or n in (">", "<", "›", "‹", "❯", "❮"):
                if n in (">", "›", "❯") and direction != "right" or n in ("<", "‹", "❮") and direction != "left":
                    continue
                found.append(it)
        if self.last_hover:                         # the row you just hovered over: nearest arrow (vertically) wins
            hx, hy = self.last_hover
            found.sort(key=lambda it: (abs(it["y"] - hy), abs(it["x"] - hx)))
        for it in found:
            it["where"] = _where(it["x"], it["y"], rect)
        return found

    def click_arrow(self, item, direction):
        self.click(item)
        self.last_arrow = (direction, item["x"], item["y"])

    def again(self):
        """Click the last arrow again (it stays in the same place on the row)."""
        if not self.last_arrow:
            return False
        import pyautogui
        _, x, y = self.last_arrow
        pyautogui.click(x, y)
        return True

    def hscroll(self, direction, amount="normal"):
        """Sideways scrolling, over whatever is under the pointer."""
        import ctypes
        self.stop_scrolling()
        if not self.last_hover:
            self._over_foreground()
        clicks = {"bit": 4, "normal": 12, "lot": 30}[amount]
        delta = 120 * clicks * (1 if direction == "right" else -1)
        ctypes.windll.user32.mouse_event(0x01000, 0, 0, delta, 0)       # MOUSEEVENTF_HWHEEL

    @staticmethod
    def risky(item, target=""):
        return bool(RISKY.search(item["name"]) or RISKY.search(target))

    @staticmethod
    def _over_foreground():
        """Wheel scrolling goes to the window under the pointer: make sure that's the app you're in."""
        import pyautogui
        _, (l, t, r, b) = _foreground()
        x, y = pyautogui.position()
        if not (l <= x <= r and t <= y <= b):
            pyautogui.moveTo((l + r) // 2, (t + b) // 2)

    def scroll(self, direction, amount="normal"):
        import pyautogui
        self.stop_scrolling()
        self._over_foreground()
        clicks = {"bit": 100, "normal": 150, "lot": 200}[amount]   # scroll units (on Windows 120 = one wheel notch)
        pyautogui.scroll(clicks if direction == "up" else -clicks)

    def keep_scrolling(self, direction="down"):
        import pyautogui
        self.stop_scrolling()
        self._over_foreground()
        self.scrolling = True

        def run():
            while self.scrolling:
                pyautogui.scroll(200 if direction == "up" else -200)
                time.sleep(0.1)
        self.scroll_thread = threading.Thread(target=run, daemon=True)
        self.scroll_thread.start()

    def stop_scrolling(self):
        was = self.scrolling
        self.scrolling = False
        return was

    @staticmethod
    def key(*keys):
        import pyautogui
        if len(keys) > 1:
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(keys[0])

    @staticmethod
    def type_text(text):
        import pyautogui
        if text.isascii():
            pyautogui.write(text, interval=0.01)
        else:                                            # accents and emoji: paste instead
            import subprocess
            subprocess.run(["clip"], input=text.encode("utf-16le"), check=False)
            pyautogui.hotkey("ctrl", "v")


def pick_from(answer, options):
    """Which of the offered options the reply means: 'the second one', 'season 4',
    'the one at the top', 'last'. Returns an index, or None."""
    a = _norm(answer)
    words = a.split()
    if "last" in words or "bottom one" in a:
        return len(options) - 1
    for w in words:
        if w in ORDINALS and ORDINALS[w] < len(options):
            return ORDINALS[w]
    for i, (_, it) in enumerate(options):              # "the one at the top left"
        if it.get("where") and it["where"] != "middle" and it["where"] in a:
            if sum(1 for _, o in options if o.get("where") == it["where"]) == 1:
                return i
    names = {_norm(it["name"]) for _, it in options}
    if len(names) == 1 and next(iter(names)) in a:      # all called the same and they just repeated the name
        return 0
    best, best_s = None, 0.0                            # "season 4": match against their names
    for i, (_, it) in enumerate(options):
        n = _norm(it["name"])
        s = 1.0 if a and a in n else difflib.SequenceMatcher(None, a, n).ratio()
        if s > best_s:
            best, best_s = i, s
    return best if best_s >= 0.6 else None
