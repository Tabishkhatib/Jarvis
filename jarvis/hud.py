"""The animated HUD: a local web page that reacts to Jarvis in real time.

Python runs a tiny web server (standard library only) and pushes events to the
page with Server-Sent Events: the current state (sleeping, listening, thinking,
speaking), voice and mic loudness, and the latest lines of conversation.
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = os.path.join(os.path.dirname(__file__), "hud.html")


class NullHUD:
    """Used when the HUD is turned off, so the rest of the code needn't check."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


class HUD:
    def __init__(self, cfg, name="Jarvis"):
        hcfg = cfg.get("hud") or {}
        self.name = name
        self.clients = []
        self.lock = threading.Lock()
        self.last = {"state": {"type": "state", "state": "booting"}}
        self.web_procs = []      # browser processes behind the small web windows
        self.web_wins = []       # open windows: {"url", "rect", "hwnd"}
        self.web_append = False  # set by main.py: True when the current question follows on from the last
        self.web_lock = threading.Lock()
        self.last_sources = None # the last pages shown, so "show me the pop-ups" can bring them back
        if os.name == "nt":
            _dpi_aware()
            threading.Thread(target=self._web_hover, daemon=True).start()
        self.web_profile = os.path.abspath(hcfg.get("web_profile", ".jarvis_session/web"))
        self.web_alpha = hcfg.get("web_opacity", 0.8)   # 1 = solid, lower = more see-through
        hud = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the terminal clean
                pass

            def do_GET(self):
                if self.path.startswith("/events"):
                    return hud._serve_events(self)
                if self.path.startswith("/open?"):
                    return hud._open_link(self)
                if self.path.startswith("/web?"):
                    return hud._web_open(self)
                if self.path.startswith("/web_close"):
                    return hud._web_close(self)
                with open(PAGE, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        port = hcfg.get("port", 8765)
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port taken, pick any
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        print(f"HUD running at {self.url}")
        if hcfg.get("open_window", True):
            self._open_window(hcfg.get("app_window", True))

    # ---------- window ----------
    def _open_window(self, app_window):
        """Open as a clean app window (no tabs or address bar) if Edge or Chrome
        is available, otherwise as a normal browser tab."""
        if app_window:
            for exe in ("msedge", "chrome", "google-chrome", "chromium"):
                path = shutil.which(exe) or _windows_browser(exe)
                if path:
                    try:
                        subprocess.Popen([path, f"--app={self.url}", "--window-size=1100,800"])
                        return
                    except OSError:
                        pass
        webbrowser.open(self.url)

    # ---------- source links clicked in the HUD ----------
    @staticmethod
    def _open_link(req):
        """Open a source link in your normal browser (not inside the HUD window)."""
        from urllib.parse import parse_qs, urlparse

        url = parse_qs(urlparse(req.path).query).get("u", [""])[0]
        if url.startswith(("http://", "https://")):
            if os.name == "nt":
                os.startfile(url)
            else:
                webbrowser.open(url)
        req.send_response(204)
        req.end_headers()

    # ---------- small live web window ----------
    def _web_open(self, req):
        """The HUD sends the full set of windows it wants (up to 3: url + where).
        Windows already open for the same page are moved into place, pages no
        longer wanted are closed, and new ones are opened as small real browser
        windows. They're normal windows, so you can scroll them, click links, or
        maximise one. They use their own browser profile so Jarvis can close them
        without touching your other browser windows."""
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(req.path).query)
        try:
            dpr = max(0.5, min(4.0, float(q.get("s", ["1"])[0])))   # page pixels -> real screen pixels
        except ValueError:
            dpr = 1.0
        want = []
        for i, url in enumerate(q.get("u", [])[:3]):
            if not url.startswith(("http://", "https://")):
                continue
            try:
                rect = tuple(int(float(v)) for v in q.get("r", [])[i].split(","))
            except (IndexError, ValueError):
                rect = (0, 0, 440, 300)
            want.append((url, rect))
        ok = True
        with self.web_lock:
            wanted = {u for u, _ in want}
            for wnd in list(self.web_wins):                       # close what's no longer wanted
                if wnd["url"] not in wanted:
                    _close_window(wnd.get("hwnd"))
                    self.web_wins.remove(wnd)
            if not self.web_wins:
                self._web_kill()                                  # start clean if nothing is left
            have = {w["url"]: w for w in self.web_wins}
            path = next((p for p in (shutil.which(e) or _windows_browser(e)
                                     for e in ("msedge", "chrome", "google-chrome", "chromium")) if p), None)
            for url, (x, y, w, h) in want:
                phys = tuple(round(v * dpr) for v in (x, y, w, h))
                if url in have:                                   # already open: slide it to its new spot
                    have[url].update(rect=(x, y, w, h), phys=phys, dpr=dpr)
                    _move_window(have[url].get("hwnd"), *phys)
                    continue
                if not path:
                    ok = False
                    continue
                try:
                    os.makedirs(self.web_profile, exist_ok=True)
                    self.web_procs.append(subprocess.Popen([
                        path, f"--app={url}", f"--user-data-dir={self.web_profile}",
                        f"--window-position={x},{y}", f"--window-size={max(w, 300)},{max(h, 160)}",
                        "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
                        "--force-dark-mode",          # dark page, so it blends with the HUD
                    ]))
                    wnd = {"url": url, "rect": (x, y, w, h), "phys": phys, "dpr": dpr, "hwnd": None}
                    self.web_wins.append(wnd)
                    if os.name == "nt":               # find it, make it see-through, remember it
                        pids = [p.pid for p in self.web_procs]
                        taken = [o["hwnd"] for o in self.web_wins if o.get("hwnd")]
                        def settle(wnd=wnd, px=phys[0], py=phys[1], pids=pids, taken=taken):
                            hwnd = _make_translucent(px, py, self.web_alpha, pids, taken)
                            wnd["hwnd"] = hwnd
                            _move_window(hwnd, *wnd["phys"])          # make sure it sits exactly in its slot
                        threading.Thread(target=settle, daemon=True).start()
                    import time
                    time.sleep(0.35)                  # let each window land before placing the next
                except OSError:
                    ok = False
        req.send_response(204 if ok else 500)
        req.end_headers()

    def _web_hover(self):
        """Make the real web windows hover gently up and down, on the same beat as
        the HUD (both use the wall clock). A window you drag or maximise is left
        alone from then on."""
        import ctypes
        import math
        import time
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        SWP = 0x0001 | 0x0004 | 0x0010            # keep size, keep stacking order, don't steal focus
        while True:
            time.sleep(1 / 30)
            if not self.web_lock.acquire(timeout=0.05):
                continue                            # windows are being rearranged right now
            try:
                self._hover_step(user32, wintypes, ctypes, math, time, SWP)
            finally:
                self.web_lock.release()

    def _hover_step(self, user32, wintypes, ctypes, math, time, SWP):
        """One hover tick for each window (called with web_lock held)."""
        for i, w in enumerate(self.web_wins):
            hwnd = w.get("hwnd")
            if not hwnd or w.get("free"):
                continue
            try:
                x, y = w["phys"][0], w["phys"][1]
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                tol = 25 * w.get("dpr", 1)
                if user32.IsZoomed(hwnd) or abs(r.left - x) > tol or abs(r.top - y) > tol:
                    w["free"] = True            # you moved or maximised it: stop hovering it
                    continue
                dy = round(math.sin(time.time() * 1.3 + i * 1.9) * 5 * w.get("dpr", 1))
                user32.SetWindowPos(hwnd, 0, x, y + dy, 0, 0, SWP)
            except Exception:
                pass

    def web_is_open(self):
        return bool(self.web_wins)

    def web_close_all(self):
        """Close the web windows (a new topic started). Tells the HUD too."""
        with self.web_lock:
            self.web_wins = []
            self._web_kill()
        self._emit({"type": "web_close"})

    def _web_close(self, req):
        with self.web_lock:
            self.web_wins = []
            self._web_kill()
        req.send_response(204)
        req.end_headers()

    def _web_kill(self):
        procs, self.web_procs = self.web_procs, []
        for p in procs:
            if p.poll() is None:
                try:
                    if os.name == "nt":   # the browser starts helper processes, so close the whole tree
                        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
                    else:
                        p.terminate()
                except Exception:
                    pass

    def close(self):
        self._web_kill()

    # ---------- events ----------
    def _serve_events(self, req):
        req.send_response(200)
        req.send_header("Content-Type", "text/event-stream")
        req.send_header("Cache-Control", "no-cache")
        req.end_headers()
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.clients.append(q)
        try:
            # catch the page up on the current state
            self._write(req, {"type": "hello", "name": self.name})
            for msg in list(self.last.values()):
                self._write(req, msg)
            while True:
                try:
                    msg = q.get(timeout=15)
                    req.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    req.wfile.write(b": ping\n\n")  # keep the connection open
                req.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.lock:
                if q in self.clients:
                    self.clients.remove(q)

    @staticmethod
    def _write(req, msg):
        req.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
        req.wfile.flush()

    def _emit(self, msg, remember=None):
        if remember:
            self.last[remember] = msg
        data = json.dumps(msg)
        with self.lock:
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass  # a stalled page shouldn't slow Jarvis down

    # ---------- what the rest of Jarvis calls ----------
    def state(self, s):
        """sleeping | listening | thinking | offline"""
        self._emit({"type": "state", "state": s}, remember="state")

    def speaking(self, on):
        self._emit({"type": "speaking", "on": bool(on)}, remember="speaking")

    def voice_level(self, v):
        self._emit({"type": "voice", "v": round(min(1.0, v), 3)})

    def mic_level(self, v):
        self._emit({"type": "mic", "v": round(min(1.0, v), 3)})

    def user(self, text):
        self._emit({"type": "user", "text": text})

    def reply(self, text):
        self._emit({"type": "reply", "text": text})

    def sources(self, query, links, show=None):
        """Where the last answer came from: the search query, up to 5 links, and
        optionally up to 3 pages to open side by side (when sources disagree)."""
        self.last_sources = (query, links, show or [])
        self._emit({"type": "sources", "query": query, "links": links, "show": show or [], "append": self.web_append})

    def web_reshow(self):
        """Bring back the last web windows (\"show me the pop-ups\"). False if there's nothing yet."""
        if not self.last_sources:
            return False
        query, links, show = self.last_sources
        self._emit({"type": "sources", "query": query, "links": links, "show": show, "append": False, "force": True})
        return True

    def stats(self, brain, latency):
        self._emit({"type": "stats", "brain": brain, "latency": latency}, remember="stats")


def _make_translucent(x, y, alpha, pids=(), taken=(), wait=8.0):
    """Find the small web window we just opened and make it partly see-through,
    so the HUD's glow shows through it. Windows only.

    It's found by its browser process (the windows belong to the browser Jarvis
    started), or failing that by being close to where we asked it to go. x, y are
    real screen pixels (the process is DPI-aware, see _dpi_aware)."""
    import ctypes
    import time
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    GWL_EXSTYLE, WS_EX_LAYERED, LWA_ALPHA = -20, 0x80000, 0x2
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    pids, taken = set(pids), set(taken)
    end = time.time() + wait
    best = None
    while time.time() < end and best is None:
        cands = []

        def check(hwnd, _):
            if hwnd in taken or not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            dist = abs(r.left - x) + abs(r.top - y)
            if pid.value in pids or dist < 140:
                cands.append((0 if pid.value in pids else 1, dist, hwnd))
            return True

        user32.EnumWindows(EnumProc(check), 0)
        if cands:
            best = min(cands)[2]
        else:
            time.sleep(0.15)
    if best:
        style = user32.GetWindowLongW(best, GWL_EXSTYLE)
        user32.SetWindowLongW(best, GWL_EXSTYLE, style | WS_EX_LAYERED)
        user32.SetLayeredWindowAttributes(best, 0, int(255 * max(0.3, min(1.0, alpha))), LWA_ALPHA)
    return best


def _dpi_aware():
    """Ask Windows for real pixel coordinates, so finding, moving and hovering the
    web windows lines up exactly on scaled (125%, 150%...) displays."""
    try:
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
        except Exception:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _move_window(hwnd, x, y, w, h):
    """Slide a web window to a new spot (Windows only)."""
    if hwnd and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MoveWindow(hwnd, x, y, max(w, 300), max(h, 160), True)
        except Exception:
            pass


def _close_window(hwnd):
    """Close one web window, leaving the others open (Windows only)."""
    if hwnd and os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
        except Exception:
            pass


def _windows_browser(exe):
    if os.name != "nt":
        return None
    candidates = {
        "msedge": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                   r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
        "chrome": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                   r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                   r"%LocalAppData%\Google\Chrome\Application\chrome.exe"],
    }.get(exe, [])
    for c in candidates:
        p = os.path.expandvars(c)
        if os.path.exists(p):
            return p
    return None
