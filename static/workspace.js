"use strict";
const $ = (id) => document.getElementById(id);
const state = {section:"movement", client:"all", filter:"exceptions", search:"", carrier:"all", page:1, data:null, selected:null, inventorySources:null, inventoryAsOf:null, inventoryRequest:0, calculated:null, calculatedError:null, shopify:null, shopifyError:null, ssk:null, sskError:null, panelRequests:{}, shopifyShown:200, incoming:null, incomingError:null, incomingRequest:0, incomingHistory:false, orderRequest:0};
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
  $("queue-description").textContent=`${state.filter==="all"?"All shipments, oldest open cases first":"Oldest matching shipments first"} · ${state.client==="all"?"All 6 clients":clientName(state.client)}`;
  $("shipment-rows").innerHTML=page.length?page.map(r=>`<tr><td><strong>${esc(r.order_number||"Order not linked")}</strong><small>${esc(clientName(r.client_id))}</small></td><td><span class="tracking">${esc(r.tracking_number)}</span><small>${esc(carrierName(r.carrier))} · ${esc(r.fulfillment_status)}</small></td><td><span class="badge ${esc(r.carrier_status)}">${esc(statusNames[r.carrier_status]||r.carrier_status)}</span></td><td>${r.last_movement_at?esc(date(r.last_movement_at)):"<span class=\"muted\">No scan recorded</span>"}<small>${r.last_movement_at?"Last physical scan":r.shipped_at?"Shipped "+esc(date(r.shipped_at)):r.label_created_at?"Label "+esc(date(r.label_created_at)):"Date needed"}</small></td><td>${["delivered","cancelled","data_gap"].includes(r.tier)?"":`<span class="age">${r.days}d</span>`}<span class="badge ${r.tier}">${tierNames[r.tier]}</span></td><td><span class="badge">${esc(caseNames[r.case_status]||"Not started")}</span></td><td><button class="row-open" data-detail="${r.id}" aria-label="View shipment ${esc(r.order_number||r.tracking_number)}">↗</button></td></tr>`).join(""):`<tr><td colspan="7" class="empty-cell"><strong>No shipments match this view</strong>${rows.length?"Try another filter, search, or client store.":"Import this client's shipments after live tracking is configured."}</td></tr>`;
  $("showing").textContent=found.length?`Showing ${start+1}–${Math.min(start+PAGE_SIZE,found.length)} of ${found.length} shipments`:"0 shipments";
  $("page-number").textContent=`${state.page} / ${pages}`;$("previous").disabled=state.page===1;$("next").disabled=state.page===pages;
  $("monitoring-count").textContent=`${rows.filter(r=>r.tier==="monitoring").length} under 5 days · ${rows.filter(r=>r.tier==="delivered").length} delivered`;
  document.querySelectorAll(".selected-client-name").forEach(el=>el.textContent=state.client==="all"?"all clients":clientName(state.client));
  if(state.section==="inventory")renderInventory();
}
function renderInventory(){
  const sources=state.inventorySources||[], loaded=sources.filter(s=>!s.error), selected=state.client==="all"?null:loaded[0];
  const labels=selected?["Products tracked","Units on hand","Need ordering","Out of stock"]:["Workbooks loaded","Products listed","Workbooks in review","Source errors"];
  ["products","remaining","reorder","out"].forEach((key,i)=>$("inventory-label-"+key).textContent=labels[i]);
  const values=selected?[selected.summary.products,selected.summary.on_hand,selected.summary.reorder,selected.summary.out]:[
    `${loaded.length}/${sources.length||6}`,loaded.reduce((n,s)=>n+s.rows.length,0),
    sources.filter(s=>s.report_status==="REVIEW"||s.warnings?.length).length,
    sources.filter(s=>s.error).length];
  ["products","remaining","reorder","out"].forEach((key,i)=>$("inventory-"+key).textContent=state.inventorySources?String(values[i]??"—"):"—");
  $("inventory-status").textContent=selected?selected.report_status:"Source values";
  $("inventory-description").textContent=selected?`Dashboard as of ${selected.as_of||"unknown"} · read ${date(selected.fetched_at,true)}`:"Values are read from each client's Dashboard tab; select a client to see its source totals.";
  const rows=loaded.flatMap(source=>source.rows.map(row=>({...row,client:source.id,url:source.source_url,as_of:source.as_of,report_status:source.report_status,fetched_at:source.fetched_at})));
  $("inventory-rows").innerHTML=rows.length?rows.map(r=>`<tr><td><strong>${esc(r.product)}</strong><small>${esc(clientName(r.client))}</small></td>${["starting","shipped","remaining","demand","cover","status"].map(k=>`<td${(r.flags||[]).includes(k)?' class="source-flag" title="Pending, formula-error, or negative value in the source sheet"':""}>${esc(r[k]||"—")}</td>`).join("")}<td class="inventory-source"><span class="badge${r.report_status==="REVIEW"?" watch":""}">${esc(r.report_status)}</span><small>As of ${esc(r.as_of||"unknown")} · read ${esc(date(r.fetched_at,true))}${r.url?` · <a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Open sheet ↗</a>`:""}</small></td></tr>`).join(""):'<tr><td colspan="8" class="empty-cell"><strong>No sheet data available</strong>Check the connection status above.</td></tr>';
  const problems=sources.map(s=>({s,text:s.error||[s.report_status==="REVIEW"?"Report marked REVIEW":"",...(s.warnings||[])].filter(Boolean).join(" · ")})).filter(p=>p.text);
  $("inventory-error").innerHTML=problems.map(({s,text})=>`<div class="source-problem${s.error?" failed":""}"><strong>${esc(clientName(s.id))}${s.error?" — not loaded":""}</strong> ${esc(text)}</div>`).join("");
  $("inventory-error").hidden=!problems.length;
  updateDailyButton();
  $("inventory-sync").textContent=state.inventoryAsOf?`Checked ${date(state.inventoryAsOf,true)} · updates about every minute while this tab is open.`:"Google Sheets access is not connected yet.";
}
function renderCalculated(){
  const clients=state.calculated||[], fmt=n=>n==null?"—":Number(n).toLocaleString();
  const badge={VERIFIED:"delivered",REVIEW:"watch",INCOMPLETE:"data_gap"};
  const rows=clients.filter(c=>!c.error).flatMap(c=>c.skus.map(r=>({...r,client:c.client_id})));
  $("calculated-rows").innerHTML=rows.length?rows.map(r=>`<tr><td><strong>${esc(r.sku)} · ${esc(r.label)}</strong><small>${esc(clientName(r.client))}</small></td><td>${r.baseline?`${fmt(r.baseline.quantity)}<small>${esc(r.baseline.date)} · ${esc(r.baseline.timing.replace("_"," "))} · ${esc(r.baseline.approved_by)}</small>`:"—"}</td><td>${fmt(r.baseline?r.usage:null)}</td><td>${fmt(r.baseline?r.receipts:null)}</td><td>${fmt(r.baseline?r.adjustments:null)}</td><td><strong>${fmt(r.calculated)}</strong></td><td class="inventory-source"><span class="badge ${badge[r.status]||""}">${esc(r.status)}</span>${(r.reasons||[]).map(t=>`<small>${esc(t)}</small>`).join("")}</td></tr>`).join(""):(state.calculated===null?'<tr><td colspan="7" class="empty-cell"><strong>Loading…</strong>Calculating.</td></tr>':'<tr><td colspan="7" class="empty-cell"><strong>No calculated inventory</strong>'+esc(state.calculatedError||"No client has rules and a readable export yet.")+'</td></tr>');
  const problems=clients.filter(c=>c.error).map(c=>`<div class="source-problem failed"><strong>${esc(clientName(c.client_id))}</strong> ${esc(c.error)}</div>`);
  $("calculated-error").innerHTML=problems.join("");$("calculated-error").hidden=!problems.length;
  $("calculated-eod").innerHTML=clients.filter(c=>c.latest_eod).map(c=>{const e=c.latest_eod;return `<div class="eod-card"><strong>${esc(clientName(c.client_id))} — latest EOD ${esc(e.date)}</strong><span>${fmt(e.orders)} orders · ${fmt(e.missions)} missions · ${fmt(e.total_units)} units${e.needs_review?" · needs review":""}</span><small>${esc(e.audit)}</small>${(e.findings||[]).slice(0,5).map(f=>`<small class="finding">${esc(f)}</small>`).join("")}</div>`}).join("");
  const counts=rows.reduce((m,r)=>(m[r.status]=(m[r.status]||0)+1,m),{});
  $("calculated-status").textContent=rows.length?Object.entries(counts).map(([k,v])=>`${v} ${k.toLowerCase()}`).join(" · "):"Read-only";
}
const incomingFlagNames={missing_boxes:"Missing boxes",receipt_not_recorded:"Receipt not recorded",needs_transfer:"Needs transfer",sku_unverified:"SKU not verified",past_expected:"Past expected date"};
function incomingCard(group){
  const flags=group.flags.map(f=>`<span class="badge watch">${esc(incomingFlagNames[f]||f)}</span>`).join(" ");
  const lines=group.lines.map(l=>`<tr><td><strong>${esc(l.product||"Product not named")}</strong><small>${esc(l.sku||"SKU not set")}</small></td><td>${esc(l.units||"—")}</td><td>${esc(l.boxes_expected||"—")} / ${esc(l.boxes_received||"—")}</td><td>${esc(l.units_received||"—")}</td><td>${esc(l.status||"—")}${l.flags.length?`<small>${l.flags.map(f=>esc(incomingFlagNames[f]||f)).join(" · ")}</small>`:""}</td></tr>`).join("");
  const first=group.lines[0]||{};
  return `<article class="incoming-shipment"><header><div><strong>${esc(group.po)}</strong><span class="tracking">${esc(group.tracking||"No tracking number")}</span></div><div>${flags}</div></header><p class="incoming-where">${esc(first.where||"Location not recorded")}${first.expected_date?` · Expected in Miami: ${esc(first.expected_date)}`:""}</p><div class="table-scroll"><table><thead><tr><th>Product / SKU</th><th>Units expected</th><th>Boxes expected / received</th><th>Units received</th><th>Status</th></tr></thead><tbody>${lines}</tbody></table></div></article>`;
}
function renderIncoming(){
  const source=state.incoming, fmt=n=>Number(n).toLocaleString();
  $("incoming-error").hidden=!state.incomingError&&!source?.error;
  $("incoming-error").textContent=state.incomingError||source?.error||"";
  $("incoming-history-toggle").hidden=true;$("incoming-history").hidden=true;$("incoming-totals").innerHTML="";
  if(state.client==="all"){$("incoming-status").textContent="Read-only";$("incoming-shipments").innerHTML='<p class="empty-cell"><strong>Select one client</strong>Incoming shipments are shown one client at a time.</p>';return}
  if(!source||source.error){$("incoming-status").textContent="Read-only";$("incoming-shipments").innerHTML=source?"":'<p class="empty-cell"><strong>Loading incoming shipments…</strong></p>';return}
  if(!source.available){$("incoming-status").textContent="Not available";$("incoming-shipments").innerHTML='<p class="empty-cell"><strong>No Incoming Stocks tab</strong>This client\'s workbook does not track incoming shipments yet.</p>';return}
  $("incoming-status").textContent=`${source.shipments.length} open`+(source.truncated?" · INCOMPLETE":"");
  if(source.truncated){$("incoming-error").hidden=false;$("incoming-error").textContent="The Incoming Stocks tab is longer than ShipMode reads; later rows are not shown."}
  $("incoming-totals").innerHTML=source.incoming_by_sku.map(t=>`<div><span>${esc(t.sku)} · ${esc(t.product)}</span><strong>${fmt(t.units)}</strong><small>incoming, not in on-hand</small></div>`).join("")+(source.unverified_lines?`<p class="incoming-note">${source.unverified_lines} line(s) have no verified SKU or no whole-number quantity and are not included in these totals.</p>`:"");
  $("incoming-shipments").innerHTML=source.shipments.length?source.shipments.map(incomingCard).join(""):'<p class="empty-cell"><strong>No open incoming shipments</strong>Everything listed in the tab is already included in the latest count.</p>';
  $("incoming-history-toggle").hidden=!source.history.length;
  $("incoming-history-toggle").textContent=state.incomingHistory?"Hide received history":`Show received history (${source.history.length})`;
  $("incoming-history-toggle").setAttribute("aria-expanded",String(state.incomingHistory));
  $("incoming-history").hidden=!state.incomingHistory;
  $("incoming-history").innerHTML=state.incomingHistory?source.history.map(incomingCard).join(""):"";
}
async function loadIncoming(){
  const request=++state.incomingRequest,client=state.client;
  state.incoming=null;state.incomingError=null;
  if(client==="all"){renderIncoming();return}
  renderIncoming();
  try{const result=await api(`/api/incoming?client_id=${encodeURIComponent(client)}`);if(request!==state.incomingRequest)return;state.incoming=result.source}
  catch(error){if(request!==state.incomingRequest)return;state.incomingError=error.message}
  renderIncoming();
}
async function openDailyUpdate(){
  const button=$("daily-update-button"),client=state.client;button.disabled=true;
  try{
    const result=await api(`/api/daily-update?client_id=${encodeURIComponent(client)}`);
    if(client!==state.client)return;  // the client changed while loading: never show another client's text
    $("update-text").value=result.text;
    const notes=[result.draft?"The source is under review: the text starts with a DRAFT line. Check the Sheet before posting.":"",result.incoming_error?`Incoming shipments were left out: ${result.incoming_error}`:"",result.held_back?.length?`Not included (fully or partly) because no verified SKU or quantity: ${result.held_back.join(", ")}. Check them in the Incoming panel.`:"",result.incoming_truncated?"The Incoming Stocks tab is longer than ShipMode reads; later shipments may be missing from this text.":""].filter(Boolean);
    $("update-warning").textContent=notes.join(" ");$("update-warning").hidden=!notes.length;
    $("update-read-at").textContent=`Built from the Sheet as read ${result.sheet_read_at?date(result.sheet_read_at,true):"just now"}.`;
    $("update-dialog").showModal();
    loadInventory();  // the panel behind the dialog shows the same snapshot the text was built from
  }catch(error){toast(error.message)}
  finally{updateDailyButton()}
}
async function copyDailyUpdate(){
  const text=$("update-text").value;
  try{await navigator.clipboard.writeText(text);toast("Update copied. Paste it into the client's channel.")}
  catch{$("update-text").select();toast("Select-all is ready; press Ctrl+C (⌘C) to copy.")}
}
function updateDailyButton(){
  const source=(state.inventorySources||[])[0];
  $("daily-update-button").disabled=state.client==="all"||!source||!!source.error;
}
const loadCalculated=makePanelLoader(state,(...a)=>api(...a),"calculated","/api/inventory/calculated",()=>renderCalculated());
function shopifyRows(){return (state.shopify||[]).filter(c=>!c.error).flatMap(c=>c.variants.map(r=>({...r,client:c.client_id})))}
function renderShopify(){
  const clients=state.shopify||[], rows=shopifyRows();
  const badge={mapped:"delivered",component:"in_transit",unmapped:"critical",no_rules:"data_gap"};
  const shown=rows.slice(0,state.shopifyShown);
  $("shopify-rows").innerHTML=rows.length?shown.map(r=>`<tr><td><strong>${esc(r.product)}${r.variant&&r.variant!=="Default Title"?" · "+esc(r.variant):""}</strong><small>${esc(clientName(r.client))} · ${esc(r.product_status.toLowerCase())}</small></td><td>${r.sku?esc(r.sku):'<span class="muted">blank</span>'}</td><td>${r.internal_sku?`<strong>${esc(r.internal_sku)}</strong><small>${esc(r.label)}</small>`:"—"}</td><td class="inventory-source"><span class="badge ${badge[r.status]||""}">${esc(r.status_text)}</span><small>${esc(r.how)}</small></td><td class="inventory-source">${r.flag_text.length?r.flag_text.map(t=>`<small class="finding">${esc(t)}</small>`).join(""):'<small>OK</small>'}</td></tr>`).join(""):(state.shopify===null?'<tr><td colspan="5" class="empty-cell"><strong>Loading…</strong>Reading Shopify.</td></tr>':'<tr><td colspan="5" class="empty-cell"><strong>No Shopify products</strong>'+esc(state.shopifyError||"No connected store returned products.")+'</td></tr>');
  const problems=clients.filter(c=>c.error).map(c=>`<div class="source-problem failed"><strong>${esc(clientName(c.client_id))} — not loaded</strong> ${esc(c.error)}</div>`)
    .concat(clients.filter(c=>!c.error).flatMap(c=>(c.warnings||[]).map(w=>`<div class="source-problem"><strong>${esc(clientName(c.client_id))}</strong> ${esc(w)}</div>`)));
  $("shopify-error").innerHTML=problems.join("");$("shopify-error").hidden=!problems.length;
  const stateText={found:"In Shopify",inactive:"Only on draft/archived products",missing:"Not found in Shopify"};
  $("shopify-rules").innerHTML=clients.filter(c=>!c.error).map(c=>`<div class="eod-card"><strong>${esc(clientName(c.client_id))} — rules ${esc(c.rule_status.toLowerCase())}</strong><span>${c.summary.mapped} mapped · ${c.summary.unmapped} not mapped · ${c.summary.flagged} with checks · ${c.summary.variants} variants</span>${c.rules.length?c.rules.map(r=>`<small class="${r.state==="found"?"":"finding"}">${esc(r.internal_sku)} ${esc(r.label)}: ${esc(stateText[r.state])}</small>`).join(""):"<small>No rule package for this client yet; nothing can be mapped.</small>"}</div>`).join("");
  const review=clients.filter(c=>!c.error).reduce((n,c)=>n+c.summary.needs_review,0);
  const complete=clients.length&&clients.every(c=>!c.error&&!c.truncated);
  $("shopify-status").textContent=state.shopify===null?"Not loaded":!clients.some(c=>!c.error)?"Not connected":review?`${review} to review`:complete?"All mapped":"Incomplete · not all stores loaded";
  $("shopify-export").disabled=!rows.length&&!clients.some(c=>c.error);
  $("shopify-more").hidden=rows.length<=state.shopifyShown;
  $("shopify-more").textContent=`Show more (${Math.min(state.shopifyShown,rows.length).toLocaleString()} of ${rows.length.toLocaleString()} shown · CSV export has all)`;
}
const loadShopify=makePanelLoader(state,(...a)=>api(...a),"shopify","/api/shopify/sku-check",()=>renderShopify());
function exportShopify(){
  const rows=shopifyRows();if(!rows.length&&!(state.shopify||[]).some(c=>c.error))return;
  const cell=v=>'"'+String(v??"").replace(/^[=+@\-\t\r]/,"'$&").replace(/"/g,'""')+'"';
  const clients=state.shopify||[], done={};
  for(const c of clients)done[c.client_id]=c.error?"NOT LOADED: "+c.error:c.truncated?"INCOMPLETE: catalog cut off; later variants not checked":"complete";
  const data=[["client","catalog","shopify_product","shopify_variant","product_status","shopify_sku","internal_sku","mapping","how","checks"],
    ...clients.filter(c=>c.error).map(c=>[clientName(c.client_id),done[c.client_id],"","","","","","","",""]),
    ...rows.map(r=>[clientName(r.client),done[r.client],r.product,r.variant,r.product_status,r.sku,r.internal_sku,r.status_text,r.how,r.flag_text.join("; ")]),
    ...clients.filter(c=>!c.error).flatMap(c=>c.rules.filter(r=>r.state!=="found").map(r=>[clientName(c.client_id),done[c.client_id],"","","","",r.internal_sku,r.state==="missing"?"RULE SKU NOT IN SHOPIFY":"RULE SKU ONLY ON DRAFT/ARCHIVED",r.label,""]))];
  const url=URL.createObjectURL(new Blob([data.map(row=>row.map(cell).join(",")).join("\r\n")],{type:"text/csv;charset=utf-8"}));
  const a=document.createElement("a");a.href=url;a.download="shipmode-shopify-sku-mapping.csv";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function sskRows(){return (state.ssk||[]).filter(c=>!c.error).flatMap(c=>c.skus.map(r=>({...r,client:c.client_id})))}
function renderSsk(){
  const clients=state.ssk||[], rows=sskRows(), fmt=n=>n==null?"—":Number(n).toLocaleString();
  const diff=n=>n==null?"—":(n>0?"+":"")+Number(n).toLocaleString();
  const badge={MATCH:"delivered",DIFFERENT:"critical",REVIEW:"watch"};
  $("ssk-rows").innerHTML=rows.length?rows.map(r=>`<tr><td><strong>${esc(r.sku)} · ${esc(r.label)}</strong><small>${esc(clientName(r.client))}${r.ssk_skus.length?" · SSK "+esc(r.ssk_skus.join(", ")):""}</small>${r.basis?`<small>${esc(r.basis)}</small>`:""}</td><td>${fmt(r.sheet_remaining)}</td><td><strong>${fmt(r.ssk&&r.ssk.available)}</strong></td><td>${fmt(r.ssk&&r.ssk.committed)}</td><td>${fmt(r.ssk&&r.ssk.incoming)}</td><td>${fmt(r.ssk&&r.ssk.damaged)}</td><td>${diff(r.vs_available)}</td><td>${diff(r.vs_available_committed)}</td><td class="inventory-source"><span class="badge ${badge[r.status]||""}">${esc(r.status)}</span>${r.notes.map(t=>`<small>${esc(t)}</small>`).join("")}</td></tr>`).join(""):(state.ssk===null?'<tr><td colspan="9" class="empty-cell"><strong>Loading…</strong>Reading ShipSidekick.</td></tr>':'<tr><td colspan="9" class="empty-cell"><strong>No ShipSidekick stock</strong>'+esc(state.sskError||"No store with rules returned stock yet.")+'</td></tr>');
  const problems=clients.filter(c=>c.error).map(c=>`<div class="source-problem failed"><strong>${esc(clientName(c.client_id))} — not loaded</strong> ${esc(c.error)}</div>`)
    .concat(clients.filter(c=>!c.error).flatMap(c=>(c.warnings||[]).concat(c.sheet_error?["Sheet: "+c.sheet_error]:[]).map(w=>`<div class="source-problem"><strong>${esc(clientName(c.client_id))}</strong> ${esc(w)}</div>`)));
  $("ssk-error").innerHTML=problems.join("");$("ssk-error").hidden=!problems.length;
  $("ssk-extra").innerHTML=clients.filter(c=>!c.error&&(c.unmatched_ssk.length||c.unmatched_sheet.length||c.components.length||c.blank_skus||c.duplicate_skus.length)).map(c=>`<div class="eod-card"><strong>${esc(clientName(c.client_id))} — needs mapping review</strong>${c.unmatched_ssk.slice(0,50).map(v=>`<small class="finding">ShipSidekick SKU not in rules: ${esc(v.sku||"(blank)")} · ${esc(v.title)} · ${fmt(v.available)} available</small>`).join("")}${c.components.slice(0,50).map(v=>`<small>${esc(v.sku)} · ${esc(v.title)}: ${esc(v.note)}</small>`).join("")}${c.components.length>50?`<small>…and ${(c.components.length-50).toLocaleString()} more not matched to one SKU</small>`:""}${c.unmatched_ssk.length>50?`<small class="finding">…and ${(c.unmatched_ssk.length-50).toLocaleString()} more ShipSidekick SKUs not in rules</small>`:""}${c.unmatched_sheet.map(p=>`<small class="finding">Sheet row not matched to a SKU: ${esc(p)}</small>`).join("")}${c.blank_skus?`<small class="finding">${c.blank_skus} ShipSidekick item(s) with a blank SKU</small>`:""}${c.duplicate_skus.slice(0,50).map(s=>`<small class="finding">SKU used by more than one ShipSidekick product: ${esc(s)}</small>`).join("")}${c.duplicate_skus.length>50?`<small class="finding">…and ${(c.duplicate_skus.length-50).toLocaleString()} more duplicated SKUs</small>`:""}</div>`).join("");
  const counts=rows.reduce((m,r)=>(m[r.status]=(m[r.status]||0)+1,m),{});
  const loaded=clients.filter(c=>!c.error);
  $("ssk-status").textContent=state.ssk===null?"Not loaded":rows.length?Object.entries(counts).map(([k,v])=>`${v} ${k.toLowerCase()}`).join(" · "):loaded.length?"Connected · no rules yet":"Not connected";
  const read=loaded.filter(c=>c.fetched_at).map(c=>`${clientName(c.client_id)} read ${new Date(c.fetched_at).toLocaleTimeString([], {hour:"numeric",minute:"2-digit"})}`);
  if(read.length)$("ssk-status").textContent+=" · "+read.join(", ");
  $("ssk-export").disabled=!rows.length&&!sskReviewRows().length;
}
const loadSsk=makePanelLoader(state,(...a)=>api(...a),"ssk","/api/ssk/inventory",()=>renderSsk());
function sskReviewRows(){return (state.ssk||[]).filter(c=>!c.error).flatMap(c=>[
  ...c.unmatched_ssk.map(v=>[clientName(c.client_id),"",v.title,v.sku,"",v.available,v.committed,"","","","","NOT IN RULES","ShipSidekick SKU not in this client's rules"]),
  ...c.components.map(v=>[clientName(c.client_id),"",v.title,v.sku,"",v.available,"","","","","","NOT MATCHED",v.note])])}
function exportSsk(){
  const rows=sskRows(), review=sskReviewRows();if(!rows.length&&!review.length)return;
  const cell=v=>'"'+String(v??"").replace(/^[=+@\-\t\r]/,"'$&").replace(/"/g,'""')+'"';
  const q=(r,k)=>r.ssk?r.ssk[k]:"";
  const src={};for(const c of state.ssk||[])src[clientName(c.client_id)]=[c.environment==="test"?"TEST (not production)":"production",c.fetched_at||""];
  const data=[["client","ssk_environment","ssk_read_at","sku","label","shipsidekick_skus","sheet_remaining","ssk_available","ssk_committed","ssk_incoming","ssk_damaged","diff_vs_available","diff_vs_available_committed","status","notes"],...[...rows.map(r=>[clientName(r.client),r.sku,r.label,r.ssk_skus.join(" "),r.sheet_remaining,q(r,"available"),q(r,"committed"),q(r,"incoming"),q(r,"damaged"),r.vs_available,r.vs_available_committed,r.status,r.notes.join("; ")]),...review].map(r=>[r[0],...(src[r[0]]||["",""]),...r.slice(1)])];
  const url=URL.createObjectURL(new Blob([data.map(row=>row.map(cell).join(",")).join("\r\n")],{type:"text/csv;charset=utf-8"}));
  const a=document.createElement("a");a.href=url;a.download="shipmode-shipsidekick-vs-sheet.csv";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function loadInventory(){
  if(!state.data)return;
  const request=++state.inventoryRequest,client=state.client;
  state.inventorySources=null;state.inventoryAsOf=null;renderInventory();
  $("inventory-refresh").disabled=true;
  $("inventory-sync").textContent="Reading Google Sheets…";
  // Independent panels start now, so a slow Sheet never delays ShipSidekick or Shopify.
  loadCalculated();loadShopify();loadSsk();loadIncoming();
  try{
    const result=await api(`/api/inventory?client_id=${encodeURIComponent(client)}`);
    if(request!==state.inventoryRequest)return;
    state.inventorySources=result.sources;state.inventoryAsOf=result.as_of;renderInventory();
  }catch(error){
    if(request!==state.inventoryRequest)return;
    state.inventorySources=[];state.inventoryAsOf=null;renderInventory();
    $("inventory-error").textContent=error.message;$("inventory-error").hidden=false;
  }finally{if(request===state.inventoryRequest)$("inventory-refresh").disabled=false}
}
function section(name){
  state.section=name;
  const title={movement:"No Movement",inventory:"Inventory",invoices:"Invoices"}[name];
  document.querySelectorAll("[data-section]").forEach(el=>{el.classList.toggle("active",el.dataset.section===name);el.setAttribute("aria-current",el.dataset.section===name?"page":"false")});
  for(const key of ["movement","inventory","invoices"])$(key+"-section").hidden=key!==name;
  $("mode-notice").hidden=name!=="movement";
  $("movement-actions").hidden=name!=="movement";$("crumb").textContent=title;
  $("title").innerHTML=esc(title)+'<span class="title-dot"></span>';
  $("eyebrow").textContent=name==="movement"?"SHIPMENT EXCEPTIONS":"CLIENT OPERATIONS";
  $("subtitle").textContent={movement:"Catch stalled shipments before your customers do.",inventory:"Stock visibility, organized by client.",invoices:"Client billing, in one place."}[name];
  render();
  if(name==="inventory")loadInventory();
}
function trackingUrl(row){
  if(state.data.mode==="demo")return null;
  if(row.tracking_url&&/^https:\/\//.test(row.tracking_url))return row.tracking_url;
  const code=encodeURIComponent(row.tracking_number);
  return {usps:`https://tools.usps.com/go/TrackConfirmAction?tLabels=${code}`,ups:`https://www.ups.com/track?tracknum=${code}`,fedex:`https://www.fedex.com/fedextrack/?trknbr=${code}`}[row.carrier]||null;
}
function details(id){
  const r=state.data.shipments.find(r=>r.id===id);if(!r)return;state.selected=id;
  const link=trackingUrl(r), demo=state.data.mode!=="live", ssk=state.data.mode==="ssk";
  $("detail-content").innerHTML=`<div class="detail-title"><h2>${esc(r.order_number||"Unlinked order")}</h2><span class="badge ${r.tier}">${tierNames[r.tier]}</span></div><div class="detail-client">${esc(clientName(r.client_id))} · ${esc(carrierName(r.carrier))}</div><div class="tracking">${esc(r.tracking_number)}</div><div class="detail-summary"><span class="eyebrow">${r.tier==="delivered"?"CARRIER CONFIRMED":"SHIPMENT EVIDENCE"}</span><strong>${r.tier==="delivered"?"Delivered":r.days===null?"Date needed":r.days+" days without movement"}</strong><p>${esc(r.reason)}</p>${r.anchor_at?`<span class="muted">Clock starts from: ${esc(r.anchor_source)} · ${esc(date(r.anchor_at,true))}</span>`:""}</div><dl class="detail-grid"><div><dt>Order fulfillment</dt><dd>${esc(r.fulfillment_status)}</dd></div><div><dt>Carrier status</dt><dd>${esc(statusNames[r.carrier_status]||r.carrier_status)}</dd></div><div><dt>Shipped</dt><dd>${esc(date(r.shipped_at,true))}</dd></div><div><dt>Latest update received</dt><dd>${esc(date(r.last_received_at,true))}</dd></div></dl><div class="setup-note">${ssk?"Read from ShipSidekick (read-only). Saving follow-up notes needs the database and is off.":demo?"Sample shipment — this tracking number is illustrative.":"Latest update received is not a fresh carrier lookup. Missing or delayed feeds require reconciliation."}</div>${link?`<a class="button secondary" href="${esc(link)}" target="_blank" rel="noopener noreferrer">Open carrier tracking ↗</a>`:""}<h3 class="detail-section-title">Carrier event history</h3><ul class="timeline">${[...(r.events||[])].reverse().map(e=>`<li>${esc(e.description)}<small>${esc(date(e.at,true))}${e.movement?" · Physical scan":" · Does not reset movement clock"}</small></li>`).join("")||"<li>No scan history available. Import the shipping date and connect tracking updates.</li>"}</ul><h3 class="detail-section-title">Follow-up</h3><label class="field-label" for="case-status">Case status</label><select id="case-status" ${demo?"disabled":""}>${Object.entries(caseNames).map(([key,value])=>`<option value="${key}" ${r.case_status===key?"selected":""}>${value}</option>`).join("")}</select><label class="field-label" for="case-notes">Notes</label><textarea id="case-notes" maxlength="4000" ${demo?"disabled":""} placeholder="Carrier case number, last contact, next action…">${esc(r.notes)}</textarea><p class="muted">${ssk?"Follow-up notes are not saved yet in ShipSidekick mode.":demo?"Notes and follow-up are read-only in the sample workspace.":"Follow-up does not clear an aging exception. Carrier evidence determines priority."}</p><div class="dialog-actions"><button id="save-case" class="button primary" ${demo?"disabled":""}>Save follow-up</button></div>`;
  if(!demo)$("save-case").addEventListener("click",saveCase);
  if(ssk&&(state.data.shopify_orders||[]).includes(r.client_id)&&r.ssk_id){
    $("detail-content").insertAdjacentHTML("beforeend",`<h3 class="detail-section-title">Shopify order</h3><div id="order-panel"><p class="muted">Read-only. The shipping address is shown here only and is never saved.</p><button class="button secondary" id="order-load">Show Shopify order and address</button></div>`);
    $("order-load").addEventListener("click",()=>loadOrder(r));
  }
  if(!$("detail-dialog").open)$("detail-dialog").showModal();
}
async function loadOrder(r){
  const panel=$("order-panel"),id=r.id,ticket=++state.orderRequest;if(!panel)return;
  panel.innerHTML='<p class="muted">Reading Shopify…</p>';
  try{
    const res=await api(`/api/shopify/order?client_id=${encodeURIComponent(r.client_id)}&shipment=${encodeURIComponent(r.ssk_id)}`);
    // Closing the dialog or opening another shipment invalidates this request (no stale address).
    if(ticket!==state.orderRequest||state.selected!==id||!$("detail-dialog").open)return;
    const o=res.order, a=o&&o.address;
    const flags=res.flag_text.map(t=>`<span class="badge watch">${esc(t)}</span>`).join(" ");
    panel.innerHTML=(flags?`<div class="order-flags">${flags}</div>`:"")+(o?`<dl class="detail-grid"><div><dt>Order</dt><dd>${esc(o.name)} · ${esc(date(o.created_at,true))}</dd></div><div><dt>Payment</dt><dd>${esc(o.financial.toLowerCase().replace(/_/g," "))}</dd></div><div><dt>Fulfillment</dt><dd>${esc(o.fulfillment.toLowerCase().replace(/_/g," "))}</dd></div><div><dt>Cancelled</dt><dd>${o.cancelled_at?esc(date(o.cancelled_at,true)):"No"}</dd></div></dl><h4>Items in Shopify${o.items_truncated?" (first 100 only)":""}</h4><ul class="timeline">${o.items.map(i=>`<li>${esc(i.name||i.sku)} × ${esc(i.qty)}<small>${esc(i.sku||"no SKU")}</small></li>`).join("")}</ul><h4>Items in this shipment</h4><ul class="timeline">${(r.items||[]).map(i=>`<li>${esc(i.name||i.sku)} × ${esc(i.qty)}<small>${esc(i.sku||"no SKU")}</small></li>`).join("")||"<li>Not listed by ShipSidekick</li>"}</ul><h4>Ship to (current, in Shopify)</h4>${a?`<address class="ship-to">${[a.name,a.address1,a.address2,[a.city,a.provinceCode,a.zip].filter(Boolean).join(" "),a.countryCodeV2].filter(Boolean).map(esc).join("<br>")}</address>`:`<p class="muted">${o.address_withheld?"Address hidden: Shopify could only search its last 60 days of orders, so this match is not confirmed. Ask the client to add read_all_orders to the ShipMode app.":o.address_visible?"No shipping address on this order.":"Address not available: Shopify has not approved address access for the ShipMode app."}</p>`}`:"")+'<p class="muted">Flags are for review only; they do not change the shipment\'s age or priority.</p>';
  }catch(error){if(ticket===state.orderRequest&&state.selected===id&&$("order-panel"))$("order-panel").innerHTML=`<p class="error">${esc(error.message)}</p>`}
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
    if(state.data.mode==="ssk"){
      const sources=state.data.sources||[], ok=sources.filter(s=>!s.error_code);
      const bad=sources.filter(s=>s.error_code&&s.error_code!=="not_configured"), nokey=sources.filter(s=>s.error_code==="not_configured");
      const partial=s=>s.truncated?" (INCOMPLETE: list cut off)":s.skipped_statuses&&s.skipped_statuses.length?` (INCOMPLETE: ${s.skipped_statuses.join(", ")} not read)`:"";
      const test=ok.some(s=>s.environment==="test");
      $("mode-notice").querySelector("strong").textContent=test?"ShipSidekick TEST environment — not production shipments":"ShipSidekick shipments (read-only)";
      $("mode-notice").querySelector("div span").textContent=(ok.length?`Not delivered, created in the last ${state.data.lookback_days} days: ${ok.map(s=>`${clientName(s.client_id)} ${s.shipments.toLocaleString()}${partial(s)}`).join(", ")}.`:"No store loaded.")+(bad.length?" Not loaded: "+bad.map(s=>`${clientName(s.client_id)} — ${s.error}`).join("; ")+".":"")+(nokey.length?" No ShipSidekick key yet, so no shipments shown: "+nokey.map(s=>clientName(s.client_id)).join(", ")+".":"");
      $("side-connection").textContent=ok.length?`Connected · ${ok.map(s=>clientName(s.client_id)).join(", ")}`:"Not connected";
      $("setup-button").hidden=true;
    }else if(state.data.mode==="live"){
      $("mode-notice").querySelector("strong").textContent="Live workspace";
      $("mode-notice").querySelector("div span").textContent="Check each client’s tracking feed before relying on coverage.";
      $("side-connection").textContent="Verify client feeds";
    }
    $("as-of").textContent=(state.data.mode==="demo"?"Sample data · ":state.data.mode==="ssk"?"ShipSidekick read · ":"Queue calculated · ")+date(state.data.as_of,true);
    render();
    if(first&&state.section==="inventory")loadInventory();
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
$("client-select").addEventListener("change",e=>{state.client=e.target.value;state.page=1;resetPanels(state,["calculated","shopify","ssk"]);state.shopifyShown=200;renderCalculated();renderShopify();renderSsk();try{localStorage.setItem("shipmode-client",state.client)}catch{}render();if(state.section==="inventory")loadInventory()});
$("search").addEventListener("input",e=>{state.search=e.target.value;state.page=1;render()});
$("carrier-filter").addEventListener("change",e=>{state.carrier=e.target.value;state.page=1;render()});
$("previous").addEventListener("click",()=>{state.page--;render()});$("next").addEventListener("click",()=>{state.page++;render()});
$("shipment-rows").addEventListener("click",e=>{const button=e.target.closest("[data-detail]");if(button)details(Number(button.dataset.detail))});
for(const id of ["setup-button","connection-details"])$(id).addEventListener("click",()=>$("setup-dialog").showModal());
document.querySelectorAll(".close-dialog").forEach(el=>el.addEventListener("click",()=>el.closest("dialog").close()));
$("detail-dialog").addEventListener("close",()=>{state.orderRequest++;const panel=$("order-panel");if(panel)panel.innerHTML=""});  // customer address leaves the page
$("export-button").addEventListener("click",exportQueue);
$("inventory-refresh").addEventListener("click",loadInventory);
$("daily-update-button").addEventListener("click",openDailyUpdate);
$("update-copy").addEventListener("click",copyDailyUpdate);
$("incoming-history-toggle").addEventListener("click",()=>{state.incomingHistory=!state.incomingHistory;renderIncoming()});
$("shopify-export").addEventListener("click",exportShopify);
$("shopify-more").addEventListener("click",()=>{state.shopifyShown+=200;renderShopify()});
$("ssk-export").addEventListener("click",exportSsk);
  $("import-button").addEventListener("click",()=>{if(!state.data||state.data.mode!=="live"){$("setup-dialog").showModal();return}if(state.client!=="all")$("import-client").value=state.client;$("import-result").textContent="";$("import-dialog").showModal()});
$("confirm-import").addEventListener("click",async()=>{
  const file=$("import-file").files[0];if(!file){$("import-result").textContent="Choose a CSV file.";return}
  if(file.size>5*1024*1024){$("import-result").textContent="File exceeds 5 MB.";return}
  $("confirm-import").disabled=true;
  try{const result=await api("/api/import",{method:"POST",body:JSON.stringify({client_id:$("import-client").value,csv:await file.text()})});$("import-result").textContent=`${result.inserted} added, ${result.updated} updated.`;await load()}catch(e){$("import-result").textContent=e.message}finally{$("confirm-import").disabled=false}
});
load();setInterval(()=>{if(!document.hidden && !$("detail-dialog").open && !$("import-dialog").open && !$("update-dialog").open){load();if(state.section==="inventory")loadInventory()}},60000);
