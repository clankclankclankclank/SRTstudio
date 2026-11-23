const pipeline = document.getElementById('desktop-pipeline');
const replay = document.getElementById('pipeline-replay');
const nodes = [...pipeline.querySelectorAll('[data-node]')];
const links = [...pipeline.querySelectorAll('[data-link]')];
const motionPreference = matchMedia('(prefers-reduced-motion: reduce)');
let events = [];

function showStep(step) {
  for (const node of nodes) {
    const index = Number(node.dataset.node);
    node.dataset.state = index < step ? 'done' : index === step ? 'active' : 'waiting';
  }
  for (const link of links) link.toggleAttribute('data-lit', Number(link.dataset.link) < step);
}

function playPipeline() {
  events.forEach(clearTimeout);
  events = [];
  if (motionPreference.matches) return showStep(4);
  showStep(0);
  for (let step = 1; step <= 4; step++) {
    events.push(setTimeout(() => showStep(step), step * 900));
  }
}

replay.addEventListener('click', playPipeline);
const onScreen = new IntersectionObserver(entries => {
  if (!entries.some(entry => entry.isIntersecting)) return;
  onScreen.disconnect();
  playPipeline();
}, {threshold:.2});
onScreen.observe(pipeline);
