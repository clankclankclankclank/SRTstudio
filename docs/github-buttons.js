const counters = [...document.querySelectorAll("[data-star-count]")];
const buttons = [...document.querySelectorAll(".repo-star")];
const key = "srt-studio-stars-v1";
const ttl = 15 * 60 * 1000;
const endpoint = "https://api.github.com/repos/clankclankclankclank/SRTstudio";

function showCount(count) {
  if (!Number.isSafeInteger(count) || count < 0) return;
  const short = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(count);
  const full = new Intl.NumberFormat().format(count);
  for (const node of counters) {
    node.textContent = short;
    node.hidden = false;
  }
  for (const node of buttons) {
    node.setAttribute("aria-label", "Star SRT Studio on GitHub. " + full + (count === 1 ? " star." : " stars."));
  }
}

async function refreshStarCount() {
  if (!counters.length) return;
  try {
    const cache = JSON.parse(sessionStorage.getItem(key));
    if (cache && Date.now() - cache.time < ttl && Number.isSafeInteger(cache.count) && cache.count >= 0) {
      showCount(cache.count);
      return;
    }
  } catch { /* The buttons also work when storage is unavailable. */ }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 6000);
  try {
    const response = await fetch(endpoint, { signal: controller.signal, credentials: "omit", referrerPolicy: "no-referrer" });
    if (!response.ok) return;
    const { stargazers_count: count } = await response.json();
    if (!Number.isSafeInteger(count) || count < 0) return;
    showCount(count);
    try { sessionStorage.setItem(key, JSON.stringify({ time: Date.now(), count })); }
    catch { /* Storage is optional. */ }
  } catch { /* An unavailable API never blocks the real GitHub links. */ }
  finally { clearTimeout(timeout); }
}

refreshStarCount();
