# CONTEXT.md

**Purpose.** This file is the source of truth for the `daily-intel` Cowork project. Read it at the start of every task. Treat the conventions and definitions here as authoritative — if a task contradicts this file, ask before deviating.

**Owner.** Mary Jones — solo founder of Resource Highway LLC and creator of @mchabits / @maryjhabits.

**Last updated.** 2026-06-04 (v1 — expect iteration; see CHANGELOG at the bottom).

---

## About the brand

**Handle:** @maryjhabits.
**Tagline:** "Habits for the long game."
**Positioning:** Longevity habits, lived and tested over decades — not theory, not influencer hype. Evidence-informed, fitness-forward, age-irrelevant.
**Voice:** Direct, dry, occasionally funny. Not preachy. Earned authority over chirpy enthusiasm.
**Platform priority:** YouTube (primary) > Reels (secondary) > TikTok (tertiary).

**Brand pillars (the actual content territory):**
- Cardio science — zone 2, zone 5, Norwegian 4x4s, VO2 max
- Strength training for longevity
- Running (40+ years of practice, knee-aware, surface choice)
- Racket sports as longevity tools — tennis (clay preferred), pickleball
- Intermittent fasting and protein tracking
- Wearables and what they actually measure (and what they miss)
- Brain health, cholesterol, biomarkers
- Habit formation as the underlying mechanism for all of the above

**What the brand is NOT:**
- Wellness woo, supplement stacks, biohacker maximalism
- "Get shredded in 30 days"
- Generic productivity / James Clear cover-band content
- Anti-cardio / carnivore / extreme protocols

---

## About HHT (the product the brand eventually connects to)

**Name:** Hey Habit Tracker.
**Pitch:** Voice-first iOS habit tracking. Log with Siri without opening the app. 100% private — local SQLite, zero cloud, no analytics tracking.
**Status:** Live on App Store.
**Brand-product relationship:** The personal brand (@mchabits) is established first. HHT mentions come later, contextually, never as the cold open. Do not flag content as "promotional opportunity for HHT" in early daily reports — that's a later workstream.

---

## YouTube niche definition (for analysis tasks)

**"My space" = the intersection of these three lenses.** A video is in-space if it hits at least two:

1. **Topic lens:** longevity, zone 2, VO2 max, strength after 40, women's fitness over 40, running form/longevity, wearables (Whoop, Oura, Apple Watch, Garmin), HRV, protein, intermittent fasting, biomarkers, healthspan, racket sports for fitness.
2. **Format lens:** short-form (under 60 sec primarily; treat up to 3 min as adjacent), creator-led (not corporate/news), educational or demonstrative.
3. **Audience lens:** addresses people 35+ who care about long-term health, not aesthetic-first fitness or weight-loss content.

**Adjacent spaces (track but lower priority):**
- Pure longevity podcast clips (Attia, Huberman, Rhonda Patrick excerpts)
- Habit/behavior change content tied to physical practice
- Female fitness creators 40+ broadly

**Out of space (ignore unless they cross into the topic lens):**
- Bodybuilding, physique transformations
- Weight loss / calorie deficit content
- Hustle culture, generic productivity, "morning routine" lifestyle vlogs
- Diet-war content (carnivore, keto evangelism, anti-seed-oil rants)

---

## Peer / competitor creator set (v1 — refine over time)

Track these handles weekly for what's working in their feeds. **Not exhaustive — add as you find them.** Flag any new creator who appears repeatedly in "top in space" results across multiple days.

- Peter Attia (MD) — longevity clips
- Rhonda Patrick / FoundMyFitness — biomarkers, longevity
- Andrew Huberman — adjacent, when topic matches
- Stacy Sims — women's physiology, training
- Gabrielle Lyon — muscle-centric medicine
- *(Mary: add the 5-10 closer peers you actually watch — the ones in your size range, not just the giants. Update this list and bump CHANGELOG.)*

---

## "Top performing" thresholds (tunable)

These are defaults. Tune in this file, not in scripts. Single source of truth.

- **In-space top shorts (daily):** minimum 50,000 views in the last 24h. Report up to 10. If fewer than 3 hit the threshold, lower to 25,000 and note it.
- **All-YouTube top 5 (daily):** top 5 shorts by 24h view velocity, no topic filter.
- **"Breakout in my space":** any in-space short over 100,000 views from a creator with under 50,000 subscribers — flag with 🚀.
- **Window:** "yesterday" = the prior calendar day in US Eastern Time (Mary's TZ).

---

## What to surface beyond raw numbers

For each video in the report, include:
1. **Title, channel, views, duration, link.**
2. **One-line "why it worked" hypothesis** — hook style, opener, format trick, controversy angle, visual gimmick. Be specific. "Strong hook" is not a hypothesis. "Opens mid-action with a wearable on the wrist before the face appears" is.
3. **Transferability flag.** Could this format work for @mchabits? (yes / no / maybe — one line why.)
4. **Pattern callouts.** If 3+ top-in-space videos share a structural pattern (same hook style, same B-roll convention, same length), call it out in a "Patterns today" section at the end.

---

## Output conventions

- **File location:** `/output/`
- **Filename:** `YYYY-MM-DD-yt-intel.md` (e.g., `2026-06-04-yt-intel.md`)
- **Format:** Markdown.
- **Sections, in order:**
  1. `# Daily YouTube Intel — [date]`
  2. `## Top in my space` (the in-space list)
  3. `## Top 5 on YouTube overall` (the no-filter list)
  4. `## Patterns today` (only if a pattern surfaces)
  5. `## Anomalies / flags` (breakouts, anything weird, error notes)
- **Tone of the report:** scannable, blunt, no preamble. Mary is reading this with coffee, not in a board meeting.

---

## Operating rules for Cowork

- **Use existing scripts in `/scripts` before writing new code.** If a script doesn't exist for a need, ask before authoring.
- **Single source of truth.** Niche definitions, thresholds, and the peer list live here, not in scripts. Scripts read from this file or from a derived config — never hardcode.
- **No magic numbers in scripts.** If a threshold appears in code, it came from this file.
- **STOP gates.** If an API quota is hit, if the YouTube API returns an unexpected schema, or if a script errors out — stop, log the issue at the top of the day's report under "Anomalies / flags," and don't paper over it with cached or invented data.
- **Date handling.** Always use Mary's local TZ (US Eastern). Never UTC silently.
- **Privacy.** Don't write any personal data, account credentials, or API keys into report files. Keys live in environment variables only.

---

## What this file is NOT

Not a brand book, not a content strategy doc, not a business plan. It's the operating context for one specific automated workstream. Keep it tight. If a section stops being load-bearing, delete it.

---

## CHANGELOG

- **v1 (2026-06-04):** Initial version. Brand pillars, niche definition, default thresholds, output conventions.
