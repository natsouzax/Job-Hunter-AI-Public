const state = { dashboard: null };
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value = "") => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));

function notify(message, error = false) {
  const box = $("#notice"); box.textContent = message; box.className = `notice${error ? " error" : ""}`;
  clearTimeout(notify.timer); notify.timer = setTimeout(() => box.classList.add("hidden"), 5000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Falha inesperada");
  return data;
}

async function load() {
  try { state.dashboard = await (await fetch("/api/dashboard")).json(); render(); }
  catch (error) { notify(error.message, true); }
}

function go(view) {
  $$(".view").forEach(el => el.classList.toggle("active", el.id === `view-${view}`));
  $$(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === view));
  $("#page-title").textContent = {dashboard:"Visão geral", "new-job":"Nova vaga", profile:"Perfil", system:"Sistema"}[view];
  location.hash = view;
}

function statusLabel(status) {
  return ({DISCOVERED:"Descoberta", SCREENED:"Avaliada", SHORTLISTED:"Selecionada", CV_GENERATING:"Gerando documentos", CV_REVIEW:"Em revisão", CV_APPROVED:"Documentos aprovados", READY_FOR_APPROVAL:"Aguardando você", USER_APPROVED:"Aprovada por você", NEEDS_INFORMATION:"Informação em falta", REVISION_REQUIRED:"Revisão necessária", REJECTED:"Rejeitada", WITHDRAWN:"Retirada"})[status] || status;
}
function statusClass(status) { return status === "READY_FOR_APPROVAL" ? "ready" : status === "USER_APPROVED" ? "approved" : status === "NEEDS_INFORMATION" ? "needs" : ""; }

function render() {
  const data = state.dashboard;
  const s = data.stats;
  $("#stats").innerHTML = [
    ["Candidaturas", s.applications, ""], ["Aguardando aprovação", s.ready, "highlight"],
    ["Informação em falta", s.needs_information, ""], ["Aprovadas por você", s.approved, ""]
  ].map(([label, value, cls]) => `<article class="stat ${cls}"><span>${label}</span><strong>${value}</strong></article>`).join("");
  $("#onboarding").innerHTML = data.profile ? "" : `<div class="onboarding"><div><h3>Prepare o seu perfil</h3><p class="muted">Introduza os seus dados e confirme apenas factos verdadeiros.</p></div><button class="primary" data-go="profile">Criar perfil</button></div>`;
  renderApplications(data.applications); renderProfile(data.profile); renderSystem(data.system);
}

function renderApplications(apps) {
  const root = $("#applications");
  if (!apps.length) { root.innerHTML = `<div class="empty"><h3>Nenhuma candidatura ainda</h3><p>Adicione uma vaga para iniciar o pipeline.</p></div>`; return; }
  root.innerHTML = apps.map(app => `<article class="application">
    <div><h3>${esc(app.job?.title || "Vaga")}</h3><p>${esc(app.job?.company || "—")} · ${esc(app.job?.location || "—")}</p></div>
    <span class="badge ${statusClass(app.state)}">${esc(statusLabel(app.state))}</span>
    <div class="score">${app.review?.score ?? app.assessment?.score ?? "—"}<small>/100</small></div>
    <small>${new Date(app.updated_at).toLocaleDateString("pt-PT", {day:"2-digit", month:"short", year:"numeric"})}</small>
    <button class="open-app" data-open-app="${app.id}" aria-label="Abrir candidatura">›</button>
  </article>`).join("");
}

function renderProfile(profile) {
  const root = $("#profile-content");
  if (!profile) { root.innerHTML = `<form id="profile-form" class="panel form-grid"><label>Nome<input name="name" required></label><label>Localização<input name="location" required></label><label class="span-2">Factos verificados (um por linha)<textarea name="facts" required rows="6" placeholder="Ex.: Experiência em apoio informático"></textarea></label><p class="muted span-2">Escreva apenas informações que você confirma. A disponibilidade fica por confirmar; você poderá defini-la depois.</p><div class="form-actions span-2"><button class="primary" type="submit">Criar perfil local</button></div></form>`; return; }
  const contact = profile.preferences.contact || {};
  root.innerHTML = `<div class="profile-grid">
    <div class="profile-meta">
      <article class="panel"><p class="eyebrow">CANDIDATO</p><h2>${esc(profile.name)}</h2><p class="muted">${esc(profile.location)}</p>
        <div class="key-value"><span>Disponibilidade</span><strong>${profile.availability.status === "confirmed" ? "Confirmada" : "Por confirmar"}</strong></div>
        <div class="key-value"><span>Email</span><strong>${esc(contact.email || "—")}</strong></div><div class="key-value"><span>Telefone</span><strong>${esc(contact.phone || "—")}</strong></div>
      </article>
      <form id="contact-form" class="panel"><h3>Contacto</h3><label>Email<input type="email" name="email" value="${esc(contact.email || "")}"></label><br><label>Telefone<input name="phone" value="${esc(contact.phone || "")}"></label><div class="form-actions"><button class="primary" type="submit">Guardar</button></div></form>
      ${profile.availability.status !== "confirmed" ? `<form id="availability-form" class="panel"><h3>Disponibilidade</h3><p class="muted">Escolha uma janela que você pode cumprir. Repita na interface depois para mais dias.</p><div class="inline-fields"><select name="day" required><option value="0">Segunda</option><option value="1">Terça</option><option value="2">Quarta</option><option value="3">Quinta</option><option value="4">Sexta</option><option value="5">Sábado</option><option value="6">Domingo</option></select><input type="time" name="start" required><input type="time" name="end" required></div><br><button class="primary" type="submit">Confirmar disponibilidade</button></form>` : ""}
    </div>
    <article class="panel"><p class="eyebrow">FACTOS VERIFICADOS</p><h3>${profile.facts.length} factos permitidos</h3><div class="facts">${profile.facts.map(f => `<div class="fact"><small>${esc(f.category)}</small><p>${esc(f.statement)}</p></div>`).join("")}</div></article>
  </div>`;
}

function renderSystem(system) {
  $("#system-content").innerHTML = `<div class="system-grid"><article class="panel"><h3>Ambiente local</h3>
    <div class="key-value"><span>Python</span><strong>${esc(system.python)}</strong></div><div class="key-value"><span>Base</span><strong>${esc(system.database)}</strong></div><div class="key-value"><span>Tectonic</span><strong>${system.tectonic ? "Disponível" : "Não encontrado"}</strong></div>
  </article><article class="panel roadmap"><p class="eyebrow">AINDA NÃO IMPLEMENTADO</p><h3>Próximas camadas</h3><p>Pesquisa automática de vagas e Applicant com preenchimento assistido. Estes recursos não aparecem como ações porque ainda não existem no backend.</p><div class="key-value"><span>Scraping</span><strong>Desativado</strong></div><div class="key-value"><span>Submissão</span><strong>Desativada</strong></div></article></div>`;
}

function openApplication(id) {
  const app = state.dashboard.applications.find(item => item.id === id); if (!app) return;
  const missing = app.assessment?.missing_information || [];
  const detail = $("#application-detail");
  detail.innerHTML = `<div class="detail-head"><p class="eyebrow">${esc(statusLabel(app.state))}</p><h2>${esc(app.job?.title)}</h2><p class="muted">${esc(app.job?.company)} · ${esc(app.job?.location)}</p></div>
    <div class="detail-grid"><div class="detail-block"><span class="muted">Score da vaga</span><strong>${app.assessment?.score ?? "—"}</strong></div><div class="detail-block"><span class="muted">Score dos documentos</span><strong>${app.review?.score ?? "—"}</strong></div></div>
    ${missing.length ? `<div class="detail-block"><h3>Informação em falta</h3><ul>${missing.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${app.review?.critical_issues?.length ? `<div class="detail-block"><h3>Problemas críticos</h3><ul>${app.review.critical_issues.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    <div class="document-links">${app.documents.map(doc => `<a href="${doc.url}" target="_blank" rel="noopener">Abrir ${doc.kind === "cv" ? "CV" : "carta"} v${doc.version}</a>`).join("")}</div>
    <div class="dialog-actions">${app.actions.resume ? `<button class="primary" data-app-action="resume" data-id="${id}">Retomar pipeline</button>` : ""}${app.actions.approve ? `<button class="primary" data-app-action="approve" data-id="${id}">Li os documentos e aprovo</button>` : ""}${app.actions.withdraw ? `<button class="danger" data-app-action="withdraw" data-id="${id}">Retirar candidatura</button>` : ""}</div>
    <h3 style="margin-top:28px">Histórico</h3><div class="timeline">${app.events.slice().reverse().map(event => `<div class="timeline-item"><strong>${esc(event.next_state ? statusLabel(event.next_state) : event.event_type)}</strong><small>${esc(event.agent)} · ${new Date(event.created_at).toLocaleString("pt-PT")}</small></div>`).join("")}</div>`;
  $("#application-dialog").showModal();
}

function addRequirement() {
  const row = document.createElement("div"); row.className = "requirement-row";
  row.innerHTML = `<input name="requirement_key" required placeholder="chave: python"><input name="requirement_description" required placeholder="Descrição do requisito"><label><input type="checkbox" name="requirement_mandatory"> Obrigatório</label><button type="button" class="icon-button" aria-label="Remover">×</button>`;
  $(".icon-button", row).onclick = () => row.remove(); $("#requirements").append(row);
}

async function mutation(path, body, success) {
  try { const data = await api(path, {method:"POST", body:JSON.stringify(body)}); state.dashboard = data.dashboard; render(); notify(success); return data; }
  catch (error) { notify(error.message, true); throw error; }
}

document.addEventListener("click", async event => {
  const nav = event.target.closest("[data-view], [data-go]"); if (nav) go(nav.dataset.view || nav.dataset.go);
  const open = event.target.closest("[data-open-app]"); if (open) openApplication(open.dataset.openApp);
  if (event.target.closest(".dialog-close")) $("#application-dialog").close();
  const action = event.target.closest("[data-action]");
  const appAction = event.target.closest("[data-app-action]");
  if (appAction) {
    const type = appAction.dataset.appAction;
    const question = type === "approve" ? "Você abriu e conferiu o CV e a carta?" : type === "withdraw" ? "Retirar esta candidatura?" : null;
    if (!question || confirm(question)) { await mutation(`/api/applications/${appAction.dataset.id}/${type}`, type === "approve" ? {confirmed:true} : {}, "Candidatura atualizada."); $("#application-dialog").close(); }
  }
});

$("#refresh").onclick = load;
$("#add-requirement").onclick = addRequirement;
document.addEventListener("submit", async event => {
  event.preventDefault();
  if (event.target.id === "profile-form") { const f = new FormData(event.target); const facts = String(f.get("facts")).split(/\r?\n/).map(s => s.trim()).filter(Boolean).map(statement => ({category:"experience", statement, tags:[]})); await mutation("/api/profile/init", {name:f.get("name"), location:f.get("location"), facts, availability:[]}, "Perfil criado. Defina a disponibilidade antes de avaliar vagas com horário."); }
  if (event.target.id === "availability-form") { const f = new FormData(event.target); if (confirm("Confirma que este horário está atual?")) await mutation("/api/profile/availability", {confirmed:true, windows:[{day:Number(f.get("day")), start:f.get("start"), end:f.get("end")}]}, "Disponibilidade confirmada."); }
  if (event.target.id === "contact-form") { const f = new FormData(event.target); await mutation("/api/profile/contact", {email:f.get("email"), phone:f.get("phone")}, "Contacto guardado."); }
  if (event.target.id === "job-form") {
    const f = new FormData(event.target); const rows = $$(".requirement-row", event.target);
    if (!rows.length) return notify("Adicione pelo menos um requisito.", true);
    const day = f.get("day");
    const job = {company:f.get("company"), title:f.get("title"), location:f.get("location"), source_url:f.get("source_url"), description:f.get("description"), work_mode:f.get("work_mode"), requirements:rows.map(row => ({key:$("[name=requirement_key]",row).value, description:$("[name=requirement_description]",row).value, mandatory:$("[name=requirement_mandatory]",row).checked})), schedule:day === "" ? null : {windows:[{day:Number(day), start:f.get("start"), end:f.get("end")}]}};
    const data = await mutation("/api/jobs/run", {job}, "Pipeline executado."); if (data) { event.target.reset(); $("#requirements").innerHTML = ""; addRequirement(); go("dashboard"); }
  }
});

addRequirement();
const initial = location.hash.slice(1); if (["dashboard","new-job","profile","system"].includes(initial)) go(initial);
load();
