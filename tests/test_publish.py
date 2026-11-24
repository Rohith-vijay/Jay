import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from jarvis import content, publish
from jarvis.grants import Grants
from jarvis.store import Store
from jarvis.tools.registry import Registry


class FakeYouTube:
    """Stands in for the YouTube API client: records what would have been uploaded."""

    def __init__(self, fail_with=None):
        self.inserted, self.thumbs, self.fail_with = [], [], fail_with

    def videos(self):
        return self

    def thumbnails(self):
        return self

    def insert(self, part, body, media_body):
        if self.fail_with and "containsSyntheticMedia" in body["status"] and self.fail_with == "synthetic":
            raise RuntimeError("Unknown name containsSyntheticMedia")
        self.inserted.append(body)
        return NS(next_chunk=lambda: (None, {"id": "VID123"}))

    def set(self, videoId, media_body):
        self.thumbs.append(videoId)
        return NS(execute=lambda: {})


class NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeInstagramHttp:
    def __init__(self, states=("IN_PROGRESS", "FINISHED"), error=None):
        self.calls, self.states, self.error = [], list(states), error

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url))
        if url.endswith("/me"):
            return FakeResponse({"user_id": "17"})
        if "fields" in (params or {}) and params["fields"] == "permalink":
            return FakeResponse({"permalink": "https://instagram.com/reel/XYZ/"})
        return FakeResponse({"status_code": self.states.pop(0)})

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, dict(data) if isinstance(data, dict) else "file", headers))
        if self.error:
            return FakeResponse({"error": {"message": self.error, "code": 10}})
        if url.endswith("/17/media"):
            return FakeResponse({"id": "C1", "uri": "https://rupload.example/C1"})
        if "rupload" in url:
            return FakeResponse({"success": True})
        if url.endswith("/media_publish"):
            return FakeResponse({"id": "M9"})
        return FakeResponse({})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.data = Path(self.dir.name)
        self.store = Store(self.data / "t.db")
        self.addCleanup(self.store._db.close)
        self.queue = content.ContentQueue(self.store)
        (self.data / "videos").mkdir()
        self.video = self.data / "videos" / "v.mp4"
        self.video.write_bytes(b"0" * 2048)
        self.yt = FakeYouTube()
        self.ig_http = FakeInstagramHttp()
        self.env = mock.patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "tok", "INSTAGRAM_PUBLISH_TOKEN": "", "JARVIS_AUTOPUBLISH": "0",
                                                "JARVIS_MAX_POSTS_PER_DAY": "3"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.publisher = publish.Publisher(self.queue, self.data, youtube=publish.YouTubeUploader(self.data, service=self.yt),
                                           instagram=publish.InstagramPublisher(http=self.ig_http, sleep=lambda s: None))

    def ready(self, platform="youtube", kind="long video", when="2020-01-01 10:00", media=True, approve=True, **extra):
        item_id = self.queue.add(platform, kind, "The Lost Bell", caption="A caption", hashtags="#lore #history",
                                 script="the script", scheduled_at=when, **extra)
        if approve:
            self.queue.approve(item_id)
        if media:
            self.queue.attach_media(item_id, str(self.video), "")
        return item_id


class GateTests(Base):
    def refused(self, item_id, text, **kw):
        before = len(self.yt.inserted)
        with self.assertRaises(publish.PublishError) as ctx:
            self.publisher.publish(item_id, **kw)
        self.assertIn(text, str(ctx.exception))
        self.assertEqual(len(self.yt.inserted), before)           # nothing more went out

    def test_a_good_post_goes_out_once(self):
        item_id = self.ready()
        out = self.publisher.publish(item_id)
        self.assertIn("https://youtu.be/VID123", out)
        self.assertEqual(self.queue.get(item_id)["status"], "posted")
        self.assertIn("youtu.be/VID123", self.queue.get(item_id)["posted_url"])
        self.refused(item_id, "already been posted")

    def test_unapproved_drafts_are_never_posted(self):
        self.refused(self.ready(approve=False), "not approved")

    def test_rejected_drafts_are_never_posted(self):
        item_id = self.ready()
        self.queue.reject(item_id)
        self.refused(item_id, "not approved")

    def test_text_changed_after_approval_is_refused_even_behind_the_queues_back(self):
        item_id = self.ready()
        self.store._run("UPDATE content SET caption = 'sneaky new caption' WHERE id = ?", (item_id,))   # not via revise()
        self.refused(item_id, "changed after you approved")

    def test_revising_through_the_queue_needs_a_new_approval(self):
        item_id = self.ready()
        self.queue.revise(item_id, caption="better caption")
        self.refused(item_id, "not approved")
        self.queue.approve(item_id)
        self.assertIn("youtu.be", self.publisher.publish(item_id))

    def test_a_video_is_required_and_must_be_one_of_ours(self):
        self.refused(self.ready(media=False), "no video file")
        outside = Path(self.dir.name) / "elsewhere.mp4"
        outside.write_bytes(b"0")
        item_id = self.ready()
        self.store._run("UPDATE content SET media_path = ? WHERE id = ?", (str(outside), item_id))
        self.refused(item_id, "made by Jay")

    def test_the_scheduled_time_must_have_come(self):
        future = self.ready(when="2999-01-01 10:00")
        self.refused(future, "isn't due until")
        self.assertIn("youtu.be", self.publisher.publish(future, ignore_schedule=True))      # the user asked for it now
        self.refused(self.ready(when=""), "no scheduled time")

    def test_formats_that_cannot_be_posted_are_explained(self):
        self.refused(self.ready(kind="carousel"), "ready-to-post package")

    def test_platform_must_be_connected(self):
        item_id = self.ready(platform="instagram", kind="reel")
        with mock.patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "", "INSTAGRAM_PUBLISH_TOKEN": ""}):
            self.refused(item_id, "isn't connected")

    def test_daily_limit(self):
        with mock.patch.dict(os.environ, {"JARVIS_MAX_POSTS_PER_DAY": "1"}):
            publisher = publish.Publisher(self.queue, self.data, youtube=publish.YouTubeUploader(self.data, service=self.yt))
            publisher.publish(self.ready())
            with self.assertRaises(publish.PublishError) as ctx:
                publisher.publish(self.ready())
            self.assertIn("limit", str(ctx.exception))
        self.assertEqual(len(self.yt.inserted), 1)


class YouTubeTests(Base):
    def test_what_is_uploaded(self):
        self.publisher.publish(self.ready())
        body = self.yt.inserted[0]
        self.assertEqual(body["snippet"]["title"], "The Lost Bell")
        self.assertEqual(body["snippet"]["tags"], ["lore", "history"])
        self.assertIn("#lore #history", body["snippet"]["description"])
        self.assertIn("Narrated with an AI voice.", body["snippet"]["description"])     # disclosed
        self.assertTrue(body["status"]["containsSyntheticMedia"])
        self.assertEqual(body["status"]["privacyStatus"], "public")
        self.assertFalse(body["status"]["selfDeclaredMadeForKids"])

    def test_privacy_can_be_set_and_is_reported(self):
        with mock.patch.dict(os.environ, {"JARVIS_YT_PRIVACY": "private"}):
            out = self.publisher.publish(self.ready())
        self.assertIn("[private]", out)

    def test_an_api_that_does_not_know_the_synthetic_flag_gets_a_second_try(self):
        self.yt.fail_with = "synthetic"
        self.assertIn("youtu.be", self.publisher.publish(self.ready()))
        self.assertNotIn("containsSyntheticMedia", self.yt.inserted[0]["status"])

    def test_thumbnail_is_sent_when_there_is_one(self):
        thumb = self.data / "videos" / "t.jpg"
        thumb.write_bytes(b"\xff\xd8\xff")
        item_id = self.ready()
        self.queue.attach_media(item_id, str(self.video), str(thumb))
        self.publisher.publish(item_id)
        self.assertEqual(self.yt.thumbs, ["VID123"])

    def test_disconnected_uploader_explains_how_to_connect(self):
        uploader = publish.YouTubeUploader(self.data)                  # no token, no service
        self.assertFalse(uploader.configured())
        with self.assertRaises(publish.PublishError) as ctx:
            uploader.service()
        self.assertIn("/connect youtube", str(ctx.exception))


class InstagramTests(Base):
    def test_a_reel_is_created_uploaded_processed_and_published(self):
        item_id = self.ready(platform="instagram", kind="reel")
        out = self.publisher.publish(item_id)
        self.assertIn("https://instagram.com/reel/XYZ/", out)
        created = next(c for c in self.ig_http.calls if c[0] == "POST" and c[1].endswith("/17/media"))
        self.assertEqual(created[2]["media_type"], "REELS")
        self.assertEqual(created[2]["caption"], "A caption\n\n#lore #history")
        upload = next(c for c in self.ig_http.calls if "rupload" in c[1])
        self.assertEqual(upload[3]["file_size"], "2048")
        self.assertEqual(upload[3]["Authorization"], "OAuth tok")
        urls = [c[1] for c in self.ig_http.calls]
        self.assertLess(urls.index("https://rupload.example/C1"), [i for i, u in enumerate(urls) if u.endswith("/media_publish")][0])
        self.assertEqual(self.queue.get(item_id)["status"], "posted")

    def test_the_publish_token_is_preferred_over_the_read_only_one(self):
        with mock.patch.dict(os.environ, {"INSTAGRAM_PUBLISH_TOKEN": "writer"}):
            self.publisher.publish(self.ready(platform="instagram", kind="reel"))
        self.assertEqual(next(c for c in self.ig_http.calls if "rupload" in c[1])[3]["Authorization"], "OAuth writer")

    def test_a_refusal_from_instagram_is_explained_and_nothing_is_marked_posted(self):
        self.ig_http.error = "Permissions error"
        item_id = self.ready(platform="instagram", kind="reel")
        with self.assertRaises(publish.PublishError) as ctx:
            self.publisher.publish(item_id)
        self.assertIn("content_publish", str(ctx.exception))
        self.assertEqual(self.queue.get(item_id)["status"], "approved")

    def test_caption_limits(self):
        item_id = self.queue.add("instagram", "reel", "T", caption="x" * 2300)
        with self.assertRaises(publish.PublishError):
            publish.InstagramPublisher().caption(self.queue.get(item_id))
        item_id = self.queue.add("instagram", "reel", "T", caption="c", hashtags=" ".join(f"#t{i}" for i in range(31)))
        with self.assertRaises(publish.PublishError):
            publish.InstagramPublisher().caption(self.queue.get(item_id))

    def test_both_platforms_post_separately_and_a_failure_on_one_is_reported(self):
        item_id = self.ready(platform="both", kind="reel")          # a reel can go to Instagram only; YouTube takes long videos and Shorts
        self.assertEqual(publish.platforms_for(self.queue.get(item_id)), ["instagram"])
        short = self.ready(platform="both", kind="short")
        self.assertEqual(publish.platforms_for(self.queue.get(short)), ["youtube"])


class ToolTests(Base):
    def setUp(self):
        super().setUp()
        self.reg = Registry()
        self.reg.grants = Grants(self.data)
        publish.register(self.reg, self.publisher)

    def test_a_good_post_asks_unless_a_permission_was_given(self):
        item_id = self.ready()
        asked = []
        result = self.reg.execute("publish_content", {"id": item_id}, lambda s: asked.append(s) or False)
        self.assertIn("declined", result.text)
        self.assertIn("The Lost Bell", asked[0])
        self.assertEqual(self.yt.inserted, [])
        self.reg.grants.command("/allow publish for 1d")
        result = self.reg.execute("publish_content", {"id": item_id}, lambda s: self.fail("should not ask"))
        self.assertIn("youtu.be", result.text)

    def test_a_post_that_would_be_refused_does_not_bother_the_user_and_posts_nothing(self):
        item_id = self.ready(approve=False)
        result = self.reg.execute("publish_content", {"id": item_id}, lambda s: self.fail("no card for a refusal"))
        self.assertIn("Not posted", result.text)
        self.assertIn("not approved", result.text)
        self.assertEqual(self.yt.inserted, [])

    def test_a_permission_never_overrides_the_gate(self):
        self.reg.grants.command("/allow publish forever")
        item_id = self.ready(approve=False)
        self.assertIn("Not posted", self.reg.execute("publish_content", {"id": item_id}, lambda s: True).text)
        self.assertEqual(self.yt.inserted, [])


class SessionTests(unittest.TestCase):
    def make(self):
        from jarvis.config import Config
        from jarvis.session import Session

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config.load(data_dir=Path(tmp.name))
        cfg.checkin_minutes, cfg.morning_brief = 0, False
        session = Session(cfg)
        self.addCleanup(session.agent.store._db.close)
        (Path(tmp.name) / "videos").mkdir()
        video = Path(tmp.name) / "videos" / "v.mp4"
        video.write_bytes(b"0" * 100)
        return session, str(video)

    def test_publish_now_command(self):
        session, video = self.make()
        queue, publisher = session.agent.registry.content, session.agent.registry.publisher
        item_id = queue.add("youtube", "short", "T", caption="c", scheduled_at="2999-01-01 10:00")
        queue.approve(item_id)
        queue.attach_media(item_id, video, "")
        done = threading.Event()
        with mock.patch.object(publisher, "publish", side_effect=lambda i, ignore_schedule=False: done.set() or f"Posted #{i}") as post:
            self.assertIn("Posting", session.phone_command(f"/publish {item_id}"))
            self.assertTrue(done.wait(3))
        post.assert_called_once_with(item_id, ignore_schedule=True)             # the user asked for it, so the time is waived
        self.assertIn("Say which one", session.phone_command("/publish"))

    def test_scheduled_posts_are_only_automatic_when_switched_on(self):
        session, video = self.make()
        queue = session.agent.registry.content
        item_id = queue.add("youtube", "short", "T", caption="c", scheduled_at="2020-01-01 10:00")
        queue.approve(item_id)
        queue.attach_media(item_id, video, "")
        session.agent.registry.phone = mock.Mock()
        session.agent.registry.phone.available.return_value = True
        publisher = session.agent.registry.publisher
        with mock.patch.object(publisher, "ready_platforms", return_value=["youtube"]), \
                mock.patch.object(session, "_autopublish") as auto, mock.patch("jarvis.tools.system.notify"):
            with mock.patch.dict(os.environ, {"JARVIS_AUTOPUBLISH": "0"}):
                session._deliver_due_content()
            auto.assert_not_called()                                           # off: you just get the reminder
            session.agent.registry.phone.send.assert_called_once()
            item2 = queue.add("youtube", "short", "T2", caption="c", scheduled_at="2020-01-01 10:00")
            queue.approve(item2)
            with mock.patch.dict(os.environ, {"JARVIS_AUTOPUBLISH": "1"}):
                session._deliver_due_content()
            for _ in range(50):
                if auto.called:
                    break
                threading.Event().wait(0.05)
            auto.assert_called_once_with(item2)

    def test_autopublish_asks_on_the_phone_and_falls_back_to_the_package_when_declined(self):
        session, video = self.make()
        registry = session.agent.registry
        item_id = registry.content.add("youtube", "short", "Lore short", caption="c", scheduled_at="2020-01-01 10:00")
        registry.content.approve(item_id)
        registry.content.attach_media(item_id, video, "")
        registry.content.mark_due(item_id)
        registry.phone = mock.Mock()
        registry.phone.available.return_value = True
        with mock.patch.object(registry.publisher, "ready_platforms", return_value=["youtube"]), \
                mock.patch.object(session, "confirm_on_phone", return_value=False) as asked, mock.patch("jarvis.tools.system.notify"):
            session._autopublish(item_id)
        asked.assert_called_once()
        self.assertEqual(registry.content.get(item_id)["status"], "due")        # not posted
        text = registry.phone.send.call_args.args[1]
        self.assertIn("ready to post yourself", text)


if __name__ == "__main__":
    unittest.main()
