/* csp-actions.js — delegated event dispatcher (Phase 5, CSP migration step 1).

   Replaces inline handlers (onclick="fn()") so a page can run under a CSP with no 'unsafe-inline'.
   Markup:   <button data-click="save" data-id="12">   <input data-input="search">
             <select data-change="filter">   <input data-keydown="submitOnEnter">   <form data-submit="send">
   Code:     wzActions.register('save', function (event, element) { var id = element.dataset.id; ... });

   Safety: an action runs ONLY if it was registered here by name (an allow-list). The attribute value
   is never evaluated and never looked up on window, so markup injected into a page cannot call an
   arbitrary function; arguments travel as data-* attributes (read from element.dataset) and so never
   need JavaScript-in-HTML escaping. */
(function () {
  'use strict';
  var registry = Object.create(null);

  function register(name, fn) {
    if (typeof name !== 'string' || typeof fn !== 'function') throw new TypeError('wzActions.register(name, fn)');
    registry[name] = fn;
  }

  function closestWith(el, attr) {
    while (el && el !== document) {
      if (el.nodeType === 1 && el.hasAttribute(attr)) return el;
      el = el.parentNode;
    }
    return null;
  }

  function handle(eventName, attr) {
    document.addEventListener(eventName, function (ev) {
      var el = closestWith(ev.target, attr);
      if (!el) return;
      var fn = registry[el.getAttribute(attr)];
      if (!fn) return;
      if (eventName === 'submit') ev.preventDefault();
      try { fn.call(el, ev, el); }
      catch (err) { if (window.console) console.error('[wzActions] ' + attr + '=' + el.getAttribute(attr) + ' failed:', err); }
    }, false);
  }

  handle('click', 'data-click');
  handle('input', 'data-input');
  handle('change', 'data-change');
  handle('keydown', 'data-keydown');
  handle('submit', 'data-submit');

  window.wzActions = { register: register, has: function (n) { return typeof registry[n] === 'function'; } };
})();
