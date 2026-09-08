"use strict";
const $ = (id) => document.getElementById(id);
const state = {section:"movement", client:"all", filter:"exceptions", search:"", carrier:"all", page:1, data:null, selected:null};
const PAGE_SIZE = 10;
const tierNames = {critical:"Critical",urgent:"Urgent",watch:"Watch",monitoring:"Monitoring",delivered:"Delivered",cancelled:"Cancelled",data_gap:"Missing data"};
const statusNames = {pre_transit:"Label created",in_transit:"In transit",out_for_delivery:"Out for delivery",available_for_pickup:"Ready for pickup",delivered:"Delivered",unknown:"Unknown",failure:"Carrier exception",return_to_sender:"Returning",cancelled:"Cancelled"};
const caseNames = {open:"Not started",investigating:"Investigating",carrier_contacted:"Carrier contacted"};
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const date = (v, full=false) => v ? new Date(v).toLocaleString(undefined, full ? {month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit"} : {month:"short",day:"numeric"}) : "Not available";
const carrierName = c => c === "fedex" ? "FedEx" : c.toUpperCase();
const clientName = id => state.data?.clients.find(c=>c.id===id)?.name || id;
const exception = r => ["watch","urgent","critical"].includes(r.tier);
function toast(message){$("toast").textContent=message;$("toast").hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$("toast").hidden=true,5000)}
function clientRows(){return state.data.shipments.filter(r=>state.client==="all" || r.client_id===state.client)}
function filtered(){
  const q=state.search.toLowerCase();
  return clientRows().filter(r=>(state.carrier==="all" || r.carrier===state.carrier) && (!q || `${r.order_number} ${r.tracking_number} ${clientName(r.client_id)}`.toLowerCase().includes(q)))
  .filter(r=>state.filter==="all" || state.filter==="exceptions" && exception(r) || state.filter==="never" && r.never_scanned && !["delivered","cancelled"].includes(r.tier) || state.filter===r.tier)
  .sort((a,b)=> (a.tier==="delivered" || a.tier==="cancelled")-(b.tier==="delivered" || b.tier==="cancelled") || (b.days??-1)-(a.days??-1) || a.id-b.id);
}
function render(){
  if(!state.data)return;
  const rows=clientRows();
  $("count-exceptions").textContent=rows.filter(exception).length;
  for(const tier of ["watch","urgent","critical"])$("count-"+tier).textContent=rows.filter(r=>r.tier===tier).length;
  $("nav-count").textContent=rows.filter(exception).length;
  document.querySelector('[data-filter="data_gap"]').textContent="Missing data ("+rows.filter(r=>r.tier==="data_gap").length+")";
  document.querySelectorAll("[data-tier]").forEach(el=>{el.classList.toggle("selected",state.filter===el.dataset.tier);el.setAttribute("aria-pressed",state.filter===el.dataset.tier)});
  document.querySelectorAll("[data-filter]").forEach(el=>{el.classList.toggle("active",state.filter===el.dataset.filter);el.setAttribute("aria-pressed",state.filter===el.dataset.filter)});
  const found=filtered(), pages=Math.max(1,Math.ceil(found.length/PAGE_SIZE));
  state.page=Math.min(state.page,pages);
  const start=(state.page-1)*PAGE_SIZE, page=found.slice(start,start+PAGE_SIZE);
  $("queue-total").textContent=found.length;
  $("queue-description").textContent=`${state.filter==="all"?"All shipments, oldest open cases first":"Oldest matching shipments first"} · ${state.client==="all"?"All 5 clients":clientName(state.client)}`;
  $("shipment-rows").innerHTML=page.length?page.map(r=>`<tr><td><strong>${esc(r.order_number||"Order not linked")}</strong><small>${esc(clientName(r.client_id))}</small></td><td><span class="tracking">${esc(r.tracking_number)}</span><small>${esc(carrierName(r.carrier))} · ${esc(r.fulfillment_status)}</small></td><td><span class="badge ${esc(r.carrier_status)}">${esc(statusNames[r.carrier_status]||r.carrier_status)}</span></td><td>${r.last_movement_at?esc(date(r.last_movement_at)):"<span class=\"muted\">No scan recorded</span>"}<small>${r.last_movement_at?"Last physical scan":r.shipped_at?"Shipped "+esc(date(r.shipped_at)):r.label_created_at?"Label "+esc(date(r.label_created_at)):"Date needed"}</small></td><td>${["delivered","cancelled","data_gap"].includes(r.tier)?"":`<span class="age">${r.days}d</span>`}<span class="badge ${r.tier}">${tierNames[r.tier]}</span></td><td><span class="badge">${esc(caseNames[r.case_status]||"Not started")}</span></td><td><button class="row-open" data-detail="${r.id}" aria-label="View shipment ${esc(r.order_number||r.tracking_number)}">↗</button></td></tr>`).join(""):`<tr><td colspan="7" class="empty-cell"><strong>No shipments match this view</strong>${rows.length?"Try another filter, search, or client store.":"Import this client's shipments after live tracking is configured."}</td></tr>`;
  $("showing").textContent=found.length?`Showing ${start+1}–${Math.min(start+PAGE_SIZE,found.length)} of ${found.length} shipments`:"0 shipments";
  $("page-number").textContent=`${state.page} / ${pages}`;$("previous").disabled=state.page===1;$("next").disabled=state.page===pages;
  $("monitoring-count").textContent=`${rows.filter(r=>r.tier==="monitoring").length} under 5 days · ${rows.filter(r=>r.tier==="delivered").length} delivered`;
  document.querySelectorAll(".selected-client-name").forEach(el=>el.textContent=state.client==="all"?"all clients":clientName(state.client));
}
function section(name){
  state.section=name;
  const title={movement:"No Movement",inventory:"Inventory",invoices:"Invoices"}[name];
  document.querySelectorAll("[data-section]").forEach(el=>{el.classList.toggle("active",el.dataset.section===name);el.setAttribute("aria-current",el.dataset.section===name?"page":"false")});
  for(const key of ["movement","inventory","invoices"])$(key+"-section").hidden=key!==name;
  $("movement-actions").hidden=name!=="movement";$("crumb").textContent=title;
  $("title").innerHTML=esc(title)+'<span class="title-dot"></span>';
  $("eyebrow").textContent=name==="movement"?"SHIPMENT EXCEPTIONS":"CLIENT OPERATIONS";
  $("subtitle").textContent={movement:"Catch stalled shipments before your customers do.",inventory:"Stock visibility, organized by client.",invoices:"Client billing, in one place."}[name];
  render();
}
function trackingUrl(row){
  if(state.data.mode==="demo")return null;
  const code=encodeURIComponent(row.tracking_number);
  return {usps:`https://tools.usps.com/go/TrackConfirmAction?tLabels=${code}`,ups:`https://www.ups.com/track?tracknum=${code}`,fedex:`https://www.fedex.com/fedextrack/?trknbr=${code}`}[row.carrier]||null;
}
function details(id){
  const r=state.data.shipments.find(r=>r.id===id);if(!r)return;state.selected=id;
  const link=trackingUrl(r), demo=state.data.mode==="demo";
  $("detail-content").innerHTML=`<div class="detail-title"><h2>${esc(r.order_number||"Unlinked order")}</h2><span class="badge ${r.tier}">${tierNames[r.tier]}</span></div><div class="detail-client">${esc(clientName(r.client_id))} · ${esc(carrierName(r.carrier))}</div><div class="tracking">${esc(r.tracking_number)}</div><div class="detail-summary"><span class="eyebrow">${r.tier==="delivered"?"CARRIER CONFIRMED":"SHIPMENT EVIDENCE"}</span><strong>${r.tier==="delivered"?"Delivered":r.days===null?"Date needed":r.days+" days without movement"}</strong><p>${esc(r.reason)}</p>${r.anchor_at?`<span class="muted">Clock starts from: ${esc(r.anchor_source)} · ${esc(date(r.anchor_at,true))}</span>`:""}</div><dl class="detail-grid"><div><dt>Order fulfillment</dt><dd>${esc(r.fulfillment_status)}</dd></div><div><dt>Carrier status</dt><dd>${esc(statusNames[r.carrier_status]||r.carrier_status)}</dd></div><div><dt>Shipped</dt><dd>${esc(date(r.shipped_at,true))}</dd></div><div><dt>Latest update received</dt><dd>${esc(date(r.last_received_at,true))}</dd></div></dl><div class="setup-note">${demo?"Sample shipment — this tracking number is illustrative.":"Latest update received is not a fresh carrier lookup. Missing or delayed feeds require reconciliation."}</div>${link?`<a class="button secondary" href="${esc(link)}" target="_blank" rel="noopener noreferrer">Open carrier tracking ↗</a>`:""}<h3 class="detail-section-title">Carrier event history</h3><ul class="timeline">${[...(r.events||[])].reverse().map(e=>`<li>${esc(e.description)}<small>${esc(date(e.at,true))}${e.movement?" · Physical scan":" · Does not reset movement clock"}</small></li>`).join("")||"<li>No scan history available. Import the shipping date and connect tracking updates.</li>"}</ul><h3 class="detail-section-title">Follow-up</h3><label class="field-label" for="case-status">Case status</label><select id="case-status" ${demo?"disabled":""}>${Object.entries(caseNames).map(([key,value])=>`<option value="${key}" ${r.case_status===key?"selected":""}>${value}</option>`).join("")}</select><label class="field-label" for="case-notes">Notes</label><textarea id="case-notes" maxlength="4000" ${demo?"disabled":""} placeholder="Carrier case number, last contact, next action…">${esc(r.notes)}</textarea><p class="muted">${demo?"Notes and follow-up are read-only in the sample workspace.":"Follow-up does not clear an aging exception. Carrier evidence determines priority."}</p><div class="dialog-actions"><button id="save-case" class="button primary" ${demo?"disabled":""}>Save follow-up</button></div>`;
  if(!demo)$("save-case").addEventListener("click",saveCase);
  if(!$("detail-dialog").open)$("detail-dialog").showModal();
}
async function api(url,options={}){
  const response=await fetch(url,{...options,headers:{"Content-Type":"application/json","X-CSRF-Token":document.querySelector('meta[name="csrf-token"]').content,...options.headers}});
  let body;try{body=await response.json()}catch{throw new Error("The server returned an unexpected response. Please try again.")}
  if(!response.ok)throw new Error(body.error||"Request failed.");return body;
}
async function saveCase(){
  $("save-case").disabled=true;
  try{await api(`/api/shipments/${state.selected}`,{method:"PATCH",body:JSON.stringify({case_status:$("case-status").value,notes:$("case-notes").value})});await load();details(state.selected);toast("Follow-up saved.")}catch(e){toast(e.message);$("save-case").disabled=false}
}
async function load(){
  try{
    const first=!state.data;state.data=await api("/api/workspace");$("load-error").hidden=true;
    if(first){
      for(const c of state.data.clients){$("client-select").add(new Option(c.name,c.id));$("import-client").add(new Option(c.name,c.id))}
      const known=new Set(state.data.clients.map(c=>c.id));
      try{const saved=localStorage.getItem("shipmode-client");if(known.has(saved)||saved==="all")state.client=saved}catch{}
      $("client-select").value=state.client;
    }
    if(state.data.mode==="live"){
      $("mode-notice").querySelector("strong").textContent="Live workspace";
      $("mode-notice").querySelector("div span").textContent="Check each client’s tracking feed before relying on coverage.";
      $("side-connection").textContent="Verify client feeds";
    }
    $("as-of").textContent=(state.data.mode==="demo"?"Sample data · ":"Queue calculated · ")+date(state.data.as_of,true);
    render();
  }catch(e){$("load-error").textContent=e.message;$("load-error").hidden=false;$("as-of").textContent="Update failed — data may be stale";if(!state.data)$("shipment-rows").innerHTML='<tr><td colspan="7" class="empty-cell">Unable to load shipments. Refresh to retry.</td></tr>'}
}
function exportQueue(){
  if(!state.data)return;
  const columns=["client","order_number","tracking_number","carrier","carrier_status","fulfillment_status","shipped_at","last_movement_at","days_without_movement","priority","case_status"];
  const cell=v=>'"'+String(v??"").replace(/^[=+@\-\t\r]/,"'$&").replace(/"/g,'""')+'"';
  const data=[columns,...filtered().map(r=>[clientName(r.client_id),r.order_number,r.tracking_number,r.carrier,r.carrier_status,r.fulfillment_status,r.shipped_at,r.last_movement_at,r.days,r.tier,r.case_status])];
  const url=URL.createObjectURL(new Blob([data.map(row=>row.map(cell).join(",")).join("\r\n")],{type:"text/csv;charset=utf-8"}));
  const a=document.createElement("a");a.href=url;a.download=(state.data.mode==="demo"?"SAMPLE-":"")+"shipmode-no-movement.csv";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
document.querySelectorAll("[data-section]").forEach(el=>el.addEventListener("click",()=>section(el.dataset.section)));
document.querySelectorAll("[data-tier],[data-filter]").forEach(el=>el.addEventListener("click",()=>{state.filter=el.dataset.tier||el.dataset.filter;state.page=1;render()}));
$("client-select").addEventListener("change",e=>{state.client=e.target.value;state.page=1;try{localStorage.setItem("shipmode-client",state.client)}catch{}render()});
$("search").addEventListener("input",e=>{state.search=e.target.value;state.page=1;render()});
$("carrier-filter").addEventListener("change",e=>{state.carrier=e.target.value;state.page=1;render()});
$("previous").addEventListener("click",()=>{state.page--;render()});$("next").addEventListener("click",()=>{state.page++;render()});
$("shipment-rows").addEventListener("click",e=>{const button=e.target.closest("[data-detail]");if(button)details(Number(button.dataset.detail))});
for(const id of ["setup-button","connection-details"])$(id).addEventListener("click",()=>$("setup-dialog").showModal());
document.querySelectorAll(".close-dialog").forEach(el=>el.addEventListener("click",()=>el.closest("dialog").close()));
$("export-button").addEventListener("click",exportQueue);
  $("import-button").addEventListener("click",()=>{if(!state.data||state.data.mode==="demo"){$("setup-dialog").showModal();return}if(state.client!=="all")$("import-client").value=state.client;$("import-result").textContent="";$("import-dialog").showModal()});
$("confirm-import").addEventListener("click",async()=>{
  const file=$("import-file").files[0];if(!file){$("import-result").textContent="Choose a CSV file.";return}
  if(file.size>5*1024*1024){$("import-result").textContent="File exceeds 5 MB.";return}
  $("confirm-import").disabled=true;
  try{const result=await api("/api/import",{method:"POST",body:JSON.stringify({client_id:$("import-client").value,csv:await file.text()})});$("import-result").textContent=`${result.inserted} added, ${result.updated} updated.`;await load()}catch(e){$("import-result").textContent=e.message}finally{$("confirm-import").disabled=false}
});
load();setInterval(()=>{if(!document.hidden && !$("detail-dialog").open && !$("import-dialog").open)load()},60000);
