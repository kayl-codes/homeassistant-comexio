// Pure helper functions for comexio-plan-card.js — no `this`, no DOM, no hass state.
// Split out of the ComexioPlanCard class because these are the only pieces of that
// class that never touch instance state: given the same arguments they always return
// the same result, so they read (and could be tested) independently of the custom
// element's lifecycle. Used both by the toolbar search box and the debug-log exclude
// filter — see comexio-plan-card.js for the call sites.

// Emulates a leading `(?<![\p{L}\p{N}])` lookbehind by checking the preceding character
// by hand instead — negative lookbehind isn't supported in Safari/WebKit before 16.4,
// and a regex that fails to compile there would silently disable all search matching.
function hasBoundedMatch(label, globalRegex, needsLeadingGuard = true) {
  for (const m of label.matchAll(globalRegex)) {
    const before = m.index > 0 ? label[m.index - 1] : undefined;
    if (!needsLeadingGuard || !before || !/[\p{L}\p{N}]/u.test(before)) {
      return true;
    }
  }
  return false;
}

function escapeRegexLiteral(t) {
  return t.replace(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`);
}

function wildcardToken(t) {
  if (t === "?") {
    return String.raw`\S`;
  }
  if (t === "*") {
    return String.raw`\S*`;
  }
  return escapeRegexLiteral(t);
}

// Wildcards, TOKEN-ANCHORED: ? = exactly one non-space char, * = any run of
// non-space chars (also empty). Guards on both ends keep the match from bleeding
// into neighbouring id characters — "M4?" hits M40–M49 but not M4/M400, "M4*"
// hits M4/M40/M400…. (Mirrored in services.py _build_label_matcher.)
export function matchesPattern(label, pattern) {
  if (/[?*]/.test(pattern)) {
    // Collapse "**"/"***" to a single "*" first — semantically identical (both mean
    // "any run of non-space chars"), but adjacent \S* \S* quantifiers on the same
    // character class are super-linear on backtracking for a non-matching input.
    const body = pattern
      .replace(/\*+/g, "*")
      .split(/([?*])/)
      .map((t) => wildcardToken(t))
      .join("");
    try {
      // Trailing boundary uses a lookahead (broadly supported); the leading boundary
      // is checked manually in hasBoundedMatch instead of via lookbehind, which
      // Safari/WebKit before 16.4 doesn't support.
      const re = new RegExp(String.raw`${body}(?![\p{L}\p{N}])`, "giu");
      return hasBoundedMatch(label, re);
    } catch {
      return false;
    }
  }
  // Plain query: substring test with token guards — an alphanumeric start must not be
  // preceded by a letter/digit and a trailing digit must not be followed by another
  // digit, so "M4" hits M4 but not M40/M400/PWM4. Use * for loose matching.
  const esc = escapeRegexLiteral(pattern);
  const needsLeadingGuard = /^[\p{L}\p{N}]/u.test(pattern);
  const after = /\d$/.test(pattern) ? String.raw`(?!\d)` : "";
  try {
    const re = new RegExp(esc + after, "giu");
    return hasBoundedMatch(label, re, needsLeadingGuard);
  } catch {
    return false;
  }
}

// dd.MM.yyyy HH:mm:ss.fff (user-specified log timestamp format)
export function fmtTs(d) {
  const p = (n, l = 2) => String(n).padStart(l, "0");
  return (
    `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`
  );
}
