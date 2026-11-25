import Quill from "quill";
import { cpIndex, install, domCaret, setDomCaret, utf16Index } from "../bridge.js";

const host = document.getElementById("editor-host");
const mount = document.createElement("div");
mount.id = "editor";
host.appendChild(mount);
const quill = new Quill(mount, { theme: false, formats: [] });
const text = () => quill.getText().replace(/\n$/, "");

install({
  getText: text,
  getCaret: () => domCaret(quill.root),
  setCaret: (n) => { quill.focus(); setDomCaret(quill.root, n); },
  setText: (s) => { quill.setText(s, "user"); quill.setSelection(utf16Index(text(), cpIndex(s, s.length)), 0, "user"); },
  focus: () => quill.focus(),
});
