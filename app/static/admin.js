function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}
async function api(m,u,b){
  const headers={};
  if(b)headers["Content-Type"]="application/json";
  const t=localStorage.getItem("ght"); if(t)headers["Authorization"]="Bearer "+t;
  const o={method:m,headers,credentials:"include"};
  if(b)o.body=JSON.stringify(b);
  const r=await fetch(u,o);
  if(!r.ok){const e=await r.json().catch(()=>({})); throw new Error(e.detail||r.status)}
  return r.json();
}

// 2026-06-12 Review #47: pro Tabelle eine monoton steigende Request-ID —
// nur die JEWEILS LETZTE Antwort darf rendern. Vorher konnten schnelle
// Filter-Klicks out-of-order auflösen und die langsamere, stale Antwort
// überschrieb den gerade gewählten Filter.
const REQ={users:0,trades:0,activity:0};
let LAST_REFRESH=0;

// 2026-06-12 Review #47: Fehler SICHTBAR als Tabellenzeile rendern statt
// silently failen (vorher: unhandled rejection -> leere Tabelle, kein Feedback).
function tableError(sel,cols,msg){
  const tb=document.querySelector(sel);
  if(tb)tb.innerHTML='<tr><td colspan="'+cols+'" class="mut">⚠ '+esc(msg)+'</td></tr>';
}

// 2026-06-12 Review #47: aktiven Filter-Button markieren, damit sichtbar ist
// welcher Filter gerade gilt (data-arg "" = "all"-Button ohne data-arg).
function markActive(action,arg){
  document.querySelectorAll('button[data-action="'+action+'"]').forEach(b=>{
    b.classList.toggle("active",(b.getAttribute("data-arg")||"")===(arg||""));
  });
}

async function loadHealth(){
  try{
    const h=await api("GET","/api/admin/health");
    document.getElementById("h-listener").innerHTML=h.goathub.listener_enabled?'<span class="pill on">on</span>':'<span class="pill off">off</span>';
    document.getElementById("h-net").textContent=h.goathub.net;
    document.getElementById("h-builder").textContent=h.goathub.builder_configured?h.goathub.builder_fee:"off";
    document.getElementById("h-sb").innerHTML=h.signalbot.reachable?'<span class="pill on">reachable</span>':'<span class="pill off">unreachable</span>';
    const c=h.signalbot.last_cycle_summary;
    document.getElementById("h-cycle").textContent=c?(c.ts+" — "+c.text):(h.signalbot.error||"—");
  }catch(e){
    console.error("health:",e);
    // 2026-06-12 Review #47: Fehler sichtbar machen statt nur console
    document.getElementById("h-cycle").textContent="health load failed: "+e.message;
  }
}

// 2026-06-12 Review #36: Emergency-Halt UI. Endpoints existieren seit dem
// 2026-06-08 Mainnet-Hardening A3 (admin.py), hatten aber KEIN UI — im
// Notfall musste der Admin curlen. GET /api/admin/halt-status liefert
// {emergency_halt_active, halt_reason, users_bot_active, users_total}.
async function loadHalt(){
  const state=document.getElementById("halt-state");
  const btn=document.getElementById("halt-btn");
  if(!state||!btn)return;
  try{
    const h=await api("GET","/api/admin/halt-status");
    if(h.emergency_halt_active){
      state.innerHTML='<span class="pill off">⛔ HALTED</span> <span class="mut">all incoming signals are ignored</span>';
      btn.textContent="▶ Resume signals";
      btn.className="resume-big";
      btn.setAttribute("data-action","clearHalt");
    }else{
      state.innerHTML='<span class="pill on">trading live</span> <span class="mut">signals are executed for active users</span>';
      btn.textContent="🚨 EMERGENCY HALT";
      btn.className="halt-big";
      btn.setAttribute("data-action","haltAll");
    }
    document.getElementById("halt-reason").textContent=(h.emergency_halt_active&&h.halt_reason)?("Reason: "+h.halt_reason):"";
    document.getElementById("halt-users").textContent=h.users_bot_active+" of "+h.users_total+" users bot-active";
  }catch(e){
    console.error("halt-status:",e);
    state.innerHTML='<span class="pill neutral">halt status unavailable</span> <span class="mut">'+esc(e.message)+'</span>';
  }
}

async function haltAll(){
  if(!confirm("🚨 EMERGENCY HALT\n\nPauses ALL active users immediately and ignores every further signal until cleared.\n\nProceed?"))return;
  try{await api("POST","/api/admin/halt")}catch(e){alert("Halt failed: "+e.message)}
  loadHalt(); loadUsers();
}
async function clearHalt(){
  if(!confirm("Clear EMERGENCY HALT?\n\nSignals will be processed again. Users stay paused until they re-activate their bot themselves."))return;
  try{await api("POST","/api/admin/halt/clear")}catch(e){alert("Clear failed: "+e.message)}
  loadHalt(); loadUsers();
}

async function loadUsers(){
  const seq=++REQ.users;
  try{
    const users=await api("GET","/api/admin/users");
    if(seq!==REQ.users)return;
    document.getElementById("user-count").textContent=users.length;
    const tb=document.querySelector("#utbl tbody"); tb.innerHTML="";
    users.forEach(u=>{
      const keyCls={ok:"on","address?!":"off","no key":"neutral",error:"off"}[u.key_status]||"neutral";
      // 2026-06-12 Review #11/#14: data-action statt inline-onclick — die CSP
      // (script-src 'self', main.py) blockt onclick-Attribute, die pause/
      // resume-Buttons waren komplett tot (einzige UI-Kontrolle gegen einen
      // misbehavenden User-Bot!). Delegation-Handler unten.
      const actions=u.bot_active
        ? `<button class="danger" data-action="pauseUser" data-arg="${u.id}">pause</button>`
        : (u.key_status==="ok" ? `<button data-action="resumeUser" data-arg="${u.id}">resume</button>` : '<span class="mut">no resume</span>');
      // 2026-06-12 Review #49: toter class-Ternary (immer "") entfernt —
      // der rote Pill bei >10 Errors rendert über den span direkt.
      tb.insertAdjacentHTML("beforeend",
        `<tr>
          <td>${u.id}</td>
          <td>${esc(u.discord_username||u.email||"")}</td>
          <td>${u.bot_active?'<span class="pill on">on</span>':'<span class="pill off">off</span>'}</td>
          <td class="mut">${esc(u.address_short||"—")}</td>
          <td><span class="pill ${keyCls}">${esc(u.key_status)}</span></td>
          <td>${u.open_trades}</td>
          <td class="mut">${u.closed_trades}</td>
          <td>${u.errors_24h>10?'<span class="pill off">'+u.errors_24h+'</span>':u.errors_24h}</td>
          <td>${actions}</td>
        </tr>`);
    });
  }catch(e){
    if(seq!==REQ.users)return;
    tableError("#utbl tbody",9,"users load failed: "+e.message);
  }
}

async function loadActivity(kind){
  const seq=++REQ.activity;
  markActive("loadActivity",kind||"");
  const url=kind?`/api/admin/activity?kind=${encodeURIComponent(kind)}`:"/api/admin/activity";
  try{
    const rows=await api("GET",url);
    if(seq!==REQ.activity)return;
    const tb=document.querySelector("#atbl tbody"); tb.innerHTML="";
    rows.forEach(r=>{
      const kpill={error:'off',skip:'neutral',order:'on',update:'on',close:'on'}[r.kind]||'neutral';
      tb.insertAdjacentHTML("beforeend",
        `<tr>
          <td class="mut">${esc((r.ts||"").replace("T"," "))}</td>
          <td>${r.user_id||"—"}</td>
          <td><span class="pill ${kpill}">${esc(r.kind)}</span></td>
          <td>${esc(r.text||"")}</td>
        </tr>`);
    });
  }catch(e){
    if(seq!==REQ.activity)return;
    tableError("#atbl tbody",4,"activity load failed: "+e.message);
  }
}

async function pauseUser(id){
  if(!confirm("Pause user "+id+"?"))return;
  try{await api("POST","/api/admin/users/"+id+"/pause"); loadUsers(); loadHalt()}catch(e){alert(e.message)}
}
async function resumeUser(id){
  try{await api("POST","/api/admin/users/"+id+"/resume"); loadUsers(); loadHalt()}catch(e){alert(e.message)}
}

async function loadCost(){
  try{
    const c=await api("GET","/api/admin/cost");
    if(c.estimates){
      document.getElementById("c-cycles").textContent=c.estimates.cycles_sampled;
      document.getElementById("c-flash").textContent=c.estimates.flash_calls_total;
      document.getElementById("c-pro").textContent=c.estimates.pro_calls_total_est;
      document.getElementById("c-cyclecost").textContent="$"+(c.estimates.usd_per_cycle_avg||0);
      document.getElementById("c-weekcost").textContent="$"+(c.estimates.usd_per_week_extrapolated||0);
      document.getElementById("c-note").textContent=c.estimates.note||"";
    }
    // Phase 6+ (2026-06-03): real TOKEN_USAGE-basierte Cost-Daten anzeigen
    if(c.real){
      const r=c.real;
      const block=document.getElementById("real-cost-block");
      if(block){
        if(r.error||!r.per_model){
          block.innerHTML='<span class="mut">No real-cost data yet — restart signal-bot to start TOKEN_USAGE logging.'+(r.error?' Error: '+esc(r.error):'')+'</span>';
        }else{
          let html='<div style="font-size:12px;margin-bottom:8px;color:var(--mut)">Real tokens (last 10MB of bot.log):</div>';
          html+='<table><thead><tr><th>model</th><th>calls</th><th>prompt</th><th>output</th><th>thoughts</th><th>cached</th><th>USD</th></tr><tbody>';
          for(const [model,m] of Object.entries(r.per_model||{})){
            html+=`<tr><td><b>${esc(model)}</b></td><td>${m.calls}</td><td>${m.prompt.toLocaleString()}</td>
              <td>${m.output.toLocaleString()}</td><td>${m.thoughts.toLocaleString()}</td>
              <td class="mut">${m.cached.toLocaleString()}</td><td><b>$${m.usd.toFixed(4)}</b></td></tr>`;
          }
          html+=`</tbody></table><div style="margin-top:8px"><b>Total: ${r.total_calls} calls = $${(r.total_usd_so_far||0).toFixed(4)} USD</b></div>`;
          block.innerHTML=html;
        }
      }
    }
    const tb=document.querySelector("#ctbl tbody"); tb.innerHTML="";
    (c.cycles||[]).slice(0,15).forEach(x=>{
      tb.insertAdjacentHTML("beforeend",
        `<tr><td class="mut">${esc(x.ts)}</td><td>${x.processed}</td>
        <td>${x.hold}</td>
        <td>${x.signals_total} (${x.new_trade}/${x.update_trade}/${x.cancel_trade})</td>
        <td>${x.pro_calls_estimate}</td>
        <td>${x.ai_skip}</td><td>${x.scrape_fail}</td></tr>`);
    });
  }catch(e){
    console.error("cost:",e);
    // 2026-06-12 Review #47: Fehler sichtbar in die Tabelle statt nur console
    tableError("#ctbl tbody",7,"cost load failed: "+e.message);
  }
}

async function loadTrades(status){
  const seq=++REQ.trades;
  markActive("loadTrades",status||"");
  const url=status?`/api/admin/trades?status=${encodeURIComponent(status)}`:"/api/admin/trades";
  try{
    const rows=await api("GET",url);
    if(seq!==REQ.trades)return;
    const tb=document.querySelector("#ttbl tbody"); tb.innerHTML="";
    rows.forEach(t=>{
      const dirCls=t.direction==="LONG"?"on":"off";
      const stPill={open:"on",closed:"neutral",resting:"on"}[t.status]||"neutral";
      const tps=(t.take_profits||[]).map(p=>`${p.percent}%@${p.price}`).join(" / ")||"—";
      tb.insertAdjacentHTML("beforeend",
        `<tr><td>${t.id}</td><td>${t.user_id}</td><td><b>${esc(t.coin)}</b></td>
         <td><span class="pill ${dirCls}">${esc(t.direction||"")}</span></td>
         <td>${esc(t.entry||"—")}</td><td>${esc(t.stop_loss||"—")}</td>
         <td class="mut">${esc(tps)}</td>
         <td><span class="pill ${stPill}">${esc(t.status)}</span></td>
         <td class="mut">${esc((t.created_at||"").replace("T"," "))}</td></tr>`);
    });
  }catch(e){
    if(seq!==REQ.trades)return;
    tableError("#ttbl tbody",9,"trades load failed: "+e.message);
  }
}

async function loadPerCoin(){
  try{
    const d=await api("GET","/api/admin/per-coin");
    document.getElementById("pc-mintrades").textContent=d.min_trades_required;
    document.getElementById("pc-minrate").textContent=Math.round((d.min_winrate||0)*100)+"%";
    const root=document.getElementById("pc-list"); root.innerHTML="";
    if(!d.users||d.users.length===0){root.innerHTML="<span class='mut'>No active users with trades yet.</span>";return}
    d.users.forEach(u=>{
      let coinHTML=u.coins.length===0?"<span class='mut'>No closed trades yet.</span>":"";
      if(u.coins.length){
        coinHTML="<table><thead><tr><th>coin</th><th>trades</th><th>wins</th><th>win-rate</th><th>status</th></tr><tbody>";
        u.coins.sort((a,b)=>b.trades-a.trades).forEach(c=>{
          const wr=Math.round(c.win_rate*100);
          const wrCls=wr>=50?"on":(wr>=30?"neutral":"off");
          coinHTML+=`<tr><td><b>${esc(c.coin)}</b></td><td>${c.trades}</td><td>${c.wins}</td>
            <td><span class="pill ${wrCls}">${wr}%</span></td>
            <td>${c.blocked?'<span class="pill off">BLOCKED</span>':'<span class="pill on">ok</span>'}</td></tr>`;
        });
        coinHTML+="</tbody></table>";
      }
      root.insertAdjacentHTML("beforeend",
        `<div style="margin:12px 0;padding:10px;background:var(--panel-2);border-radius:6px">
          <div style="font-weight:700;margin-bottom:6px">${esc(u.username)} <span class="mut" style="font-weight:normal;font-size:11px">· ${esc(u.address_short)}</span></div>
          ${coinHTML}
         </div>`);
    });
  }catch(e){
    console.error("per-coin:",e);
    // 2026-06-12 Review #47: Fehler sichtbar machen statt nur console
    const root=document.getElementById("pc-list");
    if(root)root.innerHTML="<span class='mut'>⚠ per-coin load failed: "+esc(e.message)+"</span>";
  }
}

async function loadAll(){
  try{
    // Quick guard: if not admin, /api/admin/users 403s — we show a banner
    await api("GET","/api/admin/health");
    document.getElementById("content").style.display="block";
    document.getElementById("auth-err").style.display="none";
    // 2026-06-12 Review #36: Halt-Status gehört zum Pflicht-Load.
    // Loader fangen ihre Fehler selbst (Review #47), allSettled nur als Netz.
    await Promise.allSettled([loadHalt(),loadHealth(),loadUsers(),loadActivity(""),loadCost(),loadTrades(""),loadPerCoin()]);
    _markRefreshed();
  }catch(e){
    const banner=document.getElementById("auth-err");
    banner.style.display="block";
    if((e.message||"").includes("Admin")){
      banner.innerHTML="🚫 Admin only — you don't have access to this page. <a href='/'>Back to dashboard</a>";
    }else if((e.message||"").includes("Nicht angemeldet")||(e.message||"").includes("401")){
      banner.innerHTML="Not logged in. <a href='/'>Log in here</a> then come back.";
    }else{
      banner.textContent="Error: "+e.message;
    }
  }
}
loadAll();

// 2026-06-12 Auto-Refresh: Health + Users + Halt-Status alle 30s neu laden,
// aber nur bei sichtbarem Tab (kein Hintergrund-Polling gegen die Rate-Limits)
// und nur wenn der Admin-Content sichtbar ist (eingeloggt).
function _markRefreshed(){LAST_REFRESH=Date.now();_renderAgo();}
function _renderAgo(){
  const el=document.getElementById("updated-ago");
  if(!el)return;
  el.textContent=LAST_REFRESH?("Updated "+Math.max(0,Math.round((Date.now()-LAST_REFRESH)/1000))+"s ago"):"";
}
async function refreshLive(){
  if(document.visibilityState!=="visible")return;
  if(document.getElementById("content").style.display==="none")return;
  // 2026-06-13 Review-Fix: Users-Tabelle NICHT neu bauen, während der Admin
  // mit Maus/Fokus drin ist — sonst wandert der pause/resume-Button beim
  // 30s-Tick unter dem Cursor weg (UX-Hazard auf einem Emergency-Control).
  const utbl=document.getElementById("utbl");
  const userTableBusy=utbl&&(utbl.matches(":hover")||utbl.contains(document.activeElement));
  const jobs=[loadHealth(),loadHalt()];
  if(!userTableBusy)jobs.push(loadUsers());
  await Promise.allSettled(jobs);
  _markRefreshed();
}
setInterval(refreshLive,30000);
setInterval(_renderAgo,1000);
// Tab wieder sichtbar -> sofort aktualisieren statt bis zu 30s zu warten
document.addEventListener("visibilitychange",function(){if(document.visibilityState==="visible")refreshLive();});

// 2026-06-04 CSP-Hardening (Restposten #2): data-action event-delegation
// statt inline-onclick (script-src ohne 'unsafe-inline').
document.addEventListener("click", function(e){
  const btn=e.target.closest("[data-action]");
  if(!btn) return;
  const action=btn.getAttribute("data-action");
  const arg=btn.getAttribute("data-arg")||"";
  const handlers={
    loadAll: loadAll,
    loadTrades: function(){loadTrades(arg);},
    loadActivity: function(){loadActivity(arg);},
    // 2026-06-12 Review #11/#14: pause/resume über delegation — inline-onclick
    // war durch die CSP tot, die Buttons taten NICHTS.
    pauseUser: function(){pauseUser(arg);},
    resumeUser: function(){resumeUser(arg);},
    // 2026-06-12 Review #36: Emergency-Halt-Buttons
    haltAll: haltAll,
    clearHalt: clearHalt,
  };
  const fn=handlers[action];
  if(fn) fn();
});
