// Selection actions on Wayland: "a selection settled", without reading it.
//
// Watches the shell's PRIMARY selection owner (Meta.Selection 'owner-changed': it says that
// an app took or dropped the selection, never what is in it) and the pointer's left button.
// A selection made with the mouse settles SETTLE_MS after the button is up and the owner
// stopped changing: `settled(x, y)` with the pointer's position. One made while the button
// is not held (the keyboard: the user is typing in that field) or a selection dropped:
// `cleared()`. glanced turns this on and off ([selection] enabled) with WatchSelection.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';

const SETTLE_MS = 350;
const POLL_MS = 80;
const RECENT_RELEASE_MS = 600;

function nowMs() {
    return GLib.get_monotonic_time() / 1000;
}

function held() {
    const [, , mods] = global.get_pointer();
    return (mods & Clutter.ModifierType.BUTTON1_MASK) !== 0;
}

export class SelectionWatch {
    constructor({settled, cleared}) {
        this._settled = settled;
        this._cleared = cleared;
        this._sel = null;
        this._id = 0;
        this._poll = 0;
        this._held = false;
        this._releasedAt = -1e9;
        this._changedAt = 0;
        this.stats = {on: false, changes: 0, held: 0, keyboard: 0, settled: 0, cleared: 0};
    }

    set(on) {
        if (on && !this._id) {
            this._sel = global.display.get_selection();
            this._id = this._sel.connect('owner-changed',
                (_s, type, source) => this._ownerChanged(type, source));
        } else if (!on && this._id) {
            this._sel.disconnect(this._id);
            this._id = 0;
            this._sel = null;
            this._stop();
        }
        this.stats.on = Boolean(this._id);
        return {ok: true, on: Boolean(this._id)};
    }

    destroy() {
        this.set(false);
    }

    _ownerChanged(type, source) {
        if (type !== Meta.SelectionType.SELECTION_PRIMARY)
            return;
        this.stats.changes++;
        if (!source) {
            if (held())
                return;  // the app drops and retakes it while the user drags: not cleared
            this.stats.cleared++;
            this._stop();
            this._cleared();
            return;
        }
        const now = nowMs();
        if (held()) {
            this._held = true;
            this.stats.held++;
        }
        if (!this._held && now - this._releasedAt > RECENT_RELEASE_MS) {
            this.stats.keyboard++;
            this._stop();
            this._cleared();  // made with the keyboard
            return;
        }
        this._changedAt = now;
        if (!this._poll)
            this._poll = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_MS, () => this._tick());
    }

    _tick() {
        const now = nowMs();
        if (held()) {
            this._held = true;
            return GLib.SOURCE_CONTINUE;
        }
        if (this._held) {
            this._held = false;
            this._releasedAt = now;
        }
        if (now - Math.max(this._changedAt, this._releasedAt) < SETTLE_MS)
            return GLib.SOURCE_CONTINUE;
        this._poll = 0;
        const [x, y] = global.get_pointer();
        this.stats.settled++;
        this._settled(x, y);
        return GLib.SOURCE_REMOVE;
    }

    _stop() {
        if (this._poll) {
            GLib.source_remove(this._poll);
            this._poll = 0;
        }
        this._held = false;
    }
}
