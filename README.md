# UAE Real Estate Scraper (Dubai and Sharjah)

A daily job that collects the official lists of real estate projects in Dubai and Sharjah, merges them into one private Google Sheet, works out where each project is, and can draw the located ones on a map.

It is a separate project from the scraper it was modelled on: its own repository, its own Google Cloud project and service account, its own spreadsheet and its own API keys. Nothing from that project's data or credentials is used here.

The full contract is `docs/DESIGN.md`. If this file and the design disagree, the design wins; fix both in the same change.

## What is real today, and what is not

Read this before relying on a number. Nothing below has been checked against a live service.

| Part | State |
|---|---|
| Dubai registry (`dld_projects`) | Built against the documented schema and synthetic test data. **Never run against the live Data.Dubai service**: it needs a free key and secret that have not been issued yet, and the live column names are unverified. Until both are set the source is skipped. |
| Sharjah registry (`srerd_projects`) | **Off by default and unverified.** There is no official feed; the adapter reads one public list page (project name, area, ownership type; no id and no developer) and refuses any response it does not recognise. Turn it on only after a supervised test (`ENABLE_SRERD=true`). |
| Hand-researched seed (`seed_csv`) | Ships with only a header row. Every row needs a source URL. |
| News discovery (`gemini_news`) | Off by default (`ENABLE_GEMINI_NEWS=true` turns it on). It only proposes rows for the `Launch_Candidates` tab; a person decides what they are. |
| Map | The page and the export script are built, and a demo with invented pins has been rendered. **Public hosting on GitHub Pages is decided** (27 October 2026), but nothing is published yet: the owner switches it on with `DEPLOY_MAP` once real data flows (`setup_google_sheets.md`, section 10). |

### Known limitations

1. **Live endpoints are unverified.** The Dubai and Sharjah adapters have only ever run against synthetic data. Expect to fix column names or response shapes on the first real run.
2. **Sharjah has no registry feed, and a row needs an id or a developer.** The public list has no developer column, so a list row without a project id has no key at all: it is dropped and counted, and if more than half of the rows are unusable the source fails. A project is keyed by its registry id, or else by developer plus project name. A row with neither is never written. A name can be part of a key only if it holds a Latin letter and nothing that would make two different names look alike once reduced to A-Z and digits: a name written only in Arabic or another script, or holding a letter that does not reduce to A-Z, or a `+`, `#` or `@`, makes no key part, and the row then needs a registry id. Real Sharjah coverage therefore depends on the list exposing ids, or on a hand-kept seed that names the developer.
3. **Dubai coordinates come only from geocoding.** The Dubai registry has no coordinates. Every pin is a geocoder result (OpenStreetMap by default) or a person's override. A single off-plan project is often not on the map yet, so most pins will be community-level (a hollow marker), not exact. A public-geocoder hit is accepted as an exact pin only if its name agrees with the project's and it is neither the community's own centre nor a street, a district or a different emirate; otherwise the row falls back to the community or area, or stays unlocated.
4. **The default geocoder is slow on purpose.** Public OpenStreetMap Nominatim asks scripts that run at regular intervals to stay at 4 requests per minute, so the default backend waits 15 s between requests and stops after 200 lookups a run. That is at most 200 projects a day, and fewer in practice, because one project can need several lookups: up to three name variants, then the community, then the area, plus the lookup of the centre of its community or area that an answer is checked against. How many projects a run really reaches has not been measured. A large first backfill therefore takes weeks. A keyed backend (`LOCATIONIQ_API_KEY` or `GEOAPIFY_API_KEY`, both with free tiers) is much faster: about one lookup a second for LocationIQ and about four for Geoapify, with a far bigger daily budget. The geocoder's lookup cache does not survive between CI runs (the runner starts empty); what carries over is the `Geocode Status` column in the sheet.
5. **No prices, bedrooms or handover dates from any official source.** Property portals forbid automated access and this project does not scrape them. These columns are filled only from a hand-researched seed with a source URL per row, or by a licensed feed once one is chosen. A blank cell means unknown, never zero.
6. **The Dubai Land Department data licence is unclarified.** The catalogue says "Not Specified". The owner has decided to publish the map publicly, and that decision does not settle the licence: it stays an open risk, and getting the position in writing before the first deploy is recommended (see "Open decisions").
7. **A run with no source configured is green.** It changes nothing and logs `NO SOURCE PRODUCED ANY REGISTRY ROW`. Once the Data.Dubai key works, set the repository variable `REQUIRE_SOURCES` to `dld_projects` so a missing or empty Dubai feed fails the run.
8. **The feed wins over hand edits.** See "What each tab is for" below.
9. **The map export cannot filter all free text.** It cannot recognise names of people or digits spelled out in words, so free text in the sheet is analyst-reviewed before it is published. See "The map" below.

## Principles

- Blank beats invented: no default developer, price, bedrooms, handover, id or location.
- Fail loud: a source that errors, returns nothing, or returns far fewer rows than last time makes the run fail. There is no bundled snapshot to fall back on.
- Dubai and Sharjah identities never merge. A project with two different registry ids is two projects even when the names match.
- Legitimate sources only: official open data, licensed APIs, and pages whose terms allow it. No spoofed browser identity, no certificate bypass, no getting around blocks.
- The sheet is private. The public map is a separate, deliberate switch (`DEPLOY_MAP`), off until the owner turns it on.

## Set up and run locally

You need Python 3.12. From the repository root:

```
python -m venv .venv
```

Then install the requirements with the environment's own Python:

```
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

(On macOS or Linux that is `.venv/bin/python -m pip install -r requirements.txt`.) The commands below assume the environment is active; otherwise start each with that Python.

Copy `.env.example` to `.env` and fill in only what you have. The file lists every setting by name; `.env` is git-ignored and must never be committed. An empty value means "not set".

Run the tests (no network, no credentials needed):

```
python -m scripts.check_test_gate
python -m pytest
```

The first line is the check the daily run makes before the tests (see "The daily run"): it fails when the test configuration has been changed so that `pytest` could pass without running the suite. A `PYTEST_*` variable in your own shell trips it too.

Creating the Google Cloud project, the service account and the spreadsheet is a one-time job for the owner. The steps, and the full list of repository secrets and variables, are in `setup_google_sheets.md`.

### Running the job by hand

```
python -m src.main [--dry-run] [--no-geocode] [--snapshot PATH] [--env-file PATH] [-v]
```

| Flag | Effect |
|---|---|
| `--dry-run` | Reads the sheet and fetches the sources, computes everything, and writes nothing: no registry, candidates, pins, seed, snapshot file or run-log row, and no geocoding lookups. It needs `SPREADSHEET_ID` (otherwise exit 2). The source fetches are real requests, so it uses your API quota. It still opens the spreadsheet the normal way, so on a blank or older sheet it may add a missing tab or header column (nothing else is written). |
| `--no-geocode` | Same as `GEOCODE_ENABLED=false`: no lookups. A person's `Geocode_Overrides` still apply. |
| `--snapshot PATH` | Where to write the JSON snapshot. Default: `scraped_projects_latest.json` in the repository root. |
| `--env-file PATH` | A `.env` file to layer under the real environment. It must exist. |
| `-v` | Debug lines from this project's own loggers: today only the reason a geocoder answer was refused (a fixed word) and the address and offset of each Dubai registry page request (with credentials scrubbed). Only this project's own loggers are ever shown; every library's records are dropped at every level, `-v` included, so request URLs from libraries are never logged. |

**First run: do `python -m src.main --dry-run` first.** Read the log: which sources were skipped, how many rows each returned, and what the merge would add. Then run it for real.

The exit code is what the scheduled job reports:

| Exit code | Meaning | A run-log row is written? |
|---|---|---|
| 0 | Clean. Every enabled source delivered, nothing was blocked, every write succeeded. A geocoding run that stopped on a quota (HTTP 429, or Google's over-limit statuses), a lookup budget or a time budget is still clean: the rows it did not reach are tried again on the next run. | Yes (not on `--dry-run`) |
| 1 | The run did what it could and something is wrong: a source failed (error, zero rows from a source that always has some, or fewer than 80 percent of its last good row count); a source in `REQUIRE_SOURCES` was skipped or switched off; geocoding crashed, or the geocoder gave up for a reason other than a quota (a refused or revoked key, HTTP 401, 402 or 403, or five failed lookups in a row), so that no new pins can appear until someone looks; `Registry_Projects` or `Geocode_Overrides` has rows that cannot be read; the `Listing_Data` seed file has bad rows; a write failed; or someone edited `Registry_Projects` while the run was in progress (the write is refused so their edit is kept). The log ends with a numbered list of the problems. | Yes |
| 2 | Setup failure before any work: bad command line or configuration, an unknown name in `REQUIRE_SOURCES`, the spreadsheet cannot be opened (bad credentials, not shared with the service account), or `--dry-run` without `SPREADSHEET_ID`. | No (there is nowhere to write it) |

## What each tab is for

The spreadsheet has six tabs. "Machine" means the daily run writes it; do not rely on hand edits there.

| Tab | Owner | What it holds |
|---|---|---|
| `Registry_Projects` | Machine; analysts may add rows and fill blank cells | One row per project from the registries and the seed. Rewritten in full on every run. **The feed wins**: a cell the source fills is overwritten on the next run, so a hand correction there is silently reverted. To move a pin, use `Geocode_Overrides`. To fix a `SEED` row, edit the seed CSV (`src/data/seed_registry.csv`), not the sheet: a renamed seed row looks like a new project and is added a second time. Rows no source mentions are kept, never deleted. A column an analyst adds by hand is carried across the rewrite as its displayed **value**: a formula there becomes plain text, so put formulas on `Listing_Data` or another tab. |
| `Listing_Data` | Analysts | Prices, areas, bedrooms, handover and payment plan, one row per `Project Key`, each with the `Source URL` it came from. The run only appends seed rows and never overwrites an analyst's row. A row with an invalid `Project Key` or no http(s) `Source URL` is skipped and counted. A typed number that cannot be read with certainty is left blank on the map and counted in the log, never guessed: a range (enter it in the From and To columns), a bound such as `<3 BR` or `1,200+ sq ft`, or a lone dot before three digits such as `750.000`, which could be a decimal or a thousands separator. (A price written `1,250,000+` or `from 850,000` is read as that figure.) Use the registry key (`dld:dubai:<number>`) as soon as a project has one: a `name:` key is built from the developer and project name, so it stops matching when a feed renames the project, and the price then vanishes from the map. The run counts such unmatched keys in its log and lists the first ten in the `Notes` of `Scrape_Run_Logs` (the sheet is private; the Actions log of a public repository is not). |
| `Launch_Candidates` | Machine adds, analysts decide | Leads from news discovery. The run appends new ones and never edits a row; a person sets `Status` (`new`, `confirmed`, `rejected`). Nothing is promoted automatically. |
| `Geocode_Overrides` | Analysts | A hand-placed pin per `Project Key`. It always wins over any geocoder and is never overwritten. Key it by the registry key (`dld:dubai:<number>`) where the project has one: a `name:` key that two different registry projects share is ambiguous, so it pins neither of them (the run logs a warning). A pin that lies outside its project's own emirate is still applied (the run logs a warning with a count), but the map export leaves it off the map (see "The map"). |
| `Map_Pins` | Machine | The projects that have real coordinates, exact pins first. Derived from `Registry_Projects`; rewritten on every run. |
| `Scrape_Run_Logs` | Machine | One row per run: start and end time, per-source status and row counts, geocoding totals, exit code, git commit. |

Do not edit `Registry_Projects` while the daily run is going (03:00 UTC, usually under an hour). A run that sees the tab change refuses to write and exits 1; your edit is kept and the next run starts from it.

## The daily run (GitHub Actions)

`.github/workflows/daily_uae_real_estate.yml`, named "Daily UAE Real Estate Scraper", runs at 03:00 UTC (07:00 in the UAE) and on demand from the Actions tab. In order, it checks out the code, installs Python 3.12 and the requirements, **checks the test gate** (`python -m scripts.check_test_gate`, described below), **runs the test suite (a failing test stops everything before any credential is used)**, runs `python -m src.main`, and exports the map pins. The scrape job has a 90-minute limit; geocoding stops by itself after `GEOCODE_TIME_BUDGET_SECONDS` (3300 seconds, 55 minutes, by default: room for 200 lookups paced 15 s apart, which take 50 minutes). A larger value is clamped to 4500 seconds (75 minutes), which still leaves the job room to finish inside its 90-minute limit; the run logs the name and the ceiling, never what was typed.

**The test gate.** A green test step only means something if the suite really ran, and `python -m pytest` exits 0 after running nothing when a setting says so: `--collect-only` or `--ignore` in `pytest.ini`, a `conftest.py` that skips tests, a `PYTEST_ADDOPTS` variable, deleted test files. So a step of its own, `python -m scripts.check_test_gate`, runs right before the tests, with no secrets, and fails the job (exit 1, before any test or credential) unless `pytest.ini` is exactly the expected file (`testpaths = tests`, `pythonpath = .`, `addopts` absent or holding nothing but `-q`), no `conftest.py` and no other pytest configuration file exists (`pyproject.toml`, `tox.ini`, `setup.cfg`, `pytest.toml`, `.pytest.toml`, `.pytest.ini`), no `PYTEST_*` environment variable is set, there are at least 23 test modules, and a collect-only run finds at least 5200 tests. Its messages start `test gate: ` and hold fixed phrases, counts and file names only, so they are safe in a public log; `setup_google_sheets.md`, section 7, says what each one means. The floors sit just under today's suite, so deleting a whole test module turns the gate red: raise them (`MIN_TEST_MODULES` and `MIN_COLLECTED_TESTS` in `scripts/check_test_gate.py`) when the suite grows, and treat lowering them as a change for a reviewer. The gate catches a mistaken or careless change to how the tests run. It does not stop someone who means to disarm it: it shows that the suite is collected and run, not that its tests assert anything, and it lives in the repository it guards, so a pull request that touches the tests, the gate, the workflow or `requirements.txt` needs a reviewer (see the hardening notes in `docs/DESIGN.md`).

This repository is **public**, and the artifacts and logs of a public repository can be read by anyone. The owner has decided to host the map publicly on GitHub Pages, but the licence for the Dubai data is still unclarified, so the run publishes nothing until the owner switches it on:

| Switch (repository variable) | Default | Effect when set to `true` |
|---|---|---|
| `UPLOAD_SNAPSHOT` | off | Uploads the JSON snapshot as a workflow artifact, kept 7 days. Anyone can download it. |
| `DEPLOY_MAP` | off | Packages `site/` and publishes it to GitHub Pages in a separate job, once at least one project is located. The repository is public, so Pages is free. The owner sets it after real data flows (steps in `setup_google_sheets.md`, section 10); the address will be `https://<owner>.github.io/<repository>/`. |

**What the public log may hold.** The code's own log lines carry counts, source and backend names, field names, row numbers, HTTP status codes, a provider's fixed status token and fixed phrases, and the class name of an error. They never carry a response body, a provider's error text, a query or a URL with one, a project key or name, a row's contents, a cell value, a spreadsheet title, a service-account email, a key or any other secret; the keys a person needs go to the private `Scrape_Run_Logs` notes. An error is logged as its class name and, for one the code did not plan for, the file and line it came from; the message of such an error is never shown. An error that nothing catches (a `KeyboardInterrupt`, say, from Ctrl-C or a cancelled run) is covered as well: both entry points, `python -m src.main` and `python -m scripts.export_map_pins`, install a hook as their first statement that prints just `ClassName (at file.py:line in function)`, never the message or the chain of causes that Python would print. The hook does not see an error raised while the modules are being imported, one in another thread, or a `SystemExit` such as argparse's usage error. A library's own error text (the Gemini client's, the Google sheet client's, an HTTP library's) is replaced before it can be logged: by its class name, an HTTP status code, a fixed Google status word or, for the Gemini client, a fixed hint. The pin export turns a failure to read the sheet or write the file into a one-line message that holds the class name only (`reading the sheet failed: ClassName`). The message is shown only for this project's own error types (a source, sheet, geocoder-setup, setup or seed-file error, or a registry that changed under the run), whose texts are written to hold counts, field names and fixed phrases. That is a rule for the code's authors, enforced by review and tests; the scrubber described below catches secrets, not content. No setting is echoed as typed. The start-up `config NAME: value` lines show a key or other secret, the spreadsheet id and the geocoder contact only as `set` or `not set`, the list of addresses to share the sheet with only as a count, the sheet name and the User-Agent only as `default` or `custom (not shown)`, the Gemini model name only when it has the shape of a Gemini model id, `REQUIRE_SOURCES` as the source names this scraper has plus a count of any other name, and only a yes/no, a number or a fixed choice that the code has already checked as its value. The warning for a setting that is not a valid yes/no, number or choice names the setting and the default used, never what was typed. Two rules sit on top of that, in `src/log_policy.py`, and the scrape step and the pin export both use them. First, only this project's own loggers (`src.*` and `export_map_pins`) reach the public log: every record from a library (urllib3, google-auth, gspread, requests, the Gemini client) is dropped at every level, because such records carry request URLs, search queries, headers and the service-account email, and Python warnings are captured and dropped the same way. Second, every own line is scrubbed once more of the known secret values, credential-shaped text and the identifiers in the service-account key (its email, the project id, the key id and the client id). The daily run never passes `-v`. GitHub prints the value of every repository **variable** in the step header of a public log, so put anything personal in a secret, never in a variable (`setup_google_sheets.md`). As a precaution, leave GitHub's own debug logging off on a public repository (do not create the repository secret or variable `ACTIONS_STEP_DEBUG` or `ACTIONS_RUNNER_DEBUG`, and do not tick "Enable debug logging" when re-running a job); this project cannot prevent it. Secrets are handed only to the two steps that use them (the scrape and the pin export), never to the test gate or the test step.

Things to know about a public repository:

- GitHub disables scheduled workflows after **60 days** without repository activity. If the daily run stops appearing, re-enable it in the Actions tab or push any commit.
- The workflow uses only GitHub's own actions, pinned to a major version tag (the Node 24 majors), not to a commit. A tag can move; review the actions now and then; `.github/dependabot.yml` asks Dependabot to propose each bump (of an action or of a Python dependency) as a weekly pull request. A `CODEOWNERS` file names the owner for the workflow, the gate, the tests and `requirements.txt`; it takes effect once the owner switches on "Require review from Code Owners" in a ruleset. Further hardening choices that are the owner's and are **not implemented** (hashed dependencies, who can reach the repository secrets or change the checks, a keyed geocoder's terms on a published map, the committer identity of the first commits) are listed in `setup_google_sheets.md`, section 9.
- While `DEPLOY_MAP` is on, the pin export requires at least one located project (`--min-pins 1`), so an empty map is never published and the last good one stays up. The first deploy therefore waits until the first project has been geocoded. A thin but non-empty map (for example after a bad day with few lookups) is still published.
- The daily run changes the sheet, not the repository, so a quiet repository will hit the 60-day rule.
- Scheduled runs use the workflow file on the default branch. The daily workflow itself has no push or pull-request trigger. A second workflow, `.github/workflows/tests.yml` ("Tests"), runs the test gate and the suite on every push and pull request to `main`, with no secrets, so a broken commit is noticed when it is pushed, not at the next morning's run. The daily run still repeats both checks before using any credential.

## Cleaning up duplicate rows

`scripts/dedupe_sheet.py` merges duplicate rows in the `Registry_Projects` tab, for example a hand-entered seed row and the registry row for the same project. It uses the same matching and merging rules as the daily run, and it never touches another tab.

```
python -m scripts.dedupe_sheet                 # dry run: prints what would be merged, writes nothing to the sheet
python -m scripts.dedupe_sheet --apply         # writes the cleaned tab
python -m scripts.dedupe_sheet --restore backups\Registry_Projects-<time>.json --apply
```

What it guarantees:

- It is a dry run unless you pass `--apply`.
- Before any write it saves the whole tab under `backups/` (git-ignored, because it holds live sheet data; `--backup-dir` changes the place) and checks the file reads back. If the backup cannot be written, nothing is changed.
- It refuses to proceed if any row in the tab cannot be read, if a cluster would hold two different registry ids or two emirates, or if it would remove more than half of the rows (override with `--allow-large-removal` after reading the report).
- A dry run still saves the backup file locally (the sheet is not touched).
- Just before `--apply` or `--restore` writes, it reads the tab again and refuses (exit 2, nothing written) if the tab changed since its first read, so a row someone typed in the meantime is never deleted.
- After writing it reads the tab back and compares the project keys; a mismatch exits non-zero and names the backup.
- It needs `SPREADSHEET_ID` and never creates a spreadsheet. It is meant to be run on your own machine, so its console output may name projects; the one-line description of an unreadable row never echoes the cell, secret values are scrubbed from every message, and an error it did not plan for is printed as its class and where it happened (`ClassName (at file:line in function)`), never its text.

Exit codes: 0 done or nothing to do, 1 failed (read the message), 2 refused by a safety check. Do not run it while the daily job is running.

## The map

`site/index.html` is a static page (Leaflet with marker clustering, OpenStreetMap tiles) that reads `site/pins.geojson`. `python -m scripts.export_map_pins` builds that file from the sheet; it is git-ignored and regenerated on every run. `--out PATH` changes the file, and `--min-pins N` makes it fail, and keep the old file, when fewer than N pins come out. The export needs `SPREADSHEET_ID` and exits 1 without it; `--allow-sheet-by-name` lifts that and lets the sheet code look up, or create, a sheet by name, which you almost never want.

**Which rows become pins.** A row is drawn only if it has both coordinates, the point is inside the coarse box that holds both emirates, the registry gave an emirate that is exactly Dubai or Sharjah, and the point lies strictly inside that emirate's boundary polygon (a point exactly on the boundary line is inside neither). The export is the last stop before the public file, so this applies to a geocoder result, a `Geocode_Overrides` pin and a hand-typed coordinate alike, and the emirate is never guessed from the point. A row that fails is counted by reason in the file's `meta.dropped` block (`no_coordinates`, `half_written_coordinates`, `out_of_range`, `unknown_emirate`, `outside_emirate`), as is a row that shares its project key with a better-placed row (`duplicate_key`). The log gives these counts and never names the rows.

**What a pin publishes.** A fixed list of properties only: the project's name (also in Arabic), developer, area, master project, registry and registry number, status, percent completed, completion date, units, precision, and the listing facts. It never carries `Entered By`, permit numbers, escrow agents, payment plans or source licences. Prices, bedrooms, property types, handover and `As Of` are published only together with a publishable source URL, because a fact is shown only with its source. A price above AED 10 billion, a price per sq ft above AED 100,000 (an extra digit, or a total typed into the wrong column) or a bedroom count above 20 is left out, and a range that runs backwards (price from above price to, bedrooms min above bedrooms max) loses both ends.

**Contact data.** The handover note is the only analyst-typed text exported. It is left out if it is longer than 60 characters or looks like contact data: an email, a link or anything written like a host name, or a phone number (9 or more digits in a run, or 7 or more after a `+`). The check runs after Unicode folding, so a full-width `@` or circled digits do not hide one. The source link is cleaned first: a username and password in it are cut out, and so are query parameters named like a secret or a person (`token`, `key`, `phone`, `agent`, `email` and similar) or whose value is an address, a chat link or an international number. The link is left out whole, and the facts it supports go with it, when it is a chat link (WhatsApp, Telegram and similar) in the host or the path, when its path holds a phone-number-sized run of digits (9 or more) or an address, when its host is not one a visitor on the internet can reach (`localhost`, a name with no dot, a `.local` name, a private address), or when it is not a plain http(s) link of at most 2000 characters.

**Known limit: free text.** The export cannot recognise the names of people, or digits spelled out in words, in free text, and the contact filter above is applied to the handover note only. Free text in the sheet is therefore reviewed by an analyst before the map is published. Registry names, developers and areas are published as the registry gave them, with control characters removed and a length cap.

Precision is always visible: an exact pin is a solid marker, and a community or area pin is a hollow one whose popup says the location is approximate. A pin with coordinates but no recorded precision is exported as `unknown` and drawn as approximate as well.

**Attribution.** Map tiles, the emirate boundaries used to check pins, and geocoding results come from OpenStreetMap, which is available under the Open Database Licence (ODbL). Any published copy of the map must keep the "OpenStreetMap contributors" credit visible. Dubai Municipality boundary data is not used, because public use needs its written approval.

## Open decisions (not code)

The owner must settle these; none can be done by the scraper.

1. Register for the Data.Dubai API and request access to the "Real Estate Projects" dataset. Approval time is unknown.
2. Get written clarification of the Dubai Land Department data licence, ideally before the first public deploy, and in any case before any commercial use. The catalogue says "Not Specified" and the site terms lean non-commercial. Deciding to publish does not answer this; going ahead without it is the owner's risk.
3. Decide whether to test the Sharjah list with one supervised request, and who maintains the seed file.
4. Choose a licensed source for prices, bedrooms and handover dates, after checking that its terms allow public display.
5. **Decided (27 October 2026): publish the map publicly on GitHub Pages once real data flows.** What is left is the owner's to do: set the Pages source to "GitHub Actions" and create the `DEPLOY_MAP` variable, as in `setup_google_sheets.md`, section 10.

## Project layout

```
src/
  config.py            settings read from the environment (names only in git)
  models.py            registry rows, identity keys, matching and merging
  areas.py, emirates.py, listing_data.py   area names, emirate boundaries, listing parsers
  sources/             one adapter per data source
  gsheet_manager.py    all Google Sheet access, by header name
  geocoder.py          geocoding with validation and a per-run budget
  log_policy.py        public-log rules: own loggers only, secret scrubbing, uncaught-error hook
  main.py              the daily run
  data/                bundled data (area aliases, emirate boundaries, seed files)
scripts/
  check_test_gate.py   the CI check that runs before the tests (see "The daily run")
  dedupe_sheet.py      clean-up tool described above
  export_map_pins.py   builds site/pins.geojson
  fetch_emirate_polygons.py   one-off helper that refreshes the bundled boundaries
site/                  the map page
tests/                 the test suite (invented projects, keys and rows; it also reads the bundled boundary file and a table of public place coordinates)
docs/                  design contract and research notes
.github/workflows/     the daily run
```

## Licence

The owner has not chosen an open-source licence for the code yet. Until one is added, all rights remain with the repository owner and nobody is granted any right to copy, change or reuse the code. Third-party libraries keep their own licences. Data licences are separate and are covered under "Open decisions" above.

One file is not the owner's to license: `src/data/emirate_polygons.geojson` (the Dubai and Sharjah boundaries used to check every pin) is derived from OpenStreetMap relations 3766483 and 3766486. It is (c) OpenStreetMap contributors, under the Open Database Licence (ODbL) 1.0 (https://www.openstreetmap.org/copyright), and it stays under that licence whatever licence the code gets; keep that credit with it. `scripts/fetch_emirate_polygons.py` rebuilds it. The table of public place coordinates in `tests/test_emirates.py` (the OSM-derived test coordinates) carries the same credit and the same licence: it is used only to check that the boundaries put real places in the right emirate, it is not registry data and is not published. Every named place in it is an OpenStreetMap feature, looked up by name through the Overpass API on 25 October 2026, and each row keeps its OSM element id so it can be looked up again; the only rows without one are five round-number points (open water, latitude and longitude 0, empty desert) that come from no source. No row was copied from a property portal, an encyclopedia, a search engine or a geocoding service. The expected emirate of each Dubai or Sharjah row is the plain fact about the place, not the output of the boundary code; all fifty of those rows were also checked once, by hand, with Overpass `is_in`, which reads the same OSM relations the boundaries were built from, so the table tests the boundary code and its simplification, not OSM's border, and that border has not been audited against official survey data. The credit and the method are in that file's docstring.
