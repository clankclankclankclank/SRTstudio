import { normalizeCues } from "./transcript.mjs";

const SOURCES = Object.freeze({
  tiny: "Xenova/whisper-tiny",
  base: "Xenova/whisper-base",
});
const LIBRARY = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.8.1";
const SAMPLE_RATE = 16000;
const publish = (id, payload) => self.postMessage({ id, ...payload });
const messageOf = (err) => err?.message || String(err);
let library, loaded, failedGpu = false, busy = false;

async function getLibrary() {
  if (library) return library;
  const lib = await import(LIBRARY);
  lib.env.allowLocalModels = false;
  lib.env.useBrowserCache = true;
  // GitHub Pages does not provide COOP/COEP headers for SharedArrayBuffer.
  if (lib.env.backends.onnx.wasm) {
    lib.env.backends.onnx.wasm.numThreads = self.crossOriginIsolated ?
      Math.min(navigator.hardwareConcurrency || 2, 4) : 1;
    lib.env.backends.onnx.wasm.proxy = false;
  }
  library = lib;
  return lib;
}

async function closePipeline() {
  const last = loaded;
  loaded = undefined;
  if (last) {
    try { await last.pipe.dispose(); } catch { /* runtime has already released the model */ }
  }
}

async function loadModel(id, model, device) {
  if (loaded?.model === model && loaded.device === device) return loaded.pipe;
  await closePipeline();
  publish(id, { type: "load", device });
  let lastReport = 0;
  const files = new Map();
  const { pipeline } = await getLibrary();
  const pipe = await pipeline("automatic-speech-recognition", SOURCES[model], {
    device,
    dtype: "q8",
    progress_callback(event) {
      const file = event.file || "";
      if (event.status === "progress" && event.total > 0) {
        files.set(file, [Math.min(event.loaded, event.total), event.total]);
      } else if (event.status === "done" && files.has(file)) {
        const [, total] = files.get(file);
        files.set(file, [total, total]);
      }
      const now = Date.now();
      if (now - lastReport < 100) return;
      lastReport = now;
      if (event.status === "progress" && files.size) {
        const [read, total] = [...files.values()].reduce(([a,b],[x,y]) => [a+x,b+y], [0,0]);
        publish(id, { type: "download", read, total, device });
      }
    }
  });
  loaded = { model, device, pipe };
  return pipe;
}

async function transcribe(job, device) {
  const { id, model, language, pcm } = job;
  const pipe = await loadModel(id, model, device);
  publish(id, { type: "transcribe", device });
  const out = await pipe(pcm, {
    chunk_length_s: 30,
    stride_length_s: 5,
    return_timestamps: true,
    task: "transcribe",
    ...(language ? { language } : {}),
  });
  return normalizeCues(out.chunks, pcm.length / SAMPLE_RATE);
}

async function preferredDevice(preference) {
  if (preference === "wasm" || failedGpu || !navigator.gpu) return "wasm";
  if (loaded?.device === "webgpu") return "webgpu";
  try { return await navigator.gpu.requestAdapter() ? "webgpu" : "wasm"; }
  catch { return "wasm"; }
}

self.onmessage = async ({ data: job }) => {
  if (busy || job.type !== "run") return;
  const { id } = job;
  if (!Object.hasOwn(SOURCES, job.model) || !(job.pcm instanceof Float32Array) || !job.pcm.length) {
    publish(id, { type: "error", message: "Invalid audio or model." });
    return;
  }
  busy = true;
  try {
    let device = await preferredDevice(job.device), cues;
    try {
      cues = await transcribe(job, device);
    } catch (err) {
      if (device !== "webgpu") throw err;
      failedGpu = true;
      await closePipeline();
      device = "wasm";
      publish(id, { type: "fallback", message: messageOf(err) });
      cues = await transcribe(job, device);
    }
    publish(id, { type: "done", cues, device });
  } catch (err) {
    publish(id, { type: "error", message: messageOf(err) });
  } finally {
    busy = false;
  }
};
