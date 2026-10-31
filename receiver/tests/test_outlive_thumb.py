import asyncio, base64, logging, random
from PIL import Image
from outbox import Outbox, make_thumb
from protocol import MAX_THUMB


def _photo(tmp_path):
    p = tmp_path / "sea_stones_rocks_1341262_1440x2560 (1) (1) (1).jpg"
    rnd = random.Random(1)
    im = Image.new("RGB", (1440, 2560))
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(1440 * 2560)])
    im.save(p, quality=90)
    return p


def test_thumb_for_big_noisy_photo_with_odd_name(tmp_path):
    t = make_thumb(str(_photo(tmp_path)))
    assert t and len(base64.b64decode(t)) <= MAX_THUMB and base64.b64decode(t)[:3] == b"\xff\xd8\xff"


class FakeLink:
    name = "phone"
    def __init__(self): self.frames = []
    async def frame(self, f): self.frames.append(f)
    def live(self, f): pass


def test_out_thumb_sends_one_thumb_frame_per_drag(tmp_path, caplog):
    p = _photo(tmp_path)
    ob = Outbox.__new__(Outbox)
    link = FakeLink(); ob._link = lambda: link
    async def go():
        with caplog.at_level(logging.INFO):
            assert (await ob.out_thumb(str(p)))["ok"]
            await ob.out_thumb(str(p))           # same drag, same file: not resent
            ob.out_live_end(True)
            await ob.out_thumb(str(p))           # next drag: sent again
    asyncio.run(go())
    assert [f["phase"] for f in link.frames] == ["thumb", "thumb"]
    assert "bytes -> phone phone" in caplog.text
