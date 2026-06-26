# Dashboard Visualization Ideas — categorizing the swipefile data

## Context

`data/database/swipefile.db` holds the daily-intel swipefile: short-form YouTube
videos discovered in the @maryjhabits longevity/habits niche, ranked daily. This
doc analyzes what is actually in the data, enumerates every way it can be
categorized, and proposes a catalog of dashboard visualizations spanning those
categories (habit/health lanes, protein/zone-2 sub-niches, and more).

This is a **proposal catalog** — no code. Each entry says what it shows, which
real columns back it, the chart type, whether an endpoint already exists, and
whether it is new vs. already on the dashboard. After review it is trimmed to
**nine** views. Priority lenses (lead with these): **sub-niche mix**,
**breakout signal**, **lane & category mix**.

All figures below are from a live read-only query of the seed DB on 2026-06-22
(`sqlite3 "file:...swipefile.db?immutable=1"`), so the catalog is grounded, not
hypothetical.

## What the data actually is

- **313 videos** (all `is_short=1`), **235 channels**, **32 YouTube categories**,
  **516 rankings** across **10 daily runs** (2026-06-08 → 06-18), **484
  stats_snapshots** (a real per-video time-series), **8 LLM interpretations**.
- Window is recent + short: published 06-05 → 06-18, all Shorts (`hd`).

> **Kid-exclusion (applies to every chart below).** `made_for_kids = 0` for all
> 313 rows, so YouTube's flag is useless. A title-keyword filter (brush teeth,
> wakey wakey, for kids, baby, toddler, nursery, …) removes **15** kid/parenting
> videos as out-of-niche, leaving **298 videos / 225 channels**. The per-chart
> numbers in the mockups use that 298 basis, so they differ slightly from the raw
> DB counts in the table just below (which describe the unfiltered seed).

### Every categorization dimension present in the data (raw DB, unfiltered)

| Dimension | Column(s) | Real values (counts) |
|---|---|---|
| **Lane / bucket** | `rankings.bucket`, `videos.buckets`, `interpretations.scope` | health (240 vids / 200 rank rows), habit (72 / 116), overall (200 rank rows); 1 video is `habit\|health` |
| **Sub-niche** | `videos.matched_queries` (pipe-delim) | high protein (168), sleep routine (42), build habits (39), morning routine habits (25), longevity habits (23), strength training over 50 (6), break bad habits (5), habit stacking (2), atomic habits (2), VO2 max (2), habit tracker (1) |
| **YouTube category** | `videos.category_id` → `categories.title` | People & Blogs (209), Education (43), Howto & Style (43), Sports (8), Science & Tech (3), Comedy (2)… |
| **Topic categories** | `videos.topic_categories` (pipe-delim, Freebase) | Food, Health, Lifestyle (sociology), Physical fitness, Religion |
| **Language** | `videos.audio_language` | en (147), untagged (119), en-US (23), en-IN (19), en-GB (3), en-CA/AU (1 each) |
| **Channel / creator** | `videos.channel_*`, `channels.*` | 235 channels; top recurring: Yum Recipes Hut / We Are Recipes (8 each); country US 59, IN 30, GB 6 |
| **Engagement** | `view_count`, `views_to_subs_ratio`, `views_per_day`, `like_count`, `comment_count` | views 10k–1.2M (avg 59k); ratio 0.01–3175× (avg 49×); v/day 5k–607k |
| **Breakout bands** | `views_to_subs_ratio` | <1× (155), 1–10× (103), 10–100× (40), **100×+ (15)** |
| **Time / recency** | `published_at`, `first_seen_at`, `rankings.run_date`, `stats_snapshots.captured_at` | 10 run_dates; per-video momentum series |
| **Persistence** | derived from `rankings` | 143 videos ranked 1 day, 58 across 2 days, 19 across 3 (staying power) |
| **Manual swipe layer** | `starred` (5), `user_notes`, `hook`, `first_10_sec`, `saves` | curator annotations |

### What the dashboard already visualizes (avoid duplicating)

- **/board** — ranked leaderboard table (run + lane scoped).
- **/trends** — bump chart (rank over time), view-count **histogram**, dual-axis
  **trajectory** (views + velocity for tracked videos).
- **/interpretation** — LLM prose synthesis per run + lane.
- **Spend donuts**, /models, /prices, /extract, /documentation (admin/ops).
- Filters already exist for `matched_query` (text), `channel_id`, `country`,
  `duration_band`, view/ratio ranges, dates, starred/has-notes — but these
  **filter** the board; none are **visualized as a breakdown**.

The gap is clear: the data is rich in categorical structure (sub-niche, category,
topic, breakout band, channel), but the only charts are time-series and a single
view-count histogram. Everything below fills that gap.

---

## Proposed visualizations

Legend: **[NEW]** new chart · **(reuse)** existing endpoint/data · **(new API)**
needs a new aggregation endpoint. Chart types assume the existing ECharts 5.5.1
setup (dispose+reinit theme pattern, `charts.js`).

> **Trimmed in review:** #3 (sub-niche performance bubble), #4 (breakout scatter),
> #10 (category × lane heatmap), #12 (language/geography), and #13 (publish-time)
> were dropped to keep the set focused. Survivors keep their original IDs, so the
> numbering has intentional gaps.

### Lens 1 — Sub-niche mix (`matched_queries`)  ★ priority

The "protein / zone 2" level. Today only filterable, never charted.

1. **Sub-niche bar / treemap [NEW] (new API)** — horizontal bar (or treemap) of
   video count per `matched_query`, split by lane color. Instantly answers "what
   topics are we actually capturing?" Data is lopsided and worth surfacing: high
   protein alone is 168 of 298; the whole habit lane's queries are long-tail
   (2–39 each). `matched_queries` is pipe-delimited, so the aggregation must
   explode on `|` (SQL recursive split or Python).

2. **Sub-niche × time stream [NEW] (new API)** — stacked area / multi-line of
   sub-niche share across the 10 run_dates. Shows which topics are rising vs.
   fading (e.g., is "sleep routine" gaining on "high protein"?). Backed by joining
   `rankings.run_date` to each ranked video's `matched_queries`.

### Lens 2 — Breakout signal (`views_to_subs_ratio`)  ★ priority

The brand's core "find the small channel going viral" signal. Ratio reaches
3175×. Currently only a sortable column + range filter.

5. **Breakout-band stacked bar [NEW] (new API)** — count of videos per ratio band
   (<1× / 1–10× / 10–100× / 100×+) stacked by lane. Quick read on how breakout-
   heavy each lane is.

6. **Breakout leaderboard — views ÷ subscribers [NEW] (reuse `/api/library`)** —
   a Top-N "biggest over-performers" card sorted by `views_to_subs_ratio`, with
   the ratio (views divided by subscribers) called out as the hero number so its
   meaning is explicit in the header. The board endpoint already returns ratio;
   this is a re-skinned, ratio-sorted view. Kid/parenting content is filtered out
   (it otherwise dominated this list).

### Lens 3 — Lane & category mix  ★ priority

7. **Lane composition donut/bar [NEW] (new API)** — health vs habit vs both
   (230 / 67 / 1). Tiny but frames the whole library; pairs naturally beside the
   sub-niche bar.

8. **YouTube category breakdown [NEW] (new API)** — bar of `category_id`→title
   counts (People & Blogs 196 dominates; Howto & Style 43, Education 41). Reveals
   that most "habit/health" content is filed under generic People & Blogs — useful
   signal about how YouTube classifies the niche.

9. **Topic-category composition [NEW] (new API)** — `topic_categories` is
   pipe-delimited Freebase tags (Food / Health / Lifestyle / Physical fitness …).
   Explode + count → bar. Shows the Food-vs-Fitness tilt of the swipefile.

### Secondary lenses (lower priority)

11. **Channel concentration [NEW] (new API)** — channels grouped by how many
    videos they contribute (one-off vs recurring), with channel and video counts.
    Flags whether the swipefile is creator-diverse or a few channels dominate.

14. **Engagement-quality view [NEW] (new API)** — **average likes and comments per
    video, by sub-niche** (`matched_queries`), shown as two side-by-side panels on
    independent axes (likes ≈0–2k, comments ≈0–50), bars colored by lane. Uses
    actual per-video counts, not per-1k-views. Separates volume from per-video
    resonance — high protein leads both. Flag sub-niches with n<5 as tiny samples.

### How it could land on the dashboard (for a later build)

A single new **/categories** SPA page is the cleanest home for #1, #5, #7, #8,
#9 (the categorical breakdowns), with the **breakout leaderboard (#6)** and
**engagement (#14)** alongside. Each chart follows the existing `charts.js`
mount/dispose pattern and reads `THEMES`. Most need one new read-only aggregation
endpoint apiece (or a couple of combined ones), e.g.
`/api/breakdown?dimension=subniche` returning
`{buckets:[{label,count,by_lane,median_ratio}], ...}` — mirroring the shape of the
existing `/api/distribution`. The kid filter belongs server-side in that endpoint.

## Out of scope

- Any code, endpoints, or schema changes — this doc is proposal only.
- Re-implementing existing charts (bump / histogram / trajectory / spend).

## How the data grounding was verified

Every count is reproducible read-only against a copy or the immutable handle — no
writes, honoring the WAL safety rules in CLAUDE.md:

- `sqlite3 "file:data/database/swipefile.db?immutable=1" "<query>"` (never bare
  `cp`, never `-readonly` on this WAL seed, never `wal_checkpoint`).
- Spot-check the load-bearing claims before building any chart that depends on
  them: the kid filter (15 excluded → 298), the `matched_queries` explode (high
  protein = 168), the ratio bands, and lane composition (230 / 67 / 1).
- When a chart graduates to implementation, smoke its new endpoint against a
  copy via `DASHBOARD_DB_PATH`, not the live seed.

---

## Mockups (drawn from live data, 2026-06-22 — kids excluded, 298 videos)

ASCII sketches of each surviving proposal using the real values. `[H]` =
health-lane query, `[B]` = habit-lane query. These show shape/scale, not final
styling. An interactive HTML version is at `docs/dashboard-ideas-mockup.html`.

### Lens 1 — Sub-niche mix

**#1 Sub-niche bar** — count per `matched_query` (1 block ≈ 4 videos):

```
high protein           [H] ██████████████████████████████████████████ 168
build habits           [B] ██████████ 39
sleep routine          [H] ████████ 32
longevity habits       [H] ██████ 23
morning routine habits [B] █████ 20
strength training 50+  [H] ██ 6
break bad habits       [B] █ 5
atomic habits          [B] ▏ 2
VO2 max                [H] ▏ 2
habit stacking         [B] ▏ 2
habit tracker          [B] ▏ 1
```
Read: one query (high protein) is 56% of 298; the habit lane is all long-tail.
Coverage is lopsided.

**#2 Sub-niche x time** — ranked videos per run (overall lane), 06-08 → 06-18:

```
protein  10 7  8  9  9  9 10 10  7 10   ▃▁▂▃▃▃▃▃▁▃  steady ~9, dominant
habits    5 7  5  3  3  5  6  5  5  4   ▃▅▃▁▁▃▄▃▃▂  drifts, no clear trend
sleep     2 3  4  4  1  3  1  3  1  3   ▂▃▄▄▁▃▁▃▁▃  thin, volatile
```
Read: protein is a flat ceiling; habit topics wobble; sleep never gains traction.

### Lens 2 — Breakout signal

**#5 Breakout-band stacked bar** (health █ / habit ▓):

```
<1x      ███████████████ ▓▓▓ 149   (health 123 · habit 26)
1-10x    █████████ ▓▓▓▓ 101         (health 73 · habit 28)
10-100x  ████ ▓ 40                  (health 29 · habit 11)
100x+    █ ▏ 9                      (health 6 · habit 3)
```
Read: most videos sit below 10×; the rare 100×+ tier is where the swipe value is.

**#6 Breakout leaderboard — views ÷ subscribers** (hero number = ratio):

```
┌─────────────────────────────────────────────────┬─────────┐
│ Summer special high protein drink                 │  3175×  │  22k views / 7 subs
│ Everyone has a bedtime routine. #funny            │   997×  │  29k / 29
│ Erly is the only thing that wakes me              │   393×  │  28k / 70
│ This Dog's Bedtime Routine is Too Cute! 🐶        │   341×  │  16k / 47
│ High Calories Shake For Gain Muscle Mass          │   211×  │  16k / 75
│ 5 Day High-Protein Meal Prep | 750g Protein       │   180×  │  30k / 165
└─────────────────────────────────────────────────┴─────────┘
```
Read: the × number is views ÷ subscribers — how far each Short outran its channel
size. Kid/parenting content is now filtered out, so real winners surface.

### Lens 3 — Lane & category mix

**#7 Lane composition** (donut):

```
        health 230 (77%)
      ╭────────────────╮
     │██████████████░░░░│   habit 67 (22%)
      ╰────────────────╯    (+1 video in both)
```

**#8 YouTube category** (1 block ≈ 5 videos):

```
People & Blogs       ███████████████████████████████████████ 196
Howto & Style        █████████ 43
Education             ████████ 41
Sports                ██ 8
Science & Technology  ▏ 3
Pets / Comedy          ▏ 2 / 2
```
Read: 2/3 of the niche is filed under generic "People & Blogs" by YouTube.

**#9 Topic composition** (Freebase tags, exploded; 1 block ≈ 6):

```
Lifestyle (sociology) ██████████████████████████████████████ 229
Food                  ████████████████████████████ 170
Health                ██████████████ 81
Physical fitness      ███ 19
Entertainment         █ 7
Society / Religion     ▏ 2 each
```
Read: the swipefile tilts Food-heavy; true "Physical fitness" tagging is thin (19).

### Secondary lenses

**#11 Channel concentration** (1 block ≈ 5 videos; channels in parens):

```
1 video (one-off)  █████████████████████████████████████ 187  (187 ch)
2 videos           █████████ 46    (23 ch)
3-4 videos         ███████ 34      (10 ch)
5+ videos          ██████ 31       (5 ch)
```
Read: long-tail and creator-diverse — 187 of 225 channels appear once. No small
set of creators dominates the swipefile.

**#14 Engagement quality** — avg likes & comments **per video**, by sub-niche
(actual counts, not per-1k; sorted by avg likes; bars colored by lane; `*` = n<5,
tiny sample):

```
sub-niche           lane   avg likes/video   avg comments/video
------------------  -----  ---------------   ------------------
high protein          H         2,038               44.9
morning routine       B         1,149               15.2
VO2 max *             H           880               23.0
sleep routine         H           844               11.4
break bad habits      B           694               11.4
strength 50+          H           517               13.2
atomic habits *       B           483               11.5
build habits          B           318                7.1
longevity habits      H           267                3.9
habit stacking *      B           218                2.5
habit tracker *       B            23                0.0
```
Read: high protein dominates both metrics (2,038 likes / 44.9 comments per video);
morning routine and VO2 max over-index on engagement relative to their size, while
longevity habits — a sizeable niche — engages least. Two panels share the same
sub-niche rows on independent axes so likes and comments are each legible at their
own scale. Separates volume from per-video resonance.
