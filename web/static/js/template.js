/**
 * lit-html templates escape every value, so names from MMF can't run as code.
 * Event handlers are bound with @click and friends, because the CSP forbids inline handlers.
 */
import { html, nothing, render as litRender, svg } from '/static/vendor/lit-html-3.3.1/lit-html.js';

export { html, nothing, svg };

/** Renders into a container, first clearing any placeholder markup from index.html. */
export function render(template, container) {
  if (!container._$litPart$) container.textContent = '';
  litRender(template, container);
}

/** An @error handler that swaps a broken image for a fallback, once. */
export function imageFallback(url) {
  return (event) => {
    const image = event.target;
    if (image.dataset.fellBack) return;
    image.dataset.fellBack = '1';
    if (url) image.src = url;
  };
}

export const stopPropagation = (event) => event.stopPropagation();

/** Adds a one-shot template, such as a modal, to the end of the page. */
export function appendToBody(template) {
  const fragment = new DocumentFragment();
  litRender(template, fragment);
  document.body.append(fragment);
}
