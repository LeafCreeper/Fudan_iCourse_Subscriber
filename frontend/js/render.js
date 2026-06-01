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

  // 3. Stash $...$ (inline math — non-greedy to pair nearest closing $)
  text = text.replace(/\$([\s\S]+?)\$/g, function (_, f) {
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

function _renderMarkdown(mdText, pptImgMap) {
  if (!mdText) return "";
  var stashed = _stashFormulas(mdText);

  // Stash PPT image placeholders before markdown conversion.
  // LLM outputs: ![PPT 页 N](pptimg://N)
  // We stash them to prevent marked from wrapping the URL in <a> or mangling it.
  var pptPlaceholders = [];
  var text = stashed.text.replace(
    /!\[PPT 页 (\d+)\]\(pptimg:\/\/(\d+)\)/g,
    function (match, label, pageNum) {
      var key = "\x00PPTIMG" + pptPlaceholders.length + "\x00";
      pptPlaceholders.push({ pageNum: parseInt(pageNum), label: label });
      return key;
    }
  );

  var rawHtml = marked.parse(text, { breaks: true });
  var restored = _restoreFormulas(rawHtml, stashed.formulas);

  // Restore PPT image placeholders → <img> tags or strip if no map
  for (var i = 0; i < pptPlaceholders.length; i++) {
    var p = pptPlaceholders[i];
    var imgTag = "";
    if (pptImgMap && pptImgMap[p.pageNum]) {
      imgTag =
        '<div style="text-align:center;margin:12px 0">' +
        '<img src="' + pptImgMap[p.pageNum] + '" alt="PPT 页 ' + p.pageNum + '" ' +
        'style="max-width:100%;height:auto;border:1px solid #e0e0e0;border-radius:4px;" ' +
        'loading="lazy">' +
        '</div>';
    }
    restored = restored.split("\x00PPTIMG" + i + "\x00").join(imgTag);
  }

  return DOMPurify.sanitize(restored);
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
  });
}

function _plainSnippet(mdText, maxLen) {
  maxLen = maxLen || 100;
  if (!mdText) return "";
  var text = mdText
    .replace(/!\[PPT 页 \d+\]\(pptimg:\/\/\d+\)/g, "")
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
