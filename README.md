# FastFoto Scan: Epson FastFoto FF-640 on Linux

Epson doesn't ship Linux drivers for the FastFoto FF-640 photo scanner. This
is a small app that drives it directly over USB. You drop a stack of
photos or banknotes in the feeder, it scans both sides, and it writes
cropped and straightened JPEGs into a folder, much like Epson's FastFoto
software does on Windows and macOS.

It was written to feed [Dollar Detective](#use-with-dollar-detective)
(fancy serial-number spotting on US $1 bills), but it works for anything the
feeder takes.

## Features

- **Starts by itself** when you put paper in the feeder (or click *Scan now*).
- **Both sides in one pass**, saved as `FastFoto_00001.jpg` (front) and
  `FastFoto_00001_b.jpg` (back), numbered across sessions.
- **Complete edges.** It turns on the scanner's overscan, so every page has
  background on all four sides, even when an item feeds in crooked.
- **Auto-crop and straighten.** Items that feed rotated or slanted are
  squared up. An adjustable margin (Tight / Normal / Loose) keeps a little
  background around each one.
- **300 or 600 dpi.** About 0.8 s per bill, both sides, at 300 dpi.
- Files are written under a temporary name and then renamed, so a program
  watching the folder never picks up a half-written image.

## Install

Tested on Ubuntu 24.04 with Python 3.12.

```sh
sudo apt install python3-tk python3-venv
git clone https://github.com/ptbarros/fastfoto640.git
cd fastfoto640
./install.sh
```

`install.sh` creates a virtualenv, installs a udev rule so you can use the
scanner without root (this step asks for `sudo`), and adds **FastFoto Scan**
to your app menu. Plug the scanner in, switch it on, and start the app.

## Use with Dollar Detective

Point *Save to* at Dollar Detective's Monitor folder. If Dollar Detective is
installed, the app picks up that folder by default. Dollar Detective already
pairs the `NNNN.jpg` + `NNNN_b.jpg` naming.

## How it works

| File | What it does |
|------|--------------|
| `esci2.py` | Minimal driver for Epson's ESC/I-2 scanner protocol over USB (pyusb): status, settings, feeding, and image transfer. Pages come over as JPEG made by the scanner itself. |
| `billcrop.py` | Finds the item on the grey background, fits a line to each of its four edges, and maps the corners onto an upright rectangle (OpenCV). |
| `fastfoto_scan.py` | The Tk app: watches the feeder, runs a scan, crops in a background thread, pairs fronts with backs, and writes the files. |

Why not SANE? SANE's `epsonds` backend can talk to the FF-640 if you add its
USB ID (`usb 0x04b8 0x015f` in `/etc/sane.d/epsonds.conf`), but it never asks
for overscan. Without overscan the scanner ends each page a few lines before
the paper does, so a corner of a crooked item is lost. The protocol details
here were worked out from the sane-backends `epsonds` source and by talking to
the scanner. No SANE code is included.

Settings live in `~/.config/fastfoto640/settings.json` and the log in
`~/.cache/fastfoto640/fastfoto.log`. To debug cropping, add
`"keep_raw_dir": "/some/folder"` to the settings, and every uncropped page
is saved there as well.

## Limitations

- Only tested with the FF-640 (USB ID `04b8:015f`). Other Epson sheet-fed
  scanners that speak ESC/I-2 might work with a changed product ID; that's
  untested.
- Colour only, 300/600 dpi. Epson's photo enhancement, red-eye removal and
  restoration are not implemented.
- Items are assumed to feed long-edge-first and be under 14 inches long.

## License

[GNU AGPL v3 or later](LICENSE). Not affiliated with or endorsed by Epson.
"FastFoto" and "Epson" are trademarks of Seiko Epson Corporation.
