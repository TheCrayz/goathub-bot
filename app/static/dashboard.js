// Phase 2 #18 (2026-06-02): hybrid auth.
//  - NEUE Logins → server setzt httpOnly+Secure+SameSite=Lax Session-Cookie.
//    Wir schreiben NICHTS mehr aktiv in localStorage — XSS kann den Token nicht stehlen.
//  - ALTE localStorage-Tokens funktionieren weiter (Bearer-Fallback, transition).
//  - Browser sendet das Cookie automatisch via `credentials: 'include'`.
const T=()=>localStorage.getItem("ght");  // legacy bearer fallback
// XSS-Fix (Phase 1, 2026-06-02): HTML-escape EVERY user-influenced value before
// it goes into innerHTML / insertAdjacentHTML. Activity texts come from upstream
// (exchange errors, parser output, Discord embeds) and could contain <script> etc.
function esc(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")}
function show(el,t,cls){el.textContent=t;el.className="msg "+(cls||"")}
async function api(m,u,b){
  // Cookie kommt automatisch via credentials:'include'. Bearer NUR senden,
  // falls noch alter localStorage-Token da ist (transition).
  const headers={};
  if(b)headers["Content-Type"]="application/json";
  const t=T(); if(t)headers["Authorization"]="Bearer "+t;
  const o={method:m,headers,credentials:"include"};
  if(b)o.body=JSON.stringify(b);
  const r=await fetch(u,o);
  if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||r.status);
  return r.json();
}
async function register(){try{await api("POST","/api/register",{email:email.value,password:pw.value});location.reload()}catch(e){show(authmsg,e.message,"err")}}
async function login(){try{await api("POST","/api/login",{email:email.value,password:pw.value});location.reload()}catch(e){show(authmsg,e.message,"err")}}
async function logout(){try{await api("POST","/api/logout")}catch(e){}localStorage.removeItem("ght");location.reload()}
async function saveWallet(){try{await api("POST","/api/wallet",{hl_account_address:addr.value,hl_api_secret:sec.value});show(wmsg,"Wallet saved ✓","ok");sec.value="";updateWalletLens();load()}catch(e){show(wmsg,e.message,"err")}}
// Phase 2 (2026-06-02): live length indicators on the wallet form. The
// address-in-key-field bug ate ~3 testers (users 3, 4, 6) — none knew
// which 0x-string went where. Now they see "42 / 42 ✓" or "20 / 66 ✗"
// as they type, and Save is disabled until both lengths match.
function updateWalletLens(){
  const av=addr.value.trim(), sv=sec.value.trim();
  const al=av.length, sl=sv.length;
  // Phase 6 (2026-06-02): also check 0x prefix — common copy-paste mistake.
  // 2026-06-04 Wallet-Wizard (Restposten #1): live-hints + swap-detection.
  const aPrefix=av.startsWith("0x"), sPrefix=sv.startsWith("0x");
  const aok=al===42 && aPrefix, sok=sl===66 && sPrefix;
  const ael=document.getElementById("addrlen"), sel=document.getElementById("seclen");
  const ahint=document.getElementById("addrhint"), shint=document.getElementById("sechint");
  let aMsg=al+" / 42";
  if(al===42 && !aPrefix) aMsg += " ✗ braucht 0x-Prefix";
  else if(aok) aMsg += " ✓";
  ael.textContent=aMsg; ael.style.color=al===0?"":(aok?"#3fe0a0":"#ff8a8a");
  let sMsg=sl+" / 66";
  if(sl===66 && !sPrefix) sMsg += " ✗ braucht 0x-Prefix";
  else if(sok) sMsg += " ✓";
  sel.textContent=sMsg; sel.style.color=sl===0?"":(sok?"#3fe0a0":"#ff8a8a");
  // Inline hints
  if(ahint){
    let m="", c="";
    if(al===0) {m=""; c="";}
    else if(al===66) {m="⚠ Das sieht aus wie der lange Agent-Key. MASTER ist die KURZE (42 Zeichen).";c="#ffaa44";}
    else if(al===42 && !aPrefix) {m="✗ Adresse braucht 0x-Prefix.";c="#ff8a8a";}
    else if(aok) {m="✓ Sieht gut aus.";c="#3fe0a0";}
    else {m=`Adresse hat 42 Zeichen, du hast ${al}.`;c="#ff8a8a";}
    ahint.textContent=m; ahint.style.color=c;
  }
  if(shint){
    let m="", c="";
    if(sl===0) {m=""; c="";}
    else if(sl===42) {m="⚠ Das sieht aus wie eine Adresse, nicht der Key. AGENT-KEY ist die LANGE (66 Zeichen, aus der roten Box).";c="#ffaa44";}
    else if(sl===66 && !sPrefix) {m="✗ Key braucht 0x-Prefix.";c="#ff8a8a";}
    else if(sok) {m="✓ Sieht gut aus.";c="#3fe0a0";}
    else {m=`Key hat 66 Zeichen, du hast ${sl}.`;c="#ff8a8a";}
    shint.textContent=m; shint.style.color=c;
  }
  const btn=document.getElementById("savewalletbtn");
  if(btn){btn.disabled=!(aok&&sok); btn.style.opacity=(aok&&sok)?"1":"0.5"; btn.style.cursor=(aok&&sok)?"pointer":"not-allowed";}
}
// attach as soon as inputs exist
document.addEventListener("DOMContentLoaded",function(){
  if(addr){addr.addEventListener("input",updateWalletLens);}
  if(sec){sec.addEventListener("input",updateWalletLens);}
  updateWalletLens();
  // 2026-06-04 CSP-Hardening (Restposten #2): alle onclick-Handler aus dem
  // HTML raus, hier via addEventListener verdrahten. Erlaubt CSP ohne
  // 'unsafe-inline' für script-src.
  const wire=(id,fn,ev)=>{const el=document.getElementById(id); if(el)el.addEventListener(ev||"click",fn);};
  wire("loginBtn", login);
  wire("registerBtn", register);
  wire("logoutBtn", logout);
  wire("toggle", toggleBot);
  wire("savewalletbtn", saveWallet);
  wire("saveSettingsBtn", saveSettings);
  wire("signbtn", signBuilderApproval);
  wire("bbtn", approveBuilder);
  wire("verifyBuilderBtn", verifyBuilder);
  wire("walletAnleitungBtn", function(){
    const w=document.getElementById("walletWizard");
    if(w){w.open=true; w.scrollIntoView({behavior:"smooth"});}
  });
});
async function saveSettings(){try{await api("PUT","/api/settings",{risk_pct:+risk.value,leverage:+lev.value,max_open_positions:+maxp.value,capital_cap_usdc:+cap.value});show(smsg,"Saved ✓","ok")}catch(e){show(smsg,e.message,"err")}}
async function approveBuilder(){try{const r=await api("POST","/api/builder-approved");show(bmsg,`Thank you — referral confirmed on-chain (${r.approved_bps} bps) ✓`,"ok");load();verifyBuilder()}catch(e){show(bmsg,e.message,"err")}}

// 2026-06-04: Per-Coin Filter Status für den User (Restposten #4).
// Vorher war diese Information nur im Admin-Dashboard sichtbar — Tester
// wussten nicht warum ein Coin nicht traded wurde. Jetzt direkt im Dashboard.
async function loadPerCoinStatus(){
  try{
    const s=await api("GET","/api/per-coin-status");
    const tb=document.querySelector("#pctbl tbody");
    const empty=document.getElementById("pcempty");
    const minT=document.getElementById("pcMinTrades");
    const minR=document.getElementById("pcMinRate");
    if(minT) minT.textContent=String(s.min_trades_required||10);
    if(minR) minR.textContent="< "+Math.round((s.min_winrate||0.3)*100)+"%";
    if(!tb) return;
    tb.innerHTML="";
    if(!s.connected){
      if(empty) {empty.textContent="Wallet nicht verbunden — Per-Coin-Tracking inaktiv.";empty.style.display="block";}
      document.getElementById("pctbl").style.display="none";
      return;
    }
    const coins=s.coins||[];
    if(coins.length===0){
      if(empty) {empty.textContent="Noch keine Trade-History auf HL — nichts geblockt. Trade ein paar Coins damit der Filter Daten hat.";empty.style.display="block";}
      document.getElementById("pctbl").style.display="none";
      return;
    }
    document.getElementById("pctbl").style.display="";
    if(empty) empty.style.display="none";
    coins.forEach(c=>{
      const winPct=(c.win_rate*100).toFixed(1)+"%";
      const status=c.blocked
        ? `<span class="pill off" title="Block aktiv: Win-Rate unter Schwelle">🛑 blocked</span>`
        : (c.trades<(s.min_trades_required||10)
            ? `<span class="pill mut" title="Noch unter Trade-Schwelle — Filter inaktiv">📊 sampling</span>`
            : `<span class="pill on" title="Performance ok">✓ allowed</span>`);
      tb.insertAdjacentHTML("beforeend",
        `<tr><td>${esc(c.coin)}</td><td>${c.trades}</td><td>${c.wins}</td><td>${esc(winPct)}</td><td>${status}</td></tr>`);
    });
  }catch(e){
    console.warn("per-coin status load failed:", e);
  }
}

// Phase 6+ (2026-06-03): MetaMask-driven on-chain Builder-Approval.
// HL's UI hat keinen "Add Builder"-Button — die Approval muss von einer App
// kommen, die EIP-712-signTypedData triggert. WIR sind die App.
//
// Flow:
//   1. window.ethereum verbinden + Address-Check (muss MASTER-Adresse sein,
//      die der User im Dashboard registriert hat).
//   2. EIP-712-Payload bauen (per HL Python-SDK-Specs).
//   3. signTypedData_v4 → MetaMask-Popup.
//   4. Signatur (0x{r:64}{s:64}{v:2}) splitten + POST an Server-Proxy.
//   5. Server-Proxy postet an HL /exchange + setzt DB-Flag bei Erfolg.
async function signBuilderApproval(){
  const btn=document.getElementById("signbtn");
  const _orig=btn.textContent;
  const setBtn=(t,disabled=true)=>{btn.textContent=t;btn.disabled=disabled;btn.style.opacity=disabled?"0.6":"1";};
  try{
    setBtn("⏳ Connecting MetaMask…");
    if(typeof window.ethereum==="undefined"){
      throw new Error("MetaMask ist nicht installiert/erkannt. Installier MetaMask im Browser und reload diese Seite.");
    }
    // Aktuelle Dashboard-Daten frisch holen (wir brauchen hl_account_address + builder + net)
    const d=await api("GET","/api/dashboard");
    const masterAddr=(d.user.hl_account_address||"").toLowerCase();
    const builderAddr=(d.builder&&d.builder.address||"").toLowerCase();
    const maxFeeRate=(d.builder&&d.builder.fee)||"0.05%";
    const net=d.net||"testnet";
    if(!masterAddr)  throw new Error("Wallet im Dashboard nicht gespeichert — erst speichern, dann approven.");
    if(!builderAddr) throw new Error("Server hat keine BUILDER_ADDRESS konfiguriert.");

    // MetaMask connect + check address
    setBtn("⏳ Waiting for MetaMask account…");
    const accounts=await window.ethereum.request({method:"eth_requestAccounts"});
    if(!accounts||accounts.length===0) throw new Error("Kein MetaMask-Account verbunden.");
    const connected=String(accounts[0]).toLowerCase();
    if(connected!==masterAddr){
      throw new Error(
        `Falsche Wallet in MetaMask. Du bist eingeloggt als ${connected.slice(0,6)}…${connected.slice(-4)}, `+
        `aber das Dashboard erwartet die MASTER-Adresse ${masterAddr.slice(0,6)}…${masterAddr.slice(-4)}. `+
        `Wechsle in MetaMask den Account.`
      );
    }

    // HL erwartet EIP-712-Domain auf chainId 0x66eee (= 421614, Arb Sepolia)
    // — unabhängig davon ob du auf Testnet oder Mainnet handelst. MetaMask
    // blockiert signTypedData_v4 wenn domain.chainId nicht zur aktiven Chain
    // passt, also vor dem Sign die Chain switchen (oder adden, falls fehlt).
    setBtn("⏳ Switching network to Arbitrum Sepolia…");
    const HL_CHAIN_ID_HEX="0x66eee"; // = 421614
    try{
      await window.ethereum.request({
        method:"wallet_switchEthereumChain",
        params:[{chainId:HL_CHAIN_ID_HEX}],
      });
    }catch(switchErr){
      if(switchErr&&switchErr.code===4902){
        // Chain noch nicht im Wallet — adden (MetaMask switched dann auto)
        setBtn("⏳ Adding Arbitrum Sepolia to MetaMask…");
        await window.ethereum.request({
          method:"wallet_addEthereumChain",
          params:[{
            chainId:HL_CHAIN_ID_HEX,
            chainName:"Arbitrum Sepolia",
            nativeCurrency:{name:"ETH",symbol:"ETH",decimals:18},
            rpcUrls:["https://sepolia-rollup.arbitrum.io/rpc"],
            blockExplorerUrls:["https://sepolia.arbiscan.io"],
          }],
        });
      }else if(switchErr&&switchErr.code===4001){
        throw new Error("Du hast den Chain-Switch abgelehnt. HL signiert auf chainId 0x66eee (Arb Sepolia) — das muss in MetaMask aktiv sein. Du kannst jederzeit zurück auf Arb One switchen.");
      }else{
        throw switchErr;
      }
    }

    // EIP-712-Payload nach HL-SDK-Spec (signing.py:user_signed_payload)
    const nonce=Date.now();
    const hyperliquidChain=(net==="mainnet")?"Mainnet":"Testnet";
    const typedData={
      types:{
        EIP712Domain:[
          {name:"name",type:"string"},
          {name:"version",type:"string"},
          {name:"chainId",type:"uint256"},
          {name:"verifyingContract",type:"address"},
        ],
        "HyperliquidTransaction:ApproveBuilderFee":[
          {name:"hyperliquidChain",type:"string"},
          {name:"maxFeeRate",type:"string"},
          {name:"builder",type:"address"},
          {name:"nonce",type:"uint64"},
        ],
      },
      primaryType:"HyperliquidTransaction:ApproveBuilderFee",
      domain:{
        name:"HyperliquidSignTransaction",
        version:"1",
        // HL nutzt 0x66eee (Arbitrum-Sepolia-id) als universellen signatureChainId,
        // sowohl für Testnet als auch Mainnet — siehe SDK signing.sign_user_signed_action.
        chainId:421614,
        verifyingContract:"0x0000000000000000000000000000000000000000",
      },
      message:{
        hyperliquidChain:hyperliquidChain,
        maxFeeRate:maxFeeRate,
        builder:builderAddr,
        nonce:nonce,
      },
    };

    setBtn("⏳ Sign in MetaMask…");
    const sig=await window.ethereum.request({
      method:"eth_signTypedData_v4",
      params:[connected,JSON.stringify(typedData)],
    });
    if(!sig||!sig.startsWith("0x")||sig.length<132){
      throw new Error("Ungültige Signatur von MetaMask zurückbekommen.");
    }
    // Signatur splitten: 0x{r:64}{s:64}{v:2}
    const r="0x"+sig.slice(2,66);
    const s="0x"+sig.slice(66,130);
    const v=parseInt(sig.slice(130,132),16);

    setBtn("⏳ Submitting to Hyperliquid…");
    const action={
      type:"approveBuilderFee",
      hyperliquidChain:hyperliquidChain,
      signatureChainId:"0x66eee",
      maxFeeRate:maxFeeRate,
      builder:builderAddr,
      nonce:nonce,
    };
    const res=await api("POST","/api/builder-approval-submit",{action,signature:{r,s,v},nonce});

    show(bmsg,`Builder-Approval on-chain bestätigt (${res.approved_bps||"?"} bps) ✓`,"ok");
    setBtn(_orig,false);
    load();          // refresh dashboard (bot_active, builder_approved, etc.)
    verifyBuilder(); // refresh on-chain status indicator
  }catch(e){
    show(bmsg,e.message||String(e),"err");
    setBtn(_orig,false);
  }
}
// Phase 5 (2026-06-02): explicit on-chain verification button.
async function verifyBuilder(){
  const el=document.getElementById("bonchain");
  if(el)el.textContent="checking…";
  try{
    const s=await api("GET","/api/builder-status");
    if(!s.configured){if(el)el.textContent="server has no BUILDER_ADDRESS configured";return}
    if(!s.user_wallet_connected){if(el)el.textContent="connect wallet first";return}
    if(s.error){if(el){el.textContent="error: "+s.error; el.style.color="#ff8a8a"}return}
    if(s.on_chain_ok){
      if(el){el.textContent="✓ approved on-chain ("+s.on_chain_bps+" bps, need "+s.required_bps+")"; el.style.color="#3fe0a0"}
    }else{
      if(el){el.textContent="✗ not approved on-chain ("+s.on_chain_bps+" bps, need "+s.required_bps+") — approve in Hyperliquid UI"; el.style.color="#ff8a8a"}
    }
  }catch(e){if(el){el.textContent="error: "+e.message; el.style.color="#ff8a8a"}}
}
async function toggleBot(){try{const me=await api("GET","/api/me");await api("PUT","/api/settings",{bot_active:!me.bot_active});load()}catch(e){alert(e.message)}}
let STATS=null, ACCOUNT=null, TF=30;
function fmtUsd(v){return (v>=0?"+$":"−$")+Math.abs(v).toFixed(2)}
function fmtSigned(v){return (v>=0?"+":"−")+Math.abs(v).toFixed(2);}
function seriesFor(days){
  const s=(STATS&&STATS.pnl_series)||[];
  if(!s.length) return {pts:[],total:0};
  if(days===0) return {pts:s.slice(),total:s[s.length-1].cum};
  const start=Date.now()-days*86400000;
  let base=0; const inWin=[];
  for(const p of s){ if(p.t<start) base=p.cum; else inWin.push(p); }
  const last=inWin.length?inWin[inWin.length-1].cum:base;
  return {pts:[{t:start,cum:base}].concat(inWin),total:+(last-base).toFixed(2)};
}
function drawChart(pts){
  const svg=document.getElementById("chart"), empty=document.getElementById("chartempty");
  if(!pts||pts.length<2){svg.innerHTML="";svg.style.display="none";empty.style.display="block";return;}
  svg.style.display="block";empty.style.display="none";
  const W=600,H=170,pad=8;
  const xs=pts.map(p=>p.t), ys=pts.map(p=>p.cum);
  const minX=Math.min.apply(null,xs),maxX=Math.max.apply(null,xs);
  let minY=Math.min.apply(null,ys.concat([0])),maxY=Math.max.apply(null,ys.concat([0]));
  if(minY===maxY){maxY=minY+1;minY=minY-1;}
  const sx=t=>pad+(W-2*pad)*((t-minX)/((maxX-minX)||1));
  const sy=v=>pad+(H-2*pad)*(1-((v-minY)/((maxY-minY)||1)));
  const line=pts.map((p,i)=>(i?"L":"M")+sx(p.t).toFixed(1)+" "+sy(p.cum).toFixed(1)).join(" ");
  const area=line+" L "+sx(maxX).toFixed(1)+" "+(H-pad).toFixed(1)+" L "+sx(minX).toFixed(1)+" "+(H-pad).toFixed(1)+" Z";
  const zeroY=sy(0).toFixed(1);
  svg.innerHTML='<defs><linearGradient id="gpnl" x1="0" y1="0" x2="0" y2="1">'+
    '<stop offset="0" stop-color="#10b981" stop-opacity="0.32"/>'+
    '<stop offset="1" stop-color="#10b981" stop-opacity="0"/></linearGradient></defs>'+
    '<line x1="0" y1="'+zeroY+'" x2="'+W+'" y2="'+zeroY+'" stroke="#26342d" stroke-width="1" stroke-dasharray="4 4"/>'+
    '<path class="area" d="'+area+'" fill="url(#gpnl)"/>'+
    '<path class="line" d="'+line+'" fill="none" stroke="#10b981" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>';
}
function renderStats(){
  if(!STATS) return;
  const accountValue = ACCOUNT && ACCOUNT.account_value != null ? ACCOUNT.account_value : (ACCOUNT && ACCOUNT.balance != null ? ACCOUNT.balance : 0);
  const unrealized = ACCOUNT && ACCOUNT.unrealized_pnl != null ? ACCOUNT.unrealized_pnl : 0;
  const exposure = ACCOUNT && ACCOUNT.open_exposure != null ? ACCOUNT.open_exposure : 0;
  const openCount = ACCOUNT && ACCOUNT.open_positions != null ? ACCOUNT.open_positions : (STATS.active_trades || 0);
  document.getElementById("statwin").textContent=(STATS.win_rate||0)+"%";
  document.getElementById("stattrades").textContent=(STATS.closed_trades||0)+" closed trades";
  const statact=document.getElementById("statact"); if(statact) statact.textContent=STATS.active_trades||0;  // 2026-06-12 FIX: id="statact" gibt's im Redesign nicht mehr → Null-Guard, sonst warf renderStats hier ab und ALLE Karten danach blieben "—"
  const statAccount=document.getElementById("statAccount"); if(statAccount) statAccount.textContent="$"+accountValue.toFixed(2);
  const statUnrealized=document.getElementById("statUnrealized"); if(statUnrealized){ statUnrealized.textContent=fmtSigned(unrealized); statUnrealized.className="stat-v "+(unrealized>=0?"pos":"neg"); }
  const statExposure=document.getElementById("statExposure"); if(statExposure) statExposure.textContent="$"+exposure.toFixed(2);
  const statOpen=document.getElementById("statOpen"); if(statOpen) statOpen.textContent=openCount;
  const miniAccount=document.getElementById("miniAccount"); if(miniAccount) miniAccount.textContent="$"+accountValue.toFixed(2);
  const miniUnrealized=document.getElementById("miniUnrealized"); if(miniUnrealized){ miniUnrealized.textContent=fmtSigned(unrealized); miniUnrealized.className="mini-value "+(unrealized>=0?"pos":"neg"); }
  const miniOpen=document.getElementById("miniOpen"); if(miniOpen) miniOpen.textContent=openCount;
  const miniClosed=document.getElementById("miniClosed"); if(miniClosed) miniClosed.textContent=(STATS.closed_trades||0);
  const marketWin=document.getElementById("marketWin"); if(marketWin) marketWin.textContent=(STATS.win_rate||0)+"%";
  const marketClosed=document.getElementById("marketClosed"); if(marketClosed) marketClosed.textContent=(STATS.closed_trades||0);
  const marketExposure=document.getElementById("marketExposure"); if(marketExposure) marketExposure.textContent="$"+exposure.toFixed(2);
  const marketRisk=document.getElementById("marketRisk"); if(marketRisk) marketRisk.textContent=(ACCOUNT && ACCOUNT.risk_pct != null ? ACCOUNT.risk_pct : (STATS.risk_pct || 0)) + "% / trade";
  const w=seriesFor(TF);
  const el=document.getElementById("statpnl");
  el.textContent=fmtUsd(w.total); el.className="stat-v "+(w.total>=0?"pos":"neg");
  drawChart(w.pts);
  const hb=document.querySelector("#histtbl tbody");hb.innerHTML="";
  (STATS.recent||[]).forEach(function(r){
    const cls=/long/i.test(r.dir||"")?"on":(/short/i.test(r.dir||"")?"off":"");
    const dir='<span class="pill '+cls+'">'+esc((r.dir||"—").toUpperCase())+'</span>';
    const pc=r.pnl>0?"pos":(r.pnl<0?"neg":"mut");
    const date=esc(new Date(r.t).toLocaleString());
    hb.insertAdjacentHTML("beforeend","<tr><td class=\"mut\">"+date+"</td><td><b>"+esc(r.coin||"")+"</b></td><td>"+dir+"</td><td>$"+esc(r.px)+"</td><td class=\""+pc+"\">"+(r.pnl>0?"+":"")+esc(r.pnl.toFixed(2))+"</td></tr>");
  });
  document.getElementById("histempty").style.display=(STATS.recent||[]).length?"none":"block";
}
document.querySelectorAll(".tfbtn").forEach(function(b){b.onclick=function(){
  TF=+b.dataset.d;
  document.querySelectorAll(".tfbtn").forEach(function(x){x.classList.remove("on")});
  b.classList.add("on"); renderStats();
}});
async function load(){
  try{const d=await api("GET","/api/dashboard");
    auth.classList.add("hide");app.classList.remove("hide");
    // 2026-06-08: Mainnet-aware UI — Pille + Banner-Style switch. Banner-text
    // selbst ist immer der Risk-Warning (siehe HTML); auf Mainnet zusätzlich
    // roter Style + 'real money' emphasis.
    const isMain = (d.net === "mainnet");
    // 2026-06-12 FIX: redundantes netbadge-Update hier ENTFERNT — es nutzte
    // `netbadge` VOR der `const netbadge`-Deklaration weiter unten (Temporal Dead
    // Zone → ReferenceError → load() brach ab, ALLE Metriken blieben "—"). Das
    // Badge wird unten (const-Block) korrekt gesetzt.
    const banner = document.getElementById("netbanner");
    const banTitle = document.getElementById("netbanner-title");
    if (banner) {
      if (isMain) {
        banner.style.borderColor = "#ff5555";
        banner.style.background = "#1a0707";
        banner.style.color = "#ffd2d2";
        if (banTitle) banTitle.innerHTML = "🚨 MAINNET — REAL MONEY at risk. Read carefully before trading.";
      } else {
        banner.style.borderColor = "";
        banner.style.background = "";
        banner.style.color = "";
        if (banTitle) banTitle.innerHTML = "High-Risk Trading — read before using";
      }
    }
    const hlUrl = document.getElementById("hl-url");
    if (hlUrl) {
      hlUrl.href = isMain ? "https://app.hyperliquid.xyz" : "https://app.hyperliquid-testnet.xyz";
      hlUrl.textContent = isMain ? "app.hyperliquid.xyz" : "app.hyperliquid-testnet.xyz";
    }
    const foot = document.getElementById("foot-net");
    if (foot) {
      foot.textContent = isMain
        ? "GoatHub Trading Bot · MAINNET — real money"
        : "GoatHub Trading Bot · Testnet — no real money";
    }
    const heroStatus = document.getElementById("heroStatus");
    const heroNet = document.getElementById("heroNet");
    const heroWallet = document.getElementById("heroWallet");
    if (heroStatus) heroStatus.textContent = d.user.bot_active ? "Trading enabled" : "Standby";
    if (heroNet) heroNet.textContent = isMain ? "Mainnet" : "Testnet";
    if (heroWallet) heroWallet.textContent = d.user.wallet_connected ? "Connected" : "Connect wallet";
    // Show Discord username + avatar if available, else email
    const displayName=d.user.discord_username||d.user.email;
    document.getElementById("uname").textContent=displayName;
    const av=document.getElementById("uavatar");
    if(d.user.discord_avatar_url){av.src=d.user.discord_avatar_url;av.classList.remove("hide")}
    // Balance: show "— Connect wallet" only if null, no overlap
    ubal.textContent=d.account.balance==null?"—":d.account.balance+" USDC";
    if(d.account.balance==null){
      const b=document.createElement("div");b.className="mut";b.style.fontSize="12px";b.textContent="Connect wallet to see balance";
      ubal.after(b);
    }
    const on=d.user.bot_active; botpill.textContent=on?"on":"off"; botpill.className="pill "+(on?"on":"off");
    const netbadge = document.getElementById("netbadge");
    if (netbadge) {
      netbadge.textContent = isMain ? "MAINNET" : "testnet";
      netbadge.className = "pill " + (isMain ? "off" : "on") + " pulse";
    }
    document.getElementById("toggle").textContent=on?"Disable Bot":"Enable Bot";
    // Phase 3 (2026-06-02): admin link nur für is_admin user zeigen.
    const al=document.getElementById("adminlink"); if(al){if(d.user.is_admin)al.classList.remove("hide");else al.classList.add("hide")}
    wstat.textContent=d.user.wallet_connected?"connected ✓":"not connected";
    const s=d.user.settings; risk.value=s.risk_pct;lev.value=s.leverage;maxp.value=s.max_open_positions;cap.value=s.capital_cap_usdc;
    baddr.textContent=(d.builder&&d.builder.address)||"(not set)"; bfee.textContent=(d.builder&&d.builder.fee)||"—";
    const ba=d.user.builder_approved; bstat.textContent=ba?"confirmed":"not confirmed"; bstat.className="pill "+(ba?"on":"off");
    // Phase 6 (2026-06-02): hide "confirm"-button wenn schon bestätigt; zeige stattdessen "Re-verify".
    const bbtn=document.getElementById("bbtn"); if(bbtn){bbtn.textContent=ba?"Re-verify on-chain":"Builder fee approved — confirm";}
    // 2026-06-04: wenn der Server keine BUILDER_ADDRESS hat, ist die ganze Sektion off.
    // Trades laufen dann ohne Builder-Code, also disable Buttons + zeig klaren Status.
    const builderOff=!(d.builder&&d.builder.address);
    const signbtn=document.getElementById("signbtn");
    const verifybtn=document.getElementById("verifyBuilderBtn");
    [signbtn,bbtn,verifybtn].forEach(b=>{if(b){b.disabled=builderOff;b.style.opacity=builderOff?"0.4":"1";b.style.cursor=builderOff?"not-allowed":"";}});
    if(builderOff){
      bstat.textContent="disabled (Testnet)";bstat.className="pill mut";
      const onchain=document.getElementById("bonchain");
      if(onchain){onchain.textContent="Builder-Fee aktuell deaktiviert — Trades laufen ohne Builder-Code.";onchain.style.color="";}
    }
    const pb=document.querySelector("#postbl tbody");pb.innerHTML="";
    const cards=document.getElementById("positionCards"); if(cards) cards.innerHTML="";
    const positions=d.account.positions||[];
    positions.forEach(p=>{
      const side=(Number(p.size)||0) >= 0 ? "Long" : "Short";
      const up=(Number(p.uPnl)||0);
      pb.insertAdjacentHTML("beforeend",`<tr><td>${esc(p.coin)}</td><td>${esc(p.size)}</td><td>${esc(p.entry)}</td><td class="${up>=0?'pos':'neg'}">${up>=0?'+':''}${esc(Number(p.uPnl||0).toFixed(2))}</td></tr>`);
      if(cards){ cards.insertAdjacentHTML("beforeend", `
        <article class="position-card ${up>=0?'heat-positive':'heat-negative'}">
          <div class="position-top">
            <div>
              <div class="position-coin">${esc(p.coin)}</div>
              <div class="position-side">${esc(side)} position</div>
            </div>
            <span class="pill ${up>=0?'on':'off'}">${up>=0?'+':''}${Number(up).toFixed(2)} uPnL</span>
          </div>
          <div class="position-metrics">
            <div class="metric-box"><div class="label">Size</div><div class="value">${esc(Number(p.size||0).toFixed(4))}</div></div>
            <div class="metric-box"><div class="label">Entry</div><div class="value">$${esc(Number(p.entry||0).toFixed(2))}</div></div>
            <div class="metric-box"><div class="label">uPnL</div><div class="value ${up>=0?'pos':'neg'}">${up>=0?'+':''}${esc(Number(p.uPnl||0).toFixed(2))}</div></div>
            <div class="metric-box"><div class="label">Notional</div><div class="value">$${esc((Math.abs(Number(p.size||0))*Math.abs(Number(p.entry||0))).toFixed(2))}</div></div>
          </div>
        </article>`); }
    });
    posempty.style.display=positions.length?"none":"block";
    // 2026-06-04: Per-Coin Filter Status für den User (Restposten #4).
    loadPerCoinStatus();
    const ab=document.querySelector("#acttbl tbody");ab.innerHTML="";
    d.activity.forEach(a=>{ab.insertAdjacentHTML("beforeend",`<tr><td class="mut">${esc((a.ts||"").replace("T"," "))}</td><td>${esc(a.kind)}</td><td>${esc(a.text)}</td></tr>`)});
    actempty.style.display=d.activity.length?"none":"block";
    ACCOUNT=d.account||null; STATS=d.stats||null; renderStats();
  }catch(e){localStorage.removeItem("ght")}
}
// Handle Discord OAuth redirect params
(function(){
  const p=new URLSearchParams(location.search);
  // Phase 2 #18: Server setzt httpOnly Cookie direkt — wir brauchen kein
  // #token=… mehr. Falls noch ein altes Fragment dasteht, einfach ignorieren
  // und URL aufräumen (Cookie ist schon gesetzt).
  if(location.hash && location.hash.indexOf("token=")>=0){
    history.replaceState(null,"","/");
  }
  const err=p.get("error");
  if(err){
    const msgs={
      no_role:"You need the @Goat Hub Supporter role to use this bot. Get it at <a href='https://goathub.network/join.html' target='_blank'>goathub.network/join</a>",
      discord_denied:"Discord login was cancelled.",
      oauth_state_mismatch:"OAuth security check failed. Please try logging in again.",
      oauth_failed:"Login failed. Please try again."
    };
    const el=document.getElementById("autherror");
    if(el){el.innerHTML=msgs[err]||"Unknown error.";el.style.display="block"}
    history.replaceState(null,"","/");
  }
})();
// 2026-06-12: Öffentlicher Netzwerk-/Status-Badge — läuft VOR/ohne Login, damit
// der Hero nie ein veraltetes "testnet" oder falschen Status zeigt. /api/health
// ist auth-frei und liefert {ok, listener, net}.
(async function publicStatus(){
  try{
    const h = await (await fetch("/api/health", {credentials:"include"})).json();
    const isMain = h.net === "mainnet";
    const nb=document.getElementById("netbadge");
    if(nb){ nb.textContent = isMain?"MAINNET":"testnet"; nb.className = "pill "+(isMain?"off":"on")+" pulse"; }
    const hn=document.getElementById("heroNet"); if(hn) hn.textContent = isMain?"Mainnet":"Testnet";
    const hs=document.getElementById("heroStatus"); if(hs) hs.textContent = h.ok ? "Online" : "Offline";
  }catch(e){
    const hs=document.getElementById("heroStatus"); if(hs) hs.textContent = "Offline";
  }
})();

// Phase 2 #18: immer load() probieren — auth läuft via Cookie. Wenn 401,
// fängt load() das ab und zeigt Login-Form (auth section ist by default sichtbar).
load();

// 2026-06-08 Mainnet-Hardening B4: JWT-Refresh-Loop.
// JWT_EXPIRE_HOURS=24 (statt 168). Damit Tester nicht alle 24h re-loggen
// müssen, pollen wir alle 12h /api/refresh wenn user eingeloggt ist.
// Wenn der refresh failt (401 = Session abgelaufen während AFK), bleibt
// der bisherige Cookie tot und nächster load() zeigt Login-Form.
setInterval(async function(){
  try {
    const me = await api("GET", "/api/me");
    if (me && me.email) {  // eingeloggt
      await api("POST", "/api/refresh");
    }
  } catch (e) {
    // silent — beim nächsten load() landet User auf Login wenn nötig
  }
}, 12 * 60 * 60 * 1000);  // 12h
