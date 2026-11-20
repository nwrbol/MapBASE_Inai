/* Чат отправляет только сводку карты. Все изменения обратимы в этой вкладке. */
document.addEventListener("DOMContentLoaded", async () => {
  const $ = id => document.getElementById(id);
  const app = window.MapApp, log = $("chat-log"), input = $("chat-input"), form = $("chat-form");
  const storageKey = "coverage-chat-v1";
  let history = [], pending = null, undo = null, clearConfirm = false;
  let device = "";
  try {
    device = sessionStorage.getItem("chat-device") || crypto.randomUUID();
    sessionStorage.setItem("chat-device", device);
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
    history = saved.filter(m => ["user","assistant"].includes(m.role) && typeof m.content === "string").slice(-24);
  } catch { /* без хранилища чат работает в памяти */ }

  function save() {
    history = history.slice(-24);
    try { sessionStorage.setItem(storageKey, JSON.stringify(history)); } catch {}
    $("chat-suggestions").hidden = history.length > 0;
  }
  function button(node, label, fn, cls="btn") {
    const b = document.createElement("button");
    b.type="button"; b.className=cls; b.textContent=label; b.addEventListener("click",fn); node.append(b);
    return b;
  }
  function bubble(role, text) {
    const follow = log.scrollHeight-log.scrollTop-log.clientHeight < 40;
    const node = document.createElement("div"); node.className="chat-msg "+role;
    if (role === "assistant") {
      text.split(/(\bX\d{3}_Y\d{3}\b|\bC\d+\b)/).forEach((part,i) => {
        if (i%2) button(node,part,() => { try {app.flyToTarget(part);} catch(e) {bubble("error",e.message);} },"chat-ref");
        else node.append(document.createTextNode(part));
      });
    } else node.textContent=text;
    log.append(node);
    if (follow) log.scrollTop=log.scrollHeight;
    return node;
  }
  function collectMapState() { return app.getState(); }
  const commands = {
    set_layers:a=>app.setActiveLayers(a.only), fly_to:a=>app.flyToTarget(a.target,a.zoom),
    fit_view:a=>app.fitView(a.what), open_tab:a=>app.openTab(a.tab),
    set_candidate_status:a=>app.setCandidateStatus(a.id,a.status,a.reason),
    select_pair:a=>app.selectPair(a.a,a.b), reset_plan:()=>app.resetPlan(),
    toggle_field:a=>app.setFieldIncluded(a.on), highlight_cells:a=>app.highlightCells(a.cell_ids,a.seconds),
    set_panel_width:a=>app.setPanelWidth(a.px),
  };
  async function applyActions(actions,node,snapshot,dataset) {
    if (!actions.length) return;
    if (app.analysisId !== dataset) throw new Error("Набор данных изменился. Повторите вопрос.");
    const cleanups=[];
    for (const {type,args,label} of actions) {
      const chip=document.createElement("span"); chip.className="chat-chip"; node.append(chip);
      try {
        if (type==="open_tab" && snapshot.is_mobile && snapshot.open_tab==="assistant") {
          button(chip,label,()=>app.openTab(args.tab)); continue;
        }
        const effect=await commands[type](args);
        if (typeof effect==="function") cleanups.push(effect);
        chip.textContent="✓ "+label;
        if (type==="open_tab" && args.tab!=="assistant") {
          const page=$("panel-"+args.tab);
          page.querySelector(".chat-back")?.remove();
          const back=button(page,"← К чату",()=>{app.openTab("assistant");back.remove();},"btn chat-back");
          page.prepend(back);
        }
      } catch(e) { chip.textContent="✗ "+label+" · "+e.message; }
    }
    undo?.remove();
    undo=button(node,"Отменить действия",async()=>{
      try {
        if (app.analysisId!==dataset) throw new Error("Открыт другой набор данных");
        cleanups.forEach(fn=>fn()); await app.restore(snapshot); undo.remove(); undo=null;
      } catch(e) {bubble("error",e.message);}
    });
  }

  function busy(on) {
    input.disabled=on; form.querySelector(".primary").disabled=on; $("chat-clear").disabled=on; $("chat-stop").hidden=!on;
  }
  async function send(question,retry=false) {
    if (pending || !question.trim()) return;
    const ctrl=new AbortController(); pending=ctrl; busy(true);
    if (!retry || history.at(-1)?.content!==question || history.at(-1)?.role!=="user") {
      history.push({role:"user",content:question}); bubble("user",question); save();
    }
    input.value=""; input.style.height="";
    const indicator=bubble("status","Ассистент печатает…");
    try {
      const state=collectMapState(), dataset=app.analysisId;
      const res=await fetch("/api/assistant/chat",{method:"POST",headers:{"Content-Type":"application/json","X-Device-Id":device},
        body:JSON.stringify({messages:history.map(m=>({...m,content:m.content.slice(0,2000)})),map_state:state,analysis_id:dataset,include_field:state.field_included}),signal:ctrl.signal});
      const data=await res.json();
      if (!res.ok) throw new Error(typeof data.detail==="string"?data.detail:"Не удалось обработать вопрос");
      history.push({role:"assistant",content:data.reply}); save();
      await applyActions(data.actions,bubble("assistant",data.reply),state,dataset);
    } catch(e) {
      const node=bubble("error",ctrl.signal.aborted?"Запрос остановлен":e.message);
      button(node,"Повторить",()=>send(question,true));
    } finally {
      indicator.remove(); pending=null; busy(false);
      if (!$("panel-assistant").hidden) input.focus();
    }
  }
  form.addEventListener("submit",e=>{e.preventDefault();send(input.value.trim());});
  input.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey&&!e.isComposing){e.preventDefault();form.requestSubmit();}});
  input.addEventListener("input",()=>{input.style.height="auto"; input.style.height=Math.min(input.scrollHeight,128)+"px";});
  $("chat-stop").onclick=()=>pending?.abort();
  $("chat-clear").onclick=()=>{
    if (!clearConfirm) {clearConfirm=true; $("chat-clear").textContent="Точно?"; return;}
    clearConfirm=false; $("chat-clear").textContent="Очистить"; history=[]; log.replaceChildren(); undo=null; save();
  };
  async function init() {
    try {
      const res=await fetch("/api/assistant/status"); if(!res.ok) throw Error();
      const status=await res.json();
      if (!status.enabled) {
        $("chat-status").textContent="ИИ-ассистент не настроен. Администратору: задайте GROQ_API_KEY и перезапустите сервер.";
        return;
      }
      $("chat-status").textContent="Отвечает по данным карты и управляет её видом. Модель: "+status.model;
      $("chat-controls").hidden=false;
      history.forEach(m=>bubble(m.role,m.content));
      status.suggestions.forEach(q=>button($("chat-suggestions"),q,()=>send(q))); save();
    } catch {
      $("chat-status").textContent="Не удалось проверить доступность ассистента. ";
      button($("chat-status"),"Повторить",init);
    }
  }
  await init();
});
