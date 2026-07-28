# Comprehensive TikTok Data Collection for Computational Social Science

**IC2S2 2026 Tutorial · Hands-on notebook**

Gayoung Jeon · Cameron Moy · Deen Freelon
Annenberg School for Communication, University of Pennsylvania

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![License](https://img.shields.io/badge/License-BSD--3--Clause-green) ![Status](https://img.shields.io/badge/Status-Tutorial%20Demo-orange)

This repository contains the hands-on materials for our IC2S2 2026 tutorial on collecting TikTok data for computational social science research. The notebook walks through three independent data-collection tools — the official **TikTok Research API**, **Apify** (a commercial cloud scraper), and **Pyktok** (an open-source browser-based scraper shipped in this repository) — and shows how to think critically about what each one returns. Our stress-testing research finds these tools frequently produce strikingly different results for identical queries, so understanding the differences is part of the methodology, not a side note.

> **Status:** the `pyktok_2026` build included here is the **full version** supporting both login and no-login endpoints. Login-required endpoints (user videos, comments, related videos, trending) need a TikTok session ID (see [Credentials](#credentials) below). All code is provided **as-is**; you use it at your own risk.

---

## Table of contents

- [Quick start](#quick-start)
- [What's in this repository](#whats-in-this-repository)
- [Setup](#setup)
- [Credentials](#credentials)
- [Tutorial structure](#tutorial-structure)
- [Tool comparison](#tool-comparison)
- [API quota limits (Section 2-1)](#api-quota-limits-section-2-1)
- [What this build of pyktok_2026 exposes](#what-this-build-of-pyktok_2026-exposes)
- [Output file naming](#output-file-naming)
- [Modifying inputs](#modifying-inputs)
- [A note on data quality](#a-note-on-data-quality)
- [License](#license)
- [Citation](#citation)
- [Contact and issues](#contact-and-issues)

---

## Quick start

```bash
git clone https://github.com/picl-asc/picl_ic2s2.git
cd picl_ic2s2

python3 -m venv tiktok-env
source tiktok-env/bin/activate          # Windows: tiktok-env\Scripts\activate

cp credential.env.example credential.env    # fill in your tokens — see below

jupyter notebook tt_tutorial_ic2s2.ipynb
```

The first three cells of the notebook install all Python dependencies and the bundled Chromium browser. Open the notebook and Run All — or run section by section. Each section is self-contained.

If you don't have Git, the **Code → Download ZIP** button on the GitHub page does the same thing.

---

## What's in this repository

```
tt_tutorial_ic2s2.ipynb      the main hands-on notebook
pyktok_2026/                 the open-source scraper used in Section 2-3
pyproject.toml               packaging metadata for pyktok_2026
sample_data/                 example output files used by Section 3 (analysis)
credential.env.example       template for your secrets file
.gitignore                   prevents committing credentials and scraped data
LICENSE                      BSD-3-Clause license
README.md                    this file
```

`credential.env` is **not** committed. You create it locally from the template; it's already listed in `.gitignore` along with any output CSVs.

---

## Setup

Requires **Python 3.10 or later**. A virtual environment is strongly recommended:

```bash
python3 -m venv tiktok-env
source tiktok-env/bin/activate          # Windows: tiktok-env\Scripts\activate
```

The notebook installs everything in its first three cells. If you would rather install upfront from the shell:

```bash
pip install TikTokResearchApi python-dotenv pandas requests tqdm
pip install apify-client
pip install -e .                        # installs pyktok_2026 from this folder
playwright install chromium             # downloads the headless browser
```

---

## Credentials

The three tools need different credentials. Copy the template and fill in your values:

```bash
cp credential.env.example credential.env
```

Then edit `credential.env`:

```dotenv
# TikTok Research API (Section 2-1)
CLIENT_KEY=your_client_key_here
CLIENT_SECRET=your_client_secret_here

# Apify (Section 2-2)
APIFY_API_TOKEN=your_apify_token_here

# Pyktok Session ID (Section 2-3, login-required endpoints only)
SESSIONID=your_sessionid_here
```

**Where to obtain each:**

| Tool | Where | Notes |
|---|---|---|
| **TikTok Research API** | [developers.tiktok.com/products/research-api](https://developers.tiktok.com/products/research-api/) | Academic application; review takes a few weeks. Apply well in advance. |
| **Apify** | [apify.com](https://apify.com) → Settings → Integrations → API Token | Free account; ~$5 in credits to start. |
| **Pyktok (no-login)** | — | Hashtag, Keyword, Sound endpoints work without login. |
| **Pyktok (login)** | Browser DevTools → Application → Cookies → sessionid | Required for: User videos, Comments, Related videos, Trending. |

If you don't have Research API access yet, Sections 2-2 and 2-3 still work standalone.

---

## Tutorial structure

Open `tt_tutorial_ic2s2.ipynb` and work through it in order, or jump to whichever tool you have access to. Each section is independent.

| Section | Tool | Endpoints covered |
|---|---|---|
| **1. Setup** | — | Install packages, set up credentials |
| **2-1. Research API** | TikTok Research API | User, User Info, Keyword, Hashtag, Comments |
| **2-2. Apify** | Apify (commercial) | User, Keyword, Hashtag, Comments, Related Videos |
| **2-3. Pyktok** | Open-source scraper | User, Hashtag, Keyword, Sound, Comments, Related, Trending |
| **3. Sample analysis** | pandas / matplotlib | Aggregations and exploratory plots on the collected data |

---

## Tool comparison

| | Research API | Apify | Pyktok |
|---|---|---|---|
| **Access** | Academic application | Free tier (~$5 credit) | Free, open source |
| **Cost** | Free | ~$0.25–$1.00 / 1,000 results | Free |
| **Endpoints** | User, Keyword, Hashtag, Comments | User, Keyword, Hashtag, Comments, Related | User, Hashtag, Keyword, Sound, Comments, Related |
| **Data source** | Back-end API | Front-end cloud scrape | Front-end browser scrape |
| **Daily limits** | 1,000 calls/day | Credit-based | TikTok rate limits apply |
| **Auth required** | OAuth (client key/secret) | API token | Optional (sessionid cookie) |

---

## API quota limits (Section 2-1)

The Research API gives you **1,000 calls per day**, shared across all endpoints:

| Endpoint | Max per call | Daily call budget | Practical max/day |
|---|---|---|---|
| Video (user / keyword / hashtag) | 100 videos | shared 1,000 | ~100,000 videos |
| Comments | 100 comments | shared 1,000 | ~100,000 comments |

If you run a keyword query that uses 500 calls, you have 500 left for everything else that day. Plan accordingly.

---

## What `pyktok_2026` 

Section 2-3 demonstrates the full Pyktok API. Available endpoints:

**No-login endpoints** (work without sessionid):
```python
pyk.get_hashtag_info(hashtag)          # hashtag metadata
pyk.get_hashtag_videos(hashtag, count) # videos for a hashtag
pyk.search_videos(keyword, count)      # keyword search
pyk.get_sound_info(sound_id)           # sound metadata
pyk.get_sound_videos(sound_id, count)  # videos using a sound
```

**Login-required endpoints** (need sessionid in credential.env):
```python
pyk.get_user_info(username)                   # user metadata
pyk.get_user_videos(username, count)          # user's videos
pyk.get_video_comments(url, count)            # video comments
pyk.get_comment_replies(cid, url, count)      # comment replies
pyk.get_related_videos(url, count)            # "You may like" stream
pyk.get_trending_videos(count, region='US')   # trending feed
```

**Utility functions:**
```python
pyk.specify_browser('chrome', headless=True)  # initialize browser
pyk.login_with_cookies(sessionid='...')       # authenticate
pyk.close()                                    # cleanup
```

---

## Output file naming

All CSVs follow the same convention so you can tell at a glance what tool collected what, when:

```
{tool}_{endpoint}_{target}_{YYYYMMDD}T{HHMMSS}.csv
```

Examples:

```
api_user_apnews_20240515T143022.csv
api_keyword_climate_change_20240515T160003.csv
apify_hashtag_booktok_20240515T172301.csv
pyktok_sound_7099827699635505963_20240515T191205.csv
```

---

## Modifying inputs

Every line in the notebook you would reasonably modify is marked with an ALL-CAPS comment immediately above it, for example:

```python
# CHANGE USERNAME TO YOUR OWN TARGET TIKTOK ACCOUNT (no '@')
username = "apnews"

# CHANGE START_DATE TO YOUR EARLIEST DATE (YYYYMMDD) — both start_date and end_date are REQUIRED by the API
start_date = "20240101"
```

The marked lines cover all the inputs that matter — usernames, hashtags, keywords, sound IDs, date ranges, target counts. For a basic run you should not need to edit anything outside those marked lines.

---

## A note on data quality

The three tools in this tutorial collect from **different sources** and often return **different results** for identical queries:

- **Research API** queries TikTok's back-end database directly (bypasses the recommendation algorithm)
- **Apify** and **Pyktok** scrape the front-end (capture algorithmically curated streams)

Our stress-testing research shows these differences are systematic and substantial. The notebook demonstrates the queries, but **critical evaluation of what each tool returns** is central to sound methodology. Section 3 includes basic exploratory analysis to help you start that process.

---

## License

BSD-3-Clause. See the `LICENSE` file at the repo root.

---

## Contact and Issues

Found a bug, broken endpoint, or unclear instruction? Please open a GitHub issue on the repository. That is the fastest way to get a fix.

For methodological questions, contact one of the authors:

| Author | Email |
|---|---|
| Gayoung Jeon | gjeon@upenn.edu |
| Cameron Moy | moycam@upenn.edu |
| Deen Freelon | dfreelon@upenn.edu |
