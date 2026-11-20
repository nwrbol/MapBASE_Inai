// Форматируем только текст и известные конструкции. HTML и URL из ответа остаются текстом.
function renderChatMarkdown(root, source, addReference) {
  const reference = /\b(?:X\d{3}_Y\d{3}|C\d+)\b/g;
  function element(parent, tag) {
    const child = document.createElement(tag);
    parent.append(child);
    return child;
  }
  function plain(parent, value) {
    let offset = 0;
    for (const match of value.matchAll(reference)) {
      parent.append(value.slice(offset, match.index));
      addReference(parent, match[0]);
      offset = match.index + match[0].length;
    }
    parent.append(value.slice(offset));
  }
  function inline(parent, value) {
    const tokens = /(`)([^`\n]+)\1|(\*\*|__)(\S(?:.*?\S)?)\3|(\*)([^*\n]+)\5|(?<![\wА-Яа-я])_([^_\n]+)_(?![\wА-Яа-я])/gu;
    let offset = 0;
    for (const match of value.matchAll(tokens)) {
      plain(parent, value.slice(offset, match.index));
      const child = element(parent, match[1] ? "code" : match[3] ? "strong" : "em");
      plain(child, match[2] ?? match[4] ?? match[6] ?? match[7]);
      offset = match.index + match[0].length;
    }
    plain(parent, value.slice(offset));
  }
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const columns = line => line.trim().replace(/^\||\|$/g, "").split("|").map(s => s.trim());
  let paragraph = null, list = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const item = /^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/.exec(line);
    if (item) {
      const tag = item[1] ? "ul" : "ol";
      if (list?.localName !== tag) {
        list = element(root, tag);
        if (tag === "ol") list.start = Number(item[2]);
      }
      inline(element(list, "li"), item[3]);
      paragraph = null;
      continue;
    }
    list = null;
    if (!line.trim()) { paragraph = null; continue; }
    if (/^\s*```/.test(line)) {
      const content = [];
      while (++i < lines.length && !/^\s*```\s*$/.test(lines[i])) content.push(lines[i]);
      element(element(root, "pre"), "code").textContent = content.join("\n");
    } else if (line.includes("|") && lines[i+1]?.includes("|") &&
               columns(lines[i+1]).every(s => /^:?-{3,}:?$/.test(s))) {
      const table = element(root, "table");
      const row = (section, text, tag) => {
        const tr = element(section, "tr");
        columns(text).forEach(col => inline(element(tr, tag), col));
      };
      row(element(table, "thead"), line, "th");
      const body = element(table, "tbody");
      i++;
      while (lines[i+1]?.includes("|")) row(body, lines[++i], "td");
    } else if (/^#{1,6}\s/.test(line)) {
      inline(element(root, "h3"), line.replace(/^#{1,6}\s+/, ""));
    } else {
      paragraph ??= element(root, "p");
      if (paragraph.hasChildNodes()) element(paragraph, "br");
      inline(paragraph, line);
      continue;
    }
    paragraph = null;
  }
}

/* Чат отправляет только сводку карты. Все изменения обратимы в этой вкладке. */
document.addEventListener("DOMContentLoaded", async () => {
  const $ = id => document.getElementById(id);
  const app = window.MapApp, log = $("chat-log"), input = $("chat-input"), form = $("chat-form");
  const storageKey = "coverage-chat-v1";
  let history = [], pending = null, undo = null, clearConfirm = false;
  let device = "";
  try {
    device = sessionStorage.getItem("chat-device") || Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2,"0")).join("");
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
      renderChatMarkdown(node, text, (parent, id) => button(parent, id, () => {
        try { app.flyToTarget(id); } catch (e) { bubble("error", e.message); }
      }, "chat-ref"));
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
      if (!res.ok) {
        if (res.status===422) { console.error("assistant 422:",data.detail); throw new Error("Не удалось отправить вопрос (ошибка данных)"); }
        throw new Error(typeof data.detail==="string"?data.detail:"Не удалось обработать вопрос");
      }
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
