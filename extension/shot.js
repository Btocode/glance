// Glance Shot in GNOME Shell: the region picker, the capture and the shot bar.
//
// glanced decides everything (glance/shot/*.py); this draws what it is told and reports
// back. The picker follows the same rules as glance/shot/picker.py: drag a rectangle (a
// click is not one), Enter = the monitor under the pointer, Space = the window under it,
// Esc or a right click = cancel. The capture is Shell.Screenshot.screenshot_area into memory
// (no file, no portal dialog), handed only to the verified glanced (extension.js checks the
// caller's role); the image is never kept here once it was sent.
import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {clickable, flowBox, rgb, rgba} from './overlay.js';

const MIN_SIDE = 4;            // geometry.MIN_SIDE
const LABEL_OFFSET = [16, 18]; // geometry.LABEL_OFFSET
const BAR_GAP = 8;             // geometry.BAR_GAP
const DIM = 'rgba(8, 13, 18, 0.45)';
const BAR_W = 460;           // the card at least (shot_gtk.BAR_W); else the toolbar's width
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
    // cb: picked(result), clicked(action, args)
    constructor(cb) {
        this._cb = cb;
        this._pick = null;      // the picker's state while it is up
        this._bar = null;       // the card, one actor while the shot is up
        this._frame = null;     // the region's outline
        this._barKey = '';
        this._barRect = null;
        this._lastView = null;
        this._named = new Map();
        this._placedFor = null; // the region the card was placed for: it stays there
        this._anchor = null;    // the card's top-left for that region
        this._cardW = 0;
        this._askEntry = null;  // Ask's entry while it is open, and its modal grab
        this._askGrab = null;
        this._marker = null;    // Mark while it is up
        this._words = {};
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
    // A slim floating toolbar beside the region (the selection bar's look):
    //     [mark] Text · Explain · Translate · Ask… │ Mark · Copy · Save │ ✕
    // A tab with something to show grows the card under that same toolbar, which stays where
    // it was first put for this shot (bar_model's docstring).

    _font(px, extra = '') {
        return `font-family: ${this._look.font ?? 'sans-serif'}; font-size: ${px}px; ${extra}`;
    }

    _label(text, colour, px = 13, extra = '', wrap = true) {
        const t = this._t;
        const label = new St.Label({text: text ?? '', y_align: Clutter.ActorAlign.CENTER});
        label.style = this._font(px, `color: ${t[colour] ?? colour}; ${extra}`);
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

    _button(b, onClick = null) {
        const t = this._t;
        const kind = b.muted ? 'muted' : b.primary ? 'primary' : 'secondary';
        const styles = {
            primary: [`background-color: ${t.accent_fill}; color: ${t.on_accent}; border-radius: 7px; ` +
                'padding: 5px 12px; font-weight: 600;', `background-color: ${t.accent_fill_hover};`, 12.5],
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
        const plain = this._font(px, base);
        bin.style = plain;
        bin.connect('notify::hover', () => {
            bin.style = bin.hover ? plain + hover : plain;
        });
        clickable(bin, onClick ?? (() => this._cb.clicked(b.key)));
        this._named.set(`shot:${b.key}`, bin);
        return bin;
    }

    // A language chip ("Translate to"): the selected one is the last language used.
    _chip(b) {
        const t = this._t;
        const plain = this._font(13, b.selected
            ? `background-color: ${t.highlight_rgba}; color: ${t.accent}; border: 1px solid ` +
              `${t.accent_fill}; border-radius: 14px; padding: 3px 12px; font-weight: 600;`
            : `background-color: ${t.group}; color: ${t.text}; border: 1px solid ${t.card_border}; ` +
              'border-radius: 14px; padding: 3px 12px;');
        const bin = new St.Bin({y_align: Clutter.ActorAlign.CENTER, style: plain});
        const text = new St.Label({text: b.label, y_align: Clutter.ActorAlign.CENTER});
        text.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        bin.set_child(text);
        if (!b.selected) {
            bin.connect('notify::hover', () => {
                bin.style = bin.hover ? `${plain} border-color: ${t.accent};` : plain;
            });
        }
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

    // A line glyph (glance.ui.look.GLYPHS, sent in `look.glyphs`) in the colour `token`.
    _glyph(name, px = 13, token = 'accent') {
        const area = new St.DrawingArea({width: px, height: px, y_align: Clutter.ActorAlign.CENTER});
        const path = this._look?.glyphs?.[name] ?? [];
        area.connect('repaint', a => {
            const cr = a.get_context();
            const k = (px - 1) / 16, o = 0.5;
            for (const [cmd, ...p] of path) {
                if (cmd === 'M')
                    cr.moveTo(o + p[0] * k, o + p[1] * k);
                else if (cmd === 'L')
                    cr.lineTo(o + p[0] * k, o + p[1] * k);
                else if (cmd === 'C')
                    cr.curveTo(o + p[0] * k, o + p[1] * k, o + p[2] * k, o + p[3] * k, o + p[4] * k, o + p[5] * k);
                else
                    cr.closePath();
            }
            cr.setSourceRGB(...rgb(this._t[token]));
            cr.setLineWidth(1.5);
            cr.setLineCap(Cairo.LineCap.ROUND);
            cr.setLineJoin(Cairo.LineJoin.ROUND);
            cr.stroke();
            cr.$dispose();
        });
        return area;
    }

    // One toolbar item, as the selection bar draws them: a line glyph and its label. The
    // selected one is lit, a muted one dimmed (a click on it makes glanced say why).
    _item(b, {label = true, glyph = null, onClick = null} = {}) {
        const t = this._t;
        const font = this._font(13, 'border-radius: 6px; padding: 5px 8px;');
        const plain = b.selected ? `${font} background-color: ${t.highlight_rgba};`
            : `${font} background-color: transparent;`;
        const bin = new St.Bin({y_align: Clutter.ActorAlign.CENTER, style: plain});
        const row = new St.BoxLayout({style: 'spacing: 5px;'});
        const name = glyph ?? this._words?.glyphs?.[b.key];
        if (name && this._look?.glyphs?.[name])
            row.add_child(this._glyph(name, 13, b.muted ? 'muted' : 'accent'));
        if (label) {
            row.add_child(this._label(b.label, b.selected ? 'accent' : 'body', 13,
                b.selected ? 'font-weight: 600;' : '', false));
        }
        bin.set_child(row);
        if (b.muted)
            bin.opacity = 128;
        if (!b.selected) {
            bin.connect('notify::hover', () => {
                bin.style = bin.hover ? `${font} background-color: ${t.hover};` : plain;
            });
        }
        clickable(bin, onClick ?? (() => this._cb.clicked(b.key)));
        this._named.set(`shot:${b.key}`, bin);
        return bin;
    }

    _sep() {
        return new St.Widget({style: `background-color: ${this._t.hairline}; width: 1px; margin: 6px 4px;`,
            y_expand: true});
    }

    _vbox(params = {}) {
        const box = new St.BoxLayout(params);
        if (box.orientation !== undefined)
            box.orientation = Clutter.Orientation.VERTICAL;
        else
            box.vertical = true;
        return box;
    }

    _toolbar(view, words) {
        const row = new St.BoxLayout({style: 'spacing: 2px;'});
        const lead = this._mark(16);
        lead.style = 'margin: 0 4px;';
        row.add_child(lead);
        for (const b of view.buttons) {
            if (b.key !== 'close')
                row.add_child(this._item(b));
        }
        row.add_child(this._sep());
        for (const b of view.tools ?? [])
            row.add_child(this._item(b));
        row.add_child(this._sep());
        row.add_child(this._item({key: 'close', label: `${words.close ?? 'Close'} (Esc)`},
            {label: false}));
        this._named.set('shot:toolbar', row);
        return row;
    }

    // What just happened (Copied, Saved, why a muted tab does nothing), one line.
    _status(view) {
        const status = this._label(view.banner, view.warn ? 'warn_text' : 'accent', 12, '', false);
        status.y_align = Clutter.ActorAlign.CENTER;
        this._named.set('shot:banner', status);
        this._bannerText = view.banner;
        return status;
    }

    // The body: the notice, Ask's entry, or the result (title + note, then the language
    // chips, the text, skeleton lines while it is worked on, or why it failed).
    _body(view, words) {
        const t = this._t;
        const notice = Boolean(view.notice);
        const body = this._vbox({style: (notice
            ? `background-color: ${t.warn_bg}; border: 1px solid ${t.warn_border}; `
            : `background-color: ${t.card}; border: 1px solid ${t.card_border}; `) +
            'border-radius: 8px; padding: 12px; spacing: 8px; margin: 0 4px;'});
        this._named.set('shot:body', body);
        if (notice)
            body.add_child(this._label(view.notice.text, 'warn_text', 13));
        else if (view.asking)
            this._askBody(body, words);
        else if (view.result)
            this._result(body, view.result);
        return body;
    }

    _result(body, r) {
        const t = this._t;
        const head = new St.BoxLayout({style: 'spacing: 8px;'});
        head.add_child(this._label(r.title, 'muted', 12, 'font-weight: 600;', false));
        head.add_child(new St.Widget({x_expand: true}));
        body.add_child(head);
        if (r.state === 'failed') {
            body.add_child(this._label(r.note || 'That did not work. Try again', 'warn_text', 12));
            return;
        }
        if (r.note)
            head.add_child(this._label(r.note, 'muted', 12, '', false));
        if (r.state === 'choose') {
            const chips = flowBox(6, 6);
            for (const c of r.choices ?? [])
                chips.add_child(this._chip(c));
            this._named.set('shot:choices', chips);
            body.add_child(chips);
            return;
        }
        if (r.text !== null && r.text !== undefined) {
            const scroll = new St.ScrollView({style: `max-height: ${RESULT_MAX_H}px;`,
                hscrollbar_policy: St.PolicyType.NEVER, vscrollbar_policy: St.PolicyType.AUTOMATIC});
            const holder = this._vbox({});
            const text = this._label(r.text, 'text', 13.5);
            if (r.title === 'Text')  // what was read, line for line
                text.style = `font-family: ${this._look.mono ?? 'monospace'}; font-size: 12.5px; color: ${t.text};`;
            holder.add_child(text);
            if (scroll.set_child)
                scroll.set_child(holder);
            else
                scroll.add_actor(holder);
            this._named.set('shot:result', scroll);
            this._resultText = r.text;
            body.add_child(scroll);
            return;
        }
        if (r.state === 'loading') {
            for (const frac of [1.0, 0.86, 0.6]) { // skeleton lines, the answer's shape
                const bar = new St.Widget({style: `background-color: ${t.skeleton}; border-radius: 5px; ` +
                    'margin: 2px 0;', x_align: Clutter.ActorAlign.START});
                bar.set_size(Math.round((this._cardW - 60) * frac), 10);
                body.add_child(bar);
            }
        }
    }

    // The footer: the notice's buttons or the result's actions, the status on the right.
    _foot(view) {
        let actions = [];
        if (view.notice)
            actions = view.notice.buttons ?? [];
        else if (view.result && view.result.text !== null && view.result.text !== undefined && !view.asking)
            actions = view.result.actions ?? [];
        if (!actions.length && !view.banner)
            return null;
        const foot = new St.BoxLayout({style: 'spacing: 6px; margin: 0 4px;'});
        for (const b of actions)
            foot.add_child(this._button(b));
        foot.add_child(new St.Widget({x_expand: true}));
        if (view.banner)
            foot.add_child(this._status(view));
        return foot;
    }

    // ------------------------------------------------------------------ Ask, in the card
    // The entry takes the keyboard with a modal grab inside the shell while it is open (as
    // the overlay's "Rewrite as…" does): the app stays the focused window underneath and gets
    // the keyboard back on Enter, Esc or a click outside the card.

    _askBody(body, words) {
        const t = this._t;
        body.add_child(this._label(words.ask_prompt ?? '', 'muted', 12, 'font-weight: 600;', false));
        const entry = new St.Entry({hint_text: words.ask_placeholder ?? '', can_focus: true,
            x_expand: true, style: this._font(13, `color: ${t.text}; background-color: ${t.surface}; ` +
                `border: 1px solid ${t.accent}; border-radius: 8px; padding: 7px 10px;`)});
        entry.clutter_text.max_length = 500;
        entry.clutter_text.connect('activate', () => this._askEnd(true));
        entry.clutter_text.connect('event', (_a, event) => {
            if (event.type() === Clutter.EventType.KEY_PRESS &&
                event.get_key_symbol() === Clutter.KEY_Escape) {
                this._askEnd(false);
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
        body.add_child(entry);
        const hint = new St.BoxLayout({style: 'spacing: 6px;'});
        hint.add_child(this._kbd(words.return ?? '↵'));
        hint.add_child(this._label(words.ask_go ?? 'ask', 'muted', 12, '', false));
        hint.add_child(this._kbd(words.esc ?? 'Esc'));
        hint.add_child(this._label(words.ask_back ?? 'back', 'muted', 12, '', false));
        body.add_child(hint);
        this._askEntry = entry;
        this._named.set('shot:ask_entry', entry);
    }

    _askTake() {
        if (!this._askGrab) {
            try {
                this._askGrab = Main.pushModal(this._bar, {});
            } catch (e) {
                logError(e, 'Glance: shot ask modal');
                this._askGrab = null;
            }
        }
        global.stage.set_key_focus(this._askEntry.clutter_text);
    }

    _askRelease() {
        const grab = this._askGrab;
        this._askGrab = null;
        if (grab) {
            try {
                Main.popModal(grab);
            } catch (e) {
                logError(e, 'Glance: shot ask modal');
            }
        }
    }

    _askEnd(submit) {
        const entry = this._askEntry;
        if (!entry)
            return;
        const text = entry.get_text();
        this._askEntry = null;
        this._askRelease();
        if (submit)
            this._cb.clicked('ask_submit', {text});
        else
            this._cb.clicked('ask_cancel', {});
    }

    // A press outside the card while the entry has the keyboard: back, as Esc.
    _cardEvent(event) {
        if (!this._askGrab || event.type() !== Clutter.EventType.BUTTON_PRESS)
            return Clutter.EVENT_PROPAGATE;
        const [x, y] = event.get_coords();
        const [cx, cy] = this._bar.get_transformed_position();
        const [cw, ch] = this._bar.get_transformed_size();
        if (x < cx || y < cy || x >= cx + cw || y >= cy + ch) {
            this._askEnd(false);
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    // test hook: what the user would type into Ask's entry, and Enter
    askText(text, submit) {
        if (!this._askEntry)
            return false;
        this._askEntry.set_text(text);
        if (submit)
            this._askEnd(true);
        return true;
    }

    // One card beside the region, the same actor from start to finish: a render refills it
    // in place (it never closes and opens again somewhere else) and its toolbar stays where it
    // was first put for this region (moved up only when a grown card would leave the screen).
    showBar({view, region, tokens = {}, look = {}, words = {}}) {
        const key = JSON.stringify([view, region, tokens, words]);
        if (this._bar && key === this._barKey) {
            if (!this._marker)
                this._bar.show();
            return {ok: true, rect: this._barRect};
        }
        this._barKey = key;
        this._lastView = view;
        this._t = tokens;
        this._look = look;
        this._words = words;
        this._named.clear();
        this._bannerText = null;
        this._resultText = null;
        this._askEntry = null;
        const t = tokens;
        const reg = rect(...region);
        const regKey = region.join(',');
        if (this._placedFor !== regKey) {
            this._placedFor = regKey;
            this._anchor = null;
            this._cardW = 0;
        }
        this._showFrame(reg);
        let card = this._bar;
        if (!card) {
            card = this._vbox({reactive: true});
            card.connect('captured-event', (_a, event) => this._cardEvent(event));
            Main.layoutManager.addTopChrome(card);
            this._bar = card;
        }
        card.destroy_all_children();
        // The selection bar's card (no shadow); the same padding open or not, so the toolbar
        // does not shift by a pixel as the card grows under it.
        card.style = `background-color: ${t.surface}; border: 1px solid ${t.hairline}; ` +
            `border-radius: 10px; padding: 4px 4px ${view.expanded ? 10 : 4}px 4px; ` +
            `spacing: ${view.expanded ? 10 : 4}px; box-shadow: none;`;
        const toolbar = this._toolbar(view, words);
        card.add_child(toolbar);
        const [, tw] = toolbar.get_preferred_width(-1);
        this._cardW = Math.max(this._cardW || 0, BAR_W, Math.ceil(tw) + 10);
        card.set_width(this._cardW);
        if (view.expanded) {
            card.add_child(this._body(view, words));
            const foot = this._foot(view);
            if (foot)
                card.add_child(foot);
        } else if (view.banner) {
            const status = this._status(view);
            status.style += ' padding: 0 6px 2px 6px;';
            card.add_child(status);
        }
        const w = this._cardW;
        const [, ph] = card.get_preferred_height(w);
        const h = Math.ceil(ph);
        const mons = this._monitors();
        if (!this._anchor)
            this._anchor = barPosition(reg, [w, h], mons);
        let [x, y] = this._anchor;
        const mon = mons.find(m => inside(m, x, y)) ?? mons[0];
        if (mon) {
            if (y + h > mon.y + mon.h)
                y = Math.max(mon.y, mon.y + mon.h - h);
            if (x + w > mon.x + mon.w)
                x = Math.max(mon.x, mon.x + mon.w - w);
        }
        card.set_position(x, y);
        if (this._marker)
            card.hide();
        else
            card.show();
        card.get_parent()?.set_child_above_sibling(card, null);
        this._barRect = [x, y, w, h];
        if (view.asking && this._askEntry && !this._marker)
            this._askTake();
        else
            this._askRelease();
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
        if (this._marker)
            this._frame.hide();
        else
            this._frame.show();
    }

    hideBar() {
        this._askEntry = null;
        this._askRelease();
        this._endMark(null);
        for (const actor of [this._bar, this._frame]) {
            if (actor) {
                Main.layoutManager.removeChrome(actor);
                actor.destroy();
            }
        }
        this._bar = this._frame = null;
        this._barKey = '';
        this._barRect = null;
        this._placedFor = null;
        this._anchor = null;
        this._cardW = 0;
        this._named.clear();
        return {ok: true};
    }

    // ------------------------------------------------------------------ Mark
    // The frozen region (glanced's PNG, a 0600 file it deletes once this returned) right
    // where it was taken, a canvas over it and a small bar of tools above it: Pen, Box,
    // Arrow, Highlight; Undo (Ctrl+Z), Cancel (Esc), Done (Enter). The strokes go back to
    // glanced as point lists ('mark_done'); glanced draws the marked PNG (shot_gtk.marked_png).
    // The keyboard and pointer are Glance's (a modal grab) only while this is up.

    mark({path, rect: r, tokens = {}, look = {}, tools = [], words = {}, ink, highlight,
        width = 3, highlight_width = 16}) {
        this._endMark(null);
        const run = GLib.get_user_runtime_dir();
        if (typeof path !== 'string' || !path.startsWith(`${run}/glance-mark-`) || path.includes('/../') ||
            !Array.isArray(r) || r.length !== 4 || !r.every(Number.isFinite) || r[2] < 1 || r[3] < 1)
            return {ok: false};
        let image;
        try {
            image = Cairo.ImageSurface.createFromPNG(path);
        } catch (e) {
            logError(e, 'Glance: shot mark image');
            return {ok: false};
        }
        if (this._t === undefined || Object.keys(tokens).length)
            this._t = tokens;
        this._look = look;
        const reg = rect(...r.map(Math.round));
        const monitors = this._monitors();
        const x0 = Math.min(...monitors.map(m => m.x)), y0 = Math.min(...monitors.map(m => m.y));
        const x1 = Math.max(...monitors.map(m => m.x + m.w));
        const y1 = Math.max(...monitors.map(m => m.y + m.h));
        const screen = rect(x0, y0, x1 - x0, y1 - y0);
        const root = new St.Widget({reactive: true, can_focus: true, x: screen.x, y: screen.y,
            width: screen.w, height: screen.h});
        const canvas = new St.DrawingArea({x: reg.x - screen.x, y: reg.y - screen.y,
            width: reg.w, height: reg.h, reactive: true});
        root.add_child(canvas);
        const bar = new St.BoxLayout({reactive: true});
        root.add_child(bar);
        Main.layoutManager.addTopChrome(root);
        this._bar?.hide();
        this._frame?.hide();
        this._askRelease();
        const m = {root, canvas, bar, image, rect: reg, screen, tool: 'pen', strokes: [],
            drawing: false, grab: null, tools, words,
            ink: ink ?? [0.937, 0.267, 0.267, 1], highlight: highlight ?? [0.98, 0.8, 0.08, 0.38],
            width, highlightWidth: highlight_width};
        this._marker = m;
        canvas.connect('repaint', a => this._paintMark(a));
        this._markBar();
        try {
            m.grab = Main.pushModal(root, {});
        } catch (e) {
            logError(e, 'Glance: shot mark modal');
        }
        global.stage.set_key_focus(root);
        root.connect('captured-event', (_a, event) => this._markEvent(event));
        return {ok: true};
    }

    _markBar() {
        const m = this._marker;
        const t = this._t;
        m.bar.destroy_all_children();
        for (const k of [...this._named.keys()]) {
            if (k.startsWith('shot:mark_'))
                this._named.delete(k);
        }
        m.bar.style = `background-color: ${t.surface}; border: 1px solid ${t.hairline}; ` +
            'border-radius: 10px; padding: 4px; spacing: 2px; box-shadow: none;';
        const lead = this._mark(16);
        lead.style = 'margin: 0 4px;';
        m.bar.add_child(lead);
        for (const [key, label, glyph] of m.tools) {
            m.bar.add_child(this._item({key: `mark_${key}`, label, selected: key === m.tool},
                {glyph, onClick: () => this._markAct(`mark_${key}`)}));
        }
        m.bar.add_child(this._sep());
        for (const key of ['undo', 'cancel']) {
            m.bar.add_child(this._item({key: `mark_${key}`, label: m.words[key] ?? key},
                {onClick: () => this._markAct(`mark_${key}`)}));
        }
        const done = this._button({key: 'mark_done', label: m.words.done ?? 'Done', primary: true},
            () => this._endMark(true));
        done.style += ' margin-left: 4px;';
        m.bar.add_child(done);
        // above the region, else under it, else inside its top
        const r = m.rect, s = m.screen;
        const [, w] = m.bar.get_preferred_width(-1);
        const [, h] = m.bar.get_preferred_height(w);
        const mon = this._monitors().find(mo => inside(mo, r.x + Math.floor(r.w / 2), r.y + Math.floor(r.h / 2))) ?? s;
        let y = r.y - h - BAR_GAP >= mon.y ? r.y - h - BAR_GAP : r.y + r.h + BAR_GAP;
        if (y + h > mon.y + mon.h)
            y = r.y + BAR_GAP;
        const x = Math.max(mon.x, Math.min(r.x, mon.x + mon.w - w));
        m.bar.set_position(Math.round(x - s.x), Math.round(y - s.y));
    }

    _markAct(key) {
        const m = this._marker;
        if (!m)
            return;
        const tool = key.slice(5);
        if (m.tools.some(([k]) => k === tool)) {
            m.tool = tool;
            this._markBar();
        } else if (key === 'mark_undo') {
            m.strokes.pop();
            m.canvas.queue_repaint();
        } else if (key === 'mark_cancel') {
            this._endMark(false);
        }
    }

    _markEvent(event) {
        const m = this._marker;
        if (!m)
            return Clutter.EVENT_PROPAGATE;
        const type = event.type();
        if (type === Clutter.EventType.KEY_PRESS) {
            const sym = event.get_key_symbol();
            if (sym === Clutter.KEY_Escape)
                this._endMark(false);
            else if (sym === Clutter.KEY_Return || sym === Clutter.KEY_KP_Enter)
                this._endMark(true);
            else if ((sym === Clutter.KEY_z || sym === Clutter.KEY_Z) &&
                     (event.get_state() & Clutter.ModifierType.CONTROL_MASK))
                this._markAct('mark_undo');
            return Clutter.EVENT_STOP;
        }
        const r = m.rect;
        const at = () => {
            const [ex, ey] = event.get_coords();
            return [Math.max(0, Math.min(r.w, ex - r.x)), Math.max(0, Math.min(r.h, ey - r.y))];
        };
        if (type === Clutter.EventType.BUTTON_PRESS) {
            const [ex, ey] = event.get_coords();
            const [bx, by] = m.bar.get_transformed_position();
            const [bw, bh] = m.bar.get_transformed_size();
            if (bx <= ex && ex < bx + bw && by <= ey && ey < by + bh)
                return Clutter.EVENT_PROPAGATE;  // the bar's own buttons
            if (event.get_button() === Clutter.BUTTON_PRIMARY && inside(r, ex, ey)) {
                m.drawing = true;
                m.strokes.push([m.tool, [at()]]);
                m.canvas.queue_repaint();
            }
            return Clutter.EVENT_STOP;
        }
        if (type === Clutter.EventType.MOTION && m.drawing) {
            const pts = m.strokes[m.strokes.length - 1][1];
            const p = at();
            if (m.tool === 'box' || m.tool === 'arrow') {
                pts.splice(1, pts.length - 1, p);
            } else {
                const q = pts[pts.length - 1];
                if (Math.hypot(p[0] - q[0], p[1] - q[1]) >= 1.5)
                    pts.push(p);
            }
            m.canvas.queue_repaint();
            return Clutter.EVENT_STOP;
        }
        if (type === Clutter.EventType.BUTTON_RELEASE && m.drawing) {
            const pts = m.strokes[m.strokes.length - 1][1];
            const p = at();
            if (m.tool === 'box' || m.tool === 'arrow')
                pts.splice(1, pts.length - 1, p);
            else
                pts.push(p);
            m.drawing = false;
            m.canvas.queue_repaint();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    // The preview only: glanced draws the PNG that is kept, from the same strokes.
    _paintMark(area) {
        const m = this._marker;
        const cr = area.get_context();
        if (!m) {
            cr.$dispose();
            return;
        }
        const {w, h} = m.rect;
        cr.save();
        cr.scale(w / Math.max(1, m.image.getWidth()), h / Math.max(1, m.image.getHeight()));
        cr.setSourceSurface(m.image, 0, 0);
        cr.paint();
        cr.restore();
        for (const [tool, pts] of m.strokes) {
            if (!pts.length)
                continue;
            if (tool === 'highlight') {
                cr.setSourceRGBA(...m.highlight);
                cr.setLineWidth(m.highlightWidth);
                cr.setLineCap(Cairo.LineCap.BUTT);
            } else {
                cr.setSourceRGBA(...m.ink);
                cr.setLineWidth(m.width);
                cr.setLineCap(Cairo.LineCap.ROUND);
            }
            cr.setLineJoin(Cairo.LineJoin.ROUND);
            const [x0, y0] = pts[0], [x1, y1] = pts[pts.length - 1];
            if (tool === 'box') {
                cr.rectangle(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
                cr.stroke();
            } else if (tool === 'arrow') {
                cr.moveTo(x0, y0);
                cr.lineTo(x1, y1);
                cr.stroke();
                const angle = Math.atan2(y1 - y0, x1 - x0);
                for (const side of [-0.45, 0.45]) {
                    cr.moveTo(x1, y1);
                    cr.lineTo(x1 - 14 * Math.cos(angle + side), y1 - 14 * Math.sin(angle + side));
                }
                cr.stroke();
            } else {
                cr.moveTo(x0, y0);
                for (const [x, y] of pts.slice(1))
                    cr.lineTo(x, y);
                if (pts.length === 1)
                    cr.lineTo(x0 + 0.1, y0);
                cr.stroke();
            }
        }
        cr.setSourceRGBA(...rgba(this._t?.accent_fill ?? '#3ECF8E'));
        cr.setLineWidth(2);
        cr.rectangle(1, 1, w - 2, h - 2);
        cr.stroke();
        cr.$dispose();
    }

    // `done` true: the strokes to glanced; false: cancelled (glanced told); null: closed
    // with the bar (nothing told).
    _endMark(done) {
        const m = this._marker;
        if (!m)
            return;
        this._marker = null;
        if (m.grab) {
            try {
                Main.popModal(m.grab);
            } catch (e) {
                logError(e, 'Glance: shot mark modal');
            }
        }
        Main.layoutManager.removeChrome(m.root);
        m.root.destroy();
        for (const k of [...this._named.keys()]) {
            if (k.startsWith('shot:mark_'))
                this._named.delete(k);
        }
        if (done === null)
            return;
        // the card again, as it was (glanced draws it anew once it has the marks)
        this._bar?.show();
        this._frame?.show();
        if (done) {
            const strokes = m.strokes.map(([tool, pts]) =>
                [tool, pts.map(([x, y]) => [Math.round(x * 10) / 10, Math.round(y * 10) / 10])]);
            this._cb.clicked('mark_done', {strokes});
        } else {
            this._cb.clicked('mark_cancel', {});
        }
    }

    // test hook: a stroke drawn with `tool` through `points` (region coordinates), then
    // Done when `finish`.
    markStroke({tool = 'pen', points = [], finish = false} = {}) {
        const m = this._marker;
        if (!m)
            return {ok: false};
        if (points.length)
            m.strokes.push([tool, points.map(([x, y]) => [x, y])]);
        m.canvas.queue_repaint();
        if (finish)
            this._endMark(true);
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
            toolbar: this._bar ? geo(this._named.get('shot:toolbar')) : null,
            expanded: Boolean(this._bar && this._lastView?.expanded),
            asking: Boolean(this._askEntry),
            ask_grab: Boolean(this._askGrab),
            choices: this._bar ? (this._lastView?.result?.choices ?? []).map(c => c.key) : [],
            chosen: this._bar ? (this._lastView?.result?.choices ?? []).filter(c => c.selected).map(c => c.key) : [],
            result_text: this._bar ? this._resultText ?? null : null,
            marking: this._marker ? {rect: geo(this._marker.canvas), bar: geo(this._marker.bar),
                tool: this._marker.tool, strokes: this._marker.strokes.length} : null,
            named: [...this._named.keys()],
        };
    }
}

// Who may ask for what: the capture, the picker, the bar and the image clipboard are
// glanced's alone (not even glance-capture's).
export const DAEMON_ONLY = new Set(['ShotPick', 'ShotCancel', 'ShotCapture', 'ShotBar',
    'ShotClipboard']);