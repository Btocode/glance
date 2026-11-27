// Glance Shot in GNOME Shell: the region picker, the capture and the shot bar.
//
// glanced decides everything (glance/shot/*.py); this draws what it is told and reports
// back. The picker follows the same rules as glance/shot/picker.py: drag a rectangle (a
// click is not one), Enter = the monitor under the pointer, Space = the window under it,
// Esc or a right click = cancel. The capture is Shell.Screenshot.screenshot_area into memory
// (no file, no portal dialog), handed only to the verified glanced (extension.js checks the
// caller's role); the image is never kept here once it was sent.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {clickable} from './overlay.js';

const MIN_SIDE = 4;            // geometry.MIN_SIDE
const LABEL_OFFSET = [16, 18]; // geometry.LABEL_OFFSET
const BAR_GAP = 8;             // geometry.BAR_GAP
const DIM = 'rgba(8, 13, 18, 0.45)';
const BAR_W = 460;           // the card (shot_gtk.BAR_W)
const BODY_MIN_H = 132;       // the body keeps this height: the card barely resizes per step
const RESULT_MAX_H = 260;
const FRAME_PAD = 3;          // the region's outline sits just outside it
const CAPTURE_DELAY_MS = 80;   // the picker is gone from the screen by then

function rect(x, y, w, h) {
    return {x, y, w, h};
}

function intersect(a, b) {
    const x0 = Math.max(a.x, b.x), y0 = Math.max(a.y, b.y);
    const x1 = Math.min(a.x + a.w, b.x + b.w), y1 = Math.min(a.y + a.h, b.y + b.h);
    return x1 <= x0 || y1 <= y0 ? null : rect(x0, y0, x1 - x0, y1 - y0);
}

function inside(r, x, y) {
    return r.x <= x && x < r.x + r.w && r.y <= y && y < r.y + r.h;
}

const CARD_RESERVE_H = 420;     // geometry.CARD_RESERVE_H

// geometry.bar_position: beside the region, never over it while there is room. Right of it
// (tops aligned), else left, else under, else above, else under or above with room for its
// present height only, else inside its top-right corner. The side is picked for the card's
// full height (reserveH), so it does not jump sides as a result fills it.
export function barPosition(region, size, monitors, reserveH = CARD_RESERVE_H) {
    const [w, h] = size;
    const tall = Math.max(h, reserveH);
    const cx = region.x + Math.floor(region.w / 2), cy = region.y + Math.floor(region.h / 2);
    const mon = monitors.find(m => inside(m, cx, cy)) ?? monitors[0] ??
        rect(region.x, region.y, region.w + w + BAR_GAP, Math.max(region.h, tall));
    const right = mon.x + mon.w, bottom = mon.y + mon.h;
    const fitsH = Math.min(tall, mon.h);
    let x, y;
    if (region.x + region.w + BAR_GAP + w <= right)
        [x, y] = [region.x + region.w + BAR_GAP, region.y];
    else if (region.x - BAR_GAP - w >= mon.x)
        [x, y] = [region.x - BAR_GAP - w, region.y];
    else if (region.y + region.h + BAR_GAP + fitsH <= bottom)
        [x, y] = [region.x, region.y + region.h + BAR_GAP];
    else if (region.y - BAR_GAP - fitsH >= mon.y)
        [x, y] = [region.x, region.y - BAR_GAP - h];
    else if (region.y + region.h + BAR_GAP + h <= bottom) // room for it as it is, not grown
        [x, y] = [region.x + region.w - w, region.y + region.h + BAR_GAP];
    else if (region.y - BAR_GAP - h >= mon.y)
        [x, y] = [region.x + region.w - w, region.y - BAR_GAP - h];
    else
        [x, y] = [region.x + region.w - w - 2 * BAR_GAP, region.y + 2 * BAR_GAP];
    x = Math.max(mon.x, Math.min(x, right - w));
    y = Math.max(mon.y, Math.min(y, bottom - h));
    return [x, y];
}

export class ShotUi {
    // cb: picked(result), clicked(action)
    constructor(cb) {
        this._cb = cb;
        this._pick = null;      // the picker's state while it is up
        this._bar = null;       // the card, one actor while the shot is up
        this._frame = null;     // the region's outline
        this._barKey = '';
        this._barRect = null;
        this._lastView = null;
        this._named = new Map();
    }

    destroy() {
        this.cancel();
        this.hideBar();
    }

    // ------------------------------------------------------------------ the picker

    _monitors() {
        return Main.layoutManager.monitors.map(m => rect(m.x, m.y, m.width, m.height));
    }

    // Normal windows on screen now, topmost first, with their frames.
    _windows() {
        const ws = global.workspace_manager.get_active_workspace();
        const all = global.display.sort_windows_by_stacking(global.display.list_all_windows());
        return all.reverse().filter(w => !w.minimized && w.showing_on_its_workspace?.() !== false &&
            (w.is_on_all_workspaces() || w.get_workspace() === ws) &&
            !w.is_override_redirect?.()).map(w => {
            const f = w.get_frame_rect();
            return {rect: [f.x, f.y, f.width, f.height], wm_class: w.get_wm_class() || '',
                title: w.get_title() || '', pid: w.get_pid(), id: String(w.get_id())};
        });
    }

    pick({tokens = {}, hint = '', look = {}} = {}) {
        this.cancel();
        const monitors = this._monitors();
        const x0 = Math.min(...monitors.map(m => m.x)), y0 = Math.min(...monitors.map(m => m.y));
        const x1 = Math.max(...monitors.map(m => m.x + m.w));
        const y1 = Math.max(...monitors.map(m => m.y + m.h));
        const screen = rect(x0, y0, x1 - x0, y1 - y0);
        const windows = this._windows();
        const t = tokens;
        const root = new St.Widget({reactive: true, can_focus: true, x: screen.x, y: screen.y,
            width: screen.w, height: screen.h});
        const dims = [0, 1, 2, 3].map(() => new St.Widget({style: `background-color: ${DIM};`}));
        for (const d of dims)
            root.add_child(d);
        const border = new St.Widget({style: `border: 2px solid ${t.accent_fill ?? '#3ECF8E'};`});
        root.add_child(border);
        const pill = (mono) => {
            const l = new St.Label({text: ''});
            l.style = `font-family: ${mono ? look.mono ?? 'monospace' : look.font ?? 'sans-serif'}; ` +
                `font-size: ${mono ? 12 : 13}px; color: ${t.text ?? '#e6edf3'}; ` +
                `background-color: ${t.surface ?? '#151b23'}; border: 1px solid ${t.hairline ?? '#2a3340'}; ` +
                'border-radius: 999px; padding: 4px 10px;';
            l.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
            root.add_child(l);
            return l;
        };
        const label = pill(true);
        const hintLabel = pill(false);
        hintLabel.text = hint;
        Main.layoutManager.addTopChrome(root);
        const [px, py] = global.get_pointer();
        this._pick = {root, dims, border, label, hintLabel, screen, monitors, windows,
            pointer: [px, py], start: null, selection: null, done: false, grab: null};
        try {
            this._pick.grab = Main.pushModal(root, {actionMode: Shell.ActionMode.NORMAL});
        } catch (e) {
            logError(e, 'Glance: shot modal');
        }
        global.stage.set_key_focus(root);
        root.connect('event', (_a, event) => this._event(event));
        this._layout();
        return {ok: true};
    }

    _event(event) {
        const p = this._pick;
        if (!p || p.done)
            return Clutter.EVENT_PROPAGATE;
        const type = event.type();
        if (type === Clutter.EventType.KEY_PRESS) {
            const sym = event.get_key_symbol();
            if (sym === Clutter.KEY_Escape)
                this._finish(null, 'cancel');
            else if (sym === Clutter.KEY_Return || sym === Clutter.KEY_KP_Enter)
                this._finish(p.monitors.find(m => inside(m, ...p.pointer)) ?? p.monitors[0], 'screen');
            else if (sym === Clutter.KEY_space) {
                const w = this._windowUnder();
                if (w)
                    this._finish(w, 'window');
            }
            return Clutter.EVENT_STOP;
        }
        if (type === Clutter.EventType.BUTTON_PRESS) {
            const [x, y] = event.get_coords();
            if (event.get_button() === Clutter.BUTTON_SECONDARY) {
                this._finish(null, 'cancel');
                return Clutter.EVENT_STOP;
            }
            p.pointer = [Math.round(x), Math.round(y)];
            p.start = p.pointer;
            p.selection = null;
            this._layout();
            return Clutter.EVENT_STOP;
        }
        if (type === Clutter.EventType.MOTION) {
            const [x, y] = event.get_coords();
            this._motion(Math.round(x), Math.round(y));
            return Clutter.EVENT_STOP;
        }
        if (type === Clutter.EventType.BUTTON_RELEASE) {
            if (event.get_button() !== Clutter.BUTTON_PRIMARY || !p.start)
                return Clutter.EVENT_STOP;
            const [x, y] = event.get_coords();
            this._motion(Math.round(x), Math.round(y));
            const sel = p.selection;
            p.start = null;
            p.selection = null;
            if (sel)
                this._finish(sel, 'drag');
            else
                this._layout();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    _motion(x, y) {
        const p = this._pick;
        p.pointer = [x, y];
        if (p.start) {
            const r = rect(Math.min(p.start[0], x), Math.min(p.start[1], y),
                Math.abs(x - p.start[0]), Math.abs(y - p.start[1]));
            p.selection = r.w >= MIN_SIDE && r.h >= MIN_SIDE ? intersect(r, p.screen) : null;
        }
        this._layout();
    }

    _windowUnder() {
        const p = this._pick;
        for (const w of p.windows) {
            const r = rect(...w.rect);
            if (inside(r, ...p.pointer)) {
                const cut = intersect(r, p.screen);
                return cut && cut.w >= MIN_SIDE && cut.h >= MIN_SIDE ? cut : null;
            }
        }
        return null;
    }

    _layout() {
        const p = this._pick;
        if (!p)
            return;
        const s = p.screen;
        const lit = p.selection ?? (p.start ? null : this._windowUnder());
        const [top, bottom, left, right] = p.dims;
        const place = (a, x, y, w, h) => {
            a.set_position(x - s.x, y - s.y);
            a.set_size(Math.max(0, w), Math.max(0, h));
        };
        if (!lit) {
            place(top, s.x, s.y, s.w, s.h);
            for (const a of [bottom, left, right])
                a.set_size(0, 0);
            p.border.hide();
        } else {
            place(top, s.x, s.y, s.w, lit.y - s.y);
            place(bottom, s.x, lit.y + lit.h, s.w, s.y + s.h - lit.y - lit.h);
            place(left, s.x, lit.y, lit.x - s.x, lit.h);
            place(right, lit.x + lit.w, lit.y, s.x + s.w - lit.x - lit.w, lit.h);
            place(p.border, lit.x, lit.y, lit.w, lit.h);
            p.border.show();
            p.border.style = p.border.style.replace(/solid|dashed/, p.selection ? 'solid' : 'dashed');
        }
        if (p.selection) {
            p.label.text = `${p.selection.w} × ${p.selection.h}`;
            p.label.show();
            p.hintLabel.hide();
            const [, lw] = p.label.get_preferred_width(-1);
            const [, lh] = p.label.get_preferred_height(-1);
            let x = p.pointer[0] + LABEL_OFFSET[0], y = p.pointer[1] + LABEL_OFFSET[1];
            if (x + lw > s.x + s.w)
                x = p.pointer[0] - LABEL_OFFSET[0] - lw;
            if (y + lh > s.y + s.h)
                y = p.pointer[1] - LABEL_OFFSET[1] - lh;
            p.label.set_position(Math.max(s.x, x) - s.x, Math.max(s.y, y) - s.y);
        } else {
            p.label.hide();
            p.hintLabel.show();
            const mon = p.monitors.find(m => inside(m, ...p.pointer)) ?? p.monitors[0];
            const [, hw] = p.hintLabel.get_preferred_width(-1);
            p.hintLabel.set_position(mon.x + Math.round((mon.w - hw) / 2) - s.x, mon.y + 28 - s.y);
        }
    }

    _finish(region, how) {
        const p = this._pick;
        if (!p || p.done)
            return;
        p.done = true;
        const windows = p.windows;
        this._close();
        this._cb.picked({rect: region ? [region.x, region.y, region.w, region.h] : null, how,
            windows});
    }

    _close() {
        const p = this._pick;
        this._pick = null;
        if (!p)
            return;
        if (p.grab) {
            try {
                Main.popModal(p.grab);
            } catch (e) {
                logError(e, 'Glance: shot modal');
            }
        }
        Main.layoutManager.removeChrome(p.root);
        p.root.destroy();
    }

    // glanced cancelled (or went away): close without reporting.
    cancel() {
        if (this._pick) {
            this._pick.done = true;
            this._close();
        }
        return {ok: true};
    }

    get picking() {
        return Boolean(this._pick);
    }

    // The region as PNG bytes, base64 (async). Only ever called for the verified glanced.
    capture({x, y, w, h}, done) {
        if (![x, y, w, h].every(Number.isFinite) || w < 1 || h < 1) {
            done({ok: false, error: 'bad region'});
            return;
        }
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, CAPTURE_DELAY_MS, () => {
            const stream = Gio.MemoryOutputStream.new_resizable();
            const shot = new Shell.Screenshot();
            shot.screenshot_area(Math.round(x), Math.round(y), Math.round(w), Math.round(h), stream,
                (_s, res) => {
                    try {
                        shot.screenshot_area_finish(res);
                        stream.close(null);
                        const bytes = stream.steal_as_bytes();
                        done({ok: true, png: GLib.base64_encode(bytes.get_data())});
                    } catch (e) {
                        done({ok: false, error: String(e.message ?? e)});
                    }
                });
            return GLib.SOURCE_REMOVE;
        });
    }

    // ------------------------------------------------------------------ the bar

    _label(text, colour, px = 13, extra = '', wrap = true) {
        const t = this._t;
        const label = new St.Label({text: text ?? '', y_align: Clutter.ActorAlign.CENTER});
        label.style = `font-family: ${this._look.font ?? 'sans-serif'}; font-size: ${px}px; ` +
            `color: ${t[colour] ?? colour}; ${extra}`;
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

    _button(b) {
        const t = this._t;
        const kind = b.muted ? 'muted' : b.primary ? 'primary' : 'secondary';
        const styles = {
            primary: [`background-color: ${t.accent_fill}; color: ${t.on_accent}; border-radius: 7px; ` +
                'padding: 7px 14px; font-weight: 600;', `background-color: ${t.accent_fill_hover};`, 13],
            secondary: [`background-color: ${t.card}; color: ${t.text}; border: 1px solid ${t.card_border}; ` +
                'border-radius: 7px; padding: 5px 10px;', `border-color: ${t.accent};`, 12],
            muted: [`background-color: transparent; color: ${t.muted}; border: 1px solid ${t.card_border}; ` +
                'border-radius: 7px; padding: 5px 10px;', `border-color: ${t.accent};`, 12],
        };
        const [base, hover, px] = styles[kind];
        const bin = new St.Bin({y_align: Clutter.ActorAlign.CENTER});
        const text = new St.Label({text: b.label, y_align: Clutter.ActorAlign.CENTER});
        text.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        bin.set_child(text);
        const plain = `font-family: ${this._look.font ?? 'sans-serif'}; font-size: ${px}px; ${base}`;
        bin.style = plain;
        bin.connect('notify::hover', () => {
            bin.style = bin.hover ? plain + hover : plain;
        });
        clickable(bin, () => this._cb.clicked(b.key));
        this._named.set(`shot:${b.key}`, bin);
        return bin;
    }

    _kbd(text) {
        const t = this._t;
        const label = new St.Label({text, y_align: Clutter.ActorAlign.CENTER});
        label.style = `font-family: ${this._look.mono ?? 'monospace'}; font-size: 11px; ` +
            `color: ${t.keycap_text}; background-color: ${t.keycap_bg}; ` +
            `border: 1px solid ${t.keycap_border}; border-radius: 4px; padding: 0 5px;`;
        return label;
    }

    _mark(px) {
        const look = this._look;
        if (!look?.mark_dir)
            return new St.Widget({width: px, height: px});
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor || 1;
        const sizes = look.mark_sizes ?? [];
        const size = sizes.find(s => s >= px * scale) ?? sizes[sizes.length - 1];
        const path = `${look.mark_dir}/glance-mark-${size}.png`;
        return new St.Icon({gicon: Gio.FileIcon.new(Gio.File.new_for_path(path)), icon_size: px,
            y_align: Clutter.ActorAlign.CENTER});
    }

    // A header tool (Copy image, Save): a small outlined button.
    _tool(b) {
        const t = this._t;
        const bin = new St.Bin({y_align: Clutter.ActorAlign.CENTER});
        const text = new St.Label({text: b.label, y_align: Clutter.ActorAlign.CENTER});
        text.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        bin.set_child(text);
        const plain = `font-family: ${this._look.font ?? 'sans-serif'}; font-size: 12px; ` +
            `background-color: transparent; color: ${t.text}; border: 1px solid ${t.card_border}; ` +
            'border-radius: 7px; padding: 3px 9px;';
        bin.style = plain;
        bin.connect('notify::hover', () => {
            bin.style = bin.hover ? `${plain} border-color: ${t.accent};` : plain;
        });
        clickable(bin, () => this._cb.clicked(b.key));
        this._named.set(`shot:${b.key}`, bin);
        return bin;
    }

    // A tab: one row, always in the same place; the selected one is lit, a muted one dimmed
    // (a click on it makes glanced say why).
    _tab(b) {
        const t = this._t;
        const bin = new St.Bin({x_expand: true, x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.CENTER});
        const text = new St.Label({text: b.label, x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER});
        text.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        bin.set_child(text);
        const font = `font-family: ${this._look.font ?? 'sans-serif'}; font-size: 13px; ` +
            'border-radius: 7px; padding: 5px 12px;';
        const plain = b.selected
            ? `${font} background-color: ${t.highlight_rgba}; color: ${t.accent}; font-weight: 600;`
            : `${font} background-color: transparent; color: ${t.muted};`;
        bin.style = plain;
        if (b.muted)
            bin.opacity = 128;
        if (!b.selected) {
            bin.connect('notify::hover', () => {
                bin.style = bin.hover ? `${font} background-color: transparent; color: ${t.text};` : plain;
            });
        }
        clickable(bin, () => this._cb.clicked(b.key));
        this._named.set(`shot:${b.key}`, bin);
        return bin;
    }

    _vbox(params = {}) {
        const box = new St.BoxLayout(params);
        if (box.orientation !== undefined)
            box.orientation = Clutter.Orientation.VERTICAL;
        else
            box.vertical = true;
        return box;
    }

    // The body: the hint, the notice, or the result (title + note, then the text, skeleton
    // lines while it is worked on, or why it failed). Returns the footer's actions.
    _body(card, view, words) {
        const t = this._t;
        const notice = Boolean(view.notice);
        const body = this._vbox({style: notice
            ? `background-color: ${t.warn_bg}; border: 1px solid ${t.warn_border}; ` +
              `border-radius: 8px; padding: 12px; spacing: 8px; min-height: ${BODY_MIN_H}px;`
            : `background-color: ${t.card}; border: 1px solid ${t.card_border}; ` +
              `border-radius: 8px; padding: 12px; spacing: 8px; min-height: ${BODY_MIN_H}px;`});
        this._named.set('shot:body', body);
        card.add_child(body);
        if (notice) {
            body.add_child(this._label(view.notice.text, 'warn_text', 13));
            return view.notice.buttons ?? [];
        }
        const r = view.result;
        if (!r) {
            body.add_child(this._label(view.hint ?? words.hint ?? '', 'text', 14, 'font-weight: 600;'));
            if (words.hint_sub)
                body.add_child(this._label(words.hint_sub, 'muted', 12));
            return [];
        }
        const head = new St.BoxLayout({style: 'spacing: 8px;'});
        head.add_child(this._label(r.title, 'muted', 12, 'font-weight: 600;', false));
        head.add_child(new St.Widget({x_expand: true}));
        body.add_child(head);
        if (r.state === 'failed') {
            body.add_child(this._label(r.note || 'That did not work. Try again', 'warn_text', 12));
            return [];
        }
        if (r.note)
            head.add_child(this._label(r.note, 'muted', 12, '', false));
        if (r.text !== null && r.text !== undefined) {
            const scroll = new St.ScrollView({style: `max-height: ${RESULT_MAX_H}px;`,
                hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC});
            const holder = this._vbox({});
            holder.add_child(this._label(r.text, 'text', 13.5));
            if (scroll.set_child)
                scroll.set_child(holder);
            else
                scroll.add_actor(holder);
            this._named.set('shot:result', scroll);
            body.add_child(scroll);
            return r.actions ?? [];
        }
        if (r.state === 'loading') {
            for (const frac of [1.0, 0.86, 0.6]) { // skeleton lines, the answer's shape
                const bar = new St.Widget({style: `background-color: ${t.skeleton}; border-radius: 5px; ` +
                    'margin: 2px 0;', x_align: Clutter.ActorAlign.START});
                bar.set_size(Math.round((BAR_W - 60) * frac), 10);
                body.add_child(bar);
            }
        }
        return [];
    }

    // One card beside the region, the same actor from start to finish: a render refills it
    // in place (it never closes and opens again somewhere else).
    showBar({view, region, tokens = {}, look = {}, words = {}}) {
        const key = JSON.stringify([view, region, tokens, words]);
        if (this._bar && key === this._barKey)
            return {ok: true, rect: this._barRect};
        this._barKey = key;
        this._lastView = view;
        this._t = tokens;
        this._look = look;
        this._named.clear();
        this._bannerText = null;
        const t = tokens;
        const reg = rect(...region);
        this._showFrame(reg);
        let card = this._bar;
        if (!card) {
            card = this._vbox({reactive: true});
            card.set_width(BAR_W);
            Main.layoutManager.addTopChrome(card);
            this._bar = card;
        }
        card.destroy_all_children();
        card.style = `background-color: ${t.surface}; border: 1px solid ${t.hairline}; ` +
            `border-radius: 12px; padding: 14px 16px; spacing: 12px; box-shadow: 0 4px 12px ${t.shadow};`;
        // header: the mark, "Glance Shot", the size; the image tools; Esc Close
        const header = new St.BoxLayout({style: 'spacing: 10px;'});
        header.add_child(this._mark(20));
        const titles = this._vbox({x_expand: true, y_align: Clutter.ActorAlign.CENTER, style: 'spacing: 1px;'});
        titles.add_child(this._label(words.title ?? 'Glance Shot', 'text', 14, 'font-weight: 600;', false));
        titles.add_child(this._label(view.size, 'muted', 12, '', false));
        header.add_child(titles);
        for (const b of view.tools ?? [])
            header.add_child(this._tool(b));
        const close = new St.Bin({y_align: Clutter.ActorAlign.CENTER,
            style: 'background-color: transparent; border-radius: 6px; padding: 2px 4px;'});
        const inner = new St.BoxLayout({style: 'spacing: 6px;'});
        inner.add_child(this._kbd(words.esc ?? 'Esc'));
        inner.add_child(this._label(words.close ?? 'Close', 'muted', 12, '', false));
        close.set_child(inner);
        clickable(close, () => this._cb.clicked('close'));
        this._named.set('shot:close', close);
        header.add_child(close);
        card.add_child(header);
        // the tabs
        const tabs = new St.BoxLayout({style: `background-color: ${t.group}; border: 1px solid ` +
            `${t.card_border}; border-radius: 9px; padding: 3px; spacing: 2px;`});
        for (const b of view.buttons) {
            if (b.key !== 'close')
                tabs.add_child(this._tab(b));
        }
        card.add_child(tabs);
        const actions = this._body(card, view, words);
        // the footer: the actions on the left, what just happened on the right
        const foot = new St.BoxLayout({style: 'spacing: 6px; min-height: 32px;'});
        for (const b of actions)
            foot.add_child(this._button(b));
        if (view.banner) {
            const status = this._label(view.banner, view.warn ? 'warn_text' : 'accent', 12, '', false);
            foot.add_child(new St.Widget({x_expand: true}));
            status.y_align = Clutter.ActorAlign.CENTER;
            this._named.set('shot:banner', status);
            this._bannerText = view.banner;
            foot.add_child(status);
        }
        card.add_child(foot);
        const [, h] = card.get_preferred_height(BAR_W);
        const [x, y] = barPosition(reg, [BAR_W, Math.ceil(h)], this._monitors());
        card.set_position(x, y);
        card.get_parent()?.set_child_above_sibling(card, null);
        this._barRect = [x, y, BAR_W, Math.ceil(h)];
        return {ok: true, rect: this._barRect};
    }

    // The captured region, outlined just outside it while its card is up; clicks go through.
    _showFrame(reg) {
        if (!this._frame) {
            this._frame = new St.Widget({reactive: false});
            Main.layoutManager.addTopChrome(this._frame, {affectsInputRegion: false});
        }
        this._frame.style = `border: 2px solid ${this._t.accent_fill ?? '#3ECF8E'};`;
        this._frame.set_position(reg.x - FRAME_PAD, reg.y - FRAME_PAD);
        this._frame.set_size(reg.w + 2 * FRAME_PAD, reg.h + 2 * FRAME_PAD);
        this._frame.show();
    }

    hideBar() {
        for (const actor of [this._bar, this._frame]) {
            if (actor) {
                Main.layoutManager.removeChrome(actor);
                actor.destroy();
            }
        }
        this._bar = this._frame = null;
        this._barKey = '';
        this._barRect = null;
        this._named.clear();
        return {ok: true};
    }

    // The image to the clipboard as image/png.
    copyImage({png}) {
        if (typeof png !== 'string' || !png)
            return {ok: false};
        try {
            const bytes = GLib.base64_decode(png);
            St.Clipboard.get_default().set_content(St.ClipboardType.CLIPBOARD, 'image/png',
                new GLib.Bytes(bytes));
            return {ok: true};
        } catch (e) {
            logError(e, 'Glance: shot clipboard');
            return {ok: false};
        }
    }

    // ------------------------------------------------------------------ tests

    actorFor(name) {
        return this._named.get(name) ?? null;
    }

    describe() {
        const geo = a => {
            if (!a || !a.visible)
                return null;
            const [x, y] = a.get_transformed_position();
            const [w, h] = a.get_transformed_size();
            return [Math.round(x), Math.round(y), Math.round(w), Math.round(h)];
        };
        const p = this._pick;
        return {
            picking: Boolean(p),
            selection: p?.selection ? [p.selection.x, p.selection.y, p.selection.w, p.selection.h] : null,
            label: p?.label?.visible ? p.label.text : null,
            bar: geo(this._bar),
            frame: geo(this._frame),
            selected: this._bar ? (this._lastView?.buttons ?? []).filter(b => b.selected).map(b => b.key) : [],
            banner: this._bar ? this._bannerText ?? null : null,
            named: [...this._named.keys()],
        };
    }
}

// Who may ask for what: the capture, the picker, the bar and the image clipboard are
// glanced's alone (not even glance-capture's).
export const DAEMON_ONLY = new Set(['ShotPick', 'ShotCancel', 'ShotCapture', 'ShotBar',
    'ShotClipboard']);