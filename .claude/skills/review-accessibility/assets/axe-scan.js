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

  // 'region'/'landmark-one-main' flag every top-level block not wrapped in a
  // <main>/<header>/etc. landmark — page-structure noise, not the
  // component-level checks (labels, ARIA widget state, contrast) this tool
  // is for. Disabled by default; re-enable via axe.configure if you want them.
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
