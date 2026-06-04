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

async function loadHealth(){
  try{
    const h=await api("GET","/api/admin/health");
    document.getElementById("h-listener").innerHTML=h.goathub.listener_enabled?'<span class="pill on">on</span>':'<span class="pill off">off</span>';
    document.getElementById("h-net").textContent=h.goathub.net;
    document.getElementById("h-builder").textContent=h.goathub.builder_configured?h.goathub.builder_fee:"off";
    document.getElementById("h-sb").innerHTML=h.signalbot.reachable?'<span class="pill on">reachable</span>':'<span class="pill off">unreachable</span>';
    const c=h.signalbot.last_cycle_summary;
    document.getElementById("h-cycle").textContent=c?(c.ts+" — "+c.text):(h.signalbot.error||"—");
  }catch(e){console.error("health:",e);}
}

async function loadUsers(){
  const users=await api("GET","/api/admin/users");
  document.getElementById("user-count").textContent=users.length;
  const tb=document.querySelector("#utbl tbody"); tb.innerHTML="";
  users.forEach(u=>{
    const keyCls={ok:"on","address?!":"off","no key":"neutral",error:"off"}[u.key_status]||"neutral";
    const actions=u.bot_active
      ? `<button class="danger" onclick="pauseUser(${u.id})">pause</button>`
      : (u.key_status==="ok" ? `<button onclick="resumeUser(${u.id})">resume</button>` : '<span class="mut">no resume</span>');
    tb.insertAdjacentHTML("beforeend",
      `<tr>
        <td>${u.id}</td>
        <td>${esc(u.discord_username||u.email||"")}</td>
        <td>${u.bot_active?'<span class="pill on">on</span>':'<span class="pill off">off</span>'}</td>
        <td class="mut">${esc(u.address_short||"—")}</td>
        <td><span class="pill ${keyCls}">${esc(u.key_status)}</span></td>
        <td>${u.open_trades}</td>
        <td class="mut">${u.closed_trades}</td>
        <td class="${u.errors_24h>10?'':''}">${u.errors_24h>10?'<span class="pill off">'+u.errors_24h+'</span>':u.errors_24h}</td>
        <td>${actions}</td>
      </tr>`);
  });
}

async function loadActivity(kind){
  const url=kind?`/api/admin/activity?kind=${encodeURIComponent(kind)}`:"/api/admin/activity";
  const rows=await api("GET",url);
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
}

async function pauseUser(id){
  if(!confirm("Pause user "+id+"?"))return;
  try{await api("POST","/api/admin/users/"+id+"/pause"); loadUsers()}catch(e){alert(e.message)}
}
async function resumeUser(id){
  try{await api("POST","/api/admin/users/"+id+"/resume"); loadUsers()}catch(e){alert(e.message)}
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
          let html='<div style="font-size:12px;margin-bottom:8px;color:#9fb8af">Real tokens (last 10MB of bot.log):</div>';
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
  }catch(e){console.error("cost:",e)}
}

async function loadTrades(status){
  const url=status?`/api/admin/trades?status=${encodeURIComponent(status)}`:"/api/admin/trades";
  const rows=await api("GET",url);
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
        `<div style="margin:12px 0;padding:10px;background:#0a100d;border-radius:6px">
          <div style="font-weight:700;margin-bottom:6px">${esc(u.username)} <span class="mut" style="font-weight:normal;font-size:11px">· ${esc(u.address_short)}</span></div>
          ${coinHTML}
         </div>`);
    });
  }catch(e){console.error("per-coin:",e)}
}

async function loadAll(){
  try{
    // Quick guard: if not admin, /api/admin/users 403s — we show a banner
    await api("GET","/api/admin/health");
    document.getElementById("content").style.display="block";
    document.getElementById("auth-err").style.display="none";
    loadHealth(); loadUsers(); loadActivity(""); loadCost(); loadTrades(""); loadPerCoin();
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
  };
  const fn=handlers[action];
  if(fn) fn();
});
