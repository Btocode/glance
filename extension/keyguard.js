// Transient key grabs (Esc, the use key, Tab while a suggestion shows) must never outlive
// a glanced that can answer for them.
//
// While the strip, a panel or the ghost is up, glanced asks the extension to grab a few
// everyday keys desktop-wide. If glanced quits, crashes or stops answering while it holds
// them, those keys would stay captured and the whole desktop would seem to hang. So:
// - glanced's bus name vanishing releases every grab it asked for (extension.js);
// - every press of a transient key is a lease: when glanced has not answered within
//   ANSWER_MS, the transient grabs are dropped (the press itself is not replayed). If the
//   answer comes late but fine, glanced is only slow, and the grabs it still wants are taken
//   back; if it never comes, they stay released.
//
// No Shell imports here, so this runs under plain gjs in the tests.
import GLib from 'gi://GLib';

export const ANSWER_MS = 300;

export class KeyGuard {
    // held(): [[name, accel], …] the transient grabs held now
    // drop(name), take(name, accel): release / grab one again
    // after(ms, fn) -> id, cancel(id): timers (GLib by default)
    constructor({held, drop, take, after = null, cancel = null, answerMs = ANSWER_MS}) {
        this._held = held;
        this._drop = drop;
        this._take = take;
        this._after = after ?? ((ms, fn) => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            fn();
            return GLib.SOURCE_REMOVE;
        }));
        this._cancel = cancel ?? (id => GLib.source_remove(id));
        this._answerMs = answerMs;
        this._epoch = 0;       // bumped when glanced goes: late answers then change nothing
        this._dropped = null;  // [[name, accel], …] released by the watchdog, to take back
    }

    // A transient key was pressed: `send(done)` tells glanced; done(true) once it answered.
    pressed(send) {
        const epoch = this._epoch;
        let settled = false;
        const timer = this._after(this._answerMs, () => {
            if (!settled && epoch === this._epoch)
                this._release();
        });
        send(ok => {
            if (settled)
                return;
            settled = true;
            try {
                this._cancel(timer);
            } catch (_e) {}  // already fired
            if (epoch !== this._epoch)
                return;
            if (!ok) {
                this._release();
                this._dropped = null;  // nobody to give them back to
            } else if (this._dropped) {
                this._restore();
            }
        });
    }

    // glanced is gone (its name vanished): forget everything pending.
    gone() {
        this._epoch++;
        this._dropped = null;
    }

    // glanced asked for or released a grab itself: its word replaces ours.
    asked(name) {
        if (this._dropped)
            this._dropped = this._dropped.filter(([n]) => n !== name);
    }

    _release() {
        const held = this._held();
        if (held.length === 0)
            return;
        this._dropped = [...(this._dropped ?? []), ...held];
        for (const [name] of held)
            this._drop(name);
    }

    _restore() {
        const dropped = this._dropped;
        this._dropped = null;
        const now = new Set(this._held().map(([n]) => n));
        for (const [name, accel] of dropped) {
            if (!now.has(name))
                this._take(name, accel);
        }
    }
}
