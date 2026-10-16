---
name: osint
description: Self-audit or CTF OSINT investigations with entity resolution, evidence-linked identities, information-gain crawling, and an interactive graph. Use to measure your own exposure or solve OSINT CTFs.
---

# OSINT Investigation & Entity Resolution Engine

Repo: `~/Projects/OSINT-tools`. Entrypoint: `~/Projects/OSINT-tools/tools/osint`.

Use on targets you own (self-audit) or CTF scopes. Never run against a person without their consent.

## Usage

```bash
# Scan single or multiple space-separated seeds: usernames/handles, URLs, emails, phone numbers, or names
tools/osint scan mintocha --pages 40 --min-gain 0.5 --out cases/<name>
tools/osint scan "mint ocha" https://github.com/mintocha user@example.com +14155552671
tools/osint show  cases/<name>
tools/osint graph cases/<name>
```

`scan` accepts multiple space-separated seeds across types:
- **Username / handle**: `mintocha`, `@mintocha`
- **URL / Link**: `https://github.com/mintocha`
- **Email**: `target@example.com`
- **Phone number**: `+14155552671`, `+1 (415) 555-2671` (normalized to E.164)
- **Name / string with internal space**: `"mint ocha"` (quoted so spaces are kept within the single seed)

`--min-gain` replaces depth: the crawl stops once no remaining lead is expected to add that many bits. `--pages` is a hard cap.
## Design

- Discovery (where are the target's accounts?): live web search is the primary lead source (`extensions/types/search.py` via `osint/search.py`, backend in `[search]`); each distinctive handle/email/name the target uses is searched and results become leads. Links, embedded page JSON, and pfp reverse-image exact matches (`osint/reverse_image.py`, `[reverse_image]`) add more. Guessed `site/{handle}` URLs (`leads.py`) are only a fallback.
- Identity (is this the same person?): rarity-weighted log-likelihood bits (Fellegi-Sunter) in `osint/score.py`, clustered by correlation clustering / signed multicut (HiGHS MILP, KwikCluster fallback) in `osint/resolve.py`. One false edge cannot collapse two identities because negative evidence outvotes it.
- Handles (`osint/identity.py`, `[fuzzy]`): compared on their base (case-folded, trailing digits stripped) with edit tolerance scaled by distinctiveness (unigram-segmentation surprisal). `vtloaf336`~`vtloaf`, `mintoocha`~`mintocha`; `catboy55` vs `catboys` stays unmatched. The same rule pulls self-described aliases out of any bio text without phrase templates.
- Membership is recomputed every resolve pass, never latched: confirmed = clustered with a seed anchor; tentative = review-band link to a confirmed account. Tentative accounts are explored (so a rare shared handle like `astricsll` gets followed) and dropped automatically if later evidence contradicts them; branches that grew only from a rejected account stop gaining priority.
- Relevance (where to look next?): personalized PageRank over edge conductance divided by background PageRank, in `osint/relevance.py`. Ranks the crawl; never merges identities.
- Crawl order / stop: best-first frontier by expected-information-gain-per-cost with a min-gain stopping bar, in `osint/frontier.py`.

## Agent judgment (do this, don't code it)

The CLI is deterministic. Calls that need judgment are the agent's job:

1. Run `scan`, then read `summary.json` verdicts. For every `review`-band pair, open both evidence URLs and decide. If same person, find the proving link (mutual link, shared id/email/pfp) and re-run seeded with both URLs (`scan URL1 URL2`). If not, note why.
2. Read the bios/descriptions of confirmed accounts for aliases and context the token rule can't catch (nicknames, school, city, "my main is …"). Search them (web search) and seed new scans with any accounts found.
3. Reverse-image every confirmed pfp. The tool only uses exact-reuse hits. Visually similar results are for you to check by eye.
4. Choose extra search phrasings (`"handle" site:reddit.com`, name + school, email local-part) when the default searches come back thin.
5. A custom `handle_judge` can be passed to `Scorer` for model-backed handle-variant calls; the default is the deterministic rule.

## Authoring plugins (the Emit API)

Drop one decorated function in `extensions/sites/` or `extensions/types/`. Site plugins receive one `Emit` (`osint/emit.py`); call declarative one-liners, no Entity/Edge construction:

```python
registry = globals().get("registry")

@registry.site(r"example\.com/(\w+)", name="example", priority=50)
def example(e):
    data = e.gh("...") or e.shell_json([...])
    e.account(url, ld_name=..., description=...)
    e.id(f"example:{data['id']}")   # platform id: strong cross-handle linker
    e.email(data.get("email"))       # None-safe, skipped if empty
    e.username(handle); e.image(avatar_url); e.link(outbound_url)
    e.scan(bio_text)                 # harvest every email/phone/handle/url
```

Type processors generate branch leads:

```python
@registry.processor(EntityType.USERNAME)
def probe(ent, graph):
    yield Lead(EntityType.URL, url, ent.id, "reason", weight=bits, url=url)
```

Edge kinds carry weight: HAS_ID/SAME_AS ~ proof, USES/DEPICTS strong, LINKS_TO medium, MENTIONS ~ lead only (co-occurrence is never identity evidence).

Bundled plugins: github (commit-author de-anon, fork-upstream org leads, repo metadata & README terms), twitter/x, threads, youtube, roblox, discord invite, mozilla AMO, link hubs. Processors: web search (username/email/name/term/owned-domain backlinks), guessed-profile fallback, email (with local-part username pivot), url, avatar (PDQ hash + reverse-image exact matches via Bing & Bright Data), phone (carrier/region), discord user id -> profile, Google account (email -> display name/photo with auto-continuation), WhatsApp number -> public profile.

## Config

`config/weights.toml`: `[weights]` evidence bits, `[fuzzy]` handle variants, `[posterior]` decision thresholds, `[conductance]` relevance, `[search]` search backend, plus per-processor sections (`[reverse_image]`, `[discord]`, `[google_account]`, `[whatsapp]`). No secrets in the repo.

## Optional credentials (processors no-op without them)

- Google account lookup: a signed-in Google session cookie or OAuth token in `~/.config/osint/` or `config/google.json` (gitignored).
- Bright Data SERP API (reverse image / Google Lens): `BRIGHTDATA_API_KEY` or `config/brightdata.json` (gitignored).
- Discord: `DISCORD_BOT_TOKEN` for the official API; public lookup works without it.
- WhatsApp: wa.me business cards need nothing; personal profiles need `WHATSAPP_API_KEY` or a linked session (`.venv/bin/python extensions/types/whatsapp_profile.py --setup-session`).
- Reverse image via Google Lens/TinEye: `SERPAPI_API_KEY` / `SEARCHAPI_API_KEY` / `TINEYE_API_KEY` or `BRIGHTDATA_API_KEY`; Bing exact matches need nothing.
## Not built

No payment-processor data.
