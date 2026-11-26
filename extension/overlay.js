// The overlay: status icon, strip, drawer / select-anything panel, ghost text and the
// "Rewrite as…" entry, rendered from the scene glanced sends. No decisions are made here:
// what to show, the text, the colours (ui/theme.py's tokens), the brand mark's files, the line
// glyphs and the places all come from Python.
import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Pango from 'gi://Pango';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const FONT = "Inter, Cantarell, 'Noto Sans', 'DejaVu Sans', sans-serif";
const MONO = "'JetBrains Mono', 'DejaVu Sans Mono', 'Noto Sans Mono', monospace";
const ICON = 22;
const ICON_X = 1, ICON_Y = 5;
const ICON_BOX = [30, 28];
const BADGE_C = [23, 7], BADGE_R = 7;
const MARK_PX = 18;  // the brand mark in the icon's (invisible) click target
const GHOST_GAP = 6;
const SPIN_MS = 125;
// The status icon's hover menu (glance/ui/hover_menu.py: OPEN_MS, CLOSE_MS).
const MENU_OPEN_MS = 250;
const MENU_CLOSE_MS = 400;
const ASK_RETURN_MS = 1500;
const LINE_FIXES = 3;
const MAX_CHOICES = 5;

function rgb(hex) {
    const h = (hex || '#000000').replace('#', '');
    return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16) / 255);
}

// `#rrggbb` or `rgba(r, g, b, a)` as [r, g, b, a] in 0..1.
function rgba(value) {
    const m = /rgba\(([^)]*)\)/.exec(value || '');
    if (!m)
        return [...rgb(value), 1];
    const p = m[1].split(',').map(v => parseFloat(v));
    return [p[0] / 255, p[1] / 255, p[2] / 255, p[3]];
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
let cursorAsked = null;  // GNOME 46/47: the last cursor asked for ('hand' | 'default')

// The hand cursor over everything clickable; text and empty space keep the default one.
// GNOME 48+ (mutter 16+): each actor carries its own cursor (Clutter.CursorType) and the
// stage shows it under the pointer; Meta.Cursor and display.set_cursor are gone there.
// GNOME 46/47: the display's cursor is set while the pointer is over the actor.
const ACTOR_CURSORS = Boolean(Clutter.CursorType) &&
    typeof Clutter.Actor.prototype.set_cursor_type === 'function';

function setCursor(hand) {
    cursorAsked = hand ? 'hand' : 'default';
    try {
        const cursor = hand ? Meta.Cursor.POINTING_HAND ?? Meta.Cursor.POINTER
            : Meta.Cursor.DEFAULT;
        global.display.set_cursor(cursor);
    } catch (_e) {
        // a shell without either: the default cursor stays
    }
}

// What the pointer shows now as far as the overlay is concerned (for the tests).
function cursorNow() {
    if (!ACTOR_CURSORS)
        return cursorAsked;
    const [x, y] = global.get_pointer();
    for (let a = global.stage.get_actor_at_pos(Clutter.PickMode.REACTIVE, x, y); a;
        a = a.get_parent()) {
        const type = a.get_cursor_type();
        if (type !== Clutter.CursorType.INHERIT)
            return type === Clutter.CursorType.POINTER ? 'hand' : 'default';
    }
    return 'default';
}

function clickable(actor, onClick) {
    actor.reactive = true;
    actor.track_hover = true;
    actor.can_focus = false;
    if (ACTOR_CURSORS) {
        actor.set_cursor_type(Clutter.CursorType.POINTER);
    } else {
        actor.connect('notify::hover', () => setCursor(actor.hover));
        actor.connect('destroy', () => {
            if (actor.hover)
                setCursor(false);
        });
    }
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
        this._look = {};
        this._surfaces = new Map();  // the brand mark's PNGs, for the status icon
        this._named = new Map();
        this._keys = {status: '', strip: '', panel: '', ghost: '', menu: ''};
        this._phase = 0;
        this._spin = 0;
        this._resizing = false;
        this._details = false;
        this._profiles = false;
        this._ask = null;
        this._sent = '';

        this._icon = new St.Bin({style: 'padding: 0;'});
        this._iconArea = new St.DrawingArea({width: ICON_BOX[0], height: ICON_BOX[1]});
        this._iconArea.connect('repaint', a => this._drawIcon(a));
        this._icon.set_child(this._iconArea);
        clickable(this._icon, () => {
            this._cancelMenuTimers();
            this._cb.clicked('status', {});
        });
        this._menuOpen = 0;
        this._menuClose = 0;
        this._icon.connect('notify::hover', () => this._menuHover());
        this._menu = new St.Bin({reactive: true, track_hover: true});
        this._menu.connect('notify::hover', () => this._menuHover());
        this._strip = new St.Bin({reactive: true});
        this._panel = new St.Bin({reactive: true});
        this._ghost = new St.BoxLayout({reactive: false, style: `spacing: ${GHOST_GAP}px;`});
        for (const actor of [this._ghost, this._strip, this._panel, this._icon, this._menu]) {
            actor.hide();
            Main.layoutManager.addTopChrome(actor);
        }
        this._named.set('status', this._icon);
    }

    destroy() {
        this._closeAsk(false);
        if (this._spin)
            GLib.source_remove(this._spin);
        this._cancelMenuTimers();
        for (const actor of [this._ghost, this._strip, this._panel, this._icon, this._menu]) {
            Main.layoutManager.removeChrome(actor);
            actor.destroy();
        }
    }

    // glanced is gone: close "Rewrite as…" (its modal grab) and hide everything.
    reset() {
        this._closeAsk(false);
        this.render({});
    }

    // ------------------------------------------------------------------ the scene

    render(scene) {
        this._scene = scene;
        this._t = scene.tokens || {};
        this._look = scene.look || {};
        this._resizing = false;
        const tokensKey = JSON.stringify(this._t);
        this._renderStatus(scene.status, tokensKey);
        this._renderMenu(scene.status?.menu ?? null, tokensKey);
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
        const menu = s.status?.menu ? {x: s.status.x + ICON_BOX[0], y: s.status.y} : null;
        const pieces = [[this._icon, s.status], [this._strip, s.strip],
            [this._panel, s.panel], [this._ghost, s.ghost], [this._menu, menu]];
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
        return `font-family: ${this._look?.font ?? FONT}; font-size: ${px}px; ${extra}`;
    }

    _mono(px = 11, extra = '') {
        return `font-family: ${this._look?.mono ?? MONO}; font-size: ${px}px; ${extra}`;
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

    // A keycap: Alt+Enter, Esc, 1-4, the return key, Tab.
    _kbd(text) {
        const t = this._t;
        const label = new St.Label({text, y_align: Clutter.ActorAlign.CENTER});
        label.style = this._mono(11, `color: ${t.keycap_text}; background-color: ${t.keycap_bg}; ` +
            `border: 1px solid ${t.keycap_border}; border-radius: 4px; padding: 0 5px;`);
        return label;
    }

    _keycaps(keys) {
        const row = hbox({style: 'spacing: 4px;', y_align: Clutter.ActorAlign.CENTER});
        for (const key of keys)
            row.add_child(this._kbd(key));
        return row;
    }

    _button(label, kind, onClick, name = null) {
        const t = this._t;
        const styles = {
            primary: [`background-color: ${t.accent_fill}; color: ${t.on_accent}; border-radius: 7px; ` +
                'padding: 7px 14px; font-weight: 600;', `background-color: ${t.accent_fill_hover};`, 13],
            use: [`background-color: ${t.highlight_rgba}; color: ${t.accent}; border: 1px solid ` +
                `${t.use_border}; border-radius: 5px; padding: 2px 7px; font-weight: 600; ` +
                `font-family: ${this._look?.mono ?? MONO};`,
            `background-color: ${t.accent_fill}; color: ${t.on_accent};`, 11.5],
            quiet: [`background-color: transparent; color: ${t.muted}; border-radius: 6px; ` +
                'padding: 2px 7px; font-weight: 600;', `background-color: ${t.hover}; color: ${t.text};`, 15],
            item: [`background-color: transparent; color: ${t.body}; border-radius: 6px; padding: 6px 10px;`,
                `background-color: ${t.hover}; color: ${t.text};`, 13],
            link: [`background-color: transparent; color: ${t.muted}; padding: 1px 4px; border-radius: 6px;`,
                `color: ${t.accent};`, 12],
            strong: [`background-color: transparent; color: ${t.accent}; font-weight: 600; padding: 1px 4px;`,
                `color: ${t.accent_hover};`, 12],
            secondary: [`background-color: ${t.card}; color: ${t.text}; border: 1px solid ${t.card_border}; ` +
                'border-radius: 7px; padding: 5px 10px;', `border-color: ${t.accent};`, 12],
            on: [`background-color: ${t.hover}; color: ${t.text}; border-radius: 6px; padding: 2px 7px; ` +
                'font-weight: 600;', '', 15],
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

    // A box that changes its style while the pointer is on it (a chip, the input, Close).
    _hoverBox(params, base, hover) {
        const box = new St.Bin(params);
        box.style = base;
        box.connect('notify::hover', () => {
            box.style = box.hover ? `${base} ${hover}` : base;
        });
        return box;
    }

    _act(action, args = {}) {
        return () => this._cb.clicked('action', {action, args});
    }

    // The brand mark's file for `px` logical pixels at the screen's scale: never scaled up.
    // `kind` 'glow': the soft glow behind it (bigger by `glow_pad` of it on every side).
    _markPath(px, kind = 'mark') {
        const look = this._look;
        if (!look?.mark_dir)
            return null;
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor || 1;
        const sizes = look.mark_sizes ?? [];
        const size = sizes.find(s => s >= px * scale) ?? sizes[sizes.length - 1];
        return `${look.mark_dir}/glance-${kind}-${size}.png`;
    }

    _mark(px) {
        const path = this._markPath(px);
        if (!path)
            return new St.Widget({width: px, height: px});
        return new St.Icon({gicon: Gio.FileIcon.new(Gio.File.new_for_path(path)), icon_size: px,
            y_align: Clutter.ActorAlign.CENTER});
    }

    _markSurface(px, kind = 'mark') {
        const path = this._markPath(px, kind);
        if (!path)
            return null;
        if (!this._surfaces.has(path)) {
            try {
                this._surfaces.set(path, Cairo.ImageSurface.createFromPNG(path));
            } catch (_e) {
                this._surfaces.set(path, null);
            }
        }
        return this._surfaces.get(path);
    }

    _glyphPath(cr, name, x, y, px) {
        const k = px / 16;
        for (const [cmd, ...p] of this._look?.glyphs?.[name] ?? []) {
            if (cmd === 'M')
                cr.moveTo(x + p[0] * k, y + p[1] * k);
            else if (cmd === 'L')
                cr.lineTo(x + p[0] * k, y + p[1] * k);
            else if (cmd === 'C')
                cr.curveTo(x + p[0] * k, y + p[1] * k, x + p[2] * k, y + p[3] * k, x + p[4] * k, y + p[5] * k);
            else
                cr.closePath();
        }
    }

    // A line glyph (heart, lines, bolt, briefcase, pen, spark) in the colour `token`.
    _glyph(name, px = 14, token = 'accent') {
        const area = new St.DrawingArea({width: px, height: px, y_align: Clutter.ActorAlign.CENTER});
        area.connect('repaint', a => {
            const cr = a.get_context();
            this._glyphPath(cr, name, 0.5, 0.5, px - 1);
            cr.setSourceRGB(...rgb(this._t[token]));
            cr.setLineWidth(1.5);
            cr.setLineCap(Cairo.LineCap.ROUND);
            cr.setLineJoin(Cairo.LineJoin.ROUND);
            cr.stroke();
            cr.$dispose();
        });
        return area;
    }

    _word(key, fallback) {
        return this._look?.words?.[key] ?? fallback;
    }

    // ------------------------------------------------------------------ the hover menu
    // The pointer resting on the icon asks glanced for the menu ('hover'); glanced sends it
    // in the scene (none while the user types). Leaving the icon and the menu ends it.

    _cancelMenuTimers() {
        for (const name of ['_menuOpen', '_menuClose']) {
            if (this[name]) {
                GLib.source_remove(this[name]);
                this[name] = 0;
            }
        }
    }

    _menuHover() {
        if (this._icon.hover || this._menu.hover) {
            if (this._menuClose) {
                GLib.source_remove(this._menuClose);
                this._menuClose = 0;
            }
            if (this._icon.hover && !this._scene?.status?.menu && !this._menuOpen) {
                this._menuOpen = GLib.timeout_add(GLib.PRIORITY_DEFAULT, MENU_OPEN_MS, () => {
                    this._menuOpen = 0;
                    if (this._icon.hover)
                        this._cb.clicked('hover', {});
                    return GLib.SOURCE_REMOVE;
                });
            }
            return;
        }
        if (this._menuOpen) {
            GLib.source_remove(this._menuOpen);
            this._menuOpen = 0;
        }
        if (!this._menuClose) {
            this._menuClose = GLib.timeout_add(GLib.PRIORITY_DEFAULT, MENU_CLOSE_MS, () => {
                this._menuClose = 0;
                if (!this._icon.hover && !this._menu.hover && this._scene?.status?.menu)
                    this._cb.clicked('hover_end', {});
                return GLib.SOURCE_REMOVE;
            });
        }
    }

    _renderMenu(menu, tokensKey) {
        const key = menu ? JSON.stringify([menu, tokensKey]) : '';
        if (key === this._keys.menu)
            return;
        this._keys.menu = key;
        for (const n of [...this._named.keys()]) {
            if (n.startsWith('menu:'))
                this._named.delete(n);
        }
        const old = this._menu.get_child();
        if (old) {
            this._menu.set_child(null);
            old.destroy();
        }
        if (!menu)
            return;
        const t = this._t;
        const box = vbox({style: `background-color: ${t.surface}; border: 1px solid ${t.hairline}; ` +
            `border-radius: 10px; padding: 6px; spacing: 1px; box-shadow: 0 4px 14px ${t.shadow};`});
        if (menu.note)
            box.add_child(this._label(menu.note, 'muted', 12, 'max-width: 260px; padding: 4px 8px 6px 8px;'));
        for (const item of menu.items) {
            const name = `menu:${item.action}${item.args?.key ? `:${item.args.key}` : ''}`;
            const button = this._button(item.label, 'item', this._act(item.action, item.args || {}),
                name);
            button.x_expand = true;
            button.get_child().x_expand = true;
            box.add_child(button);
        }
        if (menu.hint) {
            // "Super+Alt+G to check": the chord as keycaps, the rest muted
            const at = menu.hint.indexOf(' ');
            const chord = at < 0 ? menu.hint : menu.hint.slice(0, at);
            const rest = at < 0 ? '' : menu.hint.slice(at + 1);
            const hint = hbox({style: `spacing: 6px; padding: 6px 8px 2px 8px; ` +
                `border-top: 1px solid ${t.hairline};`});
            hint.add_child(this._keycaps(chord.split('+')));
            if (rest)
                hint.add_child(this._label(rest, 'muted', 12, '', false));
            box.add_child(hint);
        }
        this._menu.set_child(box);
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
            this._spin = GLib.timeout_add(GLib.PRIORITY_DEFAULT, this._look?.frame_ms ?? SPIN_MS, () => {
                this._phase = (this._phase + 1) % (this._look?.frames ?? 24);
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

    // The brand mark alone, no disc or ring (`look` from the scene): softer and without its
    // glow when idle, its glow breathing while working, full colour with a gentle glow and
    // the count badge at its top right when ready, a grey silhouette when muted.
    _drawIcon(area) {
        const cr = area.get_context();
        const st = this._scene?.status;
        if (!st) {
            cr.$dispose();
            return;
        }
        const t = this._t;
        const lk = st.look ?? {mark_alpha: 1, glow: 0, grey: false, pulse: false};
        const cx = ICON_X + ICON / 2, cy = ICON_Y + ICON / 2;
        const mark = this._markSurface(MARK_PX);
        const frames = this._look?.frames ?? 24;
        const glowStrength = lk.pulse ? (this._look?.pulse?.[this._phase % frames] ?? lk.glow) : lk.glow;
        if (mark && glowStrength > 0) {
            const glow = this._markSurface(MARK_PX, 'glow');
            const [r, g, b, a] = rgba(t[lk.glow_token ?? 'icon_glow'] ?? t.icon_glow);
            if (glow) {
                const k = MARK_PX / mark.getWidth();
                const side = glow.getWidth() * k;
                cr.save();
                cr.translate(cx - side / 2, cy - side / 2);
                cr.scale(k, k);
                cr.setSourceRGBA(r, g, b, Math.min(1, a * glowStrength));
                cr.maskSurface(glow, 0, 0);
                cr.restore();
            }
        }
        if (lk.arc && lk.pulse) {  // working: a thin arc turning around the mark
            const arc = this._look?.arc ?? {sweep: 1.9, radius: 10.5, width: 1.6};
            const start = 2 * Math.PI * (this._phase % frames) / frames;
            cr.setLineWidth(arc.width);
            cr.setLineCap(Cairo.LineCap.ROUND);
            cr.setSourceRGB(...rgb(t.accent_fill));
            cr.arc(cx, cy, arc.radius, start, start + arc.sweep);
            cr.stroke();
        }
        if (mark) {
            const k = MARK_PX / mark.getWidth();
            cr.save();
            cr.translate(cx - MARK_PX / 2, cy - MARK_PX / 2);
            cr.scale(k, k);
            if (lk.grey) {
                cr.setSourceRGBA(...rgb(t.dot_quiet), lk.mark_alpha);
                cr.maskSurface(mark, 0, 0);
            } else {
                cr.setSourceSurface(mark, 0, 0);
                cr.paintWithAlpha(lk.mark_alpha);
            }
            cr.restore();
        }
        const badge = st.badge;
        if (badge) {
            const [bx, by] = BADGE_C;
            const dot = badge === '•';
            cr.arc(bx, by, dot ? 3.5 : BADGE_R - 0.8, 0, 2 * Math.PI);
            cr.setSourceRGB(...rgb(t[dot ? 'dot_warn' : 'accent_fill']));
            cr.fill();
            if (!dot) {
                cr.selectFontFace('Sans', Cairo.FontSlant.NORMAL, Cairo.FontWeight.BOLD);
                cr.setFontSize(badge.length === 1 ? 8.5 : 7);
                const ext = cr.textExtents(badge);
                cr.moveTo(bx - ext.width / 2 - ext.xBearing, by - ext.height / 2 - ext.yBearing);
                cr.setSourceRGB(...rgb(t.on_accent));
                cr.showText(badge);
            }
        }
        cr.$dispose();
    }

    // ------------------------------------------------------------------ the strip

    _renderStrip(strip, tokensKey) {
        const key = strip ? JSON.stringify([strip.view, strip.markup, strip.use_label, strip.w,
            strip.loading, tokensKey]) : '';
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
        const plain = !warn && v.segments.length === 0;
        const border = warn ? t.warn_border : plain ? t.hairline : t.strip_border;
        // no bloom: at most a soft 8 px shadow (emerald at 12 % in dark, neutral in light)
        const glow = warn || plain ? '' : `box-shadow: 0 1px 8px ${t.strip_shadow};`;
        const row = hbox({style: `background-color: ${t[warn ? 'warn_bg' : 'strip']}; ` +
            `border: 1px solid ${border}; border-radius: 10px; padding: 7px 8px 7px 10px; ` +
            `spacing: 10px; ${glow}`});
        row.set_width(strip.w);
        row.add_child(this._mark(16));
        const text = vbox({x_expand: true, y_align: Clutter.ActorAlign.CENTER, style: 'spacing: 2px;'});
        if (v.segments.length > 0) {
            const sentence = this._label('', 'body', 14);
            sentence.clutter_text.set_markup(strip.markup);
            sentence.x_expand = true;
            this._named.set('strip-text', sentence);
            // "⋯" opened a compact view: the whole corrected text, scrolling past `scroll` px.
            if (strip.scroll) {
                const box = vbox({x_expand: true});
                box.add_child(sentence);
                text.add_child(this._scroll(box, strip.scroll));
            } else {
                text.add_child(sentence);
            }
        }
        if (strip.loading)  // "Checking with Claude…": the AI's corrected text is coming
            text.add_child(this._loader(strip.loading));
        if (v.summary && v.segments.length > 0)
            text.add_child(this._label(v.summary, 'muted', 12));
        if (v.meta)
            text.add_child(this._label(v.meta, 'muted', 12));
        if (v.guard)
            text.add_child(this._label(v.guard, 'warn_text', 13, 'font-weight: 600;'));
        if (v.note && v.segments.length === 0)
            text.add_child(this._label(v.note, 'muted', 13));
        row.add_child(text);
        if (strip.can_use) {
            row.add_child(this._button(strip.use_label, 'use', this._act('use'), 'use'));
        } else if (strip.use_disabled) {  // the keycap waits for the AI's corrected text
            const off = this._button(strip.use_label, 'use', () => {}, 'use-off');
            off.reactive = false;
            off.opacity = 115;
            row.add_child(off);
        }
        if (v.menu)
            row.add_child(this._button('⋯', v.expanded ? 'on' : 'quiet', this._act('more'), 'more'));
        this._strip.set_child(row);
    }

    // ------------------------------------------------------------------ the panel

    _renderPanel(panel, tokensKey) {
        // everything but the place: a move alone does not build it again
        const key = panel ? JSON.stringify([{...panel, x: 0, y: 0}, this._details, this._profiles,
            tokensKey]) : '';
        if (key === this._keys.panel)
            return;
        if (!panel) {
            this._details = this._profiles = false;
        }
        this._keys.panel = key;
        for (const n of [...this._named.keys()]) {
            if (!['status', 'use', 'more', 'strip-text', 'ask-entry'].includes(n) &&
                !n.startsWith('menu:'))
                this._named.delete(n);
        }
        this._panel.get_child()?.destroy();
        if (!panel)
            return;
        const t = this._t;
        const card = vbox({style: `background-color: ${t.surface}; border: 1px solid ${t.hairline}; ` +
            `border-radius: 12px; padding: 14px 16px; spacing: 14px; box-shadow: 0 8px 24px ${t.shadow};`});
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

    _section(text) {
        return this._label(text, 'muted', 12, 'font-weight: 600;', false);
    }

    // The brand mark, "Glance Assistant" and what is at work; who it is for; Esc Close.
    _header(panel) {
        const v = panel.view, t = this._t;
        const header = hbox({style: 'spacing: 10px;'});
        header.add_child(this._mark(22));
        const titles = vbox({x_expand: true, y_align: Clutter.ActorAlign.CENTER, style: 'spacing: 1px;'});
        titles.add_child(this._label(panel.title ?? 'Glance Assistant', 'text', 14, 'font-weight: 600;', false));
        titles.add_child(this._label(panel.subline ?? '', 'muted', 12, '', false));
        if (panel.tone_line && !v.tone_chips.length)
            titles.add_child(this._label(panel.tone_line, 'muted', 12));
        header.add_child(titles);
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
        const close = this._hoverBox({y_align: Clutter.ActorAlign.CENTER},
            'background-color: transparent; border-radius: 6px; padding: 2px 4px;',
            `background-color: ${t.hover};`);
        const inner = hbox({style: 'spacing: 6px;'});
        inner.add_child(this._kbd(this._word('esc', 'Esc')));
        inner.add_child(this._label(this._word('close', 'Close'), 'muted', 12, '', false));
        close.set_child(inner);
        clickable(close, this._act('close'));
        this._named.set('close', close);
        header.add_child(close);
        return header;
    }

    // A loading placeholder bar (look.SKELETON_*).
    _skeleton(width) {
        const h = this._look?.skeleton?.bar_h ?? 10;
        return new St.Widget({width: Math.round(width), height: h, x_expand: false,
            style: `background-color: ${this._t.skeleton}; border-radius: 5px;`});
    }

    // A small turning arc and "Checking with Claude…", muted. Its timer runs while it is
    // on the stage only.
    _loader(text) {
        const row = hbox({style: 'spacing: 6px;'});
        const area = new St.DrawingArea({width: 12, height: 12, y_align: Clutter.ActorAlign.CENTER});
        let phase = 0, src = 0;
        const frames = this._look?.frames ?? 24;
        area.connect('repaint', a => {
            const cr = a.get_context();
            const [r, g, b] = rgb(this._t.accent);
            const start = 2 * Math.PI * phase / frames;
            cr.setLineWidth(1.6);
            cr.setLineCap(Cairo.LineCap.ROUND);
            cr.setSourceRGBA(r, g, b, 0.25);
            cr.arc(6, 6, 4.6, 0, 2 * Math.PI);
            cr.stroke();
            cr.setSourceRGB(r, g, b);
            cr.arc(6, 6, 4.6, start, start + (this._look?.arc?.sweep ?? 1.9));
            cr.stroke();
            cr.$dispose();
        });
        const stop = () => {
            if (src) {
                GLib.source_remove(src);
                src = 0;
            }
        };
        area.connect('notify::mapped', () => {
            if (area.mapped && !src) {
                src = GLib.timeout_add(GLib.PRIORITY_DEFAULT, this._look?.frame_ms ?? 50, () => {
                    phase = (phase + 1) % frames;
                    area.queue_repaint();
                    return GLib.SOURCE_CONTINUE;
                });
            } else if (!area.mapped) {
                stop();
            }
        });
        area.connect('destroy', stop);
        row.add_child(area);
        row.add_child(this._label(text, 'muted', 12, '', false));
        return row;
    }

    // A tone chip: its glyph and number key, its name, one line of its rewrite once known. A
    // click selects it (emerald border and tint) and asks for its rewrite; hovering only
    // changes its look.
    _card(chip) {
        const t = this._t;
        const base = chip.selected
            ? `background-color: ${t.highlight}; border: 1px solid ${t.accent_fill}; `
            : `background-color: ${t.card}; border: 1px solid ${t.card_border}; `;
        const button = this._hoverBox({x_expand: true}, `${base}border-radius: 8px; padding: 8px 10px;`,
            chip.selected ? '' : `border-color: ${t.accent}; background-color: ${t.hover};`);
        const box = vbox({style: 'spacing: 3px;', x_expand: true});
        const top = hbox({style: 'spacing: 6px; padding-bottom: 2px;', x_expand: true});
        top.add_child(this._glyph(chip.glyph ?? 'spark', 15));
        top.add_child(new St.Widget({x_expand: true}));
        if (chip.hotkey !== null && chip.hotkey !== undefined)
            top.add_child(this._kbd(String(chip.hotkey)));
        box.add_child(top);
        box.add_child(this._label(chip.label, 'text', 13, 'font-weight: 600;', false));
        if (chip.skeleton)
            box.add_child(this._skeleton(0.8 * 90));
        else
            box.add_child(this._label(chip.line ?? '', 'muted', 12, '', false));
        button.set_child(box);
        clickable(button, this._act('tone', {key: chip.key}));
        this._named.set(`card:${chip.key}`, button);
        return button;
    }

    _cards(chips) {
        const row = hbox({style: 'spacing: 8px;'});
        for (const chip of chips)
            row.add_child(this._card(chip));
        return row;
    }

    // "Rewrite as… e.g. make it a bullet list" with the return key: a click opens the entry.
    _rewriteAs() {
        const t = this._t;
        const field = this._hoverBox({x_expand: true},
            `background-color: ${t.card}; border: 1px solid ${t.card_border}; ` +
            'border-radius: 8px; padding: 6px 8px 6px 10px;', `border-color: ${t.accent};`);
        const inner = hbox({style: 'spacing: 8px;', x_expand: true});
        inner.add_child(this._glyph('pen', 14, 'muted'));
        inner.add_child(this._label(this._word('rewrite', 'Rewrite as…'), 'body', 13, '', false));
        const hint = this._label(this._scene?.ask_placeholder ?? '', 'muted', 13, '', false);
        hint.x_expand = true;
        inner.add_child(hint);
        inner.add_child(this._kbd(this._word('return_key', '↵')));
        field.set_child(inner);
        clickable(field, this._act('tone', {key: 'custom'}));
        this._named.set('rewrite-as', field);
        return field;
    }

    // The "Tone" section: how the text reads, the chips, "Rewrite as…".
    _tones(panel) {
        const v = panel.view;
        const cards = v.tone_chips.filter(c => c.key !== 'custom');
        const custom = v.tone_chips.some(c => c.key === 'custom');
        if (!cards.length && !custom)
            return null;
        const box = vbox({style: 'spacing: 8px;'});
        const head = hbox({style: 'spacing: 8px;'});
        head.add_child(this._section(this._word('tone', 'Tone')));
        if (panel.tone_line) {
            const reads = this._label(panel.tone_line, 'muted', 12, '', false);
            reads.x_expand = true;
            head.add_child(reads);
        }
        box.add_child(head);
        if (cards.length)
            box.add_child(this._cards(cards));
        if (custom)
            box.add_child(this._rewriteAs());
        return box;
    }

    // "Suggested": the corrected text, or the selected tone's rewrite (its name beside the
    // title, "Rewriting…" while it comes in), every changed word tinted; "Apply (Alt+Enter)".
    _suggested(s) {
        const t = this._t;
        const box = vbox({style: 'spacing: 6px;'});
        const head = hbox({style: 'spacing: 8px;'});
        head.add_child(this._section(this._word('suggested', 'Suggested')));
        if (s.tone) {
            const tag = this._label(s.tone, 'accent', 12, 'font-weight: 600;', false);
            this._named.set('suggested-tone', tag);
            head.add_child(tag);
        }
        head.add_child(new St.Widget({x_expand: true}));
        if (s.working)
            head.add_child(this._label(s.working_label ?? 'Rewriting…', 'muted', 12, '', false));
        if (s.loading)
            head.add_child(this._loader(s.loading));
        box.add_child(head);
        let text;
        if (s.loading) {  // skeleton lines until the AI's corrected text is back
            const sk = this._look?.skeleton ?? {suggested: [1, 0.82, 0.55], gap: 7};
            text = vbox({x_expand: true, style: `spacing: ${sk.gap}px;`});
            const inner = Math.max(120, (this._scene?.panel?.w ?? 520) - 64);
            for (const share of sk.suggested)
                text.add_child(this._skeleton(inner * share));
        } else if (s.placeholder) {
            text = this._label(s.placeholder, 'muted', 13);
        } else {
            text = this._label('', 'text', 14);
            text.clutter_text.set_markup(s.markup);
        }
        text.x_expand = true;
        this._named.set('suggested-text', text);
        const frame = new St.Bin({x_expand: true, style: `background-color: ${t.card}; border: 1px solid ` +
            `${s.tone ? t.accent_fill : t.card_border}; border-radius: 8px; padding: 10px 12px;`});
        if (s.scroll) {
            const inner = vbox({x_expand: true});
            inner.add_child(text);
            frame.set_child(this._scroll(inner, 200));
        } else {
            frame.set_child(text);
        }
        box.add_child(frame);
        if (s.lines.length || s.can_use) {
            const bottom = hbox({style: 'spacing: 8px;'});
            const notes = vbox({x_expand: true, y_align: Clutter.ActorAlign.CENTER, style: 'spacing: 2px;'});
            for (const line of s.lines)
                notes.add_child(this._label(line, 'muted', 12));
            bottom.add_child(notes);
            if (s.can_use)
                bottom.add_child(this._button(s.apply_label, 'primary', this._act('use'), 'apply'));
            box.add_child(bottom);
        }
        return box;
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

    _fixLine(panel) {
        const v = panel.view;
        const line = hbox({style: 'spacing: 2px;'});
        const n = v.issues.length;
        line.add_child(this._button(panel.fixes_label ?? `${n} fixes`, 'link', () => {
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
            line.add_child(this._button(this._word('fix_all', 'Fix All'), 'strong', this._act('fix_all'), 'fix_all'));
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
        const v = panel.view, t = this._t;
        if (v.guard) {
            const banner = this._banner(v.guard);
            banner.add_child(this._button("Don't warn about this here", 'link',
                this._act('guard_silence', {scope: 'app'})));
            banner.add_child(this._button('Never', 'link', this._act('guard_silence', {scope: 'global'})));
            card.add_child(banner);
        }
        card.add_child(this._header(panel));
        if (v.profile_label && v.profiles.length && this._profiles) {
            const menu = hbox({style: 'spacing: 6px;'});
            for (const [name, label] of v.profiles)
                menu.add_child(this._button(label, 'secondary', this._act('profile', {name})));
            card.add_child(menu);
        }
        if (panel.suggested)
            card.add_child(this._suggested(panel.suggested));
        else if (panel.note && !v.tone)
            card.add_child(this._label(panel.note, 'muted', 12));
        const tones = this._tones(panel);
        if (tones)
            card.add_child(tones);
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
        if (v.issues.length && !v.loading) {  // the AI's list replaces the quick one
            const footer = vbox({style: `spacing: 10px; border-top: 1px solid ${t.hairline}; padding-top: 10px;`});
            footer.add_child(this._fixLine(panel));
            if (this._details) {
                const rows = vbox({style: 'spacing: 10px;'});
                for (const row of v.issues)
                    rows.add_child(this._issueRow(row));
                footer.add_child(this._scroll(rows, 240));
            }
            card.add_child(footer);
        } else if (!(v.banner || (v.copy_offer !== null && v.copy_offer !== undefined) ||
                     v.tone_chips.length || v.guard || panel.suggested)) {
            card.add_child(this._label('Nothing to fix here.', 'muted', 12));
        }
    }

    _fillSelection(card, panel) {
        const v = panel.view, t = this._t;
        card.add_child(this._header(panel));
        const tones = this._tones(panel);
        if (tones)
            card.add_child(tones);
        if (panel.preview_markup !== null && panel.preview_markup !== undefined) {
            const section = vbox({style: 'spacing: 6px;'});
            section.add_child(this._section('Rewrite'));
            const label = this._label('', 'body', 13);
            label.clutter_text.set_markup(panel.preview_markup);
            const box = new St.Bin({style: `background-color: ${t.card}; border: 1px solid ` +
                `${t.card_border}; border-radius: 8px; padding: 8px 10px;`, x_expand: true});
            box.set_child(label);
            section.add_child(this._scroll(box, 150));
            card.add_child(section);
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
    // The completion in emerald italics after the caret, with a small "Tab" keycap: Tab takes it.

    _renderGhost(ghost, tokensKey) {
        const key = ghost ? JSON.stringify([ghost.text, ghost.font_px, ghost.max_width, tokensKey]) : '';
        if (key === this._keys.ghost)
            return;
        this._keys.ghost = key;
        this._ghost.destroy_all_children();
        if (!ghost)
            return;
        const t = this._t;
        const text = new St.Label({text: ghost.text, y_align: Clutter.ActorAlign.CENTER});
        text.style = this._font(ghost.font_px, `color: ${t.ghost}; font-style: italic;`);
        text.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        const kbd = new St.Label({text: this._word('accept_key', 'Tab'), y_align: Clutter.ActorAlign.CENTER});
        kbd.style = this._mono(Math.max(8, Math.round(ghost.font_px * 0.7)),
            `color: ${t.keycap_text}; background-color: ${t.keycap_bg}; ` +
            `border: 1px solid ${t.keycap_border}; border-radius: 4px; padding: 0 5px;`);
        this._ghost.add_child(text);
        this._ghost.add_child(kbd);
        const hintW = kbd.get_preferred_width(-1)[1] + GHOST_GAP;
        if (ghost.max_width > 0 && text.get_preferred_width(-1)[1] + hintW > ghost.max_width)
            text.set_width(Math.max(1, ghost.max_width - hintW));
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
            menu: geo(this._menu) && s?.status?.menu ? {rect: geo(this._menu),
                items: s.status.menu.items.map(i => i.label), note: s.status.menu.note ?? null,
                hint: s.status.menu.hint ?? null} : null,
            cursor: cursorNow(),
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
