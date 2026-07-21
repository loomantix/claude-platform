/* eslint-env browser */
/* global axe */
/**
 * Headless accessibility scan.
 * Framework-agnostic: injected as a plain script into any live page via
 * the browser tool's JS-eval capability. No dependency on the host app's
 * source, no visual UI — just loads axe-core, runs it, and exposes the
 * full result on window.__a11yScan for an external orchestrator (the
 * /review-accessibility skill) to read back and act on.
 *
 * The orchestrator polls `ready || failed`, never `ready` alone: every
 * terminal path sets exactly one of them, so a load or run failure surfaces
 * as `failed` + `error` instead of hanging the poll forever.
 */
(function () {
  // Pinned to an exact version with a Subresource Integrity hash: this
  // script executes third-party bytes inside a live — often authenticated —
  // browser session, so the CDN must not be able to serve different code
  // than what was reviewed. A floating range (`axe-core@4`) cannot carry an
  // SRI hash and would also make scan results irreproducible as the engine
  // shifts underneath. Bumping the version means re-deriving the hash from
  // the bytes that URL serves, then pasting it below:
  //   openssl dgst -sha384 -binary axe.min.js | openssl base64 -A
  var SRC = 'https://cdn.jsdelivr.net/npm/axe-core@4.12.1/axe.min.js';
  var INTEGRITY =
    'sha384-JQegRXq6EhTiWoGPFDmqbJNsDow5BoSsGhnaeDzGp+qyOFCuMZZ24qY2fz3FxZF5';

  // Rebuilt on every injection rather than reused via `window.__a11yScan ||
  // {...}`. Phase 3 re-injects to verify a fix, and a dev-server hot reload
  // swaps modules without discarding `window` — carrying over a previous
  // run's `ready: true` would let the orchestrator read pre-fix violations
  // as if they were the verify result.
  var scan = {
    ready: false,
    failed: false,
    error: null,
    url: null,
    violations: [],
    incomplete: [],
  };
  window.__a11yScan = scan;

  function fail(message) {
    scan.error = message;
    scan.failed = true;
    console.error('[a11y-scan]', message);
  }

  function ensureAxe(cb) {
    if (window.axe) return cb();
    var s = document.createElement('script');
    s.src = SRC;
    s.integrity = INTEGRITY;
    // SRI on a cross-origin subresource is only enforced when the request is
    // made in CORS mode; without this the integrity attribute is ignored.
    s.crossOrigin = 'anonymous';
    s.onload = cb;
    s.onerror = function () {
      fail(
        'failed to load axe-core from ' +
          SRC +
          ' — CSP block, no network, or SRI mismatch',
      );
    };
    document.head.appendChild(s);
  }

  // Runs axe-core's default ruleset minus the two rules disabled below. That
  // default is WCAG 2.0/2.1 Level A + AA plus axe's curated non-WCAG
  // 'best-practice' rules (~27 that run by default, e.g. 'heading-order',
  // 'empty-heading' — no formal WCAG mapping but real value). Level AAA and
  // WCAG 2.2 rules ship `enabled: false` in axe-core and do NOT run here;
  // neither do rules tagged 'experimental' or 'deprecated'. Each violation
  // carries its own `tags` array (e.g. 'wcag2aa', 'wcag143') which the
  // orchestrator uses to classify findings by success criterion, rather than
  // dropping non-WCAG-tagged rules at scan time.
  //
  // The two exclusions are a deliberate signal-to-noise choice, not a
  // standards filter — both are 'best-practice' rules about page-level
  // landmark structure: 'region' fires for every top-level block not wrapped
  // in a landmark, and 'landmark-one-main' fires once per page lacking a
  // <main>. SKILL.md documents them so the docs match this file.
  var AXE_OPTIONS = {
    rules: {
      region: { enabled: false },
      'landmark-one-main': { enabled: false },
    },
  };

  ensureAxe(function () {
    // axe.run throws SYNCHRONOUSLY when a previous run is still in flight
    // ("Axe is already running."); the throw escapes the promise, so a bare
    // .catch() would never see it. Wrap the call itself.
    try {
      axe.run(document, AXE_OPTIONS, function (err, results) {
        if (err) {
          fail(String((err && err.message) || err));
          return;
        }
        scan.url = results.url;
        scan.violations = results.violations;
        // 'incomplete' ("needs review") is where axe puts findings it cannot
        // prove either way — classically colour contrast over a background
        // image. Dropping it would silently under-report.
        scan.incomplete = results.incomplete;
        scan.ready = true;
      });
    } catch (e) {
      fail(String((e && e.message) || e));
    }
  });
})();
