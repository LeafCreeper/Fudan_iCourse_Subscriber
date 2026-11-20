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

function _renderMarkdown(mdText, parser = marked) {
  if (!mdText) return "";
  var stashed = _stashFormulas(mdText);
  var rawHtml = parser.parse(stashed.text, { breaks: true });
  var restored = _restoreFormulas(rawHtml, stashed.formulas);
  return DOMPurify.sanitize(restored);
}

const _citationSyntax = {
  name: "lectureCitation",
  level: "inline",
  start(src) { return src.indexOf("[[cite:"); },
  tokenizer(src) {
    const match = /^\[\[cite:([^\s[\]]+?):(summary|transcript|ocr)\]\]/.exec(src);
    if (match) return { type: "lectureCitation", raw: match[0], id: match[1], mode: match[2] };
  },
};

let _citationReader;

function _citationNumber(numbers, key) {
  if (!numbers.has(key)) numbers.set(key, numbers.size + 1);
  return numbers.get(key);
}

function _indexCitations(text, numbers) {
  if (!text.includes("[[cite:")) return;
  _citationReader ??= new marked.Marked({ extensions: [_citationSyntax] });
  _citationReader.walkTokens(_citationReader.lexer(_stashFormulas(text).text), token => {
    if (token.type === _citationSyntax.name) _citationNumber(numbers, JSON.stringify([token.id, token.mode]));
  });
}

function _renderAnswer(text, findSource, numbers) {
  if (!text.includes("[[cite:")) return _renderMarkdown(text);
  const visibleNumbers = new Map(numbers);
  const rendered = new Map();
  const parser = new marked.Marked({ extensions: [{
    ..._citationSyntax,
    renderer({ id, mode }) {
      const key = JSON.stringify([id, mode]);
      if (rendered.has(key)) return rendered.get(key);
      const number = _citationNumber(visibleNumbers, key);
      const source = findSource(id);
      const mark = document.createElement(source ? "button" : "span");
      mark.className = "agent-citation";
      mark.textContent = number <= 20 ? String.fromCodePoint(0x245f + number) : "(" + number + ")";
      if (source) {
        mark.type = "button";
        mark.dataset.lectureId = id;
        mark.dataset.mode = mode;
        mark.title = [source.course_title, source.teacher || "教师未标注", source.lecture_title].join(" · ");
        mark.setAttribute("aria-label", "引用 " + number + "：" + mark.title);
      } else {
        mark.title = "本地资料中找不到该课次";
      }
      const html = mark.outerHTML;
      rendered.set(key, html);
      return html;
    },
  }] });
  return _renderMarkdown(text, parser);
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
  renderAnswer: _renderAnswer,
  indexCitations: _indexCitations,
  activateKaTeX: _activateKaTeX,
  plainSnippet: _plainSnippet,
};
