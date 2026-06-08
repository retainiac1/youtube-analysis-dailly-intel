import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

import db
from config import (
    BLOCKED_CATEGORY_IDS,
    CHANNEL_BATCH_SIZE,
    CHANNELS_QUOTA_COST,
    COMMENTS_PER_VIDEO,
    COMMENTS_QUOTA_COST,
    COMMENTS_SHEET_COLUMNS,
    CSV_COLUMNS,
    DAILY_QUOTA_LIMIT,
    DB_PATH,
    MIN_VIEWS,
    OUTPUT_CSV,
    OUTPUT_XLSX_BASE,
    PUBLISHED_BEFORE,
    RESULTS_PER_QUERY,
    SAFETY_BUFFER,
    SEARCH_QUERIES,
    SEARCH_QUOTA_COST,
    SEARCH_RELEVANCE_LANGUAGE,
    SHORT_MAX_SECONDS,
    STATE_FILE,
    TOP_N,
    VALID_BUCKETS,
    VIDEOS_QUOTA_COST,
    ConfigError,
    get_published_after,
    now_local_iso,
    pacific_date,
    validate_config,
)

# Rough per-call-count heuristics for the discover quota estimate. They are
# intentionally approximate (real channel/comment counts are unknown until the
# pool is fetched); the per-call guard is the real protection. The only hard
# requirement is that --dry-run and the pre-flight share this one estimate.
ESTIMATED_CHANNEL_CALLS = 2
ESTIMATED_COMMENT_CALLS = 75

THUMBNAIL_PRIORITY = ["maxres", "standard", "high", "medium", "default"]


def build_youtube_client(api_key: str):
    return build("youtube", "v3", developerKey=api_key)


def search_videos(youtube, query: str) -> list[str]:
    params = {
        "q": query,
        "type": "video",
        "videoDuration": "short",
        # Rolling window (now - WINDOW_DAYS), the same horizon rankings filter on.
        "publishedAfter": get_published_after(),
        "order": "viewCount",
        "maxResults": RESULTS_PER_QUERY,
        "part": "snippet",
        "relevanceLanguage": SEARCH_RELEVANCE_LANGUAGE,
    }
    if PUBLISHED_BEFORE:
        params["publishedBefore"] = PUBLISHED_BEFORE

    response = youtube.search().list(**params).execute()
    return [item["id"]["videoId"] for item in response.get("items", [])]


def fetch_video_details(youtube, video_ids: list[str]) -> list[dict]:
    if not video_ids:
        return []
    response = youtube.videos().list(
        id=",".join(video_ids),
        part="snippet,statistics,contentDetails,status,topicDetails",
    ).execute()
    return response.get("items", [])


def parse_duration(iso_duration: str) -> int:
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def filter_videos(videos: list[dict], min_views: int, max_duration: int,
                  cutoff_dt: datetime) -> tuple[list[dict], dict[str, int]]:
    """Keep English, non-kids, non-blocked, in-window Shorts at/under max_duration
    with at least min_views. Returns (kept, drops); each excluded video is counted
    under the FIRST gate that rejects it, so
    sum(drops.values()) == len(videos) - len(kept).

    Defensive per the plan-doc: every API field is read with .get(), and a video
    is failed CLOSED when a field needed to confirm it qualifies is missing — a
    missing contentDetails.duration (is_short cannot be derived; do NOT assume
    Short) or a hidden/absent statistics.viewCount (cannot confirm min_views) is
    excluded and counted, never crashes the run, never passes unchecked. The
    window gate reuses within_window with the same cutoff ranking uses."""
    drops = {k: 0 for k in (
        "window", "views", "duration", "category", "language",
        "made_for_kids", "missing_duration", "missing_views",
    )}
    kept = []
    for video in videos:
        snippet = video.get("snippet", {})
        content = video.get("contentDetails", {})
        stats = video.get("statistics", {})
        status = video.get("status", {})

        if not within_window(snippet.get("publishedAt", ""), cutoff_dt):
            drops["window"] += 1
            continue
        if status.get("madeForKids", False):
            drops["made_for_kids"] += 1
            continue
        if snippet.get("categoryId", "") in BLOCKED_CATEGORY_IDS:
            drops["category"] += 1
            continue
        lang = snippet.get("defaultAudioLanguage", "")
        if lang and not lang.startswith("en"):
            drops["language"] += 1
            continue

        duration_str = content.get("duration")
        if not duration_str:                     # cannot derive is_short -> exclude
            drops["missing_duration"] += 1
            continue
        view_raw = stats.get("viewCount")
        if view_raw is None:                     # hidden/absent -> cannot confirm
            drops["missing_views"] += 1
            continue

        if parse_duration(duration_str) > max_duration:
            drops["duration"] += 1
            continue
        if int(view_raw) < min_views:
            drops["views"] += 1
            continue

        kept.append(video)
    return kept, drops


def views_below_min(videos: list[dict], min_views: int) -> tuple[int, int]:
    """Independent, non-exclusive diagnostic over the FULL batch (ignoring every
    other gate): how many videos have a KNOWN viewCount below min_views, and how
    many have a hidden/absent viewCount. Disentangles the views floor from the
    window gate, which first-match attribution in filter_videos would otherwise
    mask. Pure (no side effects), so the count never perturbs the drop buckets."""
    below = 0
    hidden = 0
    for video in videos:
        view_raw = video.get("statistics", {}).get("viewCount")
        if view_raw is None:
            hidden += 1
        elif int(view_raw) < min_views:
            below += 1
    return below, hidden


def best_thumbnail(thumbnails: dict) -> str:
    for key in THUMBNAIL_PRIORITY:
        if key in thumbnails:
            return thumbnails[key]["url"]
    return ""


def fetch_channel_details(youtube, channel_ids: list[str], budget: "QuotaBudget") -> dict[str, dict]:
    channel_map: dict[str, dict] = {}
    for i in range(0, len(channel_ids), CHANNEL_BATCH_SIZE):
        batch = channel_ids[i:i + CHANNEL_BATCH_SIZE]
        response = api_call_with_retry(
            lambda b=batch: youtube.channels().list(
                id=",".join(b),
                part="snippet,statistics,brandingSettings",
            ).execute(),
            budget,
            CHANNELS_QUOTA_COST,
        )
        if response is None:
            break
        for ch in response.get("items", []):
            ch_stats = ch.get("statistics", {})
            ch_snippet = ch.get("snippet", {})
            ch_branding = ch.get("brandingSettings", {}).get("channel", {})
            channel_map[ch["id"]] = {
                "subscriber_count": ch_stats.get("subscriberCount", "0"),
                "channel_video_count": ch_stats.get("videoCount", "0"),
                "channel_total_views": ch_stats.get("viewCount", "0"),
                "channel_created_date": ch_snippet.get("publishedAt", "")[:10],
                "channel_country": ch_branding.get("country", ch_snippet.get("country", "")),
                "channel_keywords": ch_branding.get("keywords", ""),
            }
    return channel_map


def fetch_top_comments(youtube, video_id: str, budget: "QuotaBudget") -> str:
    response = api_call_with_retry(
        lambda: youtube.commentThreads().list(
            part="snippet",
            videoId=video_id,
            maxResults=COMMENTS_PER_VIDEO,
            order="relevance",
            textFormat="plainText",
        ).execute(),
        budget,
        COMMENTS_QUOTA_COST,
    )
    if response is None:
        return ""

    comments = []
    for item in response.get("items", []):
        top = item["snippet"]["topLevelComment"]["snippet"]
        author = top.get("authorDisplayName", "Unknown")
        text = top.get("textDisplay", "").replace("|", "/").replace("\n", " ")[:200]
        likes = top.get("likeCount", 0)
        comments.append(f"{author}: {text} ({likes})")
    return "|".join(comments)


def format_video_row(video: dict, matched_queries: list[str],
                     channel_info: dict, top_comments_str: str) -> dict:
    snippet = video["snippet"]
    stats = video["statistics"]
    content = video["contentDetails"]
    status = video.get("status", {})
    topics = video.get("topicDetails", {})

    published = snippet["publishedAt"][:10]
    description = snippet.get("description", "")[:500]
    video_views = int(stats.get("viewCount", 0))
    sub_count = int(channel_info.get("subscriber_count", 0))

    raw_topics = topics.get("topicCategories", [])
    topic_names = [url.rstrip("/").split("/")[-1].replace("_", " ") for url in raw_topics]

    return {
        "social_media": "YouTube Shorts",
        "hook": "",
        "first_10_sec": "",
        "format": "Short",
        "date": published,
        "likes": int(stats.get("likeCount", 0)),
        "saves": "",
        "views": video_views,
        "thumbnail": best_thumbnail(snippet.get("thumbnails", {})),
        "link": f"https://youtube.com/shorts/{video['id']}",
        "title": snippet["title"],
        "description": description,
        "channel": snippet["channelTitle"],
        "channel_id": snippet["channelId"],
        "subscriber_count": sub_count,
        "channel_video_count": int(channel_info.get("channel_video_count", 0)),
        "channel_total_views": int(channel_info.get("channel_total_views", 0)),
        "channel_created_date": channel_info.get("channel_created_date", ""),
        "channel_country": channel_info.get("channel_country", ""),
        "channel_keywords": channel_info.get("channel_keywords", ""),
        "views_to_subs_ratio": round(video_views / max(sub_count, 1), 2),
        "category_id": snippet.get("categoryId", ""),
        "audio_language": snippet.get("defaultAudioLanguage", ""),
        "definition": content.get("definition", ""),
        "has_captions": content.get("caption", "false") == "true",
        "made_for_kids": status.get("madeForKids", False),
        "tags": "|".join(snippet.get("tags", [])),
        "topic_categories": "|".join(topic_names),
        "top_comments": top_comments_str,
        "duration_seconds": parse_duration(content["duration"]),
        "matched_queries": "|".join(matched_queries),
    }


def dedupe_videos(all_results: list[tuple[str, dict]]) -> tuple[dict[str, dict], dict[str, list[str]]]:
    seen: dict[str, dict] = {}
    query_map: dict[str, list[str]] = {}

    for query, video in all_results:
        vid = video["id"]
        if vid not in seen:
            seen[vid] = video
            query_map[vid] = [query]
        else:
            if query not in query_map[vid]:
                query_map[vid].append(query)

    return seen, query_map


def write_csv(rows: list[dict], output_path: str) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def write_xlsx(rows: list[dict], output_path: str, seen_videos: dict[str, dict],
               comments_data: dict[str, str]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Swipe File"

    hyperlink_font = Font(color="0563C1", underline="single")

    for col_idx, col_name in enumerate(CSV_COLUMNS, start=1):
        ws.cell(row=1, column=col_idx, value=col_name)

    thumbnail_col = CSV_COLUMNS.index("thumbnail") + 1
    link_col = CSV_COLUMNS.index("link") + 1
    url_columns = {thumbnail_col, link_col}

    for row_idx, row_data in enumerate(rows, start=2):
        for col_idx, col_name in enumerate(CSV_COLUMNS, start=1):
            value = row_data.get(col_name, "")
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            if col_idx in url_columns and value:
                cell.hyperlink = value
                cell.font = hyperlink_font

    ws.freeze_panes = "A2"

    last_col = get_column_letter(len(CSV_COLUMNS))
    table_ref = f"A1:{last_col}{len(rows) + 1}"
    table = Table(displayName="youtube_export", ref=table_ref)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium9",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    ws.add_table(table)

    # Top Comments sheet
    cs = wb.create_sheet("Top Comments")
    for col_idx, col_name in enumerate(COMMENTS_SHEET_COLUMNS, start=1):
        cs.cell(row=1, column=col_idx, value=col_name)

    comment_row = 2
    for vid, video in seen_videos.items():
        raw = comments_data.get(vid, "")
        if not raw:
            continue
        title = video["snippet"]["title"]
        for rank, entry in enumerate(raw.split("|"), start=1):
            paren_idx = entry.rfind(" (")
            if paren_idx != -1:
                author_text = entry[:paren_idx]
                likes_str = entry[paren_idx + 2:].rstrip(")")
                colon_idx = author_text.find(": ")
                if colon_idx != -1:
                    author = author_text[:colon_idx]
                    text = author_text[colon_idx + 2:]
                else:
                    author = ""
                    text = author_text
            else:
                author = ""
                text = entry
                likes_str = "0"

            cs.cell(row=comment_row, column=1, value=vid)
            cs.cell(row=comment_row, column=2, value=title)
            cs.cell(row=comment_row, column=3, value=rank)
            cs.cell(row=comment_row, column=4, value=author)
            cs.cell(row=comment_row, column=5, value=text)
            cs.cell(row=comment_row, column=6, value=int(likes_str) if likes_str.isdigit() else 0)
            comment_row += 1

    cs.freeze_panes = "A2"

    if comment_row > 2:
        cs_last_col = get_column_letter(len(COMMENTS_SHEET_COLUMNS))
        cs_ref = f"A1:{cs_last_col}{comment_row - 1}"
        cs_table = Table(displayName="top_comments", ref=cs_ref)
        cs_table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium9",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        cs.add_table(cs_table)

    wb.save(output_path)


def load_state() -> dict:
    path = Path(STATE_FILE)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def clear_state() -> None:
    path = Path(STATE_FILE)
    if path.exists():
        path.unlink()


class QuotaBudget:
    """Tracks quota across one run for proactive rate limiting.

    `baseline` is units already used today (Pacific) at run start, so the per-call
    guard sees total day spend, not just this run's. `run_units` is this run's
    spend; `flushed_units` is the slice of it already written to quota_ledger
    (search.list is flushed eagerly for crash durability), so the end-of-run write
    adds only the remainder. `guard_stopped` records that a call was refused
    pre-emptively to keep under the cap."""

    def __init__(self, baseline: int, cap: int):
        self.baseline = baseline
        self.cap = cap
        self.run_units = 0
        self.flushed_units = 0
        self.guard_stopped = False

    def can_afford(self, cost: int) -> bool:
        return self.baseline + self.run_units + cost <= self.cap

    def charge(self, cost: int) -> None:
        self.run_units += cost

    def remaining(self) -> int:
        return self.cap - self.baseline - self.run_units

    def unflushed(self) -> int:
        return self.run_units - self.flushed_units


def api_call_with_retry(call_fn, budget: QuotaBudget, cost: int):
    # Per-call guard: refuse proactively if charging this call would exceed the
    # effective cap. Returns None like the reactive quotaExceeded path, so the
    # search phase's existing None-handling aborts and persists work gathered.
    if not budget.can_afford(cost):
        budget.guard_stopped = True
        print(f"Quota guard: stopping before a {cost}-unit call "
              f"(would exceed cap of {budget.cap} units).", file=sys.stderr)
        return None

    for attempt in range(2):
        try:
            result = call_fn()
            budget.charge(cost)
            return result
        except HttpError as e:
            reason = ""
            if e.error_details:
                reason = e.error_details[0].get("reason", "")

            if reason == "keyInvalid" or e.resp.status == 400:
                print(f"ERROR: Invalid API key. {e}", file=sys.stderr)
                sys.exit(1)

            if reason == "quotaExceeded":
                print(f"ERROR: Quota exhausted after {budget.run_units} units. {e}", file=sys.stderr)
                return None

            if reason in ("commentsDisabled", "forbidden", "videoNotFound"):
                return None

            if e.resp.status == 429 and attempt == 0:
                print("Rate limited, retrying in 5s...", file=sys.stderr)
                time.sleep(5)
                continue

            print(f"ERROR: API error: {e}", file=sys.stderr)
            return None
        except (ConnectionError, TimeoutError) as e:
            if attempt == 0:
                print(f"Network error, retrying in 2s... ({e})", file=sys.stderr)
                time.sleep(2)
                continue
            print(f"ERROR: Network failure: {e}", file=sys.stderr)
            return None
    return None


def bucket_union(matched_queries: list[str], q_to_bucket: dict[str, str]) -> str:
    """Return the '|'-joined sorted set of buckets a video qualifies for, based on
    the queries it matched (e.g. 'habit', or 'habit|health' for a video that hit
    both a habit and a health query)."""
    buckets = {q_to_bucket[q] for q in matched_queries if q in q_to_bucket}
    return "|".join(sorted(buckets))


def build_video_record(video: dict, matched_queries: list[str], buckets: str,
                       channel_info: dict, top_comments: str) -> dict:
    """Map a YouTube videos.list item to the `videos` API-owned columns.

    Defensive per the build plan: missing view/like/comment counts become None
    (stored as-is, excluded from ratios), and all division is guarded so a video
    published today (0 days) or a hidden subscriber count cannot raise."""
    snippet = video.get("snippet", {})
    stats = video.get("statistics", {})
    content = video.get("contentDetails", {})
    status = video.get("status", {})
    topics = video.get("topicDetails", {})

    duration_seconds = parse_duration(content.get("duration", ""))
    raw_topics = topics.get("topicCategories", [])
    topic_names = [url.rstrip("/").split("/")[-1].replace("_", " ") for url in raw_topics]

    def _int_or_none(key: str):
        value = stats.get(key)
        return int(value) if value is not None else None

    view_count = _int_or_none("viewCount")
    like_count = _int_or_none("likeCount")
    comment_count = _int_or_none("commentCount")

    sub_count = int(channel_info.get("subscriber_count", 0) or 0)
    views_to_subs_ratio = (
        round(view_count / max(sub_count, 1), 2) if view_count is not None else None
    )

    views_per_day = None
    if view_count is not None:
        try:
            published_dt = datetime.fromisoformat(
                snippet.get("publishedAt", "").replace("Z", "+00:00")
            )
            days = max((datetime.now(timezone.utc) - published_dt).days, 1)
            views_per_day = round(view_count / days, 2)
        except (ValueError, TypeError):
            views_per_day = None

    return {
        "video_id": video["id"],
        "title": snippet.get("title", ""),
        "channel_id": snippet.get("channelId", ""),
        "channel_title": snippet.get("channelTitle", ""),
        "published_at": snippet.get("publishedAt", ""),
        "duration_seconds": duration_seconds,
        "is_short": 1 if duration_seconds <= SHORT_MAX_SECONDS else 0,
        "link": f"https://youtube.com/shorts/{video['id']}",
        "thumbnail_url": best_thumbnail(snippet.get("thumbnails", {})),
        "description": snippet.get("description", "")[:500],
        "category_id": snippet.get("categoryId", ""),
        "audio_language": snippet.get("defaultAudioLanguage", ""),
        "definition": content.get("definition", ""),
        "has_captions": 1 if content.get("caption", "false") == "true" else 0,
        "made_for_kids": 1 if status.get("madeForKids", False) else 0,
        "tags": "|".join(snippet.get("tags", [])),
        "topic_categories": "|".join(topic_names),
        "top_comments": top_comments,
        "matched_queries": "|".join(matched_queries),
        "buckets": buckets,
        "view_count": view_count,
        "like_count": like_count,
        "comment_count": comment_count,
        "views_to_subs_ratio": views_to_subs_ratio,
        "views_per_day": views_per_day,
    }


def build_channel_record(channel_id: str, info: dict) -> dict:
    """Map a fetch_channel_details entry to the `channels` columns."""
    return {
        "channel_id": channel_id,
        "subscriber_count": int(info.get("subscriber_count", 0) or 0),
        "channel_video_count": int(info.get("channel_video_count", 0) or 0),
        "channel_total_views": int(info.get("channel_total_views", 0) or 0),
        "channel_created_date": info.get("channel_created_date", ""),
        "channel_country": info.get("channel_country", ""),
        "channel_keywords": info.get("channel_keywords", ""),
    }


def persist_videos(conn, records: list[dict], now: str) -> None:
    """Upsert all video records in one transaction (rolls back on error)."""
    with db.transaction(conn):
        for record in records:
            db.upsert_video(conn, record, now)


def persist_channels(conn, records: list[dict], now: str) -> None:
    """Upsert all channel records in one transaction (rolls back on error)."""
    with db.transaction(conn):
        for record in records:
            db.upsert_channel(conn, record, now)


def persist_snapshots(conn, run_id: int, snapshot_args, captured_at: str) -> None:
    """Append one stats snapshot per video for this run, in one transaction."""
    with db.transaction(conn):
        for video_id, view_count, like_count, comment_count in snapshot_args:
            db.insert_snapshot(conn, run_id, video_id, captured_at,
                               view_count, like_count, comment_count)


# --- Rankings (Phase 3) ------------------------------------------------------

def rank_sort_key(row: dict) -> tuple:
    """Total-order ranking key: views_to_subs_ratio desc, then view_count desc,
    then video_id asc. The final video_id tiebreak makes the order fully
    deterministic even when both metrics tie, so re-runs on identical data
    produce identical rankings."""
    return (-row["views_to_subs_ratio"], -(row["view_count"] or 0), row["video_id"])


def compute_rankings(pool: list[dict], valid_buckets: set[str],
                     top_n: int) -> dict[str, list[tuple[str, float]]]:
    """Compute the top-`top_n` ranking for every lane from the tracked pool.

    `pool` rows are dicts with `video_id`, `buckets` (pipe-joined sorted set),
    `views_to_subs_ratio`, and `view_count`. Rows whose `views_to_subs_ratio` is
    None (hidden/missing counts) are excluded from ranking. Returns a dict keyed
    by every bucket in `valid_buckets` plus 'overall', each mapping to an ordered
    list of (video_id, views_to_subs_ratio) of length <= top_n. A lane with no
    qualifying videos maps to an empty list (the key is always present)."""
    eligible = [row for row in pool if row["views_to_subs_ratio"] is not None]

    def lane(rows: list[dict]) -> list[tuple[str, float]]:
        ranked = sorted(rows, key=rank_sort_key)[:top_n]
        return [(row["video_id"], row["views_to_subs_ratio"]) for row in ranked]

    rankings = {"overall": lane(eligible)}
    for bucket in valid_buckets:
        members = [row for row in eligible if bucket in row["buckets"].split("|")]
        rankings[bucket] = lane(members)
    return rankings


def _rank_phase(conn, rankings: dict[str, list[tuple[str, float]]],
                run_date: str, captured_at: str) -> None:
    """Replace every lane's rankings for `run_date` in one transaction. Logs each
    lane's row count (including empty lanes). Raises on DB failure so the caller
    can mark the run partial without losing already-persisted videos."""
    with db.transaction(conn):
        for bucket, ranked in rankings.items():
            db.replace_rankings(conn, run_date, bucket, ranked, captured_at)
            print(f"  ranked {bucket}: {len(ranked)} rows", file=sys.stderr)


def within_window(published_at: str, cutoff_dt: datetime) -> bool:
    """Return whether `published_at` (an RFC3339 API timestamp) is at or after
    `cutoff_dt`. Both sides are compared as aware datetimes (instants), never as
    strings — so fractional seconds or an offset form at the boundary are handled
    correctly. An unparseable timestamp returns False (excluded)."""
    try:
        published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return False
    return published_dt >= cutoff_dt


def eligible_pool(rows: list[dict], cutoff_dt: datetime) -> list[dict]:
    """Filter the fetched pool to videos published within the window (published_at
    >= cutoff_dt), comparing instants via within_window. Rows with an unparseable
    published_at are dropped and logged. Null-metric exclusion is NOT done here —
    that stays in compute_rankings."""
    kept = []
    for row in rows:
        if within_window(row["published_at"], cutoff_dt):
            kept.append(row)
        else:
            published_at = row.get("published_at")
            # Distinguish a parse failure (drop + log) from simply being old.
            try:
                datetime.fromisoformat((published_at or "").replace("Z", "+00:00"))
            except (ValueError, TypeError, AttributeError):
                print(f"WARNING: unparseable published_at {published_at!r} for "
                      f"video {row.get('video_id')!r}, excluded from ranking",
                      file=sys.stderr)
    return kept


def _recompute_rankings(conn, now: str) -> None:
    """Recompute and write all three lanes from the persisted pool. Shared by
    discover and refresh so both rank the same way. `now` is the Eastern timestamp;
    run_date is its date part (the offset never shifts the calendar date)."""
    cutoff_dt = datetime.fromisoformat(get_published_after().replace("Z", "+00:00"))
    pool = eligible_pool(db.fetch_ranking_pool(conn), cutoff_dt)
    rankings = compute_rankings(pool, VALID_BUCKETS, TOP_N)
    run_date = now[:10]  # Eastern calendar date
    db.run_with_db_retry(lambda: _rank_phase(conn, rankings, run_date, now))


def _run_refresh(youtube, conn, run_id: int, budget: "QuotaBudget",
                 now: str) -> tuple[int, bool]:
    """Refresh mode (cheap, no searches): re-pull videos.list stats for tracked
    videos in batches of 50, preserving each video's matched_queries / buckets /
    top_comments from the DB, upsert, write one snapshot per video, and recompute
    rankings from the refreshed numbers. Returns (videos_refreshed, persist_partial).

    Every videos.list call goes through the budget guard, so a near-cap refresh
    aborts gracefully rather than nicking the ceiling. videos.list is 1 unit and is
    flushed to the ledger at run end (no eager flush — refresh has no search.list)."""
    tracked = db.fetch_videos_for_refresh(conn)
    if not tracked:
        print("Refresh: no tracked videos yet — run --discover first.", file=sys.stderr)
        return 0, False

    by_id = {row["video_id"]: row for row in tracked}
    subs = db.fetch_channel_subs(conn)
    ids = list(by_id)
    print(f"Refresh: re-pulling stats for {len(ids)} videos...", file=sys.stderr)

    video_records: list[dict] = []
    snapshot_args: list[tuple] = []
    for i in range(0, len(ids), CHANNEL_BATCH_SIZE):
        batch = ids[i:i + CHANNEL_BATCH_SIZE]
        items = api_call_with_retry(
            lambda b=batch: fetch_video_details(youtube, b),
            budget,
            VIDEOS_QUOTA_COST,
        )
        if items is None:  # guard or quota stop — persist what we have
            break
        for item in items:
            preserved = by_id.get(item.get("id"))
            if preserved is None:
                continue
            matched = (preserved["matched_queries"] or "").split("|")
            ch_id = item.get("snippet", {}).get("channelId", "")
            ch_info = {"subscriber_count": subs.get(ch_id, 0)}
            record = build_video_record(
                item, matched, preserved["buckets"] or "", ch_info,
                preserved["top_comments"] or "",
            )
            video_records.append(record)
            snapshot_args.append(
                (record["video_id"], record["view_count"],
                 record["like_count"], record["comment_count"])
            )

    # Videos are the critical write (fatal); snapshots are non-fatal, mirroring
    # discover. Channels are NOT re-fetched or written in refresh.
    db.run_with_db_retry(lambda: persist_videos(conn, video_records, now))
    persist_partial = False
    try:
        db.run_with_db_retry(lambda: persist_snapshots(conn, run_id, snapshot_args, now))
    except Exception as e:
        persist_partial = True
        print(f"WARNING: snapshot persist failed, continuing: {e}", file=sys.stderr)

    try:
        _recompute_rankings(conn, now)
    except Exception as e:
        persist_partial = True
        print(f"WARNING: ranking phase failed, continuing: {e}", file=sys.stderr)

    return len(video_records), persist_partial


def estimate_discover_units() -> dict:
    """Estimate the quota a --discover run will cost, as a breakdown plus total.

    This is the SINGLE source of the estimate: both --dry-run and the pre-flight
    guard call it, so they can never disagree. Channel and comment call counts are
    rough heuristics (the true counts are unknown until the pool is fetched); the
    per-call guard is the real protection against overruns."""
    n = len(SEARCH_QUERIES)
    search = n * SEARCH_QUOTA_COST
    videos = n * VIDEOS_QUOTA_COST
    channels = ESTIMATED_CHANNEL_CALLS * CHANNELS_QUOTA_COST
    comments = ESTIMATED_COMMENT_CALLS * COMMENTS_QUOTA_COST
    return {
        "search": search, "videos": videos, "channels": channels,
        "comments": comments, "total": search + videos + channels + comments,
    }


def _print_dry_run(units_today: int, cap: int) -> None:
    """Print the planned queries and estimated quota cost without making any API
    calls (the --dry-run path). Output goes to stderr. `units_today`/`cap` are the
    current Pacific-day quota state so the user sees headroom, not just the raw
    estimate."""
    total_queries = len(SEARCH_QUERIES)
    est = estimate_discover_units()
    estimated_quota = est["total"]

    print("DRY RUN — no API calls will be made\n", file=sys.stderr)
    for i, entry in enumerate(SEARCH_QUERIES, 1):
        print(f'  [{i}/{total_queries}] "{entry["q"]}" ({entry["bucket"]})', file=sys.stderr)
    print(f"\nEstimated quota cost: ~{estimated_quota} units", file=sys.stderr)
    print(f"  search.list: {total_queries} × {SEARCH_QUOTA_COST} = {est['search']}", file=sys.stderr)
    print(f"  videos.list: {total_queries} × {VIDEOS_QUOTA_COST} = {est['videos']}", file=sys.stderr)
    print(f"  channels.list: ~{est['channels']}", file=sys.stderr)
    print(f"  commentThreads.list: ~{est['comments']}", file=sys.stderr)
    print(f"\nToday (Pacific): {units_today} units used, cap {cap} "
          f"(limit {DAILY_QUOTA_LIMIT} − buffer {SAFETY_BUFFER}), "
          f"{max(cap - units_today, 0)} remaining", file=sys.stderr)
    fits = "yes" if units_today + estimated_quota <= cap else "NO — would pre-flight stop"
    print(f"Discover fits under cap today? {fits}", file=sys.stderr)
    print(f"Daily limit: {DAILY_QUOTA_LIMIT:,} units → ~{DAILY_QUOTA_LIMIT // estimated_quota} runs/day", file=sys.stderr)


def _normalize_state(state: dict) -> dict:
    """Coerce a loaded state file into the current {queries, channels, comments}
    shape, upgrading a legacy flat {query: [videos]} state in place."""
    if "queries" in state:
        return state
    if any(isinstance(v, list) for v in state.values()):
        return {"queries": state, "channels": {}, "comments": {}}
    return {"queries": {}, "channels": {}, "comments": {}}


def _run_search_phase(youtube, state: dict, budget: "QuotaBudget",
                      conn, today_pac: str
                      ) -> tuple[list[tuple[str, dict]], bool]:
    """Phase A: search, fetch, and filter each query, resuming cached results.

    Returns the (query, video) pairs collected and whether the run aborted early
    because quota was exhausted. Newly fetched results are cached into `state`.

    Each 100-unit search.list charge is flushed to the quota_ledger immediately
    (its own committed transaction, separate from any phase write) so a hard kill
    cannot lose record of units Google already charged. `conn`/`today_pac` are
    threaded in only for that eager flush."""
    total_queries = len(SEARCH_QUERIES)
    all_results: list[tuple[str, dict]] = []
    quota_aborted = False
    # Same window cutoff the ranking phase uses (single source).
    cutoff_dt = datetime.fromisoformat(get_published_after().replace("Z", "+00:00"))

    for i, entry in enumerate(SEARCH_QUERIES, 1):
        q = entry["q"]
        if q in state["queries"]:
            cached = state["queries"][q]
            for video in cached:
                all_results.append((q, video))
            print(f'[{i}/{total_queries}] "{q}": resumed {len(cached)} cached results', file=sys.stderr)
            continue

        video_ids = api_call_with_retry(
            lambda qq=q: search_videos(youtube, qq),
            budget,
            SEARCH_QUOTA_COST,
        )
        if video_ids is None:
            quota_aborted = True
            break
        # Eager ledger flush: the expensive search.list charge is now spent and
        # recorded, durable against a later phase rollback or a hard kill.
        db.run_with_db_retry(
            lambda: db.add_quota_units(conn, today_pac, SEARCH_QUOTA_COST, now_local_iso()))
        budget.flushed_units += SEARCH_QUOTA_COST

        details = api_call_with_retry(
            lambda ids=video_ids: fetch_video_details(youtube, ids),
            budget,
            VIDEOS_QUOTA_COST,
        )
        if details is None:
            quota_aborted = True
            break

        kept, drops = filter_videos(details, MIN_VIEWS, SHORT_MAX_SECONDS, cutoff_dt)

        for video in kept:
            all_results.append((q, video))

        state["queries"][q] = kept
        save_state(state)

        # Per-query drop diagnostics. The summed buckets equal len(details) - kept
        # (each video counted under the first gate that rejects it); the views-only
        # line is an independent, non-exclusive count so window-vs-views strictness
        # can be disentangled before any threshold is tuned.
        note = ""
        if len(details) != len(video_ids):
            note = f" ({len(video_ids) - len(details)} ids returned no details)"
        below, hidden = views_below_min(details, MIN_VIEWS)
        print(f'[{i}/{total_queries}] "{q}": fetched {len(details)}, kept {len(kept)}{note}',
              file=sys.stderr)
        print(f'       dropped: window {drops["window"]}, views {drops["views"]}, '
              f'duration {drops["duration"]}, category {drops["category"]}, '
              f'language {drops["language"]}, made_for_kids {drops["made_for_kids"]}, '
              f'missing_duration {drops["missing_duration"]}, '
              f'missing_views {drops["missing_views"]}', file=sys.stderr)
        print(f'       (views-only check: {below}/{len(details)} below '
              f'MIN_VIEWS={MIN_VIEWS}; {hidden} hidden/absent)', file=sys.stderr)

    return all_results, quota_aborted


def _run_channel_phase(youtube, state: dict, seen_videos: dict[str, dict],
                       budget: "QuotaBudget") -> dict[str, dict]:
    """Phase B: fetch channel data for every distinct channel, resuming a cached
    channel map from `state` when present."""
    if not state.get("channels"):
        unique_channel_ids = list({v["snippet"]["channelId"] for v in seen_videos.values()})
        print(f"Fetching channel data for {len(unique_channel_ids)} channels...", file=sys.stderr)
        channel_map = fetch_channel_details(youtube, unique_channel_ids, budget)
        state["channels"] = channel_map
        save_state(state)
    else:
        channel_map = state["channels"]
        print(f"Resumed {len(channel_map)} cached channels", file=sys.stderr)
    return channel_map


def _run_comment_phase(youtube, state: dict, seen_videos: dict[str, dict],
                       budget: "QuotaBudget") -> None:
    """Phase C: fetch top comments for every video missing them, caching each
    result into `state` (and periodically persisting it)."""
    if "comments" not in state:
        state["comments"] = {}

    videos_needing_comments = [vid for vid in seen_videos if vid not in state["comments"]]
    if videos_needing_comments:
        print(f"Fetching comments for {len(videos_needing_comments)} videos...", file=sys.stderr)
    for idx, vid in enumerate(videos_needing_comments, 1):
        comments_str = fetch_top_comments(youtube, vid, budget)
        state["comments"][vid] = comments_str
        if idx % 10 == 0:
            save_state(state)
            print(f"  Comments: {idx}/{len(videos_needing_comments)}", file=sys.stderr)
    if videos_needing_comments:
        save_state(state)


def _build_records(state: dict, seen_videos: dict[str, dict],
                   query_map: dict[str, list[str]], channel_map: dict[str, dict]
                   ) -> tuple[list[dict], list[tuple], list[dict]]:
    """Assemble the video records, per-video snapshot args, and channel records
    to persist from the deduped videos and fetched channel/comment data."""
    q_to_bucket = {e["q"]: e["bucket"] for e in SEARCH_QUERIES}

    video_records = []
    snapshot_args = []
    for vid, video in seen_videos.items():
        ch_id = video["snippet"]["channelId"]
        ch_info = channel_map.get(ch_id, {})
        comments = state["comments"].get(vid, "")
        matched = query_map[vid]
        buckets = bucket_union(matched, q_to_bucket)
        record = build_video_record(video, matched, buckets, ch_info, comments)
        video_records.append(record)
        snapshot_args.append(
            (vid, record["view_count"], record["like_count"], record["comment_count"])
        )

    channel_records = [build_channel_record(cid, info) for cid, info in channel_map.items()]
    return video_records, snapshot_args, channel_records


def _persist_all(conn, run_id: int, video_records: list[dict],
                 channel_records: list[dict], snapshot_args, now: str) -> bool:
    """Persist videos (fatal on failure), then channels and snapshots (non-fatal:
    a failure is warned and skipped). Returns whether any non-fatal write failed."""
    persist_partial = False

    # Videos are the critical write: a failure here is fatal (rolls back, raises).
    db.run_with_db_retry(lambda: persist_videos(conn, video_records, now))

    # Channels and snapshots are non-fatal: persist what we have and continue.
    try:
        db.run_with_db_retry(lambda: persist_channels(conn, channel_records, now))
    except Exception as e:
        persist_partial = True
        print(f"WARNING: channel persist failed, continuing: {e}", file=sys.stderr)

    try:
        db.run_with_db_retry(lambda: persist_snapshots(conn, run_id, snapshot_args, now))
    except Exception as e:
        persist_partial = True
        print(f"WARNING: snapshot persist failed, continuing: {e}", file=sys.stderr)

    return persist_partial


def _run_discover(youtube, conn, run_id: int, budget: "QuotaBudget", state: dict,
                  today_pac: str) -> tuple[int, bool, bool]:
    """Discover mode (expensive): run the search/channel/comment phases, persist
    everything, and recompute rankings. Returns
    (videos_seen, quota_aborted, persist_partial)."""
    all_results, quota_aborted = _run_search_phase(youtube, state, budget, conn, today_pac)

    seen_videos, query_map = dedupe_videos(all_results)
    print(f"\n{len(seen_videos)} unique videos after dedup", file=sys.stderr)

    channel_map = _run_channel_phase(youtube, state, seen_videos, budget)
    _run_comment_phase(youtube, state, seen_videos, budget)

    now = now_local_iso()
    video_records, snapshot_args, channel_records = _build_records(
        state, seen_videos, query_map, channel_map
    )
    persist_partial = _persist_all(
        conn, run_id, video_records, channel_records, snapshot_args, now
    )

    # Recompute all three rankings from the persisted pool. A failure here is
    # non-fatal: persisted videos are kept and the run is marked partial.
    try:
        _recompute_rankings(conn, now)
    except Exception as e:
        persist_partial = True
        print(f"WARNING: ranking phase failed, continuing: {e}", file=sys.stderr)

    return len(seen_videos), quota_aborted, persist_partial


def _discover_done_today(conn, today_pac: str) -> bool:
    """True when a discover run with status 'success' or 'partial' exists for
    today's Pacific date — its expensive searches already ran, so the no-flag
    default should refresh rather than re-discover. The Pacific date is derived
    from each run's stored Eastern started_at."""
    for row in db.fetch_runs_by_mode(conn, "discover"):
        if (row["status"] in ("success", "partial")
                and pacific_date(row["started_at"]) == today_pac):
            return True
    return False


def _pick_status(budget: "QuotaBudget", quota_aborted: bool,
                 persist_partial: bool, downgraded: bool) -> str:
    """Resolve the terminal run_log status. Order matters: a proactive guard stop
    and a reactive quota stop are the most urgent signals; a downgrade is recorded
    distinctly (never as a plain success) only on an otherwise-clean run."""
    if budget.guard_stopped:
        return "quota_guard_stop"
    if quota_aborted:
        return "quota_exceeded"
    if persist_partial:
        return "partial"
    if downgraded:
        return "discover_downgraded_to_refresh"
    return "success"


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube Habits Swipe File Builder")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true",
                            help="Print planned queries and estimated quota cost without making API calls")
    mode_group.add_argument("--discover", action="store_true",
                            help="Force a discovery run (expensive: searches + enrichment)")
    mode_group.add_argument("--refresh", action="store_true",
                            help="Force a cheap stats refresh of tracked videos (no searches)")
    args = parser.parse_args()

    cap = DAILY_QUOTA_LIMIT - SAFETY_BUFFER

    if args.dry_run:
        # No API calls; read today's quota state so the estimate shows headroom.
        db.init_db(DB_PATH)
        conn = db.get_connection(DB_PATH)
        try:
            units_today = db.get_units_used(conn, pacific_date())
        finally:
            conn.close()
        _print_dry_run(units_today, cap)
        return

    load_dotenv()
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        print("ERROR: YOUTUBE_API_KEY not found in .env", file=sys.stderr)
        sys.exit(1)

    try:
        validate_config()
    except ConfigError as e:
        print(f"ERROR: invalid config: {e}", file=sys.stderr)
        sys.exit(1)

    db.init_db(DB_PATH)
    conn = db.get_connection(DB_PATH)

    today_pac = pacific_date()
    units_today = db.get_units_used(conn, today_pac)

    # Choose the run mode.
    if args.refresh:
        mode = "refresh"
    elif args.discover:
        mode = "discover"
    else:
        mode = "refresh" if _discover_done_today(conn, today_pac) else "discover"

    # Pre-flight guard (discover only): refuse if the estimate won't fit the cap.
    downgraded = False
    if mode == "discover":
        estimate = estimate_discover_units()["total"]
        if units_today + estimate > cap:
            remaining = max(cap - units_today, 0)
            if args.discover:  # explicit discover -> hard stop, no work done
                run_id = db.start_run(conn, "discover", now_local_iso())
                db.finish_run(conn, run_id, now_local_iso(), 0, 0, "quota_preflight_stop")
                print(f"PRE-FLIGHT STOP: discover needs ~{estimate} units but only "
                      f"{remaining} remain under the cap ({cap}). No API calls made.",
                      file=sys.stderr)
                conn.close()
                return
            # no-flag default -> loud downgrade to a cheap refresh
            print(f"DOWNGRADE: discover needs ~{estimate} units but only {remaining} "
                  f"remain under the cap ({cap}); running a refresh instead.",
                  file=sys.stderr)
            mode, downgraded = "refresh", True

    run_id = db.start_run(conn, mode, now_local_iso())
    youtube = build_youtube_client(api_key)
    budget = QuotaBudget(units_today, cap)
    # state.json is the discover resume cache; refresh neither reads nor clears it.
    state = _normalize_state(load_state()) if mode == "discover" else {}
    videos_seen = 0
    quota_aborted = False
    persist_partial = False
    status = "failed"

    try:
        if mode == "discover":
            videos_seen, quota_aborted, persist_partial = _run_discover(
                youtube, conn, run_id, budget, state, today_pac
            )
        else:
            videos_seen, persist_partial = _run_refresh(
                youtube, conn, run_id, budget, now_local_iso()
            )

        status = _pick_status(budget, quota_aborted, persist_partial, downgraded)
        # Only a clean discover clears the resume cache; an aborted/partial discover
        # keeps it, and refresh never touches it.
        if mode == "discover" and status == "success":
            clear_state()

        print(f"\n[{mode}] processed {videos_seen} videos to {DB_PATH} "
              f"(run {run_id}, status: {status})", file=sys.stderr)
        print(f"Quota consumed this run: {budget.run_units} units "
              f"({units_today + budget.run_units}/{cap} used today)", file=sys.stderr)
    finally:
        try:
            # Flush only the un-flushed remainder (cheap calls); search.list was
            # already flushed eagerly. Its own committed transaction.
            if budget.unflushed() > 0:
                db.run_with_db_retry(lambda: db.add_quota_units(
                    conn, today_pac, budget.unflushed(), now_local_iso()))
            db.finish_run(conn, run_id, now_local_iso(), budget.run_units,
                          videos_seen, status)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
