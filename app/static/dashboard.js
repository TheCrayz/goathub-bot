// Phase 2 #18 (2026-06-02): hybrid auth.
//  - NEUE Logins → server setzt httpOnly+Secure+SameSite=Lax Session-Cookie.
//    Wir schreiben NICHTS mehr aktiv in localStorage — XSS kann den Token nicht stehlen.
//  - ALTE localStorage-Tokens funktionieren weiter (Bearer-Fallback, transition).
//  - Browser sendet das Cookie automatisch via `credentials: 'include'`.
//
// 2026-06-12 Frontend-Overhaul:
//  - Live-Polling (15s, nur bei sichtbarem Tab) + "Updated Xs ago"-Chip.
//  - load() unterscheidet 401 (→ Login zeigen, Legacy-Token löschen) von
//    anderen Fehlern (→ Banner, letzte Daten bleiben stehen). Vorher schluckte
//    EIN catch alles und loggte User bei jedem Netzwerk-Blip aus.
//  - Toast-System statt alert(); Intl-Zahlenformatierung; Position-Cards mit
//    SL/TP/Leverage/Liq; Chart mit Gridlines + Crosshair; Onboarding-Stepper.
//  - Tote Branches entfernt (statact, marketExposure, topOpen*, mini*/market*
//    Duplikat-Karten, #postbl-Tabelle) — die Elemente existieren nicht mehr.
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
  if(!r.ok){
    // 2026-06-12: Error trägt jetzt r.status, damit load() 401 von 500/429
    // unterscheiden kann. detail kann bei 422 ein Array sein → stringify.
    let detail=null;
    try{detail=(await r.json()).detail}catch(_){}
    if(detail!=null&&typeof detail!=="string")detail=JSON.stringify(detail);
    const err=new Error(detail||("HTTP "+r.status));
    err.status=r.status;
    throw err;
  }
  return r.json();
}

// ── Toasts (2026-06-12): ersetzt alert() + verstreute Inline-Messages.
// Erfolg = Emerald-Kante, Fehler = Rot. Auto-dismiss 4s, Klick schließt,
// max. 3 gestapelt. Die Inline-Hints am Wallet-Formular bleiben bewusst.
function toast(msg,type){
  const c=document.getElementById("toasts"); if(!c)return;
  while(c.children.length>=3)c.removeChild(c.firstChild);
  const t=document.createElement("div");
  t.className="toast"+(type==="err"?" toast-err":"");
  t.textContent=msg;
  t.addEventListener("click",function(){t.remove()});
  c.appendChild(t);
  setTimeout(function(){t.classList.add("out");setTimeout(function(){t.remove()},350)},4000);
}

// ── Zahlenformatierung (2026-06-12): EIN fmt-Helfer mit Intl statt nacktem
// toFixed. USD = 2 Dezimalstellen + Tausender-Trenner; Sizes/Preise = 4
// signifikante Stellen (große Preise wie BTC behalten via Integer-Format
// ihre vollen Stellen, sonst würde 61767 zu 61770 gerundet).
const FMT_USD=new Intl.NumberFormat("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
const FMT_INT=new Intl.NumberFormat("en-US",{maximumFractionDigits:0});
const FMT_SIG=new Intl.NumberFormat("en-US",{maximumSignificantDigits:4});
function fmtUsd(v){v=Number(v);if(!isFinite(v))return "—";return (v<0?"−$":"$")+FMT_USD.format(Math.abs(v))}
function fmtUsdSigned(v){v=Number(v);if(!isFinite(v))return "—";return (v>=0?"+$":"−$")+FMT_USD.format(Math.abs(v))}
function fmtPx(v){v=Number(v);if(!isFinite(v))return "—";return Math.abs(v)>=10000?FMT_INT.format(v):FMT_SIG.format(v)}
function fmtSize(v){v=Number(v);if(!isFinite(v))return "—";return FMT_SIG.format(v)}
function trimNum(v){return String(+(+v).toFixed(2))}

// setStat: Wert setzen + Skeleton entfernen + .flash-up/.flash-down Pulse,
// wenn sich der numerische Wert seit dem letzten Render geändert hat.
function setStat(id,text,num){
  const el=document.getElementById(id); if(!el)return;
  el.classList.remove("skeleton");
  const prev=el.dataset.n;
  el.textContent=text;
  if(num!=null&&isFinite(num)){
    if(prev!==undefined&&prev!==""&&Number(prev)!==num){
      el.classList.remove("flash-up","flash-down"); void el.offsetWidth;
      el.classList.add(num>Number(prev)?"flash-up":"flash-down");
    }
    el.dataset.n=String(num);
  }else{
    delete el.dataset.n;
  }
}
function setStatCls(id,cls){const el=document.getElementById(id);if(el){el.classList.remove("pos","neg");if(cls)el.classList.add(cls)}}
function setHero(id,text){const el=document.getElementById(id);if(el){el.classList.remove("skeleton");el.textContent=text}}
// Flash-Klassen nach der Animation wieder entfernen (delegiert, einmalig)
document.addEventListener("animationend",function(e){
  const el=e.target;
  if(el&&el.classList&&(el.classList.contains("flash-up")||el.classList.contains("flash-down")))
    el.classList.remove("flash-up","flash-down");
});

async function register(){try{await api("POST","/api/register",{email:email.value,password:pw.value});location.reload()}catch(e){show(authmsg,e.message,"err")}}
async function login(){try{await api("POST","/api/login",{email:email.value,password:pw.value});location.reload()}catch(e){show(authmsg,e.message,"err")}}
async function logout(){try{await api("POST","/api/logout")}catch(e){}localStorage.removeItem("ght");location.reload()}
async function saveWallet(){
  try{
    await api("POST","/api/wallet",{hl_account_address:addr.value,hl_api_secret:sec.value});
    toast("Wallet saved ✓","ok");
    sec.value="";updateWalletLens();load();
  }catch(e){toast("Wallet nicht gespeichert: "+e.message,"err")}
}
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
    if(w){w.open=true; const wc=document.getElementById("wallet-connect"); if(wc)wc.open=true; w.scrollIntoView({behavior:"smooth"});}
  });
  // 2026-06-12 Stepper: Klick scrollt zum jeweiligen Setup-Schritt (und
  // klappt zugeklappte details-Cards vorher auf).
  ["step1","step2","step3"].forEach(function(id){
    const el=document.getElementById(id);
    if(el)el.addEventListener("click",function(){
      const t=document.getElementById(el.dataset.target);
      if(t){ if(t.tagName==="DETAILS")t.open=true; t.scrollIntoView({behavior:"smooth",block:"start"}); }
    });
  });
  // Risk-%-Hint live mitrechnen (Prozent ↔ Fraction)
  const r=document.getElementById("risk"); if(r)r.addEventListener("input",riskHintUpd);
  // Chart-Crosshair (Handler hängen am <svg>, überleben innerHTML-Rebuilds)
  const ch=document.getElementById("chart");
  if(ch){ch.addEventListener("pointermove",chartMove);ch.addEventListener("pointerleave",chartLeave);}
});

// ── Settings (2026-06-12, Findings 12+13):
//  - Das Risk-Feld zeigt PROZENT (0.5 = 0.5 %), die API spricht Fraction
//    (0.005). Konvertierung passiert NUR hier im JS.
//  - Leere Felder werden NIE als 0 gesendet (+'' === 0 hätte z. B. den
//    Capital Cap stillschweigend auf "ganzer Account" gesetzt) — Save wird
//    mit klarer Meldung geblockt.
//  - Die Form wird aus der PUT-Response neu befüllt (Server kann clampen),
//    damit nie ein anderer Wert angezeigt wird als in der DB steht.
function riskHintUpd(){
  const el=document.getElementById("riskhint"), r=document.getElementById("risk");
  if(!el||!r)return;
  const v=parseFloat(r.value);
  if(!isFinite(v)){el.textContent="";return;}
  el.textContent=trimNum(v)+" % = Anteil "+(v/100).toFixed(4)+" vom Account-Wert pro Trade";
  el.style.color=(v<0.05||v>5)?"#ffaa44":"";
}
function applySettings(s){
  if(!s)return;
  SETTINGS=s;
  const r=document.getElementById("risk"), l=document.getElementById("lev"),
        m=document.getElementById("maxp"), c=document.getElementById("cap");
  if(r)r.value=+((s.risk_pct||0)*100).toFixed(3);
  if(l)l.value=s.leverage;
  if(m)m.value=s.max_open_positions;
  if(c)c.value=s.capital_cap_usdc;
  riskHintUpd();
}
async function saveSettings(){
  const smsgEl=document.getElementById("smsg");
  const r=document.getElementById("risk"), l=document.getElementById("lev"),
        m=document.getElementById("maxp"), c=document.getElementById("cap");
  // Blank-Guard für ALLE Felder
  for(const el of [r,l,m,c]){
    if(!el||el.value.trim()===""){
      show(smsgEl,"Bitte alle Felder ausfüllen — ein leeres Feld wird NICHT als 0 gespeichert.","err");
      toast("Settings nicht gespeichert — leeres Feld.","err");
      return;
    }
  }
  const rp=parseFloat(r.value), lv=parseFloat(l.value), mp=parseFloat(m.value), cp=parseFloat(c.value);
  let errMsg=null;
  if(!isFinite(rp)||rp<0.05||rp>5) errMsg="Risk % muss zwischen 0.05 und 5 liegen (0.5 = 0.5 % pro Trade).";
  else if(!isFinite(lv)||lv<1||lv>50) errMsg="Max Leverage Cap muss zwischen 1 und 50 liegen.";
  else if(!isFinite(mp)||mp<1||mp>20) errMsg="Max Open Positions muss zwischen 1 und 20 liegen.";
  else if(!isFinite(cp)||cp<0) errMsg="Capital Cap darf nicht negativ sein (0 = ganzer Account).";
  if(errMsg){show(smsgEl,errMsg,"err");toast("Settings nicht gespeichert: "+errMsg,"err");return;}
  try{
    const resp=await api("PUT","/api/settings",{
      risk_pct:rp/100,                 // Prozent → Fraction
      leverage:Math.round(lv),
      max_open_positions:Math.round(mp),
      capital_cap_usdc:cp
    });
    if(resp&&resp.settings)applySettings(resp.settings);  // Server-Clamps anzeigen
    localStorage.setItem("ght_risk_saved","1");           // Stepper: Schritt 2 erledigt
    show(smsgEl,"","ok");
    toast("Settings saved ✓","ok");
    load();
  }catch(e){show(smsgEl,e.message,"err");toast("Settings nicht gespeichert: "+e.message,"err")}
}
async function approveBuilder(){
  try{
    const r=await api("POST","/api/builder-approved");
    show(bmsg,`Thank you — referral confirmed on-chain (${r.approved_bps} bps) ✓`,"ok");
    toast("Builder referral confirmed ✓","ok");
    load();verifyBuilder();
  }catch(e){show(bmsg,e.message,"err");toast(e.message,"err")}
}

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
        ? `<span class="pill off" title="Block aktiv: Win-Rate unter Schwelle">blocked</span>`
        : (c.trades<(s.min_trades_required||10)
            ? `<span class="pill mut" title="Noch unter Trade-Schwelle — Filter inaktiv">sampling</span>`
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
    setBtn("Connecting MetaMask…");
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
    setBtn("Waiting for MetaMask account…");
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
    setBtn("Switching network to Arbitrum Sepolia…");
    const HL_CHAIN_ID_HEX="0x66eee"; // = 421614
    try{
      await window.ethereum.request({
        method:"wallet_switchEthereumChain",
        params:[{chainId:HL_CHAIN_ID_HEX}],
      });
    }catch(switchErr){
      if(switchErr&&switchErr.code===4902){
        // Chain noch nicht im Wallet — adden (MetaMask switched dann auto)
        setBtn("Adding Arbitrum Sepolia to MetaMask…");
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

    setBtn("Sign in MetaMask…");
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

    setBtn("Submitting to Hyperliquid…");
    const action={
      type:"approveBuilderFee",
      hyperliquidChain:hyperliquidChain,
      signatureChainId:"0x66eee",
      maxFeeRate:maxFeeRate,
      builder:builderAddr,
      nonce:nonce,
    };
    const res=await api("POST","/api/builder-approval-submit",{action,signature:{r,s,v},nonce});

    // 2026-06-12 #32 (M-12): der Server setzt builder_approved NUR noch nach
    // erfolgreicher On-Chain-Verifikation. HTTP 200 + ok:false/pending:true
    // heißt: HL hat die Submission angenommen, aber die Verifikation steht
    // noch aus (Info-API down oder Cache-Propagation) — Flag NICHT gesetzt,
    // User soll den Confirm-Schritt gleich nochmal klicken. Vorher zeigte
    // das UI hier fälschlich "bestätigt ✓".
    if(res&&res.ok){
      show(bmsg,`Builder-Approval on-chain bestätigt (${res.approved_bps||"?"} bps) ✓`,"ok");
      toast("Builder approval on-chain bestätigt ✓","ok");
    }else{
      const pendMsg=(res&&res.detail)||"Approval submitted — on-chain verification pending. Click \"Re-verify\" in a moment.";
      show(bmsg,pendMsg,"err");
      toast("Builder approval pending — bitte gleich erneut bestätigen.","err");
    }
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
async function toggleBot(){
  // 2026-06-12: alert() → Toast (Proposal "Toast system").
  try{
    const me=await api("GET","/api/me");
    const r=await api("PUT","/api/settings",{bot_active:!me.bot_active});
    toast(r&&r.bot_active?"Bot enabled — signals will execute on your account.":"Bot disabled.","ok");
    load();
  }catch(e){toast(e.message,"err")}
}

let STATS=null, ACCOUNT=null, SETTINGS=null, TF=30;
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

// ── Chart 2.0 (2026-06-12): Farbe nach Vorzeichen (eine VERLUST-Kurve war
// vorher hartkodiert grün — irreführend), 3 Gridlines mit $-Labels,
// erste/letzte Datums-Labels, pointermove-Crosshair mit Tooltip. Marching-
// Ants- und Endlos-Glow-Animationen entfernt.
let CHART={pts:[],total:0,geo:null};
function drawChart(pts,total){
  CHART.pts=pts||[]; CHART.total=total||0;
  const svg=document.getElementById("chart"), empty=document.getElementById("chartempty");
  if(!svg)return;
  if(!pts||pts.length<2){svg.innerHTML="";svg.style.display="none";if(empty)empty.style.display="block";CHART.geo=null;return;}
  svg.style.display="block"; if(empty)empty.style.display="none";
  // viewBox an die echte Pixelbreite koppeln, damit Text nicht verzerrt
  // (preserveAspectRatio="none" hätte Labels horizontal gestreckt).
  const W=Math.max(320,Math.round(svg.clientWidth||600)), H=190;
  svg.setAttribute("viewBox","0 0 "+W+" "+H);
  const padL=10,padR=64,padT=10,padB=22;
  const xs=pts.map(p=>p.t), ys=pts.map(p=>p.cum);
  const minX=Math.min.apply(null,xs),maxX=Math.max.apply(null,xs);
  let minY=Math.min.apply(null,ys.concat([0])),maxY=Math.max.apply(null,ys.concat([0]));
  if(minY===maxY){maxY=minY+1;minY=minY-1;}
  const sx=t=>padL+(W-padL-padR)*((t-minX)/((maxX-minX)||1));
  const sy=v=>padT+(H-padT-padB)*(1-((v-minY)/((maxY-minY)||1)));
  CHART.geo={W,H,sx,sy};
  const col=(total>=0)?"#10b981":"#ff7d7d";
  const line=pts.map((p,i)=>(i?"L":"M")+sx(p.t).toFixed(1)+" "+sy(p.cum).toFixed(1)).join(" ");
  const area=line+" L "+sx(maxX).toFixed(1)+" "+(H-padB).toFixed(1)+" L "+sx(minX).toFixed(1)+" "+(H-padB).toFixed(1)+" Z";
  // 3 horizontale Gridlines + rechtsbündige $-Labels
  let grid="";
  for(const k of [0.25,0.5,0.75]){
    const v=minY+(maxY-minY)*k, y=sy(v).toFixed(1);
    grid+='<line x1="'+padL+'" y1="'+y+'" x2="'+(W-padR)+'" y2="'+y+'" stroke="#1c2320" stroke-width="1"/>'+
          '<text x="'+(W-6)+'" y="'+(+y+3.5)+'" text-anchor="end" fill="#94a8a0" font-size="10" font-family="ui-monospace,Menlo,monospace">'+esc(fmtUsd(v))+'</text>';
  }
  let zero="";
  if(minY<0&&maxY>0){
    const zy=sy(0).toFixed(1);
    zero='<line x1="'+padL+'" y1="'+zy+'" x2="'+(W-padR)+'" y2="'+zy+'" stroke="#2a3530" stroke-width="1" stroke-dasharray="4 4"/>';
  }
  const dates='<text x="'+padL+'" y="'+(H-6)+'" fill="#94a8a0" font-size="10">'+esc(new Date(minX).toLocaleDateString())+'</text>'+
              '<text x="'+(W-padR)+'" y="'+(H-6)+'" text-anchor="end" fill="#94a8a0" font-size="10">'+esc(new Date(maxX).toLocaleDateString())+'</text>';
  const xhair='<line id="cxline" y1="'+padT+'" y2="'+(H-padB)+'" x1="0" x2="0" stroke="#94a8a0" stroke-width="1" stroke-dasharray="3 3" style="display:none"/>'+
              '<circle id="cxdot" r="3.5" fill="'+col+'" stroke="#050607" stroke-width="1.5" style="display:none"/>'+
              '<g id="cxtip" style="display:none"><rect id="cxrect" rx="6" fill="#0e1110" stroke="#1c2320"/>'+
              '<text id="cxdate" fill="#94a8a0" font-size="10"></text>'+
              '<text id="cxval" fill="#eef5f1" font-size="11" font-weight="700" font-family="ui-monospace,Menlo,monospace"></text></g>';
  svg.innerHTML='<defs><linearGradient id="gpnl" x1="0" y1="0" x2="0" y2="1">'+
    '<stop offset="0" stop-color="'+col+'" stop-opacity="0.28"/>'+
    '<stop offset="1" stop-color="'+col+'" stop-opacity="0"/></linearGradient></defs>'+
    grid+zero+
    '<path d="'+area+'" fill="url(#gpnl)"/>'+
    '<path d="'+line+'" fill="none" stroke="'+col+'" stroke-width="2" stroke-linejoin="round"/>'+
    dates+xhair;
}
function chartMove(e){
  const svg=document.getElementById("chart"), g=CHART.geo;
  if(!svg||!g||CHART.pts.length<2)return;
  const rect=svg.getBoundingClientRect();
  if(!rect.width)return;
  const x=(e.clientX-rect.left)/rect.width*g.W;
  let best=null,bd=Infinity;
  for(const p of CHART.pts){const d=Math.abs(g.sx(p.t)-x);if(d<bd){bd=d;best=p;}}
  if(!best)return;
  const px=g.sx(best.t), py=g.sy(best.cum);
  const lineEl=document.getElementById("cxline"), dot=document.getElementById("cxdot"),
        tip=document.getElementById("cxtip"), tr=document.getElementById("cxrect"),
        td=document.getElementById("cxdate"), tv=document.getElementById("cxval");
  if(!lineEl||!dot||!tip)return;
  lineEl.setAttribute("x1",px);lineEl.setAttribute("x2",px);lineEl.style.display="";
  dot.setAttribute("cx",px);dot.setAttribute("cy",py);dot.style.display="";
  const dateS=new Date(best.t).toLocaleDateString();
  const valS=fmtUsdSigned(best.cum);
  td.textContent=dateS; tv.textContent=valS;
  const tw=Math.max(dateS.length,valS.length)*6.8+16;
  let tx=px+12; if(tx+tw>g.W-4)tx=px-12-tw;
  const ty=Math.max(4,Math.min(py-20,g.H-58));
  tr.setAttribute("x",tx);tr.setAttribute("y",ty);tr.setAttribute("width",tw);tr.setAttribute("height",36);
  td.setAttribute("x",tx+8);td.setAttribute("y",ty+14);
  tv.setAttribute("x",tx+8);tv.setAttribute("y",ty+29);
  tip.style.display="";
}
function chartLeave(){
  ["cxline","cxdot","cxtip"].forEach(function(id){const el=document.getElementById(id);if(el)el.style.display="none";});
}
// Re-Layout des Charts bei Resize (viewBox hängt an clientWidth)
let _rszT=null;
window.addEventListener("resize",function(){
  clearTimeout(_rszT);
  _rszT=setTimeout(function(){if(CHART.pts.length>1)drawChart(CHART.pts,CHART.total)},150);
});

function renderStats(){
  if(!STATS) return;
  const hasAcct=ACCOUNT&&ACCOUNT.balance!=null;
  const accountValue=ACCOUNT&&ACCOUNT.account_value!=null?ACCOUNT.account_value:(ACCOUNT&&ACCOUNT.balance!=null?ACCOUNT.balance:null);
  const unrealized=ACCOUNT&&ACCOUNT.unrealized_pnl!=null?ACCOUNT.unrealized_pnl:null;
  const exposure=ACCOUNT&&ACCOUNT.open_exposure!=null?ACCOUNT.open_exposure:null;
  const openCount=ACCOUNT&&ACCOUNT.open_positions!=null?ACCOUNT.open_positions:(STATS.active_trades||0);
  setStat("statwin",(STATS.win_rate||0)+"%",STATS.win_rate||0);
  const st=document.getElementById("stattrades"); if(st)st.textContent=(STATS.closed_trades||0)+" closed trades";
  // "—" statt $0.00, wenn (noch) keine Wallet verbunden ist — ehrlicher.
  setStat("statAccount",accountValue!=null?fmtUsd(accountValue):"—",accountValue);
  setStat("statUnrealized",unrealized!=null?fmtUsdSigned(unrealized):"—",unrealized);
  setStatCls("statUnrealized",unrealized==null?"":(unrealized>=0?"pos":"neg"));
  setStat("statExposure",exposure!=null?fmtUsd(exposure):"—",exposure);
  setStat("statOpen",String(openCount),openCount);
  // Risk/Trade aus den User-Settings (Fraction → Prozent)
  if(SETTINGS&&SETTINGS.risk_pct!=null)setStat("statRisk",trimNum(SETTINGS.risk_pct*100)+"%",SETTINGS.risk_pct*100);
  const w=seriesFor(TF);
  setStat("statpnl",fmtUsdSigned(w.total),w.total);
  setStatCls("statpnl",w.total>=0?"pos":"neg");
  drawChart(w.pts,w.total);
  // Mobile Sticky-Bar
  setStat("mbarVal",accountValue!=null?fmtUsd(accountValue):"—",accountValue);
  setStat("mbarUpnl",unrealized!=null?fmtUsdSigned(unrealized):"—",unrealized);
  setStatCls("mbarUpnl",unrealized==null?"":(unrealized>=0?"pos":"neg"));
  const hb=document.querySelector("#histtbl tbody");hb.innerHTML="";
  (STATS.recent||[]).forEach(function(r){
    const cls=/long/i.test(r.dir||"")?"on":(/short/i.test(r.dir||"")?"off":"");
    const dir='<span class="pill '+cls+'">'+esc((r.dir||"—").toUpperCase())+'</span>';
    const pnl=Number(r.pnl)||0;
    const pc=pnl>0?"pos":(pnl<0?"neg":"mut");
    const date=esc(new Date(r.t).toLocaleString());
    hb.insertAdjacentHTML("beforeend","<tr><td class=\"mut\">"+date+"</td><td><b>"+esc(r.coin||"")+"</b></td><td>"+dir+"</td><td>$"+esc(fmtPx(r.px))+"</td><td class=\""+pc+"\">"+(pnl>0?"+":"")+esc(FMT_USD.format(pnl))+"</td></tr>");
  });
  document.getElementById("histempty").style.display=(STATS.recent||[]).length?"none":"block";
}
document.querySelectorAll(".tfbtn").forEach(function(b){b.onclick=function(){
  TF=+b.dataset.d;
  document.querySelectorAll(".tfbtn").forEach(function(x){x.classList.remove("on")});
  b.classList.add("on"); renderStats();
}});

// ── Position-Cards 2.0 (2026-06-12): SL/TP/Leverage/Liq sind für ein
// Leverage-Copy-Produkt DIE Vertrauensfrage. Felder, die der Server (noch)
// nicht liefert, degraden still; stop_loss===null (Key vorhanden, kein SL)
// zeigt einen roten "No SL"-Badge — das ist kritische Information.
let PREV_UPNL={};
function posCardHtml(p){
  const coin=String(p.coin||"");
  const size=Number(p.size)||0;
  const side=size>=0?"Long":"Short";
  const up=Number(p.uPnl)||0;
  const entry=Number(p.entry)||0;
  const mark=p.mark_px!=null?Number(p.mark_px):null;
  const hasSlKey=Object.prototype.hasOwnProperty.call(p,"stop_loss");
  const sl=(hasSlKey&&p.stop_loss!=null)?Number(p.stop_loss):null;
  const tps=Array.isArray(p.take_profits)?p.take_profits.filter(t=>t&&t.price!=null):[];
  const lev=p.leverage!=null?Number(p.leverage):null;
  const liq=p.liquidation_px!=null?Number(p.liquidation_px):null;
  const margin=p.margin_used!=null?Number(p.margin_used):null;
  const notional=Math.abs(size*entry);
  const ref=mark!=null?mark:entry;
  let near=null;
  if(tps.length)near=tps.reduce((a,b)=>Math.abs(Number(b.price)-ref)<Math.abs(Number(a.price)-ref)?b:a);
  const noSl=hasSlKey&&p.stop_loss==null;
  // Amber-Warnung, wenn Mark näher als 1 % am SL ist
  const slWarn=sl!=null&&mark!=null&&mark>0&&Math.abs(mark-sl)/mark<=0.01;
  const upPct=(margin!=null&&margin>0)?((up>=0?"+":"−")+Math.abs(up/margin*100).toFixed(1)+"% of margin"):"";
  let slTpRow="";
  if(hasSlKey||tps.length){
    const tpTitle=esc(tps.map(t=>(t.percent!=null?t.percent+"% @ ":"")+"$"+fmtPx(t.price)).join(", "));
    slTpRow=
      '<div class="metric-box'+(slWarn?' warn-sl':'')+'"'+(slWarn?' title="Mark ist < 1 % vom Stop Loss entfernt"':'')+'><div class="label">Stop Loss</div><div class="value">'+
        (sl!=null?("$"+esc(fmtPx(sl))):'<span class="badge-nosl">No SL</span>')+'</div></div>'+
      '<div class="metric-box" title="'+tpTitle+'"><div class="label">Take Profit</div><div class="value">'+
        (near?("$"+esc(fmtPx(near.price))+(tps.length>1?' <span class="mut">+'+(tps.length-1)+'</span>':"")):"—")+'</div></div>';
  }
  let bar="";
  if(sl!=null&&near&&Number(near.price)!==sl){
    let pct=(ref-sl)/(Number(near.price)-sl);
    pct=Math.max(0,Math.min(1,pct));
    bar='<div class="sl-bar" title="Position des Mark-Preises zwischen SL und nächstem TP"><div class="sl-fill" style="width:'+(pct*100).toFixed(1)+'%"></div></div>'+
        '<div class="sl-bar-l"><span>SL '+esc(fmtPx(sl))+'</span><span>TP '+esc(fmtPx(near.price))+'</span></div>';
  }
  return '<article class="position-card '+(up>=0?"heat-positive":"heat-negative")+'" data-coin="'+esc(coin)+'">'+
    '<div class="position-top">'+
      '<div class="position-coin">'+esc(coin)+
        ' <span class="pill '+(size>=0?"on":"off")+'">'+side+(lev?" "+esc(String(lev))+"x":"")+'</span>'+
        (noSl?' <span class="badge-nosl" title="Diese Position hat KEINEN Stop Loss!">No SL</span>':"")+
      '</div>'+
      '<div class="pos-upnl '+(up>=0?"pos":"neg")+'" data-coin-upnl="'+esc(coin)+'">'+esc(fmtUsdSigned(up))+
        (upPct?'<small>'+esc(upPct)+'</small>':"")+'</div>'+
    '</div>'+
    '<div class="position-metrics">'+
      '<div class="metric-box"><div class="label">Entry</div><div class="value">$'+esc(fmtPx(entry))+'</div></div>'+
      '<div class="metric-box"><div class="label">'+(mark!=null?"Mark":"Notional")+'</div><div class="value">$'+esc(mark!=null?fmtPx(mark):FMT_USD.format(notional))+'</div></div>'+
      slTpRow+
      '<div class="metric-box"><div class="label">Size</div><div class="value">'+esc(fmtSize(size))+" "+esc(coin)+'</div></div>'+
      '<div class="metric-box"><div class="label">'+(liq!=null?"Liq. Price":(margin!=null?"Margin":"Notional"))+'</div><div class="value">$'+
        esc(liq!=null?fmtPx(liq):FMT_USD.format(margin!=null?margin:notional))+'</div></div>'+
    '</div>'+bar+
  '</article>';
}
function renderPositions(list){
  const cards=document.getElementById("positionCards"); if(!cards)return;
  cards.innerHTML=list.map(posCardHtml).join("");
  const pe=document.getElementById("posempty"); if(pe)pe.style.display=list.length?"none":"block";
  // Flash bei uPnL-Änderung pro Coin
  const seen={};
  list.forEach(function(p){
    const c=String(p.coin||""); const up=Number(p.uPnl)||0; seen[c]=true;
    const el=cards.querySelector('[data-coin-upnl="'+(window.CSS&&CSS.escape?CSS.escape(c):c)+'"]');
    if(el&&PREV_UPNL[c]!==undefined&&PREV_UPNL[c]!==up){
      el.classList.add(up>PREV_UPNL[c]?"flash-up":"flash-down");
    }
    PREV_UPNL[c]=up;
  });
  for(const k of Object.keys(PREV_UPNL))if(!seen[k])delete PREV_UPNL[k];
}

// ── Onboarding-Stepper: Connect wallet → Set risk → Enable bot.
// Schritt 2 gilt als erledigt, sobald der User einmal Settings gespeichert
// hat (localStorage-Flag) oder der Bot bereits läuft — die API hat kein
// "settings wurden je angefasst"-Feld.
function setStep(id,done,current){
  const el=document.getElementById(id); if(!el)return;
  el.classList.toggle("done",!!done);
  el.classList.toggle("current",!!current);
  const n=el.querySelector(".n"); if(n)n.textContent=done?"✓":(n.dataset.i||"");
}
function renderStepper(u){
  const s1=!!u.wallet_connected;
  const s3=!!u.bot_active;
  const s2=s3||localStorage.getItem("ght_risk_saved")==="1";
  setStep("step1",s1,!s1);
  setStep("step2",s2,s1&&!s2);
  setStep("step3",s3,s1&&s2&&!s3);
  const sp=document.getElementById("stepper"); if(sp)sp.classList.remove("hide");
}

// ── load(): holt /api/dashboard und rendert. Fehlerverhalten 2026-06-12:
//  - 401 → Login-Card zeigen, NUR DANN Legacy-Token löschen.
//  - andere Fehler → rotes Banner, letzte Daten bleiben sichtbar, das
//    15s-Polling übernimmt den Retry. Nie mehr stiller Blank-Screen.
//  - Render-Fehler werden separat gefangen und sichtbar gemacht (der alte
//    Catch-All hat schon einmal einen Render-Crash wochenlang versteckt).
let LOGGED_IN=false, LAST_OK=0, FIRST_RENDER=false, LOADERR_TOASTED=false, PC_LAST=0;
function showLoadErr(){
  const b=document.getElementById("loaderr"); if(b)b.classList.remove("hide");
  if(!LOADERR_TOASTED){toast("Could not load dashboard — retrying…","err");LOADERR_TOASTED=true;}
}
function hideLoadErr(){
  const b=document.getElementById("loaderr"); if(b)b.classList.add("hide");
  LOADERR_TOASTED=false;
}
async function load(){
  let d;
  try{
    d=await api("GET","/api/dashboard");
  }catch(e){
    if(e.status===401){
      localStorage.removeItem("ght");   // Legacy-Token ist wirklich tot
      LOGGED_IN=false;
      // 2026-06-13 Review-Fix: Polling stoppen — sonst hämmert der Interval
      // nach Session-Ablauf alle 15s weiter gegen 401. startPolling() re-armt
      // nach dem nächsten erfolgreichen Login/load().
      if(POLL){clearInterval(POLL);POLL=null;}
      document.getElementById("app").classList.add("hide");
      document.getElementById("auth").classList.remove("hide");
      hideLoadErr();
    }else{
      showLoadErr();                    // non-blocking, Daten bleiben stehen
    }
    return;
  }
  hideLoadErr();
  LOGGED_IN=true; LAST_OK=Date.now();
  startPolling();
  const chip=document.getElementById("updatedChip"); if(chip)chip.classList.remove("hide");
  try{
    render(d);
    FIRST_RENDER=true;
  }catch(re){
    console.error("dashboard render failed:",re);
    toast("Render error: "+(re&&re.message||re),"err");
  }
}
function render(d){
  auth.classList.add("hide");app.classList.remove("hide");
  // 2026-06-08: Mainnet-aware UI — Pille + Banner-Style switch. Banner-text
  // selbst ist immer der Risk-Warning (siehe HTML); auf Mainnet zusätzlich
  // roter Style + 'real money' emphasis.
  const isMain=(d.net==="mainnet");
  const banner=document.getElementById("netbanner");
  const banTitle=document.getElementById("netbanner-title");
  if(banner){
    if(isMain){
      banner.style.borderColor="#ff5555";
      banner.style.background="#1a0707";
      banner.style.color="#ffd2d2";
      if(banTitle)banTitle.textContent="MAINNET — REAL MONEY at risk. Read carefully before trading.";
    }else{
      banner.style.borderColor="";
      banner.style.background="";
      banner.style.color="";
      if(banTitle)banTitle.textContent="High-Risk Trading — read before using";
    }
  }
  const hlUrl=document.getElementById("hl-url");
  if(hlUrl){
    hlUrl.href=isMain?"https://app.hyperliquid.xyz":"https://app.hyperliquid-testnet.xyz";
    hlUrl.textContent=isMain?"app.hyperliquid.xyz":"app.hyperliquid-testnet.xyz";
  }
  const foot=document.getElementById("foot-net");
  if(foot){
    foot.textContent=isMain
      ?"GoatHub Trading Bot · MAINNET — real money"
      :"GoatHub Trading Bot · Testnet — no real money";
  }
  setHero("heroStatus",d.user.bot_active?"Trading enabled":"Standby");
  setHero("heroNet",isMain?"Mainnet":"Testnet");
  setHero("heroWallet",d.user.wallet_connected?"Connected":"Connect wallet");
  const ld=document.getElementById("liveDot"); if(ld)ld.classList.toggle("active",!!d.user.bot_active);
  const nb=document.getElementById("netbadge");
  if(nb){nb.textContent=isMain?"MAINNET":"TESTNET";nb.className="pill "+(isMain?"off":"on");}
  const db=document.getElementById("demobadge"); if(db)db.classList.toggle("hide",d.demo!==true);
  // Show Discord username + avatar if available, else email
  const displayName=d.user.discord_username||d.user.email;
  document.getElementById("uname").textContent=displayName;
  const av=document.getElementById("uavatar");
  if(d.user.discord_avatar_url){av.src=d.user.discord_avatar_url;av.classList.remove("hide")}
  // 2026-06-12 Finding "duplicate hint": fixes #ubalhint-Element statt
  // ubal.after(createElement) — das stapelte bei jedem load() einen
  // weiteren identischen Hinweis unter die Balance.
  const hint=document.getElementById("ubalhint");
  if(d.account.balance==null){
    setStat("ubal","—",null);
    if(hint){hint.textContent="Connect wallet to see balance";hint.style.display="block";}
  }else{
    setStat("ubal",FMT_USD.format(Number(d.account.balance))+" USDC",Number(d.account.balance));
    if(hint)hint.style.display="none";
  }
  const on=d.user.bot_active;
  botpill.textContent=on?"on":"off"; botpill.className="pill "+(on?"on":"off");
  const mb=document.getElementById("mbarBot"); if(mb){mb.textContent=on?"bot on":"bot off";mb.className="pill "+(on?"on":"off");}
  document.getElementById("toggle").textContent=on?"Disable Bot":"Enable Bot";
  // Phase 3 (2026-06-02): admin link nur für is_admin user zeigen.
  const al=document.getElementById("adminlink"); if(al){if(d.user.is_admin)al.classList.remove("hide");else al.classList.add("hide")}
  // Wallet-Setup-Card: Status im summary + Auto-Collapse wenn erledigt
  const wa=d.user.hl_account_address||"";
  const waShort=wa?(wa.slice(0,6)+"…"+wa.slice(-4)):"";
  wstat.textContent=d.user.wallet_connected?("connected ✓ "+waShort):"not connected";
  wstat.style.color=d.user.wallet_connected?"#3fe0a0":"";
  // Settings NUR beim ersten Render in die Felder schreiben — das 15s-Polling
  // darf User-Eingaben nicht überschreiben. Nach dem Save befüllt
  // saveSettings() die Form aus der PUT-Response neu.
  if(!FIRST_RENDER){
    applySettings(d.user.settings);
  }else{
    SETTINGS=d.user.settings;
  }
  const rs=document.getElementById("riskstate");
  if(rs&&d.user.settings){
    const s=d.user.settings;
    rs.textContent=trimNum((s.risk_pct||0)*100)+"% / trade · "+s.leverage+"x cap · "+
      (s.capital_cap_usdc>0?("$"+FMT_INT.format(s.capital_cap_usdc)+" cap"):"full account");
  }
  // Setup-Cards initial auf-/zuklappen (nur einmal — danach gehört der
  // Zustand dem User, Polling klappt nichts mehr um).
  if(!FIRST_RENDER){
    const wc=document.getElementById("wallet-connect");
    if(wc)wc.open=!d.user.wallet_connected;
    const rc=document.getElementById("risk-setup");
    if(rc)rc.open=!(d.user.bot_active||localStorage.getItem("ght_risk_saved")==="1");
  }
  renderStepper(d.user);
  // Onboarded → Daten-Sektionen über die Setup-Karten (Flex-Order im CSS)
  app.classList.toggle("onboarded",!!(d.user.wallet_connected&&d.user.bot_active));
  // Builder-Card: komplett aus, solange keine BUILDER_ADDRESS konfiguriert
  // ist (statt disabled-Buttons mit "disabled (Testnet)" — verwirrte Tester).
  const builderOff=!(d.builder&&d.builder.address);
  const bc=document.getElementById("builder");
  if(bc)bc.classList.toggle("hide",builderOff);
  if(!builderOff){
    baddr.textContent=d.builder.address; bfee.textContent=d.builder.fee||"—";
    const ba=d.user.builder_approved;
    bstat.textContent=ba?"confirmed":"not confirmed"; bstat.className="pill "+(ba?"on":"off");
    // Phase 6 (2026-06-02): Button-Text je nach Status.
    const bbtn=document.getElementById("bbtn"); if(bbtn){bbtn.textContent=ba?"Re-verify on-chain":"Builder fee approved — confirm";}
  }
  renderPositions(d.account.positions||[]);
  // 2026-06-04: Per-Coin Filter Status (Restposten #4). 2026-06-12: max. alle
  // 5 min — der Endpoint macht teure HL-Calls (16 Coins) und die Daten ändern
  // sich langsam; das 15s-Dashboard-Polling soll ihn nicht mitreißen.
  if(Date.now()-PC_LAST>300000){PC_LAST=Date.now();loadPerCoinStatus();}
  const ab=document.querySelector("#acttbl tbody");ab.innerHTML="";
  d.activity.forEach(a=>{ab.insertAdjacentHTML("beforeend",`<tr><td class="mut">${esc((a.ts||"").replace("T"," "))}</td><td>${esc(a.kind)}</td><td>${esc(a.text)}</td></tr>`)});
  actempty.style.display=d.activity.length?"none":"block";
  ACCOUNT=d.account||null; STATS=d.stats||null; renderStats();
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
// 2026-06-12: Öffentlicher Status-Badge — läuft VOR/ohne Login.
// Race-Fix (Finding 45): publicStatus überschreibt NIE den Hero-State eines
// eingeloggten Users — wenn load() schon gerendert hat (app sichtbar), wird
// das Health-Resultat verworfen.
// LOW-10: /api/health liefert nur noch {status:"ok"} — listener-Status und
// testnet/mainnet leakten vorher an JEDEN unauthentifizierten Caller. Die
// Netz-Badges (netbadge/heroNet) bleiben deshalb neutral, bis render() sie
// nach Login aus /api/dashboard (d.net, auth-gated) setzt.
(async function publicStatus(){
  try{
    const h=await (await fetch("/api/health",{credentials:"include"})).json();
    if(LOGGED_IN||!document.getElementById("app").classList.contains("hide"))return;
    const ok=!!h&&(h.status==="ok"||h.ok===true);   // h.ok: Alt-Server während Deploy-Übergang
    setHero("heroStatus",ok?"Online":"Offline");
    setHero("heroWallet","—");
    // 2026-06-13 Review-Fix: heroNet bleibt pre-login bewusst neutral (LOW-10),
    // aber der Skeleton-Shimmer muss trotzdem aufgelöst werden — sonst
    // schimmert das Feld für ausgeloggte Besucher endlos.
    setHero("heroNet","—");
  }catch(e){
    if(LOGGED_IN||!document.getElementById("app").classList.contains("hide"))return;
    setHero("heroStatus","Offline");
    setHero("heroNet","—");
  }
})();

// ── Live-Polling (2026-06-12): alle 15s, aber nur wenn der Tab sichtbar ist;
// bei visibilitychange→visible sofort refreshen. 15s × 4/min liegt sicher
// unter dem 30/min-Rate-Limit von /api/dashboard (server-cached 10s).
let POLL=null;
function startPolling(){
  if(POLL)return;
  POLL=setInterval(function(){
    if(document.visibilityState==="visible")load();
  },15000);
}
document.addEventListener("visibilitychange",function(){
  if(document.visibilityState==="visible"&&LOGGED_IN)load();
});
// "Updated Xs ago"-Chip tickt sekündlich
setInterval(function(){
  const el=document.getElementById("updatedAgo"); if(!el)return;
  if(!LAST_OK){el.textContent="—";return;}
  el.textContent="Updated "+Math.max(0,Math.round((Date.now()-LAST_OK)/1000))+"s ago";
},1000);

// Phase 2 #18: immer load() probieren — auth läuft via Cookie. Wenn 401,
// zeigt load() die Login-Form (auth section ist by default sichtbar).
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
