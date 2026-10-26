#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Barros
"""FastFoto Scan - a small scanning app for the Epson FastFoto FF-640 on Linux.

Watches the feeder, starts scanning by itself when bills are loaded, crops and
straightens each side, and drops the pair into an output folder as
    FastFoto_00001.jpg  (front)
    FastFoto_00001_b.jpg  (back)
which is the naming Dollar Detective's Monitor mode already pairs.

Scanning talks to the scanner directly over USB (esci2.py, the ESC/I-2
protocol) instead of through SANE, because SANE's epsonds backend never asks
for overscan and so loses a sliver of each page's trailing edge. The "is
paper loaded?" check uses the same protocol's STAT command, which does not
move the feeder.
"""

import json
import os
import queue
import re
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, ttk

import cv2
from PIL import Image, ImageTk

import esci2
from billcrop import crop_item

VENDOR, PRODUCT = 0x04B8, 0x015F
CONFIG_PATH = Path.home() / ".config" / "fastfoto640" / "settings.json"
LOG_PATH = Path.home() / ".cache" / "fastfoto640" / "fastfoto.log"


def log(msg: str):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg.rstrip() + "\n")
    except OSError:
        pass
PREFIX = "FastFoto_"
NAME_RE = re.compile(rf"^{PREFIX}(\d+)(_b)?\.jpg$", re.IGNORECASE)
# Longest page we accept along the feed direction. A bill fed long-edge-first
# is 66 mm, plus overscan (about 11 mm each end) and room for a crooked feed.
# (With overscan on, the FF-640 ignores this and pads every page to 14 inches;
# that's why pages are transferred as JPEG, where the padding is nearly free.)
SCAN_LENGTH_MM = 120
# Background kept around the bill, in pixels at 300 dpi (scaled for 600).
MARGINS = {"Tight": 0, "Normal": 6, "Loose": 16}
SETTLE_SECONDS = 1.5     # wait after paper appears, so a stack can be squared up


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def dollar_detective_folder() -> Path:
    """The folder Dollar Detective's Monitor mode watches."""
    cfg = Path.home() / ".config" / "DollarDetective" / "user_settings.yaml"
    try:
        for line in cfg.read_text().splitlines():
            m = re.match(r"\s*data_folder:\s*['\"]?([^'\"]*)['\"]?\s*$", line)
            if m and m.group(1).strip():
                return Path(m.group(1).strip()).expanduser()
    except OSError:
        pass
    return Path.home() / "DollarDetective"


def load_settings() -> dict:
    s = {"output_dir": str(dollar_detective_folder()), "dpi": 300,
         "duplex": True, "auto_start": True, "counter": 0, "jpeg_quality": 95,
         "margin": "Normal"}
    try:
        s.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return s


_save_lock = threading.Lock()


def save_settings(s: dict):
    with _save_lock:
        _save_settings(s)


def _save_settings(s: dict):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2))
        tmp.replace(CONFIG_PATH)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Scanner status over USB (ESC/I-2)
# --------------------------------------------------------------------------- #

def feeder_status() -> str:
    """Return 'loaded', 'empty', 'busy' or 'missing'.

    If the scanner is wedged inside an old session (a previous check or scan
    was interrupted before it said FIN), it ignores everything, including
    scanimage. In that case reset it over USB once and ask again."""
    status = _query_feeder()
    if status == "stuck":
        reset_scanner()
        status = _query_feeder()
    return "busy" if status == "stuck" else status


def reset_scanner():
    """USB-reset the scanner: clears a wedged ESC/I-2 session."""
    import usb.core
    import usb.util
    try:
        dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
        if dev is not None:
            dev.reset()
            usb.util.dispose_resources(dev)
            time.sleep(1.0)
    except Exception:
        pass


def _query_feeder() -> str:
    """One status query: 'loaded', 'empty', 'busy', 'missing' or 'stuck'."""
    import usb.core
    import usb.util
    try:
        dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
    except Exception:
        return "missing"
    if dev is None:
        return "missing"
    try:
        usb.util.claim_interface(dev, 0)
    except Exception:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass
        return "busy"   # scanimage (or another program) has it open

    def drain(timeout=150):
        """Read everything pending: wait up to `timeout` ms for the first
        chunk, then only briefly for any follow-on chunks."""
        data = b""
        while True:
            try:
                data += bytes(dev.read(0x81, 65536, timeout=timeout))
            except Exception:
                return data
            timeout = 100

    locked = False
    try:
        drain()                                   # stale replies, if any
        dev.write(0x02, b"\x1cX", timeout=2000)   # open session
        locked = drain(1000)[:1] == b"\x06"
        if not locked:
            return "stuck"
        dev.write(0x02, b"STATx0000000", timeout=2000)
        reply = drain(1000)
        if not reply.startswith(b"STAT"):
            return "stuck"
        return "empty" if b"ADF PE" in reply else "loaded"
    except Exception:
        return "stuck"
    finally:
        # Always close the session, or the scanner refuses everyone after us.
        if locked:
            try:
                dev.write(0x02, b"FIN x0000000", timeout=2000)
                drain(500)
            except Exception:
                pass
        try:
            usb.util.release_interface(dev, 0)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Scanning worker
# --------------------------------------------------------------------------- #

class Scanner(threading.Thread):
    """Background thread: waits for paper, scans with the direct ESC/I-2
    driver (esci2.py, with overscan so no edge is lost), and hands each page
    to a second thread that crops and writes it (so cropping never makes the
    feeder wait). Talks to the GUI only through `events`."""

    def __init__(self, settings: dict, events: queue.Queue):
        super().__init__(daemon=True)
        self.settings = settings
        self.events = events
        self.scan_now = threading.Event()
        self.stop_scan = threading.Event()
        self.quit = threading.Event()

    def emit(self, kind, payload=None):
        if kind in ("warn", "error"):
            log(f"{kind}: {payload}")
        self.events.put((kind, payload))

    def crashed(self, where):
        tb = traceback.format_exc()
        log(f"crash in {where}:\n{tb}")
        self.emit("error", f"Internal error in {where}: {tb.strip().splitlines()[-1]} (details in {LOG_PATH})")

    def cancel(self):
        """Stop the current stack (safe to call from the GUI thread); the
        scan loop notices between data blocks and tells the scanner."""
        self.stop_scan.set()

    # -- main loop ---------------------------------------------------------- #
    def run(self):
        while not self.quit.is_set():
            try:
                self._loop()
            except Exception:
                self.crashed("scanner loop")
                self.emit("done", (0, 0.0))
                time.sleep(2)

    def _loop(self):
        last = None
        while not self.quit.is_set():
            if self.scan_now.is_set():
                self.scan_now.clear()
                self.scan_stack()
                last = None
                continue
            status = feeder_status()
            if status != last:
                self.emit("feeder", status)
                last = status
            if status == "loaded" and self.settings["auto_start"]:
                time.sleep(SETTLE_SECONDS)
                if feeder_status() == "loaded" and self.settings["auto_start"]:
                    self.scan_stack()
                    last = None
                    continue
            self.scan_now.wait(1.0 if status != "missing" else 3.0)

    # -- one stack ---------------------------------------------------------- #
    def scan_stack(self):
        s = self.settings
        out_dir = Path(s["output_dir"]).expanduser()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.emit("error", f"Can't use output folder: {e}")
            return
        self.stop_scan.clear()
        self.emit("scanning", None)
        started = time.time()
        err = None
        pages = 0
        dpi = int(s["dpi"])

        pages_q = queue.Queue(maxsize=8)
        result = {"bills": 0}
        writer = threading.Thread(target=self._process_pages,
                                  args=(pages_q, out_dir, result), daemon=True)
        writer.start()
        try:
            with esci2.FF640() as dev:
                for side, img in dev.scan(
                        dpi=dpi, duplex=bool(s["duplex"]), overscan=True,
                        max_len_px=int(SCAN_LENGTH_MM / 25.4 * dpi),
                        jpeg_quality=s.get("scanner_jpeg_quality", 95),
                        stop=self.stop_scan):
                    pages += 1
                    pages_q.put((pages, side, img))
        except esci2.ScanError as e:
            if e.cause == "PJ" and pages == 0:
                err = "no bills in the feeder"
            elif e.cause == "PJ":
                err = "paper jam - check the scanner"
            else:
                err = str(e)
        except Exception as e:
            log(f"scan crash:\n{traceback.format_exc()}")
            err = f"{type(e).__name__}: {e}"
        finally:
            pages_q.put(None)
            writer.join()

        secs = time.time() - started
        if pages == 0 and not self.stop_scan.is_set():
            self.emit("error", f"Nothing was scanned: {err or 'unknown scanner problem'}")
        elif err:
            self.emit("warn", f"Scanning stopped early: {err}")
        self.emit("done", (result["bills"], secs))

    def _process_pages(self, pages_q, out_dir, result):
        """Crop, pair each front (side A) with its back (side B), and write.
        Runs alongside the scanning.

        Both sides of a bill are scanned at the same time, and the scanner
        doesn't always finish sending the front first, so sides are paired by
        position: the nth front goes with the nth back."""
        s = self.settings
        fronts, backs = [], []
        while True:
            item = pages_q.get()
            if item is None:
                break
            page, side, img = item
            try:
                cropped = self._crop(page, img)
                if not s["duplex"]:
                    self._save_bill(out_dir, result, cropped, None)
                    continue
                (backs if side == "B" else fronts).append(cropped)
                while fronts and backs:
                    self._save_bill(out_dir, result, fronts.pop(0), backs.pop(0))
            except Exception:
                self.crashed(f"page {page}")
        try:
            for front in fronts:
                n = self._save_bill(out_dir, result, front, None)
                self.emit("warn", f"Bill {n:05d} has no back image (scan stopped mid-bill)")
            if backs:
                self.emit("warn", f"{len(backs)} back image(s) without a front were skipped")
        except Exception:
            self.crashed("last page")

    def _crop(self, page, img):
        s = self.settings
        raw_dir = s.get("keep_raw_dir")   # debugging: keep uncropped scans
        if raw_dir:
            Path(raw_dir).mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(Path(raw_dir) / time.strftime(f"raw_%H%M%S_p{page:03d}.png")), img)
        margin = MARGINS.get(s.get("margin"), 6) * int(s["dpi"]) // 300
        try:
            cropped, found = crop_item(img, margin_px=margin)
        except Exception as e:
            log(f"crop crash on page {page}:\n{traceback.format_exc()}")
            self.emit("warn", f"Page {page}: crop failed ({e}); kept the full scan")
            return img
        if not found:
            self.emit("warn", f"Page {page}: couldn't find the bill edges; kept the full scan")
        return cropped

    def _save_bill(self, out_dir, result, front, back):
        n = self._next_number(out_dir)
        self._write(out_dir, f"{PREFIX}{n:05d}.jpg", front)
        if back is not None:
            self._write(out_dir, f"{PREFIX}{n:05d}_b.jpg", back)
        result["bills"] += 1
        self.emit("bill", (result["bills"], front))
        return n

    def _next_number(self, out_dir: Path) -> int:
        highest = self.settings.get("counter", 0)
        try:
            for name in os.listdir(out_dir):
                m = NAME_RE.match(name)
                if m:
                    highest = max(highest, int(m.group(1)))
        except OSError:
            pass
        self.settings["counter"] = highest + 1
        save_settings(self.settings)
        return highest + 1

    def _write(self, out_dir: Path, name: str, img):
        # Write under a hidden temp name, then rename, so a folder watcher
        # never sees a half-written JPEG.
        tmp = out_dir / f".{name}.part"
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY,
                                             int(self.settings["jpeg_quality"])])
        if not ok:
            self.emit("error", f"Couldn't encode {name}")
            return
        tmp.write_bytes(buf.tobytes())
        os.replace(tmp, out_dir / name)


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

class App:
    FEEDER_TEXT = {
        "missing": "Scanner not found - is it plugged in and switched on?",
        "empty": "Ready - load bills face down to start",
        "loaded": "Bills loaded",
        "busy": "Scanner busy",
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_settings()
        self.events = queue.Queue()
        self.session_bills = 0
        self.scanning = False
        self._thumb = None

        root.title("FastFoto Scan")
        root.minsize(520, 460)
        pad = {"padx": 10, "pady": 4}

        # Output folder
        f = ttk.Frame(root)
        f.pack(fill="x", **pad)
        ttk.Label(f, text="Save to:").pack(side="left")
        self.dir_var = tk.StringVar(value=self.settings["output_dir"])
        ttk.Entry(f, textvariable=self.dir_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(f, text="Browse…", command=self.browse).pack(side="left")
        self.dir_var.trace_add("write", lambda *_: self.update_setting("output_dir", self.dir_var.get()))

        # Options
        o = ttk.Frame(root)
        o.pack(fill="x", **pad)
        ttk.Label(o, text="Resolution:").pack(side="left")
        self.dpi_var = tk.IntVar(value=self.settings["dpi"])
        for d in (300, 600):
            ttk.Radiobutton(o, text=f"{d} dpi", value=d, variable=self.dpi_var,
                            command=lambda: self.update_setting("dpi", self.dpi_var.get())
                            ).pack(side="left", padx=4)
        self.duplex_var = tk.BooleanVar(value=self.settings["duplex"])
        ttk.Checkbutton(o, text="Scan backs", variable=self.duplex_var,
                        command=lambda: self.update_setting("duplex", self.duplex_var.get())
                        ).pack(side="left", padx=12)
        ttk.Label(o, text="Edge margin:").pack(side="left", padx=(12, 0))
        self.margin_var = tk.StringVar(value=self.settings.get("margin", "Normal"))
        m = ttk.Combobox(o, textvariable=self.margin_var, values=list(MARGINS),
                         state="readonly", width=7)
        m.pack(side="left", padx=4)
        m.bind("<<ComboboxSelected>>",
               lambda _e: self.update_setting("margin", self.margin_var.get()))

        o2 = ttk.Frame(root)
        o2.pack(fill="x", **pad)
        self.auto_var = tk.BooleanVar(value=self.settings["auto_start"])
        ttk.Checkbutton(o2, text="Start automatically when bills are loaded",
                        variable=self.auto_var,
                        command=lambda: self.update_setting("auto_start", self.auto_var.get())
                        ).pack(side="left")

        # Buttons
        b = ttk.Frame(root)
        b.pack(fill="x", **pad)
        self.scan_btn = ttk.Button(b, text="Scan now", command=self.scan_now)
        self.scan_btn.pack(side="left")
        self.stop_btn = ttk.Button(b, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.count_var = tk.StringVar(value="0 bills this session")
        ttk.Label(b, textvariable=self.count_var, font=("TkDefaultFont", 11, "bold")
                  ).pack(side="right")

        # Status + preview + log
        self.status_var = tk.StringVar(value="Looking for scanner…")
        ttk.Label(root, textvariable=self.status_var, font=("TkDefaultFont", 12)
                  ).pack(fill="x", **pad)
        self.preview = ttk.Label(root, anchor="center")
        self.preview.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(root, height=5, state="disabled", wrap="word")
        self.log.pack(fill="x", **pad)

        self.worker = Scanner(self.settings, self.events)
        self.worker.start()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    # -- settings ------------------------------------------------------------ #
    def update_setting(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or str(Path.home()))
        if d:
            self.dir_var.set(d)

    # -- actions ------------------------------------------------------------- #
    def scan_now(self):
        self.worker.scan_now.set()

    def stop(self):
        self.worker.cancel()

    def close(self):
        self.stop()
        self.worker.quit.set()
        self.worker.scan_now.set()
        # Let a status check in progress finish, so it can close its scanner
        # session; a check cut off midway leaves the scanner wedged.
        self.worker.join(timeout=5)
        self.root.destroy()

    def add_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # -- events from the worker --------------------------------------------- #
    def poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self.handle(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def handle(self, kind, payload):
        if kind == "feeder" and not self.scanning:
            self.status_var.set(self.FEEDER_TEXT.get(payload, payload))
        elif kind == "scanning":
            self.scanning = True
            self.scan_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.status_var.set(f"Scanning at {self.settings['dpi']} dpi…")
        elif kind == "bill":
            n, img = payload
            self.session_bills += 1
            self.count_var.set(f"{self.session_bills} bill{'s' if self.session_bills != 1 else ''} this session")
            self.status_var.set(f"Scanning… {n} in this stack")
            self.show(img)
        elif kind == "done":
            bills, secs = payload
            self.scanning = False
            self.scan_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            rate = f" ({secs / bills:.1f} s per bill)" if bills else ""
            if not bills:
                return   # keep the error message on screen
            self.status_var.set(f"Stack done: {bills} bills in {secs:.0f} s{rate}. Load more to continue.")
            if bills:
                self.add_log(f"Stack done: {bills} bills{rate} -> {self.settings['output_dir']}")
        elif kind == "warn":
            self.add_log("Warning: " + payload)
        elif kind == "error":
            self.add_log("Error: " + payload)
            self.status_var.set(payload)

    def show(self, bgr):
        w = max(200, self.preview.winfo_width() - 10)
        h = max(120, self.preview.winfo_height() - 10)
        im = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        im.thumbnail((w, h))
        self._thumb = ImageTk.PhotoImage(im)
        self.preview.configure(image=self._thumb)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
