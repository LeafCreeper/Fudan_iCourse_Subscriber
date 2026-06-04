/**
 * Markdown + KaTeX rendering pipeline.
 * Sets window.ICS.render global.
 *
 * Depends on CDN globals: marked, DOMPurify, renderMathInElement
 *
 * Pipeline: stash formulas → marked → restore formulas → DOMPurify → KaTeX
 * We stash formula delimiters before marked.parse() because characters like
 * * and _ inside $...$ LaTeX (e.g. D^*, P_n, \sum_{i=1}) would otherwise
 * be treated as markdown emphasis and break the formula structure.
 */

window.ICS = window.ICS || {};

var _FORMULA_PLACEHOLDER_PREFIX = "";
var _FORMULA_PLACEHOLDER_SUFFIX = "";

/** Replace $...$ and $$...$$ with placeholders so marked won't touch them. */
function _stashFormulas(mdText) {
  var formulas = [];
  function stash(replacement) {
    var key = _FORMULA_PLACEHOLDER_PREFIX + formulas.length + _FORMULA_PLACEHOLDER_SUFFIX;
    formulas.push(replacement);
    return key;
  }

  var text = mdText;

  // 1. Stash existing \(...\) and \[...\] (already-LaTeX, protect from double-processing)
  text = text.replace(/(\\\([\s\S]*?\\\))|(\\\[[\s\S]*?\\\])/g, function (m) {
    return stash(m);
  });

  // 2. Stash $$...$$ (must run before $ → to avoid consuming individual $ chars of $$)
  text = text.replace(/\$\$([\s\S]*?)\$\$/g, function (_, f) {
    return stash("\\[" + f + "\\]");
  });

  // 3. Stash $...$ (inline math). Keep it on one line so one stray
  // dollar cannot swallow a whole paragraph and turn prose into a KaTeX
  // error block.
  text = text.replace(/\$([^\n$]+?)\$/g, function (_, f) {
    return stash("\\(" + f + "\\)");
  });

  return { text: text, formulas: formulas };
}

/** Restore stashed formulas in the HTML output after marked.parse(). */
function _restoreFormulas(html, formulas) {
  for (var i = 0; i < formulas.length; i++) {
    html = html.split(_FORMULA_PLACEHOLDER_PREFIX + i + _FORMULA_PLACEHOLDER_SUFFIX).join(formulas[i]);
  }
  return html;
}

function _normalizeDisplayText(mdText) {
  var text = String(mdText || "");

  // LLMs and OCR can produce orphan math delimiters. Balanced formulas stay
  // renderable; malformed delimiters are neutralized so the note remains read-
  // able instead of becoming a large red KaTeX error region.
  var dollarPairs = (text.match(/\$\$/g) || []).length;
  if (dollarPairs % 2 === 1) text = text.replace(/\$\$/g, "");

  var singleDollars = (text.match(/(^|[^$])\$(?!$)/g) || []).length;
  if (singleDollars % 2 === 1) text = text.replace(/\$/g, "");

  var openBlocks = (text.match(/\\\[/g) || []).length;
  var closeBlocks = (text.match(/\\\]/g) || []).length;
  if (openBlocks !== closeBlocks) text = text.replace(/\\[\[\]]/g, "");

  var openInline = (text.match(/\\\(/g) || []).length;
  var closeInline = (text.match(/\\\)/g) || []).length;
  if (openInline !== closeInline) text = text.replace(/\\[()]/g, "");

  return text;
}

function _renderMarkdown(mdText) {
  if (!mdText) return "";
  var stashed = _stashFormulas(_normalizeDisplayText(mdText));
  var rawHtml = marked.parse(stashed.text, { breaks: true });
  var restored = _restoreFormulas(rawHtml, stashed.formulas);
  return DOMPurify.sanitize(restored, {
    ALLOWED_TAGS: [
      "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3",
      "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "strong",
      "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "ul",
    ],
    ALLOWED_ATTR: ["href", "rel", "target", "title"],
    FORBID_TAGS: ["font", "style"],
    FORBID_ATTR: [
      "style", "class", "id", "align", "color", "face", "size",
      "width", "height", "bgcolor",
    ],
  });
}

function _activateKaTeX(element) {
  if (typeof renderMathInElement !== "function") return;
  renderMathInElement(element, {
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "\\[", right: "\\]", display: true },
      { left: "\\(", right: "\\)", display: false },
      // NOTE: $...$ intentionally omitted from KaTeX — converted to \(...\) in _stashFormulas
    ],
    throwOnError: false,
    errorColor: "#1a1a1a",
  });
  _downgradeKaTeXErrors(element);
}

function _downgradeKaTeXErrors(element) {
  if (!element || !element.querySelectorAll) return;
  element.querySelectorAll(".katex-error").forEach(function (node) {
    var code = document.createElement("code");
    code.textContent = node.textContent || "";
    node.replaceWith(code);
  });
}

function _plainSnippet(mdText, maxLen) {
  maxLen = maxLen || 100;
  if (!mdText) return "";
  var text = mdText
    .replace(/\$\$.+?\$\$/gs, "...")
    .replace(/\\\[.+?\\\]/gs, "...")
    .replace(/\$[^$]+?\$/g, "...")
    .replace(/\\\(.+?\\\)/g, "...")
    .replace(/#{1,6}\s+/g, "")
    .replace(/\*{1,3}(.+?)\*{1,3}/g, "$1")
    .replace(/`{1,3}[^`]*`{1,3}/g, "")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[|:\-]+/g, " ")
    .replace(/\n+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > maxLen ? text.slice(0, maxLen) + "..." : text;
}

window.ICS.render = {
  renderMarkdown: _renderMarkdown,
  activateKaTeX: _activateKaTeX,
  plainSnippet: _plainSnippet,
};
