"""`system-diff tui`: the pane's views (Now, Review, Agents, Topics) in a terminal of their own, for a separate tab or
window beside Claude Code. It draws the very page the pane draws (`pane.show`, the protocol-2 page: rows of styled
runs, blocks to pick, what each lights and shows), with the pane's keys and colours, so the two never differ.

Standard library only: ANSI escape sequences on the alternate screen (Windows 10+ consoles with virtual terminal
processing turned on), keys from termios/select on POSIX and msvcrt on Windows. The page is built in a thread, so the
keys answer while a large repository is read; the terminal is restored on every way out, Ctrl+C included.

The drawing (`render`) and the pick's movement (`move`) are pure functions, and the loop (`loop`) takes its keys from
any reader, so all of it is tested without a terminal.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__

VIEWS = (("now", "Now", "n"), ("review", "Review", "r"), ("agents", "Agents", "a"), ("topics", "Topics", "t"))
VIEW_KEYS = {"n": "now", "r": "review", "m": "review", "c": "review", "a": "agents", "t": "topics"}
ZOOM_WORD = ("full", "compact", "tiny")
REFRESH = 3.0   # seconds between looks at the system, as the pane's clock
STEP = 8        # columns a [ or ] scrolls across

# One meaning per colour, as the pane's: blue where Claude is and what you picked, yellow what changed and needs you,
# green what is new, red what went, magenta the network, gray the background.
COLORS = {
    "accent": 34, "focus": 34, "near": 34, "data": 34,
    "attention": 33, "changed": 33,
    "good": 32, "added": 32,
    "removed": 31, "network": 35,
    "agent0": 36, "agent1": 35, "agent2": 32, "agent3": 33, "agent4": 34,
}
BOLD = ("bold", "focus", "heading", "attention")
BLUE = 34
RESET = "\x1b[0m"

Run = List[Any]   # [text, style words, ids?]
Row = List[Run]


# --- what a cell is -------------------------------------------------------------------------------------------------

def cells(text: str) -> int:
    """Columns a text takes in a terminal: wide characters (🌐) two, combining marks none."""
    n = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def fit(text: str, width: int) -> str:
    """Cut to `width` columns, an ellipsis where it was cut."""
    if cells(text) <= width:
        return text
    out, n = "", 0
    for ch in text:
        w = cells(ch)
        if n + w > width - 1:
            break
        out, n = out + ch, n + w
    return out + ("…" if width > 0 else "")


def sgr(*codes: int) -> str:
    return f"\x1b[{';'.join(str(c) for c in codes)}m" if codes else ""


# --- picking and scrolling, as page.tsx -----------------------------------------------------------------------------

def _centre(b: Dict[str, Any]) -> Tuple[float, float]:
    return b["x"] + b["w"] / 2, b["y"] + b["h"] / 2


def move(blocks: List[Dict[str, Any]], current: Optional[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    """The nearest block that way, along the key's axis first: the next box down a column, the next column across."""
    if current is None:
        return blocks[0] if blocks else None
    cx, cy = _centre(current)
    best, score = None, float("inf")
    for b in blocks:
        if b["id"] == current["id"]:
            continue
        x, y = _centre(b)
        dx, dy = x - cx, y - cy
        ahead = {"down": dy, "up": -dy, "right": dx}.get(key, -dx)
        aside = abs(dx) if key in ("down", "up") else abs(dy)
        if ahead < 0.5:
            continue
        s = ahead + aside * 0.6 if key in ("down", "up") else ahead + aside * 4
        if s < score:
            best, score = b, s
    return best or current


def across(page: Dict[str, Any], cols: int, sx: int, b: Optional[Dict[str, Any]]) -> int:
    """The visible window across a wide page: as it was, moved just enough to show the picked block whole."""
    most = max(0, int(page.get("wide") or 0) - cols)
    x = min(max(0, sx), most)
    if b is not None and cols > 0:
        if b["x"] < x:
            x = max(0, b["x"] - 2)
        elif b["x"] + b["w"] > x + cols:
            x = min(most, b["x"] + b["w"] - cols + 2)
    return x


def cut(row: Row, sx: int, cols: int) -> Row:
    """One row cut to the window [sx, sx + cols): runs are split where the window's edges fall."""
    out: Row = []
    at = 0
    for run in row:
        text = run[0]
        lo, hi = max(sx, at), min(sx + cols, at + len(text))
        if hi > lo:
            out.append([text[lo - at:hi - at], run[1], run[2] if len(run) > 2 else ""])
        at += len(text)
        if at >= sx + cols:
            break
    return out


# --- drawing --------------------------------------------------------------------------------------------------------

@dataclass
class State:
    """What the person has done to the page: the view and part asked for, the pick, the scroll, the way back."""
    view: str = "now"
    part: str = ""
    zoom: int = -1
    sel: str = ""
    sx: int = 0
    sy: int = 0
    free: bool = False          # scrolled across by hand: the pick no longer pulls the window
    mine: bool = False          # the person picked: a fresh page keeps their pick, not the page's
    back: List[str] = field(default_factory=list)
    note: str = ""


def look(run: Run, sel: str, lit: set) -> str:
    """The SGR codes for one run, as the pane colours it: picked blue (inverse on a box's title), lit bold, muted dim."""
    words = str(run[1] or "").split()
    ids = str(run[2] if len(run) > 2 and run[2] else "").split()
    picked = sel != "" and sel in ids
    is_lit = not picked and any(i in lit for i in ids)
    color = BLUE if picked else next((COLORS[w] for w in words if w in COLORS), None)
    codes: List[int] = []
    if picked or is_lit or any(w in words for w in BOLD):
        codes.append(1)
    muted = "muted" in words or ("heading" in words and color is None)
    if muted and not picked and not is_lit:
        codes.append(2)
    if picked and "title" in words:
        codes.append(7)
    if color is not None:
        codes.append(color)
    return sgr(*codes)


def paint(row: Row, sel: str, lit: set) -> str:
    out = []
    for run in row:
        if run[0]:
            out.append(look(run, sel, lit) + run[0] + RESET)
    return "".join(out)


def _picked(page: Dict[str, Any], st: State) -> Optional[Dict[str, Any]]:
    by = {b["id"]: b for b in page.get("blocks") or []}
    return by.get(st.sel) or by.get(page.get("pick") or "") or next(iter(by.values()), None)


def body_rows(page: Dict[str, Any], sel: str) -> List[Row]:
    """The page's rows, the picked block's detail in the slot under its picture."""
    rows = list(page.get("rows") or [])
    b = next((b for b in page.get("blocks") or [] if b["id"] == sel), None)
    if b and b.get("slot"):
        at, n = b["slot"]
        detail = (page.get("details") or {}).get(sel) or []
        for i in range(n):
            if 0 <= at + i < len(rows):
                rows[at + i] = detail[i] if i < len(detail) else []
    return rows


def layout(page: Optional[Dict[str, Any]], st: State, width: int, height: int) -> Tuple[int, bool]:
    """How many rows of the page show, and whether the page is wider than the terminal."""
    wide = bool(page) and int(page.get("wide") or 0) > width
    return max(1, height - 2 - (1 if st.note else 0) - (1 if wide else 0)), wide


def render(page: Optional[Dict[str, Any]], st: State, width: int, height: int,
           version: str = __version__) -> List[str]:
    """The whole screen, `height` lines of ANSI text at most `width` columns each: the tabs, a note while there is one,
    the page's rows (scrolled), and at the bottom the keys, the web page's address and the version."""
    width, height = max(20, width), max(5, height)
    lines: List[str] = []

    # The tabs, as the pane's: the view in blue, the rest gray; Follow Claude when zoomed, Back when there is a way back.
    parts = [(sgr(1, 4, BLUE) if st.view == vid else sgr(2), label) for vid, label, _key in VIEWS]
    if st.part:
        parts.append((sgr(BLUE), "● Follow Claude (f)"))
    if st.back:
        parts.append((sgr(2), "← Back (b)"))
    zw = ZOOM_WORD[page.get("zoom", 0)] if page and 0 <= int(page.get("zoom") or 0) < 3 else "full"
    parts.append((sgr(2), f"⊖ {zw} ⊕ (- =)"))
    line, n = "", 0
    for code, text in parts:
        gap = 2 if n else 0
        if n + gap + cells(text) > width:
            if n + gap < width - 1:
                line += " " * gap + code + fit(text, width - n - gap) + RESET
            break
        line += " " * gap + code + text + RESET
        n += gap + cells(text)
    lines.append(line)
    if st.note:
        lines.append(sgr(2) + fit(st.note, width) + RESET)

    bh, wide = layout(page, st, width, height)
    if page is None:
        rows: List[Row] = []
    else:
        picked = _picked(page, st)
        sel = picked["id"] if picked else ""
        lit = set((page.get("lights") or {}).get(sel) or [])
        rows = body_rows(page, sel)
        if wide:
            total = int(page["wide"])
            lines.append(sgr(2) + fit(f"{'◂ ' if st.sx > 0 else '  '}columns {st.sx + 1}–{min(total, st.sx + width)} "
                                      f"of {total} · [ ] scroll across{' ▸' if st.sx + width < total else ''}",
                                      width) + RESET)
    for y in range(st.sy, st.sy + bh):
        if page is None or y >= len(rows):
            lines.append("")
            continue
        lines.append(paint(cut(rows[y], st.sx, width), sel, lit))

    # The bottom line: the keys, then the web page's address and the version on the right.
    right = (f"🌐 {page['web']}  " if page and page.get("web") else "") + f"v{version}"
    if cells(right) > width:
        right = f"v{version}"
    keys = "q quit · ←↑↓→ pick · ↵ zoom in · b back · n r a t views · [ ] ⇧↑↓ PgUp PgDn scroll · - = zoom"
    room = width - cells(right) - 2
    left = fit(keys, room) if room >= 12 else ""
    lines.append(sgr(2) + left + " " * max(1, width - cells(left) - cells(right)) + right + RESET)
    return lines[:height]


# --- the app: state, the pages built in a thread, the keys ----------------------------------------------------------

Build = Callable[[int, str, str, int], Dict[str, Any]]   # width, view, part, zoom -> page


class Tui:
    """The terminal view's state and work, without the terminal: `handle` takes a key, `screen` draws, `tick` takes in
    a finished page and asks for the next one on the clock."""

    def __init__(self, root: Path, view: str = "now", build: Optional[Build] = None,
                 size: Callable[[], Tuple[int, int]] = lambda: tuple(shutil.get_terminal_size((100, 30))),
                 clock: Callable[[], float] = time.monotonic, progress: Optional[Callable[[], str]] = None):
        self.root = root
        self.st = State(view=view if view in {v for v, _, _ in VIEWS} else "now")
        self.page: Optional[Dict[str, Any]] = None
        self.size = size
        self.clock = clock
        self._build = build or self._pane
        self._progress = progress or self._read_progress
        self._cv = threading.Condition()
        self._want: Optional[Tuple[int, str, str, int]] = None
        self._busy: Optional[Tuple[int, str, str, int]] = None
        self._began = 0.0
        self._got: Optional[Tuple[Tuple[int, str, str, int], Optional[Dict[str, Any]], str]] = None
        self._done_at = -REFRESH
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._width = 0
        self._resized_at = 0.0
        self._progress_at = 0.0
        self._error = ""

    # The page, as the pane asks the engine for it (never taking what the web page sent to Claude: the pane does).
    def _pane(self, width: int, view: str, part: str, zoom: int) -> Dict[str, Any]:
        from . import pane, progress
        progress.begin(self.root)
        try:
            return pane.show(self.root, width, view, part or None, zoom, take=False)
        finally:
            progress.end()

    def _read_progress(self) -> str:
        from . import progress
        return progress.line(self.root)

    def start(self) -> None:
        self._width = self.size()[0]
        self.request()
        self._thread = threading.Thread(target=self._work, name="system-diff-tui", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def request(self) -> None:
        with self._cv:
            self._want = (self._width or self.size()[0], self.st.view, self.st.part, self.st.zoom)
            self._cv.notify_all()

    def _work(self) -> None:
        while True:
            with self._cv:
                while self._want is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                req, self._want = self._want, None
                self._busy, self._began = req, self.clock()
            page, err = None, ""
            try:
                page = self._build(*req)
            except Exception as exc:  # noqa: BLE001 - shown in place of the page, as the pane does
                err = f"{type(exc).__name__}: {exc}".strip()[:200]
            with self._cv:
                self._busy, self._got, self._done_at = None, (req, page, err), self.clock()

    def busy(self) -> bool:
        with self._cv:
            return self._busy is not None or self._want is not None

    def tick(self) -> None:
        """Take in a finished page, follow a resize once it holds, look again on the clock, say where a long read is."""
        now = self.clock()
        with self._cv:
            got, self._got = self._got, None
            building, began, idle = self._busy is not None, self._began, self._busy is None and self._want is None
        if got is not None:
            (_w, view, part, _z), page, err = got
            self._error = f"system-diff failed here: {err}" if err else ""
            if page is not None and view == self.st.view and part == self.st.part:
                self.take(page)
        width = self.size()[0]
        if width != self._width:
            if self._resized_at == 0.0:
                self._resized_at = now
            elif now - self._resized_at >= 0.3:   # a drag passes many widths: lay out the one it settles at
                self._width, self._resized_at = width, 0.0
                self.request()
        else:
            self._resized_at = 0.0
        if idle and now - self._done_at >= REFRESH:
            self.request()
        note = self._error
        if building and (self.page is None or now - began > 1.0):
            if now - self._progress_at >= 1.0 or not self.st.note:
                self._progress_at = now
                try:
                    line = self._progress()
                except Exception:  # noqa: BLE001 - only a convenience
                    line = ""
                self._last_progress = line
            line = getattr(self, "_last_progress", "")
            note = ("Reading the system (the first time in a large repository takes a while): " + line) if line \
                else ("Reading the system…" if self.page is None else note)
        self.st.note = note

    def take(self, page: Dict[str, Any]) -> None:
        self.page = page
        ids = {b["id"] for b in page.get("blocks") or []}
        if not self.st.mine or self.st.sel not in ids:   # until the person picks, the pick follows the page's (Claude's)
            self.st.sel = page.get("pick") if page.get("pick") in ids else (page["blocks"][0]["id"] if ids else "")
            self.st.mine = False
        self._clamp()

    def _clamp(self) -> None:
        if self.page is None:
            return
        width, height = self.size()
        bh, _wide = layout(self.page, self.st, width, height)
        self.st.sy = max(0, min(self.st.sy, len(self.page.get("rows") or []) - bh))
        picked = None if self.st.free else _picked(self.page, self.st)
        self.st.sx = across(self.page, width, self.st.sx, picked)

    def _reveal(self, b: Dict[str, Any]) -> None:
        width, height = self.size()
        bh, _ = layout(self.page, self.st, width, height)
        if not (self.st.sy <= b["y"] and b["y"] + b["h"] <= self.st.sy + bh):
            self.st.sy = max(0, b["y"] - bh // 3)

    def _go(self, view: Optional[str] = None, part: Optional[str] = None) -> None:
        if view is not None:
            self.st.view = view
        if part is not None:
            self.st.part = part
        self.st.sel, self.st.sy, self.st.sx, self.st.free, self.st.mine = "", 0, 0, False, False
        self.request()

    def _act(self, what: Optional[Dict[str, Any]]) -> None:
        if not what:
            return
        if isinstance(what.get("zoom"), str):
            if what["zoom"] != self.st.part:
                self.st.back = (self.st.back + [self.st.part])[-20:]
            self._go("now", what["zoom"])
        elif isinstance(what.get("learn"), str):
            from . import profile
            profile.toggle(what["learn"])
            self.request()
        elif isinstance(what.get("view"), str):
            self._go(what["view"])

    def handle(self, key: str) -> bool:
        """One key, as the pane takes it. False: quit."""
        if key in ("q", "Q", "ctrl-c"):
            return False
        page = self.page
        width, height = self.size()
        bh, _ = layout(page, self.st, width, height)
        picked = _picked(page, self.st) if page else None
        if key in ("shift-up", "shift-down"):
            self.st.sy += -5 if key == "shift-up" else 5
        elif key in ("pageup", "pagedown"):
            self.st.sy += (-1 if key == "pageup" else 1) * max(1, bh - 2)
        elif key in ("home", "end"):
            self.st.sy = 0 if key == "home" else 10 ** 6
        elif key in ("up", "down", "left", "right"):
            if page and page.get("blocks"):
                b = move(page["blocks"], picked, key)
                if b is not None:
                    self.st.sel, self.st.free, self.st.mine = b["id"], False, True
                    self.st.sx = across(page, width, self.st.sx, b)
                    self._reveal(b)
        elif key in ("[", "]"):
            if page:
                self.st.sx = across(page, width, self.st.sx + (-STEP if key == "[" else STEP), None)
                self.st.free = True
        elif key in ("-", "=", "+"):
            drawn = int((page or {}).get("zoom") or 0) if self.st.zoom < 0 else self.st.zoom
            self.st.zoom = max(0, min(2, drawn + (1 if key == "-" else -1)))
            self.request()
        elif key == "return":
            self._act((picked or {}).get("enter"))
        elif key == " ":
            self._act((picked or {}).get("space"))
        elif key in ("b", "backspace"):
            if self.st.back:
                self._go(part=self.st.back.pop())
        elif key == "f":
            self.st.back = []
            self._go("now", "")
        elif key in VIEW_KEYS:
            self._go(VIEW_KEYS[key])
        self._clamp()
        return True

    def screen(self) -> List[str]:
        width, height = self.size()
        self._clamp()
        return render(self.page, self.st, width, height)


def frame(lines: List[str], before: Optional[List[str]], height: int) -> str:
    """The escape sequences that turn the screen `before` into `lines`: only the rows that changed, each cleared and
    written in place (no newlines, so nothing scrolls)."""
    out = []
    if before is None:
        out.append("\x1b[2J")
    for i in range(height):
        new = lines[i] if i < len(lines) else ""
        if before is not None and i < len(before) and before[i] == new:
            continue
        out.append(f"\x1b[{i + 1};1H\x1b[2K{new}{RESET}")
    return "".join(out)


def loop(tui: Tui, read_key: Callable[[float], Optional[str]], write: Callable[[str], None],
         poll: float = 0.1) -> int:
    """Draw, read a key (waiting at most `poll` seconds), handle it; until q."""
    tui.start()
    last: Optional[List[str]] = None
    shape = None
    try:
        while True:
            tui.tick()
            size = tui.size()
            lines = tui.screen()
            if size != shape:
                last, shape = None, size
            if lines != last:
                write(frame(lines, last, size[1]))
                last = lines
            key = read_key(poll)
            if key is not None and not tui.handle(key):
                return 0
    finally:
        tui.stop()


# --- the terminal ---------------------------------------------------------------------------------------------------

def parse(text: str) -> Tuple[List[str], str]:
    """Keys out of what a POSIX terminal sent, and what is left of an escape sequence cut short."""
    keys: List[str] = []
    i = 0
    finals = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
    tildes = {"1": "home", "4": "end", "5": "pageup", "6": "pagedown", "7": "home", "8": "end"}
    while i < len(text):
        ch = text[i]
        if ch == "\x1b":
            if i + 1 >= len(text):
                return keys, text[i:]
            if text[i + 1] in "[O":
                j = i + 2
                while j < len(text) and (text[j].isdigit() or text[j] == ";"):
                    j += 1
                if j >= len(text):
                    return keys, text[i:]
                params, final = text[i + 2:j].split(";"), text[j]
                shift = len(params) > 1 and params[1] in ("2", "4", "6", "8")
                name = finals.get(final) if final != "~" else tildes.get(params[0])
                if name:
                    keys.append(("shift-" + name) if shift and name in ("up", "down", "left", "right") else name)
                i = j + 1
                continue
            keys.append("esc")
            i += 1
            continue
        keys.append({"\r": "return", "\n": "return", "\x7f": "backspace", "\x08": "backspace",
                     "\x03": "ctrl-c"}.get(ch, ch))
        i += 1
    return keys, ""


class Terminal:
    """The terminal, taken for the view and given back as it was: the alternate screen, no cursor, keys one at a time,
    UTF-8 out whatever the code page."""

    def __init__(self) -> None:
        self.windows = os.name == "nt"
        self._keys: List[str] = []
        self._rest = ""
        self._saved: Any = None
        self._win: Dict[str, Any] = {}
        self._signals: Dict[int, Any] = {}

    def __enter__(self) -> "Terminal":
        if self.windows:
            self._win_enter()
        else:
            import signal
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)   # keys at once, no echo; Ctrl+C still interrupts
            import codecs
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

            def leave(signum, _frame):  # a closed window or a kill: out through the finally that restores
                raise SystemExit(128 + signum)
            for name in ("SIGTERM", "SIGHUP"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    try:
                        self._signals[sig] = signal.signal(sig, leave)
                    except (ValueError, OSError):
                        pass
        self.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        finally:
            if self.windows:
                self._win_exit()
            elif self._saved is not None:
                import signal
                import termios
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
                for sig, old in self._signals.items():
                    try:
                        signal.signal(sig, old)
                    except (ValueError, OSError):
                        pass

    def _win_enter(self) -> None:
        try:
            import ctypes
            k = ctypes.windll.kernel32  # type: ignore[attr-defined]
            out = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(out, ctypes.byref(mode)):
                self._win["mode"] = (out, mode.value)
                k.SetConsoleMode(out, mode.value | 0x0004 | 0x0001)   # virtual terminal processing
            self._win["cp"] = k.GetConsoleOutputCP()
            k.SetConsoleOutputCP(65001)   # UTF-8: the diagrams' box-drawing characters, whatever the code page
        except Exception:  # noqa: BLE001 - an old console: os.system('') turns the sequences on where it can
            os.system("")

    def _win_exit(self) -> None:
        try:
            import ctypes
            k = ctypes.windll.kernel32  # type: ignore[attr-defined]
            if "mode" in self._win:
                k.SetConsoleMode(*self._win["mode"])
            if "cp" in self._win:
                k.SetConsoleOutputCP(self._win["cp"])
        except Exception:  # noqa: BLE001
            pass

    def write(self, text: str) -> None:
        data = text.encode("utf-8", "replace")
        out = getattr(sys.stdout, "buffer", None)
        if out is not None:
            out.write(data)
            out.flush()
        else:
            sys.stdout.write(text)
            sys.stdout.flush()

    def read_key(self, timeout: float) -> Optional[str]:
        if self._keys:
            return self._keys.pop(0)
        if self.windows:
            return self._win_key(timeout)
        import select
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            if self._rest:   # a lone Esc, not the start of a sequence
                keys, self._rest = parse(self._rest + "\x00")
                self._keys += [k for k in keys if k != "\x00"]
            return self._keys.pop(0) if self._keys else None
        data = os.read(self.fd, 1024)
        if not data:
            return "q"   # the terminal went away
        keys, self._rest = parse(self._rest + self._decoder.decode(data))
        self._keys += keys
        if self._rest and not self._keys:
            ready, _, _ = select.select([self.fd], [], [], 0.05)
            if ready:
                return self.read_key(0)
            keys, self._rest = parse(self._rest + "\x00")
            self._keys += [k for k in keys if k != "\x00"]
        return self._keys.pop(0) if self._keys else None

    def _win_key(self, timeout: float) -> Optional[str]:
        import msvcrt
        end = time.monotonic() + timeout
        while not msvcrt.kbhit():  # type: ignore[attr-defined]
            if time.monotonic() >= end:
                return None
            time.sleep(0.02)
        ch = msvcrt.getwch()  # type: ignore[attr-defined]
        if ch in ("\x00", "\xe0"):
            code = msvcrt.getwch()  # type: ignore[attr-defined]
            return {"H": "up", "P": "down", "K": "left", "M": "right", "I": "pageup", "Q": "pagedown",
                    "G": "home", "O": "end", "\x8d": "shift-up", "\x91": "shift-down"}.get(code)
        return {"\r": "return", "\n": "return", "\x08": "backspace", "\x7f": "backspace", "\x1b": "esc",
                "\x03": "ctrl-c"}.get(ch, ch)


def main(root: Path, view: str = "now") -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("system-diff tui: needs a terminal (run it in a terminal tab or window of its own)", file=sys.stderr)
        return 1
    try:
        with Terminal() as term:
            return loop(Tui(root, view), term.read_key, term.write)
    except KeyboardInterrupt:
        return 0
