"""Desktop notification with a Cancel button while files go to the phone (0.20.1).

Every desktop → phone path (drag to a screen edge, Files › Send to Phone, the DropTouch window's
"Send file…", the send-to-phone CLI) ends in Outbox.push, so the notification lives here in the
receiver: shown after DELAY s (quick sends never flash one), its text follows the current file, and
it is closed when the push ends. "Cancel" → Outbox.cancel(job id).

Talks to org.freedesktop.Notifications on the receiver's session bus (ShellBridge.bus); without a
bus (headless, tests) it does nothing. DROPTOUCH_NO_NOTIFY=1 turns it off.
"""
import asyncio
import logging
import os

log = logging.getLogger("droptouch.notify")
NOTIF = "org.freedesktop.Notifications"
NOTIF_PATH = "/org/freedesktop/Notifications"
ACTION = "cancel"


class SendNotifier:
    DELAY = 0.8

    def __init__(self, shell, on_cancel):
        self.shell, self.on_cancel = shell, on_cancel
        self.jobs: dict[int, dict] = {}   # job id -> {"summary","body","nid","timer","dismissed"}
        self.by_nid: dict[int, int] = {}  # notification id -> job id
        self._daemon = None               # unique bus name of the notification server
        self._listening = False
        self._tasks = set()

    @property
    def bus(self):
        return getattr(self.shell, "bus", None)

    def _spawn(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def start(self, job_id, summary, body=""):
        if self.bus is None or os.environ.get("DROPTOUCH_NO_NOTIFY"):
            return
        st = {"summary": summary, "body": body, "nid": 0, "timer": None, "dismissed": False}
        self.jobs[job_id] = st
        st["timer"] = asyncio.get_running_loop().call_later(self.DELAY, lambda: self._spawn(self._show(job_id)))

    def update(self, job_id, summary=None, body=None):
        st = self.jobs.get(job_id)
        if st is None:
            return
        new = (summary or st["summary"], st["body"] if body is None else body)
        if new == (st["summary"], st["body"]):
            return
        st["summary"], st["body"] = new
        if st["nid"] and not st["dismissed"]:
            self._spawn(self._show(job_id))

    def finish(self, job_id):
        st = self.jobs.pop(job_id, None)
        if st is None:
            return
        if st["timer"]:
            st["timer"].cancel()
        if st["nid"]:
            self.by_nid.pop(st["nid"], None)
            self._spawn(self._close(st["nid"]))

    async def _listen(self):
        if self._listening:
            return
        from dbus_next import Message
        await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                    interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                                    body=[f"type='signal',interface='{NOTIF}'"]))
        self.bus.add_message_handler(self.handle_signal)
        self._listening = True

    async def _show(self, job_id):
        st = self.jobs.get(job_id)
        if st is None or st["dismissed"] or self.bus is None:
            return
        from dbus_next import Message, MessageType, Variant
        try:
            await self._listen()
            hints = {"urgency": Variant("y", 1), "category": Variant("s", "transfer"),
                     "suppress-sound": Variant("b", True)}
            reply = await self.bus.call(Message(
                destination=NOTIF, path=NOTIF_PATH, interface=NOTIF, member="Notify", signature="susssasa{sv}i",
                body=["DropTouch", st["nid"], "droptouch", st["summary"], st["body"], [ACTION, "Cancel"], hints, 0]))
        except Exception as e:
            log.info("send notification unavailable: %r", e)
            return
        if reply is None or reply.message_type == MessageType.ERROR:
            log.info("send notification failed: %s", getattr(reply, "body", None))
            return
        nid = int(reply.body[0])
        self._daemon = reply.sender
        if job_id not in self.jobs:          # the push ended while Notify was in flight
            await self._close(nid)
            return
        st["nid"] = nid
        self.by_nid[nid] = job_id

    async def _close(self, nid):
        from dbus_next import Message
        try:
            await self.bus.call(Message(destination=NOTIF, path=NOTIF_PATH, interface=NOTIF,
                                        member="CloseNotification", signature="u", body=[nid]))
        except Exception as e:
            log.info("close notification failed: %r", e)

    def handle_signal(self, msg):
        try:
            from dbus_next import MessageType
            if msg.message_type != MessageType.SIGNAL or msg.interface != NOTIF:
                return
            if self._daemon and msg.sender != self._daemon:   # only the notification server
                return
            if msg.member == "ActionInvoked":
                nid, key = int(msg.body[0]), msg.body[1]
                job_id = self.by_nid.get(nid)
                if job_id is not None and key == ACTION:
                    log.info("push #%d: Cancel pressed in the notification", job_id)
                    self.on_cancel(job_id)
            elif msg.member == "NotificationClosed":
                job_id = self.by_nid.pop(int(msg.body[0]), None)
                st = self.jobs.get(job_id) if job_id is not None else None
                if st is not None:           # user dismissed it: don't pop it up again on the next file
                    st["nid"], st["dismissed"] = 0, True
        except Exception as e:
            log.info("bad notification signal: %r", e)
        return None
