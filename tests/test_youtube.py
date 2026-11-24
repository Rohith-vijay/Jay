import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jarvis.tools import youtube
from jarvis.tools.registry import Registry

TODAY = date(2026, 11, 19)


def video(days_ago, hour, seconds, views, title="A video", pct=40.0, likes=10, subs=2):
    local = datetime.combine(TODAY - timedelta(days=days_ago), datetime.min.time()).replace(hour=hour).astimezone()
    return {"id": f"v{days_ago}", "title": title, "published": local.astimezone(timezone.utc), "duration": seconds,
            "views": views, "avg_pct": pct, "likes": likes, "subs": subs, "comments": 1}


class ParsingTests(unittest.TestCase):
    def test_iso_durations(self):
        self.assertEqual(youtube.iso_duration("PT12M30S"), 750)
        self.assertEqual(youtube.iso_duration("PT1H2M3S"), 3723)
        self.assertEqual(youtube.iso_duration("PT45S"), 45)
        self.assertEqual(youtube.iso_duration("junk"), 0)

    def test_report_rows_become_dicts(self):
        resp = {"columnHeaders": [{"name": "video"}, {"name": "views"}], "rows": [["a", 5], ["b", 7]]}
        self.assertEqual(youtube.rows_as_dicts(resp), [{"video": "a", "views": 5}, {"video": "b", "views": 7}])
        self.assertEqual(youtube.rows_as_dicts({}), [])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        # Evening, 12-minute videos do best; mornings and Shorts do worse.
        self.videos = [
            video(40, 19, 720, 8000, "Top 5 secrets?"), video(33, 19, 700, 7000, "10 facts"),
            video(26, 19, 710, 6500, "Why it happened?"), video(19, 8, 55, 700, "Short one"),
            video(12, 8, 50, 600, "Short two"), video(8, 8, 58, 500, "Short three"),
            video(1, 19, 720, 900, "Brand new video"),                        # too new to judge
        ]

    def test_new_videos_are_left_out_and_counted(self):
        result = youtube.analyse(self.videos, TODAY)
        self.assertEqual(result["considered"], 6)
        self.assertEqual(result["skipped_too_new"], 1)

    def test_it_finds_the_evening_and_the_length_that_work(self):
        result = youtube.analyse(self.videos, TODAY)
        parts = {r["group"]: r for r in result["by_daypart"]}
        self.assertGreater(parts["evening (17-22)"]["views_per_day"], parts["morning (5-12)"]["views_per_day"] * 3)
        lengths = {r["group"]: r for r in result["by_length"]}
        self.assertGreater(lengths["10-20 min"]["views_per_day"], lengths["under 1 min (Shorts)"]["views_per_day"])
        self.assertEqual(result["best"][0]["title"], "Why it happened?")      # 250/day beats 200/day: newer, so judged per day
        self.assertEqual(result["worst"][-1]["title"], "Short one")      # 37/day, the lowest per day

    def test_views_are_compared_per_day_not_in_total(self):
        old = video(300, 12, 600, 9000, "Old")          # 30/day
        recent = video(10, 12, 600, 3000, "Recent")      # 300/day
        result = youtube.analyse([old, recent, video(100, 12, 600, 5000)], TODAY)
        self.assertEqual(result["best"][0]["title"], "Recent")

    def test_the_text_warns_about_thin_evidence(self):
        text = youtube.format_analysis(youtube.analyse(self.videos, TODAY))
        self.assertIn("weak evidence", text)                        # groups with one or two videos
        self.assertIn("VIDEO LENGTH", text)
        self.assertIn("Based on 6 videos", text)

    def test_nothing_to_analyse(self):
        self.assertIn("Not enough", youtube.format_analysis(youtube.analyse([], TODAY)))
        self.assertIn("Not enough", youtube.format_analysis(youtube.analyse([video(1, 9, 600, 5)], TODAY)))

    def test_retention_finds_the_biggest_drop(self):
        points = [(0.0, 1.0), (0.1, 0.9), (0.2, 0.45), (0.5, 0.40), (0.9, 0.30), (1.0, 0.2)]
        text = youtube.retention_summary(points)
        self.assertIn("20%", text)
        self.assertIn("lost 45 points", text)
        self.assertIn("No retention data", youtube.retention_summary([]))


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class FakeData:
    def channels(self):
        return self

    def playlistItems(self):
        return self

    def videos(self):
        return self

    def list(self, **kw):
        part = kw.get("part", "")
        if "contentDetails" in part and "mine" in kw:
            return FakeRequest({"items": [{"snippet": {"title": "The Wild Lore"},
                                           "statistics": {"subscriberCount": "1200", "viewCount": "88000", "videoCount": "30"},
                                           "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if "playlistId" in kw:
            return FakeRequest({"items": [{"contentDetails": {"videoId": "abc"}}]})
        return FakeRequest({"items": [{"id": "abc", "snippet": {"title": "Top 5 myths", "publishedAt": "2026-10-01T12:00:00Z"},
                                       "contentDetails": {"duration": "PT10M5S"},
                                       "statistics": {"viewCount": "5000", "likeCount": "300", "commentCount": "20"}}]})


class FakeAnalytics:
    def reports(self):
        return self

    def query(self, **kw):
        if kw.get("dimensions") == "video":
            return FakeRequest({"columnHeaders": [{"name": n} for n in ("video", "averageViewPercentage", "subscribersGained", "averageViewDuration")],
                                "rows": [["abc", 52.5, 14, 300]]})
        if kw.get("dimensions") == "elapsedVideoTimeRatio":
            return FakeRequest({"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}],
                                "rows": [[0.0, 1.0], [0.5, 0.5], [1.0, 0.3]]})
        if kw.get("dimensions") == "insightTrafficSourceType":
            return FakeRequest({"columnHeaders": [{"name": "insightTrafficSourceType"}, {"name": "views"}],
                                "rows": [["YT_SEARCH", 60], ["SUGGESTED", 40]]})
        return FakeRequest({"columnHeaders": [{"name": n} for n in ("views", "estimatedMinutesWatched", "averageViewDuration", "subscribersGained", "subscribersLost", "likes", "comments", "shares")],
                            "rows": [[9000, 30000, 210, 120, 20, 800, 50, 90]]})


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.reg = Registry()
        client = youtube.YouTube(Path(self.dir.name), data_service=FakeData(), analytics_service=FakeAnalytics())
        youtube.register(self.reg, Path(self.dir.name), client)

    def run_tool(self, name, **args):
        return self.reg.execute(name, args, lambda s: True).text

    def test_channel_overview(self):
        text = self.run_tool("youtube_channel")
        self.assertIn("The Wild Lore", text)
        self.assertIn("1,200 subscribers", text)
        self.assertIn("net subscribers +100", text)
        self.assertIn("Yt Search 60%", text)

    def test_video_list_joins_public_and_analytics_numbers(self):
        text = self.run_tool("youtube_videos")
        self.assertIn("Top 5 myths", text)
        self.assertIn("52% watched", text)
        self.assertIn("+14 subs", text)

    def test_retention_report(self):
        text = self.run_tool("youtube_video_report", video_id="abc")
        self.assertIn("Top 5 myths", text)
        self.assertIn("50%: 50%", text)

    def test_insights_runs_end_to_end(self):
        self.assertIn("Based on 1 videos", self.run_tool("youtube_insights"))

    def test_tools_are_read_only(self):
        names = {t.name for t in self.reg.tools()}
        self.assertTrue({"youtube_channel", "youtube_videos", "youtube_insights", "youtube_video_report"} <= names)
        self.assertFalse([n for n in names if n.startswith("youtube_") and any(w in n for w in ("upload", "post", "delete", "update"))])

    def test_setup_help_is_offered_until_google_is_configured(self):
        with tempfile.TemporaryDirectory() as d:
            reg = Registry()
            youtube.register(reg, Path(d))
            names = {t.name for t in reg.tools()}
            self.assertIn("youtube_setup", names)
            self.assertNotIn("youtube_insights", names)
            (Path(d) / "google_credentials.json").write_text("{}", encoding="utf-8")
            names = {t.name for t in reg.tools()}
            self.assertIn("youtube_insights", names)
            self.assertNotIn("youtube_setup", names)


if __name__ == "__main__":
    unittest.main()
