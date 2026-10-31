"""0.15: "Connect <phone>?" prompt on the laptop, with the 6-digit code and Accept / Decline.
1. the GNOME extension's card (ShowPairRequest; buttons run `droptouch pair accept|decline`);
2. otherwise a libadwaita dialog (pair_dialog.py) — e.g. the extension wasn't reloaded yet;
3. the CLI `droptouch pair accept|decline` always works."""
import asyncio
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("droptouch.pairprompt")


async def ask(shell, device: str, code: str, on_answer, timeout: float = 60.0):
    """Shows the prompt; on_answer(True|False) for the dialog path. Returns a closer."""
    if os.environ.get("DROPTOUCH_NO_PROMPT"):
        return lambda: None
    if shell is not None and await shell.show_pair_request(device, code, int(timeout)):
        log.info("pairing prompt shown by the GNOME extension")
        return lambda: asyncio.get_running_loop().create_task(shell.hide_pair_request())
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name("pair_dialog.py")),
                                                    device, code, str(int(timeout)), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
    except Exception as e:
        log.info("pairing dialog unavailable (%s); use: droptouch pair accept", e)
        return lambda: None
    log.info("pairing prompt shown as a GTK dialog")

    async def watch():
        line = (await proc.stdout.readline()).decode().strip()
        if line in ("accept", "decline"):
            on_answer(line == "accept")
    task = asyncio.get_running_loop().create_task(watch())

    def close():
        task.cancel()
        if proc.returncode is None:
            proc.terminate()
    return close
