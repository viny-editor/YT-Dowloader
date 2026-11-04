/* YT Downloader — painel do Premiere Pro (UXP).
 *
 * O Premiere não deixa extensões usarem http:// no Mac, então o painel conversa
 * com o app YT Downloader por arquivos numa pasta compartilhada:
 *   premiere/pedidos/<id>.json  ← o painel pede um download
 *   premiere/status/<id>.json   → o app responde com o andamento e os arquivos
 *   premiere/app.json           → "sinal de vida" do app (atualizado a cada 1,5 s)
 * Quando o download termina, o painel importa os arquivos no projeto, na pasta
 * "YT Downloads" ▸ Vídeos / SFX / B-roll.
 */
const fs = require("fs");
const os = require("os");
let ppro = null;
try { ppro = require("premierepro"); } catch (e) { /* fora do Premiere */ }
let shell = null;
try { shell = require("uxp").shell; } catch (e) { /* fora do Premiere */ }

const IS_WIN = /^win/i.test(String(os.platform()));
const HOME = String(os.homedir()).replace(/\\/g, "/").replace(/\/$/, "");
const BRIDGE = IS_WIN
  ? `${HOME}/AppData/Local/YT Downloader/premiere`
  : `${HOME}/Library/Application Support/YT Downloader/premiere`;
const APP_PATH = IS_WIN
  ? `${HOME}/AppData/Local/Programs/YT Downloader/YT Downloader.exe`
  : "/Applications/YT Downloader.app";
const ROOT_BIN = "YT Downloads";
const STORE_KEY = "ytd-premiere-v1";

const state = {
  kind: "video",       // video | sfx
  vq: "1080",
  aq: "wav",
  range: false,
  dest: "project",     // project | app | custom
  customFolder: "",    // pasta escolhida em "OUTRA PASTA"
  alive: false,
  appFolder: "",
  projectDir: "",
  jobs: [],            // {id, n, url, title, kind, quality, range, bin, status, phase, progress, files, imported, error}
  seq: 0,
};

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const esc = (t) => String(t == null ? "" : t).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------ memória */
function save() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      kind: state.kind, vq: state.vq, aq: state.aq, dest: state.dest, seq: state.seq,
      customFolder: state.customFolder,
      jobs: state.jobs.slice(-40),
    }));
  } catch (e) { /* sem armazenamento: segue sem lembrar */ }
}
function load() {
  try {
    const d = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    ["kind", "vq", "aq", "dest", "seq", "customFolder"].forEach((k) => { if (d[k] != null) state[k] = d[k]; });
    if (Array.isArray(d.jobs)) state.jobs = d.jobs;
  } catch (e) { /* ignora */ }
}

/* ------------------------------------------------------------ arquivos */
async function readJson(path) {
  try {
    const txt = await fs.readFile(path, { encoding: "utf-8" });
    return JSON.parse(txt);
  } catch (e) {
    return null;
  }
}
async function writeJsonAtomic(path, data) {
  const tmp = `${path}.${Math.random().toString(36).slice(2, 8)}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(data), { encoding: "utf-8" });
  await fs.rename(tmp, path);
}
async function ensureDirs() {
  try { await fs.mkdir(`${BRIDGE}/pedidos`, { recursive: true }); } catch (e) { /* já existe */ }
}

/* ------------------------------------------------------------ Premiere */
async function getProject() {
  if (!ppro) return null;
  try { return await ppro.Project.getActiveProject(); } catch (e) { return null; }
}
function dirname(p) {
  const s = String(p || "");
  const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
  return i > 0 ? s.slice(0, i) : "";
}
async function refreshProject() {
  const project = await getProject();
  const path = project && project.path ? String(project.path) : "";
  state.projectDir = /\.prproj$/i.test(path) ? dirname(path) : "";
}
async function findBin(parent, name) {
  const items = await parent.getItems();
  for (const it of items) {
    const f = ppro.FolderItem.cast(it);
    if (f && it.name === name) return f;
  }
  return null;
}
async function ensureBin(project, parent, name) {
  let bin = await findBin(parent, name);
  if (bin) return bin;
  project.lockedAccess(() => {
    project.executeTransaction((compound) => {
      compound.addAction(parent.createBinAction(name, false));
    }, `Criar pasta ${name}`);
  });
  for (let i = 0; i < 20; i++) {
    bin = await findBin(parent, name);
    if (bin) return bin;
    await sleep(100);
  }
  return parent;
}
async function importJob(job) {
  const project = await getProject();
  if (!project) throw new Error("Abra um projeto no Premiere para importar.");
  const root = await project.getRootItem();
  const main = await ensureBin(project, root, ROOT_BIN);
  const target = await ensureBin(project, main, job.bin);
  const ok = await project.importFiles(job.files, true, target, false);
  if (ok === false) throw new Error("O Premiere não conseguiu importar o arquivo.");
}

/* ------------------------------------------------------------ pedidos */
function binFor(kind, range) {
  if (kind === "sfx") return "SFX";
  return range ? "B-roll" : "Vídeos";
}
function destFolder() {
  if (state.dest === "project" && state.projectDir) return `${state.projectDir}/${ROOT_BIN}`;
  if (state.dest === "custom" && state.customFolder) return state.customFolder;
  return "";  // vazio = pasta padrão do app
}
async function pickFolder() {
  try {
    const lfs = require("uxp").storage.localFileSystem;
    const folder = await lfs.getFolder();
    if (!folder) return false;  // cancelou
    state.customFolder = String(folder.nativePath || "");
    state.dest = "custom";
    save();
    renderControls();
    renderDest();
    say("Pasta escolhida. Os próximos downloads vão para ela.");
    return true;
  } catch (e) {
    say(`Não consegui abrir o seletor de pastas: ${e.message || e}`, true);
    return false;
  }
}
function say(text, err) {
  const m = $("msg");
  m.textContent = text;
  m.className = err ? "msg err" : "msg";
}

async function submit() {
  const url = $("url").value.trim();
  if (!/^https?:\/\//i.test(url)) {
    say(url ? "O link precisa começar com https://" : "Cole um link do YouTube.", true);
    return;
  }
  const range = state.range;
  const start = range ? $("start").value.trim() : "";
  const end = range ? $("end").value.trim() : "";
  if (range && !start && !end) {
    say("Preencha o início e/ou o fim do trecho.", true);
    return;
  }
  await refreshProject();
  const quality = state.kind === "sfx" ? state.aq : state.vq;
  const id = `p${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const folder = destFolder();
  const req = { id, url, quality, h264: true, start, end, playlist: null };
  if (folder) req.folder = folder;
  try {
    await ensureDirs();
    await writeJsonAtomic(`${BRIDGE}/pedidos/${id}.json`, req);
  } catch (e) {
    say(`Não consegui falar com o app: ${e.message || e}`, true);
    return;
  }
  state.seq += 1;
  state.jobs.push({
    id, n: state.seq, url, title: url, kind: state.kind, quality, range,
    bin: binFor(state.kind, range), status: "sent", phase: state.alive ? "Enviado" : "Aguardando o app abrir",
    progress: null, files: [], imported: [], error: null, created: Date.now(),
  });
  save();
  $("url").value = "";
  $("url").focus();
  say(state.alive ? "Na fila! Cole o próximo link e aperte Enter." : "Pedido guardado. Abra o YT Downloader para começar.");
  render();
}

async function cancelJob(id) {
  const job = state.jobs.find((j) => j.id === id);
  if (!job) return;
  if (["sent", "queued", "starting", "downloading", "processing"].includes(job.status)) {
    try { await fs.writeFile(`${BRIDGE}/pedidos/${id}.cancel`, "1", { encoding: "utf-8" }); } catch (e) { /* ignora */ }
    try { await fs.unlink(`${BRIDGE}/pedidos/${id}.json`); } catch (e) { /* já foi lido */ }
    job.status = "canceled";
    job.phase = "Cancelado";
  } else {
    state.jobs = state.jobs.filter((j) => j.id !== id);
  }
  save();
  render();
}

/* ------------------------------------------------------------ ciclo */
const ACTIVE = ["sent", "queued", "starting", "downloading", "processing", "importing"];
let busy = false;
async function tick() {
  if (busy) return;
  busy = true;
  try {
    const app = await readJson(`${BRIDGE}/app.json`);
    const alive = !!(app && Date.now() / 1000 - Number(app.alive || 0) < 6);
    state.alive = alive;
    state.appFolder = app && app.folder ? app.folder : state.appFolder;

    let changed = false;
    for (const job of state.jobs) {
      if (!ACTIVE.includes(job.status) || job.status === "importing") continue;
      const st = await readJson(`${BRIDGE}/status/${job.id}.json`);
      if (!st) {
        if (job.status === "sent" && !alive && job.phase !== "Aguardando o app abrir") {
          job.phase = "Aguardando o app abrir"; changed = true;
        }
        continue;
      }
      const before = JSON.stringify([job.status, job.phase, job.progress, job.title, job.files]);
      job.status = st.status;
      job.phase = st.phase;
      job.progress = st.progress;
      job.eta = st.eta;
      job.error = st.error;
      if (st.title && st.title !== job.url) job.title = st.title;
      job.files = st.files || [];
      if (before !== JSON.stringify([job.status, job.phase, job.progress, job.title, job.files])) changed = true;
    }
    // importa o que ficou pronto
    for (const job of state.jobs) {
      if (job.status !== "done") continue;
      const pending = job.files.filter((f) => !job.imported.includes(f));
      if (!pending.length) continue;
      job.status = "importing";
      job.phase = "Importando no Premiere";
      render();
      try {
        await importJob({ ...job, files: pending });
        job.imported.push(...pending);
        job.status = "imported";
        job.phase = `Importado → ${ROOT_BIN} ▸ ${job.bin}`;
      } catch (e) {
        job.status = "import_error";
        job.error = String(e.message || e);
        job.phase = "Erro ao importar";
      }
      changed = true;
    }
    if (changed) save();
    renderConn();
    renderDest();
    if (changed) render();
  } finally {
    busy = false;
  }
}

/* ------------------------------------------------------------ tela */
function setSeg(id, attr, value) {
  document.querySelectorAll(`#${id} .opt`).forEach((o) => {
    o.className = o.getAttribute(attr) === value ? "opt active" : "opt";
  });
}
function renderControls() {
  setSeg("kind", "data-kind", state.kind);
  setSeg("vq", "data-q", state.vq);
  setSeg("aq", "data-q", state.aq);
  setSeg("dest", "data-dest", state.dest);
  $("vq").className = state.kind === "video" ? "seg mt-s" : "seg mt-s hidden";
  $("aq").className = state.kind === "sfx" ? "seg mt-s" : "seg mt-s hidden";
  $("kindNote").textContent = state.kind === "sfx"
    ? (state.aq === "wav" ? "Só o áudio em WAV, sem compressão. Ideal para SFX." : "Só o áudio em MP3.")
    : (state.vq === "max" ? "Até 4K, convertido para H.264 (mais lento)." : "Sempre H.264 + AAC, pronto para a timeline.");
  $("useRange").className = state.range ? "check mt on" : "check mt";
  $("range").className = state.range ? "range" : "range hidden";
  $("binName").textContent = binFor(state.kind, state.range);
}
function renderConn() {
  $("dot").className = state.alive ? "dot on" : "dot off";
  $("conn").textContent = state.alive ? "APP ONLINE" : "APP FECHADO";
  $("offline").className = state.alive ? "banner hidden" : "banner";
}
function shortPath(p) {
  return String(p || "").replace(/\\/g, "/").replace(/^\/Users\/[^/]+/, "~").replace(/^[A-Za-z]:\/Users\/[^/]+/, "~");
}
function renderDest() {
  let txt, note = "", pick = false;
  if (state.dest === "project") {
    txt = state.projectDir ? `${shortPath(state.projectDir)}/${ROOT_BIN}`
      : "Salve o projeto primeiro. Até lá, vai para a pasta do app.";
    note = "Uma pasta YT Downloads ao lado do arquivo do projeto.";
  } else if (state.dest === "custom") {
    pick = true;
    txt = state.customFolder ? shortPath(state.customFolder) : "Nenhuma pasta escolhida";
    note = state.customFolder ? "" : "Clique em ESCOLHER. Até lá, vai para a pasta do app.";
  } else {
    txt = state.appFolder ? shortPath(state.appFolder) : "Pasta definida no app";
    note = "A mesma pasta de SALVAR EM do app YT Downloader.";
  }
  const el = $("destPath");
  if (el.textContent !== txt) el.textContent = txt;
  if ($("destNote").textContent !== note) $("destNote").textContent = note;
  const pc = pick ? "btn sm2" : "btn sm2 hidden";
  if ($("pick").className !== pc) $("pick").className = pc;
  $("pick").textContent = state.customFolder ? "Alterar" : "Escolher";
}
function fmtEta(s) {
  if (s == null) return "";
  s = Math.round(s);
  return s >= 60 ? `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}
const QLABEL = { "1080": "1080P", "720": "720P", max: "MÁX", wav: "WAV", mp3: "MP3" };
function render() {
  const box = $("jobs");
  if (!state.jobs.length) {
    box.innerHTML = '<div class="empty">// Nenhum download ainda.</div>';
    return;
  }
  box.innerHTML = state.jobs.map((j) => {
    const active = ACTIVE.includes(j.status);
    const pct = j.progress == null ? null : Math.round(j.progress);
    let cls = "";
    if (j.status === "imported") cls = "ok";
    else if (["error", "import_error"].includes(j.status)) cls = "bad";
    const bits = [];
    if (active && pct != null && ["downloading", "processing"].includes(j.status)) bits.push(`[${pct}%]`);
    bits.push(`<span class="${cls}">${esc(String(j.phase || "").toUpperCase())}</span>`);
    if (active && j.eta != null && ["downloading", "processing"].includes(j.status)) bits.push(`ETA ${fmtEta(j.eta)}`);
    bits.push(`<span class="tag">${QLABEL[j.quality] || j.quality}${j.range ? " · TRECHO" : ""}</span>`);
    let bar = "";
    if (active || j.status === "imported") {
      const w = j.status === "imported" ? 100 : (pct == null ? 8 : Math.max(4, pct));
      bar = `<div class="pbar"><div class="pfill${j.status === "imported" ? " done" : ""}" style="width:${w}%"></div></div>`;
    }
    const err = j.error && !active ? `<div class="js bad">! ${esc(j.error)}</div>` : "";
    return `<div class="job">
      <div class="jt"><span class="jn">${String(j.n).padStart(2, "0")}</span>
        <span class="jtitle">${esc(j.title)}</span>
        <span class="jx" data-x="${j.id}" title="${active ? "Cancelar" : "Tirar da lista"}">×</span></div>
      <div class="js">${bits.join("  ")}</div>${bar}${err}
    </div>`;
  }).join("");
  box.querySelectorAll("[data-x]").forEach((el) => {
    el.addEventListener("click", () => cancelJob(el.getAttribute("data-x")));
  });
}

/* ------------------------------------------------------------ início */
async function openApp() {
  if (!shell) return;
  const r = await shell.openPath(APP_PATH, "Abrir o YT Downloader para baixar os vídeos.");
  if (r) say(IS_WIN ? "Abra o YT Downloader pelo atalho da Área de Trabalho." : "Abra o YT Downloader na pasta Aplicativos.", true);
  else say("Abrindo o YT Downloader...");
}

function bind() {
  document.querySelectorAll("#kind .opt").forEach((o) => o.addEventListener("click", () => {
    state.kind = o.getAttribute("data-kind"); save(); renderControls();
  }));
  document.querySelectorAll("#vq .opt").forEach((o) => o.addEventListener("click", () => {
    state.vq = o.getAttribute("data-q"); save(); renderControls();
  }));
  document.querySelectorAll("#aq .opt").forEach((o) => o.addEventListener("click", () => {
    state.aq = o.getAttribute("data-q"); save(); renderControls();
  }));
  document.querySelectorAll("#dest .opt").forEach((o) => o.addEventListener("click", async () => {
    const d = o.getAttribute("data-dest");
    if (d === "custom" && !state.customFolder) {
      await pickFolder();  // primeira vez: já abre o seletor
      return;
    }
    state.dest = d; save(); renderControls(); renderDest();
  }));
  $("pick").addEventListener("click", pickFolder);
  $("useRange").addEventListener("click", () => { state.range = !state.range; renderControls(); });
  $("go").addEventListener("click", submit);
  $("url").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submit(); }
  });
  $("paste").addEventListener("click", async () => {
    try {
      let t = await navigator.clipboard.readText();
      if (t && typeof t === "object") t = t["text/plain"] || "";
      if (t) { $("url").value = String(t).trim(); $("url").focus(); }
      else say("A área de transferência está vazia.", true);
    } catch (e) { say("Não consegui colar. Use ⌘V / Ctrl+V no campo.", true); }
  });
  $("openApp").addEventListener("click", openApp);
  $("clear").addEventListener("click", () => {
    state.jobs = state.jobs.filter((j) => ACTIVE.includes(j.status));
    save(); render();
  });
}

window.addEventListener("load", async () => {
  load();
  bind();
  renderControls();
  render();
  await ensureDirs();
  await refreshProject();
  await tick();
  setInterval(tick, 700);
  setInterval(refreshProject, 4000);
});
