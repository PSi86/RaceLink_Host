(function(){
  const $ = (sel, ctx=document) => ctx.querySelector(sel);
  const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));
  const fmt = {
    bool: v => v ? "Yes" : "No",
    num: v => (v===null || v===undefined || isNaN(v)) ? "" : String(v),
  };
  let state = {
    groups: [],
    devices: [],
    selGroupId: null,
    sortKey: null,
    sortDir: 1, // 1 asc, -1 desc
    selected: new Set(),
  };

  async function apiGet(url){
    const res = await fetch(url, {credentials:"same-origin"});
    return await res.json();
  }
  async function apiPost(url, body){
    const res = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body||{}), credentials:"same-origin"});
    return await res.json();
  }

  // Load initial data
  async function loadAll(){
    const [g, d] = await Promise.all([apiGet("/gatecontrol/api/groups"), apiGet("/gatecontrol/api/devices")]);
    state.groups = (g.groups||[]);
    state.devices = (d.devices||[]);
    if(state.selGroupId===null && state.groups.length>0){ state.selGroupId = state.groups[0].id; }
    renderGroups();
    renderBulkGroup();
    renderTable();
  }

  function renderGroups(){
    const ul = $("#gcGroups");
    ul.innerHTML = "";
    state.groups.forEach(gr => {
      const li = document.createElement("li");
      li.className = (gr.id===state.selGroupId) ? "active" : "";
      li.innerHTML = `<span>${gr.name}</span> <span class="count">${gr.device_count||0}</span>`;
      li.addEventListener("click", () => { state.selGroupId = gr.id; renderGroups(); renderTable(); });
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
    // filter by group (show all if selected is static 0? we still filter)
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
        <td>${Number(r.state) ? '<span class="tag ok">On</span>' : '<span class="tag off">Off</span>'}</td>
        <td>${fmt.num(r.effect)}</td>
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
      cb.addEventListener("change", e => {
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

  // Buttons
  $("#btnSave").addEventListener("click", async ()=>{ await apiPost("/gatecontrol/api/save",{}); });
  $("#btnReload").addEventListener("click", async ()=>{ await apiPost("/gatecontrol/api/reload",{}); await loadAll(); });
  $("#btnForce").addEventListener("click", async ()=>{ await apiPost("/gatecontrol/api/groups/force",{}); });
  $("#btnStatusSel").addEventListener("click", async ()=>{
    const macs = Array.from(state.selected);
    if(macs.length===0) return;
    await apiPost("/gatecontrol/api/status", {macs});
    await loadAll();
  });
  $("#btnStatusAll").addEventListener("click", async ()=>{
    await apiPost("/gatecontrol/api/status", {});
    await loadAll();
  });

  $("#btnBulkSetGroup").addEventListener("click", async ()=>{
    const macs = Array.from(state.selected);
    const gid = Number($("#bulkGroup").value);
    if(macs.length===0) return;
    await apiPost("/gatecontrol/api/devices/update-meta", {macs, groupId: gid});
    await loadAll();
  });

  // Discover modal
  const dlg = $("#dlgDiscover");
  $("#btnDiscover").addEventListener("click", ()=> dlg.showModal());
  $("#btnDiscoverStart").addEventListener("click", async (e)=>{
    e.preventDefault();
    const targetGroupId = Number($("#discoverGroup").value);
    const newGroupName = ($("#discoverNewGroup").value || "").trim() || null;
    const res = await apiPost("/gatecontrol/api/discover", {targetGroupId, newGroupName});
    $("#discoverResult").textContent = res.ok ? `Found: ${res.found}` : (res.error||"Error");
    await loadAll();
  });

  // Select all
  $("#selAll").addEventListener("change", (e)=>{
    const c = e.target.checked;
    state.selected.clear();
    $$("#gcBody input[type=checkbox]").forEach(cb => { cb.checked = c; if(c) state.selected.add(cb.getAttribute("data-mac")); });
  });

  // New group
  $("#btnNewGroup").addEventListener("click", async ()=>{
    const name = prompt("New group name:");
    if(!name) return;
    const r = await apiPost("/gatecontrol/api/groups/create", {name});
    if(r.ok){ await loadAll(); }
  });

  // Auto refresh every 20s (lightweight)
  setInterval(async ()=>{
    await apiPost("/gatecontrol/api/status", {groupId: state.selGroupId});
    const d = await apiGet("/gatecontrol/api/devices");
    state.devices = d.devices||[];
    renderTable();
  }, 10000);

  loadAll().catch(console.error);
})();