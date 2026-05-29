"""Host-side WiFi operations for Windows via ``netsh wlan``.

Windows analogue of :class:`racelink.services.host_wifi_service.HostWifiService`
(which drives Linux NetworkManager via ``nmcli``). Implements the subset of
the host-WiFi API the OTA workflow needs so a Windows host can connect to a
WLED node's SoftAP, push firmware/config over HTTP, and disconnect again.

Windows-specific design constraints (verified on a German Windows 11 host):

* **No scanning.** ``netsh wlan show networks`` requires Windows Location
  Services to be enabled (and elevation), otherwise it errors out. This
  backend therefore never scans: it connects by adding a WLAN profile and
  calling ``netsh wlan connect``, then confirms association by polling
  ``netsh wlan show interfaces`` for the target SSID. ``show interfaces``
  works without Location Services.
* **No BSSID targeting.** netsh connects by SSID/profile only and cannot
  pin a specific BSSID, so the ``bssid`` / ``avoid_bssid`` hints are
  ignored. A multi-gateway OTA where several nodes broadcast the *same*
  SSID may associate with the wrong node — flash such fleets one node at a
  time, or give each node a unique AP SSID.
* **Locale-independent parsing.** netsh output labels are localized (e.g.
  German ``Status : getrennt``). This backend never matches localized
  labels: it detects the interface name positionally (the first
  ``key : value`` line of each ``show interfaces`` block is the adapter
  name) and confirms association by matching the SSID *value* string.
* **Radio / adapter enable needs admin.** ``netsh interface set interface
  admin=enable`` requires elevation. The common case (adapter already on)
  is handled without it: :meth:`radio_enabled` reports ``True`` whenever a
  WLAN interface is enumerable, so the OTA proceeds straight to connect.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Iterable, List, Optional, Sequence, Union
from xml.sax.saxutils import escape as _xml_escape

logger = logging.getLogger(__name__)

SsidArg = Union[str, Sequence[str]]


_PROFILE_WPA2PSK = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{ssid}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security>
    <authEncryption>
      <authentication>WPA2PSK</authentication>
      <encryption>AES</encryption>
      <useOneX>false</useOneX>
    </authEncryption>
    <sharedKey>
      <keyType>passPhrase</keyType>
      <protected>false</protected>
      <keyMaterial>{key}</keyMaterial>
    </sharedKey>
  </security></MSM>
</WLANProfile>
"""

_PROFILE_OPEN = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{ssid}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security>
    <authEncryption>
      <authentication>open</authentication>
      <encryption>none</encryption>
      <useOneX>false</useOneX>
    </authEncryption>
  </security></MSM>
</WLANProfile>
"""


class NetshWifiService:
    """Windows ``netsh wlan`` host-WiFi helpers (OTA WiFi handover)."""

    def __init__(self):
        self._iface_cache: Optional[str] = None

    # -- subprocess plumbing ---------------------------------------------

    def _netsh(self, args: list, timeout_s: float = 20.0) -> subprocess.CompletedProcess:
        if not shutil.which("netsh"):
            raise RuntimeError("netsh not available on host (cannot switch WiFi automatically)")
        # errors="replace": netsh console output uses an OEM codepage on
        # localized Windows; we only ever match ASCII (interface names,
        # SSID values), so replacement of non-ASCII label bytes is safe and
        # avoids a UnicodeDecodeError aborting the OTA.
        return subprocess.run(
            ["netsh"] + args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=max(1.0, timeout_s),
        )

    def _show_interfaces_raw(self) -> str:
        try:
            return self._netsh(["wlan", "show", "interfaces"], timeout_s=6.0).stdout or ""
        except Exception:
            # swallow-ok: callers treat empty output as "no WLAN interface"
            return ""

    @staticmethod
    def _is_location_block(text: str) -> bool:
        """Detect the "Location Services required" response netsh emits when
        Windows Location is off. The localized prose differs by locale, but
        the ``ms-settings:privacy-location`` URI it prints is stable ASCII."""
        return "privacy-location" in (text or "").lower()

    def _location_services_blocked(self) -> bool:
        """True when Windows Location Services is off.

        Modern Windows hides WLAN SSID/BSSID from the ``netsh wlan`` APIs
        unless Location is enabled — both scanning *and* reading the
        connected SSID via ``show interfaces`` are blocked. A ``show
        networks`` probe is the reliable up-front detector (it always needs
        Location), so callers can fail fast with an actionable message
        instead of a 30 s "no association" timeout.
        """
        try:
            proc = self._netsh(["wlan", "show", "networks"], timeout_s=6.0)
            return self._is_location_block((proc.stdout or "") + (proc.stderr or ""))
        except Exception:
            # swallow-ok: if the probe itself fails, don't block the attempt
            return False

    # -- interface detection ---------------------------------------------

    @staticmethod
    def _parse_iface_names(show_interfaces_output: str) -> List[str]:
        """Extract WLAN adapter names from ``show interfaces`` output.

        Locale-independent: the first ``key : value`` line of each
        blank-line-separated block is the adapter name (always the first
        field). The leading "N interface(s)..." summary line has an empty
        value and is skipped.
        """
        names: List[str] = []
        prev_blank = True
        for line in show_interfaces_output.splitlines():
            if not line.strip():
                prev_blank = True
                continue
            is_first_in_block = prev_blank
            prev_blank = False
            if not is_first_in_block or ":" not in line:
                continue
            value = line.partition(":")[2].strip()
            if value:
                names.append(value)
        return names

    def _detect_ifaces(self) -> List[str]:
        return self._parse_iface_names(self._show_interfaces_raw())

    def _resolve_iface(self, iface: str) -> str:
        """Pick the Windows WLAN adapter name.

        The OTA passes a Linux-style default (``wlan0``); on Windows the
        adapter is named e.g. ``WLAN`` (German) or ``Wi-Fi`` (English), so
        we auto-detect unless the caller supplied a concrete non-``wlan*``
        name that actually exists.
        """
        iface = (iface or "").strip()
        detected = self._detect_ifaces()
        if iface and not iface.lower().startswith("wlan") and (not detected or iface in detected):
            self._iface_cache = iface
            return iface
        if detected:
            self._iface_cache = detected[0]
            return detected[0]
        return iface or self._iface_cache or "WLAN"

    def wifi_interfaces(self) -> List[str]:
        return self._detect_ifaces()

    # -- radio / readiness -----------------------------------------------

    def radio_enabled(self) -> bool:
        # Treat an enumerable WLAN interface as "radio available" so the
        # OTA skips the admin-only ``set_radio`` step in the common case.
        try:
            return bool(self._detect_ifaces())
        except Exception:
            # swallow-ok: best-effort; caller proceeds with safe default
            return False

    def set_radio(self, enabled: bool) -> None:
        iface = self._resolve_iface("")
        state = "enable" if enabled else "disable"
        proc = self._netsh(
            ["interface", "set", "interface", f"name={iface}", f"admin={state}"],
            timeout_s=12.0,
        )
        if proc.returncode != 0:
            out = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"netsh interface set interface admin={state} failed "
                f"({proc.returncode}): {out} — enabling/disabling the WLAN "
                "adapter requires running the host as Administrator."
            )

    def wait_iface_ready(self, iface: str, timeout_s: float = 12.0) -> None:
        deadline = time.time() + max(1.0, float(timeout_s))
        while time.time() < deadline:
            if self._detect_ifaces():
                return
            time.sleep(0.4)
        raise RuntimeError("no WLAN interface available on host (netsh)")

    # -- connect / disconnect --------------------------------------------

    @staticmethod
    def _coerce_ssid_list(ssids: SsidArg) -> List[str]:
        if isinstance(ssids, str):
            items: Iterable[str] = [ssids]
        else:
            items = ssids or []
        return [str(s).strip() for s in items if str(s).strip()]

    @staticmethod
    def _profile_xml(ssid: str, password: str) -> str:
        ssid_x = _xml_escape(ssid)
        if password:
            return _PROFILE_WPA2PSK.format(ssid=ssid_x, key=_xml_escape(password))
        return _PROFILE_OPEN.format(ssid=ssid_x)

    def _delete_profile(self, ssid: str) -> None:
        """Best-effort delete of any existing WLAN profile named ``ssid``.

        ``netsh wlan add profile user=current`` refuses to overwrite a
        profile of the same name that already exists in another scope
        (all-user / group policy), failing with a "profile with this name
        already exists in group policy or another user scope and cannot be
        overwritten" error. Deleting first clears that collision. All-user
        profiles delete without elevation; a (rare) group-policy profile
        can't be removed and the subsequent add will surface the error.
        """
        try:
            self._netsh(["wlan", "delete", "profile", f"name={ssid}"], timeout_s=8.0)
        except Exception as ex:
            # swallow-ok: delete is a precondition cleanup; the add below
            # surfaces any real problem with a clearer message.
            logger.debug("netsh delete profile %r failed (continuing): %s", ssid, ex)

    def _add_profile(self, ssid: str, password: str, iface: str) -> None:
        # Clear any pre-existing profile (any scope) so the current-user
        # add never collides with an all-user/group-policy entry.
        self._delete_profile(ssid)
        xml = self._profile_xml(ssid, password)
        fd, path = tempfile.mkstemp(prefix="rl_wlan_", suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(xml)
            proc = self._netsh(
                ["wlan", "add", "profile", f"filename={path}",
                 f"interface={iface}", "user=current"],
                timeout_s=10.0,
            )
            if proc.returncode != 0:
                out = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"netsh wlan add profile failed ({proc.returncode}): {out}")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _connect(self, ssid: str, iface: str) -> None:
        proc = self._netsh(
            ["wlan", "connect", f"name={ssid}", f"ssid={ssid}", f"interface={iface}"],
            timeout_s=12.0,
        )
        if proc.returncode != 0:
            out = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"netsh wlan connect failed ({proc.returncode}): {out}")

    def _disconnect(self, iface: str) -> None:
        try:
            self._netsh(["wlan", "disconnect", f"interface={iface}"], timeout_s=6.0)
        except Exception as ex:
            # swallow-ok: disconnect is best-effort cleanup
            logger.debug("netsh disconnect on %s failed: %s", iface, ex)

    def _is_associated(self, ssid: str, iface: str) -> bool:
        return self._associated_candidate([ssid]) == ssid

    def _iface_values(self) -> set:
        """All ``key : value`` values from ``show interfaces`` (the SSID /
        Profil fields carry the associated network name)."""
        return {
            line.partition(":")[2].strip()
            for line in self._show_interfaces_raw().splitlines()
            if ":" in line
        }

    def _associated_candidate(self, candidates: Sequence[str]) -> str:
        """Return the candidate SSID the adapter is currently associated
        with, or ``""``.

        Locale-independent: the connected network's SSID value appears in
        ``show interfaces`` output (the ``SSID`` / ``Profil`` field) only
        while associated. We match the SSID *value* string, never a
        localized state label, and check every candidate so a connect that
        lands on any of them is detected immediately.
        """
        values = self._iface_values()
        for ssid in candidates:
            if ssid in values:
                return ssid
        return ""

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
        """Connect to the first of ``ssids`` that associates; returns its SSID.

        ``bssid`` / ``avoid_bssid`` are accepted for API parity with the
        nmcli backend but ignored — netsh cannot target a BSSID.

        Strategy (netsh can't scan, so we connect-and-watch):

        1. Disconnect first for a clean state, then pre-create one
           current-user profile per candidate (delete-then-add clears any
           all-user / group-policy collision).
        2. Issue ``netsh wlan connect`` for a candidate and watch
           ``show interfaces`` for association to *any* candidate, giving
           each candidate a **patient** slice of the remaining budget
           (min 10 s) so a slow WLED AP bring-up isn't torn down by
           prematurely switching SSIDs — the failure mode that produced
           "connect issued but no association within timeout" despite the
           AP actually coming up.
        3. Open-AP fallback: if a PSK profile never associates, re-add the
           candidates as open networks and try again (some WLED nodes run
           an open SoftAP), as long as budget remains.
        """
        candidates = self._coerce_ssid_list(ssids)
        if not candidates:
            raise RuntimeError("no candidate SSIDs supplied")
        iface = self._resolve_iface(iface)
        # Fail fast and actionably: without Location Services netsh can
        # neither find the WLED AP nor confirm the connection, so the OTA
        # would otherwise just time out opaquely.
        if self._location_services_blocked():
            raise RuntimeError(
                "Windows Location Services is disabled, so netsh cannot read "
                "or confirm WLAN networks — the host can neither find the WLED "
                "access point nor verify the connection. Open Location settings "
                "(paste 'ms-settings:privacy-location' into the address bar or "
                "Win+R) and turn on 'Location services' AND 'Let desktop apps "
                "access your location', then retry the firmware update."
            )
        deadline = time.time() + max(15.0, float(timeout_s))
        logger.info(
            "netsh connect_ap: candidates=%s iface=%s budget=%.1fs (bssid hints ignored on Windows)",
            candidates, iface, deadline - time.time(),
        )
        # Clean slate so a stale association doesn't mask the target SSID.
        self._disconnect(iface)
        for ssid in candidates:
            try:
                self._add_profile(ssid, password, iface)
            except Exception as ex:
                logger.debug("netsh add profile %r failed: %s", ssid, ex)

        hit = self._associated_candidate(candidates)
        if hit:
            return hit

        last_err: Optional[str] = None
        open_fallback_done = not password  # nothing to fall back to for open APs
        while time.time() < deadline:
            for i, ssid in enumerate(candidates):
                if time.time() >= deadline:
                    break
                try:
                    self._connect(ssid, iface)
                except Exception as ex:
                    last_err = str(ex)
                    logger.debug("netsh connect %r failed: %s", ssid, ex)
                    continue
                cands_left = len(candidates) - i
                window = max(10.0, (deadline - time.time()) / max(1, cands_left))
                w_end = min(time.time() + window, deadline)
                while time.time() < w_end:
                    hit = self._associated_candidate(candidates)
                    if hit:
                        logger.info("netsh connect_ap: associated with %r on %s", hit, iface)
                        return hit
                    time.sleep(0.6)
                # Diagnostic snapshot (INFO so it survives the default log
                # level): what is the adapter actually associated to, if
                # anything? A non-candidate value here means Windows roamed
                # back to a known network (NCSI/auto-connect) instead of
                # holding the WLED AP.
                seen = sorted(v for v in self._iface_values() if v)
                logger.info(
                    "netsh connect_ap: %r window (%.0fs) elapsed without association; "
                    "interface currently reports values=%s", ssid, window, seen,
                )
                last_err = f"connect issued for {ssid!r} but no association within window"
            # PSK profiles didn't associate — retry once with open profiles.
            if not open_fallback_done and time.time() < deadline:
                open_fallback_done = True
                logger.info("netsh connect_ap: PSK profiles did not associate; retrying as open networks")
                for ssid in candidates:
                    try:
                        self._add_profile(ssid, "", iface)
                    except Exception as ex:
                        logger.debug("netsh add open profile %r failed: %s", ssid, ex)
                continue
            break
        if last_err:
            raise RuntimeError(f"could not connect to any of {candidates}: {last_err}")
        raise RuntimeError(f"timeout connecting to one of {candidates} on {iface}")

    def active_bssid(self, iface: str) -> str:
        # netsh can't target/avoid a BSSID; the OTA's avoid-list logic
        # gracefully no-ops on an empty string.
        return ""

    def disconnect_iface_fast(self, iface: str) -> None:
        self._disconnect(self._resolve_iface(iface))
        logger.info("Host WiFi (netsh): disconnect issued on %s", iface)

    def disconnect_ap(self, ssid: str, timeout_s: float = 20.0) -> None:
        # netsh disconnects by interface, not by SSID/profile name.
        self._disconnect(self._resolve_iface(""))
