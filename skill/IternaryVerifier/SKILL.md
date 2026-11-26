---
name: IternaryVerifier
description: >-
  Audit and verify a travel itinerary across eight mandatory gates: Google review
  & quality inspection (reading actual negative/1-star reviews rather than star
  averages, especially for hotels), schedule timing & buffer checks, risk
  review, opportunity check, ADHD stimulation/novelty check, cost/value
  alternatives, local common sense sanity check (most important), and tourist
  trap / hidden fees & T&C check. Use when verifying, reviewing, or auditing any
  trip in TripPlanner/TREK.
---

# Itinerary Verifier (`IternaryVerifier`)

Run a strict 8-gate audit on a planned itinerary (e.g., via `.venv/bin/python trek_cli.py summary <trip_id>`). Report a clear **PASS / WARN / FAIL** verdict per gate with evidence, and only recommend substitutions that pass Gate 1.

---

## Gate 1: Google Review & Quality Audit (Read the Reviews, Not the Stars)

**Rule:** A high average can hide recurring problems. Inspect actual low-rated reviews for every planned place, especially hotels. For hotels, also check topic sentiment where available. Treat review solicitation as a separate claim that needs direct evidence.

### How to read low-rated reviews without an API key

1. **Hotels — read the full low-rated reviews first.** Find the Google Hotels entity URL with `site:google.com/travel/hotels/entity "<hotel name>" <city>` and verify the address. From this skill's directory run:

   ```bash
   python3 scripts/read_hotel_reviews.py "<Google Hotels entity URL>" --filter bad
   python3 scripts/read_hotel_reviews.py "<Google Hotels entity URL>" --filter bad --topic room
   ```

   This reads the same public review feed as the Google Hotels page using Python's standard library: no key, browser, or account. `bad` (default) reads 1–2★, `good` reads 4–5★, `relevant` reads what Google calls **Most helpful** (realistic expectations regardless of stars), and `recent` reads newest reviews. Use `--topic room`, `sleep`, `bathroom`, or `cleanliness` when a specific concern needs a closer look. Use `relevant` and `recent` to understand realistic day-to-day guest expectations rather than relying on skewed topic unfavorable percentages alone. JSON retains the review's full text, original score, date, and provider. The page can mix providers; do not label all results as Google reviews. This tool reads one batch, not a census, and the feed is undocumented. If it fails, open the public Google Hotels `/reviews?hl=en` page and use **Lowest score** and **Read more**.
2. **Hotels — fetch realistic expectations, recent/low reviews, and live prices together.** `python3 scripts/gmaps_tools.py reviews --url "<Google Hotels entity URL>"` returns `relevant_reviews` (Most helpful — realistic expectations), `recent_reviews` (newest stays), `lowest_rated_reviews` (1–2★ defects), `prices` (live OTA offers + typical nightly range from Google Hotels `/prices`), and `topics`. Judge Gate 1 by reading the actual verbatim comments for realistic expectations and recurring defects—do **not** fail a hotel solely because a topic has a high unfavorable percentage.
3. **Non-hotel places (restaurants, attractions, pool halls, museums, etc.) — use `gmaps_tools.py reviews`.** Run keyless against any Google Maps place by query, Maps URL, or `data_id`:

   ```bash
   python3 scripts/gmaps_tools.py reviews --query "Asia Club Snooker & Pool SS15 Subang Jaya" --maps
   python3 scripts/gmaps_tools.py reviews --url "https://www.google.com/maps/place/..."
   ```

   This resolves the place via Google Maps and returns `title`, `source_url`, `google_rating`, `google_review_count`, `rating_histogram`, `topics`, `relevant_reviews`, `recent_reviews`, and `lowest_rated_reviews` (no API key or browser needed). Add `--maps` if you want to skip the Google Hotels lookup for `--query`. If a place has few or no text reviews, report the sample as limited rather than assuming no complaints exist.
4. **Corroborate complaints.** Search `"<place name>" <city> reviews` with terms such as `"noisy"`, `"dirty"`, `"broken lock"`, `"hidden fee"`, or `"forced"`. Read the original review pages on Booking.com, Agoda, Trip.com, Tripadvisor, or relevant forums. Verify that complaints refer to the correct property and are from distinct reviewers. Missing low-rated evidence is a coverage gap, not a pass.

**Triage each complaint by what happened, not how angry the reviewer sounds.** Separate repeatable property issues (e.g. mold, broken locks, noise, dirty bedding) from context-dependent incidents (e.g. arriving after the stated check-in cutoff or a booking-channel dispute). Check whether an incident matters to this itinerary: a late check-in complaint matters if the traveler will arrive late. Look for independent repeats and recency; note when an owner response disputes a claim. In the verdict, give a short verbatim excerpt, score, date, and source link for each material pattern. Label isolated or disputed reports as such.

### Gate 1 Verdict Criteria
- **Compulsory Smoke-Free Rule (Hotels & Indoor Attractions/Venues):** **Smoke-free is mandatory.** For hotels, any recurring reports of cigarette/smoke smell in non-smoking rooms or corridors is an automatic **FAIL**. For indoor attractions and venues (pool/billiards halls, arcades, lounges, eateries), verify via reviews/local reports that smoking is strictly prohibited and enforced indoors; if indoor smoking or heavy smoke haze is tolerated, mark **FAIL**.
- **FAIL:** Independent low-rated reviews show recurring, traveler-relevant problems (e.g. cigarette/smoke smell, noise, mold, broken locks, coercion, or hidden fees), or a credible serious safety issue. Topic sentiment alone flags what to investigate; it does not establish a failure.
- **WARN:** Mixed reviews with isolated fixable caveats (e.g., "book river-view room to avoid street noise"), or insufficient review coverage to verify quality.
- **PASS:** Consistent positive feedback across physical quality, cleanliness, smoke-free air, and genuine guest experience without coercion signals.

---

## Gate 2: Time & Buffer Check

Audit every day's timeline from first departure to hotel return:
- **Keyless routing & corridor checks (`scripts/gmaps_tools.py`):** Verify travel times/distances and nearby or en-route stops without an API key:
  ```bash
  python3 scripts/gmaps_tools.py route --origin "Sunway Mentari" --destination "Asia Club Snooker & Pool SS15 Subang Jaya"
  python3 scripts/gmaps_tools.py near --near "Sunway Mentari" --query "cafe" --limit 5
  python3 scripts/gmaps_tools.py along-route --origin "Kuala Lumpur" --destination "Melaka" --query "kopitiam" --samples 5 --limit 10
  ```
- **Physical feasibility:** Check drive/transit durations between consecutive stops (including border/checkpoint queues, toll/highway traffic, parking, and gear/check-in time). Unless flights require tight connections, require **ample driving buffer** (≥ 20–30% margin on intercity drives, ≥ 60 min on border crossings).
- **Late-night return rule:** Flag hotel returns **after 21:30 (9:30 PM)** unless required by a flight or an intentional night activity (e.g., fireflies).
- **Early-morning wake rule:** Flag departures/wakes **before 06:30 AM** unless required by a flight.
- **Backup/overlapping slots:** Distinguish explicit backup options (noted in day title/notes) from actual double-bookings.

### Gate 2 Verdict Criteria
- **FAIL:** Impossible transfer times, missing buffers on critical arrivals, or repeated `< 06:30` wakes / `> 21:30` returns without flight justification.
- **WARN:** Minor buffer tightness on 1 leg or a single borderline late return.
- **PASS:** Realistic pacing, ample driving buffers, wakes ≥ 06:30, returns ≤ 21:30.

---

## Gate 3: Risk Review

Evaluate operational, environmental, physical-safety, and local crime risks—and **explicitly raise "Watch out for..." warnings** tailored to the destination:
- **Driving & road risks:** Late-night driving on unlit/rural/wildlife-heavy roads (e.g., regional Australia), driver fatigue after physically exhausting activities.
- **Location-specific crime & personal safety:** Pickpockets/bag-snatching in tourist hubs (e.g., Paris/Barcelona transit & landmarks), car break-ins or carjacking hotspots (e.g., leaving luggage visible in unattended cars, sketchy night parking), and unsafe neighborhoods.
- **Weather & natural hazards:** Monsoon/afternoon thunderstorm season, river/dam release levels (e.g., rafting cancellations/mandatory river switches), landslides, flash floods, heatwaves.

### Gate 3 Verdict Criteria
- **FAIL:** Unmitigated high-risk activity (e.g., night rural driving in wildlife zones, severe weather/disaster exposure with no backup, high-crime exposure without mitigation).
- **WARN:** Moderate seasonal/safety/crime risk where the itinerary is viable **with specific "Watch out for..." precautions** (e.g., afternoon storm season, river-level backup, pickpocket/car-security precautions).
- **PASS:** Low risk or well-mitigated with clear contingencies.

---

## Gate 4: Opportunity Check

Ask: *"Would the traveller be missing something noticeably better (more memorable, higher quality, or better value) along the exact same corridor or time window?"*
- Compare planned stops against top-rated alternatives in the same region/corridor.
- Flag obvious tourist traps or low-value fillers when a stronger alternative fits the same slot.

### Gate 4 Verdict Criteria
- **FAIL:** Major missed highlight or obvious tourist trap occupying prime time.
- **WARN:** Minor optimization possible (e.g., a better nearby food stop or scenic detour).
- **PASS:** Strong route selection with good use of time along the corridor.

---

## Gate 5: ADHD / Stimulation & Novelty Check

**Traveller profile:** ADHD brain — thrives on **high-stimulation, novel, hands-on, adrenaline/sensory-rich experiences** (white-water rafting, luge/karting, climbing/bouldering, unique wildlife, night markets, unusual local terrain) and **crashes out** on slow, passive, repetitive days or generic attractions easily found in **Singapore** (basic shopping malls, generic aquariums/zoos, standard chain cafes, full days of passive museums).

- **Long-ish car rides are fine:** Do **not** penalize or fail an itinerary for long driving stretches; road-trip driving time is acceptable as long as the destinations themselves are stimulating.
- **FAIL:** Any full day dominated by passive/low-stimulation sightseeing ("boring ass museums" all day) or generic activities indistinguishable from Singapore.
- **WARN:** A slow/passive half-day without a stimulating anchor nearby.
- **PASS:** High-novelty, high-engagement anchors each active day; any brief cultural/heritage stop (≤ 1–1.5h) is balanced by active/novel experiences or transit pacing.

---

## Gate 6: Cost & Value Check (Gated by Gate 1)

Search for lower-cost alternatives of **similar or better quality** for hotels, activities, and transport.
- **Hard constraint:** **NEVER substitute a cheaper option if it fails Gate 1 (Google Review & Quality Audit).** Paying slightly more to escape a noisy/dirty/review-coerced hotel is always preferred.

---

## Gate 7: Common Sense Check (MOST IMPORTANT)

**This is the most important gate.** Step back from checklists and apply **pure common sense + local knowledge of the destination** to judge the overall vibe and whether the plan actually works in real life:
- Does the geography and daily flow make sense without pointless backtracking?
- Do the vibes and local realities fit (e.g., realistic border/holiday traffic, night driving conditions, energy levels after physically exhausting activities like rafting, local meal/opening-hour culture, whether a "backup" makes sense in context)?
- **FAIL:** The itinerary feels logistically absurd, ignores obvious local realities (e.g., Causeway/holiday jam timings, driving exhausted at night in unlit areas), or the overall vibe simply doesn't work.
- **WARN:** Mostly sensible with 1–2 rough edges worth smoothing out.
- **PASS:** Cohesive, realistic, and passes the local-knowledge "does this actually make sense?" test.

---

## Gate 8: Tourist Trap, Scams, Hidden Fees & T&C Check

**Threshold:** Focus strictly on **egregious traps**—the kind of nasty surprise that, if not known beforehand, will genuinely ruin the experience, blow up the budget, or leave you feeling ripped off (not minor nitpicks; physical safety/crime belongs in **Gate 3**):
- **Experience-ruining tourist traps:** e.g., The Shore Sky Tower banning personal phones/cameras on the glass ledge and forcing overpriced official photos, rigged/unmetered taxis, fake guides/tickets, or menus without prices.
- **Predatory / missing T&Cs & refund traps:** Undisclosed partial-refund policies, missing cancellation terms, or mandatory venue-switch clauses (e.g., no refund if you decline a last-minute river/location swap).
- **Surprise fees & OTA double-charging:** Mandatory **conservation fees, state/sustainability fees, tourism tax, resort/cleaning fees, or large cash deposits**—especially checking whether an OTA (e.g., Agoda, Booking.com, Klook) **already collected** the fee so you don't pay twice at check-in.
- **FAIL:** An egregious experience-ruining trap, predatory/undisclosed refund clause, or major surprise/double-charge risk with no warning.
- **WARN:** Restrictive T&Cs or extra local fees that are manageable once known and pre-verified before paying.
- **PASS:** Transparent pricing and T&Cs with no egregious tourist-trap surprises.

---

## Output Format

Present a concise audit table first, followed by per-gate findings and actionable fixes:

```markdown
| Gate | Status | Key Finding |
| :--- | :--- | :--- |
| 1. Google Reviews & Quality | FAIL / WARN / PASS | ... |
| 2. Time & Buffers | ... | ... |
| 3. Risk Review | ... | ... |
| 4. Opportunity Check | ... | ... |
| 5. ADHD / Stimulation | ... | ... |
| 6. Cost & Value (Gate 1 Gated) | ... | ... |
| 7. Common Sense (Most Important) | ... | ... |
| 8. Tourist Trap, Fees & T&Cs | ... | ... |
```
