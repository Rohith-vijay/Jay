import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from jarvis import content, video
from jarvis.store import Store
from jarvis.tools.registry import Registry


class PlanningTests(unittest.TestCase):
    def test_script_becomes_scenes_with_the_title_first(self):
        script = "First sentence here. " * 16 + "Then the twist arrives! Why did it happen? Nobody knows."
        scenes = video.auto_scenes(script, "The Title")
        self.assertGreater(len(scenes), 2)
        self.assertEqual(scenes[0].on_screen, "The Title")
        self.assertTrue(all(len(s.narration) < 260 for s in scenes))
        self.assertIn("Nobody knows.", scenes[-1].narration)

    def test_captions_group_into_short_phrases(self):
        words = [video.Word(w, i * 0.3, i * 0.3 + 0.25) for i, w in enumerate("It was a quiet night, and nobody saw it coming.".split())]
        phrases = video.caption_phrases(words)
        self.assertTrue(all(len(p) <= 4 for p in phrases))
        self.assertEqual([w.text for p in phrases for w in p], [w.text for w in words])
        self.assertTrue(any(p[-1].text == "night," for p in phrases))        # a comma ends a phrase

    def test_words_are_spread_evenly_when_the_voice_gives_no_timing(self):
        words = video.even_words("one two three four", 4.0)
        self.assertEqual(len(words), 4)
        self.assertAlmostEqual(words[-1].end, 4.0, places=3)
        self.assertEqual(video.even_words("", 3.0), [])


class RenderTests(unittest.TestCase):
    """A real render of a small video, with the voice replaced by a tone so no network is needed."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def render(self, scenes, vertical=True):
        async def fake_speak(text, voice, rate, mp3):
            Path(mp3).write_bytes(b"x")
            return [video.Word(w, i * 0.25, i * 0.25 + 0.2) for i, w in enumerate(text.split())]

        tone = (0.2 * np.sin(np.linspace(0, 2 * np.pi * 220 * 1.5, int(video.SAMPLE_RATE * 1.5)))).astype("float32")
        with mock.patch.object(video, "_speak", fake_speak), mock.patch.object(video, "decode_audio", return_value=tone), \
                mock.patch.object(video, "VERTICAL", (270, 480)), mock.patch.object(video, "HORIZONTAL", (480, 270)):
            return video.VideoMaker(Path(self.dir.name)).make("Test Title", scenes, vertical=vertical, name="t")

    def test_it_produces_a_playable_mp4_and_a_thumbnail(self):
        import av

        result = self.render([video.Scene("one two three four five six", "Heading"), video.Scene("seven eight nine")])
        self.assertEqual(result.scenes, 2)
        self.assertAlmostEqual(result.seconds, 2 * (1.5 + video.PAD_SECONDS), places=1)
        with av.open(result.path) as container:
            kinds = {s.type: s for s in container.streams}
            self.assertIn("video", kinds)
            self.assertIn("audio", kinds)
            self.assertEqual((kinds["video"].width, kinds["video"].height), (270, 480))
            self.assertAlmostEqual(float(container.duration) / 1e6, result.seconds, delta=0.5)
            frames = [f for _, f in zip(range(5), container.decode(video=0))]
            self.assertTrue(frames)
        self.assertTrue(Path(result.thumbnail).stat().st_size > 1000)

    def test_horizontal_format_and_missing_pictures_are_noted(self):
        import av

        result = self.render([video.Scene("hello there world", "", "some search phrase")], vertical=False)
        with av.open(result.path) as container:
            self.assertEqual((container.streams.video[0].width, container.streams.video[0].height), (480, 270))
        self.assertTrue(any("no picture found" in n for n in result.notes))

    def test_a_picture_from_disk_is_used(self):
        from PIL import Image

        picture = Path(self.dir.name) / "p.jpg"
        Image.new("RGB", (600, 400), (200, 30, 30)).save(picture)
        self.assertEqual(self.render([video.Scene("one two three", "", str(picture))]).notes, [])

    def test_nothing_to_say_is_refused(self):
        with self.assertRaises(ValueError):
            video.VideoMaker(Path(self.dir.name)).make("x", [])


class JobTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(Path(self.dir.name) / "t.db")
        self.addCleanup(self.store._db.close)
        self.queue = content.ContentQueue(self.store)
        self.reg = Registry()
        self.phone = mock.Mock()
        self.phone.available.return_value = True
        self.told = []
        self.videos = video.Videos(Path(self.dir.name), self.queue, self.phone, lambda t, x: self.told.append(x))
        video.register(self.reg, self.videos, self.queue)

    def wait(self, job=1):
        for _ in range(100):
            if self.videos.jobs[job]["status"] != "running":
                return
            threading.Event().wait(0.05)

    def fake_make(self, title, scenes, vertical=True, name=None):
        video_file = Path(self.dir.name) / "out.mp4"
        video_file.write_bytes(b"0" * 100)
        self.made = (title, scenes, vertical)
        return video.VideoResult(str(video_file), str(Path(self.dir.name) / "out.jpg"), 42.0, len(scenes), ["scene 2: no picture"])

    def test_a_draft_becomes_a_video_that_is_attached_and_sent(self):
        item_id = self.queue.add("instagram", "reel", "Lore reel", script="The first thing. The second thing. The third thing.")
        self.queue.approve(item_id)
        with mock.patch.object(video.VideoMaker, "make", self.fake_make):
            out = self.reg.execute("make_video", {"title": "Lore reel", "content_id": item_id}, lambda s: True).text
            self.assertIn("Making video #1", out)
            self.wait()
        self.assertTrue(self.made[2])                                          # a reel is vertical
        item = self.queue.get(item_id)
        self.assertTrue(item["media_path"].endswith("out.mp4"))
        self.assertEqual(item["status"], "approved")                           # attaching the video keeps the approval
        self.phone.send_video.assert_called_once()
        self.assertTrue(any("attached to draft" in t for t in self.told))

    def test_scenes_from_the_model_are_used_and_long_videos_are_horizontal(self):
        scenes = [{"narration": "Hello", "on_screen": "Hi", "image": "old castle"}, {"narration": "World"}]
        with mock.patch.object(video.VideoMaker, "make", self.fake_make):
            self.reg.execute("make_video", {"title": "T", "scenes": scenes, "format": "horizontal"}, lambda s: True)
            self.wait()
        title, plan, vertical = self.made
        self.assertEqual([s.image for s in plan], ["old castle", ""])
        self.assertFalse(vertical)

    def test_helpful_errors(self):
        self.assertIn("Give the scenes", self.reg.execute("make_video", {"title": "T"}, lambda s: True).text)
        self.assertIn("No draft #9", self.reg.execute("make_video", {"title": "T", "content_id": 9}, lambda s: True).text)
        empty = self.queue.add("youtube", "long video", "No script")
        self.assertIn("Give the scenes", self.reg.execute("make_video", {"title": "T", "content_id": empty}, lambda s: True).text)

    def test_a_failed_render_is_reported_not_hidden(self):
        def boom(self_, *a, **k):
            raise RuntimeError("The voice service failed")

        with mock.patch.object(video.VideoMaker, "make", boom):
            self.reg.execute("make_video", {"title": "T", "scenes": [{"narration": "x"}]}, lambda s: True)
            self.wait()
        self.assertEqual(self.videos.jobs[1]["status"], "failed")
        self.assertTrue(any("failed" in t for t in self.told))

    def test_large_files_are_not_pushed_through_telegram(self):
        big = Path(self.dir.name) / "big.mp4"
        big.write_bytes(b"0" * 10)
        with mock.patch("os.path.getsize", return_value=60 * 1024 * 1024):
            self.videos._announce("Ready", str(big))
        self.phone.send_video.assert_not_called()
        self.assertIn("too big", self.phone.send.call_args.args[1])


class ContentLinkTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = Store(Path(self.dir.name) / "t.db")
        self.addCleanup(self.store._db.close)             # runs first, so Windows lets the folder go

    def test_media_does_not_revoke_approval_but_text_changes_do(self):
        queue = content.ContentQueue(self.store)
        item_id = queue.add("youtube", "short", "T", script="s")
        queue.approve(item_id)
        queue.revise(item_id, media_path="C:/v.mp4")
        self.assertEqual(queue.get(item_id)["status"], "approved")
        queue.attach_media(item_id, "C:/v.mp4", "C:/t.jpg")
        self.assertIn("Thumbnail: C:/t.jpg", content.full(queue.get(item_id)))
        queue.revise(item_id, script="a different script")
        self.assertEqual(queue.get(item_id)["status"], "draft")
        self.assertIn("C:/v.mp4", content.due_message(queue.get(item_id)))


if __name__ == "__main__":
    unittest.main()
