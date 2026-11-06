"""Colin's PC: runs keeper.ps1 and shows it in the Windows notification area, so nothing runs
unseen. Started at sign-in from the Startup folder (runbook, "PC build slots", has the command).
"""

import ctypes
import json
import os
import subprocess
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

ROOT = Path(r"D:\kaizen-ci")
LOG = ROOT / "keeper.log"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flashes up
ALREADY_EXISTS = 183
COLOURS = {"busy": "#3fb950", "paused": "#d29922", "off": "#8b949e", "down": "#f85149"}
SAID = {"paused": "paused while you game", "off": "off", "down": "keeper stopped, restarting it"}


def quiet(*args: str) -> str:
    """A command's output, "" if it fails or takes over 20 s (the watch loop must not stall)."""
    try:
        done = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    except OSError, subprocess.TimeoutExpired:
        return ""
    return done.stdout


def start_keeper() -> subprocess.Popen | None:
    keeper = ["pwsh", "-NoProfile", "-WindowStyle", "Hidden", "-File", str(ROOT / "keeper.ps1")]
    try:
        return subprocess.Popen(keeper, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except OSError:
        return None


def stop_keeper(keeper: subprocess.Popen | None) -> None:
    """The keeper and the ssh, gh and lease calls it has running."""
    if keeper and keeper.poll() is None:
        quiet("taskkill", "/T", "/F", "/PID", str(keeper.pid))


def kaizen_status() -> str:
    """The keeper's status on its lease: "1/2" busy/online, "‖" paused, "" when it holds none."""
    try:
        holds = json.loads(quiet("cmd", "/c", "lease", "show", "--json"))["view"]["holds"]
    except ValueError, KeyError, TypeError:
        return ""
    return next((str(h.get("status") or "") for h in holds if h.get("name") == "kaizen-ci"), "")


def dot(colour: str) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((8, 8, 56, 56), fill=colour)
    return image


def set_slots(slots: str) -> None:
    quiet("pwsh", "-NoProfile", "-File", str(ROOT / "kaizen-pc.ps1"), slots)


def main() -> None:
    # One tray, so one keeper: two would fight over the slots and the lease.
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\kaizen-ci-keeper-tray")
    if ctypes.windll.kernel32.GetLastError() == ALREADY_EXISTS:
        return
    lock = threading.Lock()
    stopping = threading.Event()
    keeper = start_keeper()
    icon = pystray.Icon("kaizen-ci", dot(COLOURS["off"]), "Kaizen PC: starting")

    def watch() -> None:
        nonlocal keeper
        shown, wait = "", 10.0
        while not stopping.wait(wait):
            with lock:
                if stopping.is_set():
                    return
                if keeper is None or keeper.poll() is not None:
                    state, text = "down", SAID["down"]
                    keeper = start_keeper()
                    wait = min(wait * 2, 300)  # one that dies at once is not restarted every 10 s
                else:
                    status = kaizen_status()
                    state = "paused" if status == "‖" else "busy" if status else "off"
                    text, wait = SAID.get(state, f"{status} runners busy"), 10.0
            if text != shown:
                icon.icon, icon.title, shown = dot(COLOURS[state]), f"Kaizen PC: {text}", text

    def quit_tray() -> None:
        with lock:
            stopping.set()
            stop_keeper(keeper)
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem("2 slots", lambda: set_slots("2")),
        pystray.MenuItem("6 slots", lambda: set_slots("6")),
        pystray.MenuItem("Off (after running jobs)", lambda: set_slots("off")),
        pystray.MenuItem("Open log", lambda: os.startfile(LOG)),
        pystray.MenuItem("Quit", quit_tray),
    )
    threading.Thread(target=watch, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
