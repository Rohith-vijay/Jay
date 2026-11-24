import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jarvis.tools import instagram
from jarvis.tools.registry import Registry

TODAY = date(2026, 11, 19)


def post(days_ago, hour, kind="Reel", reach=1000, caption="a post", interactions=50, saves=5, shares=3, watch=0):
    local = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time()).replace(hour=hour).astimezone()
    return {"caption": caption, "type": kind, "published": local.astimezone(timezone.utc), "reach": reach,
            "interactions": interactions, "saves": saves, "shares": shares, "likes": 10, "comments": 1, "watch_s": watch}


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.posts = [
            post(30, 20, "Reel", 9000, "Lore of the lost city #lore #history #mystery", 900, 90, 60, 8.5),
            post(24, 20, "Reel", 8000, "The truth about it #lore #history", 700, 80, 55, 7.5),
            post(18, 20, "Reel", 7000, "Nobody tells you this #lore #mystery", 600, 70, 40, 7.0),
            post(12, 9, "Photo", 600, "good morning " + "x" * 200, 20, 1, 0),
            post(8, 9, "Photo", 500, "another photo #nature", 15, 1, 0),
            post(5, 9, "Carousel", 700, "tips", 30, 3, 2),
            post(1, 20, "Reel", 100, "brand new"),                                  # too new
            post(40, 12, "Reel", 0, "no insights available"),                        # no reach data
        ]

    def test_counts_and_exclusions(self):
        result = instagram.analyse(self.posts, TODAY)
        self.assertEqual(result["considered"], 6)
        self.assertEqual(result["skipped"], 2)

    def test_reels_in_the_evening_beat_morning_photos(self):
        result = instagram.analyse(self.posts, TODAY)
        types = {r["group"]: r for r in result["by_type"]}
        self.assertGreater(types["Reel"]["avg_reach"], types["Photo"]["avg_reach"] * 5)
        self.assertEqual(types["Reel"]["avg_watch_s"], 7.7)
        parts = {r["group"]: r for r in result["by_daypart"]}
        self.assertGreater(parts["evening (17-22)"]["avg_reach"], parts["morning (5-12)"]["avg_reach"])
        self.assertEqual(result["best"][0]["reach"], 9000)
        self.assertEqual(result["worst"][-1]["reach"], 500)

    def test_hashtags_need_three_posts_to_count(self):
        result = instagram.analyse(self.posts, TODAY)
        tags = {h["tag"]: h for h in result["hashtags"]}
        self.assertIn("#lore", tags)                       # on 3 posts
        self.assertNotIn("#nature", tags)                  # on 1 post: not enough to say anything
        self.assertGreater(tags["#lore"]["vs_average"], 1)
        buckets = {r["group"]: r["posts"] for r in result["by_hashtag_count"]}
        self.assertEqual(buckets["1-5 hashtags"], 4)

    def test_text_flags_thin_evidence(self):
        text = instagram.format_analysis(instagram.analyse(self.posts, TODAY))
        self.assertIn("weak evidence", text)
        self.assertIn("HASHTAGS used on 3+ posts", text)
        self.assertIn("Based on 6 posts", text)

    def test_nothing_to_analyse(self):
        self.assertIn("Not enough", instagram.format_analysis(instagram.analyse([], TODAY)))

    def test_busiest_hours(self):
        self.assertTrue(instagram.busiest_hours({"7": 10, "20": 90, "21": 80, "3": 5}).startswith("20:00 (90"))

    def test_hashtag_parsing(self):
        self.assertEqual(instagram.hashtags("Hi #Lore and #history_x!"), ["lore", "history_x"])
        self.assertEqual(instagram.hashtags(None), [])


class FakeResponse:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class FakeHttp:
    """Plays Instagram's API, including the quirk that some metrics are refused for some media."""

    def __init__(self):
        self.urls = []

    def get(self, url, params=None, timeout=None):
        self.urls.append((url, dict(params or {})))
        if url.endswith("/me"):
            return FakeResponse({"user_id": "17", "username": "thewildlore", "followers_count": 4200, "follows_count": 10, "media_count": 2})
        if url.endswith("/me/media"):
            return FakeResponse({"data": [
                {"id": "m1", "caption": "Lore #lore", "media_type": "VIDEO", "media_product_type": "REELS",
                 "timestamp": "2026-10-01T18:00:00+0000", "like_count": 100, "comments_count": 5},
                {"id": "m2", "caption": "Photo", "media_type": "IMAGE", "media_product_type": "FEED",
                 "timestamp": "2026-10-05T07:00:00+0000", "like_count": 10, "comments_count": 0}]})
        if url.endswith("/m1/insights"):
            return FakeResponse({"data": [{"name": "reach", "values": [{"value": 5000}]}, {"name": "saved", "values": [{"value": 70}]},
                                          {"name": "shares", "values": [{"value": 40}]}, {"name": "total_interactions", "values": [{"value": 600}]},
                                          {"name": "ig_reels_avg_watch_time", "values": [{"value": 6500}]}]})
        if url.endswith("/m2/insights"):
            if "views" in params["metric"]:                      # refused: falls back to the core metrics
                return FakeResponse({"error": {"message": "metric views not supported for this media", "code": 100}}, 400)
            return FakeResponse({"data": [{"name": "reach", "values": [{"value": 400}]}, {"name": "total_interactions", "values": [{"value": 20}]}]})
        if url.endswith("/me/insights"):
            return FakeResponse({"data": [{"name": "online_followers", "values": [{"value": {"19": 300, "20": 500, "21": 450, "4": 20}}]}]})
        return FakeResponse({"error": {"message": "nope", "code": 100}}, 400)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.http = FakeHttp()
        self.reg = Registry()
        instagram.register(self.reg, Path(self.dir.name), instagram.Instagram(Path(self.dir.name), token="TOKEN", http=self.http))

    def run_tool(self, name, **args):
        return self.reg.execute(name, args, lambda s: True).text

    def test_account(self):
        text = self.run_tool("instagram_account")
        self.assertIn("@thewildlore", text)
        self.assertIn("4,200 followers", text)
        self.assertIn("20:00 (500 followers online)", text)

    def test_posts_combine_listing_and_insights_and_survive_refused_metrics(self):
        text = self.run_tool("instagram_posts")
        self.assertIn("Reel | reach 5,000", text)
        self.assertIn("Photo | reach 400", text)                  # got there by retrying with fewer metrics
        self.assertTrue(all(p["access_token"] == "TOKEN" for _, p in self.http.urls))

    def test_reel_watch_time_is_converted_to_seconds(self):
        posts = instagram.Instagram(Path(self.dir.name), token="T", http=self.http).posts(5)
        self.assertEqual(posts[0]["watch_s"], 6.5)
        self.assertEqual(posts[0]["type"], "Reel")
        self.assertEqual(posts[1]["type"], "Photo")

    def test_insights_end_to_end(self):
        text = self.run_tool("instagram_insights")
        self.assertIn("CONTENT TYPE", text)
        self.assertIn("FOLLOWERS ONLINE: 20:00", text)

    def test_expired_token_gives_a_clear_message(self):
        http = mock.Mock()
        http.get.return_value = FakeResponse({"error": {"message": "Error validating access token", "code": 190}}, 400)
        client = instagram.Instagram(Path(self.dir.name), token="old", http=http)
        with self.assertRaises(instagram.InstagramError) as ctx:
            client.profile()
        self.assertIn("expired", str(ctx.exception))

    def test_tools_are_read_only(self):
        names = {t.name for t in self.reg.tools()}
        self.assertTrue({"instagram_account", "instagram_posts", "instagram_insights"} <= names)
        self.assertFalse([n for n in names if n.startswith("instagram_") and any(w in n for w in ("publish", "post_", "comment", "delete"))])

    def test_setup_is_offered_until_a_token_exists(self):
        with mock.patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": ""}):
            reg = Registry()
            instagram.register(reg, Path(self.dir.name))
            names = {t.name for t in reg.tools()}
            self.assertIn("instagram_setup", names)
            self.assertNotIn("instagram_insights", names)
        with mock.patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "abc"}):
            names = {t.name for t in reg.tools()}
            self.assertIn("instagram_insights", names)
            self.assertNotIn("instagram_setup", names)

    def test_token_from_env_is_remembered_and_renewed_when_old(self):
        with mock.patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "ENVTOKEN"}):
            http = mock.Mock()
            http.get.return_value = FakeResponse({"access_token": "RENEWED"})
            client = instagram.Instagram(Path(self.dir.name), http=http)
            self.assertEqual(client.token(), "ENVTOKEN")
            saved = json.loads((Path(self.dir.name) / "instagram_token.json").read_text(encoding="utf-8"))
            saved["renewed"] -= 50 * 86400                        # 50 days old
            (Path(self.dir.name) / "instagram_token.json").write_text(json.dumps(saved), encoding="utf-8")
            later = instagram.Instagram(Path(self.dir.name), http=http)
            self.assertEqual(later.token(), "RENEWED")


if __name__ == "__main__":
    unittest.main()
