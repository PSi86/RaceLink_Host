/**
 * Scene Manager — page-scoped script for /racelink/scenes (R5).
 *
 * This file replaces the modal-dialog scene editor that previously lived
 * inside racelink.js. Loads on the dedicated /scenes page only. Reuses
 * the shared helpers (apiGet, apiPost, apiPut, apiDelete, state) and the
 * RL-Preset modal opener (btnRlPresets) by reading them off ``window.RL``
 * exported by racelink.js.
 */
(function(){
  // racelink.js runs first and publishes window.RL with the shared helpers.
  // If it didn't (e.g. wrong page-load order), bail out loudly so the
  // failure is obvious instead of producing silently-broken UI.
  const RL = window.RL;
  if(!RL){
    console.error("[scenes] window.RL not available — racelink.js must load before scenes.js");
    return;
  }
  const { apiGet, apiPost, apiPut, apiDelete, state } = RL;
  const $ = (sel, ctx=document) => ctx.querySelector(sel);

  const SCENE_KIND_LABELS = {
    rl_preset: "Apply RL Preset",
    wled_preset: "Apply WLED Preset",
    wled_control: "Apply WLED Control",
    startblock: "Startblock Control",
    sync: "SYNC (fire armed)",
    delay: "Delay",
  };
  const SCENE_KINDS_ORDER = ["rl_preset", "wled_preset", "wled_control", "startblock", "sync", "delay"];
  const SCENE_MAX_ACTIONS = 20;

  function setScenesHint(text){
    const el = $("#scenesHint");
    if(el) el.textContent = text || "";
  }

  async function loadScenes(){
    const r = await apiGet("/racelink/api/scenes");
    state.scenes.items = (r && r.ok && r.scenes) ? r.scenes : [];
    return state.scenes.items;
  }

  async function ensureScenesSchema(){
    if(state.scenes.schema) return state.scenes.schema;
    const r = await apiGet("/racelink/api/scenes/editor-schema");
    if(!r || !r.ok) return null;
    state.scenes.schema = {
      kinds: Array.isArray(r.kinds) ? r.kinds : [],
      flagKeys: Array.isArray(r.flag_keys) ? r.flag_keys : [],
    };
    return state.scenes.schema;
  }

  async function loadGroupsAndDevicesForTargetPicker(){
    // The scenes page doesn't render the device table, but the action target
    // picker still needs current group/device lists. Fetch them once on
    // editor open; SSE refreshes update them when groups/devices change.
    try{
      const [g, d] = await Promise.all([
        apiGet("/racelink/api/groups"),
        apiGet("/racelink/api/devices"),
      ]);
      if(g && g.ok) state.groups = g.groups || [];
      if(d && d.ok) state.devices = d.devices || [];
    }catch(e){
      console.error("[scenes] failed to fetch groups/devices for target picker", e);
    }
  }

  function findKindMeta(kind){
    if(!state.scenes.schema) return null;
    return state.scenes.schema.kinds.find(k => k.kind === kind) || null;
  }

  function defaultActionForKind(kind){
    const meta = findKindMeta(kind);
    const action = { kind };
    if(kind === "delay"){
      action.duration_ms = 0;
      return action;
    }
    if(kind === "sync"){
      return action;
    }
    action.target = { kind: "group", value: 1 };
    action.params = {};
    if(meta && meta.supports_flags_override){
      action.flags_override = {};
    }
    return action;
  }

  function cloneAction(action){
    return JSON.parse(JSON.stringify(action || {}));
  }

  function renderSceneList(){
    const listEl = $("#sceneList");
    if(!listEl) return;
    listEl.innerHTML = "";
    if(!state.scenes.items.length){
      const empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "(no scenes yet)";
      listEl.appendChild(empty);
      return;
    }
    state.scenes.items.forEach(s => {
      const li = document.createElement("li");
      li.textContent = `${s.label} (${(s.actions || []).length})`;
      li.dataset.key = s.key;
      if(s.key === state.scenes.selectedKey) li.classList.add("active");
      li.addEventListener("click", () => selectScene(s.key));
      listEl.appendChild(li);
    });
  }

  function selectScene(key){
    state.scenes.selectedKey = key;
    state.scenes.lastRunResult = null;
    const scene = state.scenes.items.find(s => s.key === key) || null;
    state.scenes.draft = scene ? cloneAction(scene) : null;
    renderSceneList();
    renderSceneEditor();
  }

  function newSceneDraft(){
    state.scenes.selectedKey = null;
    state.scenes.lastRunResult = null;
    state.scenes.draft = { id: null, key: null, label: "", actions: [] };
    renderSceneList();
    renderSceneEditor();
  }

  function renderSceneEditor(){
    const editor = $("#sceneEditor");
    if(!editor) return;
    editor.innerHTML = "";
    const draft = state.scenes.draft;
    if(!draft){
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "Select a scene on the left, or create a new one.";
      editor.appendChild(p);
      return;
    }

    // --- Meta row -------------------------------------------------------
    const meta = document.createElement("div");
    meta.className = "rl-scene-meta";

    const labelLbl = document.createElement("label");
    labelLbl.textContent = "Label";
    const labelIn = document.createElement("input");
    labelIn.type = "text";
    labelIn.value = draft.label || "";
    labelIn.id = "sceneLabelInput";
    labelIn.style.minWidth = "240px";
    meta.appendChild(labelLbl);
    meta.appendChild(labelIn);

    if(draft.key){
      const keyInfo = document.createElement("span");
      keyInfo.className = "muted";
      keyInfo.textContent = `key: ${draft.key}`;
      meta.appendChild(keyInfo);
    }

    editor.appendChild(meta);

    // --- Run progress strip (shown after a run) -------------------------
    if(state.scenes.lastRunResult){
      const strip = document.createElement("div");
      strip.className = "rl-scene-progress";
      const status = document.createElement("span");
      const r = state.scenes.lastRunResult;
      status.textContent = r.ok ? "Last run: OK" : `Last run: ${r.error || "failed"}`;
      strip.appendChild(status);
      (r.actions || []).forEach(a => {
        const pip = document.createElement("span");
        pip.className = "pip " + (a.degraded ? "degraded" : (a.ok ? "ok" : "error"));
        // Display rebased to 1 to match the action-row labels (#1 #2 …).
        // The runner's ActionResult.index stays 0-based for log/structured output.
        const display = a.index + 1;
        pip.textContent = String(display);
        pip.title = `#${display} ${a.kind}${a.error ? " — " + a.error : ""} (${a.duration_ms}ms)`;
        strip.appendChild(pip);
      });
      editor.appendChild(strip);
    }

    // --- Action list ----------------------------------------------------
    const actionsContainer = document.createElement("div");
    actionsContainer.className = "rl-scenes-actions";
    (draft.actions || []).forEach((action, idx) => {
      actionsContainer.appendChild(buildSceneActionRow(action, idx, draft));
    });
    editor.appendChild(actionsContainer);

    // --- Add-action row -------------------------------------------------
    const addRow = document.createElement("div");
    addRow.className = "rl-scene-add-row";
    const addLbl = document.createElement("span");
    addLbl.className = "muted";
    addLbl.textContent = `Add action (${(draft.actions || []).length}/${SCENE_MAX_ACTIONS}):`;
    const kindPicker = document.createElement("select");
    SCENE_KINDS_ORDER.forEach(k => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = SCENE_KIND_LABELS[k] || k;
      kindPicker.appendChild(opt);
    });
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.textContent = "+ Add";
    addBtn.disabled = (draft.actions || []).length >= SCENE_MAX_ACTIONS;
    addBtn.addEventListener("click", () => {
      if((draft.actions || []).length >= SCENE_MAX_ACTIONS) return;
      draft.actions = draft.actions || [];
      draft.actions.push(defaultActionForKind(kindPicker.value));
      renderSceneEditor();
    });
    addRow.appendChild(addLbl);
    addRow.appendChild(kindPicker);
    addRow.appendChild(addBtn);
    editor.appendChild(addRow);

    // --- Action bar -----------------------------------------------------
    const actionBar = document.createElement("div");
    actionBar.className = "rl-special-actions";
    actionBar.style.marginTop = "12px";

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.textContent = draft.key ? "Save" : "Create";
    saveBtn.addEventListener("click", saveSceneDraft);
    actionBar.appendChild(saveBtn);

    if(draft.key){
      const runBtn = document.createElement("button");
      runBtn.type = "button";
      runBtn.textContent = "Run";
      runBtn.addEventListener("click", () => runScene(draft.key));
      actionBar.appendChild(runBtn);

      const dupBtn = document.createElement("button");
      dupBtn.type = "button";
      dupBtn.textContent = "Duplicate";
      dupBtn.addEventListener("click", async () => {
        const newLabel = prompt("Label for duplicate?", `${draft.label} copy`);
        if(!newLabel) return;
        const r = await apiPost(`/racelink/api/scenes/${draft.key}/duplicate`, {label: newLabel});
        if(!r.ok){ setScenesHint(r.error || "Duplicate failed."); return; }
        await loadScenes();
        renderSceneList();
        selectScene(r.scene.key);
      });
      actionBar.appendChild(dupBtn);

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", async () => {
        if(!confirm(`Delete scene "${draft.label}"?`)) return;
        const r = await apiDelete(`/racelink/api/scenes/${draft.key}`);
        if(!r.ok){ setScenesHint(r.error || "Delete failed."); return; }
        await loadScenes();
        state.scenes.selectedKey = null;
        state.scenes.draft = null;
        state.scenes.lastRunResult = null;
        renderSceneList();
        renderSceneEditor();
      });
      actionBar.appendChild(delBtn);
    }

    editor.appendChild(actionBar);
  }

  function buildSceneActionRow(action, idx, draft){
    const row = document.createElement("div");
    row.className = "rl-scene-action-row";
    // R7: stamp the row with its position so __rlSceneProgress can target
    // it by querySelector without rebuilding the editor.
    row.dataset.actionIdx = String(idx);

    // R7: live status (set by __rlSceneProgress during a run) wins over the
    // post-run lastRunResult fallback. Both paths apply the same border
    // colour rules; the live path beats the post-hoc one only because it
    // arrives earlier on the wire.
    const liveStatus = state.scenes.actionStatus
      ? state.scenes.actionStatus[idx]
      : undefined;
    if(liveStatus){
      row.classList.add(liveStatus);
    }else{
      const runResult = state.scenes.lastRunResult;
      if(runResult && runResult.actions){
        const r = runResult.actions.find(x => x.index === idx);
        if(r){
          if(r.degraded) row.classList.add("degraded");
          else if(r.ok) row.classList.add("ok");
          else row.classList.add("error");
        }
      }
    }

    const indexCol = document.createElement("div");
    indexCol.className = "rl-scene-action-index";
    indexCol.textContent = `#${idx + 1}`;
    row.appendChild(indexCol);

    const kindCol = document.createElement("div");
    kindCol.className = "rl-scene-action-kind";
    const kindLabel = document.createElement("label");
    kindLabel.textContent = "Kind";
    const kindSelect = document.createElement("select");
    SCENE_KINDS_ORDER.forEach(k => {
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = SCENE_KIND_LABELS[k] || k;
      if(k === action.kind) opt.selected = true;
      kindSelect.appendChild(opt);
    });
    kindSelect.addEventListener("change", () => {
      draft.actions[idx] = defaultActionForKind(kindSelect.value);
      renderSceneEditor();
    });
    kindCol.appendChild(kindLabel);
    kindCol.appendChild(kindSelect);
    row.appendChild(kindCol);

    const bodyCol = document.createElement("div");
    bodyCol.className = "rl-scene-action-body";
    buildActionBody(bodyCol, action, idx, draft);
    row.appendChild(bodyCol);

    const ctrl = document.createElement("div");
    ctrl.className = "rl-scene-action-controls";
    const upBtn = document.createElement("button");
    upBtn.type = "button";
    upBtn.textContent = "↑";
    upBtn.disabled = idx === 0;
    upBtn.addEventListener("click", () => moveAction(draft, idx, -1));
    const downBtn = document.createElement("button");
    downBtn.type = "button";
    downBtn.textContent = "↓";
    downBtn.disabled = idx === draft.actions.length - 1;
    downBtn.addEventListener("click", () => moveAction(draft, idx, +1));
    const rmBtn = document.createElement("button");
    rmBtn.type = "button";
    rmBtn.textContent = "Remove";
    rmBtn.addEventListener("click", () => {
      draft.actions.splice(idx, 1);
      renderSceneEditor();
    });
    ctrl.appendChild(upBtn);
    ctrl.appendChild(downBtn);
    ctrl.appendChild(rmBtn);
    row.appendChild(ctrl);

    return row;
  }

  function moveAction(draft, idx, delta){
    const j = idx + delta;
    if(j < 0 || j >= draft.actions.length) return;
    const [item] = draft.actions.splice(idx, 1);
    draft.actions.splice(j, 0, item);
    renderSceneEditor();
  }

  function buildActionBody(container, action, idx, draft){
    container.innerHTML = "";
    const kindMeta = findKindMeta(action.kind);

    if(action.kind === "sync"){
      const note = document.createElement("span");
      note.className = "muted";
      note.textContent = "Broadcasts OPC_SYNC — fires every node currently in arm-on-sync state.";
      container.appendChild(note);
      return;
    }

    if(action.kind === "delay"){
      const wrap = document.createElement("div");
      wrap.className = "rl-slider-wrap";
      const lbl = document.createElement("span");
      lbl.className = "muted";
      lbl.textContent = "Duration (ms):";
      const inp = document.createElement("input");
      inp.type = "number";
      inp.min = 0;
      inp.max = 60000;
      inp.step = 50;
      inp.value = Number(action.duration_ms || 0);
      inp.style.width = "100px";
      inp.addEventListener("input", () => {
        const v = Math.max(0, Math.min(60000, Number(inp.value) || 0));
        draft.actions[idx].duration_ms = v;
      });
      wrap.appendChild(lbl);
      wrap.appendChild(inp);
      container.appendChild(wrap);
      return;
    }

    if(kindMeta && kindMeta.supports_target){
      container.appendChild(buildTargetPicker(action, idx, draft));
    }
    if(kindMeta && Array.isArray(kindMeta.vars) && kindMeta.vars.length){
      container.appendChild(buildVarsRow(action, idx, draft, kindMeta));
    }
    if(kindMeta && kindMeta.supports_flags_override){
      container.appendChild(buildFlagsOverrideRow(action, idx, draft));
    }
  }

  function buildTargetPicker(action, idx, draft){
    const wrap = document.createElement("div");
    wrap.className = "rl-scene-target";

    const target = action.target || (action.target = { kind: "group", value: 1 });

    const groupRadio = document.createElement("input");
    groupRadio.type = "radio";
    groupRadio.name = `target-kind-${idx}`;
    groupRadio.value = "group";
    groupRadio.checked = target.kind === "group";

    const deviceRadio = document.createElement("input");
    deviceRadio.type = "radio";
    deviceRadio.name = `target-kind-${idx}`;
    deviceRadio.value = "device";
    deviceRadio.checked = target.kind === "device";

    const groupLbl = document.createElement("label");
    groupLbl.className = "inline";
    groupLbl.appendChild(groupRadio);
    groupLbl.appendChild(document.createTextNode(" Group"));

    const deviceLbl = document.createElement("label");
    deviceLbl.className = "inline";
    deviceLbl.appendChild(deviceRadio);
    deviceLbl.appendChild(document.createTextNode(" Device"));

    wrap.appendChild(groupLbl);
    wrap.appendChild(deviceLbl);

    const valueSelect = document.createElement("select");

    function fillValueOptions(){
      valueSelect.innerHTML = "";
      // R4: use only the radio's checked state. The previous fallback
      // ``|| (target.kind === "group")`` consulted a stale closure: commit()
      // reassigns ``draft.actions[idx].target`` to a fresh object, so the
      // captured ``target`` reference still pointed at the pre-switch dict
      // and forced the group branch on every device-radio click.
      const isGroup = groupRadio.checked;
      if(isGroup){
        (state.groups || []).forEach(g => {
          if(typeof g.id !== "number" && typeof g.groupId !== "number") return;
          const id = (typeof g.id === "number") ? g.id : g.groupId;
          if(id < 0 || id > 254) return;
          const opt = document.createElement("option");
          opt.value = String(id);
          opt.textContent = `${g.name || ("Group " + id)} (${id})`;
          if(Number(target.value) === id && target.kind === "group") opt.selected = true;
          valueSelect.appendChild(opt);
        });
        if(!valueSelect.options.length){
          const opt = document.createElement("option");
          opt.value = "1";
          opt.textContent = "(no groups — using id=1)";
          valueSelect.appendChild(opt);
        }
      }else{
        (state.devices || []).forEach(d => {
          if(!d.addr) return;
          const addr = String(d.addr).toUpperCase();
          if(addr.length !== 12) return;
          const opt = document.createElement("option");
          opt.value = addr;
          opt.textContent = `${d.name || addr} (${addr})`;
          if(target.kind === "device" && String(target.value).toUpperCase() === addr) opt.selected = true;
          valueSelect.appendChild(opt);
        });
        if(!valueSelect.options.length){
          const opt = document.createElement("option");
          opt.value = "AABBCCDDEEFF";
          opt.textContent = "(no devices yet — placeholder)";
          valueSelect.appendChild(opt);
        }
      }
    }

    function commit(){
      const isGroup = groupRadio.checked;
      if(isGroup){
        draft.actions[idx].target = { kind: "group", value: Number(valueSelect.value) || 1 };
      }else{
        draft.actions[idx].target = { kind: "device", value: String(valueSelect.value || "").toUpperCase() };
      }
    }

    function refreshAndCommit(){
      fillValueOptions();
      commit();
    }
    groupRadio.addEventListener("change", refreshAndCommit);
    deviceRadio.addEventListener("change", refreshAndCommit);
    valueSelect.addEventListener("change", commit);

    fillValueOptions();
    wrap.appendChild(valueSelect);
    return wrap;
  }

  function buildVarsRow(action, idx, draft, kindMeta){
    const wrap = document.createElement("div");
    wrap.className = "rl-scene-vars";
    if(!action.params) action.params = {};
    const params = action.params;

    function coerceSelectValue(v){
      if(v === undefined || v === null) return v;
      const n = Number(v);
      return (Number.isFinite(n) && String(n) === String(v)) ? n : v;
    }

    kindMeta.vars.forEach(varKey => {
      const uiInfo = (kindMeta.ui && kindMeta.ui[varKey]) || {};
      const fieldWrap = document.createElement("div");
      fieldWrap.className = "rl-special-input";
      const lbl = document.createElement("span");
      lbl.className = "rl-special-input-label";
      lbl.textContent = varKey;
      fieldWrap.appendChild(lbl);

      let input;
      if(uiInfo.widget === "select"){
        input = document.createElement("select");
        (uiInfo.options || []).forEach(o => {
          const opt = document.createElement("option");
          opt.value = String(o.value);
          opt.textContent = o.label || String(o.value);
          if(String(params[varKey]) === String(o.value)) opt.selected = true;
          input.appendChild(opt);
        });
        if(!input.options.length){
          const opt = document.createElement("option");
          opt.value = "";
          opt.textContent = "(no options)";
          input.appendChild(opt);
        }
        input.addEventListener("change", () => {
          params[varKey] = coerceSelectValue(input.value);
        });
        // R3: commit the initial selection so a Save without user input
        // still posts a valid action. Without this, freshly-added rl_preset
        // / wled_preset / wled_control actions persisted with presetId
        // undefined and the runner returned ``missing_preset_id``.
        if(params[varKey] === undefined && input.options.length){
          params[varKey] = coerceSelectValue(input.value);
        }
      }else if(uiInfo.widget === "slider"){
        const sliderWrap = document.createElement("div");
        sliderWrap.className = "rl-slider-wrap";
        const range = document.createElement("input");
        range.type = "range";
        // R2: 50%-of-range default mirrors buildSpecialVarInput
        // (racelink.js A13 contract).
        const min = uiInfo.min !== undefined ? Number(uiInfo.min) : 0;
        const max = uiInfo.max !== undefined ? Number(uiInfo.max) : 255;
        const fallback = Math.round((min + max) / 2);
        const initial = (params[varKey] != null) ? Number(params[varKey]) : fallback;
        range.min = String(min);
        range.max = String(max);
        range.value = String(initial);
        const num = document.createElement("input");
        num.type = "number";
        num.min = String(min);
        num.max = String(max);
        num.value = String(initial);
        num.style.width = "70px";
        params[varKey] = initial;
        const sync = (src) => {
          const v = Number(src.value) || 0;
          range.value = v;
          num.value = v;
          params[varKey] = v;
        };
        range.addEventListener("input", () => sync(range));
        num.addEventListener("input", () => sync(num));
        sliderWrap.appendChild(range);
        sliderWrap.appendChild(num);
        fieldWrap.appendChild(sliderWrap);
        wrap.appendChild(fieldWrap);
        return;
      }else{
        input = document.createElement("input");
        input.type = "text";
        input.value = (params[varKey] != null) ? String(params[varKey]) : "";
        input.addEventListener("input", () => {
          params[varKey] = input.value;
        });
        if(params[varKey] === undefined && input.value !== ""){
          params[varKey] = input.value;
        }
      }
      fieldWrap.appendChild(input);
      wrap.appendChild(fieldWrap);
    });

    return wrap;
  }

  function buildFlagsOverrideRow(action, idx, draft){
    const wrap = document.createElement("div");
    wrap.className = "rl-scene-flags";
    const lbl = document.createElement("span");
    lbl.className = "muted";
    lbl.textContent = "Flags override:";
    wrap.appendChild(lbl);

    if(!action.flags_override) action.flags_override = {};
    const flagKeys = (state.scenes.schema && state.scenes.schema.flagKeys) || [];
    flagKeys.forEach(fk => {
      const tw = document.createElement("label");
      tw.className = "rl-toggle-wrap";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = Boolean(action.flags_override[fk]);
      cb.addEventListener("change", () => {
        action.flags_override[fk] = cb.checked;
      });
      const txt = document.createElement("span");
      txt.textContent = fk;
      tw.appendChild(cb);
      tw.appendChild(txt);
      wrap.appendChild(tw);
    });

    if(!flagKeys.length){
      const w = document.createElement("span");
      w.className = "muted";
      w.textContent = "(schema not loaded)";
      wrap.appendChild(w);
    }

    return wrap;
  }

  async function saveSceneDraft(){
    const draft = state.scenes.draft;
    if(!draft) return;
    const labelInput = $("#sceneLabelInput");
    const label = (labelInput && labelInput.value || "").trim();
    if(!label){ setScenesHint("Label is required."); return; }
    const body = { label, actions: draft.actions || [] };
    let r;
    if(draft.key){
      r = await apiPut(`/racelink/api/scenes/${draft.key}`, body);
    }else{
      r = await apiPost(`/racelink/api/scenes`, body);
    }
    if(!r.ok){ setScenesHint(r.error || "Save failed."); return; }
    setScenesHint(`Saved "${r.scene.label}".`);
    await loadScenes();
    state.scenes.selectedKey = r.scene.key;
    state.scenes.draft = cloneAction(r.scene);
    renderSceneList();
    renderSceneEditor();
  }

  async function runScene(key){
    setScenesHint(`Running "${key}"…`);
    // R7: arm live-status tracking. Each action row clears its border
    // colour and the SSE handler will paint blue (running) / green (ok) /
    // red (error/degraded) as transitions arrive.
    state.scenes.activeRunKey = key;
    state.scenes.actionStatus = [];
    state.scenes.lastRunResult = null;
    renderSceneEditor();

    const r = await apiPost(`/racelink/api/scenes/${key}/run`, {});
    state.scenes.activeRunKey = null;
    if(r && r.result){
      state.scenes.lastRunResult = r.result;
      const summary = r.ok ? "OK" : (r.result.error || "failed");
      setScenesHint(`Run "${key}": ${summary}`);
    }else{
      setScenesHint(`Run "${key}" failed: ${(r && r.error) || "unknown error"}`);
    }
    // Drop the per-row live state on completion — lastRunResult drives the
    // borders from here on, identical to pre-R7 behaviour.
    state.scenes.actionStatus = [];
    renderSceneEditor();
  }

  // R7: live progress handler installed for racelink.js's SSE listener.
  // Filtered by activeRunKey so a parallel run from another tab doesn't
  // colour rows on this tab (the user there hasn't clicked Run).
  window.__rlSceneProgress = (payload) => {
    if(!payload || payload.scene_key !== state.scenes.activeRunKey) return;
    const idx = Number(payload.index);
    if(!Number.isFinite(idx)) return;
    if(!Array.isArray(state.scenes.actionStatus)){
      state.scenes.actionStatus = [];
    }
    state.scenes.actionStatus[idx] = payload.status;
    const editor = document.getElementById("sceneEditor");
    if(!editor) return;
    const row = editor.querySelector(`[data-action-idx="${idx}"]`);
    if(!row) return;
    row.classList.remove("running", "ok", "error", "degraded");
    if(payload.status){
      row.classList.add(String(payload.status));
    }
  };

  // Exposed for racelink.js's SSE refresh handler — fires when the SCENES
  // topic arrives (CRUD on another tab / RH plugin etc.).
  window.__rlScenesRefresh = async () => {
    await loadScenes();
    renderSceneList();
    if(state.scenes.selectedKey){
      const fresh = state.scenes.items.find(s => s.key === state.scenes.selectedKey);
      if(fresh && state.scenes.draft && state.scenes.draft.key === state.scenes.selectedKey){
        try{
          const sameAsDraft = JSON.stringify(fresh.actions) === JSON.stringify(state.scenes.draft.actions || [])
                            && fresh.label === state.scenes.draft.label;
          if(sameAsDraft){
            state.scenes.draft = cloneAction(fresh);
            renderSceneEditor();
          }
        }catch{
          // ignore
        }
      }else if(!fresh){
        state.scenes.selectedKey = null;
        state.scenes.draft = null;
        renderSceneEditor();
      }
    }
  };

  // ---- bootstrap (page-load) ------------------------------------------

  async function init(){
    setScenesHint("");
    await Promise.all([ensureScenesSchema(), loadScenes(), loadGroupsAndDevicesForTargetPicker()]);
    if(!state.scenes.selectedKey && state.scenes.items.length){
      state.scenes.selectedKey = state.scenes.items[0].key;
    }
    const sel = state.scenes.items.find(s => s.key === state.scenes.selectedKey) || null;
    state.scenes.draft = sel ? cloneAction(sel) : null;
    renderSceneList();
    renderSceneEditor();
  }

  document.addEventListener("DOMContentLoaded", () => {
    init().catch(e => {
      console.error("[scenes] init failed", e);
      setScenesHint("Initialisation failed — check the console.");
    });

    const newBtn = $("#btnSceneNew");
    if(newBtn){
      newBtn.addEventListener("click", () => {
        newSceneDraft();
        setScenesHint("New scene — enter label and add actions, then Create.");
      });
    }
    const rlPresetsLink = $("#btnSceneOpenRlPresets");
    if(rlPresetsLink){
      rlPresetsLink.addEventListener("click", () => {
        // The dlgRlPresets dialog is rendered on this page too (see scenes.html);
        // its open-handler lives in racelink.js and is bound to ``btnRlPresets``.
        const btnRl = $("#btnRlPresets");
        if(btnRl) btnRl.click();
      });
    }
  });

  // If DOM already parsed at script-eval time, fire init right away.
  if(document.readyState === "interactive" || document.readyState === "complete"){
    init().catch(e => console.error("[scenes] late init failed", e));
  }
})();
