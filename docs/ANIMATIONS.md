# DropTouch animations — a beginner's map

This page lists **every animation** in DropTouch, in both directions, and tells you where it lives,
what it does and what to change. Line numbers are approximate (as of extension v9 / app 0.10.0) —
search for the function name if they have drifted.

**Paths used below**
- `ext/` = `gnome-extension/droptouch@droptouch.dev/` (GNOME Shell extension, JavaScript)
- `app/` = `android/app/src/main/java/dev/droptouch/` (Android app, Kotlin + Jetpack Compose)

## How to try a change

**Laptop (extension):** edit the file in your checkout, then reinstall and re-login:
```sh
rm -rf ~/.local/share/gnome-shell/extensions/droptouch@droptouch.dev
cp -r gnome-extension/droptouch@droptouch.dev ~/.local/share/gnome-shell/extensions/
# log out and back in (Wayland can't reload extensions in place)
journalctl --user -f -o cat | grep DropTouch       # watch the logs; JS errors appear here too
```
Run `cd gnome-extension && node --test test/` afterwards — it checks the maths in `physics.js` / `outlive.js`.

**Phone (app):** `cd android && ./gradlew assembleDebug`, then `adb install -r app/build/outputs/apk/debug/app-debug.apk`
(or `send-to-phone --update-app app/build/outputs/apk/debug/app-debug.apk` and tap Install).

**Basic vocabulary**
- *duration* — milliseconds. In GNOME `actor.ease({…, duration: 250})`; in Compose `tween(250)`.
- *easing / mode* — the speed curve. GNOME: `Clutter.AnimationMode.EASE_OUT_QUAD`, `EASE_OUT_BACK` (small overshoot), `LINEAR`.
  Compose: `FastOutSlowInEasing`, `LinearEasing`.
- *spring* — physics-based motion. Higher **stiffness** = faster/snappier; **damping** < 1 = bouncy, 1 = no bounce.
  Compose: `spring(dampingRatio = 0.6f, stiffness = Spring.StiffnessLow)`.
- *colors* — GNOME colours live in `ext/stylesheet.css` (CSS-like: `rgba(61,90,254,0.55)` is the DropTouch blue `#3d5afe`).
  Android uses `Color(0xFF3D5AFE)` or the theme colour `MaterialTheme.colorScheme.primary`.

**Turning animations off globally**
- Laptop: GNOME Settings → Accessibility → *Reduce Animation* (the extension checks `animationsEnabled()`, `ext/card.js` ~L28).
- Phone: Developer options → *Animator duration scale: off* (the app checks `reducedMotion()`, `app/ui/HandoffScreen.kt` ~L57).
  Cards then appear in place without flight.

---

## 1. Phone → laptop (you flick a file from the phone)

| # | Effect | Where | What it does | Common tweaks |
|---|--------|-------|--------------|---------------|
| 1.1 | **Drag card on the phone** (lift, shadow, tilt, stretch) | `app/ui/HandoffScreen.kt` `HandoffCard` ~L89–125 | Card scales up when grabbed (`lift`), shadow grows (`elevation`), tilts with velocity (`smoothVel`). | Lift: `spring(dampingRatio = 0.55f…)` ~L114. Shadow: `if (dragging) 26f else 10f` ~L115. Disable tilt: make `smoothVel` always `Offset.Zero`. |
| 1.2 | **Pull toward the laptop** while BLE says "touching" | same file ~L121 | A soft spring pulls the card toward the facing edge. | `spring(dampingRatio = 0.5f, stiffness = Spring.StiffnessLow)`; set the target to zero to disable. |
| 1.3 | **Fly-off after a flick** | same file ~L160–175 (`anim.animateTo(end, tween(e.durationMs…))`) | Card leaves the phone through the flicked edge, speed from `FlingMath.exit`. | Speed: `FlingMath.kt` `MIN_OUT_SPEED_WIDTHS` (L12) and `FLING_WIDTHS` (L13, how hard a flick must be). |
| 1.4 | **Snap back** (released without a flick) | same file ~L268–275 | Spring back to centre. | `spring(0.62f, Spring.StiffnessMediumLow)` — lower first number = bouncier. |
| 1.5 | **Phone edge glow** | `app/ui/HandoffScreen.kt` `EdgeGlowOverlay` ~L63; intensity ~L206 | Gradient on the edge the card will leave through. | Colour argument (`MaterialTheme.colorScheme.primary`); fade speed `tween(90)` ~L206; disable by passing intensity `0f`. |
| 1.6 | **Old drop stage / pulse rings** (main screen) | `app/ui/Animations.kt` `DropStage` ~L28–60 | Pulsing rings, progress lift, fade when done. | Pulse period `tween(1400)` L31, fade `tween(900, delayMillis = 600)` L38. |
| 1.7 | **Laptop: continuous-drag card** (part "past" the phone edge appears on the laptop) | `ext/live.js` `LiveDrag` L21–124 | Follows the phone's finger with smoothing; slides out if frames stop. | `SMOOTH_TAU` L19 (smaller = snappier), `WATCHDOG_MS` L18, size `CARD_PX` L17. |
| 1.8 | **Laptop: landing card flight** | `ext/card.js` `FlungCard` L41; physics `_startPhysics` ~L218, `_step` ~L270, `_land` ~L291 | Card enters from the mapped edge, flies with spring physics, lands, overshoots softly. | Spring: `ext/physics.js` `K` (L2, stiffness) and `C` (L3, `0.72` = damping, 1.0 = no overshoot). Card size `CARD_PX` card.js L15. Landing height: `_init` ~L105 (rest y clamp 35–45 % of the screen). Disable flight: set `animationsEnabled()` to return `false`. |
| 1.9 | **Laptop: lift + motion stretch/tilt** | `ext/physics.js` `motionStyle` L115; `LIFT` card.js L21 | Stretches the card along its motion and tilts it. | `LIFT = 1.08`; to disable stretch/tilt return `{tilt: 0, scaleX: 1, scaleY: 1}` from `motionStyle`. |
| 1.10 | **Laptop: progress ring** | `ext/card.js` `_paintRing` ~L322 | Blue ring around the card border fills with the transfer. | Colour inside `_paintRing` (blue `0x3d/0x5a/0xfe`), `RING_PAD` L22. |
| 1.11 | **Laptop: multi-file fan** | `ext/physics.js` `fanLayout` L127 | Back cards spread on landing. | Angles/offsets in `fanLayout`; return `[]` to disable. |
| 1.12 | **Laptop: morph into a file tile, fly to the Desktop icon** | `ext/card.js` `_morph`/`_morphInner` ~L386–450, `_flyToDesktopIcon` ~L451 | Thumbnail shrinks into a "file saved" tile; if a Desktop icon exists it flies there. | `LINGER_MS` L17 (how long the tile stays), `TEARDOWN_MS` L18 (hard cap). Disable the fly-to-icon: `return false` at the top of `_flyToDesktopIcon`. |
| 1.13 | **Laptop: edge glow** (shared, one at a time) | `ext/edgeglow.js` `EdgeGlow.set` L71, `hide` L110 | Blue gradient along the entry edge; green when "touching". | `THICK` L15 (size), `SPAN` L16 (length), `IDLE_MS` L17; colours `.droptouch-glow` in `stylesheet.css` L1. Disable: make `set()` return immediately. |
| 1.14 | **Laptop: approach ghost + "Receiving…" card** | `ext/extension.js` `Approach` ~L78, `DragUpdate` ~L173, `_ensureCard` ~L98 | Glowing ghost while the phone gets close (BLE); small status card with a progress bar. | Styles `.droptouch-ghost`, `.droptouch-card`, `.droptouch-bar` in `stylesheet.css`; fade `EASE` (extension.js L45) and `_fadeOutGlow` ~L207. |
| 1.15 | **Safety nets** (not visual, but they end animations) | `ext/util.js` `easeSafe` L14; `card.js` `_armTeardown` ~L89 | If an ease never completes, the end state is applied anyway; every card is destroyed after a cap. | Leave these on — they prevent stuck cards. |

## 2. Laptop → phone (you drag a file to a screen edge on the laptop)

| # | Effect | Where | What it does | Common tweaks |
|---|--------|-------|--------------|---------------|
| 2.1 | **Edge glow while dragging** | `ext/outgoing.js` `_onDndMove` ~L182 | The nearest edge glows more the closer the pointer gets; green right at the edge. | `GLOW_RANGE` L30 (px where it starts); green threshold `n.proximity > 0.97`. |
| 2.2 | **Drop pad bounce** | `ext/outgoing.js` `_onDndEnter` ~L170 / `_onDndLeave` ~L197 | The bottom drop pad scales up while a drag is active. | `scale_x: 1.08`, `duration: 220`, `EASE_OUT_BACK`; delete the `a.ease(...)` lines to disable. |
| 2.3 | **NEW v9: live peek on the phone** (reverse continuous drag) | Laptop: `ext/outlive.js` (all of it) + hook in `outgoing.js _onDndMove`. Phone: `app/OutLive.kt` (`OutLiveModel`), `app/ui/IncomingOverlay.kt` `OutLiveOverlay` ~L188–230 | As the pointer nears an edge, the part of the card "past" the laptop edge appears on the phone from the mapped edge. Leaving the edge or cancelling slides it back out; dropping hands it to 2.5. | See *Where to tweak the new feature* below. |
| 2.4 | **Throw-out card on the laptop** | `ext/outgoing.js` `SendOut` ~L213, `_animate` ~L233 | On drop the card is thrown out through the edge with gravity, tilt and stretch. | `GRAVITY_CARDS` L32 (pull), `MIN_OUT_CARDS` L33 (minimum speed), pick-up pop `scale 0.85 → 1.08, duration: 160`; 3 s safety deadline at the end of `_animate`. |
| 2.5 | **Phone: incoming card flight + landing** | `app/ui/IncomingOverlay.kt` `IncomingOverlay` ~L55–110; physics `app/SpringPhysics.kt` | Enters from the mapped edge (or from the live-peek position), springs to rest. | `SpringPhysics.K` (L9, stiffness); landing bounds (`bounds = …` ~L70: margins `24`, `48`, `160` px). Disable flight: `if (reduced)` path — force `reduced = true`. |
| 2.6 | **Phone: entry-edge glow** | same ~L95–98 | Strong while crossing the edge, fades as the card moves in. | `tween(120)`; `sh * 0.45` = how far in it fades. |
| 2.7 | **Phone: progress ring, landed pulse, fan, morph to saved tile** | same ~L100–175 | Ring around the border (`tween(200)`), expanding outline when landed (`tween(650)`), fan spread (`spring(0.55, StiffnessLow)`), morph into "Tap to open" (`spring(0.6)`). | Accent colour `Color(0xFF3D5AFE)` ~L109; pulse size `28.dp`; how long the card stays: `delay(… 8000)` ~L90. |

---

## Where to tweak the new feature (reverse continuous drag, v9 / 0.10.0)

| Want to… | Change |
|----------|--------|
| Show more/less of the card on the phone at the edge | `ext/outlive.js` `MAX_OFFSET` (0.5 = half the card) |
| Start the peek earlier/later | The peek starts when the pointer is within half a card of the edge (`outOffset` in `outlive.js`); the glow range is `GLOW_RANGE` in `outgoing.js` |
| Send fewer/more updates | `outlive.js` `MIN_INTERVAL_MS` (16 ms ≈ 60/s) |
| How long a drop may take before the phone treats it as cancelled | `outlive.js` `CANCEL_GRACE_MS` (laptop) and `OutLiveModel(commitWaitMs = 3000)` in `app/OutLive.kt` (phone) |
| Phone watchdog when frames stop | `OutLiveModel(watchdogMs = 1500)` |
| Smoother / snappier following on the phone | `OUTLIVE_SMOOTH_MS` in `app/ui/IncomingOverlay.kt` (90 ms; 0 = instant) |
| Card size / shape on the phone | `OUTLIVE_CARD_W` (0.62 of the phone width), `OUTLIVE_ASPECT` (1.25), `RoundedCornerShape(22.dp)` |
| Colours | Card `MaterialTheme.colorScheme.surfaceVariant`, icon + glow `MaterialTheme.colorScheme.primary` in `OutLiveOverlay` |
| Turn it off | The phone's **Continuous drag** switch (the phone ignores "outlive" frames when it is off). To remove the glow only: delete the `EdgeGlowOverlay(...)` line in `OutLiveOverlay`. |

Data path, for orientation: `outgoing.js _onDndMove` → `OutLiveTracker` → D-Bus signal `OutLive(edge, along, offset)` /
`OutLiveEnd(commit)` → receiver `shell_bridge.py handle_signal` → `outbox.py PhoneLink.live` (newest-only, under the writer lock) →
phone `InboxService` "outlive" frame → `OutLiveModel` → `OutLiveOverlay`. On drop the file's entry frame calls
`OutLiveModel.handoff()` and `IncomingOverlay` continues from the same spot.

## v14 / app 0.10.5

- All GNOME edge glows are off (`GLOWS_ENABLED = false` in `edgeglow.js`; set to `true` to bring them back). The card/peek is the only feedback.
- Laptop to phone live peek also shows when the phone app is in the background, via a click-through window. Needs Android "Display over other apps" (Settings > Apps > DropTouch > Appear on top); the phone posts a one-time notification linking there.
- Logs: `journalctl --user -u droptouch | grep OutLive`, GNOME `journalctl /usr/bin/gnome-shell | grep OutLive`, phone `adb logcat -s DropTouch`.
