// The overlay: status icon, strip, drawer / select-anything panel, ghost text and the
// "Rewrite as…" entry, rendered from the scene glanced sends. No decisions are made here:
// what to show, the text, the colours and the places all come from Python.
import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const FONT = "Inter, Cantarell, 'Noto Sans', 'DejaVu Sans', sans-serif";
const ICON = 22;
const ICON_X = 1, ICON_Y = 5;
const ICON_BOX = [30, 28];
const BADGE_C = [23, 7], BADGE_R = 6.5;
const SPIN_MS = 125;
const HOVER_MS = 350;
const ASK_RETURN_MS = 1500;
const SPARKLE = [[12, 3], [13.8, 7.7], [18.5, 9.5], [13.8, 11.3], [12, 16], [10.2, 11.3],
    [5.5, 9.5], [10.2, 7.7]];
const LINE_FIXES = 3;
const MAX_CHOICES = 5;

function rgb(hex) {
    const h = (hex || '#000000').replace('#', '');
    return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16) / 255);
}

function esc(text) {
    return GLib.markup_escape_text(text ?? '', -1);
}

function vbox(params = {}) {
    const box = new St.BoxLayout(params);
    if (box.orientation !== undefined)
        box.orientation = Clutter.Orientation.VERTICAL;
    else
        box.vertical = true;
    return box;
}

function hbox(params = {}) {
    return new St.BoxLayout(params);
}

// A click target that never takes the keyboard: St.Button and Clutter.ClickAction grab the
// pointer on press, and on Wayland a shell grab makes the focused application lose the
// keyboard (its field blurs). A plain reactive actor gets the release without any grab.
let presses = 0, releases = 0;

function clickable(actor, onClick) {
    actor.reactive = true;
    actor.track_hover = true;
    actor.can_focus = false;
    // the generic `event` signal: the per-type signals are gone from newer Clutter
    actor.connect('event', (_a, event) => {
        const type = event.type();
        if (type === Clutter.EventType.BUTTON_PRESS) {
            presses++;
            return Clutter.EVENT_STOP;
        }
        if (type === Clutter.EventType.BUTTON_RELEASE) {
            releases++;
            if (event.get_button() === Clutter.BUTTON_PRIMARY)
                onClick();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    });
    return actor;
}

function textButton(label, params = {}) {
    const bin = new St.Bin(params);
    const text = new St.Label({text: label, y_align: Clutter.ActorAlign.CENTER});
    text.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    bin.set_child(text);
    return bin;
}

export class Overlay {
    constructor(cb) {
        this._cb = cb;
        this._scene = null;
        this._t = {};
        this._named = new Map();
        this._keys = {status: '', strip: '', panel: '', ghost: ''};
        this._phase = 0;
        this._spin = 0;
        this._hover = 0;
        this._resizing = false;
        this._details = false;
        this._profiles = false;
        this._ask = null;
        this._sent = '';

        this._icon = new St.Bin({style: 'padding: 0;'});
        this._iconArea = new St.DrawingArea({width: ICON_BOX[0], height: ICON_BOX[1]});
        this._iconArea.connect('repaint', a => this._drawIcon(a));
        this._icon.set_child(this._iconArea);
        clickable(this._icon, () => this._cb.clicked('status', {}));
        this._strip = new St.Bin({reactive: true});
        this._panel = new St.Bin({reactive: true});
        this._ghost = new St.Label({reactive: false});
        for (const actor of [this._ghost, this._strip, this._panel, this._icon]) {
            actor.hide();
            Main.layoutManager.addTopChrome(actor);
        }
        this._named.set('status', this._icon);
    }

    destroy() {
        this._closeAsk(false);
        if (this._spin)
            GLib.source_remove(this._spin);
        if (this._hover)
            GLib.source_remove(this._hover);
        for (const actor of [this._ghost, this._strip, this._panel, this._icon]) {
            Main.layoutManager.removeChrome(actor);
            actor.destroy();
        }
    }

    // ------------------------------------------------------------------ the scene

    render(scene) {
        this._scene = scene;
        this._t = scene.tokens || {};
        this._resizing = false;
        const tokensKey = JSON.stringify(this._t);
        this._renderStatus(scene.status, tokensKey);
        this._renderStrip(scene.strip, tokensKey);
        this._renderPanel(scene.panel, tokensKey);
        this._renderGhost(scene.ghost, tokensKey);
        this._place();
        this._reportSizes();
    }

    _delta(piece) {
        // The places were computed against the window's frame in the scene; when the window
        // has moved since, everything that follows it moves along.
        const win = this._scene?.window;
        if (!piece || piece.follow === false || !win)
            return [0, 0];
        const now = this._cb.windowFrame(win.id);
        if (!now)
            return [0, 0];
        return [now[0] - win.frame[0], now[1] - win.frame[1]];
    }

    _place() {
        const s = this._scene;
        if (!s)
            return;
        const pieces = [[this._icon, s.status], [this._strip, s.strip],
            [this._panel, s.panel], [this._ghost, s.ghost]];
        for (const [actor, piece] of pieces) {
            if (!piece || this._resizing && piece.follow !== false) {
                actor.hide();
                continue;
            }
            const [dx, dy] = this._delta(piece);
            actor.set_position(Math.round(piece.x + dx), Math.round(piece.y + dy));
            if (!actor.visible) {
                actor.show();
                actor.get_parent()?.set_child_above_sibling(actor, null);
            }
        }
        this._animate();
    }

    windowMoved(info, resized) {
        const win = this._scene?.window;
        if (!win || !info || info.id !== win.id)
            return;
        if (resized)
            this._resizing = true; // hidden until glanced places it again
        this._place();
    }

    _reportSizes() {
        const s = this._scene;
        const sizes = {};
        if (s.strip && this._strip.get_child())
            sizes.strip_h = Math.ceil(this._strip.get_child().get_preferred_height(s.strip.w)[1]);
        if (s.panel && this._panel.get_child())
            sizes.panel_h = Math.ceil(this._panel.get_child().get_preferred_height(s.panel.w)[1]);
        const key = JSON.stringify(sizes);
        const assumed = s.sizes || {};
        const differs = Object.keys(sizes).some(k => Math.abs((assumed[k] ?? -1) - sizes[k]) > 1);
        if (differs && key !== this._sent) {
            this._sent = key;
            this._cb.sized(sizes);
        }
    }

    // ------------------------------------------------------------------ styles

    _font(px = 14, extra = '') {
        return `font-family: ${FONT}; font-size: ${px}px; ${extra}`;
    }

    _label(text, colour, px = 13, extra = '', wrap = true) {
        const label = new St.Label({text: text ?? '', y_align: Clutter.ActorAlign.CENTER});
        label.style = this._font(px, `color: ${this._t[colour] ?? colour}; ${extra}`);
        const ct = label.clutter_text;
        if (wrap) {
            ct.line_wrap = true;
            ct.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
            ct.ellipsize = Pango.EllipsizeMode.NONE;
        } else {
            ct.ellipsize = Pango.EllipsizeMode.END;
        }
        return label;
    }

    _button(label, kind, onClick, name = null) {
        const t = this._t;
        const styles = {
            primary: [`background-color: ${t.accent}; color: ${t.on_accent}; border-radius: 7px; ` +
                'padding: 6px 10px; font-weight: 600;', `background-color: ${t.accent_hover};`, 12],
            quiet: [`background-color: transparent; color: ${t.body}; border-radius: 7px; padding: 4px 8px;`,
                `background-color: ${t.hover};`, 14],
            link: [`background-color: transparent; color: ${t.muted}; padding: 1px 4px; border-radius: 6px;`,
                `color: ${t.accent};`, 12],
            strong: [`background-color: transparent; color: ${t.accent}; font-weight: 600; padding: 1px 4px;`,
                `color: ${t.accent_hover};`, 12],
            secondary: [`background-color: ${t.card}; color: ${t.text}; border: 1px solid ${t.card_border}; ` +
                'border-radius: 7px; padding: 5px 10px;', `border-color: ${t.accent};`, 12],
            on: [`background-color: ${t.hover}; color: ${t.body}; border-radius: 7px; padding: 4px 8px;`,
                '', 14],
        };
        const [base, hover, px] = styles[kind] ?? styles.link;
        const button = textButton(label, {y_align: Clutter.ActorAlign.CENTER});
        const plain = this._font(px, base);
        button.style = plain;
        button.connect('notify::hover', () => {
            button.style = button.hover ? plain + hover : plain;
        });
        clickable(button, onClick);
        if (name)
            this._named.set(name, button);
        return button;
    }

    _act(action, args = {}) {
        return () => this._cb.clicked('action', {action, args});
    }

    _sparkle(warn, size = 16) {
        const area = new St.DrawingArea({width: size, height: size,
            y_align: Clutter.ActorAlign.CENTER});
        area.connect('repaint', a => {
            const cr = a.get_context();
            cr.scale(size / 24, size / 24);
            this._path(cr);
            cr.setSourceRGB(...rgb(this._t[warn ? 'warn_text' : 'accent']));
            cr.setLineWidth(2);
            cr.setLineJoin(Cairo.LineJoin.ROUND);
            cr.stroke();
            cr.$dispose();
        });
        return area;
    }

    _path(cr) {
        cr.moveTo(...SPARKLE[0]);
        for (const p of SPARKLE.slice(1))
            cr.lineTo(...p);
        cr.closePath();
    }

    // ------------------------------------------------------------------ the status icon

    _renderStatus(status, tokensKey) {
        const key = status ? JSON.stringify([status.state, status.badge, status.spin, tokensKey]) : '';
        if (key !== this._keys.status) {
            this._keys.status = key;
            this._iconArea.queue_repaint();
        }
    }

    _animate() {
        const st = this._scene?.status;
        const want = Boolean(st && st.state === 'working' && st.spin && this._icon.visible);
        if (want && !this._spin) {
            this._spin = GLib.timeout_add(GLib.PRIORITY_DEFAULT, SPIN_MS, () => {
                this._phase = (this._phase + 1) % 12;
                this._iconArea.queue_repaint();
                const still = this._scene?.status;
                if (still && still.state === 'working' && still.spin)
                    return GLib.SOURCE_CONTINUE;
                this._spin = 0;
                return GLib.SOURCE_REMOVE;
            });
        } else if (!want && this._spin) {
            GLib.source_remove(this._spin);
            this._spin = 0;
        }
    }

    _drawIcon(area) {
        const cr = area.get_context();
        const st = this._scene?.status;
        if (!st) {
            cr.$dispose();
            return;
        }
        const t = this._t;
        const r = ICON / 2, cx = ICON_X + r, cy = ICON_Y + r, tau = 2 * Math.PI;
        const [fill, ring, mark] = {
            ready: ['accent', 'accent', 'on_accent'],
            muted: ['card', 'card_border', 'dot_quiet'],
        }[st.state] ?? ['strip', 'strip_border', 'muted'];
        cr.arc(cx, cy, r, 0, tau);
        cr.setSourceRGB(...rgb(t[fill]));
        cr.fill();
        cr.arc(cx, cy, r - 0.75, 0, tau);
        cr.setSourceRGB(...rgb(t[ring]));
        cr.setLineWidth(1.5);
        cr.stroke();
        if (st.state === 'working') {
            const start = this._phase * tau / 12;
            cr.arc(cx, cy, r - 1.25, start, start + tau / 4);
            cr.setSourceRGB(...rgb(t.accent));
            cr.setLineWidth(2.5);
            cr.setLineCap(Cairo.LineCap.ROUND);
            cr.stroke();
        }
        cr.save();
        const k = 18 / 24;
        cr.translate(cx - 12 * k, cy - 9.5 * k);
        cr.scale(k, k);
        this._path(cr);
        cr.restore();
        cr.setSourceRGB(...rgb(t[st.state === 'working' ? 'accent' : mark]));
        cr.setLineWidth(1.6);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        cr.stroke();
        const badge = st.badge;
        if (badge) {
            const [bx, by] = BADGE_C;
            cr.arc(bx, by, BADGE_R, 0, tau);
            cr.setSourceRGB(...rgb(t.surface));
            cr.fill();
            cr.arc(bx, by, BADGE_R - 0.5, 0, tau);
            cr.setSourceRGB(...rgb(t.accent));
            cr.setLineWidth(1);
            cr.stroke();
            if (badge === '•') {
                cr.arc(bx, by, 2.2, 0, tau);
                cr.setSourceRGB(...rgb(t.dot_warn));
                cr.fill();
            } else {
                cr.selectFontFace('Sans', Cairo.FontSlant.NORMAL, Cairo.FontWeight.BOLD);
                cr.setFontSize(badge.length === 1 ? 8.5 : 7);
                const ext = cr.textExtents(badge);
                cr.moveTo(bx - ext.width / 2 - ext.xBearing, by - ext.height / 2 - ext.yBearing);
                cr.setSourceRGB(...rgb(t.accent));
                cr.showText(badge);
            }
        }
        cr.$dispose();
    }

    // ------------------------------------------------------------------ the strip

    _renderStrip(strip, tokensKey) {
        const key = strip ? JSON.stringify([strip.view, strip.markup, strip.use_label, strip.w, tokensKey]) : '';
        if (key === this._keys.strip)
            return;
        this._keys.strip = key;
        for (const n of ['use', 'more', 'strip-text'])
            this._named.delete(n);
        this._strip.get_child()?.destroy();
        if (!strip)
            return;
        const v = strip.view, t = this._t;
        const warn = v.guard !== null && v.guard !== undefined && v.segments.length === 0;
        const row = hbox({style: `background-color: ${t[warn ? 'warn_bg' : 'strip']}; ` +
            `border: 1px solid ${t[warn ? 'warn_border' : 'strip_border']}; border-radius: 9px; ` +
            'padding: 8px 10px; spacing: 10px;'});
        row.set_width(strip.w);
        row.add_child(this._sparkle(warn));
        const text = vbox({x_expand: true, y_align: Clutter.ActorAlign.CENTER, style: 'spacing: 2px;'});
        if (v.segments.length > 0) {
            const sentence = this._label('', 'body', 14);
            sentence.clutter_text.set_markup(strip.markup);
            sentence.x_expand = true;
            this._named.set('strip-text', sentence);
            text.add_child(sentence);
        }
        if (v.guard)
            text.add_child(this._label(v.guard, 'warn_text', 13, 'font-weight: 600;'));
        if (v.note && v.segments.length === 0)
            text.add_child(this._label(v.note, 'muted', 13));
        row.add_child(text);
        if (strip.can_use)
            row.add_child(this._button(strip.use_label, 'primary', this._act('use'), 'use'));
        if (v.menu)
            row.add_child(this._button('⋯', v.expanded ? 'on' : 'quiet', this._act('more'), 'more'));
        this._strip.set_child(row);
    }

    // ------------------------------------------------------------------ the drawer

    _renderPanel(panel, tokensKey) {
        const key = panel ? JSON.stringify([panel.view, panel.preview_markup, panel.w,
            this._details, this._profiles, tokensKey]) : '';
        if (key === this._keys.panel)
            return;
        if (!panel) {
            this._details = this._profiles = false;
        }
        this._keys.panel = key;
        this._cancelHover();
        for (const n of [...this._named.keys()]) {
            if (!['status', 'use', 'more', 'strip-text', 'ask-entry'].includes(n))
                this._named.delete(n);
        }
        this._panel.get_child()?.destroy();
        if (!panel)
            return;
        const t = this._t;
        const card = vbox({style: `background-color: ${t.surface}; border: 1px solid ${t.hairline}; ` +
            `border-radius: 12px; padding: 14px 16px; spacing: 10px; box-shadow: 0 6px 18px ${t.shadow};`});
        card.set_width(panel.w);
        if (panel.view.mode === 'selection')
            this._fillSelection(card, panel);
        else
            this._fillDrawer(card, panel);
        this._panel.set_child(card);
    }

    _rerender() {
        this._keys.panel = '';
        if (this._scene) {
            this._renderPanel(this._scene.panel, JSON.stringify(this._t));
            this._place();
            this._reportSizes();
        }
    }

    _header(v, title) {
        const header = hbox({style: 'spacing: 6px;'});
        if (title)
            header.add_child(this._label(title, 'text', 14, 'font-weight: 600;', false));
        if (v.tone) {
            let line = `Reads ${v.tone.toLowerCase()}`;
            if (v.tone_reason)
                line += ` — ${v.tone_reason}`;
            const tone = this._label(line, 'muted', 12);
            tone.x_expand = true;
            header.add_child(tone);
        } else {
            header.add_child(new St.Widget({x_expand: true}));
        }
        if (v.profile_label) {
            if (v.mode === 'selection') {
                header.add_child(this._label(`For ${v.profile_label}`, 'muted', 12, '', false));
            } else {
                header.add_child(this._button(`For ${v.profile_label} ▾`, 'link', () => {
                    this._profiles = !this._profiles;
                    this._rerender();
                }, 'profiles'));
            }
        }
        header.add_child(this._button('Close', 'link', this._act('close'), 'close'));
        return header;
    }

    _card(chip, action) {
        const t = this._t;
        const base = `background-color: ${t.card}; border: 1px solid ${t.card_border}; ` +
            'border-radius: 9px; padding: 9px 10px;';
        const button = new St.Bin({x_expand: true});
        button.style = base;
        const box = vbox({style: 'spacing: 3px;'});
        const top = hbox({style: 'spacing: 6px;'});
        const title = this._label(chip.label, 'text', 13, 'font-weight: 600;', false);
        title.x_expand = true;
        top.add_child(title);
        if (chip.hotkey !== null && chip.hotkey !== undefined) {
            top.add_child(this._label(String(chip.hotkey), 'muted', 11,
                `border: 1px solid ${t.card_border}; border-radius: 4px; padding: 0 4px; ` +
                `background-color: ${t.surface};`, false));
        }
        box.add_child(top);
        let line = 'Hover to preview';
        if (chip.state === 'loading' && !chip.preview)
            line = 'Rewriting…';
        else if (chip.state === 'failed')
            line = 'Did not work';
        else if (chip.preview)
            line = chip.preview.split(/\s+/).join(' ');
        box.add_child(this._label(line, 'muted', 12, '', false));
        button.set_child(box);
        button.connect('notify::hover', () => {
            button.style = button.hover ? `${base} border-color: ${t.accent};` : base;
            this._cancelHover();
            if (button.hover) {
                this._hover = GLib.timeout_add(GLib.PRIORITY_DEFAULT, HOVER_MS, () => {
                    this._hover = 0;
                    this._cb.clicked('action', {action: 'tone_hover', args: {key: chip.key}});
                    return GLib.SOURCE_REMOVE;
                });
            }
        });
        clickable(button, this._act(action, {key: chip.key}));
        this._named.set(`card:${chip.key}`, button);
        return button;
    }

    _cancelHover() {
        if (this._hover) {
            GLib.source_remove(this._hover);
            this._hover = 0;
        }
    }

    _cards(chips, action) {
        const row = hbox({style: 'spacing: 8px;'});
        for (const chip of chips)
            row.add_child(this._card(chip, action));
        return row;
    }

    _rewriteAs() {
        const t = this._t;
        const row = hbox({style: 'spacing: 8px;'});
        row.add_child(this._label('Rewrite as…', 'muted', 12, '', false));
        const base = `background-color: ${t.surface}; border: 1px solid ${t.hairline}; ` +
            'border-radius: 8px; padding: 7px 10px;';
        const field = new St.Bin({x_expand: true});
        field.style = base;
        field.set_child(this._label(this._scene?.ask_placeholder ?? '', 'muted', 13, '', false));
        field.connect('notify::hover', () => {
            field.style = field.hover ? `${base} border-color: ${t.accent};` : base;
        });
        clickable(field, this._act('tone', {key: 'custom'}));
        this._named.set('rewrite-as', field);
        row.add_child(field);
        return row;
    }

    _banner(text) {
        const t = this._t;
        const box = hbox({style: `background-color: ${t.warn_bg}; border: 1px solid ${t.warn_border}; ` +
            'border-radius: 9px; padding: 8px 10px; spacing: 8px;'});
        const label = this._label(text, 'warn_text', 13);
        label.x_expand = true;
        box.add_child(label);
        return box;
    }

    _fixLine(v) {
        const line = hbox({style: 'spacing: 2px;'});
        const n = v.issues.length;
        line.add_child(this._button(`${n} fix${n === 1 ? '' : 'es'}`, 'link', () => {
            this._details = !this._details;
            this._rerender();
        }, 'details'));
        const shown = v.issues.filter(r => r.replacements.length > 0).slice(0, LINE_FIXES);
        for (const row of shown) {
            line.add_child(this._label('·', 'muted', 12, '', false));
            const flagged = (row.flagged || row.excerpt).replace(/\n/g, ' ');
            line.add_child(this._button(`${flagged} → ${row.replacements[0] || '(remove)'}`, 'link',
                this._act('accept', {issue_id: row.issue_id, choice: 0}), `fix:${row.issue_id}`));
        }
        const rest = n - shown.length;
        if (rest > 0) {
            line.add_child(this._label('·', 'muted', 12, '', false));
            line.add_child(this._button(`+${rest} more`, 'link', () => {
                this._details = !this._details;
                this._rerender();
            }));
        }
        line.add_child(new St.Widget({x_expand: true}));
        if (v.can_fix_all)
            line.add_child(this._button('Fix all', 'strong', this._act('fix_all'), 'fix_all'));
        return line;
    }

    _issueRow(row) {
        const box = vbox({style: 'spacing: 4px;'});
        box.add_child(this._label(row.excerpt.replace(/\n/g, ' '), 'text', 13));
        if (row.message)
            box.add_child(this._label(row.message, 'muted', 12));
        const fixes = hbox({style: 'spacing: 6px;'});
        row.replacements.slice(0, MAX_CHOICES).forEach((rep, choice) => {
            fixes.add_child(this._button(rep || 'Remove', 'secondary',
                this._act('accept', {issue_id: row.issue_id, choice})));
        });
        if (row.spelling)
            fixes.add_child(this._button('Add to dictionary', 'link',
                this._act('add_dict', {issue_id: row.issue_id})));
        fixes.add_child(this._button('Ignore here', 'link',
            this._act('ignore', {issue_id: row.issue_id, scope: 'app'})));
        fixes.add_child(this._button('Ignore everywhere', 'link',
            this._act('ignore', {issue_id: row.issue_id, scope: 'global'})));
        box.add_child(fixes);
        return box;
    }

    _scroll(child, maxH) {
        const view = new St.ScrollView({style: `max-height: ${maxH}px;`, x_expand: true});
        view.set_policy(St.PolicyType.NEVER, St.PolicyType.AUTOMATIC);
        if (view.set_child)
            view.set_child(child);
        else
            view.add_actor(child);
        return view;
    }

    _fillDrawer(card, panel) {
        const v = panel.view;
        if (v.guard) {
            const banner = this._banner(v.guard);
            banner.add_child(this._button("Don't warn about this here", 'link',
                this._act('guard_silence', {scope: 'app'})));
            banner.add_child(this._button('Never', 'link', this._act('guard_silence', {scope: 'global'})));
            card.add_child(banner);
        }
        card.add_child(this._header(v, null));
        if (v.profile_label && v.profiles.length && this._profiles) {
            const menu = hbox({style: 'spacing: 6px;'});
            for (const [name, label] of v.profiles)
                menu.add_child(this._button(label, 'secondary', this._act('profile', {name})));
            card.add_child(menu);
        }
        const cards = v.tone_chips.filter(c => c.key !== 'custom');
        if (cards.length)
            card.add_child(this._cards(cards, 'tone_apply'));
        if (v.tone_chips.some(c => c.key === 'custom'))
            card.add_child(this._rewriteAs());
        if (v.banner)
            card.add_child(this._banner(v.banner));
        if (v.copy_offer !== null && v.copy_offer !== undefined) {
            const rewrite = v.preview_state !== null && v.preview_state !== undefined;
            const offer = hbox({style: 'spacing: 8px;'});
            const label = this._label(rewrite ? 'The rewrite was not typed into the field.'
                : 'The change could not be applied here.', 'muted', 12);
            label.x_expand = true;
            offer.add_child(label);
            offer.add_child(this._button(rewrite ? 'Copy rewrite' : 'Copy instead', 'secondary',
                this._act('copy'), 'copy'));
            card.add_child(offer);
        }
        if (v.issues.length) {
            card.add_child(this._fixLine(v));
            if (this._details) {
                const rows = vbox({style: 'spacing: 10px;'});
                for (const row of v.issues)
                    rows.add_child(this._issueRow(row));
                card.add_child(this._scroll(rows, 240));
            }
        } else if (!(v.banner || (v.copy_offer !== null && v.copy_offer !== undefined) ||
                     v.tone_chips.length || v.guard)) {
            card.add_child(this._label('Nothing to fix here.', 'muted', 12));
        }
    }

    _fillSelection(card, panel) {
        const v = panel.view, t = this._t;
        card.add_child(this._header(v, 'Selected text'));
        const cards = v.tone_chips.filter(c => c.key !== 'custom');
        if (cards.length)
            card.add_child(this._cards(cards, 'tone'));
        if (v.tone_chips.some(c => c.key === 'custom'))
            card.add_child(this._rewriteAs());
        if (panel.preview_markup !== null && panel.preview_markup !== undefined) {
            const label = this._label('', 'body', 13);
            label.clutter_text.set_markup(panel.preview_markup);
            const box = new St.Bin({style: `background-color: ${t.card}; border: 1px solid ` +
                `${t.card_border}; border-radius: 9px; padding: 8px 10px;`, x_expand: true});
            box.set_child(label);
            card.add_child(this._scroll(box, 150));
        }
        if (v.preview_state === 'loading')
            card.add_child(this._label('Rewriting…', 'muted', 12));
        if (v.banner)
            card.add_child(this._banner(v.banner));
        if (v.issues.length) {
            const rows = vbox({style: 'spacing: 8px;'});
            for (const row of v.issues) {
                const item = vbox({style: 'spacing: 2px;'});
                item.add_child(this._label(row.excerpt.replace(/\n/g, ' '), 'text', 13));
                if (row.message)
                    item.add_child(this._label(row.message, 'muted', 12));
                rows.add_child(item);
            }
            card.add_child(this._scroll(rows, 240));
        }
        const footer = hbox({style: 'spacing: 8px;'});
        footer.add_child(new St.Widget({x_expand: true}));
        if (v.can_paste)
            footer.add_child(this._button('Paste over selection', 'secondary', this._act('sel_paste'), 'sel_paste'));
        footer.add_child(this._button('Copy rewrite', 'primary', this._act('sel_copy'), 'sel_copy'));
        card.add_child(footer);
    }

    // ------------------------------------------------------------------ the ghost

    _renderGhost(ghost, tokensKey) {
        const key = ghost ? JSON.stringify([ghost.text, ghost.font_px, ghost.max_width, tokensKey]) : '';
        if (key === this._keys.ghost)
            return;
        this._keys.ghost = key;
        if (!ghost)
            return;
        this._ghost.text = ghost.text;
        this._ghost.style = this._font(ghost.font_px, `color: ${this._t.ghost};`);
        this._ghost.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._ghost.set_width(-1);
        if (ghost.max_width > 0 && this._ghost.get_preferred_width(-1)[1] > ghost.max_width)
            this._ghost.set_width(ghost.max_width);
    }

    // ------------------------------------------------------------------ "Rewrite as…"

    ask({prompt, placeholder}, window) {
        this._closeAsk(false);
        const t = this._t;
        const box = vbox({reactive: true, style: `background-color: ${t.surface}; border: 1px solid ` +
            `${t.hairline}; border-radius: 12px; padding: 12px 14px; spacing: 6px; ` +
            `box-shadow: 0 6px 18px ${t.shadow};`});
        box.add_child(this._label(prompt ?? '', 'muted', 12));
        const entry = new St.Entry({hint_text: placeholder ?? '', can_focus: true,
            style: this._font(13, `color: ${t.text}; background-color: ${t.surface}; ` +
                `border: 1px solid ${t.accent}; border-radius: 8px; padding: 7px 10px; width: 360px;`)});
        entry.clutter_text.max_length = 300;
        box.add_child(entry);
        Main.layoutManager.addTopChrome(box);
        const anchor = this._named.get('rewrite-as');
        let x, y;
        if (anchor && anchor.mapped) {
            [x, y] = anchor.get_transformed_position();
            x -= 14;
            y -= 30;
        } else if (this._panel.visible) {
            [x, y] = this._panel.get_transformed_position();
        } else {
            const m = Main.layoutManager.primaryMonitor;
            x = m.x + m.width / 2 - 190;
            y = m.y + m.height / 3;
        }
        box.set_position(Math.round(x), Math.round(y));
        // The one place Glance takes the keyboard: a modal grab inside the shell, so the
        // application window keeps being the focused window underneath.
        const grab = Main.pushModal(box, {});
        this._ask = {box, entry, grab, window};
        this._named.set('ask-entry', entry);
        entry.clutter_text.connect('activate', () => this._closeAsk(true));
        entry.clutter_text.connect('event', (_a, event) => {
            if (event.type() === Clutter.EventType.KEY_PRESS &&
                event.get_key_symbol() === Clutter.KEY_Escape) {
                this._closeAsk(false);
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
        global.stage.set_key_focus(entry.clutter_text);
    }

    askText(text, submit) {
        // test hook: what the user would type into the entry, and Enter
        if (!this._ask)
            return false;
        this._ask.entry.set_text(text);
        if (submit)
            this._closeAsk(true);
        return true;
    }

    _closeAsk(submit) {
        const ask = this._ask;
        this._ask = null;
        if (!ask)
            return;
        const text = ask.entry.get_text();
        Main.popModal(ask.grab);
        Main.layoutManager.removeChrome(ask.box);
        ask.box.destroy();
        this._named.delete('ask-entry');
        const target = ask.window;
        if (target && global.display.focus_window !== target)
            Main.activateWindow(target);
        if (!submit)
            return;
        const deadline = GLib.get_monotonic_time() + ASK_RETURN_MS * 1000;
        const check = () => {
            const back = Boolean(target) && global.display.focus_window === target;
            if (!back && target && GLib.get_monotonic_time() < deadline)
                return GLib.SOURCE_CONTINUE;
            this._cb.askDone({text, focus_returned: back});
            return GLib.SOURCE_REMOVE;
        };
        if (check() === GLib.SOURCE_CONTINUE)
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 20, check);
    }

    // ------------------------------------------------------------------ test access

    actorFor(name) {
        return this._named.get(name) ?? null;
    }

    describe() {
        const geo = a => {
            if (!a.visible)
                return null;
            const [x, y] = a.get_transformed_position();
            const [w, h] = a.get_transformed_size();
            return [Math.round(x), Math.round(y), Math.round(w), Math.round(h)];
        };
        const s = this._scene;
        const text = this._named.get('strip-text');
        return {
            status: geo(this._icon) && s?.status ? {rect: geo(this._icon), state: s.status.state,
                badge: s.status.badge, spinning: Boolean(this._spin)} : null,
            strip: geo(this._strip) && s?.strip ? {rect: geo(this._strip),
                text: text && text.mapped ? text.clutter_text.get_text() : null} : null,
            panel: geo(this._panel) ? {rect: geo(this._panel), mode: s?.panel?.view?.mode} : null,
            ghost: geo(this._ghost) ? {rect: geo(this._ghost)} : null,
            ask: Boolean(this._ask),
            named: [...this._named.keys()].filter(n => this._named.get(n).mapped),
            key_focus: String(global.stage.get_key_focus()),
            modal: Main.modalCount,
            presses,
            releases,
            grab: String(global.stage.get_grab_actor?.()),
            action_mode: Main.actionMode,
            focus: global.display.focus_window ? String(global.display.focus_window.get_id()) : null,
        };
    }
}
