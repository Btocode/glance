// Shared by every fixture page: exposes window.glance, POSTs {text, caret, seq} to /state
// on input / selectionchange and long-polls GET /cmd for {"op": setText|setCaret|focus}.
// Offsets are Unicode code points into getText().

export const cpLength = (s) => Array.from(s).length;
export const cpIndex = (s, utf16) => cpLength(s.slice(0, utf16));
export const utf16Index = (s, cp) => Array.from(s).slice(0, cp).join("").length;

const OBJECT = "￼";

// Text of a contenteditable subtree: non-editable inline atoms count as one U+FFFC; <br>
// and block boundaries are not produced (the fixtures hold one line).
function* leaves(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ALL);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) yield n;
}
const inAtom = (n, root) => {
  for (let e = n.nodeType === 1 ? n : n.parentElement; e && e !== root; e = e.parentElement) {
    if (e.getAttribute && e.getAttribute("contenteditable") === "false") return e;
  }
  return null;
};

// Code-point offset of a DOM point inside `root`, atoms counting 1.
export function domOffset(root, node, offset) {
  let count = 0;
  const seen = new Set();
  const target = node.nodeType === 3 ? node : node.childNodes[offset] || null;
  for (const n of leaves(root)) {
    if (n === target && node.nodeType !== 3) return count;
    const atom = inAtom(n, root);
    if (atom) {
      if (!seen.has(atom)) {
        seen.add(atom);
        if (atom.contains(node) || atom === node) return count; // inside the atom
        count += 1;
      }
      continue;
    }
    if (n.nodeType !== 3) continue;
    if (n === node) return count + cpIndex(n.data, offset);
    count += cpLength(n.data);
  }
  return count;
}

export function domPoint(root, cp) {
  let left = cp;
  const seen = new Set();
  let last = null;
  for (const n of leaves(root)) {
    const atom = inAtom(n, root);
    if (atom) {
      if (seen.has(atom)) continue;
      seen.add(atom);
      const parent = atom.parentNode;
      const index = Array.prototype.indexOf.call(parent.childNodes, atom);
      if (left === 0) return [parent, index];
      left -= 1;
      last = [parent, index + 1];
      continue;
    }
    if (n.nodeType !== 3) continue;
    const len = cpLength(n.data);
    if (left <= len) return [n, utf16Index(n.data, left)];
    left -= len;
    last = [n, n.data.length];
  }
  return last || [root, 0];
}

export function domCaret(root) {
  const sel = getSelection();
  if (!sel.focusNode || !root.contains(sel.focusNode)) return 0;
  return domOffset(root, sel.focusNode, sel.focusOffset);
}

export function setDomCaret(root, cp) {
  const [node, offset] = domPoint(root, cp);
  getSelection().setBaseAndExtent(node, offset, node, offset);
}

export function domText(root) {
  let out = "";
  const seen = new Set();
  for (const n of leaves(root)) {
    const atom = inAtom(n, root);
    if (atom) {
      if (!seen.has(atom)) { seen.add(atom); out += OBJECT; }
    } else if (n.nodeType === 3) out += n.data;
  }
  return out;
}

export function install(api) {
  window.glance = api;
  let seq = 0;
  const post = () => {
    seq += 1;
    fetch("/state", {
      method: "POST", keepalive: true,
      body: JSON.stringify({ text: api.getText(), caret: api.getCaret(), seq }),
    }).catch(() => {});
  };
  addEventListener("input", () => setTimeout(post, 0), true);
  document.addEventListener("selectionchange", () => setTimeout(post, 0));
  addEventListener("keyup", () => setTimeout(post, 0), true);

  async function poll() {
    for (;;) {
      try {
        const res = await fetch("/cmd");
        const cmd = await res.json();
        if (cmd.op === "noop") continue;
        if (cmd.op === "setText") api.setText(cmd.text);
        else if (cmd.op === "setCaret") api.setCaret(cmd.n);
        else if (cmd.op === "focus") api.focus();
        setTimeout(post, 30);
      } catch (e) {
        await new Promise((r) => setTimeout(r, 200));
      }
    }
  }
  api.focus();
  post();
  poll();
}
