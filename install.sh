#!/bin/bash
# Install Spectre on this Linux machine, with two launchers in the app
# launcher: Spectre (the arena from the desk) and Spectre VR (the same,
# through a headset over OpenXR).
#
#     ./install.sh               install, or update an earlier install
#     ./install.sh --uninstall   take it all back out
#
# The game is copied -- not linked -- into ~/.local/share/spectre with a
# Python environment of its own (pygame-ce, PyOpenGL and pyopenxr), so work
# in this checkout never breaks the installed copy; run the script again to
# install what the checkout has now.  Settings stay where they always are,
# in ~/.config/spectre, shared with a game run from the checkout.
#
# The VR launcher starts the WiVRn server if it is not up and says to
# connect the Quest; the game plays on the desk until it does.  It is also
# listed in the WiVRn app on the Quest (the X-WiVRn-VR category), so it
# can be started from inside the headset.  Both launchers log to
# ~/.local/state/spectre/.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/spectre"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
LOGS="${XDG_STATE_HOME:-$HOME/.local/state}/spectre"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
PYTHON="${PYTHON:-python3}"

say() { printf '\033[1;32mspectre:\033[0m %s\n' "$*"; }

refresh_launcher() {
  update-desktop-database "$APPS" &>/dev/null || true
}

if [[ ${1:-} == "--uninstall" ]]; then
  rm -f "$APPS/spectre.desktop" "$APPS/spectre-vr.desktop" \
        "$ICONS/spectre.png" "$ICONS/spectre-vr.png"
  refresh_launcher
  if [[ -d $DEST ]]; then
    rm -rf "$DEST"
    say "removed $DEST"
  fi
  say "launchers removed.  Settings in ~/.config/spectre and logs in" \
      "$LOGS were left alone."
  exit 0
elif [[ -n ${1:-} ]]; then
  echo "usage: $0 [--uninstall]" >&2
  exit 2
fi

# -- the game's files -------------------------------------------------------
# The game is two modules; the rest of the checkout is for its developer.
say "copying the game into $DEST"
mkdir -p "$DEST"
cp "$SRC/spectre.py" "$SRC/spectre_vr.py" "$SRC/requirements.txt" \
   "$SRC/README.md" "$DEST/"

# -- its own Python ---------------------------------------------------------
if [[ ! -x $DEST/venv/bin/python ]]; then
  say "making a Python environment ($("$PYTHON" --version))"
  "$PYTHON" -m venv "$DEST/venv"
fi
say "installing pygame-ce, PyOpenGL and pyopenxr"
"$DEST/venv/bin/pip" install --quiet --upgrade pip
"$DEST/venv/bin/pip" install --quiet --upgrade -r "$DEST/requirements.txt"

# -- icons ------------------------------------------------------------------
say "drawing the icons"
mkdir -p "$DEST/icons"
PYGAME_HIDE_SUPPORT_PROMPT=1 \
  "$DEST/venv/bin/python" "$SRC/tools/make_icon.py" "$DEST/icons"
# Into the icon theme as well, by name: the WiVRn app on the Quest looks
# its icons up there, not by path.
mkdir -p "$ICONS"
cp "$DEST/icons/spectre.png" "$DEST/icons/spectre-vr.png" "$ICONS/"
gtk-update-icon-cache -q -t "${ICONS%/256x256/apps}" &>/dev/null || true

# -- the launch scripts -----------------------------------------------------
mkdir -p "$DEST/bin"
cat >"$DEST/bin/spectre" <<EOF
#!/bin/bash
# Spectre, from the desk.  Output goes to $LOGS/spectre.log.
mkdir -p "$LOGS"
cd "$DEST"
exec "$DEST/venv/bin/python" -u "$DEST/spectre.py" "\$@" \\
  >"$LOGS/spectre.log" 2>&1
EOF

cat >"$DEST/bin/spectre-vr" <<EOF
#!/bin/bash
# Spectre through a headset.  Wakes the WiVRn server if it is down and
# says to connect the Quest (the game plays on the desk until it joins);
# started from the WiVRn app inside the headset, it just goes.  Output
# goes to $LOGS/spectre-vr.log.
mkdir -p "$LOGS"
if systemctl --user list-unit-files wivrn.service &>/dev/null \\
    && ! systemctl --user is-active --quiet wivrn; then
  systemctl --user start wivrn || true
  notify-send -a Spectre -i spectre-vr \\
    "Spectre VR" "Start the WiVRn app on the Quest -- the game plays on
the desk until it joins." 2>/dev/null || true
fi
cd "$DEST"
exec "$DEST/venv/bin/python" -u "$DEST/spectre.py" --vr "\$@" \\
  >"$LOGS/spectre-vr.log" 2>&1
EOF
chmod +x "$DEST/bin/spectre" "$DEST/bin/spectre-vr"

# -- the launchers ----------------------------------------------------------
say "adding the launchers"
mkdir -p "$APPS"
cat >"$APPS/spectre.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Spectre
Comment=The wireframe tank arena
Exec=$DEST/bin/spectre
Path=$DEST
Icon=spectre
Terminal=false
StartupNotify=true
StartupWMClass=spectre
Categories=Game;ActionGame;
Keywords=tank;wireframe;arena;
EOF

cat >"$APPS/spectre-vr.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Spectre VR
Comment=The wireframe tank arena, in a headset
Exec=$DEST/bin/spectre-vr
Path=$DEST
Icon=spectre-vr
Terminal=false
StartupNotify=true
StartupWMClass=spectre
Categories=Game;ActionGame;X-WiVRn-VR;
Keywords=tank;wireframe;arena;vr;quest;openxr;
EOF
chmod +x "$APPS/spectre.desktop" "$APPS/spectre-vr.desktop"
refresh_launcher

say "installed.  Find Spectre and Spectre VR in the launcher" \
    "(Super + Space)."
