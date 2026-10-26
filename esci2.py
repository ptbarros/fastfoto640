# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Paul Barros
"""Tiny ESC/I-2 driver for the Epson FastFoto FF-640, over USB (pyusb).

Why not SANE: the epsonds backend never asks for overscan, so the scanner
stops each page a few lines before the paper's trailing edge and corners of
crooked bills are lost. Talking to the scanner directly lets us send OVSN.

Protocol (from sane-backends epsonds-cmd.c):
  * session:   send FS 'X' (1c 58) -> ACK 06;  end with "FIN x0000000"
  * command:   12-byte header "CMD x%07x" (payload length), then the payload.
               Reply: 64-byte header "CMD x%07x#tok...#tok..." + that many
               more bytes of data.
  * scanning:  PARA (settings), TRDT (go), then repeat "IMG x0000000": each
               reply carries a block of raw image data and tokens:
                 #typIMGA / #typIMGB  which side the data belongs to
                 #pst...              page start (width, dummy bytes, height)
                 #pen...              page end (final width, height)
                 #lftd000             no pages left
                 #errADF PE           feeder empty, #errADF PJ jam, ...
"""

import re
import time

import cv2
import numpy as np
import usb.core
import usb.util

VENDOR, PRODUCT = 0x04B8, 0x015F
EP_OUT, EP_IN = 0x02, 0x81


class ScanError(Exception):
    def __init__(self, msg, cause=""):
        super().__init__(msg)
        self.cause = cause        # e.g. "PE" (paper empty), "PJ" (jam)


def _decode(v: bytes):
    """Decode one ESC/I-2 number: d123 (dec), i1234567 (dec), x/h (hex)."""
    t = v[:1]
    if t in (b"d", b"i"):
        return int(v[1:])
    if t in (b"x", b"h"):
        return int(v[1:], 16)
    raise ValueError(v)


def _numbers(v: bytes):
    """Split a token value like b'i0002544i0000000i0004193' into ints."""
    return [_decode(m) for m in re.findall(rb"d\d{3}|i\d{7}|x[0-9A-Fa-f]{7}|h[0-9A-Fa-f]{3}", v)]


def _tokens(block: bytes):
    """Parse '#nrdNONE#parOK  #pen...' into [(name, value)]."""
    out = []
    block = block.split(b"\x00", 1)[0]
    for part in block.split(b"#")[1:]:
        if part.startswith(b"---"):
            break
        out.append((part[:3].decode("ascii", "replace"), part[3:]))
    return out


class FF640:
    """One open ESC/I-2 session. Use as a context manager."""

    def __init__(self):
        dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
        if dev is None:
            raise ScanError("scanner not found")
        self.dev = dev
        try:
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as e:
            usb.util.dispose_resources(dev)
            raise ScanError(f"scanner busy ({e})")
        self.locked = False
        self._drain()
        if not self._lock():
            # Wedged in an old session: reset the device and try once more.
            self._release()
            dev.reset()
            usb.util.dispose_resources(dev)
            time.sleep(1.0)
            dev = usb.core.find(idVendor=VENDOR, idProduct=PRODUCT)
            if dev is None:
                raise ScanError("scanner vanished after reset")
            self.dev = dev
            usb.util.claim_interface(dev, 0)
            self._drain()
            if not self._lock():
                self._release()
                raise ScanError("scanner not responding")

    # -- plumbing ----------------------------------------------------------- #
    def _drain(self, timeout=150):
        data = b""
        while True:
            try:
                data += bytes(self.dev.read(EP_IN, 65536, timeout=timeout))
            except usb.core.USBError:
                return data
            timeout = 100

    def _lock(self):
        self.dev.write(EP_OUT, b"\x1cX", timeout=2000)
        self.locked = self._drain(1000)[:1] == b"\x06"
        return self.locked

    def _read(self, n, timeout=30000):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.dev.read(EP_IN, max(n - len(buf), 512), timeout=timeout)
            buf += bytes(chunk)
        return bytes(buf[:n])

    def _release(self):
        try:
            usb.util.release_interface(self.dev, 0)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass

    def close(self):
        if self.dev is None:
            return
        if self.locked:
            try:
                self.dev.write(EP_OUT, b"FIN x0000000", timeout=2000)
                self._drain(500)
            except Exception:
                pass
            self.locked = False
        self._release()
        self.dev = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def cmd(self, name: str, payload: bytes = b"", timeout=30000):
        """Send one command, return (tokens, data)."""
        head = f"{name:<4.4}x{len(payload):07x}".encode()
        self.dev.write(EP_OUT, head, timeout=5000)
        if payload:
            self.dev.write(EP_OUT, payload, timeout=5000)
        reply = self._read(64, timeout)
        if reply[:4] != head[:4]:
            raise ScanError(f"{name}: unexpected reply {reply[:12]!r}")
        more = int(reply[5:12], 16)
        toks = _tokens(reply[12:])
        data = self._read(more, timeout) if more else b""
        return toks, data

    # -- commands ----------------------------------------------------------- #
    def status(self):
        toks, data = self.cmd("STAT")
        return toks + _tokens(data)

    def paper_loaded(self):
        return not any(n == "ERR" and v.startswith(b"ADF PE") for n, v in self.status())

    @staticmethod
    def _decode_page(buf, w, h, color, jpeg):
        if not buf:
            return None
        if jpeg:
            return cv2.imdecode(np.frombuffer(buf, np.uint8),
                                cv2.IMREAD_COLOR if color else cv2.IMREAD_GRAYSCALE)
        if not h:
            return None
        ch = 3 if color else 1
        bpl = len(buf) // h                # may include padding bytes per line
        img = np.frombuffer(buf, np.uint8)[: bpl * h].reshape(h, bpl)[:, : w * ch]
        if not color:
            return img.reshape(h, w)
        return cv2.cvtColor(img.reshape(h, w, 3), cv2.COLOR_RGB2BGR)

    def scan(self, dpi=300, duplex=True, overscan=True, color=True, stop=None,
             width_px=None, max_len_px=None, jpeg_quality=95):
        """Feed and scan every sheet in the feeder.

        Yields (side, image) for each finished side: side is "A" (front) or
        "B" (back), image a BGR (or gray) numpy array.

        jpeg_quality: the scanner compresses each page itself before sending
        it (None = raw pixels). With overscan on, the scanner pads every page
        to its full 14-inch length with white; raw, that padding is 3/4 of the
        USB transfer and makes scanning ~3x slower. As JPEG it's almost free."""
        # Whole-width window, full length; with OVSN the scanner adds margin.
        width_px = width_px or int(8.5 * dpi)
        max_len_px = max_len_px or int(14 * dpi)
        adf = b"#ADF" + (b"DPLX" if duplex else b"") + (b"OVSN" if overscan else b"")
        para = (adf
                + (b"#COLC024" if color else b"#COLM008")
                + (f"#FMTJPG #JPGd{int(jpeg_quality):03d}".encode() if jpeg_quality
                   else b"#FMTRAW ")
                + b"#GMMUG18"
                + f"#RSMd{dpi:03d}#RSSd{dpi:03d}".encode()
                + b"#BSZi1048576"
                + f"#ACQi{0:07d}i{0:07d}i{width_px:07d}i{max_len_px:07d}".encode())
        toks, _ = self.cmd("PARA", para)
        if ("par", b"OK  ") not in [(n, v[:4]) for n, v in toks]:
            raise ScanError(f"scanner rejected settings: {toks}")
        self.cmd("TRDT")

        bufs = {"A": bytearray(), "B": bytearray()}
        dims = {}
        side = "A"
        while True:
            if stop is not None and stop.is_set():
                try:
                    self.cmd("CAN ")
                except Exception:
                    pass
                return
            toks, data = self.cmd("IMG ")
            ended = None
            last = False
            err = None
            for n, v in toks:
                if n == "typ":
                    side = "B" if v[3:4] == b"B" else "A"
                elif n == "pst":
                    nums = _numbers(v)
                    if len(nums) == 3:
                        dims[side] = {"w": nums[0], "dummy": nums[1], "h": nums[2]}
                elif n == "pen":
                    nums = _numbers(v)
                    ended = (nums[0], nums[1]) if len(nums) >= 2 else (None, None)
                elif n == "lft" and _numbers(v) == [0]:
                    last = True
                elif n == "err":
                    err = v[4:8].strip().decode("ascii", "replace") or "ERR"
                elif n == "atn" and v.startswith(b"CAN"):
                    return
            if data:
                bufs[side] += data
            if ended is not None:
                w, h = ended
                buf = bytes(bufs[side])
                bufs[side] = bytearray()
                img = self._decode_page(buf, w, h, color, jpeg_quality)
                if img is not None:
                    yield side, img
            if err:
                if err == "PE":
                    return               # feeder empty: done
                raise ScanError(f"scanner error: {err}", cause=err)
            if last and ended is not None and (not duplex or side == "B"):
                return
