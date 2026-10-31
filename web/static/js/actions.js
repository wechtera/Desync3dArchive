/**
 * Static markup in index.html names its handlers with data attributes, because the CSP forbids
 * inline onclick. data-click="fn" calls window.fn with the JSON array in data-args. data-pass-checked
 * adds the element's checked state, and data-pass-event adds the event.
 */
function dispatch(event, attribute) {
  const target = event.target.closest?.(`[${attribute}]`);
  if (!target) return;

  const handler = window[target.getAttribute(attribute)];
  if (typeof handler !== 'function') {
    console.error(`No handler for ${target.getAttribute(attribute)}`);
    return;
  }

  const args = JSON.parse(target.dataset.args || '[]');
  if ('passChecked' in target.dataset) args.push(target.checked);
  if ('passEvent' in target.dataset) args.push(event);
  handler(...args);
}

export function bindActions() {
  document.addEventListener('click', (event) => dispatch(event, 'data-click'));
  document.addEventListener('change', (event) => dispatch(event, 'data-change'));
  document.addEventListener('input', (event) => dispatch(event, 'data-input'));
}
