"use strict";
const byId = id => document.getElementById(id);
const cart = [], search = {cursor:"0", next:null, previous:null};
const selectedAddons = new Map(), draftKey = "foundry-prepare-draft-v1";
let edition = null, sending = false, seeds = [], digitalPage = [], searchSequence = 0, draftReady = false;
function item(tag, cls, text) { const n=document.createElement(tag); n.className=cls||""; if(text!==undefined)n.textContent=text; return n; }
function notice(text) { byId("prepare-notice").textContent=text; }
async function request(path, data) {
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),20000);
  try { const r=await fetch(path,{method:data===undefined?"GET":"POST",cache:"no-store",signal:controller.signal,headers:{Accept:"application/json","Content-Type":"application/json","X-Requested-With":"XMLHttpRequest"},...(data===undefined?{}:{body:JSON.stringify(data)})});
    if(!(r.headers.get("Content-Type")||"").includes("application/json"))throw Error("Sign in again, then reload this page.");
    const v=await r.json(); if(!r.ok)throw Error(v.error||`HTTP ${r.status}`); return v;
  } finally { clearTimeout(timer); }
}
function key() { const b=new Uint8Array(16);crypto.getRandomValues(b);return "web_"+[...b].map(v=>v.toString(16).padStart(2,"0")).join(""); }
function saveDraft() {
  if(!draftReady)return;
  try { sessionStorage.setItem(draftKey,JSON.stringify({version:1,cart:cart.filter(e=>!e.sent),addons:[...selectedAddons.values()],device:byId("prepare-device").value,
    mode:byId("prepare-mode").value,unlock:byId("prepare-unlock").checked,query:byId("prepare-query").value,kind:byId("prepare-kind").value})); } catch(_) { /* A blocked/full session store must not prevent preparation. */ }
}
function restoreDraft() {
  try {
    const raw=sessionStorage.getItem(draftKey);if(!raw||raw.length>262144)return;
    const saved=JSON.parse(raw);if(saved.version!==1||!Array.isArray(saved.cart)||saved.cart.length>32)return;
    const validId=id=>typeof id==="string"&&/^catalog-[1-9][0-9]*$/.test(id);
    for(const e of saved.cart) {
      if(!e||typeof e.name!=="string"||e.name.length>1024||!e.selection||typeof e.selection!=="object"||Array.isArray(e.selection)||typeof e.request_key!=="string"||!/^web_[a-f0-9]{32}$/.test(e.request_key)||JSON.stringify(e.selection).length>65536||!(e.device==null||/^[a-f0-9]{32}$/.test(e.device)))continue;
      cart.push({selection:e.selection,name:e.name,request_key:e.request_key,device:e.device||null,sent:false});
    }
    for(const source of Array.isArray(saved.addons)?saved.addons.slice(0,32):[])if(source&&validId(source.release_id)&&typeof source.name==="string"&&source.name.length<=1024&&source.kind==="digital")selectedAddons.set(source.release_id,source);
    if([...byId("prepare-device").options].some(o=>o.value===saved.device))byId("prepare-device").value=saved.device;
    if(["server","console"].includes(saved.mode))byId("prepare-mode").value=saved.mode;
    if(typeof saved.unlock==="boolean")byId("prepare-unlock").checked=saved.unlock;
    if(typeof saved.query==="string")byId("prepare-query").value=saved.query.slice(0,120);
    if(["game","digital"].includes(saved.kind))byId("prepare-kind").value=saved.kind;
  } catch(_) { /* Ignore an invalid draft; all requests are validated by the server. */ }
}
function size(n) { return (Number(n||0)/1073741824).toFixed(2)+" GB"; }
function button(label, fn) { const b=item("button","button-secondary",label);b.type="button";b.onclick=async()=>{b.disabled=true;try{await fn();}catch(e){notice(e.name==="AbortError"?"Request timed out. Retry to check or reuse its saved request.":e.message);}finally{b.disabled=false;}};return b; }
function renderCart() {
  byId("prepare-cart").replaceChildren();byId("cart-count").textContent=`${cart.length} item${cart.length===1?"":"s"}`;
  for(const entry of cart) { const row=item("div","prepare-result"), info=item("div"); info.append(item("strong","",entry.name),item("p","muted",entry.sent?"Sent to Xbox inbox":`${entry.selection.conversion==="server"?"Server preparation":"Convert on Xbox"} · ${entry.selection.release_ids?.length||1} source(s)`));
    row.append(info,button(entry.sent?"Sent":"Remove",()=>{if(!sending&&!entry.sent){cart.splice(cart.indexOf(entry),1);renderCart();}}));byId("prepare-cart").append(row); }
  if(!cart.length)byId("prepare-cart").append(item("p","empty","Choose a source above to get started."));
  byId("send-preparations").disabled=sending||!cart.some(e=>!e.sent)||!byId("prepare-device").value;
  renderBulk();saveDraft();
}
function addonSelection(source) { return {conversion:"server",unlock_packages:byId("prepare-unlock").checked,release_id:source.release_id}; }
function renderBulk() {
  const panel=byId("dlc-bulk-tools");if(!panel)return;
  panel.hidden=!digitalPage.length&&!selectedAddons.size;
  const count=selectedAddons.size, checked=digitalPage.filter(s=>selectedAddons.has(s.release_id)).length;
  byId("dlc-page").checked=digitalPage.length>0&&checked===digitalPage.length;byId("dlc-page").indeterminate=checked>0&&checked<digitalPage.length;
  byId("dlc-page").disabled=sending||!digitalPage.length;
  byId("dlc-count").textContent=`${count} selected · selections stay across result pages`;
  byId("add-selected-dlc").textContent=`Add ${count||"selected"} add-on${count===1?"":"s"}`;
  byId("add-selected-dlc").disabled=sending||!count;byId("clear-selected-dlc").disabled=sending||!count;
  for(const check of byId("prepare-results").querySelectorAll("input[data-addon]")){check.checked=selectedAddons.has(check.value);check.disabled=sending;}
}
function addSelectedAddons() {
  if(sending||!selectedAddons.size)return;
  const additions=[...selectedAddons.values()].map(source=>({source,selection:addonSelection(source)})).filter(({selection})=>!cart.some(e=>JSON.stringify(e.selection)===JSON.stringify(selection)));
  if(cart.length+additions.length>32){notice(`There is room for ${32-cart.length} more items. Reduce the add-on selection or send the current list first.`);return;}
  for(const {source,selection} of additions)cart.push({selection,name:source.name,request_key:key(),sent:false,device:null});
  selectedAddons.clear();renderCart();notice(additions.length?`${additions.length} add-ons added together. Server processing and the current unlock choice are saved for each; your game/disc selection stays in the list.`:"These add-ons are already in your preparation list.");
}
function setupBulk() {
  const panel=item("div","dlc-bulk-tools");panel.id="dlc-bulk-tools";panel.hidden=true;
  const label=item("label","checkbox-label"),check=item("input");check.id="dlc-page";check.type="checkbox";label.append(check,document.createTextNode("Select this page"));
  const count=item("span","muted");count.id="dlc-count";count.setAttribute("aria-live","polite");
  const add=item("button","","Add selected add-ons");add.type="button";add.id="add-selected-dlc";add.onclick=addSelectedAddons;
  const clear=item("button","button-secondary","Clear selection");clear.type="button";clear.id="clear-selected-dlc";clear.onclick=()=>{selectedAddons.clear();renderBulk();saveDraft();};
  const note=item("p","muted","Tick several XBLA / DLC sources, then add them together. Add-ons use server processing and the current unlock choice. Nothing is submitted until Prepare for Xbox.");
  panel.append(label,count,add,clear,note);byId("prepare-results").before(panel);
  check.onchange=()=>{
    if(check.checked){const missing=digitalPage.filter(s=>!selectedAddons.has(s.release_id));if(selectedAddons.size+missing.length>32){notice("Select at most 32 add-ons at a time. Your earlier selections are kept.");renderBulk();return;}for(const source of missing)selectedAddons.set(source.release_id,source);}
    else for(const source of digitalPage)selectedAddons.delete(source.release_id);
    renderBulk();saveDraft();
  };
}
async function choose(release) {
  edition=await request(`/api/consoles/releases/${encodeURIComponent(release)}`);
  byId("edition-options").replaceChildren();
  const discs=edition.disc_set?.items||[{...edition,recommended:true,disc_note:""}];
  byId("edition-note").textContent=edition.disc_set?.note||"Review the full source name, region and edition before adding it.";
  for(const disc of discs) { const label=item("label","edition-option"), check=item("input");check.type="checkbox";check.value=disc.release_id;check.checked=disc.recommended!==false;check.disabled=disc.recommended===false;
    const text=item("span","",disc.name);if(disc.disc_note)text.append(item("p","muted",disc.disc_note));label.append(check,text);byId("edition-options").append(label); }
  byId("edition-panel").hidden=false;byId("edition-panel").scrollIntoView({behavior:"smooth",block:"nearest"});
}
async function find() {
  const sequence=++searchSequence, kind=byId("prepare-kind").value;
  const params=new URLSearchParams({q:byId("prepare-query").value,kind:byId("prepare-kind").value,cursor:search.cursor});
  const v=await request(`/api/consoles/catalog?${params}`);if(sequence!==searchSequence)return;byId("prepare-results").replaceChildren();digitalPage=kind==="digital"?v.items.filter(s=>s.kind==="digital"):[];
  for(const source of v.items) { const row=item("div","prepare-result"),info=item("div");info.append(item("strong","",source.name),item("p","muted",`${size(source.source_bytes)} · ${source.availability}`));
    if(kind==="digital"&&source.kind==="digital"){
      const label=item("label","addon-result"),check=item("input");check.type="checkbox";check.value=source.release_id;check.dataset.addon="true";check.checked=selectedAddons.has(source.release_id);
      check.onchange=()=>{if(check.checked){if(selectedAddons.size>=32){check.checked=false;notice("Select at most 32 add-ons at a time.");return;}selectedAddons.set(source.release_id,source);}else selectedAddons.delete(source.release_id);renderBulk();saveDraft();};
      label.append(check,info);row.append(label,button("Details",()=>choose(source.release_id)));
    }else row.append(info,button("Choose",()=>choose(source.release_id)));byId("prepare-results").append(row); }
  if(!v.items.length)byId("prepare-results").append(item("p","empty","No matching sources. Try another title."));
  search.next=v.next_cursor;search.previous=v.previous_cursor;byId("search-next").disabled=!search.next;byId("search-previous").disabled=!search.previous;
  renderBulk();saveDraft();
}
byId("prepare-search").onsubmit=async e=>{e.preventDefault();search.cursor="0";try{await find();}catch(err){notice(err.message);}};
for(const [id,field] of [["search-next","next"],["search-previous","previous"]])byId(id).onclick=async()=>{if(search[field]){search.cursor=search[field];try{await find();}catch(e){notice(e.message);}}};
byId("add-preparation").onclick=async()=>{
  if(!edition||sending)return;const ids=[...byId("edition-options").querySelectorAll("input:checked")].map(i=>i.value).sort();if(!ids.length){notice("Select at least one supported source.");return;}
  if(cart.length>=32){notice("Send this list before adding more items (maximum 32).");return;}
  const mode=byId("prepare-mode").value;if(mode==="console"&&(edition.kind!=="game"||edition.disc_set)){notice("Use server preparation for multi-disc or digital selections.");return;}
  const selection={conversion:mode,unlock_packages:byId("prepare-unlock").checked};
  if(edition.disc_set)Object.assign(selection,{kind:"disc_bundle",release_ids:ids});else selection.release_id=ids[0];
  if(cart.some(e=>JSON.stringify(e.selection)===JSON.stringify(selection))){notice("This selection is already in your list.");return;}
  cart.push({selection,name:edition.name,request_key:key(),sent:false,device:null});renderCart();notice("Added. You can search for another game or prepare the list below.");
  const reviewed=new Set(edition.disc_set?.items.map(d=>d.release_id)||[edition.release_id]);seeds=seeds.filter(id=>!reviewed.has(id));
  if(seeds.length){try{await choose(seeds[0]);notice(`Added. Review the next selected source (${seeds.length} remaining).`);}catch(e){notice(e.message);}}
};
byId("prepare-device").onchange=renderCart;
byId("prepare-mode").onchange=byId("prepare-unlock").onchange=saveDraft;
byId("prepare-query").oninput=saveDraft;
byId("prepare-kind").onchange=async()=>{search.cursor="0";try{await find();}catch(e){notice(e.message);}};
byId("send-preparations").onclick=async()=>{
  if(sending)return;const device=byId("prepare-device").value;if(!device)return;
  sending=true;byId("prepare-device").disabled=true;renderCart();
  try { for(const entry of cart.filter(e=>!e.sent)) {
    if(entry.device&&entry.device!==device)throw Error("Retry this list for the original console before changing its destination.");
    entry.device=device;saveDraft();await request(`/api/consoles/${device}/prepare`,{selection:entry.selection,request_key:entry.request_key});entry.sent=true;renderCart(); }
    notice("Sent. The server is preparing your downloads. Open Console activity for progress, then Prepared for Xbox in Omni360 when ready.");
  } catch(e){notice(`Some items may still be waiting to submit. ${e.message} Press Prepare for Xbox again to retry safely.`);}
  finally{sending=false;byId("prepare-device").disabled=false;renderCart();}
};
setupBulk();
(async()=>{try{const [v,settings]=await Promise.all([request("/api/consoles"),request("/api/settings")]);byId("prepare-unlock").checked=settings.unlock_packages===true;byId("prepare-device").replaceChildren();for(const d of v.devices.filter(d=>!d.revoked&&!d.hidden)){const o=item("option","",d.name);o.value=d.id;byId("prepare-device").append(o);}
  restoreDraft();draftReady=true;if(!byId("prepare-device").options.length)notice("Pair a console on Console activity first.");else if(cart.length)notice("Your unsent preparation list was restored in this tab.");renderCart();
  seeds=(new URLSearchParams(location.search).get("releases")||"").split(",").filter(s=>/^catalog-[1-9][0-9]*$/.test(s)).slice(0,32);
  const covered=new Set(cart.flatMap(e=>e.selection.release_ids||[e.selection.release_id]));seeds=seeds.filter(id=>!covered.has(id));
  if(seeds.length){await choose(seeds[0]);if(seeds.length>1)notice("Review this game's matching discs first. Add other games using search below.");}else await find();
}catch(e){notice(e.message);}})();
