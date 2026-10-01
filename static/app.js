const $ = id => document.getElementById(id);
let count = 3, total = 3, qNum = 1, timerSec = 0, timerInt = null, pendingNext = null, pendingDone = false;

function show(name){["setupScreen","interviewScreen","reportScreen"].forEach(s=>$(s).classList.toggle("hidden",s!==name));}
function fmt(s){return String(Math.floor(s/60)).padStart(2,"0")+":"+String(s%60).padStart(2,"0");}
function startTimer(){stopTimer();timerSec=0;$("timer").textContent="00:00";timerInt=setInterval(()=>{timerSec++;$("timer").textContent=fmt(timerSec);},1000);}
function stopTimer(){if(timerInt)clearInterval(timerInt);timerInt=null;}
function friendly(e){return (e && e.error) || "Request failed. Check server / Groq status.";}
function setBusy(btn,busy,idle){if(!btn)return;btn.disabled=!!busy;if(busy){btn.dataset.idle=btn.textContent;btn.textContent=idle||"Working…";}else if(btn.dataset.idle){btn.textContent=btn.dataset.idle;delete btn.dataset.idle;}}

async function refreshStatus(){
  try{
    const r = await fetch("/api/status"); const j = await r.json();
    const p = $("statusPill");
    if(j.key_configured){p.className="pill on";p.textContent="● groq ready ("+j.key_source+")";}
    else{p.className="pill off";p.textContent="● no key — open Settings";}
  }catch{const p=$("statusPill");p.className="pill off";p.textContent="● server offline?";}
}
async function refreshKeyState(){
  try{const r=await fetch("/api/key/status");const j=await r.json();
    $("keyState").textContent=j.configured?("Key configured via "+j.source+"."):"No key configured (falls back to .env GROQ_API_KEY).";
  }catch{$("keyState").textContent="Status unreachable.";}
}

// setup
document.querySelectorAll("#countSeg button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("#countSeg button").forEach(x=>x.classList.remove("on"));
  b.classList.add("on"); count=parseInt(b.dataset.n,10);
});
$("answer").addEventListener("input",()=>{
  const n=$("answer").value.length;
  $("charCount").textContent=n+" / 2000";
  $("charCount").style.color=n>2000?"#e30613":"";
});
$("answer").addEventListener("keydown",(e)=>{
  if((e.ctrlKey||e.metaKey)&&e.key==="Enter"){e.preventDefault();$("submitBtn").click();}
});
$("startBtn").onclick=async()=>{
  $("setupErr").textContent="";setBusy($("startBtn"),true,"Starting…");
  try{
    const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({topic:$("topic").value,difficulty:$("difficulty").value,count})});
    const j=await r.json();
    if(!r.ok||!j.ok)throw j;
    total=j.total;qNum=1;$("diffBadge").textContent=j.difficulty;$("topicBadge").textContent=j.topic;
    setQuestion(j.current);show("interviewScreen");
  }catch(e){$("setupErr").textContent=friendly(e);}
  setBusy($("startBtn"),false);
};
function setQuestion(q){
  qNum=q.n;$("question").textContent=q.question;
  $("progress").textContent=`Q${q.n}/${total}`;
  $("barFill").style.width=((q.n-1)/total*100)+"%";
  $("fuBadge").classList.toggle("hidden",!q.is_followup);
  $("evalCard").classList.add("hidden");$("nextBtn").classList.add("hidden");$("reportBtn").classList.add("hidden");
  $("answer").value="";$("charCount").textContent="0 / 2000";$("charCount").style.color="";$("answerErr").textContent="";
  startTimer();$("answer").focus();
}
// answer
$("submitBtn").onclick=async()=>{
  const raw=$("answer").value||"";
  if(!raw.trim()){$("answerErr").textContent="Answer is empty. Type something (max 2000 chars).";$("answer").focus();return;}
  if(raw.trim().length>2000){$("answerErr").textContent=`Answer too long (${raw.trim().length} chars). Max 2000.`;return;}
  $("answerErr").textContent="";setBusy($("submitBtn"),true,"Evaluating…");
  try{
    const r=await fetch("/api/answer",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({answer:raw})});
    const j=await r.json();
    if(!r.ok||!j.ok)throw j;
    stopTimer();showEval(j.evaluation);
    $("barFill").style.width=(j.done?100:(j.next.n-1)/total*100)+"%";
    pendingNext=j.next||null;pendingDone=!!j.done;
    $("nextBtn").classList.toggle("hidden",!(!j.done&&j.next));
    $("reportBtn").classList.toggle("hidden",!j.done);
  }catch(e){$("answerErr").textContent=friendly(e);}
  setBusy($("submitBtn"),false);
};
function showEval(ev){
  $("evalCard").classList.remove("hidden");
  const ring=$("scoreRing");ring.textContent=ev.score;
  ring.style.borderColor=ev.score>=70?"#0d7a3f":ev.score>=45?"#9a6a00":"#e30613";
  $("feedback").textContent=ev.feedback||"";
  $("strengths").innerHTML=(ev.strengths||[]).map(s=>`<li>${escapeHtml(s)}</li>`).join("")||"<li>—</li>";
  $("missing").innerHTML=(ev.missing||[]).map(s=>`<li>${escapeHtml(s)}</li>`).join("")||"<li>—</li>";
  $("evalCard").scrollIntoView({behavior:"smooth",block:"nearest"});
}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
$("nextBtn").onclick=()=>{if(pendingNext)setQuestion(pendingNext);};
$("reportBtn").onclick=()=>fetchReport("/api/report");
$("quitBtn").onclick=()=>{if(confirm("Quit now? A partial report will be generated."))fetchReport("/api/quit");};
async function fetchReport(url){
  $("answerErr").textContent="Loading report…";
  try{
    const r=await fetch(url,{method:"POST"});const j=await r.json();
    if(!r.ok||!j.ok)throw j;
    if(!j.report){alert(j.message||"Quit with no report.");location.reload();return;}
    renderReport(j);show("reportScreen");stopTimer();window.scrollTo(0,0);
  }catch(e){$("answerErr").textContent=friendly(e);}
}
function renderReport(j){
  const rep=j.report;
  $("partialTag").classList.toggle("hidden",!j.partial);
  const v=$("verdict");v.textContent=rep.verdict;
  v.className="verdict "+(rep.verdict==="HIRE"?"hire":"nohire");
  $("overall").textContent=rep.overall;
  $("perQ").innerHTML=(rep.per_question||[]).map(p=>`<span>Q${Number(p.n)}: <b>${Number(p.score)}</b></span>`).join("");
  $("rConcepts").innerHTML=(rep.concepts_demonstrated||[]).map(s=>`<li>${escapeHtml(s)}</li>`).join("")||"<li>—</li>";
  $("rMissing").innerHTML=(rep.missing_concepts||[]).map(s=>`<li>${escapeHtml(s)}</li>`).join("")||"<li>—</li>";
  $("rStudy").innerHTML=(rep.study_topics||[]).map(s=>`<li>${escapeHtml(s)}</li>`).join("");
  $("rStrong").textContent=rep.stronger_answer_example||"";
}
$("copyBtn").onclick=async()=>{
  const t=`InterviewSim report — ${$("topicBadge").textContent} ${$("diffBadge").textContent}\nVerdict: ${$("verdict").textContent} | Overall: ${$("overall").textContent}/100\n${$("perQ").textContent}\nStronger answer:\n${$("rStrong").textContent}`;
  try{
    await navigator.clipboard.writeText(t);
    alert("Report copied.");
  }catch{
    try{
      const ta=document.createElement("textarea");
      ta.value=t;document.body.appendChild(ta);ta.select();
      document.execCommand("copy");ta.remove();
      alert("Report copied.");
    }catch{alert("Copy failed — select the report text manually.");}
  }
};
$("printBtn").onclick=()=>window.print();
$("restartBtn").onclick=()=>location.reload();
// settings
$("settingsBtn").onclick=()=>{$("settingsModal").classList.remove("hidden");$("keyMsg").textContent="";refreshKeyState();$("keyInput").focus();};
$("closeSettingsBtn").onclick=()=>$("settingsModal").classList.add("hidden");
$("settingsModal").addEventListener("click",(e)=>{if(e.target===$("settingsModal"))$("settingsModal").classList.add("hidden");});
document.addEventListener("keydown",(e)=>{if(e.key==="Escape")$("settingsModal").classList.add("hidden");});
$("saveKeyBtn").onclick=async()=>{
  const v=($("keyInput").value||"").trim();
  if(!v){$("keyMsg").textContent="Paste a key first.";return;}
  $("keyMsg").textContent="Verifying via models.list…";
  try{
    const r=await fetch("/api/key",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key:v})});
    const j=await r.json();if(!r.ok||!j.ok)throw j;
    $("keyMsg").textContent="Key verified & saved (session memory).";$("keyInput").value="";
    refreshStatus();refreshKeyState();
  }catch(e){$("keyMsg").textContent=friendly(e);}
};
$("removeKeyBtn").onclick=async()=>{
  await fetch("/api/key",{method:"DELETE"});$("keyMsg").textContent="Session key removed (.env fallback now).";
  refreshStatus();refreshKeyState();
};
refreshStatus();setInterval(refreshStatus,30000);
// voice input (browser SpeechRecognition only — no server, no key, no cost)
// Text stays primary: dictation appends into #answer; user edits + submits manually.
(function(){
  const micBtn=$("micBtn"), micStatus=$("micStatus"), ans=$("answer");
  if(!micBtn||!micStatus||!ans)return;
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  let rec=null, listening=false, baseText="", finalText="";
  function setStatus(t){micStatus.textContent=t||"";}
  function setBtn(){micBtn.textContent=listening?"STOP":"MIC";micBtn.classList.toggle("listening",listening);}
  setBtn();
  function updateCount(){ans.dispatchEvent(new Event("input"));}
  function combined(finalPart,interimPart){
    const add=(finalPart+" "+interimPart).trim();
    if(!add)return baseText;
    const sep=baseText&&!/\s$/.test(baseText)?" ":("");
    return (baseText+sep+add).slice(0,2000);
  }
  if(!SR){
    micBtn.onclick=()=>setStatus("Voice input not supported in this browser (e.g. Firefox) — please type your answer instead.");
    return;
  }
  micBtn.onclick=()=>{
    if(listening){try{rec&&rec.stop();}catch{}return;} // onend finalizes
    baseText=ans.value||"";finalText="";
    try{
      rec=new SR();
    }catch{setStatus("Could not start voice input — please type instead.");return;}
    rec.lang="en-US";rec.interimResults=true;rec.continuous=true;rec.maxAlternatives=1;
    listening=true;setBtn();setStatus("Listening… speak now. Tap STOP again to stop.");
    rec.onresult=e=>{
      let interim="";
      for(let i=e.resultIndex;i<e.results.length;i++){
        const r=e.results[i];
        if(r.isFinal)finalText=(finalText+" "+r[0].transcript).trim();
        else interim+=r[0].transcript;
      }
      ans.value=combined(finalText,interim);updateCount();
    };
    rec.onerror=e=>{
      const c=(e&&e.error)||"";
      if(c==="not-allowed"||c==="service-not-allowed")setStatus("Microphone denied — allow mic access in the browser, then retry (or type instead).");
      else if(c==="audio-capture")setStatus("No microphone found — check your device, or type instead.");
      else if(c==="no-speech")setStatus("No speech detected — try again, or type instead.");
      else if(c==="network")setStatus("Speech service unavailable (network) — type instead.");
      else if(c==="aborted")setStatus("Voice stopped — partial text kept; edit before submitting.");
      else setStatus("Voice error ("+(c||"unknown")+") — partial text kept; edit or type instead.");
    };
    rec.onend=()=>{
      listening=false;setBtn();
      const done=combined(finalText,"");
      if(finalText){ans.value=done;updateCount();setStatus("Voice added — review/edit, then Submit answer.");}
      else if(!/denied|No microphone|unavailable|detected|error|stopped/i.test(micStatus.textContent))
        setStatus("Empty result — nothing heard. Try again, or type instead.");
      try{ans.focus();}catch{}
    };
    try{rec.start();}catch{listening=false;setBtn();setStatus("Could not start voice input — please type instead.");}
  };
})();
