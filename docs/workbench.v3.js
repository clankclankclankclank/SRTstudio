import { exportStem, makeZip, serialize, stamp, uniqueSrtFiles } from "./transcript.mjs";

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  const use = document.createElementNS(SVG_NS, "use");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}
const RATE = 16000, LIMIT = 150 * 1024 * 1024, MAX_SECONDS = 25 * 60;
const ext = /\.(?:wav|mp3|m4a|aac|flac|ogg|opus|webm|mp4|aif|aiff|caf)$/iu;
const AudioDecoder = window.OfflineAudioContext || window.webkitOfflineAudioContext;
const supported = Boolean(AudioDecoder && window.Worker && window.WebAssembly);
const queue = [];
let serial = 0, active, worker, pending, current, previewUrl, previewCues;
let addingSample = false;
const drop = $("drop-zone"), audio = $("audio");
const output = $("output-file");

const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const mb = (bytes) => (bytes / (1024 * 1024)).toFixed(1) + " MB";
const deviceName = (device) => device === "webgpu" ? "WebGPU" : "CPU";
const computeOptions = () => {
  const language = $("language").value;
  const choice = $("model").value;
  const model = choice === "accurate" && language === "en" ? "english" : choice;
  return { model, language, device: $("device").value, settingsKey: model + ":" + language };
};
const modeName = (model) => ({ accurate: "Accurate", english: "Accurate English", quick: "Quick", max: "Max" })[model] || "";
const modelHint = () => ({
  quick: "Whisper Base · first download about 80 MB.",
  accurate: "Whisper Small · first download about 255 MB.",
  english: "Whisper Small English · first download about 255 MB.",
  max: "Whisper Turbo · about 570 MB. Requires WebGPU with FP16."
})[computeOptions().model];
const isCancelled = () => active?.cancelled;
const cancellation = () => new DOMException("Batch stopped.", "AbortError");
const statuses = { queued: "Queued", active: "Running", done: "Done", failed: "Failed" };

function say(title, detail, value) {
  if ($("status").textContent !== title) $("status").textContent = title;
  $("detail").textContent = detail;
  const p = $("progress");
  p.hidden = value === undefined;
  if (value === null) p.removeAttribute("value");
  else if (value !== undefined) { p.max = 1; p.value = Math.max(0, Math.min(1, value)); }
}

function notice(text) {
  $("notice").textContent = text || "";
  $("notice").hidden = !text;
}

function drawWave() {
  const marks = [[.17,.09,.56],[.34,.048,1.05],[.45,.07,.65],[.61,.055,.92],[.76,.07,.58],[.87,.04,.35]];
  let path = "";
  for (let i = 0; i <= 100; i++) {
    const u = i / 100;
    const sum = marks.reduce((n, [at, width, gain]) => n + gain * Math.exp(-(((u - at) / width) ** 2)), 0);
    const amp = 1 + 39 * sum * (.15 + .85 * Math.abs(Math.sin(i * 1.09) * Math.cos(i * 1.73)));
    path += "M" + 4 * i + " " + (50 - amp).toFixed(1) + "v" + (amp * 2).toFixed(1);
  }
  $("signal-bars").setAttribute("d", path);
}
drawWave();

function cleanupPreview() {
  audio.pause();
  audio.removeAttribute("src");
  if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = undefined; }
}

function renderCaptions(selected) {
  if (current === selected?.id && previewCues === selected?.cues) return;
  cleanupPreview();
  current = selected?.id;
  previewCues = selected?.cues;
  $("captions").replaceChildren();
  if (!selected) return;
  previewUrl = URL.createObjectURL(selected.file);
  audio.src = previewUrl;
  const { cues } = selected;
  $("cue-count").textContent = cues.length + (cues.length === 1 ? " CUE" : " CUES");
  $("captions").replaceChildren(...cues.map((cue) => {
    const at = element("button", "button ghost", stamp(cue.start).replace(/^00:/, ""));
    at.type = "button";
    at.setAttribute("aria-label", "Seek to " + stamp(cue.start));
    at.onclick = () => {
      if (!Number.isFinite(audio.duration)) {
        const src = audio.src;
        audio.addEventListener("loadedmetadata", () => {
          if (audio.src === src) audio.currentTime = cue.start;
        }, { once: true });
        return;
      }
      audio.currentTime = cue.start;
    };
    const line = element("div", "caption");
    line.append(at, element("span", "", cue.text));
    return line;
  }));
}

function render(preferOutput) {
  const locked = Boolean(active);
  $("counter").textContent = queue.length;
  $("browse").disabled = locked || !supported;
  $("sample").disabled = locked || addingSample || !supported;
  $("clear-queue").disabled = locked || !queue.length;
  const completed = Boolean(queue.length && queue.every((item) => item.status === "done"));
  const key = computeOptions().settingsKey;
  const changed = completed && queue.some((item) => item.settingsKey !== key);
  $("start").disabled = locked || !supported || !queue.length;
  $("start-label").textContent = locked ? "Transcribing" : changed ? "Transcribe again" : completed ? "Add more files" : "Transcribe";
  $("cancel").hidden = !locked;
  $("cancel").disabled = Boolean(active?.cancelled);
  ["model", "language", "device"].forEach((id) => { $(id).disabled = locked; });

  $("queue").replaceChildren(...queue.map((item) => {
    const li = element("li");
    const title = element("div", "file-title");
    title.append(element("strong", "", item.file.name),
      element("small", "", item.note || mb(item.file.size)));
    const remove = element("button", "button ghost remove");
    remove.append(icon("x"));
    remove.type = "button";
    remove.disabled = locked;
    remove.setAttribute("aria-label", "Remove " + item.file.name);
    remove.onclick = () => {
      if (active) return;
      const i = queue.indexOf(item);
      if (i >= 0) queue.splice(i, 1);
      render();
    };
    li.append(icon("wave"), title, element("span", "badge " + item.status, statuses[item.status]), remove);
    return li;
  }));

  const done = queue.filter((item) => item.cues?.length);
  $("result-empty").hidden = Boolean(done.length);
  $("result-content").hidden = !done.length;
  $("zip").hidden = done.length < 2;
  const selected = done.find((item) => item.id === preferOutput) ||
    done.find((item) => item.id === current) || done[0];
  output.replaceChildren(...done.map((item) => new Option(item.file.name, item.id)));
  if (selected) output.value = selected.id;
  renderCaptions(selected);
}
output.onchange = () => renderCaptions(queue.find((i) => i.id === Number(output.value)));

function addFiles(files) {
  if (active || !supported) return 0;
  let added = 0;
  const messages = [];
  const keys = new Set(queue.map((i) => i.key));
  for (const file of files) {
    const key = JSON.stringify([file.name, file.size, file.lastModified]);
    if (keys.has(key)) continue;
    if (!file.size || !(file.type.startsWith("audio/") || ext.test(file.name))) {
      messages.push(file.name + " is not a supported audio file.");
    } else if (file.size > LIMIT) {
      messages.push(file.name + " exceeds 150 MB. Use the desktop edition for large files.");
    } else if (queue.length >= 32) {
      messages.push("The browser queue holds up to 32 files.");
      break;
    } else {
      keys.add(key);
      queue.push({ id: ++serial, key, file, status: "queued" });
      added++;
    }
  }
  notice(messages.length ? messages[0] + (messages.length > 1 ? " +" + (messages.length - 1) + " more." : "") : "");
  if (added) say("Ready to transcribe", modelHint());
  render();
  return added;
}

$("browse").onclick = () => $("media-files").click();
$("media-files").onchange = (e) => {
  addFiles(e.target.files);
  e.target.value = "";
};
$("clear-queue").onclick = () => {
  if (active) return;
  queue.length = 0;
  notice("");
  say("Ready to transcribe", modelHint());
  render();
};

for (const id of ["model", "language", "device"]) {
  $(id).addEventListener("change", () => {
    if (!active) {
      notice("");
      say("Ready to transcribe", modelHint());
      render();
    }
  });
}

const isFileDrag = (e) => Array.from(e.dataTransfer?.types || []).includes("Files");
const insideDrop = (target) => target instanceof Node && drop.contains(target);
window.addEventListener("dragover", (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  const target = !active && supported && insideDrop(e.target);
  e.dataTransfer.dropEffect = target ? "copy" : "none";
  drop.classList.toggle("is-drag", target);
});
drop.addEventListener("dragleave", (e) => {
  if (!insideDrop(e.relatedTarget)) drop.classList.remove("is-drag");
});
window.addEventListener("drop", (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  drop.classList.remove("is-drag");
  if (insideDrop(e.target)) addFiles(e.dataTransfer.files);
});

$("sample").onclick = async () => {
  if (addingSample || active) return;
  addingSample = true;
  render();
  try {
    const r = await fetch(new URL("./assets/sample.mp3", import.meta.url));
    if (!r.ok) throw new Error("Sample audio is unavailable. You can still add your own files.");
    const file = new File([await r.blob()], "srt-studio-sample.mp3", { type: "audio/mpeg", lastModified: 0 });
    const added = addFiles([file]);
    if (!added && !active && queue.some((item) => item.file.name === file.name && item.file.lastModified === 0)) {
      say("Sample already added", "Remove it from the queue to run it again.");
    }
  } catch (err) { notice(err.message); }
  finally { addingSample = false; render(); }
};

async function decode(file) {
  const ctx = new AudioDecoder(1, 1, RATE);
  let decoded;
  try { decoded = await ctx.decodeAudioData(await file.arrayBuffer()); }
  catch { throw new Error("Your browser could not decode this file. Try WAV or MP3."); }
  if (decoded.duration > MAX_SECONDS) throw new Error("This file exceeds 25 minutes. Use the desktop edition for longer audio.");
  if (!decoded.length) throw new Error("No audio samples were found.");
  const pcm = new Float32Array(decoded.length);
  for (let ch = 0; ch < decoded.numberOfChannels; ch++) {
    const data = decoded.getChannelData(ch);
    if (decoded.numberOfChannels === 1) pcm.set(data);
    else for (let i = 0; i < pcm.length; i++) pcm[i] += data[i] / decoded.numberOfChannels;
  }
  return pcm;
}

function terminateWorker(reason) {
  worker?.terminate();
  worker = undefined;
  const job = pending;
  pending = undefined;
  job?.reject(reason);
}

function createWorker() {
  const w = new Worker(new URL("./engine.v3.mjs", import.meta.url), { type: "module" });
  w.onmessage = ({ data }) => {
    if (w !== worker || !pending || pending.id !== data.id) return;
    if (data.type === "done" || data.type === "error") {
      const job = pending;
      pending = undefined;
      if (data.type === "done") job.resolve(data);
      else job.reject(new Error(data.message));
    } else pending.report(data);
  };
  w.onerror = (event) => {
    if (w === worker) {
      event.preventDefault();
      terminateWorker(new Error("The browser could not start Whisper. Reload the page and try CPU compatibility mode."));
    }
  };
  w.onmessageerror = () => {
    if (w === worker) terminateWorker(new Error("The Whisper worker returned unreadable data."));
  };
  return w;
}

function askWorker(id, pcm, options, report) {
  if (!worker) worker = createWorker();
  return new Promise((resolve, reject) => {
    pending = { id, report, resolve, reject };
    try { worker.postMessage({ type: "run", id, pcm, ...options }, [pcm.buffer]); }
    catch (err) { pending = undefined; reject(err); }
  });
}

$("cancel").onclick = () => {
  if (!active || active.cancelled) return;
  active.cancelled = true;
  terminateWorker(cancellation());
  say("Stopping batch", "Completed captions are kept.");
  render();
};

function cleanEngineError(error) {
  const raw = error?.message || "Transcription failed.";
  if (/fetch|network|load failed|could not locate|http status/iu.test(raw)) {
    return "Could not download the Whisper model or runtime. Check your connection, then retry.";
  }
  return raw.length > 230 ? raw.slice(0, 230) + "…" : raw;
}

$("start").onclick = async () => {
  if (active || !supported) return;
  const options = computeOptions();
  const batch = queue.filter((item) => item.status !== "done" || item.settingsKey !== options.settingsKey);
  if (!batch.length) {
    if (queue.length) $("media-files").click();
    return;
  }
  if (options.model === "max" && options.device === "wasm") {
    notice("Max quality requires WebGPU. Change Compute to Auto or select Accurate.");
    return;
  }
  active = { cancelled: false };
  notice("");
  render();
  let done = 0, failed = 0;
  try {
    for (const [idx, item] of batch.entries()) {
      if (isCancelled()) break;
      const position = (idx + 1) + " / " + batch.length;
      let decoded = false, infer = false;
      const before = { status: item.status, note: item.note };
      item.status = "active";
      item.note = "Decoding audio";
      render();
      say("Reading " + position, item.file.name, idx / batch.length);
      try {
        const pcm = await decode(item.file);
        if (isCancelled()) throw cancellation();
        decoded = true;
        item.note = "Preparing Whisper";
        render();
        say("Preparing Whisper", item.file.name, null);
        const result = await askWorker(item.id, pcm, options, (ev) => {
          if (isCancelled()) return;
          if (ev.type === "download") say("Downloading " + modeName(ev.model) + " model", mb(ev.read) + " / " + mb(ev.total) + " · first run", ev.read / ev.total);
          if (ev.type === "load") say("Preparing " + modeName(ev.model) + " · " + deviceName(ev.device), modelHint(), null);
          if (ev.type === "fallback") {
            infer = false;
            item.note = "Switching to CPU";
            render();
            say("Switching to CPU", "WebGPU could not run this model.", null);
          }
          if (ev.type === "segment-only") say("Reading segment timestamps " + position, item.file.name, null);
          if (ev.type === "transcribe") {
            infer = true;
            item.note = "Transcribing · " + deviceName(ev.device);
            render();
            say("Transcribing " + position + " · " + deviceName(ev.device), item.file.name, null);
          }
        });
        if (isCancelled()) throw cancellation();
        infer = true;
        if (!result.cues.length) throw new Error("No timestamped speech detected.");
        item.settingsKey = options.settingsKey;
        item.cues = result.cues;
        item.status = "done";
        item.note = result.cues.length + (result.cues.length === 1 ? " cue" : " cues") + " · " + modeName(result.model) + " · " + deviceName(result.device);
        done++;
        render(done === 1 && current === undefined ? item.id : undefined);
        say("Finished " + position, item.file.name, (idx + 1) / batch.length);
      } catch (err) {
        if (isCancelled() || err.name === "AbortError") {
          item.status = before.status === "done" ? "done" : "queued";
          item.note = before.status === "done" ? before.note : "";
          break;
        }
        item.status = "failed";
        item.note = cleanEngineError(err);
        failed++;
        if (decoded && !infer) {
          notice(item.note);
          break;
        }
      }
      render();
    }
  } finally {
    const stopped = isCancelled();
    active = undefined;
    say(stopped ? "Batch stopped" : failed ? "Batch finished with errors" : "Batch complete",
      done + " transcribed" + (failed ? " · " + failed + " failed" : "") +
      (batch.length > done + failed ? " · " + (batch.length - done - failed) + " waiting" : ""));
    if (failed) {
      const error = batch.find((item) => item.status === "failed");
      if (error) notice(error.file.name + ": " + error.note);
    }
    render();
  }
};

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = element("a");
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 20000);
}

document.querySelectorAll("[data-format]").forEach((btn) => {
  btn.onclick = () => {
    const item = queue.find((i) => i.id === current);
    if (!item?.cues) return;
    const format = btn.dataset.format;
    download(new Blob([serialize(item.cues, format)], {
      type: (format === "vtt" ? "text/vtt" : "text/plain") + ";charset=utf-8"
    }), exportStem(item.file.name) + "." + format);
  };
});
$("zip").onclick = () => {
  const entries = uniqueSrtFiles(queue.filter((i) => i.cues?.length)
    .map((item) => ({ name: item.file.name, cues: item.cues })));
  if (entries.length) download(makeZip(entries), "srt-studio-batch.zip");
};

if (!supported) {
  notice("This browser does not support the audio tools SRT Studio needs. Try current Chrome or Edge, or use the desktop edition.");
}
render();
document.documentElement.dataset.ready = "true";
$("boot-notice").hidden = true;
