/**
 * Static markup in index.html names its handlers with data attributes, because the CSP forbids
 * inline onclick. data-click="fn" calls the registered fn with the JSON array in data-args. data-pass-checked
 * adds the element's checked state, and data-pass-event adds the event.
 */
const ACTIONS = new Map();

/**
 * Names functions for markup. tests/test_frontend_actions.py fails on any data-click with no entry
 * here, because a name in markup would otherwise break silently on a rename. Views still call each
 * other through window, so these stay there until those calls become imports.
 */
export function registerActions(actions) {
  Object.entries(actions).forEach(([name, handler]) => ACTIONS.set(name, handler));
  Object.assign(window, actions);
}

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
