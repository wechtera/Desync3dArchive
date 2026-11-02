/**
 * Static markup in index.html names its handlers with data attributes, because the CSP forbids
 * inline onclick. data-click="fn" calls the registered fn with the JSON array in data-args. data-pass-checked
 * adds the element's checked state, and data-pass-event adds the event.
 */
const ACTIONS = new Map();

/**
 * Names functions for markup, and for views that can't import each other without a cycle.
 * tests/test_frontend_actions.py fails on a name with no entry here, because a name in a string
 * would otherwise break silently on a rename.
 */
export function registerActions(entries) {
  Object.entries(entries).forEach(([name, handler]) => ACTIONS.set(name, handler));
}

/** actions.name(...) calls the registered function. Reading it is safe before it's registered. */
export const actions = new Proxy({}, { get: (_, name) => ACTIONS.get(name) });

function dispatch(event, attribute) {
  const target = event.target.closest?.(`[${attribute}]`);
  if (!target) return;

  const handler = ACTIONS.get(target.getAttribute(attribute));
  if (typeof handler !== 'function') {
    console.error(`No handler for ${target.getAttribute(attribute)}`);
    return;
  }

  const args = JSON.parse(target.dataset.args || '[]');
  if ('passChecked' in target.dataset) args.push(target.checked);
  if ('passEvent' in target.dataset) args.push(event);
  handler(...args);
}

/** Routes data-click, data-change and data-input to registered handlers, because the CSP forbids inline handlers. */
export function bindActions() {
  document.addEventListener('click', (event) => dispatch(event, 'data-click'));
  document.addEventListener('change', (event) => dispatch(event, 'data-change'));
  document.addEventListener('input', (event) => dispatch(event, 'data-input'));
}
