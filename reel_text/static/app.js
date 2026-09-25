"use strict";
const $ = (id) => document.getElementById(id);
const pending = new Set(["queued", "downloading", "transcribing"]);
const labels = {queued:"Queued",downloading:"Downloading audio",transcribing:"Transcribing",done:"Done",error:"Error",interrupted:"Interrupted"};
let ready = false, submitting = false, jobs = [], timer = null, lastSnapshot = "", toastTimer;
function message(id,text,error=false){const el=$(id);el.textContent=text;el.hidden=!text;el.classList.toggle("error",error);}
function updateSubmit(){$("submit").disabled=!ready||submitting;$('submit').firstChild.textContent=submitting?"Adding… ":"Get transcripts ";}
async function api(path,options={}){const response=await fetch(path,{...options,headers:{"Content-Type":"application/json",...options.headers}});let body;try{body=await response.json();}catch{throw new Error("Could not read the application response.");}if(!response.ok)throw new Error(typeof body.detail==="string"?body.detail:"Could not complete the request.");return body;}
function applyStatus(status){ready=status.configured&&status.ffmpeg&&status.downloader;$("key-state").textContent=status.configured?"Key saved":"Key required";if(!status.configured)$("settings").open=true;const missing=[];if(!status.ffmpeg)missing.push("ffmpeg");if(!status.downloader)missing.push("yt-dlp");message("requirements",missing.length?`Missing ${missing.join(" and ")}. See the project README for setup instructions.`:"",true);updateSubmit();}
function toast(text){$("toast").textContent=text;$("toast").hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$("toast").hidden=true;},2200);}
async function copyText(text){try{await navigator.clipboard.writeText(text);toast("Text copied");}catch{toast("Copying is unavailable. Download the .txt file or select the text.");}}
function el(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;}
function render(){
 const complete=jobs.filter(j=>j.status==="done"),active=jobs.filter(j=>pending.has(j.status));
 $("count").textContent=String(complete.length);$("export").hidden=!complete.length;$("empty").hidden=!!jobs.length;$("history-note").hidden=jobs.length<200;
 $("queue-summary").textContent=active.length?`Processing or queued: ${active.length}. Links are processed one at a time.`:jobs.length?"Completed transcripts stay here between sessions.":"Your video transcripts will appear here.";
 const snapshot=JSON.stringify(jobs);if(snapshot===lastSnapshot)return;lastSnapshot=snapshot;
 const fragment=document.createDocumentFragment();
 for(const job of jobs){
  const card=el("article","job"),head=el("div","job-head"),source=el("a","job-source",job.url.replace("https://www.instagram.com/","instagram.com/"));
  source.href=job.url;source.target="_blank";source.rel="noreferrer noopener";source.title=job.url;head.append(source,el("span",`badge ${job.status}`,labels[job.status]||job.status));card.append(head);
  if(job.status==="done"){
   card.append(el("p","transcript",job.transcript));const footer=el("div","job-footer"),copy=el("button","small-button","Copy"),download=el("a","","Download .txt");
   copy.type="button";copy.addEventListener("click",()=>copyText(job.transcript));download.href=`/api/jobs/${encodeURIComponent(job.id)}/text`;footer.append(copy,download);card.append(footer);
  }else if(job.error){card.append(el("p","job-error",job.error));}else{card.append(el("p","job-pending",job.status==="queued"?"Processing will start after the previous link finishes.":job.status==="downloading"?"Downloading the video’s audio track…":"Audio sent to Deepgram. Waiting for the transcript…"));}
  fragment.append(card);
 }
 $("jobs").replaceChildren(fragment);
}
async function refresh(){clearTimeout(timer);try{const result=await api("/api/jobs");jobs=result.jobs;message("connection-error","");render();if(jobs.some(j=>pending.has(j.status)))timer=setTimeout(refresh,2000);}catch{message("connection-error","Cannot reach the application. Keep its terminal window open. Retrying in 5 seconds.",true);timer=setTimeout(refresh,5000);}}
$("links-form").addEventListener("submit",async(event)=>{event.preventDefault();if(submitting||!ready)return;submitting=true;updateSubmit();message("form-error","");try{const result=await api("/api/jobs",{method:"POST",body:JSON.stringify({urls:$("urls").value,language:$("language").value})});$("urls").value="";toast(`Links added: ${result.jobs.length}`);await refresh();}catch(error){message("form-error",error.message||"Could not add the links.",true);}finally{submitting=false;updateSubmit();}});
$("key-form").addEventListener("submit",async(event)=>{event.preventDefault();$("save-key").disabled=true;message("key-message","");try{const result=await api("/api/settings",{method:"POST",body:JSON.stringify({api_key:$("api-key").value})});$("api-key").value="";applyStatus(result);message("key-message","Key saved. You can now add links.");}catch(error){message("key-message",error.message||"Could not save the key.",true);}finally{$("save-key").disabled=false;}});
async function start(){try{applyStatus(await api("/api/status"));}catch{message("requirements","Cannot connect. Reload this page after starting the application.",true);}await refresh();}
start();
