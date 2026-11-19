import { normalizeCues } from "./transcript.mjs";
import { audioEnvelope, filterQuiet, wordCues } from "./cue-words.mjs";

const SOURCES = Object.freeze({
  quick: { hub: "Xenova/whisper-base", dtype: "q8", alignment: false },
  accurate: { hub: "Xenova/whisper-small", dtype: "q8", alignment: true },
  english: { hub: "Xenova/whisper-small.en", dtype: "q8", alignment: true },
  max: { hub: "onnx-community/whisper-large-v3-turbo", dtype: "q4f16", alignment: true, gpuOnly: true },
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
  const pack = SOURCES[model];
  if (loaded?.model === model && loaded.device === device) return loaded.pipe;
  await closePipeline();
  publish(id, { type: "load", device, model });
  let lastReport = 0;
  const files = new Map();
  const { pipeline } = await getLibrary();
  const pipe = await pipeline("automatic-speech-recognition", pack.hub, {
    device,
    dtype: pack.dtype,
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
        publish(id, { type: "download", read, total, device, model });
      }
    }
  });
  loaded = { model, device, pipe };
  return pipe;
}

async function transcribe(job, device, envelope) {
  const { id, model, language, pcm } = job;
  const pack = SOURCES[model];
  const pipe = await loadModel(id, model, device);
  publish(id, { type: "transcribe", device, model });
  // Prefer whole words with real alignment so SRT cues can be split at
  // punctuation and pauses instead of one long Whisper segment.
  const options = {
    chunk_length_s: 30,
    stride_length_s: 5,
    return_timestamps: pack.alignment ? "word" : true,
    ...(model === "english" ? {} : { task: "transcribe", ...(language ? { language } : {}) }),
  };
  let out;
  try {
    out = await pipe(pcm, options);
  } catch (err) {
    // A missing ONNX attention output should not block an entire export.
    if (!pack.alignment || !/align|attention|timestamp|past_key|shape.*rank/iu.test(messageOf(err))) throw err;
    options.return_timestamps = true;
    publish(id, { type: "segment-only" });
    out = await pipe(pcm, options);
  }
  const duration = pcm.length / SAMPLE_RATE;
  const cues = options.return_timestamps === "word" ?
    wordCues(out.chunks, duration) : normalizeCues(out.chunks, duration);
  return filterQuiet(cues, envelope);
}

async function preferredDevice(preference, gpuOnly) {
  if (preference === "wasm" || (!gpuOnly && failedGpu) || !navigator.gpu) return "wasm";
  if (!gpuOnly && loaded?.device === "webgpu") return "webgpu";
  try {
    const adapter = await navigator.gpu.requestAdapter({ powerPreference: "high-performance" });
    if (!adapter || (gpuOnly && !adapter.features.has("shader-f16"))) return "wasm";
    return "webgpu";
  } catch { return "wasm"; }
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
    const envelope = audioEnvelope(job.pcm);
    if (!envelope.audible) {
      publish(id, { type: "done", cues: [], model: job.model, device: "wasm" });
      return;
    }
    const pack = SOURCES[job.model];
    let device = await preferredDevice(job.device, pack.gpuOnly), cues;
    if (pack.gpuOnly && device !== "webgpu") {
      throw new Error("Max quality needs WebGPU with FP16. Select Accurate to keep transcribing on this device.");
    }
    try {
      cues = await transcribe(job, device, envelope);
    } catch (err) {
      if (device !== "webgpu") throw err;
      await closePipeline();
      if (pack.gpuOnly) {
        throw new Error("Max quality could not run on this GPU. Select Accurate or try another browser. " + messageOf(err));
      }
      failedGpu = true;
      device = "wasm";
      publish(id, { type: "fallback", message: messageOf(err), model: job.model });
      cues = await transcribe(job, device, envelope);
    }
    publish(id, { type: "done", cues, device, model: job.model });
  } catch (err) {
    publish(id, { type: "error", message: messageOf(err) });
  } finally {
    busy = false;
  }
};
