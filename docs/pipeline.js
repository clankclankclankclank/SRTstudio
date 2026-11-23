const pipeline = document.getElementById('desktop-pipeline');
const replay = document.getElementById('pipeline-replay');
const nodes = [...pipeline.querySelectorAll('[data-node]')];
const descriptions = nodes.map(node => node.querySelector('small').textContent);
const links = [...pipeline.querySelectorAll('[data-link]')];
const motionPreference = matchMedia('(prefers-reduced-motion: reduce)');
let events = [];

function showStep(step, sending = false) {
  for (const [position, node] of nodes.entries()) {
    const index = Number(node.dataset.node);
    node.dataset.state = (index < step || (index === step && sending)) ? 'done' : index === step ? 'active' : 'waiting';
    const status = node.querySelector('small');
    status.textContent = status.dataset[node.dataset.state === 'active' ? 'running' : node.dataset.state] ?? descriptions[position];
  }
  for (const link of links) link.toggleAttribute('data-lit', Number(link.dataset.link) < step || (sending && Number(link.dataset.link) === step));
}

function playPipeline() {
  events.forEach(clearTimeout);
  events = [];
  if (motionPreference.matches) return showStep(4);
  showStep(0);
  const timings = [700, 1000, 1450, 850];
  let elapsed = 0;
  timings.forEach((duration, step) => {
    elapsed += duration;
    events.push(setTimeout(() => showStep(step, true), elapsed));
    if (step < timings.length - 1) {
      elapsed += 400;
      events.push(setTimeout(() => showStep(step + 1), elapsed));
    }
  });
}

replay.addEventListener('click', playPipeline);
const onScreen = new IntersectionObserver(entries => {
  if (!entries.some(entry => entry.intersectionRatio >= .6)) return;
  onScreen.disconnect();
  playPipeline();
}, {threshold:.6});
onScreen.observe(pipeline);
