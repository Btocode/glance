// Glance for GNOME Shell (Wayland): the overlay, hotkeys, write-back keys and clipboard.
//
// A dumb renderer and input source: glanced (Python) decides everything and talks to this
// extension over the session bus. Text that crosses the bus is never logged here.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {KeyGuard} from './keyguard.js';
import {Overlay} from './overlay.js';
import {Peers} from './peers.js';
import {SelectionWatch} from './selwatch.js';
import {DAEMON_ONLY, ShotUi} from './shot.js';

const VERSION = 1;
const SHELL_NAME = 'io.github.btocode.GlanceShell';
const SHELL_PATH = '/io/github/btocode/GlanceShell';
const DAEMON_PATH = '/io/github/btocode/Glance';
const DAEMON_IFACE = 'io.github.btocode.Glance1';

const METHODS = ['Ping', 'Render', 'Grab', 'Ungrab', 'FocusedWindow', 'Monitors', 'KeyState',
    'TypeText', 'Chord', 'ClipboardSave', 'ClipboardSet', 'ClipboardRestore', 'ReadPrimary',
    'ActivateWindow', 'Ask', 'Pointer', 'WatchSelection',
    // Glance Shot (shot.js): glanced's alone, see DAEMON_ONLY
    'ShotPick', 'ShotCancel', 'ShotCapture', 'ShotBar', 'ShotClipboard'];
// GLANCE_SHELL_TEST=1 only (the private test shell). TestCall runs any method of METHODS for
// the test process, which is not Glance and is refused by every one of them otherwise.
const TEST_METHODS = ['TestHideOverview', 'TestMoveWindow', 'TestOverlay', 'TestClick',
    'TestScreenshot', 'TestAskText', 'TestPointer', 'TestGrabs', 'TestCall'];
// Methods that answer later (they wait for the clipboard, a timer or a screenshot).
const ASYNC = new Set(['TypeText', 'ClipboardSave', 'ReadPrimary', 'ClipboardRestore', 'ShotCapture',
    'TestClick', 'TestScreenshot', 'TestCall']);
// What any caller may ask: whether the extension is there (the installer and doctor ask).
const OPEN = new Set(['Ping']);

function ifaceXml(test) {
    const names = test ? [...METHODS, ...TEST_METHODS] : METHODS;
    const methods = names.map(m =>
        `<method name="${m}"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>`
    ).join('');
    return `<node><interface name="io.github.btocode.GlanceShell1">${methods}` +
        '<signal name="WindowChanged"><arg type="s"/></signal></interface></node>';
}

// Hotkey names glanced configures ([general.hotkeys]); they are keys of our schema.
const SCHEMA_KEYS = new Set(['check', 'panel', 'fix-all', 'revert', 'pause', 'tone1', 'tone2', 'tone3',
    'tone4', 'complete', 'selection', 'shot']);

const MODIFIER_KEYVALS = {
    ctrl: Clutter.KEY_Control_L, shift: Clutter.KEY_Shift_L,
    alt: Clutter.KEY_Alt_L, super: Clutter.KEY_Super_L,
};
const HELD = Clutter.ModifierType.SHIFT_MASK | Clutter.ModifierType.CONTROL_MASK |
    Clutter.ModifierType.MOD1_MASK | Clutter.ModifierType.MOD4_MASK |
    Clutter.ModifierType.SUPER_MASK | Clutter.ModifierType.HYPER_MASK |
    Clutter.ModifierType.META_MASK | Clutter.ModifierType.MOD3_MASK |
    Clutter.ModifierType.MOD5_MASK;
// Clipboard representations that set_text puts back as they were.
const TEXT_MIMES = new Set(['text/plain', 'text/plain;charset=utf-8', 'UTF8_STRING', 'STRING',
    'TEXT', 'COMPOUND_TEXT', 'text/plain;charset=UTF-8']);

function rect(r) {
    return [r.x, r.y, r.width, r.height];
}

export default class GlanceExtension extends Extension {
    enable() {
        this._test = GLib.getenv('GLANCE_SHELL_TEST') === '1';
        this._settings = this.getSettings();
        this._seat = Clutter.get_default_backend().get_default_seat();
        this._kbd = this._seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        this._grabs = new Map();      // name -> {kind: 'wm'|'accel', action}
        this._actions = new Map();    // accelerator action id -> name
        this._window = null;
        this._windowIds = [];
        this._overlay = new Overlay({
            clicked: (target, args) => this._daemon('Clicked', {target, args}),
            sized: sizes => this._daemon('Sized', sizes),
            askDone: result => this._daemon('AskDone', result),
            windowFrame: id => this._frameOf(id),
        });
        // Selection actions: "a selection settled" (never its text) for glanced.
        this._selwatch = new SelectionWatch({
            settled: (x, y) => this._daemon('SelectionSettled', {x, y}),
            cleared: () => this._daemon('SelectionCleared', {}),
        });
        this._shot = new ShotUi({
            picked: result => this._daemon('ShotPicked', result),
            clicked: action => this._daemon('Clicked', {target: 'shot', args: {action}}),
        });
        // Transient grabs never outlive an answering glanced (see keyguard.js).
        this._guard = new KeyGuard({
            held: () => [...this._grabs].filter(([, g]) => g.kind === 'accel')
                .map(([name, g]) => [name, g.accel]),
            drop: name => this._ungrab(name),
            take: (name, accel) => this._mGrab({name, accel}),
        });
        this._peers = new Peers(Gio.DBus.session, role => {
            if (role === 'daemon')
                this._daemonGone();
        });
        this._obj = Gio.DBusExportedObject.wrapJSObject(ifaceXml(this._test), this._dispatcher());
        this._obj.export(Gio.DBus.session, SHELL_PATH);
        this._nameId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, SHELL_NAME, Gio.BusNameOwnerFlags.NONE, null, null);
        this._accelId = global.display.connect('accelerator-activated',
            (_d, action) => this._accelerator(action));
        this._focusId = global.display.connect('notify::focus-window', () => this._focusChanged());
        this._monitorsId = Main.layoutManager.connect('monitors-changed',
            () => this._windowChanged('monitors'));
        this._focusChanged();
        this._daemon('ExtensionHello', {version: VERSION, shell: Config.PACKAGE_VERSION});
    }

    disable() {
        this._selwatch.destroy();
        this._selwatch = null;
        this._guard.gone();
        for (const name of [...this._grabs.keys()])
            this._ungrab(name);
        this._unfollow();
        global.display.disconnect(this._accelId);
        global.display.disconnect(this._focusId);
        Main.layoutManager.disconnect(this._monitorsId);
        this._overlay.destroy();
        this._overlay = null;
        this._shot.destroy();
        this._shot = null;
        this._obj.unexport();
        this._obj = null;
        Gio.bus_unown_name(this._nameId);
        this._peers.destroy();
        this._peers = null;
        this._kbd = null;
        this._pointer = null;
        this._settings = null;
    }

    // ------------------------------------------------------------------ the bus

    _dispatcher() {
        // Every method is in the Async form, so that its invocation (and with it the caller)
        // is known: only glanced and glance-capture may use the extension (see peers.js).
        // Each takes and returns one JSON string.
        const target = {};
        const names = this._test ? [...METHODS, ...TEST_METHODS] : METHODS;
        for (const name of names) {
            target[`${name}Async`] = (params, invocation) => {
                let payload = {};
                try {
                    payload = JSON.parse(params[0] || '{}');
                } catch (_e) {}
                this._authorized(name, invocation.get_sender()).then(ok => {
                    if (!ok || !this._obj) {
                        invocation.return_dbus_error('org.freedesktop.DBus.Error.AccessDenied',
                            'only Glance may use this');
                        return;
                    }
                    this._run(name, payload, answer => invocation.return_value(
                        new GLib.Variant('(s)', [JSON.stringify(answer ?? null)])));
                }).catch(() => {
                    invocation.return_dbus_error('org.freedesktop.DBus.Error.Failed', 'error');
                });
            };
        }
        return target;
    }

    async _authorized(name, sender) {
        if (OPEN.has(name))
            return true;
        if (TEST_METHODS.includes(name))
            return this._test;  // exported only in the test shell anyway
        if (!sender)
            return false;
        const role = await this._peers.authorize(sender);
        // the screen capture and the rest of Glance Shot: the verified glanced only
        return DAEMON_ONLY.has(name) ? role === 'daemon' : role !== null;
    }

    _run(name, payload, done) {
        const fn = this[`_m${name}`].bind(this);
        if (ASYNC.has(name)) {
            fn(payload, done);
            return;
        }
        let answer = null;
        try {
            answer = fn(payload);
        } catch (e) {
            logError(e, `Glance: ${name}`);  // the error, never the payload
        }
        done(answer);
    }

    _daemon(method, payload, done = null) {
        // To the verified glanced only: a process that took its name gets nothing.
        // `done(answered)`, when given, learns whether glanced took the call.
        const peers = this._peers;
        if (!peers) {
            done?.(false);
            return;
        }
        peers.peer('daemon').then(dest => {
            if (!dest) {
                done?.(false);
                return;
            }
            Gio.DBus.session.call(dest, DAEMON_PATH, DAEMON_IFACE, method,
                new GLib.Variant('(s)', [JSON.stringify(payload)]), null,
                Gio.DBusCallFlags.NO_AUTO_START, 2000, null, (conn, res) => {
                    let ok = true;
                    try {
                        conn.call_finish(res);
                    } catch (_e) {
                        ok = false;  // glanced went away or hangs: nothing to tell
                    }
                    done?.(ok);
                });
        });
    }

    // glanced left the bus (quit, crashed, killed): nothing may stay captured or on screen
    // for it. A new glanced grabs and draws again when it starts.
    _daemonGone() {
        this._selwatch?.set(false);
        this._guard?.gone();
        for (const name of [...this._grabs.keys()])
            this._ungrab(name);
        try {
            this._overlay?.reset();
            this._shot?.destroy();
        } catch (e) {
            logError(e, 'Glance: overlay reset');
        }
    }

    _signal(state) {
        // Unicast to glanced and glance-capture, not broadcast to the whole bus.
        const body = new GLib.Variant('(s)', [JSON.stringify(state)]);
        for (const dest of this._peers?.accepted() ?? []) {
            try {
                Gio.DBus.session.emit_signal(dest, SHELL_PATH, 'io.github.btocode.GlanceShell1',
                    'WindowChanged', body);
            } catch (_e) {}
        }
    }

    _mTestCall({method, payload}, done) {
        if (!METHODS.includes(method)) {
            done({ok: false, reason: 'no such method'});
            return;
        }
        this._run(method, payload ?? {}, done);
    }

    // ------------------------------------------------------------------ windows

    _info(w) {
        if (!w)
            return null;
        return {
            id: String(w.get_id()),
            pid: w.get_pid(),
            wm_class: w.get_wm_class() || '',
            wm_class_instance: w.get_wm_class_instance() || '',
            title: w.get_title() || '',
            frame: rect(w.get_frame_rect()),
            buffer: rect(w.get_buffer_rect()),
            monitor: w.get_monitor(),
            x11: w.get_client_type() === Meta.WindowClientType.X11,
        };
    }

    _monitors() {
        return Main.layoutManager.monitors.map(m => [m.x, m.y, m.width, m.height]);
    }

    _shellHasKeyboard() {
        const focus = global.stage.get_key_focus();
        return Main.modalCount > 0 || Main.overview.visible ||
            (focus !== null && focus !== global.stage);
    }

    _state(reason) {
        return {
            reason,
            focus: this._info(this._window),
            shell_has_keyboard: this._shellHasKeyboard(),
            monitors: this._monitors(),
        };
    }

    _windowChanged(reason) {
        const state = this._state(reason);
        this._signal(state);
        this._daemon(reason === 'focus' ? 'WindowChanged' : 'GeometryChanged', state);
        this._overlay?.windowMoved(state.focus, reason === 'size');
    }

    _unfollow() {
        if (this._window) {
            for (const id of this._windowIds)
                this._window.disconnect(id);
        }
        this._window = null;
        this._windowIds = [];
    }

    _focusChanged() {
        const w = global.display.focus_window;
        if (w === this._window)
            return;
        this._unfollow();
        this._window = w;
        if (w) {
            this._windowIds = [
                w.connect('position-changed', () => this._windowChanged('position')),
                w.connect('size-changed', () => this._windowChanged('size')),
                w.connect('unmanaged', () => {
                    if (this._window === w) {
                        this._unfollow();
                        this._windowChanged('focus');
                    }
                }),
            ];
        }
        this._windowChanged('focus');
    }

    _frameOf(id) {
        const w = this._window;
        return w && String(w.get_id()) === id ? rect(w.get_frame_rect()) : null;
    }

    _byId(id) {
        return global.display.list_all_windows().find(w => String(w.get_id()) === String(id)) ?? null;
    }

    // ------------------------------------------------------------------ methods

    _mPing() {
        return {ok: true, version: VERSION, shell: Config.PACKAGE_VERSION, test: this._test};
    }

    _mRender(scene) {
        this._overlay.render(scene);
        return {ok: true};
    }

    _mFocusedWindow() {
        return this._state('query');
    }

    _mMonitors() {
        return this._monitors();
    }

    _mPointer() {
        const [x, y] = global.get_pointer();
        return [x, y];
    }

    _mKeyState() {
        const [, , mods] = global.get_pointer();
        return {
            mods,
            down: (mods & HELD) !== 0,
            caps: (mods & Clutter.ModifierType.LOCK_MASK) !== 0,
            shell_has_keyboard: this._shellHasKeyboard(),
        };
    }

    _mActivateWindow({id}) {
        const w = this._byId(id);
        if (w)
            Main.activateWindow(w);
        return {ok: Boolean(w)};
    }

    // hotkeys

    _mGrab({name, accel}) {
        if (typeof name !== 'string' || typeof accel !== 'string')
            return {ok: false, reason: 'invalid'};
        this._ungrab(name);
        this._guard?.asked(name);
        const key = name.replace(/_/g, '-');
        if (SCHEMA_KEYS.has(key)) {
            this._settings.set_strv(key, [accel]);
            const action = Main.wm.addKeybinding(key, this._settings, Meta.KeyBindingFlags.NONE,
                Shell.ActionMode.NORMAL, () => this._daemon('Key', {name}));
            if (action === Meta.KeyBindingAction.NONE)
                return {ok: false, reason: 'conflict'};
            this._grabs.set(name, {kind: 'wm', key});
            return {ok: true};
        }
        const action = global.display.grab_accelerator(accel, Meta.KeyBindingFlags.NONE);
        if (action === Meta.KeyBindingAction.NONE)
            return {ok: false, reason: 'conflict'};
        Main.wm.allowKeybinding(Meta.external_binding_name_for_action(action),
            Shell.ActionMode.NORMAL);
        this._grabs.set(name, {kind: 'accel', action, accel});
        this._actions.set(action, name);
        return {ok: true};
    }

    _mUngrab({name}) {
        this._guard?.asked(name);
        this._ungrab(name);
        return {ok: true};
    }

    _ungrab(name) {
        const grab = this._grabs.get(name);
        if (!grab)
            return;
        this._grabs.delete(name);
        if (grab.kind === 'wm') {
            Main.wm.removeKeybinding(grab.key);
        } else {
            global.display.ungrab_accelerator(grab.action);
            Main.wm.allowKeybinding(Meta.external_binding_name_for_action(grab.action),
                Shell.ActionMode.NONE);
            this._actions.delete(grab.action);
        }
    }

    _accelerator(action) {
        const name = this._actions.get(action);
        if (name)
            this._guard.pressed(done => this._daemon('Key', {name}, done));
    }

    // write-back keys

    _onTarget(id) {
        const w = global.display.focus_window;
        return Boolean(w) && !this._shellHasKeyboard() && (!id || String(w.get_id()) === String(id));
    }

    _tap(keyval) {
        const t = GLib.get_monotonic_time();
        this._kbd.notify_keyval(t, keyval, Clutter.KeyState.PRESSED);
        this._kbd.notify_keyval(t + 1, keyval, Clutter.KeyState.RELEASED);
    }

    _mTypeText({text, window, delay_ms: delayMs}, done) {
        if (typeof text !== 'string') {
            done({ok: false, reason: 'bad request'});
            return;
        }
        const chars = [...text];
        let i = 0;
        const step = () => {
            while (i < chars.length) {
                if (!this._onTarget(window)) {   // before every single key
                    done({ok: false, aborted: true, typed: i});
                    return GLib.SOURCE_REMOVE;
                }
                this._tap(Clutter.unicode_to_keysym(chars[i].codePointAt(0)));
                i++;
                if (delayMs > 0 && i < chars.length) {
                    GLib.timeout_add(GLib.PRIORITY_DEFAULT, delayMs, step);
                    return GLib.SOURCE_REMOVE;
                }
            }
            done({ok: true, typed: i});
            return GLib.SOURCE_REMOVE;
        };
        step();
    }

    _mChord({keys, window}) {
        if (!Array.isArray(keys) || keys.length === 0)
            return {ok: false, reason: 'bad request'};
        if (!this._onTarget(window))
            return {ok: false, aborted: true};
        const held = keys.slice(0, -1).map(k => MODIFIER_KEYVALS[k]);
        const key = Clutter[`KEY_${keys[keys.length - 1]}`];
        if (held.some(k => k === undefined) || key === undefined)
            return {ok: false, reason: 'unknown key'};
        let t = GLib.get_monotonic_time();
        const down = [];
        try {
            for (const k of [...held, key]) {
                this._kbd.notify_keyval(t++, k, Clutter.KeyState.PRESSED);
                down.push(k);
            }
        } finally {
            // whatever went down comes up again: a modifier left down corrupts all input
            for (const k of down.reverse())
                this._kbd.notify_keyval(t++, k, Clutter.KeyState.RELEASED);
        }
        return {ok: true};
    }

    // clipboard

    _clipType(which) {
        return which === 'primary' ? St.ClipboardType.PRIMARY : St.ClipboardType.CLIPBOARD;
    }

    _mClipboardSave({which}, done) {
        const clip = St.Clipboard.get_default();
        const type = this._clipType(which);
        const mimes = clip.get_mimetypes(type) ?? [];
        if (mimes.length === 0) {
            done({restorable: true, text: null});
            return;
        }
        if (mimes.some(m => !TEXT_MIMES.has(m) && !m.startsWith('text/plain'))) {
            done({restorable: false});
            return;
        }
        clip.get_text(type, (_c, text) => done({restorable: true, text: text ?? ''}));
    }

    _mClipboardSet({which, text}) {
        if (typeof text !== 'string')
            return {ok: false};
        St.Clipboard.get_default().set_text(this._clipType(which), text);
        return {ok: true};
    }

    _mClipboardRestore({which, text, after_ms: afterMs}, done) {
        // The pasting application reads the clipboard asynchronously after Ctrl+V: give it
        // a moment before the user's content goes back.
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, Math.max(0, afterMs ?? 0), () => {
            const clip = St.Clipboard.get_default();
            if (typeof text === 'string')
                clip.set_text(this._clipType(which), text);
            else
                clip.set_content(this._clipType(which), 'text/plain', new GLib.Bytes(new Uint8Array()));
            done({ok: true});
            return GLib.SOURCE_REMOVE;
        });
    }

    _mReadPrimary({max_bytes: maxBytes}, done) {
        St.Clipboard.get_default().get_text(St.ClipboardType.PRIMARY, (_c, text) => {
            if (text && maxBytes && new TextEncoder().encode(text).length > maxBytes)
                done({text: null, too_large: true});
            else
                done({text: text ?? null});
        });
    }

    _mWatchSelection({on}) {
        return this._selwatch.set(Boolean(on));
    }

    // "Rewrite as…"

    _mAsk(payload) {
        this._overlay.ask(payload, this._window);
        return {ok: true};
    }

    // Glance Shot (shot.js)

    _mShotPick(payload) {
        return this._shot.pick(payload);
    }

    _mShotCancel() {
        return this._shot.cancel();
    }

    _mShotCapture(payload, done) {
        this._shot.capture(payload, done);
    }

    _mShotBar(payload) {
        return payload?.hide ? this._shot.hideBar() : this._shot.showBar(payload);
    }

    _mShotClipboard(payload) {
        return this._shot.copyImage(payload);
    }

    // ------------------------------------------------------------------ test hooks
    // Exported only when gnome-shell runs with GLANCE_SHELL_TEST=1 (the private test shell).

    _mTestPointer({x, y, how, button}) {
        if (!this._pointer)
            this._pointer = this._seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        if (how === 'button') {
            this._pointer.notify_button(GLib.get_monotonic_time(), button ?? Clutter.BUTTON_PRIMARY,
                x ? Clutter.ButtonState.PRESSED : Clutter.ButtonState.RELEASED);
            return {ok: true};
        }
        if (how === 'warp')
            this._seat.warp_pointer(x, y);
        else if (how === 'rel')
            this._pointer.notify_relative_motion(GLib.get_monotonic_time(), x, y);
        else
            this._pointer.notify_absolute_motion(GLib.get_monotonic_time(), x, y);
        return {ok: true};
    }

    _mTestHideOverview() {
        Main.overview.hide();
        return {ok: true};
    }

    _mTestMoveWindow({x, y, w, h}) {
        const win = global.display.focus_window;
        if (!win)
            return {ok: false};
        win.move_resize_frame(true, x, y, w, h);
        return {ok: true};
    }

    _mTestAskText({text, submit}) {
        return {ok: this._overlay.askText(text, Boolean(submit))};
    }

    _mTestOverlay() {
        return {...this._overlay.describe(), selwatch: this._selwatch?.stats ?? null,
            shot: this._shot.describe()};
    }

    // The keys held for glanced now: its keybindings ('wm') and transient grabs ('accel').
    _mTestGrabs() {
        return {grabs: [...this._grabs].map(([name, g]) => ({name, kind: g.kind}))};
    }

    _mTestClick({target}, done) {
        const actor = this._overlay.actorFor(target) ?? this._shot.actorFor(target);
        if (!actor || !actor.mapped) {
            done({ok: false});
            return;
        }
        const [ax, ay] = actor.get_transformed_position();
        const [w, h] = actor.get_transformed_size();
        if (!this._pointer)
            this._pointer = this._seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        const tx = Math.round(ax + w / 2), ty = Math.round(ay + h / 2);
        // A new virtual pointer's first motion lands on a wrong axis: move twice.
        this._pointer.notify_absolute_motion(GLib.get_monotonic_time(), tx, ty);
        this._pointer.notify_absolute_motion(GLib.get_monotonic_time() + 1, tx, ty);
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 60, () => {
            const [px, py] = global.get_pointer();
            const hit = global.stage.get_actor_at_pos(Clutter.PickMode.REACTIVE, px, py);
            this._lastClick = {at: [ax + w / 2, ay + h / 2], pointer: [px, py], hit: String(hit),
                same: hit === actor || actor.contains(hit)};
            const t2 = GLib.get_monotonic_time();
            this._pointer.notify_button(t2, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.PRESSED);
            this._pointer.notify_button(t2 + 1000, Clutter.BUTTON_PRIMARY, Clutter.ButtonState.RELEASED);
            done({ok: true, ...this._lastClick});
            return GLib.SOURCE_REMOVE;
        });
    }

    _mTestScreenshot({path}, done) {
        const file = Gio.File.new_for_path(path);
        const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
        const shot = new Shell.Screenshot();
        shot.screenshot(false, stream, (_s, res) => {
            try {
                shot.screenshot_finish(res);
                stream.close(null);
                done({ok: true});
            } catch (e) {
                done({ok: false, error: String(e.message ?? e)});
            }
        });
    }
}
