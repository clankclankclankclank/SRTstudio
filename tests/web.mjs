import test from "node:test";
import assert from "node:assert/strict";
import { normalizeCues, stamp, serialize, exportStem, uniqueSrtFiles, makeZip } from "../docs/transcript.mjs";

test("Whisper timestamps are clamped, ordered, and closed at media duration", () => {
  const cues = normalizeCues([
    { timestamp: [-1, 1.234], text: "  Start     here " },
    { timestamp: [1, null], text: " Second" },
    { timestamp: [3.456, null], text: " Last" },
    { timestamp: [9, 10], text: "end" },
  ], 7.123);
  assert.deepEqual(cues, [
    { start: 0, end: 1.234, text: "Start here" },
    { start: 1.234, end: 3.456, text: "Second" },
    { start: 3.456, end: 7.123, text: "Last end" },
  ]);
  assert.deepEqual(normalizeCues([], 10), []);
  assert.equal(stamp(3601.002), "01:00:01.002");
});

test("subtitle formats preserve timing, Unicode and safe caption text", () => {
  const cues = [{ start: 1.01, end: 2.405, text: "喂 <left> & right" }];
  assert.equal(serialize(cues, "srt"), "1\n00:00:01,010 --> 00:00:02,405\n喂 &lt;left&gt; &amp; right\n\n");
  assert.equal(serialize(cues, "vtt"), "WEBVTT\n\n00:00:01.010 --> 00:00:02.405\n喂 &lt;left&gt; &amp; right\n\n");
  assert.equal(serialize(cues, "txt"), "喂 <left> & right\n");
});

test("batch ZIP has unique Unicode paths and valid end record", async () => {
  const cues = [{ start: 0, end: 1, text: "é worked" }];
  const files = uniqueSrtFiles(["café.mp3","Café.wav","AUX.wav","<clip>.wav"].map(name=>({name,cues})));
  assert.deepEqual(files.map(f=>f.name), ["café.srt","Café (2).srt","_AUX.srt","_clip_.srt"]);
  assert.equal(exportStem(".ogg"), "subtitles");
  const data = await makeZip(files).arrayBuffer();
  const view = new DataView(data);
  assert.equal(view.getUint32(0, true), 0x04034b50);
  const end = data.byteLength - 22;
  assert.equal(view.getUint32(end, true), 0x06054b50);
  assert.equal(view.getUint16(end + 10, true), files.length);
  assert.equal(view.getUint32(view.getUint32(end + 16, true), true), 0x02014b50);
});

test("word alignment creates separate timed cues at sentence ends, pauses and line limits", async () => {
  const { wordCues } = await import("../docs/cue-words.mjs");
  const words = [
    { timestamp: [0.1, 0.31], text: " Welcome" },
    { timestamp: [0.31, .75], text: " back." },
    { timestamp: [.82, 1.03], text: " Here" },
    { timestamp: [1.03, 1.41], text: " we go" },
    { timestamp: [2.15, 2.4], text: " again" },
    { timestamp: [2.4, 2.41], text: "!" },
  ];
  assert.deepEqual(wordCues(words, 3), [
    { start: .1, end: .75, text: "Welcome back." },
    { start: .82, end: 1.41, text: "Here we go" },
    { start: 2.15, end: 2.41, text: "again!" },
  ]);
  const many = "The studio model should split this long sentence into readable phrases without losing words".split(" ");
  const result = wordCues(many.map((text, i) => ({ text, timestamp: [i*.29, (i+1)*.29] })), 15);
  assert.ok(result.length > 1);
  assert.ok(result.every((cue) => cue.text.length <= 43));
  assert.equal(result.map((cue) => cue.text).join(" "), many.join(" "));
  const cjk = [{ text: "你好", timestamp:[0,.4]}, {text:"世界",timestamp:[.4,.8]}, {text:"。",timestamp:[.8,.9]}];
  assert.equal(wordCues(cjk, 1)[0].text, "你好世界。");
});

test("digital silence is omitted but quiet speech and timing are preserved", async () => {
  const { audioEnvelope, filterQuiet } = await import("../docs/cue-words.mjs");
  const silence = new Float32Array(16000 * 3);
  assert.equal(audioEnvelope(silence).audible, false);
  const audio = silence.slice();
  audio.fill(.004, 16000, 20000);
  const env = audioEnvelope(audio);
  assert.equal(env.audible, true);
  const good = { start:.94, end:1.37, text:"Quiet words" };
  assert.deepEqual(filterQuiet([{ start:0, end:.6, text:"made up" },good,
    { start:2, end:2.7, text:"more hallucination" }], env), [good]);
});
