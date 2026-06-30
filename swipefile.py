import argparse
import csv
import http.client
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httplib2
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

import classify
import config
import db
import llm
from config import (
    BLOCKED_CATEGORY_IDS,
    CATEGORIES_QUOTA_COST,
    CATEGORY_REGION,
    CHANNEL_BATCH_SIZE,
    CHANNELS_QUOTA_COST,
    COMMENTS_PER_VIDEO,
    COMMENTS_QUOTA_COST,
    COMMENTS_SHEET_COLUMNS,
    CSV_COLUMNS,
    DAILY_QUOTA_LIMIT,
    DB_PATH,
    DISTRIBUTION_BUCKETS,
    KID_TITLE_KEYWORDS,
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
    EXIT_AUTH,
    EXIT_CLASSIFY_BUDGET,
    EXIT_DB_LOCKED,
    EXIT_NETWORK,
    EXIT_NO_ROWS,
    EXIT_OK,
    EXIT_OTHER,
    EXIT_QUOTA,
    NETWORK_FAILURE_REASON,
    QUOTA_STOP_STATUSES,
    ConfigError,
    distribution_buckets,
    exit_code_for_api_failure,
    get_published_after,
    now_local_iso,
    pacific_date,
    validate_config,
)


# Reason slugs for the single PIPELINE_RESULT line, keyed by exit code. Purely
# for the grep-able log; the code itself is the contract.
_EXIT_REASON = {
    EXIT_OK: "ok",
    EXIT_NETWORK: "network",
    EXIT_QUOTA: "quota_exceeded",
    EXIT_AUTH: "auth",
    EXIT_OTHER: "other",
    EXIT_DB_LOCKED: "db_locked",
    EXIT_NO_ROWS: "no_rows",
    EXIT_CLASSIFY_BUDGET: "classify_budget_exceeded",
}


class PipelineApiError(Exception):
    """A terminal YouTube API failure raised by api_call_with_retry after its own
    in-function retries are exhausted. Carries the RAW signal only (`reason` from
    error_details, `status` HTTP code); classification into an exit code is done
    solely by config.exit_code_for_api_failure in main(). This is never raised for
    benign per-item conditions (comments disabled, video not found), which stay a
    graceful None skip."""

    def __init__(self, reason: str, status: int | None):
        self.reason = reason
        self.status = status
        super().__init__(f"reason={reason!r} status={status}")

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


def title_has_kid_keyword(title: str, keywords) -> str | None:
    """Return the FIRST keyword in `keywords` found in `title` as a whole word
    (case-insensitive), else None. Word-boundary matched so `kid` never fires inside
    `kidney`/`kidding`; a multi-word entry ('for kids') matches as a boundary-wrapped
    phrase. Pure and list-injected: the cheap gate passes config.KID_TITLE_KEYWORDS,
    and because a match is a PERMANENT drop the curated, high-precision list lives in
    config (not here). Iterating in list order makes 'which keyword' deterministic for
    logging."""
    for kw in keywords:
        if re.search(rf"\b{re.escape(kw)}\b", title, re.IGNORECASE):
            return kw
    return None


def _classify_video(video: dict, min_views: int, max_duration: int,
                    cutoff_dt: datetime) -> str:
    """Return the name of the FIRST gate that rejects `video`, or 'kept' if it
    passes all of them. The single source of the gate sequence — filter_videos and
    every tuning diagnostic derive from this, so they can never disagree.

    Defensive per the plan-doc: every API field is read with .get(), and a video
    is failed CLOSED when a field needed to confirm it qualifies is missing — a
    missing contentDetails.duration (is_short cannot be derived; do NOT assume
    Short) or a hidden/absent statistics.viewCount (cannot confirm min_views) is
    excluded, never crashes, never passes unchecked. The window gate reuses
    within_window with the same cutoff ranking uses."""
    snippet = video.get("snippet", {})
    content = video.get("contentDetails", {})
    stats = video.get("statistics", {})
    status = video.get("status", {})

    if not within_window(snippet.get("publishedAt", ""), cutoff_dt):
        return "window"
    if status.get("madeForKids", False) or status.get("selfDeclaredMadeForKids", False):
        return "made_for_kids"
    # Deterministic kid/parenting title reject, right after the API made-for-kids flag
    # (both are kid drops). Runs BEFORE the LLM classify phase, so an obvious kid video
    # is dropped cheaply and never spends an LLM call. A miss (innocent title) falls
    # through to the LLM gate downstream, exactly as before.
    if title_has_kid_keyword(snippet.get("title", ""), KID_TITLE_KEYWORDS):
        return "kid_keyword"
    if snippet.get("categoryId", "") in BLOCKED_CATEGORY_IDS:
        return "category"
    lang = snippet.get("defaultAudioLanguage", "")
    if lang and not lang.startswith("en"):
        return "language"
    duration_str = content.get("duration")
    if not duration_str:                         # cannot derive is_short
        return "missing_duration"
    view_raw = stats.get("viewCount")
    if view_raw is None:                         # hidden/absent -> cannot confirm
        return "missing_views"
    if parse_duration(duration_str) > max_duration:
        return "duration"
    if int(view_raw) < min_views:
        return "views"
    return "kept"


def filter_videos(videos: list[dict], min_views: int, max_duration: int,
                  cutoff_dt: datetime) -> tuple[list[dict], dict[str, int]]:
    """Keep English, non-kids, non-blocked, in-window Shorts at/under max_duration
    with at least min_views. Returns (kept, drops); each excluded video is counted
    under the FIRST gate that rejects it (via _classify_video), so
    sum(drops.values()) == len(videos) - len(kept). All gate logic lives in
    _classify_video — this is a thin loop over it."""
    drops = {k: 0 for k in (
        "window", "views", "duration", "category", "language",
        "made_for_kids", "kid_keyword", "missing_duration", "missing_views",
    )}
    kept = []
    for video in videos:
        gate = _classify_video(video, min_views, max_duration, cutoff_dt)
        if gate == "kept":
            kept.append(video)
        else:
            drops[gate] += 1
    return kept, drops


# --- Tuning diagnostics (read-only; never perturb the drop counts) -----------

# DISTRIBUTION_BUCKETS and distribution_buckets() now live in config.py (stdlib-
# only) so the dashboard can reuse the exact thresholds without importing this
# module; both are re-imported above and remain available as swipefile.* here.


def qualifying_view_counts(videos: list[dict], min_views: int, max_duration: int,
                           cutoff_dt: datetime) -> list[int]:
    """View counts (descending) of every video that passes every gate EXCEPT the
    views threshold — i.e. _classify_video is 'kept' or 'views' (viewCount is
    guaranteed present for both, the missing_views gate precedes them). The
    per-query near-miss line is the < min_views slice; the aggregate
    qualifying-view distribution buckets the whole list (incl. kept), so the
    >=100k bucket is a true count."""
    counts = []
    for video in videos:
        gate = _classify_video(video, min_views, max_duration, cutoff_dt)
        if gate in ("kept", "views"):
            counts.append(int(video.get("statistics", {}).get("viewCount")))
    counts.sort(reverse=True)
    return counts


def language_drop_values(videos: list[dict], min_views: int, max_duration: int,
                         cutoff_dt: datetime) -> dict[str, int]:
    """Tally of the present defaultAudioLanguage values that triggered the language
    gate (_classify_video == 'language'), e.g. {'es': 5, 'hi': 3}. Reveals whether
    the gate is wrongly dropping English regional variants (en-US/en-GB -> gate
    bug) or genuinely dropping foreign content (gate correct -> tune search params).
    Blank never appears here (the gate only fires on a present, non-'en' value)."""
    tally: dict[str, int] = {}
    for video in videos:
        if _classify_video(video, min_views, max_duration, cutoff_dt) == "language":
            lang = video.get("snippet", {}).get("defaultAudioLanguage", "")
            tally[lang] = tally.get(lang, 0) + 1
    return tally


def untagged_audio_language_count(videos: list[dict]) -> int:
    """Batch-wide count of videos with a blank/missing defaultAudioLanguage. These
    PASS the language gate (it only fires on a present non-'en' value), so this is
    the blind-spot signal: untagged (possibly foreign) videos slipping through."""
    return sum(1 for v in videos
               if not v.get("snippet", {}).get("defaultAudioLanguage", ""))


def _fmt_counts(counts: list[int]) -> str:
    """Render view counts for the diagnostic line in k-notation (>=1000 -> '92k'),
    raw below. Display-only; never used for bucketing."""
    if not counts:
        return "(none)"
    return ", ".join(f"{c // 1000}k" if c >= 1000 else str(c) for c in counts)


def _fmt_tally(tally: dict[str, int]) -> str:
    """Render a value->count tally sorted by count desc, '(none)' when empty."""
    if not tally:
        return "(none)"
    return ", ".join(f"{k}: {v}" for k, v in sorted(tally.items(), key=lambda kv: -kv[1]))


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


def fetch_video_categories(youtube, region: str, budget: "QuotaBudget") -> list[dict]:
    """Fetch the full YouTube category list for `region` and map it to `categories`
    records. One quota unit, one call. Each record carries the numeric category_id
    (the same value stored in videos.category_id), its human-readable title, and
    the sourcing region. Returns [] on a quota abort (None response), matching the
    channel/comment fetches so the caller skips persistence rather than crashing."""
    response = api_call_with_retry(
        lambda: youtube.videoCategories().list(
            part="snippet",
            regionCode=region,
        ).execute(),
        budget,
        CATEGORIES_QUOTA_COST,
    )
    if response is None:
        return []
    return [
        {
            "category_id": item["id"],
            "title": item.get("snippet", {}).get("title", ""),
            "region_code": region,
        }
        for item in response.get("items", [])
    ]


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
            status = e.resp.status

            # Benign per-item conditions: a private/deleted video or a video with
            # comments off. These are normal data, not a systemic failure, so we
            # skip the one item and keep going. NEVER raised: one private video
            # must not abort the whole run.
            if reason in ("commentsDisabled", "forbidden", "videoNotFound"):
                return None

            # Daily quota cap: retrying same-day is pointless, so raise straight
            # away. main() classifies this as EXIT_QUOTA.
            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                print(f"ERROR: Quota exhausted after {budget.run_units} units. {e}",
                      file=sys.stderr)
                raise PipelineApiError(reason, status)

            # Transient rate limiting (HTTP 429 or the 403 rate-limit reasons):
            # retry once in-function; only a still-failing call surfaces (as
            # EXIT_NETWORK) below.
            transient = status == 429 or reason in (
                "rateLimitExceeded", "userRateLimitExceeded")
            if transient and attempt == 0:
                print("Rate limited, retrying in 5s...", file=sys.stderr)
                time.sleep(5)
                continue

            # Every other HttpError is terminal: raise with the RAW reason/status
            # and let config.exit_code_for_api_failure decide (keyInvalid/401 ->
            # AUTH, exhausted rate-limit -> NETWORK, a bare 400 or unknown reason
            # -> OTHER). No sys.exit here; main() is the sole exit-code decider.
            print(f"ERROR: API error: {e}", file=sys.stderr)
            raise PipelineApiError(reason, status)
        except (OSError, httplib2.HttpLib2Error, http.client.HTTPException) as e:
            # Connectivity/transport failure, retried once then raised -> EXIT_NETWORK.
            # The tuple is verified against the installed stack (googleapiclient over
            # httplib2): OSError covers socket-level errors incl. ConnectionError,
            # TimeoutError (socket.timeout is TimeoutError) and socket.gaierror;
            # httplib2.HttpLib2Error covers ServerNotFoundError (DNS lookup failure,
            # which is NOT an OSError); http.client.HTTPException covers a malformed
            # response mid-transfer.
            if attempt == 0:
                print(f"Network error, retrying in 2s... ({e})", file=sys.stderr)
                time.sleep(2)
                continue
            print(f"ERROR: Network failure: {e}", file=sys.stderr)
            raise PipelineApiError(NETWORK_FAILURE_REASON, None)
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


def persist_categories(conn, records: list[dict], now: str) -> None:
    """Upsert all category records in one transaction (rolls back on error)."""
    with db.transaction(conn):
        for record in records:
            db.upsert_category(conn, record, now)


def persist_snapshots(conn, run_id: int, snapshot_args, captured_at: str) -> None:
    """Append one stats snapshot per video for this run, in one transaction. Each
    arg is (video_id, view_count, like_count, comment_count, subscriber_count)."""
    with db.transaction(conn):
        for (video_id, view_count, like_count, comment_count,
             subscriber_count) in snapshot_args:
            db.insert_snapshot(conn, run_id, video_id, captured_at,
                               view_count, like_count, comment_count,
                               subscriber_count)


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


def videos_to_age_out(candidates: list[dict], cutoff_dt: datetime) -> list[str]:
    """Pure decision: of the active+unstarred `candidates` (each a dict with
    video_id + last_view_growth_at, from db.fetch_aging_candidates), return the
    video_ids to retire — those whose view count has not grown since `cutoff_dt`
    (= now - REFRESH_MAX_AGE_DAYS). Growth at or before the cutoff retires (`<=`);
    strictly after keeps. Comparison is instant-vs-instant on aware datetimes, NOT
    a lexical string compare, so a DST offset shift (-04:00/-05:00) is handled
    chronologically.

    `cutoff_dt` MUST be offset-aware: the assert turns a naive cutoff into a loud
    failure here rather than a `TypeError: can't compare offset-naive and
    offset-aware` on the first candidate (or a silent miscompare). now_local_iso()
    always carries an Eastern offset, so a correctly-built cutoff is aware.

    A NULL or unparseable last_view_growth_at is KEPT (fail-safe: never retire on a
    missing clock) and logged. A new catch sets the clock to first_seen_at and the
    v12 migration backfills every row, so a NULL should never occur — it signals a
    backfill miss or a bug, not a new video. A persistent NULL is a latent leak (a
    row that can never age out), so this branch is a guard against bugs, not an
    expected path."""
    assert cutoff_dt.tzinfo is not None, "cutoff_dt must be offset-aware"
    to_retire = []
    for row in candidates:
        ts = row.get("last_view_growth_at")
        try:
            grew_at = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            print(f"WARNING: unparseable/NULL last_view_growth_at {ts!r} for video "
                  f"{row.get('video_id')!r}, kept (not aged out)", file=sys.stderr)
            continue
        if grew_at <= cutoff_dt:
            to_retire.append(row["video_id"])
    return to_retire


def _recompute_rankings(conn, captured_at: str, run_date: str) -> None:
    """Recompute and write all three lanes from the persisted pool. Shared by
    discover and refresh so both rank the same way. `run_date` is the Eastern
    rankings key captured ONCE at run start in main() and threaded here, so the
    rows are stamped with the same date main() later counts (a midnight-straddling
    run never diverges). `captured_at` is the live Eastern write timestamp."""
    cutoff_dt = datetime.fromisoformat(get_published_after().replace("Z", "+00:00"))
    pool = eligible_pool(db.fetch_ranking_pool(conn), cutoff_dt)
    rankings = compute_rankings(pool, VALID_BUCKETS, TOP_N)
    db.run_with_db_retry(lambda: _rank_phase(conn, rankings, run_date, captured_at))


def _run_catalog_sweep(youtube, conn, run_id: int, budget: "QuotaBudget",
                       now: str, run_date: str, *, mode: str = "refresh",
                       run_started: str | None = None) -> tuple[int, bool]:
    """The shared catalog sweep: re-pull videos.list stats for the active tracked
    catalog in batches of CHANNEL_BATCH_SIZE (50), preserving each video's
    matched_queries / buckets / top_comments from the DB, upsert, write one
    snapshot per video, and recompute rankings from the refreshed numbers. Returns
    (videos_refreshed, persist_partial). Called by --refresh, and (Phase 3) by
    discover after it persists its catch, so one daily run snapshots the whole
    catalog.

    Every videos.list call goes through the budget guard, so a near-cap sweep aborts
    gracefully (persisting what it has already fetched) rather than nicking the
    ceiling. videos.list is 1 unit, charged via api_call_with_retry and flushed to
    the ledger at run end."""
    tracked = db.fetch_videos_for_refresh(conn)
    if not tracked:
        print("Catalog sweep: no active videos to snapshot.", file=sys.stderr)
        return 0, False

    by_id = {row["video_id"]: row for row in tracked}
    subs = db.fetch_channel_subs(conn)
    ids = list(by_id)
    print(f"Catalog sweep: re-pulling stats for {len(ids)} videos...", file=sys.stderr)

    # The most recent snapshot per video STRICTLY BEFORE this run, read once before
    # we write any of this run's snapshots — the order-immune growth source. A
    # video's view_count rose iff this run's fetched count beats its prior snapshot
    # (db.is_view_growth), the SAME definition the v12 backfill uses.
    prior = db.latest_snapshot_view_counts(conn, run_id)

    video_records: list[dict] = []
    snapshot_args: list[tuple] = []
    returned_ids: set[str] = set()   # ids the API returned a row for
    sent_ids: set[str] = set()       # ids in batches the API actually ANSWERED
    grown_ids: list[str] = []        # returned ids whose view_count beat the prior snapshot
    swept_fully = True               # cleared if a guard/quota stop truncates the loop
    for i in range(0, len(ids), CHANNEL_BATCH_SIZE):
        batch = ids[i:i + CHANNEL_BATCH_SIZE]
        items = api_call_with_retry(
            lambda b=batch: fetch_video_details(youtube, b),
            budget,
            VIDEOS_QUOTA_COST,
        )
        if items is None:  # guard or quota stop — persist what we have, defer aging
            swept_fully = False
            break
        # This batch was answered in full (videos.list omits invalid ids but never
        # truncates a guarded call), so every id we asked about is now confirmed
        # present-or-gone.
        sent_ids.update(batch)
        for item in items:
            item_id = item.get("id")
            returned_ids.add(item_id)
            preserved = by_id.get(item_id)
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
            # v14: capture the channel's current sub count on the snapshot row (the
            # value already fetched for the ratio); None when the channel is absent so
            # the read falls back, never a fake 0 that would inflate the ratio.
            snapshot_args.append(
                (record["video_id"], record["view_count"],
                 record["like_count"], record["comment_count"], subs.get(ch_id))
            )
            if db.is_view_growth(prior.get(item_id), record["view_count"]):
                grown_ids.append(item_id)

    # Confirmed asked-but-absent ids are gone (deleted/private/region-blocked).
    # Truncation-safe: an unanswered batch never enters sent_ids, so a quota/guard
    # stop can't false-positive its videos as gone.
    gone_ids = sent_ids - returned_ids

    # Videos are the critical write (fatal); everything below is non-fatal. Channels
    # are NOT re-fetched or written in the sweep.
    db.run_with_db_retry(lambda: persist_videos(conn, video_records, now))
    persist_partial = False
    try:
        db.run_with_db_retry(lambda: persist_snapshots(conn, run_id, snapshot_args, now))
    except Exception as e:
        persist_partial = True
        print(f"WARNING: snapshot persist failed, continuing: {e}", file=sys.stderr)

    # Growth clock, gone transitions, and the aging pass in ONE transaction so the
    # aging read sees the just-bumped clocks and the just-marked-gone exclusions.
    # Aging runs LAST and ONLY on a complete sweep: a truncated sweep didn't give
    # every active video its confirming fetch this run, so retiring on stale clocks
    # (aged_out is sticky) is deferred to the next full sweep.
    try:
        with db.transaction(conn):
            db.bump_view_growth(conn, grown_ids, now)
            for vid in gone_ids:
                db.set_video_status(conn, vid, "gone", now)
            if swept_fully:
                candidates = db.fetch_aging_candidates(conn)
                cutoff = datetime.fromisoformat(now) - timedelta(
                    days=config.REFRESH_MAX_AGE_DAYS)
                for vid in videos_to_age_out(candidates, cutoff):
                    db.set_video_status(conn, vid, "aged_out", now)
    except Exception as e:
        persist_partial = True
        print(f"WARNING: status/growth writes failed, continuing: {e}", file=sys.stderr)

    try:
        _recompute_rankings(conn, now, run_date)
    except Exception as e:
        persist_partial = True
        print(f"WARNING: ranking phase failed, continuing: {e}", file=sys.stderr)

    counts = db.run_change_counts(conn, run_id, now)
    print(f"Run summary: added={counts['added']} updated={counts['updated']} "
          f"aged_out={counts['aged_out']} gone={counts['gone']}", file=sys.stderr)

    # Persist the run summary (non-fatal: a missed summary row doesn't fail the run).
    # classify_cost is summed over THIS run's wall-clock window [run_started, now] —
    # a refresh window contains no classify calls, so it is 0 (the classify-free
    # invariant, computed not hardcoded).
    try:
        snapshot_count = db.count_snapshots_for_run(conn, run_id)
        classify_cost = db.classify_cost_in_window(
            conn, _CLASSIFY_SCOPE, run_started or now, now)
        with db.transaction(conn):
            db.write_run_summary(conn, run_id, run_date, mode, counts,
                                 classify_cost, snapshot_count, now)
    except Exception as e:
        persist_partial = True
        print(f"WARNING: run_summary persist failed, continuing: {e}", file=sys.stderr)

    return len(video_records), persist_partial


def estimate_discover_units(eligible_count: int) -> dict:
    """Estimate the quota a --discover run will cost, as a breakdown plus total.

    This is the SINGLE source of the estimate: both --dry-run and the pre-flight
    guard call it, so they can never disagree. Channel and comment call counts are
    rough heuristics (the true counts are unknown until the pool is fetched); the
    per-call guard is the real protection against overruns.

    `eligible_count` is the active catalog size (db.count_eligible_for_refresh) and
    drives the `sweep` term: the merged discover re-fetches the whole active catalog
    via videos.list after its catch, costing ceil(eligible_count / CHANNEL_BATCH_SIZE)
    units. This is the PRE-catch count, so the term undercounts by ~one batch (the
    just-caught videos the sweep also refreshes) — an accepted approximation."""
    n = len(SEARCH_QUERIES)
    search = n * SEARCH_QUOTA_COST
    videos = n * VIDEOS_QUOTA_COST
    channels = ESTIMATED_CHANNEL_CALLS * CHANNELS_QUOTA_COST
    comments = ESTIMATED_COMMENT_CALLS * COMMENTS_QUOTA_COST
    sweep = ((eligible_count + CHANNEL_BATCH_SIZE - 1) // CHANNEL_BATCH_SIZE) * VIDEOS_QUOTA_COST
    return {
        "search": search, "videos": videos, "channels": channels,
        "comments": comments, "sweep": sweep,
        "total": search + videos + channels + comments + sweep,
    }


def _print_dry_run(units_today: int, cap: int, eligible_count, pending=None) -> None:
    """Print the planned queries and estimated quota cost without making any API
    calls (the --dry-run path). Output goes to stderr. `units_today`/`cap` are the
    current Pacific-day quota state so the user sees headroom, not just the raw
    estimate. `eligible_count` is the active catalog size driving the sweep term, or
    None when it cannot be read yet (a pending migration). `pending`, when set, is
    `(current_version, target_version)`: the schema is behind the code, so the
    estimate omits the sweep term and the user is told to apply the migration via a
    real run (which backs up first)."""
    total_queries = len(SEARCH_QUERIES)
    # When the count is unknown (pending migration), estimate with sweep=0 and label
    # the sweep line n/a, rather than guessing a catalog size.
    est = estimate_discover_units(eligible_count if eligible_count is not None else 0)

    print("DRY RUN — no API calls will be made\n", file=sys.stderr)
    if pending is not None:
        print(f"⚠ Schema migration pending v{pending[0]} → v{pending[1]}: apply it "
              f"via a real (non --dry-run) pipeline run, which backs up the seed "
              f"first. The sweep term is omitted below until then.\n", file=sys.stderr)
    for i, entry in enumerate(SEARCH_QUERIES, 1):
        print(f'  [{i}/{total_queries}] "{entry["q"]}" ({entry["bucket"]})', file=sys.stderr)
    print(f"\nEstimated quota cost: ~{est['total']} units", file=sys.stderr)
    print(f"  search.list: {total_queries} × {SEARCH_QUOTA_COST} = {est['search']}", file=sys.stderr)
    print(f"  videos.list: {total_queries} × {VIDEOS_QUOTA_COST} = {est['videos']}", file=sys.stderr)
    print(f"  channels.list: ~{est['channels']}", file=sys.stderr)
    print(f"  commentThreads.list: ~{est['comments']}", file=sys.stderr)
    if eligible_count is None:
        print("  videos.list (sweep): n/a (schema migration pending)", file=sys.stderr)
    else:
        print(f"  videos.list (sweep): ceil({eligible_count}/{CHANNEL_BATCH_SIZE}) × "
              f"{VIDEOS_QUOTA_COST} = {est['sweep']}", file=sys.stderr)
    print(f"\nToday (Pacific): {units_today} units used, cap {cap} "
          f"(limit {DAILY_QUOTA_LIMIT} − buffer {SAFETY_BUFFER}), "
          f"{max(cap - units_today, 0)} remaining", file=sys.stderr)
    fits = "yes" if units_today + est["total"] <= cap else "NO — would pre-flight stop"
    print(f"Discover fits under cap today? {fits}", file=sys.stderr)
    print(f"Daily limit: {DAILY_QUOTA_LIMIT:,} units → ~{DAILY_QUOTA_LIMIT // est['total']} runs/day", file=sys.stderr)


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
    # Run-level tuning accumulators (only fresh-fetched queries contribute).
    agg_qualifying: list[int] = []
    agg_lang: dict[str, int] = {}
    agg_untagged = 0
    agg_details = 0

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
              f'kid_keyword {drops["kid_keyword"]}, '
              f'missing_duration {drops["missing_duration"]}, '
              f'missing_views {drops["missing_views"]}', file=sys.stderr)
        print(f'       (views-only check: {below}/{len(details)} below '
              f'MIN_VIEWS={MIN_VIEWS}; {hidden} hidden/absent)', file=sys.stderr)

        # Tuning diagnostics (read-only): where qualifying view counts land, and
        # what the language gate is actually dropping. Accumulate for the run-level
        # summary printed after the loop.
        qualifying = qualifying_view_counts(details, MIN_VIEWS, SHORT_MAX_SECONDS, cutoff_dt)
        near_miss = [c for c in qualifying if c < MIN_VIEWS]
        lang_tally = language_drop_values(details, MIN_VIEWS, SHORT_MAX_SECONDS, cutoff_dt)
        untagged = untagged_audio_language_count(details)
        agg_qualifying.extend(qualifying)
        for k, v in lang_tally.items():
            agg_lang[k] = agg_lang.get(k, 0) + v
        agg_untagged += untagged
        agg_details += len(details)

        print(f'       near-miss view counts: {_fmt_counts(near_miss)}', file=sys.stderr)
        print(f'       lang-drop values -> {_fmt_tally(lang_tally)}', file=sys.stderr)
        print(f'       untagged (blank, passed gate): {untagged} of {len(details)}',
              file=sys.stderr)

    if agg_details:
        dist = distribution_buckets(agg_qualifying)
        print("\nqualifying-view distribution (all queries): "
              + " | ".join(f"{k}: {dist[k]}" for k in DISTRIBUTION_BUCKETS),
              file=sys.stderr)
        print(f"lang-drop values (all queries): {_fmt_tally(agg_lang)}; "
              f"untagged: {agg_untagged} of {agg_details}", file=sys.stderr)

    return all_results, quota_aborted


def _run_category_phase(youtube, conn, budget: "QuotaBudget", region: str,
                        now: str) -> None:
    """Sync the `categories` reference table from videoCategories.list for `region`.
    Runs once per invocation in every mode (1 quota unit). Non-fatal: categories
    are reference data, so a fetch/persist failure is warned and the run
    continues, consistent with the channel/snapshot writes in _persist_all.

    Pre-checks headroom and skips quietly when the cap is reached: this optional
    refresh must NOT trip budget.guard_stopped (which signals that *core* work was
    cut short and drives the run's terminal status)."""
    if not budget.can_afford(CATEGORIES_QUOTA_COST):
        print("Skipping category sync: no quota headroom under the cap.", file=sys.stderr)
        return
    try:
        records = fetch_video_categories(youtube, region, budget)
        if not records:  # quota abort or empty response — nothing to persist
            return
        db.run_with_db_retry(lambda: persist_categories(conn, records, now))
        print(f"Synced {len(records)} video categories (region {region})", file=sys.stderr)
    except PipelineApiError:
        # A systemic API failure (network/auth/quota) is terminal: let it propagate
        # so main() classifies it into an exit code. Only non-API hiccups (a persist
        # error, a malformed reference response) are the non-fatal "continue" case.
        raise
    except Exception as e:
        print(f"WARNING: category sync failed, continuing: {e}", file=sys.stderr)


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
        # v14: capture channel subs on the snapshot row (None when unknown).
        snapshot_args.append(
            (vid, record["view_count"], record["like_count"],
             record["comment_count"], ch_info.get("subscriber_count"))
        )

    channel_records = [build_channel_record(cid, info) for cid, info in channel_map.items()]
    return video_records, snapshot_args, channel_records


# --- Phase A.5: English-only + no-kids LLM classification -------------------
# Runs after dedupe and BEFORE the channel/comment phases, so a rejected video never
# incurs their quota. The verdict is a binary the YouTube API cannot express. The core
# logic lives in classify.py (DB/llm-free); here we BIND the real llm.generate + db
# logging and orchestrate preflight + per-video failover, mirroring how
# price_refresh.daily binds extraction.

# Best-effort fixed seed (low temperature does the real variance reduction). NOTE: it
# buys NO reproducibility on the reasoning fallback (nano's reasoning is on and
# OpenAI's seed is deprecated) — only the gemini primary's verdicts are diffable across
# runs. Do not treat nano classify output as deterministic.
CLASSIFY_SEED = 7

# Scope under which each classification call is logged to llm_invocations, so its cost
# (including reasoning-token spend on the fallback) flows into the spend panel.
_CLASSIFY_SCOPE = "video_classification"

# A trivial preflight prompt: we only care that the provider answers without an
# LLMError (key present, provider reachable), not that the body parses.
_PREFLIGHT_PROMPT = (
    'Reply with this exact JSON and nothing else: '
    '{"english": true, "kids_targeted": false, "kids_subject": false, '
    '"reason": "preflight"}'
)


def _make_classify_generate(conn, model: str, run_date: str):
    """Return a `generate_fn(prompt) -> GenerateResult` bound to `model`, reading its
    capability flags + the model-aware output cap from the registry ONCE and logging
    each call to llm_invocations. The cap comes from config.default_max_tokens so a
    reasoning fallback (nano) gets the generous ceiling its hidden reasoning needs
    instead of being starved. Provider failures propagate as llm.LLMError (the phase
    owns retry/failover). An unknown model raises ValueError."""
    flags = db.fetch_model(conn, model)
    if flags is None:
        raise ValueError(f"unknown classification model {model!r}")
    provider, _ = llm.split_model(model)
    max_tokens = config.default_max_tokens(provider, bool(flags["is_reasoning"]))

    def generate_fn(prompt: str):
        start = time.monotonic()
        result = llm.generate(
            model, prompt, temperature=0.0, seed=CLASSIFY_SEED,
            supports_temperature=bool(flags["supports_temperature"]),
            supports_seed=bool(flags["supports_seed"]),
            is_reasoning=bool(flags["is_reasoning"]),
            max_tokens=max_tokens)
        duration_ms = int((time.monotonic() - start) * 1000)
        now = now_local_iso()

        def _log() -> None:
            with db.transaction(conn):
                db.log_invocation(
                    conn, run_date, _CLASSIFY_SCOPE, model, 0.0,
                    result.seed_applied, None, result.input_tokens,
                    result.output_tokens, now, duration_ms=duration_ms)

        db.run_with_db_retry(_log)
        return result

    return generate_fn


def _preflight_classifiers(models: list[str], generate_fns: dict) -> set[str]:
    """Probe each model's provider with a trivial call; return the set of models whose
    provider answered (no llm.LLMError). A failure is warned, not fatal — the other
    model may still be alive."""
    alive: set[str] = set()
    for model in models:
        try:
            generate_fns[model](_PREFLIGHT_PROMPT)
            alive.add(model)
        except llm.LLMError as e:
            print(f"Classification preflight: {model} unavailable ({e})",
                  file=sys.stderr)
    return alive


def _retry_backoff_seconds(exc, attempt: int, retry_cfg: dict) -> float:
    """How long to wait before the next retry of a failed primary/fallback attempt.
    A 429 RESOURCE_EXHAUSTED honors the provider's retryDelay when present (read off
    the LLMError, never hardcoded), capped at max_backoff_seconds; a 429 without a
    hint and a 503 UNAVAILABLE use exponential backoff base * 2**attempt (also
    capped). Any other failure (a parse glitch / unclassified provider error) gets no
    wait: it is not a throttle, so an immediate retry is correct. `attempt` is the
    zero-based index of the attempt that just failed."""
    status = getattr(exc, "status_code", None)
    cap = retry_cfg["max_backoff_seconds"]
    if status == 429:
        retry_after = getattr(exc, "retry_after_seconds", None)
        if retry_after is not None:
            return min(retry_after, cap)
        return min(retry_cfg["base_backoff_seconds"] * (2 ** attempt), cap)
    if status == 503:
        return min(retry_cfg["base_backoff_seconds"] * (2 ** attempt), cap)
    return 0.0


def _attempt_model(video: dict, gen, retry_cfg: dict) -> tuple:
    """Run one model over `video` with bounded retries, returning
    (Verdict, was_429) on success or (None, was_429) once retries are exhausted.
    Attempts = 1 + retry_cfg['max_retries'] (so max_retries=1 => 2 attempts). 429/503
    and a ClassificationError are all retried (the phase owns the policy); the sleep
    between attempts is _retry_backoff_seconds. `was_429` reports whether the FINAL
    failure was a 429 RESOURCE_EXHAUSTED, so the caller can make stickiness depend on
    the durable signal alone."""
    max_retries = retry_cfg["max_retries"]
    was_429 = False
    last = None
    for attempt in range(max_retries + 1):
        try:
            return classify.classify_video(video, generate_fn=gen), was_429
        except (llm.LLMError, classify.ClassificationError) as e:
            last = e
            was_429 = getattr(e, "status_code", None) == 429
            if attempt < max_retries:
                delay = _retry_backoff_seconds(e, attempt, retry_cfg)
                if delay > 0:
                    time.sleep(delay)
    print(f"  classify: model failed for {video.get('id')} after "
          f"{max_retries + 1} attempts: {last}", file=sys.stderr)
    return None, was_429


def _classify_one(video: dict, model_order: list[str], generate_fns: dict,
                  runtime: dict, retry_cfg: dict):
    """Classify `video`: try the in-use (primary) model with bounded retries, then
    fail over to the fallback (also with bounded retries) before giving up. Returns
    (Verdict, model_used), or (None, None) when every attempt fails OR the fallback
    spend breaker trips (signaled via runtime['budget_exhausted']).

    Stickiness is gated on the DURABLE signal only: when the primary exhausts its
    retries on a 429 (free-tier quota gone for the window), runtime['primary_exhausted']
    is set so every subsequent video skips the primary and fails straight over. A 503
    or a parse glitch fails over THIS video only and leaves the primary live. The
    breaker checks runtime['fallback_count'] BEFORE spending on the fallback, so the
    tripping video never incurs a wasted paid call."""
    in_use = model_order[0]
    fallback = model_order[1] if len(model_order) > 1 else None

    if not runtime["primary_exhausted"]:
        verdict, was_429 = _attempt_model(video, generate_fns[in_use], retry_cfg)
        if verdict is not None:
            return verdict, in_use
        # in_use exhausted. Only a 429 (with a fallback to go to) condemns the run.
        if was_429 and fallback is not None and not runtime["primary_exhausted"]:
            runtime["primary_exhausted"] = True
            print(f"  classify: primary {in_use} throttled (429); failing over to "
                  f"{fallback} for remaining videos", file=sys.stderr)

    if fallback is None:
        return None, None

    # Fallback spend breaker: stop BEFORE the call that would exceed the per-run cap.
    if runtime["fallback_count"] >= retry_cfg["max_fallback_videos"]:
        runtime["budget_exhausted"] = True
        return None, None
    runtime["fallback_count"] += 1
    verdict, _ = _attempt_model(video, generate_fns[fallback], retry_cfg)
    if verdict is not None:
        return verdict, fallback
    return None, None


def _classify_survivors(state: dict, seen_videos: dict, query_map: dict,
                        in_use: str, model_order: list[str], generate_fns: dict
                        ) -> tuple:
    """Classify every survivor (skipping cache hits stamped with the current in-use
    model + prompt version), prune `seen_videos`/`query_map` of drops in place, and
    return (tally, budget_exhausted). Fail-closed: a video whose every attempt errors
    is dropped and counted under classify_error. A mid-run mass failure (>half of
    THIS-run attempts, once at least 4 were attempted) raises ClassificationUnavailable:
    a provider degraded after preflight, and silently emptying the board is wrong.

    Retry/failover policy is per-video and run-scoped via `runtime`: bounded retries
    honoring 429 retryDelay / 503 backoff, sticky failover ONLY on a 429 primary
    exhaust, and the per-run fallback spend breaker. When the breaker trips,
    budget_exhausted is returned True and `seen_videos`/`query_map` are pruned down to
    ONLY the classified-and-kept survivors (the unprocessed remainder is dropped so no
    unclassified video reaches the board), for the caller's single board write."""
    cache = state.setdefault("classification", {})
    retry_cfg = config.CLASSIFICATION_RETRY
    runtime = {"primary_exhausted": False, "fallback_count": 0,
               "budget_exhausted": False}
    tally = {"kept": 0, "not_english": 0, "kids": 0, "classify_error": 0}
    tokens: dict[str, list[int]] = {}            # model -> [input, output]
    attempts = errors = 0
    dropped: list[str] = []
    budget_tripped = False
    survivors = list(seen_videos.items())
    for idx, (vid, video) in enumerate(survivors, 1):
        cached = cache.get(vid)
        if not (cached and cached.get("model") == in_use
                and cached.get("prompt_version") == classify.CLASSIFY_PROMPT_VERSION):
            verdict, model_used = _classify_one(
                video, model_order, generate_fns, runtime, retry_cfg)
            if runtime["budget_exhausted"]:
                # Breaker tripped BEFORE any fallback spend on this video: stop here
                # and persist only what was already classified-and-kept.
                budget_tripped = True
                break
            attempts += 1
            if verdict is None:
                errors += 1
                cached = {"english": False, "kids_targeted": False,
                          "kids_subject": False, "reason": "classify_error",
                          "model": in_use,
                          "prompt_version": classify.CLASSIFY_PROMPT_VERSION,
                          "keep": False, "error": True}
            else:
                tok = tokens.setdefault(model_used, [0, 0])
                tok[0] += verdict.input_tokens
                tok[1] += verdict.output_tokens
                cached = {"english": verdict.english,
                          "kids_targeted": verdict.kids_targeted,
                          "kids_subject": verdict.kids_subject,
                          "reason": verdict.reason, "model": model_used,
                          "prompt_version": classify.CLASSIFY_PROMPT_VERSION,
                          "keep": verdict.keep, "error": False}
            cache[vid] = cached
            if idx % 10 == 0:
                save_state(state)
            # Mass-failure guard, counted over THIS run's attempts only (cache hits
            # excluded): a provider that died after preflight should abort, not quietly
            # empty the board.
            if attempts >= 4 and errors * 2 > attempts:
                save_state(state)
                raise classify.ClassificationUnavailable(
                    f"classification failing for most videos ({errors}/{attempts} "
                    "attempts); aborting rather than emptying the board")

        if cached["keep"]:
            tally["kept"] += 1
        else:
            dropped.append(vid)
            if cached.get("error"):
                tally["classify_error"] += 1
            elif not cached["english"]:
                tally["not_english"] += 1
            else:
                tally["kids"] += 1

    if budget_tripped:
        # Prune to ONLY classified-and-kept survivors: this sweeps out both the drops
        # and the unprocessed remainder in one pass, so the caller's single board write
        # never persists an unclassified video.
        kept_ids = {v for v in seen_videos if cache.get(v, {}).get("keep")}
        for vid in list(seen_videos):
            if vid not in kept_ids:
                seen_videos.pop(vid, None)
                query_map.pop(vid, None)
        print(f"  fallback budget ({retry_cfg['max_fallback_videos']}) exceeded after "
              f"primary throttle, persisted {len(kept_ids)} classified, aborting run",
              file=sys.stderr)
    else:
        for vid in dropped:
            seen_videos.pop(vid, None)
            query_map.pop(vid, None)
    save_state(state)

    cost = sum(
        (llm.estimate_cost(m, t[0], t[1], config.PRICES) or 0.0)
        for m, t in tokens.items())
    per_model = ", ".join(
        f"{m} in={t[0]} out={t[1]}" for m, t in tokens.items()) or "no live calls"
    print(f"Classification: kept {tally['kept']}, not_english "
          f"{tally['not_english']}, kids {tally['kids']}, classify_error "
          f"{tally['classify_error']} (in_use={in_use})", file=sys.stderr)
    print(f"  classify cost: ${cost:.4f} ({per_model})", file=sys.stderr)
    return tally, budget_tripped


def _run_classification_phase(conn, state: dict, seen_videos: dict,
                              query_map: dict, run_date: str) -> tuple:
    """Phase A.5: resolve primary + fallback classifier models, warn on any missing
    API key, preflight both providers, and classify all survivors with failover. If
    BOTH providers fail preflight, raise ClassificationUnavailable (we cannot guarantee
    English and must neither leak nor silently empty the board). Returns
    (tally, budget_exhausted) so the caller can map a fallback-spend-breaker trip to
    its own exit code."""
    if not seen_videos:                              # nothing to classify; no LLM calls
        return {"kept": 0, "not_english": 0, "kids": 0, "classify_error": 0}, False
    primary = classify.active_classification_model(conn)
    fallback = classify.active_classification_fallback_model(conn)
    models = [primary, fallback]
    for model in models:
        provider, _ = llm.split_model(model)
        if not llm.api_key_present(provider):
            print(f"WARNING: no API key for classifier provider {provider!r} "
                  f"({model}); failover may be unavailable", file=sys.stderr)

    generate_fns = {m: _make_classify_generate(conn, m, run_date) for m in models}
    print(f"Classifying {len(seen_videos)} videos "
          f"(primary={primary}, fallback={fallback})...", file=sys.stderr)
    alive = _preflight_classifiers(models, generate_fns)
    if not alive:
        raise classify.ClassificationUnavailable(
            f"both classifier providers unavailable ({primary}, {fallback})")

    # In-use = primary when alive, else the fallback. Per-video order tries the in-use
    # provider first, then the other ONLY if it passed preflight (never a known-dead one).
    model_order = [m for m in models if m in alive]
    in_use = model_order[0]
    return _classify_survivors(state, seen_videos, query_map, in_use,
                               model_order, generate_fns)


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
                  today_pac: str, run_date: str,
                  run_started: str) -> tuple[int, bool, bool, bool]:
    """Discover mode (expensive): run the search/channel/comment phases and persist
    the catch (videos + channels), then hand off to the shared catalog sweep, which
    snapshots and ranks the WHOLE active catalog (now including the just-caught
    videos). Discover writes no snapshot or ranking of its own — the sweep is the
    single authoritative pass, so one daily run keeps the full back-catalog's
    trajectory current with no double rows. `run_date` is the Eastern rankings key
    captured once in main() and threaded through. Returns
    (videos_seen, quota_aborted, persist_partial, classify_budget_exhausted)."""
    all_results, quota_aborted = _run_search_phase(youtube, state, budget, conn, today_pac)

    seen_videos, query_map = dedupe_videos(all_results)
    print(f"\n{len(seen_videos)} unique videos after dedup", file=sys.stderr)

    # Phase A.5: drop non-English and kids content BEFORE spending channel/comment
    # quota on them. Skipped entirely when disabled (cheap gates still applied above).
    classify_budget_exhausted = False
    if config.CLASSIFICATION_ENABLED:
        _, classify_budget_exhausted = _run_classification_phase(
            conn, state, seen_videos, query_map, run_date)
        print(f"{len(seen_videos)} videos after classification", file=sys.stderr)

    # The fallback spend breaker aborts the run: skip the (quota-spending) channel and
    # comment phases entirely and persist only the survivors classified so far. The
    # board write below tolerates an empty channel_map (records default via .get()).
    if not classify_budget_exhausted:
        channel_map = _run_channel_phase(youtube, state, seen_videos, budget)
        _run_comment_phase(youtube, state, seen_videos, budget)
    else:
        channel_map = {}

    now = now_local_iso()
    # Discover persists NO snapshot of its own (empty snapshot_args): the sweep
    # below is the authoritative snapshot pass, so a caught video gets exactly one
    # stats_snapshots row, never a double. persist_videos commits its own
    # transaction here, so the catch is durable BEFORE the sweep runs.
    video_records, _snapshot_args, channel_records = _build_records(
        state, seen_videos, query_map, channel_map
    )
    persist_partial = _persist_all(
        conn, run_id, video_records, channel_records, [], now
    )

    # Hand off to the shared catalog sweep: it snapshots and ranks the whole active
    # catalog (which already includes the just-persisted catch) once. A sweep
    # failure is NON-FATAL — the catch is already committed, so mark the run partial
    # and continue rather than failing it (a missed snapshot day is recoverable; a
    # lost catch is not). Runs unconditionally, including when the classify breaker
    # tripped: the snapshot value is independent of LLM classification, the YouTube
    # budget guard still protects every videos.list, and _pick_status keeps the
    # classify_budget_stop label.
    try:
        _, sweep_partial = _run_catalog_sweep(
            youtube, conn, run_id, budget, now, run_date,
            mode="discover", run_started=run_started,
        )
        persist_partial = persist_partial or sweep_partial
    except Exception as e:
        persist_partial = True
        print(f"WARNING: catalog sweep failed, continuing: {e}", file=sys.stderr)

    return len(seen_videos), quota_aborted, persist_partial, classify_budget_exhausted


def _discover_done_today(conn, today_pac: str) -> bool:
    """Thin seam over db.discover_ran_today — the SINGLE source of the once-a-day
    cap, shared with the dashboard's /api/run-state (the dashboard can't import
    swipefile, so the logic lives in db). True when a discover with status
    success/partial completed today (Pacific)."""
    return db.discover_ran_today(conn, today_pac)


def _pick_status(budget: "QuotaBudget", quota_aborted: bool,
                 persist_partial: bool, downgraded: bool,
                 classify_budget_exhausted: bool = False) -> str:
    """Resolve the terminal run_log status. Order matters: a proactive guard stop
    and a reactive quota stop are the most urgent signals; a downgrade is recorded
    distinctly (never as a plain success) only on an otherwise-clean run. The classify
    fallback-spend breaker (classify_budget_stop) is ranked AFTER the two YouTube quota
    stops so that, when both fire in one run, the not-retryable-today daily blocker
    wins the label; it sits ABOVE persist_partial because it aborted the run."""
    if budget.guard_stopped:
        return "quota_guard_stop"
    if quota_aborted:
        return "quota_exceeded"
    if classify_budget_exhausted:
        return "classify_budget_stop"
    if persist_partial:
        return "partial"
    if downgraded:
        return "discover_downgraded_to_refresh"
    return "success"


def _finalize(code: int) -> int:
    """Print the single grep-able result line and return the exit code. The
    __main__ guard does sys.exit(main()); main() is the sole code-decider, this is
    just the one place every path funnels through to log + return."""
    reason = _EXIT_REASON.get(code, "other")
    print(f"PIPELINE_RESULT: code={code} reason={reason}", file=sys.stderr)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube Habits Swipe File Builder")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true",
                            help="Print planned queries and estimated quota cost without making API calls")
    mode_group.add_argument("--discover", action="store_true",
                            help="Force a discovery run (expensive: searches + enrichment)")
    mode_group.add_argument("--refresh", action="store_true",
                            help="Force a cheap stats refresh of tracked videos (no searches)")
    try:
        args = parser.parse_args()
    except SystemExit as e:
        # argparse exits 0 on --help (let it pass), 2 on a usage error. Remap the
        # usage error to EXIT_OTHER so a bad flag is never read as EXIT_NETWORK(2).
        return _finalize(EXIT_OK if e.code in (0, None) else EXIT_OTHER)

    cap = DAILY_QUOTA_LIMIT - SAFETY_BUFFER

    if args.dry_run:
        # Terminal branch: no API calls and NO DB writes. The DB is opened
        # READ-ONLY (mode=ro) and init_db is deliberately NOT called, so a pending
        # schema migration cannot be applied here — SQLite physically refuses the
        # write rather than us merely promising not to. Go STRAIGHT to the finalizer
        # as EXIT_OK; it must NOT fall through to the clean-run classification below
        # (that would count 0 rankings rows and false-fire EXIT_NO_ROWS).
        units_today = 0
        eligible = 0          # int -> printed sweep term; None -> "n/a (pending)"
        pending = None
        if not os.path.exists(DB_PATH):
            print("DRY RUN: no database yet (a real run will create it).",
                  file=sys.stderr)
        else:
            conn = db.get_readonly_connection(DB_PATH)
            try:
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                units_today = db.get_units_used(conn, pacific_date())
                if current < db.SCHEMA_VERSION:
                    # Schema is behind the code: the status column the eligible
                    # count needs may not exist yet, so skip it and report.
                    pending = (current, db.SCHEMA_VERSION)
                    eligible = None
                else:
                    eligible = db.count_eligible_for_refresh(conn)
            finally:
                conn.close()
        _print_dry_run(units_today, cap, eligible, pending)
        return _finalize(EXIT_OK)

    load_dotenv()
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        print("ERROR: YOUTUBE_API_KEY not found in .env", file=sys.stderr)
        return _finalize(EXIT_AUTH)

    try:
        validate_config()
    except ConfigError as e:
        print(f"ERROR: invalid config: {e}", file=sys.stderr)
        return _finalize(EXIT_OTHER)

    # Single run_date capture (Eastern, once). Threaded to BOTH the rankings write
    # and the NO_ROWS count so a midnight-straddling run never stamps rows under one
    # date while counting under another. now_local_iso() is the same Eastern helper
    # the rows were always stamped from; only the capture moment moves to run start.
    run_started = now_local_iso()
    run_date = run_started[:10]

    conn = None
    run_id = None
    mode = None
    today_pac = pacific_date()
    videos_seen = 0
    quota_aborted = False
    persist_partial = False
    downgraded = False
    status = "failed"
    code = EXIT_OTHER

    try:
        db.init_db(DB_PATH)
        # get_connection is inside the try so a "database is locked" at open time
        # classifies as EXIT_DB_LOCKED rather than crashing as a generic error.
        conn = db.get_connection(DB_PATH)

        units_today = db.get_units_used(conn, today_pac)

        # Resolve the run mode. ONE discover per Pacific day is the cap, enforced
        # HERE (not in the UI) so the CLI, a scheduler, and the dashboard button all
        # honor it. Once a discover has completed today (Pacific), even an explicit
        # --discover resolves to a refresh — which routes to _run_catalog_sweep and
        # never enters _run_discover, so zero search/classify quota is spent. Only a
        # new Pacific day clears it.
        if args.refresh:
            mode = "refresh"
        elif _discover_done_today(conn, today_pac):
            mode = "refresh"
            print("Discover already ran today (Pacific); running stats refresh only "
                  "— no new catch, no classify spend.", file=sys.stderr)
        else:
            mode = "discover"

        # Pre-flight guard (discover only): refuse if the estimate won't fit the cap.
        # The sweep term uses the PRE-catch active count (read here, before start_run
        # and the catch persist), so it undercounts by ~one batch — accepted.
        if mode == "discover":
            estimate = estimate_discover_units(
                db.count_eligible_for_refresh(conn))["total"]
            if units_today + estimate > cap:
                remaining = max(cap - units_today, 0)
                if args.discover:  # explicit discover -> hard stop, no work done
                    run_id = db.start_run(conn, "discover", run_started)
                    db.finish_run(conn, run_id, now_local_iso(), 0, 0,
                                  "quota_preflight_stop")
                    print(f"PRE-FLIGHT STOP: discover needs ~{estimate} units but only "
                          f"{remaining} remain under the cap ({cap}). No API calls made.",
                          file=sys.stderr)
                    return _finalize(EXIT_QUOTA)
                # no-flag default -> loud downgrade to a cheap refresh
                print(f"DOWNGRADE: discover needs ~{estimate} units but only {remaining} "
                      f"remain under the cap ({cap}); running a refresh instead.",
                      file=sys.stderr)
                mode, downgraded = "refresh", True

        run_id = db.start_run(conn, mode, run_started)
        youtube = build_youtube_client(api_key)
        budget = QuotaBudget(units_today, cap)
        # state.json is the discover resume cache; refresh neither reads nor clears it.
        state = _normalize_state(load_state()) if mode == "discover" else {}

        try:
            # Reference-data sync runs once per invocation, before either mode, so the
            # categories table stays current and its 1-unit charge is flushed by the
            # finally block below like every other call.
            _run_category_phase(youtube, conn, budget, CATEGORY_REGION, now_local_iso())

            classify_budget_exhausted = False
            if mode == "discover":
                (videos_seen, quota_aborted, persist_partial,
                 classify_budget_exhausted) = _run_discover(
                    youtube, conn, run_id, budget, state, today_pac, run_date,
                    run_started
                )
            else:
                videos_seen, persist_partial = _run_catalog_sweep(
                    youtube, conn, run_id, budget, now_local_iso(), run_date,
                    mode="refresh", run_started=run_started,
                )

            status = _pick_status(budget, quota_aborted, persist_partial, downgraded,
                                  classify_budget_exhausted)
            # Only a clean discover clears the resume cache; an aborted/partial discover
            # keeps it, and refresh never touches it.
            if mode == "discover" and status == "success":
                clear_state()

            print(f"\n[{mode}] processed {videos_seen} videos to {DB_PATH} "
                  f"(run {run_id}, status: {status})", file=sys.stderr)
            print(f"Quota consumed this run: {budget.run_units} units "
                  f"({units_today + budget.run_units}/{cap} used today)", file=sys.stderr)
        finally:
            # Durably record the run even on abort: on a raise, status stays "failed"
            # so run_log never falsely reads "success". Each cleanup step swallows its
            # own error so a failing finally cannot replace the already-decided exit
            # code (worst case: a still-held DB lock making finish_run raise).
            try:
                if budget.unflushed() > 0:
                    # Losing the last quota increment on a DB-locked abort is
                    # DELIBERATE: eager per-call flushing already persisted the rest,
                    # and a locked DB could not write this remainder anyway.
                    db.run_with_db_retry(lambda: db.add_quota_units(
                        conn, today_pac, budget.unflushed(), now_local_iso()))
            except Exception as e:
                print(f"WARNING: final quota flush failed (ignored): {e}",
                      file=sys.stderr)
            try:
                db.finish_run(conn, run_id, now_local_iso(), budget.run_units,
                              videos_seen, status)
            except Exception as e:
                print(f"WARNING: finish_run failed (ignored): {e}", file=sys.stderr)

        # Clean (no-exception) classification. Order: YouTube quota stop first (so a
        # both-fire run reports quota, not the breaker), then the classify fallback-spend
        # breaker, then the discover-only no-rows check, else success.
        if status in QUOTA_STOP_STATUSES:
            code = EXIT_QUOTA
        elif status == "classify_budget_stop":
            code = EXIT_CLASSIFY_BUDGET
        elif mode == "discover" and \
                db.count_rankings_for_run_date(conn, run_date) == 0:  # resolved mode, not args.discover
            code = EXIT_NO_ROWS
        else:
            code = EXIT_OK

    except PipelineApiError as e:
        code = exit_code_for_api_failure(e.reason, e.status)
    except sqlite3.OperationalError as e:
        code = EXIT_DB_LOCKED if "database is locked" in str(e).lower() else EXIT_OTHER
        print(f"ERROR: database error: {e}", file=sys.stderr)
    except Exception:
        traceback.print_exc()
        code = EXIT_OTHER
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return _finalize(code)


if __name__ == "__main__":
    sys.exit(main())
