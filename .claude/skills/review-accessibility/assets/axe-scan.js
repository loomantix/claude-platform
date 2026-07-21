/**
 * Headless accessibility scan.
 * Framework-agnostic: injected as a plain script into any live page via
 * the browser tool's JS-eval capability. No dependency on the host app's
 * source, no visual UI — just loads axe-core, runs it, and exposes the
 * full result on window.__a11yScan for an external orchestrator (the
 * /review-accessibility skill) to read back and act on.
 */
(function () {
  window.__a11yScan = window.__a11yScan || { ready: false, violations: [] };

  function ensureAxe(cb) {
    if (window.axe) return cb();
    var s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/axe-core@4/axe.min.js';
    s.onload = cb;
    s.onerror = function () {
      console.error('[a11y-scan] failed to load axe-core from CDN');
    };
    document.head.appendChild(s);
  }

  // Runs axe-core's full default ruleset (WCAG 2.0/2.1 A/AA/AAA plus its
  // curated non-WCAG 'best-practice' rules — ~30 rules like 'heading-order'
  // and 'empty-heading' with no formal WCAG mapping but real value) rather
  // than filtering to a single standard here. Each violation carries its
  // own axe `tags` array (e.g. 'wcag2aa', 'wcag143'), which the orchestrator
  // uses to classify findings by WCAG level in its reporting instead of
  // silently dropping non-WCAG-tagged rules at scan time.
  // 'region'/'landmark-one-main' are the exception: disabled outright as
  // page-structure noise (every top-level block not wrapped in a landmark),
  // not because they're off-standard.
  var AXE_OPTIONS = {
    rules: {
      region: { enabled: false },
      'landmark-one-main': { enabled: false },
    },
  };

  ensureAxe(function () {
    axe.run(document, AXE_OPTIONS, function (err, results) {
      if (err) {
        console.error('[a11y-scan]', err);
        return;
      }
      window.__a11yScan.violations = results.violations;
      window.__a11yScan.ready = true;
    });
  });
})();
