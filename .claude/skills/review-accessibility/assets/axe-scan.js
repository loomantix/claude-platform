/* eslint-env browser */
/* global axe */
/**
 * Headless accessibility scan.
 * Framework-agnostic: injected as a plain script into any live page via
 * the browser tool's JS-eval capability. No dependency on the host app's
 * source, no visual UI — just loads axe-core, runs it, and exposes a bounded,
 * sanitized result on window.__a11yScan for an external orchestrator (the
 * /review-accessibility skill) to read back and act on. Raw page content,
 * URLs, selectors, and attribute values never cross that boundary.
 *
 * The orchestrator polls `ready || failed`, never `ready` alone: every
 * terminal path sets exactly one of them, so a load or run failure surfaces
 * as `failed` + `error` instead of hanging the poll forever.
 */
(function () {
  // Pinned to an exact version with a Subresource Integrity hash: this
  // script executes third-party bytes inside a live — often authenticated —
  // synthetic-data browser session, so the CDN must not be able to serve different code
  // than what was reviewed. A floating range (`axe-core@4`) cannot carry an
  // SRI hash and would also make scan results irreproducible as the engine
  // shifts underneath. Bumping the version means re-deriving the hash from
  // the bytes that URL serves, then pasting it below:
  //   openssl dgst -sha384 -binary axe.min.js | openssl base64 -A
  var SRC = 'https://cdn.jsdelivr.net/npm/axe-core@4.12.1/axe.min.js';
  var EXPECTED_VERSION = '4.12.1';
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
    violations: [],
    incomplete: [],
    counts: { violations: 0, incomplete: 0 },
    truncated: false,
  };
  window.__a11yScan = scan;

  function fail(code) {
    scan.error = code;
    scan.failed = true;
    console.error('[a11y-scan]', code);
  }

  var SAFE_IMPACTS = ['critical', 'serious', 'moderate', 'minor', null];
  var MAX_TOTAL_FINDINGS = 200;
  var MAX_NODES_PER_FINDING = 50;
  var MAX_TOTAL_ELEMENT_HINTS = 128;
  var MAX_TAGS_PER_FINDING = 8;
  var MAX_ATTRIBUTES_PER_HINT = 8;
  var SAFE_HTML_TAGS = [
    'a',
    'article',
    'aside',
    'audio',
    'button',
    'canvas',
    'details',
    'dialog',
    'div',
    'fieldset',
    'footer',
    'form',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'header',
    'iframe',
    'img',
    'input',
    'label',
    'li',
    'main',
    'nav',
    'ol',
    'option',
    'p',
    'section',
    'select',
    'span',
    'summary',
    'table',
    'tbody',
    'td',
    'textarea',
    'th',
    'thead',
    'tr',
    'ul',
    'video',
  ];
  var SAFE_ATTRIBUTE_NAMES = [
    'alt',
    'aria-activedescendant',
    'aria-atomic',
    'aria-autocomplete',
    'aria-braillelabel',
    'aria-brailleroledescription',
    'aria-busy',
    'aria-checked',
    'aria-colcount',
    'aria-colindex',
    'aria-colindextext',
    'aria-colspan',
    'aria-controls',
    'aria-current',
    'aria-describedby',
    'aria-description',
    'aria-details',
    'aria-disabled',
    'aria-dropeffect',
    'aria-errormessage',
    'aria-expanded',
    'aria-flowto',
    'aria-grabbed',
    'aria-haspopup',
    'aria-hidden',
    'aria-invalid',
    'aria-keyshortcuts',
    'aria-label',
    'aria-labelledby',
    'aria-level',
    'aria-live',
    'aria-modal',
    'aria-multiline',
    'aria-multiselectable',
    'aria-orientation',
    'aria-owns',
    'aria-placeholder',
    'aria-posinset',
    'aria-pressed',
    'aria-readonly',
    'aria-relevant',
    'aria-required',
    'aria-roledescription',
    'aria-rowcount',
    'aria-rowindex',
    'aria-rowindextext',
    'aria-rowspan',
    'aria-selected',
    'aria-setsize',
    'aria-sort',
    'aria-valuemax',
    'aria-valuemin',
    'aria-valuenow',
    'aria-valuetext',
    'for',
    'lang',
    'role',
    'tabindex',
    'title',
    'type',
  ];

  function safeRuleId(value) {
    return typeof value === 'string' && /^[a-z0-9-]{1,64}$/.test(value)
      ? value
      : 'unrecognized-rule';
  }

  function isSafeTag(tag) {
    return (
      typeof tag === 'string' &&
      tag.length <= 32 &&
      /^(?:wcag[0-9a-z]+|best-practice|cat\.[a-z0-9-]+|EN-[0-9.]+)$/.test(tag)
    );
  }

  function safeTags(tags) {
    if (!Array.isArray(tags)) return [];
    return tags
      .filter(function (tag, index, allTags) {
        return isSafeTag(tag) && allTags.indexOf(tag) === index;
      })
      .slice(0, MAX_TAGS_PER_FINDING);
  }

  function resolveElement(target) {
    if (
      !Array.isArray(target) ||
      target.length !== 1 ||
      typeof target[0] !== 'string' ||
      target[0].length > 512
    ) {
      return null;
    }
    try {
      return document.querySelector(target[0]);
    } catch (_error) {
      return null;
    }
  }

  function safeElementHint(node) {
    var element = resolveElement(node && node.target);
    if (!element) return null;

    var rawTagName = String(element.tagName || '').toLowerCase();
    var tagName = SAFE_HTML_TAGS.includes(rawTagName)
      ? rawTagName
      : 'custom-element';
    var safeAttributeNames = Array.from(element.getAttributeNames()).filter(
      function (name, index, names) {
        return (
          SAFE_ATTRIBUTE_NAMES.includes(name) && names.indexOf(name) === index
        );
      },
    );
    var attributeNames = safeAttributeNames.slice(0, MAX_ATTRIBUTES_PER_HINT);

    return {
      hint: { tagName: tagName, attributeNames: attributeNames.sort() },
      truncated: safeAttributeNames.length > MAX_ATTRIBUTES_PER_HINT,
    };
  }

  /**
   * Return only bounded axe metadata and structural element hints. Raw DOM,
   * visible text, URLs, selectors, and attribute values are deliberately
   * discarded before the browser tool can read the result.
   */
  function sanitizeFinding(finding, hintBudget) {
    var nodes = Array.isArray(finding && finding.nodes) ? finding.nodes : [];
    var rawTags = Array.isArray(finding && finding.tags) ? finding.tags : [];
    var tags = safeTags(rawTags);
    var safeTagCount = rawTags.filter(function (tag, index, allTags) {
      return isSafeTag(tag) && allTags.indexOf(tag) === index;
    }).length;
    var elementHints = [];
    var hintsTruncated = false;
    var attributesTruncated = false;
    var nodeLimit = Math.min(nodes.length, MAX_NODES_PER_FINDING);

    for (var index = 0; index < nodeLimit; index += 1) {
      if (hintBudget.remaining === 0) {
        hintsTruncated = true;
        break;
      }
      var safeHint = safeElementHint(nodes[index]);
      if (!safeHint) continue;
      elementHints.push(safeHint.hint);
      hintBudget.remaining -= 1;
      attributesTruncated = attributesTruncated || safeHint.truncated;
    }

    return {
      ruleId: safeRuleId(finding && finding.id),
      impact: SAFE_IMPACTS.includes(finding && finding.impact)
        ? finding.impact
        : null,
      tags: tags,
      nodeCount: nodes.length,
      elementHints: elementHints,
      truncated:
        nodes.length > MAX_NODES_PER_FINDING ||
        safeTagCount > MAX_TAGS_PER_FINDING ||
        hintsTruncated ||
        attributesTruncated,
    };
  }

  function sanitizeFindings(findings, findingBudget, hintBudget) {
    if (!Array.isArray(findings)) return [];
    var retained = findings.slice(0, findingBudget.remaining);
    findingBudget.remaining -= retained.length;
    return retained.map(function (finding) {
      return sanitizeFinding(finding, hintBudget);
    });
  }

  function ensureAxe(cb) {
    // A page-owned global is untrusted and would bypass both the version pin
    // and SRI. Full-page navigation between scans gives every injection a clean
    // realm, so collision is a hard failure rather than a reuse path.
    if (window.axe) {
      fail('AXE_GLOBAL_COLLISION');
      return;
    }
    var s = document.createElement('script');
    s.src = SRC;
    s.integrity = INTEGRITY;
    // SRI on a cross-origin subresource is only enforced when the request is
    // made in CORS mode; without this the integrity attribute is ignored.
    s.crossOrigin = 'anonymous';
    s.onload = function () {
      if (!window.axe || window.axe.version !== EXPECTED_VERSION) {
        fail('AXE_VERSION_MISMATCH');
        return;
      }
      cb(window.axe);
    };
    s.onerror = function () {
      fail('AXE_LOAD_FAILED');
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

  ensureAxe(function (pinnedAxe) {
    // axe.run throws SYNCHRONOUSLY when a previous run is still in flight
    // ("Axe is already running."); the throw escapes the promise, so a bare
    // .catch() would never see it. Wrap the call itself.
    try {
      pinnedAxe.run(document, AXE_OPTIONS, function (err, results) {
        if (err) {
          fail('AXE_RUN_FAILED');
          return;
        }
        scan.counts.violations = Array.isArray(results.violations)
          ? results.violations.length
          : 0;
        scan.counts.incomplete = Array.isArray(results.incomplete)
          ? results.incomplete.length
          : 0;
        var findingBudget = { remaining: MAX_TOTAL_FINDINGS };
        var hintBudget = { remaining: MAX_TOTAL_ELEMENT_HINTS };
        scan.violations = sanitizeFindings(
          results.violations,
          findingBudget,
          hintBudget,
        );
        // 'incomplete' ("needs review") is where axe puts findings it cannot
        // prove either way — classically colour contrast over a background
        // image. Dropping it would silently under-report.
        scan.incomplete = sanitizeFindings(
          results.incomplete,
          findingBudget,
          hintBudget,
        );
        scan.truncated =
          scan.counts.violations > scan.violations.length ||
          scan.counts.incomplete > scan.incomplete.length ||
          scan.violations.some(function (finding) {
            return finding.truncated;
          }) ||
          scan.incomplete.some(function (finding) {
            return finding.truncated;
          });
        scan.ready = true;
      });
    } catch (_error) {
      fail('AXE_RUN_FAILED');
    }
  });
})();
