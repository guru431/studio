/* Capture the actual DOM range: partial emphasis/code, list numbers and tables keep their markup. */
const CONTENT = '.md, .bubble, .code-msg.user, .code-step-input, .code-step-result, .think-text';
const TAGS = new Set('DIV SPAN P BR HR H1 H2 H3 H4 H5 H6 STRONG B EM I DEL S U CODE PRE BLOCKQUOTE UL OL LI TABLE THEAD TBODY TFOOT TR TH TD A SUB SUP'.split(' '));
const SKIP = 'button, input, select, textarea, img, svg, [hidden], [aria-hidden="true"]';

export function selectedChatHTML(root) {
  const selection = document.getSelection();
  if (!root?.isConnected || !selection?.rangeCount || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  const copy = (node) => {
    if (!range.intersectsNode(node)) return null;
    if (node.nodeType === Node.TEXT_NODE) {
      const start = node === range.startContainer ? range.startOffset : 0;
      const end = node === range.endContainer ? range.endOffset : node.length;
      return document.createTextNode(node.data.slice(start, end));
    }
    if (node.nodeType !== Node.ELEMENT_NODE || node.matches(SKIP) || node.closest('details:not([open])')) return null;
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') return null;
    const out = document.createElement(TAGS.has(node.tagName) ? node.tagName.toLowerCase() : 'div');
    for (const key of ['href', 'title', 'class', 'start', 'value', 'align', 'colspan', 'rowspan']) {
      if (node.hasAttribute(key)) out.setAttribute(key, node.getAttribute(key));
    }
    // Markdown tables carry alignment in inline styles; only the semantic value is sent.
    if (node.matches('th, td')) out.setAttribute('align', style.textAlign);
    for (const child of node.childNodes) {
      const cloned = copy(child);
      if (cloned) out.append(cloned);
    }
    if (node.matches('ol')) {
      const first = [...node.children].findIndex((li) => range.intersectsNode(li));
      let number = node.start;
      for (const li of [...node.children].slice(0, first + 1)) {
        if (li.hasAttribute('value')) number = li.value;
        if (li !== node.children[first]) number++;
      }
      out.start = number;
    }
    if (node.matches('.bubble, .code-msg.user, .think-text')) out.className = 'chat-export-plain';
    return out;
  };
  const output = document.createElement('div');
  for (const node of root.querySelectorAll(CONTENT)) {
    if (node.parentElement.closest(CONTENT) && root.contains(node.parentElement.closest(CONTENT))) continue;
    const part = copy(node);
    if (part?.textContent.trim()) output.append(part);
  }
  return output.textContent.trim() ? output.innerHTML : null;
}
