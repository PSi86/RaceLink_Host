"""Declarative capture manifest for the RaceLink Host screenshot workflow.

``capture_all(cap)`` is the single entry point driven by ``capture.py``. Each
shot is wrapped in :func:`_safe` so a selector that drifts after a UI change
logs a warning and skips, rather than aborting the whole run — making the
"what broke after my change?" signal obvious without losing the rest.

Edit this file (not ``capture.py``) when pages, dialogs or close-ups change.
The demo data the shots assume (groups, the "Race Start Sequence" scene that
contains every action kind, etc.) is seeded by
``racelink/integrations/standalone/_sim.py``.
"""

from __future__ import annotations

import traceback


def _safe(label: str, fn) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] skipped {label}: {type(e).__name__}: {e}")
        if _DEBUG_TRACEBACK:
            traceback.print_exc()


# Flip to True locally to see why a shot was skipped.
_DEBUG_TRACEBACK = False


# Scene-editor action rows in the seeded "Race Start Sequence", in order.
_ACTION_ROWS = [
    ("scene_action_rl_effect", "Scene action: RL Effect (inline OPC_CONTROL params)"),
    ("scene_action_rl_preset", "Scene action: RL Preset (apply saved preset by id)"),
    ("scene_action_wled_preset", "Scene action: WLED Preset (OPC_PRESET)"),
    ("scene_action_startblock", "Scene action: Startblock control"),
    ("scene_action_sync", "Scene action: SYNC (fire armed actions)"),
    ("scene_action_delay", "Scene action: Delay"),
    ("scene_action_offset_group", "Scene action: Offset Group container (with child + offset formula)"),
]


def _select_group(cap, name: str) -> None:
    cap.page.locator("aside").get_by_text(name, exact=False).first.click()
    cap.page.wait_for_timeout(500)


def _devices_page(cap) -> None:
    # Wide base; autosize sets the height per shot so full-width/height
    # elements neither clip nor leave large empty panels.
    cap.set_viewport(1800, 900)
    cap.goto("/")
    # Land on a populated group so the table shows real rows (incl. the
    # low-battery device) rather than the near-empty Unconfigured sink.
    _safe("select_start_gate", lambda: _select_group(cap, "Start Gate"))

    # Element close-ups (height-agnostic — element screenshots crop tightly).
    _safe("ribbon_menu_band_devices", lambda: cap.shot_selector(
        "ribbon_menu_band_devices",
        "Menu band (ribbon) on the Devices page", "header"))
    _safe("master_bar_gateways", lambda: cap.shot_selector(
        "master_bar_gateways",
        "Master bar: two RF gateways + one Ethernet gateway, all ready (IDLE)",
        "header div.mt-1.w-full.items-center"))
    _safe("devices_table", lambda: cap.shot_selector(
        "devices_table", "Device table with all columns", "#rlTable"))
    _safe("toolbar_bulk_actions", lambda: cap.shot_selector(
        "toolbar_bulk_actions", "Bulk actions toolbar",
        "xpath=//label[contains(.,'Move selected to group')]/ancestor::div[1]"))
    _safe("toolbar_node_config", lambda: cap.shot_selector(
        "toolbar_node_config", "Node config toolbar",
        "xpath=//label[contains(.,'Node Config')]/ancestor::div[1]"))
    _safe("toolbar_config_display", lambda: cap.shot_selector(
        "toolbar_config_display", "Config display toolbar",
        "xpath=//label[contains(.,'Config display')]/ancestor::div[1]"))
    _safe("banner_low_battery", lambda: cap.shot_selector(
        "banner_low_battery", "Low-battery warning banner",
        "xpath=//*[contains(@class,'ffd28a')]/ancestor::div[1]"))

    # Sidebar: the grid stretches this column to the (taller) table column, so
    # clip to the last group row instead of capturing the full-height card.
    _safe("devices_sidebar_groups", lambda: cap.shot_region_to(
        "devices_sidebar_groups",
        "Devices sidebar: network filter and groups with RF / Ethernet badges",
        "aside", "aside ul li:last-child"))

    # Overview: fit the viewport to the device pane so all rows show without
    # clipping and without an empty panel below.
    def overview():
        cap.autosize_selector("main > section")
        cap.shot_page(
            "page_devices_overview",
            "Devices page — full view: menu band, master bar, sidebar and device table")
    _safe("page_devices_overview", overview)


def _devices_dialogs(cap) -> None:
    dialogs = [
        ("dialog_discover_devices", "Discover Devices dialog",
         lambda: cap.click_text("Discover Devices")),
        ("dialog_firmware_update", "Firmware Update dialog (pre-start)",
         lambda: cap.click_text("Firmware Update")),
        ("dialog_wled_presets", "WLED Presets dialog",
         lambda: cap.click_text("WLED Presets")),
        ("dialog_rl_presets", "RaceLink Presets dialog (list + editor)",
         lambda: cap.click_text("RL Presets")),
        ("dialog_resync_groups", "Re-sync group config dialog",
         lambda: cap.click_text("Re-sync group config")),
        ("dialog_pair_assistant", "Onboarding / Pair Assistant dialog",
         lambda: cap.click_label("Pair Assistant")),
        ("dialog_channel_scan", "Channel Scan dialog",
         lambda: cap.click_label("Channel scan")),
        ("dialog_host_settings", "Host Settings dialog",
         lambda: cap.click_label("Settings")),
        ("dialog_new_group", "New Group dialog",
         lambda: cap.click_title("Create group")),
        ("dialog_manage_groups", "Manage Groups dialog",
         lambda: cap.click_label("Manage groups")),
    ]
    for name, desc, trigger in dialogs:
        def run(name=name, desc=desc, trigger=trigger):
            # Reset to a base viewport; shot_dialog grows it to fit tall
            # dialogs. Width is comfortably wider than the 560px dialog.
            cap.set_viewport(1280, 900)
            cap.goto("/")
            trigger()
            cap.shot_dialog(name, desc)
        _safe(name, run)

    # Network Manager is opened from inside Host Settings.
    def network_manager():
        cap.set_viewport(1280, 900)
        cap.goto("/")
        cap.click_label("Settings")
        cap.page.get_by_role("button", name="Network", exact=False).first.click()
        cap.page.wait_for_timeout(400)
        cap.shot_dialog("dialog_network_manager", "Network Manager dialog (RF + Ethernet networks)")
    _safe("dialog_network_manager", network_manager)

    # Specials: per-device capability dialog. The trigger is the device-type
    # link in the Type column (only present when the device has options).
    def specials():
        cap.set_viewport(1280, 900)
        cap.goto("/")
        _select_group(cap, "Start Gate")
        cap.click_selector('[title^="Configure options for"]')
        cap.shot_dialog("dialog_specials", "Device Specials dialog (per-capability options)")
    _safe("dialog_specials", specials)


def _scenes_page(cap) -> None:
    cap.set_viewport(1500, 980)
    cap.goto("/scenes")
    _safe("ribbon_menu_band_scenes", lambda: cap.shot_selector(
        "ribbon_menu_band_scenes", "Menu band on the Scenes page", "header"))
    _safe("page_scenes_overview", lambda: cap.shot_page(
        "page_scenes_overview", "Scenes page — scene list sidebar and editor"))

    # Select the rich scene that contains every action kind.
    def open_rich_scene():
        cap.page.locator('li[title="Race Start Sequence"]').first.click()
        cap.page.wait_for_timeout(600)
    _safe("select_race_start_sequence", open_rich_scene)

    _safe("scenes_sidebar_list", lambda: cap.shot_region_to(
        "scenes_sidebar_list", "Scene list sidebar (with scope badges)",
        "aside", "aside ul li:last-child"))

    # Fit the viewport to the action list so the whole editor (all 7 actions
    # + footer) is captured without internal scrolling.
    def editor_full():
        cap.autosize_selector("main > section .overflow-auto")
        cap.shot_selector(
            "scene_editor_full", "Scene editor with the full action list (every action kind)",
            "main > section")
    _safe("scene_editor_full", editor_full)

    _safe("scene_editor_sticky_footer", lambda: cap.shot_selector(
        "scene_editor_sticky_footer",
        "Scene editor sticky footer (run pip strip, cost badge, Delete / Duplicate / Save / Run)",
        "xpath=//button[normalize-space()='Run']/ancestor::div[contains(@class,'border-t')][1]"))

    for i, (name, desc) in enumerate(_ACTION_ROWS):
        def run(i=i, name=name, desc=desc):
            cap.shot_selector(
                name, desc,
                f"xpath=(//*[contains(@class,'rl-action-grip')]/ancestor::div[contains(@class,'rounded-md')][1])[{i + 1}]")
        _safe(name, run)


def capture_all(cap) -> None:
    _devices_page(cap)
    _devices_dialogs(cap)
    _scenes_page(cap)
