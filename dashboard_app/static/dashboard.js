const tabs = [...document.querySelectorAll('[role="tab"]')];
const panels = [...document.querySelectorAll('[role="tabpanel"]')];

function activateTab(name, updateHash = true) {
  if (!panels.some((panel) => panel.id === name)) name = 'overview';
  tabs.forEach((tab) => tab.setAttribute('aria-selected', String(tab.dataset.tab === name)));
  panels.forEach((panel) => { panel.hidden = panel.id !== name; });
  if (updateHash) history.replaceState(null, '', `#${name}`);
}

tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => activateTab(tab.dataset.tab));
  tab.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const offset = event.key === 'ArrowRight' ? 1 : -1;
    const next = tabs[(index + offset + tabs.length) % tabs.length];
    next.focus();
    activateTab(next.dataset.tab);
  });
});

activateTab(location.hash.slice(1) || 'overview', false);
window.addEventListener('hashchange', () => activateTab(location.hash.slice(1), false));

document.querySelector('#refresh-form').addEventListener('submit', () => {
  document.querySelector('#refresh-button').disabled = true;
  document.querySelector('#loading').hidden = false;
});
