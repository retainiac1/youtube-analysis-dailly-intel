# Kid/parenting videos to delete from swipefile.db

Generated read-only from `data/database/swipefile.db` on 2026-06-22 via the
immutable handle. These are out-of-niche for the @maryjhabits 35+ longevity brand
(tooth-brushing songs, baby sleep, toddler routines) and were polluting the
breakout leaderboard.

**Review this list.** The Phase-3 delete script targets these exact 15 `video_id`s
(explicit IN-list, not a keyword search), so whatever you approve here is exactly
what gets deleted — nothing more, nothing less.

## The 15 videos

| # | video_id | Title | Channel | Matched on |
|---|---|---|---|---|
| 1 | `LrKTK37MfPk` | Bedtime Routine with my 7 mo and Neice #sahm #girlmom #nightroutine #momlife #baby #dayinmylife | Coco & Cheeks 💞 | baby |
| 2 | `NOr2BF1UUzU` | Does your baby have a bedtime routine with a million tiny rules? #momlife #fpy #baby #cute #kestnest | SnugNest | baby |
| 3 | `25d3vd1dl4w` | Is your baby struggling to sleep comfortably? Baby Sleep Positioner Pillow helps! #Shorts | BabyGrowthDiary | baby |
| 4 | `9ysTNnOpDQM` | Is your baby struggling to sleep comfortably? Baby Sleep Positioner Pillow helps! #Shorts | BabyGrowthDiary | baby |
| 5 | `jpABm4pn9dg` | Is your baby struggling to sleep comfortably? Baby Sleep Positioner Pillow helps! #Shorts | BabyGrowthDiary | baby |
| 6 | `8I5-vNNplW8` | Night Routine with my noisy 7 Month Old #sahm #momlife #realistic #babybedtime #nightroutine #life | Coco & Cheeks 💞 | baby |
| 7 | `ru6hzAV0Fx8` | Why Your Baby Stay Awake All Night And Sleep All Day \| Dr Kinshuki Sharma | Dr. KINSHUKI SHARMA | baby |
| 8 | `k64A1p8J89Y` | 😴✨ Baby Sleep Tips Every Parent Needs 👶💤 Peaceful Night Routine For Happy Babies 🌙 | BabySoft World | baby |
| 9 | `dXtx6Lzl0BI` | Good Morning! Brush Your Teeth Song for Kids \| Healthy Habits for Children | ToonGanta | brush…teeth |
| 10 | `20Gg7zu8IIk` | I Brush My Teeth Every Day! \| Healthy Habits for Kids \| Fun Toddler Learning Song | Good Habits Club | brush…teeth |
| 11 | `a3rW-h0b3ow` | Wakey Wakey! Time to Brush Our Teeth! 🪥 \| Morning Routine for Kids \| Toddler Learning | Jolly Jumbo | brush…teeth |
| 12 | `-8pyOzK7TNc` | 🌞 Wakey Wakey! Time to Brush Your Teeth 🪥 \| Morning Routine for Kids #shorts | Magic Nursery Kids | brush…teeth |
| 13 | `XKTT6ppY87w` | 🌞 Wakey Wakey! Time to Brush Your Teeth 🪥 \| Morning Routine for Kids #shorts | Jolly Jumbo | brush…teeth |
| 14 | `SwpIEEDIAhQ` | A 2020 study on toddlers' sleep and parental stress reported that parents who described higher | Rachael Shepard-Ohta | toddler |
| 15 | `YR7VyxoRO6I` | why the same sleep routine doesn't work for every toddler? | edwinchuahpapa | toddler |

## What the delete will touch

`video_id` appears in only three tables, and no foreign keys are declared, so the
script deletes from all three explicitly.

| Table | Rows removed | Before → After |
|---|---|---|
| `videos` | 15 | 313 → 298 |
| `rankings` | 29 | 516 → 487 |
| `stats_snapshots` | 24 | 484 → 460 |

`channels` is left untouched. After deletion the `rankings` leaderboard will be
re-sequenced so rank is contiguous 1..N within each `(run_date, bucket)` group.

## Not deleted (intentionally)

- `This Dog's Bedtime Routine is Too Cute! 🐶` — a dog, not a kid; stays in.
- `Everyone has a bedtime routine. #funny` — adult content; stays in.

## Delete script (run in DB Browser)

Run the steps **in order**. Each numbered block can be pasted into the *Execute SQL*
tab. Do not skip the backup.

### Step 0 — back up first, on its own (REQUIRED)

`swipefile.db` runs in WAL mode and is the irreplaceable seed. `VACUUM INTO` cannot
run with pending edits, so run this **first, before any of the deletes below, with
no unsaved changes**. It writes one consistent file (no WAL sidecars to worry about).

```sql
VACUUM INTO 'swipefile-backup-2026-06-23.db';
```

Then verify the backup before continuing (open it, or trust DB Browser's success
message): it should report `ok` and 313 videos.

### Step 1 — pre-check (read-only, deletes nothing)

Confirms exactly 15 target rows are present before you change anything.

```sql
SELECT COUNT(*) AS kid_videos_found
FROM videos
WHERE video_id IN (
  'LrKTK37MfPk','NOr2BF1UUzU','25d3vd1dl4w','9ysTNnOpDQM','jpABm4pn9dg',
  '8I5-vNNplW8','ru6hzAV0Fx8','k64A1p8J89Y','dXtx6Lzl0BI','20Gg7zu8IIk',
  'a3rW-h0b3ow','-8pyOzK7TNc','XKTT6ppY87w','SwpIEEDIAhQ','YR7VyxoRO6I'
);  -- expect 15
```

### Step 2 — delete + re-sequence ranks

Paste this whole block and run it. **Do not click _Write Changes_ yet** — run
Step 3's checks first; DB Browser keeps everything pending so it's all one atomic
unit (no explicit `BEGIN`/`COMMIT` needed, and that avoids DB Browser's
nested-transaction error).

```sql
-- a) remove the kid videos from all three tables that reference video_id
DELETE FROM stats_snapshots WHERE video_id IN (
  'LrKTK37MfPk','NOr2BF1UUzU','25d3vd1dl4w','9ysTNnOpDQM','jpABm4pn9dg',
  '8I5-vNNplW8','ru6hzAV0Fx8','k64A1p8J89Y','dXtx6Lzl0BI','20Gg7zu8IIk',
  'a3rW-h0b3ow','-8pyOzK7TNc','XKTT6ppY87w','SwpIEEDIAhQ','YR7VyxoRO6I'
);
DELETE FROM rankings WHERE video_id IN (
  'LrKTK37MfPk','NOr2BF1UUzU','25d3vd1dl4w','9ysTNnOpDQM','jpABm4pn9dg',
  '8I5-vNNplW8','ru6hzAV0Fx8','k64A1p8J89Y','dXtx6Lzl0BI','20Gg7zu8IIk',
  'a3rW-h0b3ow','-8pyOzK7TNc','XKTT6ppY87w','SwpIEEDIAhQ','YR7VyxoRO6I'
);
DELETE FROM videos WHERE video_id IN (
  'LrKTK37MfPk','NOr2BF1UUzU','25d3vd1dl4w','9ysTNnOpDQM','jpABm4pn9dg',
  '8I5-vNNplW8','ru6hzAV0Fx8','k64A1p8J89Y','dXtx6Lzl0BI','20Gg7zu8IIk',
  'a3rW-h0b3ow','-8pyOzK7TNc','XKTT6ppY87w','SwpIEEDIAhQ','YR7VyxoRO6I'
);

-- b) re-sequence rank to a gap-free 1..N within each (run_date, bucket).
--    rankings PK is (run_date,bucket,rank); the +100000 bump moves current
--    ranks out of the way so renumbering down to 1..N can't hit a PK collision.
--    Requires SQLite >= 3.25 for ROW_NUMBER() (DB Browser ships a newer build).
CREATE TEMP TABLE rank_map AS
  SELECT rowid AS rid,
         ROW_NUMBER() OVER (PARTITION BY run_date, bucket ORDER BY rank) AS rn
  FROM rankings;

UPDATE rankings SET rank = rank + 100000;
UPDATE rankings SET rank = (SELECT rn FROM rank_map WHERE rank_map.rid = rankings.rowid);

DROP TABLE rank_map;
```

### Step 3 — post-checks (run BEFORE _Write Changes_)

Every result should match the expected value. If any is off, click **Revert
Changes** and stop.

```sql
SELECT
  (SELECT COUNT(*) FROM videos)          AS videos,          -- expect 298
  (SELECT COUNT(*) FROM rankings)        AS rankings,        -- expect 487
  (SELECT COUNT(*) FROM stats_snapshots) AS snapshots;       -- expect 460

-- zero kid videos remain
SELECT COUNT(*) AS kid_remaining FROM videos WHERE video_id IN (
  'LrKTK37MfPk','NOr2BF1UUzU','25d3vd1dl4w','9ysTNnOpDQM','jpABm4pn9dg',
  '8I5-vNNplW8','ru6hzAV0Fx8','k64A1p8J89Y','dXtx6Lzl0BI','20Gg7zu8IIk',
  'a3rW-h0b3ow','-8pyOzK7TNc','XKTT6ppY87w','SwpIEEDIAhQ','YR7VyxoRO6I'
);  -- expect 0

-- zero orphaned references
SELECT
  (SELECT COUNT(*) FROM rankings        WHERE video_id NOT IN (SELECT video_id FROM videos)) AS orphan_rankings,   -- expect 0
  (SELECT COUNT(*) FROM stats_snapshots WHERE video_id NOT IN (SELECT video_id FROM videos)) AS orphan_snapshots; -- expect 0

-- every (run_date, bucket) group is contiguous 1..N (expect 0 rows returned)
SELECT run_date, bucket, MIN(rank) AS lo, MAX(rank) AS hi, COUNT(*) AS n
FROM rankings GROUP BY run_date, bucket
HAVING MIN(rank) <> 1 OR MAX(rank) <> COUNT(*);

PRAGMA integrity_check;  -- expect: ok
```

### Step 4 — commit

If all of Step 3 checks out, click **Write Changes** in DB Browser. Done.
(To undo before committing: **Revert Changes**.)

