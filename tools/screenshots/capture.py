"""Automated screenshot documentation for the RaceLink Host WebUI.

Launches the standalone Host in simulation mode (``RACELINK_SIM=1`` — see
``racelink/integrations/standalone/_sim.py``), which seeds a deterministic demo
dataset (2 RF gateways + 2 RF networks + 1 Ethernet network, devices, groups,
RL presets and scenes) and attaches simulated transports so the full feature
surface renders without any hardware. A headless Chromium (Playwright) then
walks every page, dialog and close-up defined in ``shots.py`` and writes PNGs
with self-explanatory English names into ``docs/screenshots/`` (git-ignored,
never shipped in the wheel).

Change detection: a SHA-256 over the built UI bundle (``racelink/static/dist``)
is stored next to the screenshots. A re-run with no UI change is a no-op unless
``--force`` is passed — so refreshing the docs after a code change is cheap.

Usage (from the repo root, after ``cd frontend && npm run build``):

    py -3 tools/screenshots/capture.py            # capture iff UI changed
    py -3 tools/screenshots/capture.py --force    # always capture
    py -3 tools/screenshots/capture.py --headed   # watch it run

Prerequisites: ``pip install -r tools/screenshots/requirements.txt`` and
``playwright install chromium`` (one-time).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = REPO_ROOT / "racelink" / "static" / "dist"
OUT_DIR = REPO_ROOT / "docs" / "screenshots"
HASH_FILE = OUT_DIR / "ui_build.hash"
MANIFEST_FILE = OUT_DIR / "manifest.json"


# ---- UI build hashing -----------------------------------------------------

def compute_ui_hash() -> str:
    """SHA-256 over the built UI bundle (path + bytes), order-independent.

    Hashes every file under ``racelink/static/dist`` except ``.map`` sidecars
    (excluded from the wheel too, and noisy) so any source change that Vite
    rebuilds flips the hash.
    """
    if not DIST_DIR.is_dir():
        raise SystemExit(
            f"UI bundle not found at {DIST_DIR}.\n"
            f"Build it first:  cd frontend && npm run build"
        )
    h = hashlib.sha256()
    files = sorted(
        p for p in DIST_DIR.rglob("*")
        if p.is_file() and p.suffix != ".map"
    )
    for p in files:
        h.update(p.relative_to(DIST_DIR).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def read_stored_hash() -> str | None:
    try:
        return HASH_FILE.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


# ---- standalone server lifecycle ------------------------------------------

def _free_port(preferred: int = 5077) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


class StandaloneServer:
    """Run ``racelink-standalone`` in sim mode in an isolated home dir."""

    def __init__(self, port: int):
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}/racelink"
        self._home = Path(tempfile.mkdtemp(prefix="rl-screenshots-home-"))
        self._proc: subprocess.Popen | None = None
        self._log = open(self._home / "server.log", "w", encoding="utf-8")

    def __enter__(self) -> "StandaloneServer":
        # Seed an isolated standalone config so we control the port and never
        # touch the operator's real ~/.racelink.
        cfgdir = self._home / ".racelink"
        cfgdir.mkdir(parents=True, exist_ok=True)
        (cfgdir / "rl_standalone_config.json").write_text(
            json.dumps({"host": "127.0.0.1", "port": self.port,
                        "debug": False, "options": {}}),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["RACELINK_SIM"] = "1"
        env["RACELINK_LOG_LEVEL"] = "WARNING"
        env["HOME"] = str(self._home)
        env["USERPROFILE"] = str(self._home)
        env["PYTHONUNBUFFERED"] = "1"
        self._proc = subprocess.Popen(
            [sys.executable, "-c",
             "from racelink.integrations.standalone.webapp import run_standalone;"
             " run_standalone()"],
            cwd=str(REPO_ROOT), env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        self._wait_ready()
        return self

    def _wait_ready(self, timeout_s: float = 40.0) -> None:
        deadline = time.time() + timeout_s
        last_err: Exception | None = None
        while time.time() < deadline:
            if self._proc and self._proc.poll() is not None:
                raise SystemExit(
                    f"server exited early (code {self._proc.returncode}); "
                    f"see {self._home / 'server.log'}"
                )
            try:
                with urllib.request.urlopen(self.base_url, timeout=2) as r:
                    if r.status == 200:
                        return
            except Exception as e:  # noqa: BLE001
                last_err = e
            time.sleep(0.4)
        raise SystemExit(f"server did not become ready: {last_err}")

    def __exit__(self, *exc) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        try:
            self._log.close()
        except Exception:
            pass
        shutil.rmtree(self._home, ignore_errors=True)


# ---- capture context (the API shots.py drives) ----------------------------

class Capturer:
    """Thin helper around a Playwright page for declarative capture in shots.py."""

    def __init__(self, page, base_url: str, out_dir: Path):
        self.page = page
        self.base_url = base_url
        self.out_dir = out_dir
        self.shots: list[dict] = []

    # viewport -------------------------------------------------------------
    def set_viewport(self, width: int, height: int) -> None:
        """Resize the viewport to match a page's content shape.

        The app shell fills the viewport (internal scroll regions), so a
        full-page screenshot is exactly the viewport — picking a per-page
        aspect ratio keeps full-width/height elements from leaving large
        empty panels. Dialogs are captured as elements, so they are
        unaffected by viewport height.
        """
        self.page.set_viewport_size({"width": width, "height": height})
        self.page.wait_for_timeout(200)

    # navigation -----------------------------------------------------------
    def goto(self, route: str = "/") -> None:
        # The WebUI holds an SSE stream open, so "networkidle" never fires —
        # wait for the DOM + a fixed settle for the SPA to hydrate and fetch.
        url = self.base_url + ("" if route == "/" else route)
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1200)

    def wait(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    # interaction ----------------------------------------------------------
    def click_text(self, text: str) -> None:
        self.page.get_by_role("button", name=text, exact=False).first.click()
        self.page.wait_for_timeout(350)

    def click_label(self, label: str) -> None:
        self.page.get_by_label(label, exact=False).first.click()
        self.page.wait_for_timeout(350)

    def click_title(self, title: str) -> None:
        self.page.locator(f'[title="{title}"]').first.click()
        self.page.wait_for_timeout(350)

    def click_selector(self, selector: str) -> None:
        self.page.locator(selector).first.click()
        self.page.wait_for_timeout(350)

    def press_escape(self) -> None:
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(250)

    # screenshots ----------------------------------------------------------
    def _save(self, name: str, desc: str, png: bytes) -> None:
        path = self.out_dir / f"{name}.png"
        path.write_bytes(png)
        self.shots.append({"name": name, "file": path.name, "description": desc})
        print(f"  captured {path.name}")

    def shot_page(self, name: str, desc: str) -> None:
        self._save(name, desc, self.page.screenshot(full_page=True))

    def shot_viewport(self, name: str, desc: str) -> None:
        self._save(name, desc, self.page.screenshot(full_page=False))

    def shot_selector(self, name: str, desc: str, selector: str, nth: int = 0) -> None:
        loc = self.page.locator(selector).nth(nth)
        loc.scroll_into_view_if_needed()
        self.page.wait_for_timeout(150)
        self._save(name, desc, loc.screenshot())

    def autosize(self, loc, *, pad: int = 28, min_h: int = 360,
                 max_h: int = 4500, iters: int = 6) -> None:
        """Set the viewport height so a scroll container shows all its content.

        This is the per-screenshot "intelligence": ``loc`` is the element that
        actually scrolls (a dialog's ``max-h-[90vh]`` body, the device-table
        pane, a sidebar list, the scene-action list, …). We compute the
        vertical overhead that is *not* this scroller
        (``viewport - clientHeight`` — i.e. chrome, the element's own offset,
        sibling footers) and set::

            viewport_height = overhead + scrollHeight + pad

        so the scroller renders its full ``scrollHeight`` with neither
        clipping (cut-off dialogs) nor a large empty panel. It both grows and
        shrinks, and iterates because for viewport-proportional caps
        (``90vh``) the overhead itself moves with the viewport. Width is left
        untouched (element screenshots already crop horizontally).
        """
        for _ in range(iters):
            m = loc.evaluate("el => ({sh: el.scrollHeight, ch: el.clientHeight})")
            vp = self.page.viewport_size
            overhead = vp["height"] - m["ch"]
            desired = max(min_h, min(max_h, overhead + m["sh"] + pad))
            if abs(desired - vp["height"]) <= 3:
                return
            self.page.set_viewport_size({"width": vp["width"], "height": desired})
            self.page.wait_for_timeout(200)

    def autosize_selector(self, selector: str, **kw) -> None:
        self.autosize(self.page.locator(selector).first, **kw)

    def shot_region_to(self, name: str, desc: str, container_sel: str,
                       bottom_sel: str, pad: int = 12) -> None:
        """Screenshot ``container_sel`` clipped from its top to the bottom of
        ``bottom_sel``.

        For panels that stretch to full height (a sidebar whose grid column is
        as tall as the taller table column next to it), an element screenshot
        would include a large empty area below the real content. Clipping to
        the last meaningful child gives a tight, content-fitted image.
        """
        cont = self.page.locator(container_sel).first
        cont.scroll_into_view_if_needed()
        cbox = cont.bounding_box()
        bbox = self.page.locator(bottom_sel).last.bounding_box()
        if not cbox or not bbox:
            self._save(name, desc, cont.screenshot())
            return
        height = (bbox["y"] + bbox["height"]) - cbox["y"] + pad
        clip = {"x": cbox["x"], "y": cbox["y"], "width": cbox["width"],
                "height": max(40.0, height)}
        self._save(name, desc, self.page.screenshot(clip=clip))

    def shot_dialog(self, name: str, desc: str) -> None:
        """Screenshot the open Reka UI dialog (role=dialog), then close it.

        Auto-fits the viewport first so a tall dialog isn't clipped by its
        own ``max-h-[90vh]`` scroll cap.
        """
        loc = self.page.get_by_role("dialog").first
        loc.wait_for(state="visible", timeout=4000)
        self.page.wait_for_timeout(500)  # let async content (release lists…) load
        self.autosize(loc)
        self.page.wait_for_timeout(150)
        self._save(name, desc, loc.screenshot())
        self.press_escape()


# ---- main -----------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="capture even when the UI bundle is unchanged")
    ap.add_argument("--headed", action="store_true",
                    help="run the browser headed (visible) for debugging")
    args = ap.parse_args()

    current_hash = compute_ui_hash()
    stored_hash = read_stored_hash()
    have_output = OUT_DIR.is_dir() and any(OUT_DIR.glob("*.png"))
    if not args.force and stored_hash == current_hash and have_output:
        print(f"UI unchanged (hash {current_hash[:12]}…) — skipping. Use --force to override.")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clear stale PNGs so removed shots don't linger.
    for old in OUT_DIR.glob("*.png"):
        old.unlink()

    from playwright.sync_api import sync_playwright  # imported here so --help
    import shots                                      # works without playwright

    port = _free_port()
    print(f"Launching standalone (sim mode) on 127.0.0.1:{port} …")
    with StandaloneServer(port) as server, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1600, "height": 900},
                                device_scale_factor=2)
        cap = Capturer(page, server.base_url, OUT_DIR)
        print("Capturing screenshots …")
        shots.capture_all(cap)
        browser.close()

    HASH_FILE.write_text(current_hash + "\n", encoding="utf-8")
    MANIFEST_FILE.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "ui_build_hash": current_hash,
        "shot_count": len(cap.shots),
        "shots": cap.shots,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"\nDone — {len(cap.shots)} screenshots in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
