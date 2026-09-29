const $ = id => document.getElementById(id);
let count = 3, total = 3, qNum = 1, timerSec = 0, timerInt = null, pendingNext = null, pendingDone = false;

function show(name){["setupScreen","interviewScreen","reportScreen"].forEach(s=>$(s).classList.toggle("hidden",s!==name));}
function fmt(s){return String(Math.floor(s/60)).padStart(2,"0")+":"+String(s%60).padStart(2,"0");}
function startTimer(){stopTimer();timerSec=0;$("timer").textContent="00:00";timerInt=setInterval(()=>{timerSec++;$("timer").textContent=fmt(timerSec);},1000);}
function stopTimer(){if(timerInt)clearInterval(timerInt);timerInt=null;}
function friendly(e){return (e && e.error) || "Request failed. Check server / Groq status.";}

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
$("answer").addEventListener("input",()=>{$("charCount").textContent=$("answer").value.length+" / 2000";});
$("startBtn").onclick=async()=>{
  $("setupErr").textContent="";$("startBtn").disabled=true;$("startBtn").textContent="Starting…";
  try{
    const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({topic:$("topic").value,difficulty:$("difficulty").value,count})});
    const j=await r.json();
    if(!r.ok||!j.ok)throw j;
    total=j.total;qNum=1;$("diffBadge").textContent=j.difficulty;$("topicBadge").textContent=j.topic;
    setQuestion(j.current);show("interviewScreen");
  }catch(e){$("setupErr").textContent=friendly(e);}
  $("startBtn").disabled=false;$("startBtn").textContent="▶ Start interview";
};
function setQuestion(q){
  qNum=q.n;$("question").textContent=q.question;
  $("progress").textContent=`Q${q.n}/${total}`;
  $("barFill").style.width=((q.n-1)/total*100)+"%";
  $("fuBadge").classList.toggle("hidden",!q.is_followup);
  $("evalCard").classList.add("hidden");$("nextBtn").classList.add("hidden");$("reportBtn").classList.add("hidden");
  $("answer").value="";$("charCount").textContent="0 / 2000";$("answerErr").textContent="";
  startTimer();$("answer").focus();
}
// answer
$("submitBtn").onclick=async()=>{
  $("answerErr").textContent="";$("submitBtn").disabled=true;$("submitBtn").textContent="Evaluating…";
  try{
    const r=await fetch("/api/answer",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({answer:$("answer").value})});
    const j=await r.json();
    if(!r.ok||!j.ok)throw j;
    stopTimer();showEval(j.evaluation);
    $("barFill").style.width=(j.done?100:(j.next.n-1)/total*100)+"%";
    pendingNext=j.next||null;pendingDone=!!j.done;
    $("nextBtn").classList.toggle("hidden",!(!j.done&&j.next));
    $("reportBtn").classList.toggle("hidden",!j.done);
  }catch(e){$("answerErr").textContent=friendly(e);}
  $("submitBtn").disabled=false;$("submitBtn").textContent="Submit answer";
};
function showEval(ev){
  $("evalCard").classList.remove("hidden");
  const ring=$("scoreRing");ring.textContent=ev.score;
  ring.style.borderColor=ev.score>=70?"#22c55e":ev.score>=45?"#f5b301":"#e01b3c";
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
    renderReport(j);show("reportScreen");stopTimer();
  }catch(e){$("answerErr").textContent=friendly(e);}
}
function renderReport(j){
  const rep=j.report;
  $("partialTag").classList.toggle("hidden",!j.partial);
  const v=$("verdict");v.textContent=rep.verdict;
  v.className="verdict "+(rep.verdict==="HIRE"?"hire":"nohire");
  $("overall").textContent=rep.overall;
  $("perQ").innerHTML=(rep.per_question||[]).map(p=>`<span>Q${p.n}: <b>${p.score}</b></span>`).join("");
  $("rConcepts").innerHTML=(rep.concepts_demonstrated||[]).map(s=>`<li>${escapeHtml(s)}</li>`).join("")||"<li>—</li>";
  $("rMissing").innerHTML=(rep.missing_concepts||[]).map(s=>`<li>${escapeHtml(s)}</li>`).join("")||"<li>—</li>";
  $("rStudy").innerHTML=(rep.study_topics||[]).map(s=>`<li>${escapeHtml(s)}</li>`).join("");
  $("rStrong").textContent=rep.stronger_answer_example||"";
}
$("copyBtn").onclick=()=>{
  const t=`InterviewSim report — ${$("topicBadge").textContent} ${$("diffBadge").textContent}\nVerdict: ${$("verdict").textContent} | Overall: ${$("overall").textContent}/100\n${$("perQ").textContent}\nStronger answer:\n${$("rStrong").textContent}`;
  navigator.clipboard.writeText(t).then(()=>alert("Report copied."));
};
$("printBtn").onclick=()=>window.print();
$("restartBtn").onclick=()=>location.reload();
// settings
$("settingsBtn").onclick=()=>{$("settingsModal").classList.remove("hidden");$("keyMsg").textContent="";refreshKeyState();};
$("closeSettingsBtn").onclick=()=>$("settingsModal").classList.add("hidden");
$("saveKeyBtn").onclick=async()=>{
  $("keyMsg").textContent="Verifying via models.list…";
  try{
    const r=await fetch("/api/key",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key:$("keyInput").value})});
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
