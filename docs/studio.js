"use strict";

const segments = [
  ["00:00.0 → 00:03.1", "The city sounds different before sunrise.", "00:00:01.550", 14.76],
  ["00:03.1 → 00:06.7", "Even the busiest streets go quiet.", "00:00:04.900", 46.67],
  ["00:06.7 → 00:10.5", "For a few minutes, the whole place feels like yours.", "00:00:08.600", 81.90],
];
const byId = (id) => document.getElementById(id);
const marks = [[.11,.04,.53],[.21,.056,.74],[.34,.04,.75],[.44,.04,.53],[.55,.05,.57],[.70,.037,.73],[.80,.038,.58],[.9,.054,.71]];
let paths = "";
for (let i = 0; i < 117; i++) {
  const p = i / 116;
  const envelope = marks.reduce((sum, [center, width, height]) => sum + height * Math.exp(-(((p-center)/width)**2)), 0);
  const amp = 1 + 52 * envelope * (.19 + .81 * Math.abs(Math.cos(i * .92) * Math.sin(i * 1.77)));
  paths += "M" + (8 + p * 484).toFixed(1) + " " + (57 - amp).toFixed(1) + "v" + (2 * amp).toFixed(1);
}
byId("signal-bars").setAttribute("d", paths);
const buttons = [...document.querySelectorAll("[data-cue]")];
buttons.forEach((button) => button.addEventListener("click", () => {
  const [time, line, cursorTime, position] = segments[Number(button.dataset.cue)];
  buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
  byId("cue-time").textContent = time;
  byId("cue-label").textContent = line;
  byId("timeline").textContent = cursorTime;
  byId("cursor").style.left = position + "%";
}));
const copy = byId("copy-setup");
if (navigator.clipboard?.writeText) {
  copy.hidden = false;
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(byId("setup-commands").textContent);
      copy.textContent = "Copied";
    } catch {
      copy.textContent = "Select commands";
    }
  };
}
