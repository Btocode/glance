import { baseKeymap } from "prosemirror-commands";
import { keymap } from "prosemirror-keymap";
import { Schema } from "prosemirror-model";
import { schema as basic } from "prosemirror-schema-basic";
import { EditorState, TextSelection } from "prosemirror-state";
import { EditorView } from "prosemirror-view";
import { install, domCaret, setDomCaret } from "../bridge.js";

// A mention is an inline atom rendered as a non-editable span. role="img" makes it an
// embedded object in the browser's accessibility text (U+FFFC), like an emoji image or a chip.
const schema = new Schema({
  nodes: basic.spec.nodes.addToEnd("mention", {
    group: "inline", inline: true, atom: true, selectable: true,
    attrs: { name: { default: "alice" } },
    toDOM: (node) => ["span", { contenteditable: "false", class: "mention", role: "img", "aria-label": "@" + node.attrs.name }, "@" + node.attrs.name],
    parseDOM: [{ tag: "span.mention" }],
  }),
  marks: basic.spec.marks,
});

const host = document.getElementById("editor-host");
const mount = document.createElement("div");
mount.id = "editor";
host.appendChild(mount);

const withMention = new URLSearchParams(location.search).has("mention");
const initial = withMention
  ? schema.node("doc", null, [schema.node("paragraph", null, [
      schema.text("Hi "), schema.node("mention", { name: "alice" }), schema.text(" teh plan"),
    ])])
  : undefined;
const view = new EditorView(mount, {
  state: EditorState.create({ schema, doc: initial, plugins: [keymap(baseKeymap)] }),
});
const text = () => view.state.doc.textBetween(0, view.state.doc.content.size, "\n", "￼");

install({
  getText: text,
  getCaret: () => domCaret(view.dom),
  setCaret: (n) => { view.focus(); setDomCaret(view.dom, n); },
  setText: (s) => {
    const para = schema.node("paragraph", null, s ? [schema.text(s)] : []);
    const tr = view.state.tr.replaceWith(0, view.state.doc.content.size, para);
    view.dispatch(tr.setSelection(TextSelection.atEnd(tr.doc)));
    view.focus();
    document.dispatchEvent(new Event("selectionchange"));
  },
  focus: () => view.focus(),
});
