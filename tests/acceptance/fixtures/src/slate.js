import { createElement as h, useMemo } from "react";
import { createRoot } from "react-dom/client";
import { Editor, Node, Transforms, createEditor } from "slate";
import { Editable, Slate, ReactEditor, withReact } from "slate-react";
import { install, domCaret, setDomCaret } from "../bridge.js";

const host = document.getElementById("editor-host");
const mount = document.createElement("div");
mount.id = "editor";
host.appendChild(mount);

const editor = withReact(createEditor());
const initialValue = [{ type: "paragraph", children: [{ text: "" }] }];

function App() {
  return h(Slate, { editor, initialValue },
    h(Editable, { "aria-label": "editor", style: { outline: "none", minHeight: "100px" } }));
}
createRoot(mount).render(h(App));

const root = () => ReactEditor.toDOMNode(editor, editor);
setTimeout(() => {
  install({
    getText: () => editor.children.map((n) => Node.string(n)).join("\n"),
    getCaret: () => domCaret(root()),
    setCaret: (n) => { ReactEditor.focus(editor); setDomCaret(root(), n); },
    setText: (s) => {
      Transforms.select(editor, { anchor: Editor.start(editor, []), focus: Editor.end(editor, []) });
      Transforms.delete(editor);
      if (s) Transforms.insertText(editor, s);
      ReactEditor.focus(editor);
    },
    focus: () => ReactEditor.focus(editor),
  });
}, 100);
