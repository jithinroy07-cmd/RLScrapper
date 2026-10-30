# Setting up Google Sheets and the repository

One-time setup for the owner. It creates the Google Cloud service account that the daily job uses, the spreadsheet it writes to, and the repository secrets and variables that connect them.

This is a separate project with its own Google Cloud project, service account and spreadsheet. Do not reuse the ones from any other project.

**Never paste a key, a token, the service-account JSON or the spreadsheet id into a chat, an issue, a commit, a pull request or a log.** This guide uses placeholders in angle brackets (`<SPREADSHEET_ID>`) on purpose. Where a value is needed, you paste it straight into the GitHub or `.env` field yourself.

## Before the first live run

The repository is public, and so are its Actions logs and artifacts. Go through this list once, in order, before the first real run. The numbered sections below have the detail.

1. **Create the secrets and variables by name** (Settings, then "Secrets and variables", then "Actions"; sections 4 and 5 give what each one is). Names only here; every value you paste yourself.
   - Needed for a first run: the secrets `GCP_SERVICE_ACCOUNT_KEY` and `SPREADSHEET_ID`.
   - When the Data.Dubai key is issued: the secrets `DATA_DUBAI_API_KEY` and `DATA_DUBAI_API_SECRET`.
   - Optional secrets: `GEOCODE_CONTACT`, `GEMINI_API_KEY`, `LOCATIONIQ_API_KEY`, `GEOAPIFY_API_KEY`, `GOOGLE_MAPS_API_KEY`, `SHARE_WITH_EMAILS`.
   - Variables: none is needed for a first run. `REQUIRE_SOURCES` comes in step 2; the rest are listed in section 5.
2. **Get the Data.Dubai key, then dry-run it locally, then require it.** Register for the Data.Dubai API and request access to the "Real Estate Projects" dataset (approval time is unknown). When it arrives, and once sections 1 to 3 are done, put the key and secret in your local `.env` only, run `python -m src.main --dry-run` on your machine, and read the log: the live column names have never been checked, so expect to fix something on this first live contact. Only when that shows rows, add the two secrets in GitHub, run the workflow once by hand, and then create the variable `REQUIRE_SOURCES` with the value `dld_projects`, so that a missing or empty Dubai feed fails the run instead of passing it.
3. **Never turn on GitHub's debug logging.** Do not create a secret or variable named `ACTIONS_STEP_DEBUG` or `ACTIONS_RUNNER_DEBUG`, and do not tick "Enable debug logging" when you re-run a job. Debug output is not under this project's control, and the log is public.
4. **Leave `DEPLOY_MAP` and `UPLOAD_SNAPSHOT` unset for now.** They are off until you create them. Hosting the map publicly on GitHub Pages is decided, so `DEPLOY_MAP` is the switch you will turn on later, in section 10, once real data is on the sheet. `UPLOAD_SNAPSHOT` is not part of that decision (it makes the raw JSON downloadable by anyone), so leave it unset. The Dubai Land Department data licence is still unclarified: getting it in writing before you set `DEPLOY_MAP` is recommended (README, "Open decisions").
5. **Read section 9, "Hardening you may want".** Nothing in it is implemented; it lists the choices that are still yours.

## 1. Create the Google Cloud project and service account

1. In the Google Cloud console, create a new project for this scraper.
2. Enable two APIs for it: the **Google Sheets API** and the **Google Drive API**. (Drive is needed only when the job has to create and share a sheet; with `SPREADSHEET_ID` set it asks for Sheets access alone.)
3. Open "IAM and admin", then "Service accounts", and create a service account. It needs no project role.
4. Open the new service account, then "Keys", then "Add key", then "Create new key", type JSON. The browser downloads one file. That file is the secret `GCP_SERVICE_ACCOUNT_KEY` (how to enter it is in sections 3 and 4).
5. Note the service account's email address (it ends in `iam.gserviceaccount.com`). You need it in step 2.

Keep the downloaded file out of the repository. `.gitignore` already excludes `service_account*.json`, but the safest place for it is outside the project folder.

## 2. Create the spreadsheet and share it with the service account

1. In Google Sheets, create an empty spreadsheet. Name it `UAE Real Estate Projects Tracker`, the built-in default title. The daily run opens the sheet by `SPREADSHEET_ID`, so the title matters only if the job ever has to look a sheet up by name; CI always uses the default title (a different one can be set with `GOOGLE_SHEET_NAME` in a local `.env` only, see section 5).
2. Share it with the service account's email address, with the **Editor** role. Turn off the notification email.
3. Copy the spreadsheet id: the long token in the browser address between `/d/` and `/edit`. That is the secret `SPREADSHEET_ID`. (If the whole address is pasted instead, the run takes the id out of it and logs a warning that names the setting; a wrong id is answered by Google with HTTP 400 or 404 at "open the configured spreadsheet".)

Tabs are owned either by the machine (the daily run rewrites them) or by people (the run never overwrites them); the README, "What each tab is for", has the table. In short: do not hand-edit `Map_Pins` or `Scrape_Run_Logs`; put pins in `Geocode_Overrides`; and expect a hand edit to a cell the registry feed fills in `Registry_Projects` to be reverted by the next run.

Always set `SPREADSHEET_ID`. If it is missing, the job **creates a new spreadsheet** named `UAE Real Estate Projects Tracker` (or, in a local `.env`, whatever `GOOGLE_SHEET_NAME` says), which is almost never what you want after the first day. The clean-up tool and the map export refuse to run without it for the same reason.

The job creates the six tabs (`Registry_Projects`, `Listing_Data`, `Launch_Candidates`, `Map_Pins`, `Geocode_Overrides`, `Scrape_Run_Logs`) and their header rows on the first run, and adds any missing column later. It never reorders or drops one. Treat the sheet as private: share it only with people who should see registry and pricing data.

## 3. Fill in `.env` for local runs

Copy `.env.example` to `.env` (git-ignored) and set at least:

```
SPREADSHEET_ID=<SPREADSHEET_ID>
GCP_SERVICE_ACCOUNT_FILE=<path to the downloaded key file, relative to the repository root>
```

`DATA_DUBAI_PROJECTS_FILE=<path to a downloaded copy of the DLD projects dataset>` is a third, optional local-only line: with it the Dubai source reads the file instead of the API. `GCP_SERVICE_ACCOUNT_FILE` is for local runs only. The key can instead go into `GCP_SERVICE_ACCOUNT_KEY` in `.env`, but there it must be on ONE line: join the downloaded file's lines into one and leave the `\n` sequences inside the private key as they are (a `.env` line cannot hold line breaks). If `GCP_SERVICE_ACCOUNT_KEY` is set it wins, and a broken value is an error: the run does not fall back to the file. In GitHub Actions the secret `GCP_SERVICE_ACCOUNT_KEY` is the whole downloaded JSON file, pasted as is (section 4).

## 4. Add the repository secrets

In the repository, open Settings, then "Secrets and variables", then "Actions", then "New repository secret". Create these by name. Only the first two are needed for a first run.

| Secret | What it is | Needed for |
|---|---|---|
| `GCP_SERVICE_ACCOUNT_KEY` | The whole service-account JSON file, pasted as is (line breaks included; only a local `.env` needs it on one line, see section 3). If pasting keeps failing with "is not valid JSON", paste the file's base64 encoding instead (`base64 -w0 key.json` on Linux or macOS, `[Convert]::ToBase64String([IO.File]::ReadAllBytes('key.json'))` in PowerShell): the run decodes it. A byte-order mark, surrounding whitespace or one pair of quotes around the whole file are tolerated as well. The error says only how long the value was and whether it began with `{`, never its text. | Reading and writing the sheet |
| `SPREADSHEET_ID` | The id from step 2 | Reaching the existing sheet |
| `DATA_DUBAI_API_KEY` | Data.Dubai API key | Dubai registry source (not issued yet) |
| `DATA_DUBAI_API_SECRET` | Data.Dubai API secret | Dubai registry source (not issued yet) |
| `GEMINI_API_KEY` | Gemini key | Only if news discovery is switched on |
| `LOCATIONIQ_API_KEY` | LocationIQ key | Optional geocoder; the default needs none |
| `GEOAPIFY_API_KEY` | Geoapify key | Optional geocoder; the default needs none |
| `GOOGLE_MAPS_API_KEY` | Google Maps key | Only if you choose that backend, which is never picked automatically |
| `SHARE_WITH_EMAILS` | Comma-separated addresses to share a newly created sheet with | Only when the job creates the sheet |
| `GEOCODE_CONTACT` | A way to reach whoever runs this (an email address or a web page), added to the identification the public geocoder sees. Public geocoders ask for one. | Optional. It is a secret, not a variable, because a variable's value is printed in the public job log and a secret is masked. It is the only way CI sets the contact: the User-Agent text itself (`GEOCODE_USER_AGENT`) is a local-only setting (section 5). |

An empty or missing secret counts as "not set". A source whose credentials are missing is skipped with a log line, not failed, unless you list it in `REQUIRE_SOURCES`.

## 5. Add the repository variables (all optional)

On the same page, open the "Variables" tab. A variable you do not create keeps its built-in default (`src/config.py` owns the defaults).

**Every Variable is printed in the public log: put anything personal in a Secret.** GitHub shows the value of each repository variable a step receives in that step's header, and masks only secrets. The variables below hold switches, numbers, fixed choices, a model name and source names, none of them personal, which is why they are safe as variables.

| Variable | Purpose |
|---|---|
| `ENABLE_SRERD` | `true` turns on the Sharjah list source. Off by default; it is unverified. |
| `ENABLE_GEMINI_NEWS` | `true` turns on news discovery into `Launch_Candidates`. Off by default. |
| `GEMINI_MODEL` | The model name for news discovery. No default is built in; set it when you enable the feature. |
| `REQUIRE_SOURCES` | Comma-separated source names that must succeed, for example `dld_projects`. A required source that is skipped or switched off fails the run, and a name this scraper does not have stops the run before any work (exit 2). Set this once the Dubai key works. |
| `ALLOW_ROW_DROP` | Leave unset or `false`. `true` lets a source return fewer than 80 percent of its previous row count without failing the run. An empty result is never excused. |
| `GEOCODE_ENABLED` | `false` stops geocoding. On by default. |
| `GEOCODE_BACKEND` | `nominatim`, `locationiq`, `geoapify` or `google`. Blank picks automatically and never picks `google`. |
| `GEOCODE_MAX_PER_RUN` | Cap on lookups per run. `0` means the backend's own limit. |
| `GEOCODE_RETRY_COOLDOWN_DAYS` | Days before a project that failed to geocode is tried again. |
| `GEOCODE_TIME_BUDGET_SECONDS` | Stops geocoding after this long, so the job stays inside its 90-minute limit. Default 3300 (55 minutes: room for 200 paced lookups). A larger value is cut down to 4500 (75 minutes), and the log says so by name without repeating what you typed. |
| `GEOCODE_FLUSH_EVERY` | Saves progress to the sheet after this many resolved projects. |
| `SHARE_ROLE` | `reader`, `commenter` or `writer`, for `SHARE_WITH_EMAILS`. |
| `SHARE_NOTIFY` | `true` sends Google's notification email when sharing. |
| `UPLOAD_SNAPSHOT` | `true` uploads the JSON snapshot as a workflow artifact. **Off by default: artifacts of a public repository can be downloaded by anyone.** |
| `DEPLOY_MAP` | `true` publishes the map to GitHub Pages. **Off by default.** Public Pages hosting is decided; you set this once real data flows (section 10). |

**Two settings are local-only and are not variables at all.** `GOOGLE_SHEET_NAME` (the title used when the job has to create or look up a spreadsheet by name) and `GEOCODE_USER_AGENT` (the text that identifies this tool to the public geocoder) are free text that could carry a business name or a contact address, so the workflow does not pass them. CI uses the built-in defaults, and creating a repository variable with either name changes nothing. A local `.env` can set both (`.env.example` lists them). To give the geocoder a way to reach you in CI, use the contact secret from section 4.

## 6. Check it works

Optional first: with `.env` filled in (step 3), run `python -m src.main --dry-run` on your machine. It reads the sheet and the sources and writes nothing (see the README for the flags and the exit codes). It needs `SPREADSHEET_ID`, and opening the sheet may still add a missing tab or header column.

Then on GitHub:

1. Open the Actions tab, choose "Daily UAE Real Estate Scraper", and press "Run workflow".
2. Read the log. It prints which settings are set (a key or other secret, the spreadsheet id and the contact are shown only as set or not set, and free text such as the sheet name or the User-Agent appears only as `default` or `custom (not shown)`), a row count per source, geocoding totals and the exit status. A setting that is not a valid yes/no, number or choice is reported by its name only, with the default that is used instead; the log never repeats what you typed. The log holds only this project's own messages: whatever a library writes (request addresses, headers, a service's own error text) is dropped at every level, and the service account's email, project id and key ids are scrubbed from the rest.
3. Open the sheet. The tabs and header rows should exist, and `Scrape_Run_Logs` should have one new row.

Until the Data.Dubai key is issued, the Dubai source logs `SKIPPED (not configured: ...)`, naming the settings that are missing, and the registry stays empty (apart from any seed rows). That is expected, and it is why `REQUIRE_SOURCES` matters later.

To publish the map, follow section 10 (Pages source, then `DEPLOY_MAP`). Do that after this check has passed and the sheet holds real projects.

## 7. If something goes wrong

- **The run fails at the "Check the test gate" step.** Nothing after it ran and no credential was used. The step prints lines that start `test gate: `, made of fixed phrases, counts and file names only (the public log never carries a line of a file or pytest's own output), and `python -m scripts.check_test_gate` prints the same lines on your machine. What they mean:
  - `pytest.ini is missing`, `pytest.ini cannot be read`, `pytest.ini has no [pytest] section`, `pytest.ini line N is not one the gate accepts`, `pytest.ini does not set testpaths to tests` (or `pythonpath` to `.`), `pytest.ini addopts holds something other than -q`: the file was changed. Put back `[pytest]`, `testpaths = tests`, `pythonpath = .` and `addopts = -q` (the `addopts` line may be left out; anything else, or a name given twice, is refused).
  - `conftest.py exists (N found)`, or the same for `pyproject.toml`, `tox.ini`, `setup.cfg`, `pytest.toml`, `.pytest.toml` or `.pytest.ini`: delete the file. Any of them can change which tests run, skip them, or (a `pytest.toml`, even an empty one) make pytest ignore `pytest.ini`. N counts the files anywhere in the checkout outside `.git` and `.venv`; the message does not say where they are.
  - `N PYTEST_* environment variable(s) set`: something sets a variable whose name starts `PYTEST_`, for example `PYTEST_ADDOPTS`, which can make pytest run nothing. Look in the workflow file, or in your own shell if you ran the check there. The message names neither the variable nor its value.
  - `N test modules found under tests, at least 23 expected` or `N tests collected, at least 5200 expected`: test files were deleted or no longer load. Restore them. If the suite has really grown, raise the floors (`MIN_TEST_MODULES` and `MIN_COLLECTED_TESTS` in `scripts/check_test_gate.py`); lowering them is a change for a reviewer, not a way to get a green run.
  - `the collect-only run failed (exit code N)`, `the collect-only run did not finish within 600 seconds` or `the collect-only run printed no test count the gate can read`: pytest could not even list the tests (an import error in a test or in the code, a broken install, a hang). Run `python -m pytest --collect-only -q` on your machine to see the real error, which the public log deliberately does not carry.
  - `the gate itself failed (ClassName)`: the check hit an error of its own. Run it on your machine.
- **The run fails at the test step.** The gate passed, so this is a failing test and not a missing suite. Nothing else ran and no credential was used. Read the failing test.
- **Authorisation or "permission denied" errors.** The log gives the HTTP status and Google's fixed status word, for example `HTTP 403 PERMISSION_DENIED`, never Google's message text. The usual causes: the sheet is not shared with the service account as Editor, `SPREADSHEET_ID` is wrong, or the Sheets or Drive API is not enabled for the project. The log does not print the service account's address or any other identifier from the key (the log is public); read `client_email` in the key file, or find the address in the Cloud console.
- **"... is not valid JSON" or "... is missing field(s)" about the key.** The key is incomplete or was altered. In GitHub Actions paste the whole downloaded file; in a local `.env` put it on one line (section 3). The message names the problem, never the key text.
- **A new, empty spreadsheet appeared.** `SPREADSHEET_ID` was not set for that run, so the job created one. It belongs to the service account: you see it under "Shared with me" only if `SHARE_WITH_EMAILS` listed your address, otherwise only the service account can open it. Fix the secret; the stray sheet can be ignored or, if you can see it, deleted.
- **The log says creating the spreadsheet failed with a server error, but Google may have created it.** Without a `SPREADSHEET_ID` the job creates the sheet once and does not retry, so a lost reply could leave a sheet only the service account can open. Look for a sheet named `UAE Real Estate Projects Tracker` (the title CI always uses) in the service account's Drive, share it with yourself as Editor by hand, copy its ID into the `SPREADSHEET_ID` secret, and run again. Better still: create the sheet yourself as in section 2, so the job never has to.
- **429 or quota errors.** The job backs off and retries. A geocoding run that stops on a quota with projects still waiting is logged and is not an error; they are picked up next time.
- **The daily run stopped appearing.** GitHub disables scheduled workflows after 60 days without repository activity. Re-enable it in the Actions tab.
- **A warning like `Scrape_Run_Logs row 7: entry 2 of the Sources cell is unreadable; ignored`.** Someone edited the `Sources` cell of an old run-log row by hand. The log gives the row number and the position of the entry, never the source name. The run skips that entry when it looks for each source's last good row count; restore the cell or leave it, nothing else is affected.
- **`ClassName (at file.py:line in function)` in the log.** That is how the log reports an error the code did not plan for: the class and the place it came from, never the message or the chain of causes, which can carry a request address or a project name. On a line of its own at the end of a run (without the usual timestamp, level and logger prefix) it is an exception that nothing caught, for example Ctrl-C on your machine or a cancelled run. The same text appears when you run the command on your machine; to see the whole error there, reproduce it under a debugger. Do not paste a full traceback into an issue before checking it for keys and project names.
- **Duplicate rows in `Registry_Projects`.** Use `scripts/dedupe_sheet.py` (README, "Cleaning up duplicate rows"). It does a dry run unless you pass `--apply`, and saves a backup first.

## 8. If a key leaks

Treat any secret that appeared in a chat, an issue, a log or a commit as exposed. Revoke it at its provider, create a new one, and replace the repository secret. For the service account, delete the leaked key in the Cloud console and create another. Rewriting git history does not make an exposed key safe again.

## 9. Hardening you may want

These are choices for the owner. The repository side is in place where it could be (a Tests workflow on every push and pull request, a Dependabot configuration, a `CODEOWNERS` file); **the repository settings and the lock file below are not**. The daily run works without them; they are listed so that nobody assumes they are already in place. `docs/DESIGN.md`, section 8, has the technical detail.

1. **Pin the dependencies.** `requirements.txt` only sets minimum versions, there is no lock file, and the install step runs in the same job that later holds the secrets. Dependabot is already configured for `pip` and `github-actions` (`.github/dependabot.yml`), so each bump arrives as a pull request that the Tests workflow checks. To close the rest, produce a hashed lock file with `pip-compile --generate-hashes` and install it with `pip install --require-hashes -r` in the workflow. The workflow check in `tests/test_workflow.py` accepts only the current install line, so it must be changed in the same commit. A pytest plugin that arrives through `requirements.txt` is also outside what the test gate can see (it cannot tell that a plugin skips tests at run time), so a change to that file deserves the same review as the code.
2. **Limit who can reach the secrets, and who can change the checks.** Repository secrets are readable by any branch that someone with write access runs. The test gate (`scripts/check_test_gate.py`), `pytest.ini`, the tests and the workflow file live in the repository they guard, so one pull request can weaken them together, and only a reviewer notices. Protect `main` with a ruleset (no direct pushes, reviewed pull requests) and keep the list of collaborators short. The `.github/CODEOWNERS` file already names you for `.github/`, `scripts/check_test_gate.py`, `pytest.ini`, `requirements.txt` and `tests/`; switching on "Require review from Code Owners" in the ruleset makes that review mandatory. To go further, move the secrets into a GitHub Environment that only `main` may use and name it in the scrape job; the workflow check must then be told, on purpose, that the job may carry an `environment`.
3. **Actions are pinned to major version tags, not to commit SHAs.** A tag can move. This is an accepted risk for now; Dependabot (configured) makes each bump a reviewed change.
4. **A keyed geocoder brings its own terms to the map.** If you turn on `DEPLOY_MAP` while LocationIQ, Geoapify or Google answers the lookups, that provider's attribution and terms apply to the published map. The map credits OpenStreetMap only, and Google's terms restrict showing its results on a map that is not Google's, so do not deploy a map while Google is the backend.
5. **Your first commits publish your name and email.** Use the noreply address that GitHub offers in the account's email settings (and tick the option that blocks pushes which would expose a private address) before the first push.

## 10. Publish the map on GitHub Pages (the owner's checklist)

**Decided on 27 October 2026: the map is hosted publicly on GitHub Pages.** The repository is public, so Pages is free. Nothing is published until you finish Part B, and Part B should wait until Part A has put real projects on the sheet. Every value you paste yourself, into GitHub or a local `.env`; none goes into a chat, an issue or a commit.

### Part A. Get real data flowing (nothing is public yet)

1. **Google Cloud and the sheet.** Do sections 1 and 2: the project, the Sheets and Drive APIs, the service account and its JSON key, and the spreadsheet `UAE Real Estate Projects Tracker`, shared with the service account as **Editor** with the notification email turned off.
2. **The two secrets a first run needs.** Create `GCP_SERVICE_ACCOUNT_KEY` and `SPREADSHEET_ID` (section 4). Add `GEOCODE_CONTACT` as well, so the public geocoder can reach you.
3. **Data.Dubai.** Register for the Data.Dubai API and request access to the "Real Estate Projects" dataset (the approval time is unknown). While you wait, a signed-in user can download the dataset from its portal page as a JSON file and load it from your own machine with `DATA_DUBAI_PROJECTS_FILE` in `.env` (README, "Loading the official download by hand"); that fills the sheet with the real register today, and the daily run then keeps geocoding it. When the key arrives, put it in your local `.env` only and run `python -m src.main --dry-run`. Read the log: the live column names have never been checked. Only when it shows rows, create the secrets `DATA_DUBAI_API_KEY` and `DATA_DUBAI_API_SECRET`.
4. **First live run.** In the Actions tab, run "Daily UAE Real Estate Scraper" by hand (section 6). Then create the variable `REQUIRE_SOURCES` with the value `dld_projects`, so that a missing or empty Dubai feed fails the run.
5. **Wait for pins.** The Dubai registry has no coordinates; pins come from geocoding. The default geocoder (public OpenStreetMap) does at most 200 lookups a run, which is fewer than 200 projects a day, so a large registry takes weeks to appear. `LOCATIONIQ_API_KEY` or `GEOAPIFY_API_KEY` is much faster. Do not choose the Google backend for a map that will be published (section 9, item 4). Open the `Map_Pins` tab: when it holds rows, Part A is done.

### Part B. Publish

1. **Recommended first: the data licence.** The Dubai Land Department catalogue says "Not Specified". Getting its position in writing before you publish is advised (README, "Open decisions"). Publishing without it is your decision and your risk; nothing in the code settles it.
2. **Know what becomes public.** Each pin carries the project name (also in Arabic), developer, emirate, area, registry and registry number, status, completion date, percent complete, units, coordinates and how precise they are. Prices, bedrooms, property types and handover appear only for a project that has a `Listing_Data` row with a `Source URL`. No phone number, email address, permit number, escrow agent or payment plan is exported. Free text typed into the sheet (the handover note) is filtered for contact details but not for names of people, so have an analyst read the `Listing_Data` tab first (README, "The map").
3. **Turn Pages on, once.** Open Settings, then "Pages", and under "Build and deployment" set the source to **GitHub Actions**.
4. **Create the switch.** Open Settings, then "Secrets and variables", then "Actions", then the "Variables" tab, and create the repository variable `DEPLOY_MAP` with the value `true`. Leave `UPLOAD_SNAPSHOT` unset.
5. **Run the workflow by hand.** In the Actions tab, run "Daily UAE Real Estate Scraper". Two jobs should go green: "Test, scrape and sync to the sheet" and "Publish the map to GitHub Pages". If the export step fails because fewer than one pin was found, no project has been located yet: go back to Part A, step 5. The last published map, if any, stays up.
6. **Open the map.** The address is `https://<owner>.github.io/<repository>/`; the publish job's summary links to it. Reload with the cache cleared, check that the "OpenStreetMap contributors" credit shows, and click a few pins. A hollow marker means an approximate location.
7. **From then on** every daily run refreshes the map after it has tested, scraped and exported successfully. A run that fails leaves the previous map in place.

### Taking the map down

Change `DEPLOY_MAP` to anything other than `true`, or delete the variable: the next run no longer publishes. That does not remove the page that is already live, so also open Settings, then "Pages", and unpublish the site there. Pins that were once public may have been copied by others; unpublishing cannot undo that.

### Keep the log clean

Never create `ACTIONS_STEP_DEBUG` or `ACTIONS_RUNNER_DEBUG`, as a secret or a variable, and never tick "Enable debug logging" when re-running a job (item 3 of "Before the first live run" and the README explain why). The log of a public repository is public.
