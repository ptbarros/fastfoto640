#!/bin/sh
# Install FastFoto Scan for the current user:
#   - Python virtualenv with the dependencies
#   - udev rule so the scanner can be used without root (asks for sudo)
#   - app-menu entry
set -e
here="$(cd "$(dirname "$0")" && pwd)"

if ! python3 -c "import tkinter" 2>/dev/null; then
    echo "Tkinter is missing. On Debian/Ubuntu: sudo apt install python3-tk python3-venv"
    exit 1
fi

python3 -m venv "$here/venv"
"$here/venv/bin/pip" install -q -r "$here/requirements.txt"

if [ ! -f /etc/udev/rules.d/60-epson-ff640.rules ]; then
    echo "Installing udev rule (needs sudo)..."
    sudo cp "$here/60-epson-ff640.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger --action=add --attr-match=idVendor=04b8 --attr-match=idProduct=015f || true
fi

apps="$HOME/.local/share/applications"
mkdir -p "$apps"
cat > "$apps/fastfoto-scan.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=FastFoto Scan
Comment=Scan photos and bills with the Epson FastFoto FF-640
Exec=$here/fastfoto-scan
Icon=scanner
Terminal=false
Categories=Graphics;Scanning;
DESKTOP
update-desktop-database "$apps" 2>/dev/null || true

echo "Done. Start \"FastFoto Scan\" from your app menu, or run $here/fastfoto-scan"
