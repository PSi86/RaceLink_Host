(function(){
  const $ = (sel, ctx=document) => ctx.querySelector(sel);
  const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));

  const fmt = {
    num: v => (v===null || v===undefined || isNaN(v)) ? "" : String(v),
    hex2: v => ("0" + (Number(v) & 0xFF).toString(16).toUpperCase()).slice(-2),
  };

  // Flag bits (must match firmware; kept local for UI only)
  const GC_FLAG_POWER_ON    = 0x01;
  const GC_FLAG_ARM_ON_SYNC = 0x02;
  const GC_FLAG_HAS_BRI     = 0x04;

  let state = {
    groups: [],
    devices: [],
    selGroupId: null,
    sortKey: null,
    sortDir: 1,
    selected: new Set(),
    busy: false,
    lastTask: null,
    lastMaster: null,
  };

  async function apiGet(url){
    const res = await fetch(url, {credentials:"same-origin"});
    const j = await res.json().catch(()=>({ok:false,error:"Bad JSON"}));
    j.__status = res.status;
    return j;
  }
  async function apiPost(url, body){
    const res = await fetch(url, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body||{}),
      credentials:"same-origin"
    });
    const j = await res.json().catch(()=>({ok:false,error:"Bad JSON"}));
    j.__status = res.status;
    return j;
  }

  function setBusy(isBusy){
    state.busy = !!isBusy;
    const disable = state.busy;

    // header action buttons
    $$(".gc-actions button").forEach(b => b.disabled = disable);
    // group creation + bulk
    $("#btnNewGroup").disabled = disable;
    $("#btnBulkSetGroup").disabled = disable;

    // allow closing modal even when busy
    $("#btnDiscoverStart").disabled = disable;
  }

  function flagsLabel(flags){
    const f = Number(flags) & 0xFF;
    const parts = [];
    if(f & GC_FLAG_POWER_ON) parts.push("PWR");
    if(f & GC_FLAG_ARM_ON_SYNC) parts.push("ARM");
    if(f & GC_FLAG_HAS_BRI) parts.push("BRI");
    const p = parts.length ? parts.join("+") : "-";
    return `0x${fmt.hex2(f)} ${p}`;
  }

  function powerTag(flags){
    return (Number(flags) & GC_FLAG_POWER_ON)
      ? '<span class="tag ok">On</span>'
      : '<span class="tag off">Off</span>';
  }

  // Load initial data (and on refresh)
  async function loadGroups(){
    const g = await apiGet("/gatecontrol/api/groups");
    state.groups = (g.groups||[]);
    if(state.selGroupId===null && state.groups.length>0){ state.selGroupId = state.groups[0].id; }
    renderGroups();
    renderBulkGroup();
  }

  async function loadDevices(){
    const d = await apiGet("/gatecontrol/api/devices");
    state.devices = (d.devices||[]);
    renderTable();
  }

  async function loadAll(){
    await Promise.all([loadGroups(), loadDevices()]);
  }

  function renderGroups(){
    const ul = $("#gcGroups");
    ul.innerHTML = "";
    state.groups.forEach(gr => {
      const li = document.createElement("li");
      li.className = (gr.id===state.selGroupId) ? "active" : "";
      li.innerHTML = `<span>${gr.name}</span> <span class="count">${gr.device_count||0}</span>`;
      li.addEventListener("click", () => {
        state.selGroupId = gr.id;
        renderGroups();
        renderTable();
      });
      ul.appendChild(li);
    });
  }

  function renderBulkGroup(){
    const sel = $("#bulkGroup"), sel2 = $("#discoverGroup");
    sel.innerHTML = ""; sel2.innerHTML = "";
    state.groups.forEach(gr => {
      const o = document.createElement("option");
      o.value = gr.id; o.textContent = `${gr.id}: ${gr.name}`;
      sel.appendChild(o);
      const o2 = o.cloneNode(true);
      sel2.appendChild(o2);
    });
    if(state.selGroupId!==null){ sel.value = state.selGroupId; sel2.value = state.selGroupId; }
  }

  function renderTable(){
    const body = $("#gcBody");
    body.innerHTML = "";
    let rows = state.devices.slice();

    if(state.selGroupId!==null){
      rows = rows.filter(r => Number(r.groupId)===Number(state.selGroupId));
    }
    if(state.sortKey){
      const key = state.sortKey, dir = state.sortDir;
      rows.sort((a,b)=>{
        const av = (a[key] ?? ""); const bv = (b[key] ?? "");
        if(av < bv) return -dir;
        if(av > bv) return dir;
        return 0;
      });
    }

    rows.forEach(r => {
      const tr = document.createElement("tr");
      const checked = state.selected.has(r.addr);
      tr.innerHTML = `
        <td><input type="checkbox" ${checked?"checked":""} data-mac="${r.addr}"></td>
        <td>${r.name ?? ""}</td>
        <td class="mono">${r.addr ?? ""}</td>
        <td>${r.groupId}</td>
        <td>${powerTag(r.flags)} <span class="mono">${flagsLabel(r.flags)}</span></td>
        <td>${fmt.num(r.presetId)}</td>
        <td>${fmt.num(r.brightness)}</td>
        <td>${fmt.num(r.voltage_mV)}</td>
        <td>${fmt.num(r.node_rssi)}</td>
        <td>${fmt.num(r.node_snr)}</td>
        <td>${fmt.num(r.host_rssi)}</td>
        <td>${fmt.num(r.host_snr)}</td>
        <td>${r.version ?? ""}</td>
        <td>${r.caps ?? ""}</td>
        <td>${r.online ? '<span class="tag online">Online</span>' : ''}</td>
      `;
      body.appendChild(tr);
    });

    // Selection handlers
    $$("#gcBody input[type=checkbox]").forEach(cb => {
      cb.addEventListener("change", () => {
        const mac = cb.getAttribute("data-mac");
        if(cb.checked) state.selected.add(mac); else state.selected.delete(mac);
      });
    });
  }

  // Sorting
  $$("#gcTable thead th").forEach(th => {
    const key = th.getAttribute("data-key");
    if(!key) return;
    th.addEventListener("click", ()=>{
      if(state.sortKey===key) state.sortDir *= -1;
      else { state.sortKey = key; state.sortDir = 1; }
      renderTable();
    });
  });

  // Master/task UI
  function updateMaster(m){
    state.lastMaster = m;
    const pill = $("#masterPill");
    const detail = $("#masterDetail");

    const st = (m && m.state) ? String(m.state) : "IDLE";
    pill.textContent = st;
    pill.classList.remove("idle","tx","rx","err");
    if(st==="TX") pill.classList.add("tx");
    else if(st==="RX") pill.classList.add("rx");
    else if(st==="ERROR") pill.classList.add("err");
    else pill.classList.add("idle");

    const parts = [];
    if(m.tx_pending) parts.push("TX pending");
    if(m.rx_window_open) parts.push(`RX window ${m.rx_window_ms||0}ms`);
    if(m.last_rx_count_delta) parts.push(`ΔRX ${m.last_rx_count_delta}`);
    if(m.last_event) parts.push(`last: ${m.last_event}`);
    if(m.last_error) parts.push(`err: ${m.last_error}`);
    detail.textContent = parts.join(" · ");
  }

  function updateTask(t){
    state.lastTask = t;
    const el = $("#taskDetail");
    if(!t){
      el.textContent = "";
      setBusy(false);
      return;
    }
    const st = String(t.state||"");
    const name = String(t.name||"task");
    if(st === "running"){
      setBusy(true);
      const meta = t.meta || {};
      const mparts = [];
      if(meta.targetGroupId!==undefined && meta.targetGroupId!==null) mparts.push(`gid ${meta.targetGroupId}`);
      if(meta.selectionCount) mparts.push(`sel ${meta.selectionCount}`);
      if(meta.groupId!==undefined && meta.groupId!==null) mparts.push(`gid ${meta.groupId}`);
      const p = [
        `${name}…`,
        mparts.length ? `(${mparts.join(", ")})` : "",
        `replies ${t.rx_replies||0}`,
        `windows ${t.rx_windows||0}`,
        `Δ ${t.rx_count_delta_total||0}`,
      ].filter(Boolean).join(" ");
      el.textContent = p;
    } else {
      setBusy(false);
      const dur = (t.started_ts && t.ended_ts) ? Math.max(0, (t.ended_ts - t.started_ts)) : null;
      const tail = (dur!==null) ? `(${dur.toFixed(1)}s)` : "";
      const res = t.result ? JSON.stringify(t.result) : "";
      const err = t.last_error ? `err: ${t.last_error}` : "";
      el.textContent = [ `${name} ${st}`, tail, err || res ].filter(Boolean).join(" · ");

      // Discover modal helper
      if(name==="discover"){
        const r = t.result || {};
        if(r && typeof r === "object" && $("#discoverResult")){
          if(st==="done") $("#discoverResult").textContent = `Found: ${r.found ?? "?"}`;
          else if(st==="error") $("#discoverResult").textContent = `Error: ${t.last_error||"unknown"}`;
        }
      }
    }
  }

  // SSE connection
  function connectEvents(){
    try{
      const es = new EventSource("/gatecontrol/api/events", {withCredentials:true});
      es.addEventListener("master", (e)=>{ try{ updateMaster(JSON.parse(e.data)); }catch{} });
      es.addEventListener("task", (e)=>{ try{ updateTask(JSON.parse(e.data)); }catch{} });
      es.addEventListener("refresh", async (e)=>{
        try{
          const p = JSON.parse(e.data);
          const what = (p && p.what) ? p.what : ["groups","devices"];
          if(what.includes("groups")) await loadGroups();
          if(what.includes("devices")) await loadDevices();
        }catch{
          await loadAll();
        }
      });
      es.onerror = () => {
        // If SSE fails, do a one-shot fetch so UI isn't empty
        apiGet("/gatecontrol/api/master").then(r=>{
          if(r.master) updateMaster(r.master);
          if(r.task) updateTask(r.task);
        }).catch(()=>{});
      };
    }catch(e){
      console.warn("SSE not available", e);
    }
  }

  // Buttons
  $("#btnSave").addEventListener("click", async ()=>{
    const r = await apiPost("/gatecontrol/api/save",{});
    if(r.busy) return;
  });

  $("#btnReload").addEventListener("click", async ()=>{
    const r = await apiPost("/gatecontrol/api/reload",{});
    if(!r.busy) await loadAll();
  });

  $("#btnForce").addEventListener("click", async ()=>{
    const r = await apiPost("/gatecontrol/api/groups/force",{});
    if(r.busy) return;
  });

  $("#btnStatusSel").addEventListener("click", async ()=>{
    const macs = Array.from(state.selected);
    if(macs.length===0) return;
    const r = await apiPost("/gatecontrol/api/status", {selection: macs});
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  $("#btnStatusAll").addEventListener("click", async ()=>{
    const r = await apiPost("/gatecontrol/api/status", {});
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  $("#btnBulkSetGroup").addEventListener("click", async ()=>{
    const macs = Array.from(state.selected);
    const gid = Number($("#bulkGroup").value);
    if(macs.length===0) return;
    const r = await apiPost("/gatecontrol/api/devices/update-meta", {macs, groupId: gid});
    if(!r.busy) { /* refresh happens via SSE */ }
  });

  // Discover modal
  const dlg = $("#dlgDiscover");
  $("#btnDiscover").addEventListener("click", ()=>{
    $("#discoverResult").textContent = "";
    dlg.showModal();
  });

  $("#btnDiscoverStart").addEventListener("click", async (e)=>{
    e.preventDefault();
    const targetGroupId = Number($("#discoverGroup").value);
    const newGroupName = ($("#discoverNewGroup").value || "").trim() || null;
    $("#discoverResult").textContent = "Running…";
    const r = await apiPost("/gatecontrol/api/discover", {targetGroupId, newGroupName});
    if(r.busy){
      $("#discoverResult").textContent = `Busy: ${r.task?.name || "task"} is running`;
    }
  });

  // Select all
  $("#selAll").addEventListener("change", (e)=>{
    const c = e.target.checked;
    state.selected.clear();
    $$("#gcBody input[type=checkbox]").forEach(cb => {
      cb.checked = c;
      if(c) state.selected.add(cb.getAttribute("data-mac"));
    });
  });

  // New group
  $("#btnNewGroup").addEventListener("click", async ()=>{
    const name = prompt("New group name:");
    if(!name) return;
    const r = await apiPost("/gatecontrol/api/groups/create", {name});
    if(r.busy){
      alert(`Busy: ${r.task?.name || "task"} is running`);
    }
  });

  // Startup
  (async ()=>{
    await loadAll();
    connectEvents();

    // One-shot sync of master/task in case SSE is delayed
    const m = await apiGet("/gatecontrol/api/master");
    if(m.master) updateMaster(m.master);
    if(m.task) updateTask(m.task);
  })().catch(console.error);
})();