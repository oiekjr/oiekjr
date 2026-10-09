"""Test public activity collection, SVG rendering, and update failures."""

from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from unittest.mock import Mock, patch

from scripts import update_stats
from scripts.update_stats import Stats, fetch_stats, render_count, render_profile, request_graphql, validate_count

NOW = datetime(2026, 10, 9, 7, 30, tzinfo=timezone.utc)


def response(contributed_repositories: int = 12) -> dict[str, object]:
    """Build a GraphQL response fixture for public activity.

    Args:
        contributed_repositories: Number of repositories with commit contributions.

    Returns:
        A response with the same structure as the GraphQL API.
    """
    return {
        "data": {"user": {
            "contributionsCollection": {
                "totalRepositoriesWithContributedCommits": contributed_repositories,
                "totalCommitContributions": 1234,
                "totalPullRequestContributions": 56,
                "totalIssueContributions": 7,
            },
        }},
    }


class StatsTests(unittest.TestCase):
    """Test observable behavior from the API boundary to generated images."""

    def test_contributed_repository_count_is_distinct_from_commits(self) -> None:
        """Fetch commit and contributed repository counts separately."""
        request = Mock(return_value=response())

        stats = fetch_stats("oiekjr", NOW, request)

        self.assertEqual(stats, Stats(2026, 12, 1234, 56, 7))
        self.assertEqual(request.call_count, 1)

    def test_calendar_year_is_explicit(self) -> None:
        """Switch reporting years at the calendar year boundary."""
        for now, expected in [
            (datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc), "2026-01-01T00:00:00Z"),
            (datetime(2027, 1, 1, tzinfo=timezone.utc), "2027-01-01T00:00:00Z"),
            (datetime(2027, 1, 1, 0, 0, 1, tzinfo=timezone.utc), "2027-01-01T00:00:00Z"),
        ]:
            with self.subTest(now=now):
                request = Mock(return_value=response())

                fetch_stats("oiekjr", now, request)

                self.assertEqual(request.call_args.args[0]["from"], expected)
                self.assertEqual(request.call_args.args[0]["to"], now.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_no_repositories_is_a_valid_zero(self) -> None:
        """Accept zero contributed repositories as a valid count."""
        request = Mock(return_value=response(0))

        stats = fetch_stats("oiekjr", NOW, request)

        self.assertEqual(stats.contributed_repositories, 0)
        self.assertEqual(request.call_count, 1)

    def test_partial_api_error_is_not_saved_as_success(self) -> None:
        """Reject responses containing errors even when partial data is present."""
        payload = response()
        payload["errors"] = [{"message": "failed"}]
        request = Mock(return_value=payload)

        with self.assertRaises(ValueError):
            fetch_stats("oiekjr", NOW, request)

    def test_missing_user_fails(self) -> None:
        """Reject a missing user rather than displaying zero activity."""
        request = Mock(return_value={"data": {"user": None}})

        with self.assertRaises(ValueError):
            fetch_stats("missing", NOW, request)

    def test_counts_are_nonnegative_integers(self) -> None:
        """Validate counts around zero and reject invalid types."""
        self.assertEqual(validate_count(0), 0)
        self.assertEqual(validate_count(1), 1)
        for invalid in [-1, True, None, "1", 1.5]:
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_count(invalid)

    def test_rendered_svg_exposes_stats_and_period(self) -> None:
        """Render the same counts and reporting year in every display variant."""
        template = Template((update_stats.ROOT / "scripts/profile.svg.template").read_text())

        for colors in update_stats.THEMES.values():
            for compact in (False, True):
                with self.subTest(colors=colors, compact=compact):
                    svg = render_profile(template, Stats(2026, 12, 1234, 56, 7), "Updated 2026-10-09 UTC", colors, "oiekjr", compact=compact)
                    root = ET.fromstring(svg)

                    text = " ".join(root.itertext())
                    self.assertIn("1,234", text)
                    self.assertIn("Public activity in 2026 — Repositories with commit contributions: 12. Commits: 1,234. Pull requests: 56. Issues: 7.", text)
                    self.assertIn("Updated 2026-10-09 UTC", text)

    def test_successful_update_saves_every_display_variant(self) -> None:
        """Save both themes and layouts from a single stats fetch."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            source = update_stats.ROOT / "scripts/profile.svg.template"
            (root / "scripts/profile.svg.template").write_text(source.read_text())

            with patch.object(update_stats, "ROOT", root), patch.dict("os.environ", {"GH_TOKEN": "test"}), \
                    patch("sys.argv", ["update_stats.py", "--username", "oiekjr"]), \
                    patch.object(update_stats, "fetch_stats", return_value=Stats(2026, 12, 1234, 56, 7)) as fetch:
                result = update_stats.main()

            self.assertEqual(result, 0)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual({path.name for path in (root / "assets").iterdir()}, {
                "profile.svg", "profile-dark.svg", "profile-mobile.svg", "profile-dark-mobile.svg",
            })
            for path in (root / "assets").iterdir():
                svg = ET.fromstring(path.read_text())
                self.assertIn("Commits: 1,234.", " ".join(svg.itertext()))

    def test_placeholder_does_not_invent_counts(self) -> None:
        """Distinguish unavailable counts from zero before the first update."""
        template = Template((update_stats.ROOT / "scripts/profile.svg.template").read_text())

        root = ET.fromstring(render_profile(template, Stats(2026), "Awaiting first update", update_stats.THEMES["profile.svg"], "oiekjr"))

        text = " ".join(root.itertext())
        self.assertIn("Awaiting first update", text)
        self.assertIn("Public activity in 2026 — Repositories with commit contributions: —. Commits: —.", text)

    def test_stars_are_not_displayed(self) -> None:
        """Exclude star counts from both themes."""
        template = Template((update_stats.ROOT / "scripts/profile.svg.template").read_text())

        for colors in update_stats.THEMES.values():
            with self.subTest(colors=colors):
                svg = render_profile(template, Stats(2026, 12, 1234, 56, 7), "Updated 2026-10-09 UTC", colors, "oiekjr")

                self.assertNotIn("Stars", svg)

    def test_rendered_text_is_escaped(self) -> None:
        """Escape XML characters without corrupting displayed text."""
        template = Template("<svg><text>$updated</text></svg>")

        root = ET.fromstring(render_profile(template, Stats(2026), "<pending> & retry", {}, "oiekjr"))

        self.assertEqual(root.findtext("text"), "<pending> & retry")

    def test_count_up_increases_without_overshooting(self) -> None:
        """Increase from zero without overshooting and retain the exact final count."""
        for value in (1, 12, 1234):
            with self.subTest(value=value):
                root = ET.fromstring(f"<svg>{render_count(value)}</svg>")

                frames = [int(text.text.replace(",", "")) for text in root.findall('.//text[@class="count-frame"]')]
                self.assertEqual(frames[0], 0)
                self.assertEqual(frames, sorted(set(frames)))
                self.assertTrue(all(number < value for number in frames))
                self.assertEqual(root.find('.//text[@class="count-final"]').text, f"{value:,}")

    def test_count_up_finishes_once_without_gaps_or_overlaps(self) -> None:
        """Display each step once without gaps and finish after 1.8 seconds."""
        root = ET.fromstring(f"<svg>{render_count(1234)}</svg>")

        intervals = [
            (int(animation.attrib["begin"].removesuffix("ms")), int(animation.attrib["dur"].removesuffix("ms")))
            for animation in root.findall('.//text[@class="count-frame"]/set')
        ]
        self.assertEqual(intervals[0][0], 0)
        self.assertEqual(sum(intervals[-1]), 1800)
        for current, following in zip(intervals, intervals[1:]):
            self.assertEqual(sum(current), following[0])
        self.assertTrue(all(duration > 0 for _, duration in intervals))
        for animation in root.iter("set"):
            self.assertNotIn("repeatCount", animation.attrib)
            self.assertNotEqual(animation.attrib.get("fill"), "freeze")
        final_animation = root.find('.//text[@class="count-final"]/set')
        self.assertEqual(final_animation.attrib, {"attributeName": "opacity", "to": "0", "begin": "0ms", "dur": "1800ms"})

    def test_count_up_output_is_bounded_for_large_counts(self) -> None:
        """Keep display steps and output size bounded for large counts."""
        markup = render_count(1_000_000_000_000)

        root = ET.fromstring(f"<svg>{markup}</svg>")
        self.assertLessEqual(len(root.findall('.//text[@class="count-frame"]')), 32)
        self.assertLess(len(markup), 10_000)

    def test_count_up_keeps_the_final_value_without_animation(self) -> None:
        """Retain the final value for reduced-motion and static rendering."""
        root = ET.fromstring(f"<svg>{render_count(1234)}</svg>")

        static = root.find('.//g[@class="count-static"]/text')
        self.assertEqual(static.text, "1,234")
        self.assertEqual(len(static), 0)
        self.assertNotIn("opacity", root.find('.//text[@class="count-final"]').attrib)
        self.assertTrue(all(text.attrib["opacity"] == "0" for text in root.findall('.//text[@class="count-frame"]')))

    def test_unavailable_and_zero_counts_do_not_count_up(self) -> None:
        """Render unavailable and zero counts without count-up animation."""
        for value, expected in ((None, "—"), (0, "0")):
            with self.subTest(value=value):
                root = ET.fromstring(f"<svg>{render_count(value)}</svg>")

                self.assertEqual(root.findtext("text"), expected)
                self.assertEqual(len(list(root.iter("set"))), 0)

    def test_failed_update_preserves_previous_images(self) -> None:
        """Preserve every previous image when the API request fails."""
        image_names = ("profile.svg", "profile-dark.svg", "profile-mobile.svg", "profile-dark-mobile.svg")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            for name in image_names:
                (root / "assets" / name).write_text("previous image")

            with patch.object(update_stats, "ROOT", root), patch.dict("os.environ", {"GH_TOKEN": "test"}), \
                    patch("sys.argv", ["update_stats.py", "--username", "oiekjr"]), \
                    patch.object(update_stats, "request_graphql", side_effect=OSError("unavailable")):
                result = update_stats.main()

            self.assertEqual(result, 1)
            for name in image_names:
                self.assertEqual((root / "assets" / name).read_text(), "previous image")

    def test_response_size_is_bounded(self) -> None:
        """Validate responses immediately below, at, and above the size limit."""
        for size in [31, 32, 33]:
            with self.subTest(size=size):
                body = b'{"data":{}}' + b" " * (size - 11)
                transport = Mock()
                transport.__enter__ = Mock(return_value=transport)
                transport.__exit__ = Mock(return_value=False)
                transport.read.return_value = body

                with patch.object(update_stats, "MAX_RESPONSE_BYTES", 32), \
                        patch.object(update_stats, "urlopen", return_value=transport):
                    if size > 32:
                        with self.assertRaises(ValueError):
                            request_graphql("test", {})
                    else:
                        self.assertEqual(request_graphql("test", {}), {"data": {}})


if __name__ == "__main__":
    unittest.main()
