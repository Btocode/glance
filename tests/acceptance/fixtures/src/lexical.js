import { registerPlainText } from "@lexical/plain-text";
import {
  $createParagraphNode, $createTextNode, $getRoot, createEditor,
} from "lexical";
import { install, domCaret, setDomCaret } from "../bridge.js";

const host = document.getElementById("editor-host");
const el = document.createElement("div");
el.id = "editor";
el.contentEditable = "true";
el.setAttribute("role", "textbox");
el.setAttribute("aria-label", "editor");
host.appendChild(el);

const editor = createEditor({ namespace: "glance", onError: (e) => { throw e; } });
editor.setRootElement(el);
registerPlainText(editor);

install({
  getText: () => editor.getEditorState().read(() => $getRoot().getTextContent()),
  getCaret: () => domCaret(el),
  setCaret: (n) => { el.focus(); setDomCaret(el, n); },
  setText: (s) => {
    editor.update(() => {
      const root = $getRoot();
      root.clear();
      const p = $createParagraphNode();
      if (s) p.append($createTextNode(s));
      root.append(p);
      p.selectEnd();
    });
    el.focus();
  },
  focus: () => el.focus(),
});
