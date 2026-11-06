"""Colin's PC: runs keeper.ps1 and shows it in the Windows notification area, so nothing runs
unseen. Started at sign-in by the Startup folder (runbook, "PC build slots"):
    uvw run --no-project --with pystray --with pillow D:\\kaizen-ci\\keeper-tray.pyw
"""

import json
import subprocess
import threading
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

ROOT = Path(r"D:\kaizen-ci")
LOG = ROOT / "keeper.log"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flashes up
COLOURS = {"busy": "#3fb950", "paused": "#d29922", "off": "#8b949e", "down": "#f85149"}


def quiet(*args: str) -> str:
    done = subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW)
    return done.stdout


def start_keeper() -> subprocess.Popen:
    keeper = ["pwsh", "-NoProfile", "-WindowStyle", "Hidden", "-File", str(ROOT / "keeper.ps1")]
    return subprocess.Popen(keeper, creationflags=NO_WINDOW)


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
    keeper = start_keeper()
    icon = pystray.Icon("kaizen-ci", dot(COLOURS["off"]), "Kaizen PC: starting")
    stopping = threading.Event()

    def watch() -> None:
        nonlocal keeper
        while not stopping.is_set():
            if keeper.poll() is not None:  # a crash must not leave the PC unwatched while it games
                state, text = "down", "keeper stopped, starting it again"
                keeper = start_keeper()
            else:
                status = kaizen_status()
                state = "paused" if status == "‖" else "busy" if status else "off"
                said = {"paused": "paused while you game", "off": "off"}
                text = said.get(state, f"{status} runners busy")
            icon.icon, icon.title = dot(COLOURS[state]), f"Kaizen PC: {text}"
            stopping.wait(10)

    def quit_tray() -> None:
        stopping.set()
        keeper.terminate()
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem("2 slots", lambda: set_slots("2")),
        pystray.MenuItem("6 slots", lambda: set_slots("6")),
        pystray.MenuItem("Off (after running jobs)", lambda: set_slots("off")),
        pystray.MenuItem("Open log", lambda: quiet("cmd", "/c", "start", "", str(LOG))),
        pystray.MenuItem("Quit keeper", quit_tray),
    )
    threading.Thread(target=watch, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
