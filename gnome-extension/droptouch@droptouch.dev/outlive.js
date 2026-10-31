// Reverse continuous drag (laptop → phone), pure logic — no gi:// imports so node tests it
// (test/outlive.test.mjs). outgoing.js feeds it pointer positions during an xdnd drag; it emits
// the D-Bus signals OutLive(edge, along, offset) / OutLiveEnd(commit) through `emit`.
//
// offset = fraction of the (virtual) card that is already "past" the laptop edge, i.e. what the
// phone shows. The card is imagined centred on the pointer, so at the very edge half the card
// (MAX_OFFSET) is over on the phone.

export const MAX_OFFSET = 0.5;
export const MIN_INTERVAL_MS = 16;   // ≤ ~60 signals/s
export const CANCEL_GRACE_MS = 1500; // drag ended without a SendOut within this → cancel

/** dist: px from pointer to the edge, cardH: card size across that edge (px). */
export function outOffset(dist, cardH) {
    if (!Number.isFinite(dist) || !(cardH > 0)) return 0;
    return Math.min(Math.max((cardH / 2 - dist) / cardH, 0), MAX_OFFSET);
}

export class OutLiveTracker {
    /** emit(name, args), now() → ms, schedule(fn, ms) → cancel fn */
    constructor({emit, now = () => Date.now(), schedule}) {
        this._emit = emit; this._now = now; this._schedule = schedule;
        this.active = false;       // phone currently shows (or is sliding out) a live card
        this._last = 0; this._lastOffset = 0; this._cancelEnd = null;
    }

    /** Pointer moved during a drag. edge=null → not near any edge. */
    move(edge, along, dist, cardH) {
        const off = edge ? outOffset(dist, cardH) : 0;
        if (off <= 0 && !this.active) return;
        this._clearEnd();
        const t = this._now();
        const leaving = off <= 0;              // always send the "slide back" frame
        if (!leaving && t - this._last < MIN_INTERVAL_MS) return;
        if (leaving && this._lastOffset <= 0) return;
        this._last = t; this._lastOffset = off;
        this._edge = edge ?? this._edge;
        this.active = true;
        this._emit('OutLive', [this._edge, Math.min(Math.max(along ?? 0.5, 0), 1), off]);
    }

    /** dnd-leave: either a drop (SendOut follows soon) or a cancel. */
    dragEnded() {
        if (!this.active) return;
        this._clearEnd();
        this._cancelEnd = this._schedule(() => { this._cancelEnd = null; this.end(false); }, CANCEL_GRACE_MS);
    }

    /** commit=true from SendOut (the file is on its way), false on cancel. */
    end(commit) {
        this._clearEnd();
        if (!this.active) return false;
        this.active = false; this._lastOffset = 0;
        this._emit('OutLiveEnd', [!!commit]);
        return true;
    }

    _clearEnd() { if (this._cancelEnd) { this._cancelEnd(); this._cancelEnd = null; } }
}
