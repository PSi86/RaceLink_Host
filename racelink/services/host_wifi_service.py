"""Host-side WiFi operations driving NetworkManager via ``nmcli``.

Used by the OTA workflow to bring the host's WiFi up, connect to
the WLED node's AP, and restore the previous state when done.
Pure subprocess wrapper — the actual interface manipulation is
delegated to ``nmcli`` because it's the only cross-distro tool
we can rely on (works on Raspberry Pi OS / Ubuntu / Debian and
its variants).

Public API:

* ``wifi_interfaces()`` — enumerate available wireless interfaces.
* ``radio_enabled()`` — is the radio on?
* ``set_radio(on: bool)`` — turn the radio on/off.
* ``connect_ap(ssids, password, *, iface, bssid, timeout_s)`` —
  scan for any of the candidate SSIDs and connect via
  ``nmcli dev wifi connect`` with the supplied PSK. Returns the
  matched SSID. NM creates one persistent profile per distinct
  SSID and reuses it on subsequent calls; we deliberately don't
  delete it post-OTA (the secrets and SSID are static, so the
  profile is reused identically by the next run).
* ``disconnect_ap(ssid, timeout_s=...)`` — bring the named
  connection profile down so the operator's normal WiFi
  auto-reconnects after the OTA.
* ``rescan(iface)`` / ``wait_iface_ready(iface, timeout_s)`` —
  helpers used by the connect path.

Threading: blocking subprocess calls. Always invoked from a
task-manager worker thread so the OTA workflow can wait without
blocking the web request thread.

Permissions: ``nmcli`` requires either root, membership of an
appropriate wheel/netdev-style group, or a polkit rule that
authorises the running user. ``scripts/setup_nmcli_polkit.sh``
takes care of this on a fresh Linux install — see that script
plus ``docs/standalone.md`` for the operator-side setup.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Iterable, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)


SsidArg = Union[str, Sequence[str]]


def _setup_command_hint() -> str:
    """Return the exact, copy-pasteable command an operator should run
    to fix the polkit denial — including the absolute path to the
    bundled console script.

    Bare ``sudo racelink-setup-nmcli`` fails when the host is installed
    in a venv (the typical pip / piwheel layout used by the
    RotorHazard plugin) because ``sudo``'s default ``secure_path`` does
    not include the venv's ``bin/`` directory. The error message we
    surface to the operator therefore embeds the absolute path the
    running process resolves to. If the script is not on disk for some
    reason we fall back to invoking the module via the venv's Python
    so the command still works.
    """
    script_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates = [
        os.path.join(script_dir, "racelink-setup-nmcli"),
        os.path.join(script_dir, "racelink-setup-nmcli.exe"),  # Windows
    ]
    for path in candidates:
        if os.path.isfile(path):
            return f"sudo {path}"
    # Console script not in the venv's bin/. Invoke the module directly
    # via the same Python interpreter so we still hit the right install.
    return f"sudo {sys.executable} -m racelink.tools.setup_nmcli_polkit"


class HostWifiService:
    """Reusable host WiFi helpers independent of Flask routes."""

    def wifi_interfaces(self) -> List[str]:
        base = "/sys/class/net"
        interfaces: List[str] = []
        try:
            for name in os.listdir(base):
                if name.startswith("."):
                    continue
                if os.path.isdir(os.path.join(base, name, "wireless")):
                    interfaces.append(name)
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            interfaces = []
        if not interfaces:
            try:
                interfaces = [name for name in os.listdir(base) if not name.startswith(".")]
            except Exception:
                # swallow-ok: best-effort fallback; caller proceeds with safe default
                interfaces = []
        return sorted(set(interfaces))

    def nmcli_run(self, args: list, timeout_s: float = 20.0) -> subprocess.CompletedProcess:
        if not shutil.which("nmcli"):
            raise RuntimeError("nmcli not available on host (cannot switch WiFi automatically)")
        return subprocess.run(["nmcli"] + args, capture_output=True, text=True, timeout=max(1.0, timeout_s))

    def radio_enabled(self) -> bool:
        try:
            proc = self.nmcli_run(["-t", "-f", "WIFI", "radio"], timeout_s=6.0)
            if proc.returncode != 0:
                raw = (proc.stderr or proc.stdout or "").lower()
                return "enabled" in raw and "disabled" not in raw
            return (proc.stdout or "").strip().lower() == "enabled"
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return False

    def set_radio(self, enabled: bool) -> None:
        onoff = "on" if enabled else "off"
        proc = self.nmcli_run(["radio", "wifi", onoff], timeout_s=12.0)
        if proc.returncode != 0:
            out = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"nmcli radio wifi {onoff} failed ({proc.returncode}): {out}")

    def wait_iface_ready(self, iface: str, timeout_s: float = 12.0) -> None:
        iface = (iface or "wlan0").strip()
        deadline = time.time() + max(1.0, float(timeout_s))
        last_state = None
        while time.time() < deadline:
            proc = self.nmcli_run(["-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"], timeout_s=6.0)
            if proc.returncode == 0:
                for line in (proc.stdout or "").splitlines():
                    parts = line.split(":")
                    if len(parts) >= 3 and parts[0] == iface and parts[1] == "wifi":
                        last_state = parts[2]
                        if last_state and last_state.lower() != "unavailable":
                            return
            time.sleep(0.4)
        raise RuntimeError(f"host WiFi iface '{iface}' not ready (state={last_state})")

    def rescan(self, iface: str) -> None:
        iface = (iface or "wlan0").strip()
        self.nmcli_run(["dev", "wifi", "rescan", "ifname", iface], timeout_s=10.0)

    def list_ssids(self, iface: str) -> list:
        iface = (iface or "wlan0").strip()
        proc = self.nmcli_run(["-t", "-f", "SSID", "dev", "wifi", "list", "ifname", iface, "--rescan", "no"], timeout_s=12.0)
        if proc.returncode != 0:
            return []
        return [line.strip() for line in (proc.stdout or "").splitlines() if (line or "").strip()]

    @staticmethod
    def _split_nmcli_terse_fields(line: str) -> List[str]:
        """Split one ``nmcli -t``-formatted line.

        nmcli -t uses ``:`` as field separator and escapes literal
        colons inside field values as ``\\:`` — relevant for BSSID
        fields like ``DC\\:B4\\:D9\\:A8\\:A9\\:5B``. We walk the
        string instead of using ``str.split(":")`` so the BSSID
        round-trips correctly.
        """
        fields: List[str] = []
        cur: list[str] = []
        i = 0
        line = line or ""
        while i < len(line):
            c = line[i]
            if c == "\\" and i + 1 < len(line) and line[i + 1] == ":":
                cur.append(":")
                i += 2
            elif c == ":":
                fields.append("".join(cur))
                cur = []
                i += 1
            else:
                cur.append(c)
                i += 1
        fields.append("".join(cur))
        return fields

    def list_aps_detailed(self, iface: str) -> List[dict]:
        """Structured scan view used by ``connect_ap``'s BSSID-cascade.

        Returns a list of ``{"in_use", "bssid", "ssid"}`` dicts for
        every AP currently in nmcli's scan cache for ``iface``. BSSIDs
        are normalised to upper-case so case-insensitive comparison
        against operator-supplied / predicted values is trivial.

        Used to detect "exactly one candidate-SSID AP visible whose
        BSSID is not the previous device" (the fallback path for nodes
        that don't follow the ESP32 ``AP_MAC = STA_MAC + 1`` convention).
        """
        iface = (iface or "wlan0").strip()
        try:
            proc = self.nmcli_run(
                ["-t", "-f", "IN-USE,BSSID,SSID", "dev", "wifi", "list",
                 "ifname", iface, "--rescan", "no"],
                timeout_s=8.0,
            )
        except Exception:
            # swallow-ok: caller treats empty list as "nothing visible"
            return []
        if proc.returncode != 0:
            return []
        aps: List[dict] = []
        for raw in (proc.stdout or "").splitlines():
            if not raw.strip():
                continue
            fields = self._split_nmcli_terse_fields(raw)
            if len(fields) < 3:
                continue
            in_use, bssid, ssid = fields[0], fields[1], fields[2]
            if not bssid:
                # Hidden / cloaked APs report empty BSSID — useless
                # for BSSID-based targeting.
                continue
            aps.append({
                "in_use": in_use.strip() == "*",
                "bssid": bssid.upper(),
                "ssid": ssid,
            })
        return aps

    def active_bssid(self, iface: str) -> str:
        """BSSID currently associated on ``iface`` (or ``""``).

        Used by the OTA workflow after a successful ``connect_ap`` to
        record which BSSID to *avoid* on the next iteration's connect.
        """
        for ap in self.list_aps_detailed(iface):
            if ap.get("in_use"):
                return str(ap.get("bssid") or "")
        return ""

    def wifi_state_snapshot(self, iface: str, candidates: Optional[Sequence[str]] = None) -> str:
        """Compact diagnostic snapshot of ``iface``'s NM state and
        visible APs matching the ``candidates`` SSID list.

        Best-effort: any nmcli failure falls through to a placeholder
        string so the caller's error-path log line is never dropped.
        Used by :meth:`connect_ap` around the connect attempt so a
        failed run leaves enough info to tell apart NM-state drift,
        AP-bringup race, and a misbehaving hostapd on the device.

        Format is intentionally one short line per nmcli call so the
        log entry stays grep-friendly:
        ``dev=<wlan0:state:connection> aps=[<rows matching candidate SSIDs>]``.
        """
        iface = (iface or "wlan0").strip()
        parts: list[str] = []
        try:
            proc = self.nmcli_run(
                ["-t", "-f", "DEVICE,STATE,CONNECTION", "dev"],
                timeout_s=4.0,
            )
            line = next(
                (l for l in (proc.stdout or "").splitlines() if l.startswith(f"{iface}:")),
                f"{iface}:<unknown>",
            )
            parts.append(f"dev={line}")
        except Exception as ex:
            # swallow-ok: diagnostic snapshot, never the source of truth
            parts.append(f"dev=<error:{type(ex).__name__}>")
        try:
            # Non-terse format keeps BSSIDs / signal columns human-
            # readable. ``--rescan no`` reuses the most recent scan so
            # this reflects what the connect call would have seen.
            proc = self.nmcli_run(
                ["-f", "IN-USE,BSSID,SSID,SIGNAL,CHAN", "dev", "wifi", "list",
                 "ifname", iface, "--rescan", "no"],
                timeout_s=4.0,
            )
            cand = {str(c) for c in (candidates or []) if c}
            lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
            relevant = [l for l in lines if any(c in l for c in cand)] if cand else lines[:6]
            parts.append(f"aps={relevant}")
        except Exception as ex:
            # swallow-ok: diagnostic snapshot, never the source of truth
            parts.append(f"aps=<error:{type(ex).__name__}>")
        return " ".join(parts)

    @staticmethod
    def _coerce_ssid_list(ssids: SsidArg) -> List[str]:
        if isinstance(ssids, str):
            items: Iterable[str] = [ssids]
        else:
            items = ssids or []
        out = [str(s).strip() for s in items if str(s).strip()]
        return out

    def connect_ap(
        self,
        ssids: SsidArg,
        password: str,
        *,
        iface: str = "",
        bssid: str = "",
        avoid_bssid: str = "",
        timeout_s: float = 35.0,
    ) -> str:
        """Connect to the first visible SSID from ``ssids`` using ``password``.

        ``nmcli dev wifi connect`` creates (or reuses) one persistent NM
        profile per distinct SSID. The profile is keyed on the SSID — so
        connecting to ten different WLED nodes that all broadcast
        ``WLED_RaceLink_AP`` produces exactly one profile entry, updated
        in place when the password changes. We deliberately don't delete
        the profile after the OTA: it would just churn the NM
        configuration and force a re-authorisation on the next run.

        BSSID selection cascade (per scan iteration):

        1. ``bssid`` hint provided AND that BSSID is in the scan → use it.
           This is the primary path when the OTA workflow predicts the
           target's SoftAP MAC from the device's STA MAC.
        2. ``avoid_bssid`` provided AND exactly one candidate-SSID AP is
           visible whose BSSID is *not* ``avoid_bssid`` → use that BSSID.
           Multi-device fallback for nodes that don't follow the ESP32
           ``AP_MAC = STA_MAC + 1`` default — picks the new device's AP
           when there's no ambiguity. Disabled when ``avoid_bssid`` is
           empty (i.e. first device of a run) because there's no
           discriminator there.
        3. Neither hint nor avoid set → connect with ``bssid=<auto>`` and
           let NM pick the strongest matching SSID (legacy behavior).
        4. Otherwise (e.g. hint set but not visible and we have an avoid):
           skip this iteration and rescan.

        Returns the SSID we actually connected to. Raises ``RuntimeError``
        on timeout or auth failure.
        """
        candidates = self._coerce_ssid_list(ssids)
        if not candidates:
            raise RuntimeError("no candidate SSIDs supplied")
        if not password:
            raise RuntimeError("AP password missing")
        iface = str(iface or "wlan0").strip()
        bssid = str(bssid or "").strip().upper()
        avoid_bssid = str(avoid_bssid or "").strip().upper()
        candidate_set = set(candidates)

        self.wait_iface_ready(iface, timeout_s=12.0)
        # Pre-emptive disconnect: if ``iface`` is still actively bound
        # to one of our candidate SSIDs from a previous iteration (or
        # an aborted run), drop that connection BEFORE the scan-loop
        # starts. Two effects:
        #   1. Unblocks NM's scan-throttle so subsequent ``rescan``
        #      calls actually surface new BSSIDs quickly.
        #   2. Makes the eventual ``_dev_wifi_connect`` start from a
        #      clean state — no stale BSSID-affinity that NM could
        #      cling to (the neu 94.txt "Secrets were required"
        #      failure mode).
        # Idempotent per SSID — no-op if iface is not connected to it.
        for cand in candidates:
            self._disconnect_iface_from_ssid(iface, cand)
        # Stale-BSSID filter for the multi-device fallback (cascade 2):
        # NM keeps a previously seen AP in its scan cache for ~30 s
        # after it stops responding (neu 97.txt: device 1's BSSID still
        # at signal 75 long after that device rebooted out of AP mode).
        # If we used "any single non-avoid BSSID" as the fallback rule,
        # we'd pick that stale entry over the still-not-yet-visible
        # BSSID of the current device. Capture the snapshot at start
        # so the fallback can require "BSSID *appeared after* we
        # started looking" — which is the reliable signal for "the
        # current device's AP came up just now".
        initial_bssids = {
            ap.get("bssid", "")
            for ap in self.list_aps_detailed(iface)
            if ap.get("bssid")
        }
        # Pre-connect state snapshot so a failed run captures whether
        # NM was already mid-transition (carrying state from a previous
        # OTA iteration) or whether the device's AP simply wasn't
        # visible yet. Cheap; only runs once per call.
        logger.info(
            "AP connect_ap: start candidates=%s iface=%s bssid=%s avoid_bssid=%s timeout_s=%.1fs initial_bssids=%s | %s",
            candidates, iface, bssid or "<auto>", avoid_bssid or "<none>", float(timeout_s),
            sorted(initial_bssids) if initial_bssids else "[]",
            self.wifi_state_snapshot(iface, candidates),
        )
        deadline = time.time() + max(5.0, float(timeout_s))
        last_err = None
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                self.rescan(iface)
            except Exception:
                # swallow-ok: scan failures retry on the next loop iteration
                pass

            aps = self.list_aps_detailed(iface)
            cand_aps = [ap for ap in aps if ap.get("ssid") in candidate_set]

            chosen_bssid = ""
            matched = ""

            # (1) primary: predicted-BSSID match
            if bssid:
                for ap in cand_aps:
                    if ap.get("bssid") == bssid:
                        chosen_bssid = bssid
                        matched = ap.get("ssid") or ""
                        break

            # (2) multi-device fallback: exactly one candidate AP that
            #     is BOTH not the previous device's BSSID AND wasn't
            #     in the scan cache when we started looking. The
            #     "freshly appeared" filter excludes stale entries
            #     (previously-flashed devices whose APs are long down
            #     but still in NM's cache). Skipped on the first device
            #     (avoid_bssid empty) — no discriminator there.
            if not matched and avoid_bssid:
                fresh_non_avoid = [
                    ap for ap in cand_aps
                    if ap.get("bssid") != avoid_bssid
                    and ap.get("bssid") not in initial_bssids
                ]
                if len(fresh_non_avoid) == 1:
                    chosen_bssid = fresh_non_avoid[0].get("bssid") or ""
                    matched = fresh_non_avoid[0].get("ssid") or ""
                    logger.info(
                        "AP connect_ap: attempt %d predicted bssid not visible; "
                        "fallback to freshly appeared non-avoid candidate bssid=%s",
                        attempt, chosen_bssid,
                    )

            # (3) legacy: no hint, no avoid → first candidate, no bssid lock
            if not matched and not bssid and not avoid_bssid:
                matched_ssid = next((s for s in candidates if any(
                    ap.get("ssid") == s for ap in cand_aps)), "")
                if matched_ssid:
                    matched = matched_ssid
                    chosen_bssid = ""  # let nmcli auto-pick

            if not matched:
                # DEBUG (not INFO) — busy-loop until something matches;
                # log full scan so a final timeout has enough context.
                logger.debug(
                    "AP connect_ap: attempt %d no usable candidate (cand_aps=%s, avoid=%s, hint=%s)",
                    attempt,
                    [(ap.get("bssid"), ap.get("ssid")) for ap in cand_aps],
                    avoid_bssid or "<none>", bssid or "<auto>",
                )
                # 1.5 s rhythm: NM internally throttles ``rescan`` to
                # roughly every 5–15 s anyway, so polling faster just
                # burns CPU and log lines without surfacing new APs
                # any sooner. 1.5 s matches a typical active-scan
                # round-trip on a single band; faster (0.7 s, observed
                # in neu 96.txt as 13 attempts in 11 s) only produces
                # noise.
                time.sleep(1.5)
                continue

            t_attempt = time.monotonic()
            logger.info(
                "AP connect_ap: attempt %d invoking nmcli connect ssid=%r bssid=%s",
                attempt, matched, chosen_bssid or "<auto>",
            )
            try:
                self._dev_wifi_connect(matched, password, iface=iface, bssid=chosen_bssid,
                                       timeout_s=min(60.0, max(15.0, float(timeout_s))))
                logger.info(
                    "AP connect_ap: attempt %d ssid=%r SUCCESS elapsed=%.2fs",
                    attempt, matched, time.monotonic() - t_attempt,
                )
                return matched
            except Exception as ex:
                attempt_elapsed = time.monotonic() - t_attempt
                last_err = str(ex)
                # WARNING: every failed attempt gets one line so a
                # repro-after-the-fact can correlate timestamps with
                # the rest of the OTA log. The snapshot is the actual
                # diagnostic payload — it shows what NM saw AT THE
                # MOMENT the attempt failed (rescan=no, no extra delay).
                logger.warning(
                    "AP connect_ap: attempt %d ssid=%r FAILED elapsed=%.2fs: %s | %s",
                    attempt, matched, attempt_elapsed, last_err,
                    self.wifi_state_snapshot(iface, candidates),
                )
                if "Wi-Fi is disabled" in last_err or "wireless is disabled" in last_err.lower():
                    raise RuntimeError(last_err)
                if "Secrets were required" in last_err or "no secrets provided" in last_err.lower():
                    # NM raises this whenever the PSK auth handshake is
                    # rejected. Two practical causes the operator should
                    # know about: actually-wrong password (config issue)
                    # OR ESP hostapd briefly rate-limiting the host MAC
                    # after a few failed attempts (transient). Naming
                    # both keeps a tired race-day operator from ripping
                    # the firmware open hunting for a wrong-password bug
                    # when the real fix is a 30-second wait.
                    raise RuntimeError(
                        f"AP {matched!r}: authentication failed (Secrets rejected). "
                        "Likely causes: the AP password is wrong, OR the device's "
                        "hostapd is briefly rate-limiting the host after recent "
                        "failed attempts — wait ~30 s and retry. "
                        f"Raw nmcli output: {last_err}"
                    )
                # polkit denial is deterministic — re-trying produces the
                # same denial. Re-raise immediately so the operator sees
                # the actionable hint without waiting for the outer
                # ``timeout_s`` budget.
                if "racelink-setup-nmcli" in last_err:
                    raise
                # Other transient errors — retry next loop tick.
                time.sleep(0.9)

        if last_err:
            raise RuntimeError(
                f"could not connect to any of {candidates}: {last_err}"
            )
        raise RuntimeError(
            f"timeout waiting for one of {candidates} to appear on {iface}"
        )

    def disconnect_iface_fast(self, iface: str) -> None:
        """Tell NM to disconnect ``iface`` and return without waiting
        for the 802.11 deactivation handshake to finish.

        Used post-OTA-upload: the WLED device is about to reboot, so
        there is no peer for a graceful 4-way disconnect anyway. The
        ``-w 0`` top-level flag tells nmcli to return as soon as NM has
        accepted the request, instead of blocking the operator-facing
        workflow on the full deactivation timeout.

        Why this matters at the OTA layer: leaving the host in
        ``connected:WLED_RaceLink_AP`` while the device reboots makes
        NM throttle subsequent rescans (it thinks it's already in a
        good state). The next iteration's ``connect_ap`` then spends
        ~10 s burning attempts before the new device's BSSID appears
        in the scan cache (observed in neu 96.txt as 13 vs 6 attempts
        between first and follow-up devices). Forcing a disconnect
        right after the upload moves NM into the "actively looking"
        state so its background scan refreshes faster.

        Best-effort: failures are swallowed because the workflow's
        success path doesn't depend on it.
        """
        if not iface:
            return
        try:
            # ``-w 0`` is a top-level nmcli option (it precedes the
            # subcommand) that disables waiting for the operation to
            # finish — important for "device is about to reboot, won't
            # complete the disconnect handshake".
            self.nmcli_run(["-w", "0", "dev", "disconnect", iface], timeout_s=4.0)
            logger.info("Host WiFi: disconnect (-w 0) issued on %s", iface)
        except Exception as ex:
            # swallow-ok: best-effort post-upload cleanup. Logged at
            # DEBUG so a chronic NM hiccup is still diagnosable.
            logger.debug("disconnect_iface_fast(%s) failed: %s", iface, ex)

    def _disconnect_iface_from_ssid(self, iface: str, ssid: str) -> None:
        """Drop a stale active connection to ``ssid`` on ``iface`` so a
        fresh ``dev wifi connect`` doesn't re-bind to the previous BSSID.

        Background: in a multi-device OTA every WLED node broadcasts the
        same SSID. After device N finishes, NM is left actively bound to
        device N's BSSID (state ``connected:WLED_RaceLink_AP``). The next
        ``dev wifi connect`` for the same SSID tries to *re-use* that
        active connection rather than authenticating fresh against device
        N+1's BSSID — and if device N is in its AP-shutdown window the
        auth fails with ``"Secrets were required, but not provided"``
        (observed in neu 94.txt). Forcing a clean disconnect first
        removes the affinity.

        SSID-scoped on purpose: we don't want to drop the operator's
        regular WiFi when that WiFi happens to live on the same iface.
        """
        if not ssid:
            return
        try:
            proc = self.nmcli_run(
                ["-t", "-f", "DEVICE,STATE,CONNECTION", "dev"],
                timeout_s=4.0,
            )
        except Exception as ex:
            # swallow-ok: best-effort precondition; on failure we
            # proceed without pre-disconnect.
            logger.debug("pre-connect state check failed: %s", ex)
            return
        for line in (proc.stdout or "").splitlines():
            parts = line.split(":", 2)
            if (len(parts) == 3
                    and parts[0] == iface
                    and parts[1] == "connected"
                    and parts[2] == ssid):
                logger.info(
                    "AP %r: dropping stale active connection on %s before fresh connect",
                    ssid, iface,
                )
                try:
                    self.nmcli_run(["dev", "disconnect", iface], timeout_s=10.0)
                except Exception as ex:
                    # swallow-ok: best-effort cleanup; the subsequent
                    # connect may still succeed.
                    logger.debug("pre-connect disconnect failed: %s", ex)
                return

    def _delete_profile_if_exists(self, ssid: str) -> None:
        """Best-effort: delete the NM connection profile named ``ssid``.

        Called right before ``nmcli dev wifi connect`` to dodge a class
        of NM bugs where a stale profile from a prior OTA makes the
        next connect fail with::

            Error: 802-11-wireless-security.key-mgmt: property is missing.

        Reproduction: OTA device A → succeeds; OTA device B with the
        same SSID a moment later → NM tries to reuse the profile
        created for device A, fails to re-derive ``key-mgmt`` from the
        freshly-cached AP info, and aborts. Forcing a clean profile
        state on every connect makes this deterministic.

        End-state on disk is unchanged from before — exactly one
        profile per SSID, kept by ``nmcli dev wifi connect`` after a
        successful association. We just delete any pre-existing entry
        first instead of trying to reuse it.

        Tolerates the "unknown connection" return — safe no-op when no
        profile exists yet (first OTA on a fresh host).
        """
        proc = self.nmcli_run(
            ["con", "delete", "id", ssid],
            timeout_s=10.0,
        )
        if proc.returncode == 0:
            return
        err = (proc.stderr or proc.stdout or "").lower()
        if "unknown connection" in err or "not found" in err or "no such" in err:
            return
        # Anything else: don't raise. The subsequent connect may still
        # work (e.g. permission issues on ``con delete`` show up later
        # on ``dev wifi connect`` too, with a clearer message). We
        # don't want a delete-step hiccup to mask the real error.

    def _nmcli_connect_once(
        self,
        ssid: str,
        *,
        password: Optional[str],
        iface: str,
        bssid: str,
        wait_s: int,
    ) -> tuple:
        """Single ``nmcli dev wifi connect`` invocation.

        Returns ``(returncode, combined_stderr_stdout)``. Used by
        :meth:`_dev_wifi_connect` so the PSK and open-AP-fallback paths
        share one source of truth for the argv layout. ``password=None``
        omits the ``password`` argument entirely — the right call for
        an open AP (NM rejects ``password <X>`` if the BSS shows no
        security with ``key-mgmt: property is missing``).
        """
        args = [
            "--wait", str(wait_s),
            "dev", "wifi", "connect", ssid,
            "ifname", iface,
        ]
        if password:
            args += ["password", password]
        if bssid:
            args += ["bssid", bssid]
        proc = self.nmcli_run(args, timeout_s=max(15.0, min(70.0, float(wait_s) + 10.0)))
        out = (proc.stderr or proc.stdout or "").strip()
        return proc.returncode, out

    def _dev_wifi_connect(self, ssid: str, password: str, *, iface: str, bssid: str, timeout_s: float) -> None:
        """Run ``nmcli dev wifi connect <ssid> [password <pass>] ...``.

        ``nmcli`` honours ``--wait`` for both the scan and the activation;
        clamp it to a sensible band so a stuck device doesn't hold the
        worker thread for the full caller-supplied timeout (the outer
        retry loop in :meth:`connect_ap` re-issues with a fresh rescan).

        Open-AP fallback: if the PSK connect fails with ``key-mgmt:
        property is missing``, NM is telling us the AP advertises no
        security (open). We retry once without the password rather than
        bubble the error up — handles WLED nodes flashed with an empty
        AP password (the failure mode that broke a fleet OTA where some
        nodes used the default ``wled1234`` and others had cleared it).
        """
        # NOTE: the stale-active-connection cleanup (``_disconnect_iface_from_ssid``)
        # is now performed once at the top of ``connect_ap`` before the
        # scan-loop, so NM's scan-throttle is released earlier. No need
        # to repeat it here per attempt.
        # Pre-delete any stale profile for this SSID so NM creates a
        # fresh one with correct key-mgmt. See
        # ``_delete_profile_if_exists`` for the full failure-mode
        # context. Same on-disk end-state as before (one profile per
        # SSID), just always created fresh per OTA.
        self._delete_profile_if_exists(ssid)

        wait_s = int(max(10.0, min(60.0, float(timeout_s))))
        rc, out = self._nmcli_connect_once(
            ssid, password=password, iface=iface, bssid=bssid, wait_s=wait_s,
        )
        if rc == 0:
            return

        lower = out.lower()
        # Open-AP fallback. The exact string NM emits for a security-mode
        # mismatch is ``Error: 802-11-wireless-security.key-mgmt:
        # property is missing.`` Match on the two keywords so wording
        # variants across NM versions still hit the fallback.
        if "key-mgmt" in lower and "missing" in lower:
            logger.info(
                "AP %r: PSK connect failed with key-mgmt missing; "
                "retrying as open network (no PSK)", ssid,
            )
            # The failed PSK attempt may have left a partial profile —
            # purge it so the open retry creates fresh.
            self._delete_profile_if_exists(ssid)
            rc2, out2 = self._nmcli_connect_once(
                ssid, password=None, iface=iface, bssid=bssid, wait_s=wait_s,
            )
            if rc2 == 0:
                logger.info("AP %r: connected as open network (no PSK)", ssid)
                return
            # Open retry also failed. Surface the original PSK-mode
            # error — it's typically the more informative signal
            # (the AP wasn't actually open, key-mgmt is just stale
            # cache or NM bug; operator should retry the OTA).

        # polkit denial: rc=4 + the literal "Insufficient privileges"
        # / "Not authorized" string on stderr. Translate to an
        # actionable toast pointing at the bundled setup tool — the
        # raw nmcli message is decipherable but the operator usually
        # doesn't recognise it as a polkit issue.
        if "insufficient privileges" in lower or "not authorized" in lower:
            hint = _setup_command_hint()
            raise RuntimeError(
                "nmcli access denied (polkit). Run this command on the "
                "host to grant the running user unattended access, then "
                "restart the host (RotorHazard or racelink-standalone):\n"
                f"  {hint}\n"
                f"Raw nmcli output: {out}"
            )
        raise RuntimeError(f"nmcli dev wifi connect failed ({rc}): {out}")

    def disconnect_ap(self, ssid: str, timeout_s: float = 20.0) -> None:
        """Bring the named NM connection (= SSID) down post-OTA so the
        operator's normal WiFi auto-reconnects. The profile itself is
        kept on disk so the next OTA reuses the stored secrets without
        re-prompting; only the active connection is deactivated.
        """
        ssid = str(ssid or "").strip()
        if not ssid:
            return
        self.nmcli_run(
            ["--wait", str(int(max(5.0, min(40.0, float(timeout_s))))), "con", "down", "id", ssid],
            timeout_s=max(10.0, min(45.0, float(timeout_s) + 10.0)),
        )
