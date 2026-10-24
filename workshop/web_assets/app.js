'use strict';
const $ = id => document.getElementById(id);
const form = $('run-form');
let snapshot = null, watching = '', pending = null, posting = false;
const startedHere = new Set();
let signature = '', lastStatus = '', pollTimer;
function el(tag, text, cls) { const n = document.createElement(tag); if (text != null) n.textContent = String(text); if (cls) n.className = cls; return n; }
function status(text, error = false) { if (lastStatus !== text) { $('status').textContent = text; lastStatus = text; } $('status').classList.toggle('error', error); }
function fmt(n, suffix = '') { return n == null ? '未取得' : Number(n).toLocaleString('ja-JP', {maximumFractionDigits:1}) + suffix; }
function stamp(s) { return s ? new Date(s).toLocaleString('ja-JP', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) : ''; }
function time(s) { return s ? new Date(s).toLocaleString('ja-JP', {hour:'2-digit',minute:'2-digit'}) : ''; }
function safeTrace(url) { try { const u=new URL(url); return u.protocol==='https:' && u.hostname==='console.cloud.google.com' && u.pathname.startsWith('/traces/') ? u.href : null; } catch {return null;} }
// 最新の実行だけを表示する。前の実行の処理は Cloud Trace 側で見られる。
function showResult(r) {
 const target=$('result-content'); target.replaceChildren();
 // この画面で実行したもの（または実行中のもの）以外は「前回の回答」と明示する。
 const previous=Boolean(r) && !startedHere.has(r.run_id) && snapshot.active!==r.run_id;
 $('result-heading').textContent=previous?`前回の回答（${stamp(r.started_at)}）`:'回答';
 $('result-panel').classList.toggle('previous',previous);
 if(!r){target.append(el('p','実行すると、ここに回答と Cloud Trace へのボタンが表示されます。','hint'));return;}
 const job=snapshot.jobs.find(j=>j.run_id===r.run_id);
 const running=snapshot.active===r.run_id;
 if(r.question!==$('question').value)target.append(el('p','質問：'+r.question,'hint'));
 target.append(el('p',r.answer?.customer_message_ja||job?.error||r.error||(running?'回答を作成しています。':'回答はありません。'),'answer'));
 if(running)return;
 target.append(el('p',`Data Agent 呼び出し ${fmt(r.data_agent_calls,' 回')} ・ 実行時間 ${fmt(r.elapsed_seconds,' 秒')}${r.root_model_requested?' ・ '+r.root_model_requested:''}`,'facts'));
 const link=safeTrace(r.trace_console_url);
 if(link){const a=el('a','Cloud Trace で処理を開く ↗','primary');a.href=link;a.target='_blank';a.rel='noopener';target.append(a);}
 else target.append(el('p','この実行には Cloud Trace へのリンクがありません。','hint'));
 target.append(el('p',`実行した SQL は、BigQuery の「ジョブ履歴」で ${time(r.started_at)} ごろのクエリを開いて確かめます。`,'hint'));
}
function lock(busy){$('run-button').disabled=busy; $('run-button').textContent=busy?'分析しています…':'実行する →';}
async function refresh(){
 try {
  const response=await fetch('/api/state',{cache:'no-store'});if(!response.ok)throw Error('実行記録を読み込めません。');snapshot=await response.json();
  $('project').textContent=`${snapshot.project} / ${snapshot.dataset}`;
  if(!watching&&snapshot.active)watching=snapshot.active;
  lock(Boolean(snapshot.active)||posting);
  const sig=JSON.stringify([snapshot.active,snapshot.records,snapshot.jobs]);
  if(sig!==signature){signature=sig;showResult(snapshot.records.find(r=>r.run_id===watching)||snapshot.records[0]);}
  const j=snapshot.jobs.find(j=>j.run_id===watching),r=snapshot.records.find(r=>r.run_id===watching);
  if(snapshot.active){const ar=snapshot.records.find(r=>r.run_id===snapshot.active);status(ar?.phase==='data_agent'?'Data Agent が根拠を取得しています。':ar?.phase==='root_answer'?'取得した根拠から回答をまとめています。':'定義・Agent・実行環境を確認しています。'); const start=ar?.started_at||snapshot.jobs.find(j=>j.run_id===snapshot.active)?.started_at;$('timer').textContent=start?`経過 ${Math.floor((Date.now()-new Date(start))/1000)} 秒（数十秒〜数分かかります）`:'';}
  else if(watching){
   // 受付直後や終了直後は、記録がまだ running のことがある。確定した状態だけで判定する。
   const failed=['failed','interrupted'].includes(j?.state)||['failed','interrupted'].includes(r?.state), done=j?.state==='completed'||r?.state==='completed';
   if(failed||done){status(failed?(j?.error||r?.error||'処理を完了できませんでした。'):'実行が完了しました。「Cloud Trace で処理を開く」へ進みます。',failed);$('timer').textContent='';watching='';pending=null;form.elements.request_id.value=crypto.randomUUID().replaceAll('-','');}
   else status('実行を開始しています。');
  }
  else if(pending){status('実行の受付を確認できませんでした。同じ内容で再試行できます。',true);}
 }catch(err){status('画面の更新に失敗しました。サーバーの起動状態を確認してください。',true);}
 clearTimeout(pollTimer);pollTimer=setTimeout(refresh,snapshot?.active?2000:5000);
}
form.addEventListener('submit',async event=>{
 event.preventDefault();if(posting||snapshot?.active)return;
 const data=Object.fromEntries(new FormData(form));
 if(pending&&pending.question!==data.question){status('前の実行の受付を確認中です。質問を戻して再試行してください。',true);return;}
 if(pending)data.request_id=pending.request_id;pending=data;posting=true;lock(true);status('実行を受け付けています。');
 try{const response=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const result=await response.json();if(!response.ok){pending=null;throw Error(result.error||'実行を開始できません。');}watching=result.run_id;startedHere.add(result.run_id);pending=null;}
 catch(err){status(err.message||'通信を確認して再試行してください。',true);}
 finally{posting=false;signature='';await refresh();}
});
for(const event of ['blur','invalid','input'])$('question').addEventListener(event,()=>{$('question').setAttribute('aria-invalid',String(!$('question').validity.valid));});
refresh();
