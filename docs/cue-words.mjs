import { normalizeCues } from "./transcript.mjs";

const terminal = /[.!?。！？…]["'”’）)]?$/u;
const clause = /[,;:，、；：]["'”’）)]?$/u;
const closePunctuation = /^[,.;:!?)%\]}’”、。，；：！？…）]/u;
const compactScript = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Thai}\p{Script=Lao}]/u;
const width = (text) => Array.from(text).length;

function glue(previous, raw) {
  const next = raw.trim();
  if (!previous) return next;
  if (closePunctuation.test(next) || /[(\[“‘（]$/u.test(previous)) return previous + next;
  if (/^\s/u.test(raw)) return previous + " " + next;
  const last = Array.from(previous).at(-1);
  const first = Array.from(next)[0];
  return previous + (compactScript.test(last) || compactScript.test(first) ? "" : " ") + next;
}

// A deliberately low threshold for true quiet. This is not a voice-activity
// detector and never rejects a quiet-looking cue if the timing looks invalid.
const FRAME = 320;
const QUIET_POWER = .0012 ** 2;
const MIN_AUDIBLE_FRAMES = 2;

export function audioEnvelope(pcm) {
  const frames = new Uint8Array(Math.ceil(pcm.length / FRAME));
  let audibleFrames = 0;
  for (let f = 0; f < frames.length; f++) {
    const start = f * FRAME, end = Math.min(start + FRAME, pcm.length);
    let power = 0;
    for (let i = start; i < end; i++) power += pcm[i] * pcm[i];
    if (power / (end - start) >= QUIET_POWER) {
      audibleFrames++;
      frames[f] = 1;
    }
  }
  return { frames, audible: audibleFrames >= MIN_AUDIBLE_FRAMES };
}

export function filterQuiet(cues, envelope, rate = 16000) {
  if (!envelope) return cues;
  return cues.filter(({ start, end }) => {
    if (end - start < .09) return true;
    const a = Math.max(0, Math.floor((start - .12) * rate / FRAME));
    const b = Math.min(envelope.frames.length, Math.ceil((end + .12) * rate / FRAME));
    for (let f = a; f < b; f++) if (envelope.frames[f]) return true;
    return false;
  });
}

export function wordCues(chunks, duration) {
  if (!Array.isArray(chunks)) return [];
  const words = chunks.filter((chunk) => typeof chunk?.text === "string" && chunk.text.trim());
  if (words.length < 2) return normalizeCues(words, duration);
  // Preserve the decoder's text even when alignment returns null start and end
  // timestamps. Group by recognizer-provided word boundaries, never guessed time.
  const groups = [];
  let used = [];
  let text = "";
  const emit = () => {
    if (!used.length) return;
    const first = used.find((w) => Number.isFinite(w.timestamp?.[0]));
    const last = [...used].reverse().find((w) => Number.isFinite(w.timestamp?.[1]));
    groups.push({ text, timestamp: [first?.timestamp[0], last?.timestamp[1]] });
    used = [];
    text = "";
  };
  for (const word of words) {
    const start = word.timestamp?.[0], priorEnd = used.at(-1)?.timestamp?.[1];
    const startOfLine = used[0]?.timestamp?.[0], end = word.timestamp?.[1];
    const next = glue(text, word.text);
    const pause = Number.isFinite(start) && Number.isFinite(priorEnd) && start - priorEnd > .55;
    const longTime = Number.isFinite(end) && Number.isFinite(startOfLine) && end - startOfLine > 5;
    if (used.length && (pause || width(next) > 43 || longTime || terminal.test(text) ||
      (clause.test(text) && width(text) >= 27))) {
      emit();
    }
    text = glue(text, word.text);
    used.push(word);
  }
  emit();
  return normalizeCues(groups, duration);
}
