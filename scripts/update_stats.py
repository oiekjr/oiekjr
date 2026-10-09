"""Fetch public GitHub activity and generate profile SVGs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from string import Template
from typing import cast
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
MAX_RESPONSE_BYTES = 2_000_000
COUNT_STEPS = 32
COUNT_DURATION_MS = 1800
COUNT_INITIAL_HOLD_MS = 180
QUERY = """
query Profile($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalRepositoriesWithContributedCommits
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
    }
  }
}
"""
THEMES = {
    # Use Primer's official colors to match GitHub's light theme.
    # https://primer.style/product/primitives/color/
    "profile.svg": {
        "background": "#ffffff", "text": "#1f2328", "muted": "#59636e", "line": "#d1d9e0",
        "surface": "#f6f8fa", "inverse_muted": "#d1d9e0", "inverse_line": "#59636e",
    },
    "profile-dark.svg": {
        "background": "#101317", "text": "#f1f2ee", "muted": "#a4a9b0", "line": "#34393e",
        "surface": "#1a1f25", "inverse_muted": "#535b63", "inverse_line": "#cdd0cb",
    },
}


@dataclass(frozen=True)
class Stats:
    """Store public activity counts and their calendar year."""

    year: int
    contributed_repositories: int | None = None
    commits: int | None = None
    pull_requests: int | None = None
    issues: int | None = None


def main() -> int:
    """Parse arguments and update the profile images.

    Returns:
        Zero on success, or one if fetching or rendering fails.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--placeholder", action="store_true", help="Generate placeholder images without calling the API")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)

    try:
        if args.placeholder:
            stats = Stats(year=now.year)
            updated = "Awaiting first update"
        else:
            token = os.environ.get("GH_TOKEN")
            if not token:
                raise ValueError("GH_TOKEN is required")
            stats = fetch_stats(args.username, now, lambda variables: request_graphql(token, variables))
            updated = f"Updated {now:%Y-%m-%d} UTC"

        template = Template((ROOT / "scripts/profile.svg.template").read_text(encoding="utf-8"))
        images = {
            name.replace(".svg", "-mobile.svg") if compact else name:
                render_profile(template, stats, updated, colors, args.username, compact=compact)
            for name, colors in THEMES.items()
            for compact in (False, True)
        }
        # Preserve previous images until fetching and all rendering succeed.
        (ROOT / "assets").mkdir(exist_ok=True)
        for name, svg in images.items():
            (ROOT / "assets" / name).write_text(svg, encoding="utf-8")
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Profile update failed: {type(error).__name__}", file=sys.stderr)
        return 1

    print("Profile SVGs generated.")
    return 0


def fetch_stats(username: str, now: datetime, request: Callable[[dict[str, object]], dict[str, object]]) -> Stats:
    """Fetch public activity and repositories with commit contributions this year.

    Args:
        username: GitHub login to query.
        now: End of the reporting period in UTC.
        request: Callable that sends GraphQL variables and returns a response.

    Returns:
        Activity counts for the specified calendar year.

    Raises:
        ValueError: The API reports an error, the user is missing, or a count is invalid.
    """
    variables: dict[str, object] = {
        "login": username,
        "from": f"{now.year}-01-01T00:00:00Z",
        "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    payload = request(variables)
    if payload.get("errors"):
        raise ValueError("GitHub API returned errors")
    data = cast(dict[str, object], payload["data"])
    user = cast(dict[str, object], data["user"])
    if not user:
        raise ValueError("GitHub user not found")
    contributions = cast(dict[str, object], user["contributionsCollection"])

    return Stats(
        year=now.year,
        contributed_repositories=validate_count(contributions["totalRepositoriesWithContributedCommits"]),
        commits=validate_count(contributions["totalCommitContributions"]),
        pull_requests=validate_count(contributions["totalPullRequestContributions"]),
        issues=validate_count(contributions["totalIssueContributions"]),
    )


def request_graphql(token: str, variables: dict[str, object]) -> dict[str, object]:
    """Query the GitHub API with timeout and response size limits.

    Args:
        token: GitHub access token.
        variables: Variables to pass to the GraphQL query.

    Returns:
        The decoded JSON response.

    Raises:
        ValueError: The response exceeds the size limit or is not a JSON object.
        URLError: The request fails.
    """
    request = Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": variables}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "profile-stats"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("API response exceeds the size limit")
    payload: object = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("API response must be a JSON object")
    return cast(dict[str, object], payload)


def validate_count(value: object) -> int:
    """Validate that an activity count is a nonnegative integer.

    Args:
        value: Activity count returned by the API.

    Returns:
        The validated count.

    Raises:
        ValueError: The value is negative, boolean, or not an integer.
    """
    if type(value) is not int or value < 0:
        raise ValueError("Activity count must be a nonnegative integer")
    return value


def render_profile(
    template: Template, stats: Stats, updated: str, colors: dict[str, str], username: str, *, compact: bool = False,
) -> str:
    """Render a profile SVG with the supplied identity, stats, and theme.

    Args:
        template: Profile SVG template.
        stats: Activity counts and reporting year.
        updated: Update date or placeholder status.
        colors: Color palette for the selected theme.
        username: GitHub login used in the image title.
        compact: Whether to use the layout for narrow screens.

    Returns:
        The rendered SVG image.
    """
    language_keys = ("japanese", "html", "css", "javascript", "typescript", "php", "node", "python", "sql")
    language_positions = (
        [(0, 137), (0, 193), (0, 249), (0, 305), (186, 305), (0, 361), (186, 361), (0, 417), (186, 417)]
        if compact else [(index % 3 * 186, 137 + index // 3 * 56) for index in range(9)]
    )
    counts = (stats.contributed_repositories, stats.commits, stats.pull_requests, stats.issues)
    count_width = max(len("—" if value is None else f"{value:,}") for value in counts)
    cell_width = 180 if compact else 210

    return template.substitute(
        colors,
        username=escape(username),
        year=stats.year,
        updated=escape(updated),
        width=360 if compact else 840,
        height=1040 if compact else 712,
        mark_transform="translate(294 220) scale(0.55)" if compact else "translate(702 220)",
        chip_width=173 if compact else 167,
        **{f"{key}_position": f"translate({x} {y})" for key, (x, y) in zip(language_keys, language_positions)},
        ai_heading_y=508 if compact else 340,
        ai_rule_y=524 if compact else 356,
        tool_y=548 if compact else 380,
        tool_width=173 if compact else 407,
        tool_height=103 if compact else 87,
        tool_second_x=186 if compact else 432,
        tool_icon_position="translate(71 18)" if compact else "translate(22 28)",
        tool_text_x=87 if compact else 74,
        tool_text_y=80 if compact else 51,
        tool_text_anchor="middle" if compact else "start",
        activity_heading_y=699 if compact else 515,
        activity_rule_y=714 if compact else 530,
        activity_y=738 if compact else 554,
        activity_height=300 if compact else 156,
        activity_dividers="M180 20V280 M22 150H338" if compact else "M210 20V136 M420 20V136 M630 20V136",
        repositories_position="translate(0 0)",
        commits_position=f"translate({cell_width} 0)",
        pull_requests_position="translate(0 150)" if compact else "translate(420 0)",
        issues_position="translate(180 150)" if compact else "translate(630 0)",
        number_size=min(42, (cell_width - 44) * 100 // (count_width * 62)),
        repositories_count=render_count(stats.contributed_repositories),
        commits_count=render_count(stats.commits),
        pull_requests_count=render_count(stats.pull_requests),
        issues_count=render_count(stats.issues),
        contributed_repositories="—" if stats.contributed_repositories is None else f"{stats.contributed_repositories:,}",
        commits="—" if stats.commits is None else f"{stats.commits:,}",
        pull_requests="—" if stats.pull_requests is None else f"{stats.pull_requests:,}",
        issues="—" if stats.issues is None else f"{stats.issues:,}",
    )


def render_count(value: int | None) -> str:
    """Animate a count with bounded display steps and retain its final value.

    Args:
        value: Activity count, or None if it has not been fetched.

    Returns:
        SVG markup with the final value as the reduced-motion and static fallback.

    Raises:
        ValueError: The count is negative, boolean, or not an integer.
    """
    if value is None:
        return "<text>—</text>"
    validate_count(value)
    if value == 0:
        return "<text>0</text>"

    # Avoid allocating one animation frame per increment for large counts.
    frames: list[tuple[int, int]] = []
    for index in range(COUNT_STEPS):
        intermediate = value * (COUNT_STEPS ** 3 - (COUNT_STEPS - index) ** 3) // COUNT_STEPS ** 3
        if not frames or frames[-1][0] != intermediate:
            # Hold the initial zero briefly so it is visible after the image loads.
            start_ms = 0 if index == 0 else COUNT_INITIAL_HOLD_MS + (COUNT_DURATION_MS - COUNT_INITIAL_HOLD_MS) * index // COUNT_STEPS
            frames.append((intermediate, start_ms))

    markup = [
        f'<g class="count-static"><text>{value:,}</text></g>',
        '<g class="count-motion">',
        f'<text class="count-final">{value:,}<set attributeName="opacity" to="0" begin="0ms" dur="{COUNT_DURATION_MS}ms"/></text>',
    ]
    for index, (intermediate, start_ms) in enumerate(frames):
        end_ms = frames[index + 1][1] if index + 1 < len(frames) else COUNT_DURATION_MS
        markup.append(
            f'<text class="count-frame" opacity="0">{intermediate:,}'
            f'<set attributeName="opacity" to="1" begin="{start_ms}ms" dur="{end_ms - start_ms}ms"/></text>'
        )
    markup.append('</g>')
    return "".join(markup)


if __name__ == "__main__":
    sys.exit(main())
