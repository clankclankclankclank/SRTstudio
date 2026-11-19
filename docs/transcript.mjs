const millis = (seconds) => Math.max(0, Math.round((Number.isFinite(seconds) ? seconds : 0) * 1000));
const pad = (n, w = 2) => String(n).padStart(w, "0");
const textOf = (s) => String(s ?? "").replace(/\s+/gu, " ").trim();

export function stamp(seconds, sep = ".") {
  const ms = millis(seconds);
  return [pad(Math.floor(ms / 3600000)), pad(Math.floor(ms / 60000) % 60),
    pad(Math.floor(ms / 1000) % 60)].join(":") + sep + pad(ms % 1000, 3);
}

export function normalizeCues(chunks, duration) {
  const length = millis(duration);
  if (!length || !Array.isArray(chunks)) return [];
  const cues = [];
  let previousEnd = 0;
  for (let i = 0; i < chunks.length; i++) {
    const chunk = chunks[i], text = textOf(chunk?.text);
    if (!text) continue;
    const [rawStart, rawEnd] = chunk?.timestamp || [];
    const nextStart = chunks[i + 1]?.timestamp?.[0];
    const start = Math.max(previousEnd, Math.min(length, Number.isFinite(rawStart) ? millis(rawStart) : previousEnd));
    const requestedEnd = Number.isFinite(rawEnd) ? millis(rawEnd) :
      Number.isFinite(nextStart) ? millis(nextStart) : length;
    const end = Math.min(length, Math.max(start + 1, requestedEnd));
    if (end <= start) {
      if (cues.length) cues.at(-1).text += " " + text;
      continue;
    }
    cues.push({ start: start / 1000, end: end / 1000, text });
    previousEnd = end;
  }
  return cues;
}

const escapeCue = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export function serialize(cues, format) {
  if (format === "txt") return cues.map((c) => c.text).join("\n") + "\n";
  if (format !== "srt" && format !== "vtt") throw new Error("Unknown subtitle format.");
  const sep = format === "srt" ? "," : ".";
  const blocks = cues.map((cue, i) =>
    (format === "srt" ? (i + 1) + "\n" : "") +
    stamp(cue.start, sep) + " --> " + stamp(cue.end, sep) + "\n" + escapeCue(cue.text)
  );
  return (format === "vtt" ? "WEBVTT\n\n" : "") + blocks.join("\n\n") + "\n\n";
}

export function exportStem(filename) {
  let s = String(filename).replace(/\.[^.]*$/u, "").replace(/[<>:"/\\|?*\x00-\x1f]/gu, "_").trim();
  s = Array.from(s).slice(0, 120).join("").replace(/[. ]+$/u, "");
  if (/^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/iu.test(s)) s = "_" + s;
  return s || "subtitles";
}

export function uniqueSrtFiles(results) {
  const used = new Set();
  return results.map(({ name, cues }) => {
    const stem = exportStem(name);
    let id = stem, n = 2;
    while (used.has(id.toLowerCase())) id = stem + " (" + n++ + ")";
    used.add(id.toLowerCase());
    return { name: id + ".srt", text: serialize(cues, "srt") };
  });
}

const CRC_TABLE = Uint32Array.from({ length: 256 }, (_, i) => {
  for (let k = 0; k < 8; k++) i = (i & 1) ? (0xedb88320 ^ (i >>> 1)) : (i >>> 1);
  return i >>> 0;
});
const crc32 = (data) => {
  let crc = 0xffffffff;
  for (const b of data) crc = CRC_TABLE[(crc ^ b) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
};

export function makeZip(entries) {
  const enc = new TextEncoder();
  const locals = [], central = [];
  let offset = 0, centralLength = 0;
  for (const { name, text } of entries) {
    const path = enc.encode(name), data = enc.encode(text), crc = crc32(data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);
    local.setUint16(6, 0x0800, true);
    local.setUint16(12, 33, true); // DOS date: 1980-01-01
    local.setUint32(14, crc, true);
    local.setUint32(18, data.length, true);
    local.setUint32(22, data.length, true);
    local.setUint16(26, path.length, true);
    locals.push(local.buffer, path, data);
    const cd = new DataView(new ArrayBuffer(46));
    cd.setUint32(0, 0x02014b50, true);
    cd.setUint16(4, 20, true);
    cd.setUint16(6, 20, true);
    cd.setUint16(8, 0x0800, true);
    cd.setUint16(14, 33, true);
    cd.setUint32(16, crc, true);
    cd.setUint32(20, data.length, true);
    cd.setUint32(24, data.length, true);
    cd.setUint16(28, path.length, true);
    cd.setUint32(42, offset, true);
    central.push(cd.buffer, path);
    centralLength += 46 + path.length;
    offset += 30 + path.length + data.length;
  }
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, centralLength, true);
  end.setUint32(16, offset, true);
  return new Blob([...locals, ...central, end.buffer], { type: "application/zip" });
}
