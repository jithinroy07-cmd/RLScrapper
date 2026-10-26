"""
Tests for .github/workflows/daily_uae_real_estate.yml and the documents that
describe it.

A workflow cannot be run here (no runner, no network, no secrets), so the file
is checked as data. `workflow_problems` is a pure function that turns the parsed
YAML into a list of problems; the real file must produce none, and a long list
of deliberately broken copies must each produce the matching problem, which is
what proves the checker can fail.

The rules it enforces come from docs/DESIGN.md sections 8, 9 and 10: a failing
test blocks the run, secrets reach only the two steps that need them, nothing is
published unless the owner switches it on with a repository variable, and every
permission is the least the job needs.

The checker is an allowlist, not a list of known-bad commands: every step must be
one of the known steps with its known command, action, options and environment
names, so a step nobody thought of (a second pip install, a printf into
$GITHUB_ENV, a wrapped shell, a PYTHONPATH) is a problem by itself. Adding a step,
an action or an environment name to the workflow therefore means adding it to the
constants below, on purpose.

Everything below is SYNTHETIC or a name only; no real value appears here.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "daily_uae_real_estate.yml"
DESIGN_PATH = ROOT / "docs" / "DESIGN.md"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
README_PATH = ROOT / "README.md"
SETUP_PATH = ROOT / "setup_google_sheets.md"

WORKFLOW_NAME = "Daily UAE Real Estate Scraper"
SCRAPE_JOB = "scrape-and-sync"
DEPLOY_JOB = "deploy-map"
SNAPSHOT_PATH = "scraped_projects_latest.json"

# docs/DESIGN.md section 9, by name. A test below compares these with the design
# text, so the two cannot drift apart unnoticed.
SECRET_NAMES = frozenset(
    {
        "GEMINI_API_KEY",
        "GCP_SERVICE_ACCOUNT_KEY",
        "SPREADSHEET_ID",
        "DATA_DUBAI_API_KEY",
        "DATA_DUBAI_API_SECRET",
        "LOCATIONIQ_API_KEY",
        "GEOAPIFY_API_KEY",
        "GOOGLE_MAPS_API_KEY",
        "SHARE_WITH_EMAILS",
        "GEOCODE_CONTACT",
    }
)
WORKFLOW_ONLY_VARIABLES = frozenset({"UPLOAD_SNAPSHOT", "DEPLOY_MAP"})
# Free-text settings that could name the business or carry a contact address. GitHub prints every
# non-secret value (each vars.* one) in the step header of a public log, so the workflow never passes
# them: CI uses the defaults in src/config.py (the contact comes from the GEOCODE_CONTACT secret) and
# only a local .env can set them.
LOCAL_ONLY_SETTINGS = frozenset({"GOOGLE_SHEET_NAME", "GEOCODE_USER_AGENT"})
# The repository variables the workflow may reference.
VARIABLE_NAMES = frozenset(
    {
        "GEMINI_MODEL",
        "ENABLE_SRERD",
        "ENABLE_GEMINI_NEWS",
        "REQUIRE_SOURCES",
        "ALLOW_ROW_DROP",
        "GEOCODE_ENABLED",
        "GEOCODE_BACKEND",
        "GEOCODE_MAX_PER_RUN",
        "GEOCODE_RETRY_COOLDOWN_DAYS",
        "GEOCODE_TIME_BUDGET_SECONDS",
        "GEOCODE_FLUSH_EVERY",
        "SHARE_ROLE",
        "SHARE_NOTIFY",
    }
) | WORKFLOW_ONLY_VARIABLES
# The export step reads the sheet and nothing else.
EXPORT_SECRETS = frozenset({"GCP_SERVICE_ACCOUNT_KEY", "SPREADSHEET_ID"})

# The checker is an ALLOWLIST: whatever is not listed here is a problem, so a step nobody thought of
# cannot slip through the way it could past a list of known-bad commands.
# Environment names a step may be given. Any other name, a harmless-looking one too, is refused:
# PYTHONPATH, PATH, LD_PRELOAD, PIP_INDEX_URL and their kind decide which code the next command loads.
SCRAPE_ENV = SECRET_NAMES | (VARIABLE_NAMES - WORKFLOW_ONLY_VARIABLES)
EXPORT_ENV = EXPORT_SECRETS | {"MIN_PINS"}
STEP_ENV = {"scrape": SCRAPE_ENV, "export": EXPORT_ENV}
JOB_ENV = {SCRAPE_JOB: frozenset({"PYTHONUNBUFFERED"})}
MIN_PINS_EXPRESSION = "${{ vars.DEPLOY_MAP == 'true' && '1' || '0' }}"
# The scrape job runs exactly these steps, once each, in this order. The last two are switched on by
# repository variables, so they may be absent; a step of any other kind is refused whatever it runs.
# "gate" is scripts/check_test_gate.py: it fails the job when the "tests" step after it could pass
# without running the suite (a changed pytest.ini, a conftest.py, a PYTEST_* variable, deleted tests).
SCRAPE_STEPS = ("checkout", "setup-python", "install", "gate", "tests", "scrape", "export", "snapshot", "pages-artifact")
REQUIRED_STEPS = SCRAPE_STEPS[:7]
ALLOWED_ACTIONS = frozenset(
    {"actions/checkout", "actions/setup-python", "actions/upload-artifact", "actions/upload-pages-artifact", "actions/deploy-pages"}
)
PAGES_PATH = "site"
# Runs queue in this one group and are never cancelled (a second run waits for the first instead of racing
# it for the sheet or the Pages deployment). Any other name, or an expression such as the run id, would
# give each run a queue of its own.
CONCURRENCY_GROUP = "daily-uae-real-estate"
# The only keys each level may carry, and the only `with:` options each action may be given
# (checkout's repository/ref/path, for one, could swap the code that later runs next to the secrets).
TOP_KEYS = frozenset({"name", "on", True, "permissions", "concurrency", "jobs"})
# The scrape job has no `environment:` on purpose (owner decision): its secrets are repository secrets, and
# an environment would add its own protection rules and secret store. Only the deploy job names one
# (github-pages). Allowing it on the scrape job means adding it here deliberately.
JOB_KEYS = {
    SCRAPE_JOB: frozenset({"name", "runs-on", "timeout-minutes", "env", "steps", "continue-on-error"}),
    DEPLOY_JOB: frozenset(
        {"name", "needs", "if", "runs-on", "timeout-minutes", "permissions", "environment", "steps", "continue-on-error"}
    ),
}
STEP_KEYS = frozenset({"name", "id", "if", "uses", "run", "env", "with", "continue-on-error"})
WITH_KEYS = {
    "checkout": frozenset({"persist-credentials"}),
    "setup-python": frozenset({"python-version", "cache"}),
    "snapshot": frozenset({"name", "path", "retention-days", "if-no-files-found"}),
    "pages-artifact": frozenset({"path"}),
}
# These change how or where a command runs (a wrapped shell can dump the secret environment in a form the
# log masking does not match), so they are refused at every depth.
FORBIDDEN_KEYS = frozenset({"shell", "working-directory", "defaults"})
# One command shape per run step, compared with the whole text. The test gate takes no option: any flag
# could narrow the run (-k, -m, --deselect, --ignore, --collect-only, a path) and still read "python -m
# pytest" at a glance; options belong in pytest.ini. The scrape may only skip geocoding or name the snapshot
# file the upload step collects: --dry-run would make a green run write nothing, --env-file would layer a
# file under the secrets, and -v/--verbose would print project names in a public log.
COMMANDS = {
    "install": (
        re.compile(r"(?:python -m pip install --upgrade pip\n)?pip install -r requirements\.txt"),
        "'pip install -r requirements.txt', optionally after 'python -m pip install --upgrade pip'",
    ),
    "gate": (re.compile(r"python -m scripts\.check_test_gate"), "a plain 'python -m scripts.check_test_gate'"),
    "tests": (re.compile(r"python -m pytest"), "a plain 'python -m pytest'"),
    "scrape": (
        re.compile(rf"python -m src\.main(?: --no-geocode)?(?: --snapshot {re.escape(SNAPSHOT_PATH)})?"),
        f"'python -m src.main', optionally with --no-geocode and --snapshot {SNAPSHOT_PATH}",
    ),
    "export": (
        re.compile(r'python -m scripts\.export_map_pins --out site/pins\.geojson --min-pins "\$MIN_PINS"'),
        "'python -m scripts.export_map_pins --out site/pins.geojson --min-pins \"$MIN_PINS\"'",
    ),
}

_REFERENCE = re.compile(r"\b(secrets|vars)\.([A-Za-z_][A-Za-z0-9_]*)")
# An allowlisted owner at a major version tag (v6, v6.1, v6.1.2) or a full commit; never a branch, 'latest' or a short hash.
_PINNED = re.compile(r"actions/[\w.-]+@(?:v\d+(?:\.\d+){0,2}|[0-9a-f]{40})")
# Belt and braces behind the command shapes above: commands that can print the environment.
_RISKY_COMMAND = re.compile(
    r"printenv|set\s+-x|\becho\b|\bprintf\b|\bcurl\b|\bwget\b|\bcat\b|\beval\b|(?<![\w-])env(?![\w-])"
    r"|\b(?:python3?|bash|sh)\s+-c\b|ACTIONS_STEP_DEBUG"
)
# -v, -vv, -sv, --verbose, --debug: the code's DEBUG lines (project names, queries) are only safe because no step enables them.
_VERBOSE_FLAG = re.compile(r"(?<!\S)(?:-[A-Za-z]*v[A-Za-z]*|--v|--debug)")
# The runner's command files and tokens: writing GITHUB_ENV or GITHUB_PATH poisons every later step.
_RUNNER_HOOKS = re.compile(r"GITHUB_(?:ENV|PATH|OUTPUT|STATE|STEP_SUMMARY|TOKEN)|ACTIONS_\w*", re.I)
# Names that decide which code an interpreter, loader, package manager or runner executes, or which tests the
# test runner runs (PYTEST_ADDOPTS can add --collect-only or --ignore, PYTEST_PLUGINS loads any module): a
# step's own `env` may not set them even when the value looks harmless.
_LOADER_ENV = re.compile(
    r"PYTHON\w*|PYTEST\w*|PIP_\w+|LD_\w+|DYLD_\w+|PATH|BASH_ENV|ENV|BASHOPTS|SHELLOPTS|PS4|IFS|HOME|NODE_\w+|NPM_\w+|PERL\w*|RUBY\w*"
    r"|JAVA\w*|SSL_\w+|\w+_PROXY|\w+_CA_BUNDLE|GITHUB_\w+|ACTIONS_\w+|RUNNER_\w+",
    re.I,
)
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
# The whole secrets context (toJSON(secrets), secrets['NAME'], secrets[var]) or the job token, in any expression.
_BARE_CONTEXT = re.compile(r"\bsecrets\b(?!\.[A-Za-z_])|\bgithub\s*\.\s*token\b|\bGITHUB_TOKEN\b")


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------
def load_workflow(text: str) -> Any:
    """Parse workflow text. Raises yaml.YAMLError for broken YAML."""
    return yaml.safe_load(text)


def _as_map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)


def _references(value: Any) -> set[tuple[str, str]]:
    """Every ('secrets' | 'vars', NAME) mentioned anywhere inside `value`."""
    return {m.groups() for text in _walk_strings(value) for m in _REFERENCE.finditer(text)}


def _expressions(value: Any, key: Any = None) -> Iterator[str]:
    """Every ${{ }} body in `value`, plus the bare text of any `if:` (which needs no wrapper)."""
    if isinstance(value, str):
        if key == "if":
            yield value
        yield from _EXPRESSION.findall(value)
    elif isinstance(value, Mapping):
        for name, item in value.items():
            yield from _expressions(item, name)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _expressions(item)


def _context_problems(doc: Any) -> list[str]:
    hits = sorted({m.group(0) for text in _expressions(doc) for m in _BARE_CONTEXT.finditer(text)})
    return [f"an expression reads {hit!r}: name each secret as secrets.NAME, and never hand over the job token" for hit in hits]


def _steps(job: Mapping) -> list[Mapping]:
    steps = job.get("steps")
    return [s for s in steps if isinstance(s, Mapping)] if isinstance(steps, list) else []


def _role(step: Mapping) -> str:
    """What a step is for, judged by what it runs, so renaming a step does not hide it."""
    uses, run = str(step.get("uses") or ""), str(step.get("run") or "").strip()
    for prefix, role in (
        ("actions/checkout@", "checkout"),
        ("actions/setup-python@", "setup-python"),
        ("actions/upload-artifact@", "snapshot"),
        ("actions/upload-pages-artifact@", "pages-artifact"),
        ("actions/deploy-pages@", "deploy-pages"),
    ):
        if uses.startswith(prefix):
            return role
    if re.match(r"python -m src\.main\b", run):
        return "scrape"
    if run.startswith("python -m scripts.export_map_pins"):
        return "export"
    if run.startswith("python -m scripts.check_test_gate"):
        return "gate"
    if run.startswith("python -m pytest"):
        return "tests"
    if "pip install" in run:
        return "install"
    return "other"


def _label(step: Mapping) -> str:
    return str(step.get("name") or step.get("uses") or str(step.get("run") or "?")[:40])


def _command(step: Mapping) -> str:
    """The run: text with indentation and blank lines removed, so only the commands are compared."""
    return "\n".join(line.strip() for line in str(step.get("run") or "").strip().splitlines() if line.strip())


def _key_problems(where: str, node: Any, allowed: frozenset) -> list[str]:
    if not isinstance(node, Mapping):
        return []
    return [f"{where}: unexpected key {key!r}" for key in node if key not in allowed]


def _forbidden_key_problems(value: Any, path: str = "workflow") -> list[str]:
    """Every use of shell, working-directory or defaults, at any depth."""
    out = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in FORBIDDEN_KEYS:
                out.append(f"{path}: {key!r} is not allowed anywhere (it changes how or where a command runs)")
            out += _forbidden_key_problems(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out += _forbidden_key_problems(item, f"{path}[{index}]")
    return out


def _env_name_problems(where: str, env: Any, allowed: frozenset) -> list[str]:
    if env is None:
        return []
    if not isinstance(env, Mapping):
        return [f"{where}: env must be a mapping written out in full, not an expression (a variable could then choose the names)"]
    out = []
    for name in env:
        if name in allowed:
            continue
        if _LOADER_ENV.fullmatch(str(name)):
            out.append(f"{where}: env {name} can change which code or tests the interpreter, loader or runner executes")
        else:
            out.append(f"{where}: env {name} is not one of the documented names this level may receive")
    return out


def _condition(holder: Mapping) -> str:
    """The `if:` expression without its ${{ }} wrapper."""
    text = str(holder.get("if") or "").strip()
    wrapped = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.S)
    return (wrapped.group(1) if wrapped else text).strip()


def _gated_on(holder: Mapping, variable: str, *, allow_always: bool) -> bool:
    """
    True when the condition is exactly `vars.<variable> == 'true'`. always() is
    allowed only where a step must also run after a failure (the snapshot);
    anywhere else it would let a job run after a failed scrape.
    """
    prefix = r"(?:always\(\) && )?" if allow_always else ""
    return re.fullmatch(rf"{prefix}vars\.{variable} == 'true'", _condition(holder)) is not None


def _grants_write(permissions: Any) -> bool:
    if isinstance(permissions, str):
        return "write" in permissions
    return any(str(v) == "write" for v in _as_map(permissions).values())


def _trigger_problems(doc: Mapping) -> list[str]:
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1), so look for both.
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, Mapping):
        return ["the workflow has no 'on' mapping"]
    out = []
    extra = sorted({str(k) for k in triggers} - {"schedule", "workflow_dispatch"})
    if extra:
        out.append(f"unexpected trigger(s) {extra}: only a schedule and a manual run may sit beside secrets")
    if "workflow_dispatch" not in triggers:
        out.append("workflow_dispatch (the manual run button) is missing")
    schedule = triggers.get("schedule")
    crons = [i.get("cron") for i in schedule if isinstance(i, Mapping)] if isinstance(schedule, list) else []
    if crons != ["0 3 * * *"]:
        out.append(f"the schedule must be exactly one cron, '0 3 * * *' (03:00 UTC); found {crons}")
    return out


def _permission_problems(doc: Mapping, jobs: Mapping) -> list[str]:
    out = []
    if doc.get("permissions") != {"contents": "read"}:
        out.append("workflow-level permissions must be exactly {contents: read}")
    for name, job in jobs.items():
        permissions = _as_map(job).get("permissions")
        if name == DEPLOY_JOB:
            if permissions != {"pages": "write", "id-token": "write"}:
                out.append("deploy-map permissions must be exactly {pages: write, id-token: write}")
        elif permissions is not None and _grants_write(permissions):
            out.append(f"job {name!r} must not be granted write access")
    return out


def _concurrency_problems(doc: Mapping) -> list[str]:
    concurrency = doc.get("concurrency")
    if not isinstance(concurrency, Mapping) or not str(concurrency.get("group") or "").strip():
        return ["a concurrency group is required so two runs never race for the sheet or Pages"]
    out = []
    if concurrency.get("group") != CONCURRENCY_GROUP:
        out.append(
            f"the concurrency group must be exactly {CONCURRENCY_GROUP!r}: any other name, or an expression "
            "such as the run id, gives runs separate queues, so two of them can race for the sheet or Pages"
        )
    if concurrency.get("cancel-in-progress") is not False:
        out.append("concurrency must set cancel-in-progress: false (queue, never cancel a run mid-write)")
    return out


def _env_problems(where: str, env: Any) -> list[str]:
    if _as_map(env) and any(kind == "secrets" for kind, _ in _references(env)):
        return [f"a secret is set in {where} env, where every step could read it; use step-level env"]
    return []


def _secret_problems(job_name: str, step: Mapping) -> list[str]:
    out = []
    label, role = _label(step), _role(step)
    for key, value in step.items():
        if key != "env" and any(kind == "secrets" for kind, _ in _references(value)):
            out.append(f"step {label!r}: a secret is used in {key!r}, not in env")
    env = _as_map(step.get("env"))
    for key, value in env.items():
        text = str(value)
        for kind, name in _references(text):
            if kind == "secrets":
                if role not in {"scrape", "export"}:
                    out.append(f"step {label!r}: only the scrape and export steps may receive secrets")
                elif role == "export" and name not in EXPORT_SECRETS:
                    out.append(f"step {label!r}: the export step may only receive {sorted(EXPORT_SECRETS)}, not {name}")
                if name not in SECRET_NAMES:
                    out.append(f"step {label!r}: secret {name} is not one of the documented secrets")
            elif name not in VARIABLE_NAMES:
                out.append(f"step {label!r}: variable {name} is not one of the documented variables")
            if str(key) != name and re.fullmatch(r"\s*\$\{\{\s*(?:secrets|vars)\.\w+\s*\}\}\s*", text):
                out.append(f"step {label!r}: env {key} maps to {kind}.{name}; the names must match")
    return out


def _step_problems(job_name: str, step: Mapping) -> list[str]:
    out = _secret_problems(job_name, step)
    label, role = _label(step), _role(step)
    out += _key_problems(f"step {label!r}", step, STEP_KEYS)
    if (step.get("uses") is None) == (step.get("run") is None):
        out.append(f"step {label!r}: a step must have exactly one of uses: or run:")
    if step.get("continue-on-error") not in (None, False):
        out.append(f"step {label!r}: continue-on-error hides failures; a green run must mean a good run")
    uses = step.get("uses")
    if uses is not None:
        if str(uses).partition("@")[0] not in ALLOWED_ACTIONS:
            out.append(f"step {label!r}: action {uses!r} is not an allowlisted action ({sorted(ALLOWED_ACTIONS)})")
        elif not _PINNED.fullmatch(str(uses)):
            out.append(f"step {label!r}: action {uses!r} is not pinned to a version tag or a commit")
    run = step.get("run")
    if isinstance(run, str):
        if "${{" in run:
            out.append(f"step {label!r}: an expression inside run: is a script-injection risk; pass it through env")
        if _RISKY_COMMAND.search(run):
            out.append(f"step {label!r}: run: uses a command that can print the environment")
        hooks = sorted({m.group(0).upper() for m in _RUNNER_HOOKS.finditer(run)})
        if hooks:
            out.append(f"step {label!r}: run: reaches the runner's command files or tokens {hooks}; a step could rewrite the next step's environment")
        verbose = _VERBOSE_FLAG.search(run)
        if verbose:
            out.append(f"step {label!r}: run: passes the verbose or debug flag {verbose.group(0)!r}; debug lines name projects and this log is public")
    out += _env_name_problems(f"step {label!r}", step.get("env"), STEP_ENV.get(role, frozenset()))
    options = step.get("with")
    if options is not None and not isinstance(options, Mapping):
        out.append(f"step {label!r}: with: must be a mapping written out in full, not an expression")
    for option in _as_map(options):
        if option not in WITH_KEYS.get(role, frozenset()):
            out.append(f"step {label!r}: option {option!r} is not one of the options this step may pass")
    if step.get("if") is not None and role not in {"snapshot", "pages-artifact"}:
        out.append(f"step {label!r}: only the snapshot and Pages steps may carry an if: (any other step always runs, so a failed test cannot be skipped past)")
    for variable in _references(step.get("if")):
        if variable[0] == "secrets":
            out.append(f"step {label!r}: a condition must not depend on a secret")
    if role == "deploy-pages" and job_name != DEPLOY_JOB:
        out.append(f"step {label!r}: deploy-pages belongs in the {DEPLOY_JOB} job only")
    return out


def _scrape_problems(job: Mapping) -> list[str]:
    out = []
    steps = _steps(job)
    roles = [_role(s) for s in steps]
    for role in REQUIRED_STEPS:
        if role not in roles:
            out.append(f"the {SCRAPE_JOB} job has no {role} step")
    for role in SCRAPE_STEPS:
        if roles.count(role) > 1:
            out.append(f"the {SCRAPE_JOB} job has more than one {role} step")
    for step, role in zip(steps, roles):
        if role not in SCRAPE_STEPS:
            out.append(f"step {_label(step)!r} is not one of the steps the {SCRAPE_JOB} job may run ({', '.join(SCRAPE_STEPS)})")
        elif role in COMMANDS and not COMMANDS[role][0].fullmatch(_command(step)):
            out.append(f"the {role} step must run {COMMANDS[role][1]}, found {_command(step)!r}")
    known = [r for r in roles if r in SCRAPE_STEPS]
    if known != sorted(known, key=SCRAPE_STEPS.index):
        out.append("steps are out of order: " + ", ".join(SCRAPE_STEPS))
    by_role = {r: s for r, s in zip(reversed(roles), reversed(steps))}

    gate = by_role.get("gate")
    if gate is not None:
        if gate.get("env") or gate.get("with") or gate.get("if"):
            out.append("the test gate check must be unconditional and run with no env (no secrets, no switches)")
    tests = by_role.get("tests")
    if tests is not None:
        if tests.get("env") or tests.get("with") or tests.get("if"):
            out.append("the test gate must be unconditional and run with no env (no secrets, no switches)")
    checkout = by_role.get("checkout")
    if checkout is not None and _as_map(checkout.get("with")).get("persist-credentials") is not False:
        out.append("checkout must set persist-credentials: false")
    setup = by_role.get("setup-python")
    if setup is not None and _as_map(setup.get("with")).get("python-version") != "3.12":
        out.append("python-version must be the string '3.12' (an unquoted 3.10 would read as 3.1)")

    scrape = by_role.get("scrape")
    if scrape is not None:
        passed = {str(k) for k in _as_map(scrape.get("env"))}
        missing = sorted((SECRET_NAMES | (VARIABLE_NAMES - WORKFLOW_ONLY_VARIABLES)) - passed)
        if missing:
            out.append(f"the scrape step does not pass {missing} (an unset SPREADSHEET_ID would create a new sheet)")
    export = by_role.get("export")
    if export is not None:
        wired = {n for k, n in _references(export.get("env")) if k == "secrets"}
        if wired != EXPORT_SECRETS:
            out.append(f"the export step must receive exactly {sorted(EXPORT_SECRETS)}, found {sorted(wired)}")
        if _as_map(export.get("env")).get("MIN_PINS") != MIN_PINS_EXPRESSION:
            out.append(f"the export step's MIN_PINS must be exactly {MIN_PINS_EXPRESSION!r} (an empty map is refused while DEPLOY_MAP is on)")

    snapshot = by_role.get("snapshot")
    if snapshot is not None:
        if not _gated_on(snapshot, "UPLOAD_SNAPSHOT", allow_always=True):
            out.append("the snapshot upload must be gated on vars.UPLOAD_SNAPSHOT == 'true' (artifacts of a public repo are public)")
        options = _as_map(snapshot.get("with"))
        if options.get("path") != SNAPSHOT_PATH:
            out.append(f"the snapshot path must be exactly {SNAPSHOT_PATH!r}, not a directory or a wildcard")
        retention = options.get("retention-days")
        if not isinstance(retention, int) or isinstance(retention, bool) or not 1 <= retention <= 14:
            out.append("the snapshot retention-days must be a whole number from 1 to 14")
    pages = by_role.get("pages-artifact")
    if pages is not None and not _gated_on(pages, "DEPLOY_MAP", allow_always=False):
        out.append("the Pages artifact must be gated on vars.DEPLOY_MAP == 'true' only")
    if pages is not None and _as_map(pages.get("with")).get("path") != PAGES_PATH:
        out.append(f"the Pages artifact path must be exactly {PAGES_PATH!r}, the export folder, never the repository root")
    return out


def _deploy_problems(job: Mapping) -> list[str]:
    out = []
    needs = job.get("needs")
    needs = [needs] if isinstance(needs, str) else needs
    if not isinstance(needs, list) or SCRAPE_JOB not in needs:
        out.append(f"{DEPLOY_JOB} must declare needs: {SCRAPE_JOB}, so a failed scrape never deploys")
    if not _gated_on(job, "DEPLOY_MAP", allow_always=False):
        out.append("the deploy job must be gated on vars.DEPLOY_MAP == 'true' only (no always())")
    if _as_map(job.get("environment")).get("name") != "github-pages":
        out.append("the deploy job must use the github-pages environment")
    steps = _steps(job)
    if [_role(s) for s in steps] != ["deploy-pages"]:
        out.append("the deploy job (which holds the OIDC token) must contain the deploy-pages step and nothing else")
    return out


def _job_problems(name: str, job: Mapping) -> list[str]:
    out = []
    if job.get("runs-on") != "ubuntu-latest":
        out.append(f"job {name!r}: runs-on must be ubuntu-latest (no self-hosted runner next to secrets)")
    timeout = job.get("timeout-minutes")
    longest = {SCRAPE_JOB: 120, DEPLOY_JOB: 30}.get(name)
    if longest is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= longest):
        out.append(f"job {name!r}: set timeout-minutes between 1 and {longest} so a hung run cannot burn minutes")
    if job.get("continue-on-error") not in (None, False):
        out.append(f"job {name!r}: continue-on-error hides failures")
    out += _env_problems(f"job-level ({name})", job.get("env"))
    out += _env_name_problems(f"job {name!r}", job.get("env"), JOB_ENV.get(name, frozenset()))
    out += _key_problems(f"job {name!r}", job, JOB_KEYS.get(name, frozenset(job)))
    steps = job.get("steps")
    if not isinstance(steps, list) or not all(isinstance(s, Mapping) for s in steps):
        out.append(f"job {name!r}: steps must be a list of mappings")
    for step in _steps(job):
        out += _step_problems(name, step)
    return out


def workflow_problems(doc: Any) -> list[str]:
    """Every rule the parsed workflow breaks, one sentence each; empty when it is sound."""
    if not isinstance(doc, Mapping):
        return ["the workflow is not a YAML mapping"]
    jobs = _as_map(doc.get("jobs"))
    problems: list[str] = []
    if doc.get("name") != WORKFLOW_NAME:
        problems.append(f"the workflow name must be {WORKFLOW_NAME!r}")
    problems += _trigger_problems(doc)
    problems += _permission_problems(doc, jobs)
    problems += _concurrency_problems(doc)
    problems += _context_problems(doc)
    problems += _key_problems("workflow", doc, TOP_KEYS)
    problems += _forbidden_key_problems(doc)
    problems += _env_problems("workflow-level", doc.get("env"))
    problems += _env_name_problems("workflow-level", doc.get("env"), frozenset())
    for required in (SCRAPE_JOB, DEPLOY_JOB):
        if not isinstance(jobs.get(required), Mapping):
            problems.append(f"job {required!r} is missing")
    for name, job in jobs.items():
        if name not in (SCRAPE_JOB, DEPLOY_JOB):
            problems.append(f"unexpected job {str(name)!r}: only {SCRAPE_JOB} and {DEPLOY_JOB} may exist")
        problems += _job_problems(str(name), _as_map(job))
    if isinstance(jobs.get(SCRAPE_JOB), Mapping):
        problems += _scrape_problems(jobs[SCRAPE_JOB])
    if isinstance(jobs.get(DEPLOY_JOB), Mapping):
        problems += _deploy_problems(jobs[DEPLOY_JOB])
    return problems


# ---------------------------------------------------------------------------
# The real file
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture()
def doc(workflow_text) -> dict:
    """A fresh parsed copy per test, so a mutation never leaks into the next one."""
    return load_workflow(workflow_text)


def step_of(document: Mapping, role: str, job: str = SCRAPE_JOB) -> dict:
    return next(s for s in document["jobs"][job]["steps"] if _role(s) == role)


def test_the_real_workflow_is_sound(doc):
    assert workflow_problems(doc) == []


def test_the_file_is_where_the_design_says():
    assert WORKFLOW_PATH.is_file()
    assert WORKFLOW_PATH.name == "daily_uae_real_estate.yml"


def test_pyyaml_reads_the_on_key_as_true(doc):
    # The reason _trigger_problems looks at both keys: without this quirk handled
    # every real workflow would look as if it had no trigger.
    assert True in doc and "on" not in doc


def test_the_text_is_clean(workflow_text):
    assert "\t" not in workflow_text, "YAML must not contain tab characters"
    assert workflow_text.endswith("\n")
    assert "pull_request_target" not in workflow_text
    assert not any(line != line.rstrip() for line in workflow_text.splitlines()), "trailing whitespace"


@pytest.mark.parametrize(
    "shape",
    [
        r"AIza[0-9A-Za-z_-]{20,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"gh[pousr]_[A-Za-z0-9]{20,}",
        r'"private_key"\s*:',
        r"\b[A-Za-z0-9_-]{40,}\b",
    ],
)
def test_no_secret_shaped_text_is_committed(workflow_text, shape):
    assert re.search(shape, workflow_text) is None


def test_every_action_is_pinned(doc):
    uses = [s["uses"] for job in doc["jobs"].values() for s in job["steps"] if "uses" in s]
    assert uses, "the workflow should use at least checkout"
    assert all(_PINNED.fullmatch(u) for u in uses), uses


def test_secrets_appear_only_in_step_env_of_the_two_steps(doc):
    holders = []
    for job_name, job in doc["jobs"].items():
        for step in job["steps"]:
            if any(kind == "secrets" for kind, _ in _references(step)):
                holders.append(_role(step))
    assert sorted(holders) == ["export", "scrape"]


def test_scrape_step_passes_every_documented_setting(doc):
    env = step_of(doc, "scrape")["env"]
    for name in SECRET_NAMES:
        assert env[name] == f"${{{{ secrets.{name} }}}}"
    for name in VARIABLE_NAMES - WORKFLOW_ONLY_VARIABLES:
        assert env[name] == f"${{{{ vars.{name} }}}}"


def test_export_step_gets_only_the_sheet_credentials(doc):
    env = step_of(doc, "export")["env"]
    assert set(env) == EXPORT_SECRETS | {"MIN_PINS"}
    assert step_of(doc, "export")["run"] == 'python -m scripts.export_map_pins --out site/pins.geojson --min-pins "$MIN_PINS"'


def test_an_empty_map_is_never_published(doc):
    # The pin minimum is 1 while DEPLOY_MAP is on (an empty map is refused, so the deploy job never starts) and 0 otherwise.
    assert step_of(doc, "export")["env"]["MIN_PINS"] == "${{ vars.DEPLOY_MAP == 'true' && '1' || '0' }}"
    assert doc["jobs"][DEPLOY_JOB]["needs"] == SCRAPE_JOB


def test_publishing_is_off_unless_the_owner_turns_it_on(doc):
    assert _gated_on(step_of(doc, "snapshot"), "UPLOAD_SNAPSHOT", allow_always=True)
    assert _gated_on(step_of(doc, "pages-artifact"), "DEPLOY_MAP", allow_always=False)
    assert _gated_on(doc["jobs"][DEPLOY_JOB], "DEPLOY_MAP", allow_always=False)


def test_tests_run_before_any_credential_is_used(doc):
    roles = [_role(s) for s in doc["jobs"][SCRAPE_JOB]["steps"]]
    assert roles.index("tests") < roles.index("scrape") < roles.index("export")
    assert "env" not in step_of(doc, "tests")


def test_the_test_gate_check_runs_after_the_install_and_right_before_the_tests(doc):
    roles = [_role(s) for s in doc["jobs"][SCRAPE_JOB]["steps"]]
    assert roles.index("install") < roles.index("gate") == roles.index("tests") - 1
    gate = step_of(doc, "gate")
    assert gate["run"] == "python -m scripts.check_test_gate"
    assert set(gate) == {"name", "run"}, "the check takes no env, no condition, no options and may not fail softly"


def test_the_schedule_is_seven_in_the_morning_in_the_uae(doc):
    assert doc[True]["schedule"] == [{"cron": "0 3 * * *"}]


def test_runs_queue_in_one_group_and_are_never_cancelled(doc):
    assert doc["concurrency"] == {"group": CONCURRENCY_GROUP, "cancel-in-progress": False}


def test_the_free_text_identity_settings_are_not_passed_to_any_step(doc, workflow_text):
    # A repository variable is printed in the public step header, so a sheet name or a User-Agent that
    # carries a business name or a contact address would be published. The code reads both from a local .env.
    passed = {name for _, name in _references(doc)}
    passed |= {str(key) for job in doc["jobs"].values() for step in _steps(job) for key in _as_map(step.get("env"))}
    assert not passed & LOCAL_ONLY_SETTINGS, sorted(passed & LOCAL_ONLY_SETTINGS)
    for name in LOCAL_ONLY_SETTINGS:
        assert f"vars.{name}" not in workflow_text and f"secrets.{name}" not in workflow_text


# ---------------------------------------------------------------------------
# The checker can fail: every broken copy must be caught, for the right reason
# ---------------------------------------------------------------------------
def _set(document: dict, path: list, value: Any) -> None:
    node = document
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value


def _drop_role(role: str) -> Callable[[dict], None]:
    def mutate(d: dict) -> None:
        d["jobs"][SCRAPE_JOB]["steps"] = [s for s in d["jobs"][SCRAPE_JOB]["steps"] if _role(s) != role]

    return mutate


def _swap_tests_after_scrape(d: dict) -> None:
    steps = d["jobs"][SCRAPE_JOB]["steps"]
    roles = [_role(s) for s in steps]
    i, j = roles.index("tests"), roles.index("scrape")
    steps[i], steps[j] = steps[j], steps[i]


def _move_after(role: str, anchor: str) -> Callable[[dict], None]:
    """Take the `role` step out and put it back right after the `anchor` step."""

    def mutate(d: dict) -> None:
        steps = d["jobs"][SCRAPE_JOB]["steps"]
        moved = steps.pop(next(i for i, s in enumerate(steps) if _role(s) == role))
        steps.insert(next(i for i, s in enumerate(steps) if _role(s) == anchor) + 1, moved)

    return mutate


def _insert_after(role: str, new_step: dict) -> Callable[[dict], None]:
    def mutate(d: dict) -> None:
        steps = d["jobs"][SCRAPE_JOB]["steps"]
        steps.insert(next(i for i, s in enumerate(steps) if _role(s) == role) + 1, copy.deepcopy(new_step))

    return mutate


# A shell wrapper that would print the environment backwards, past the log masking, then run the real script.
_WRAPPED_SHELL = "bash -c 'env | rev >&2; exec bash \"$0\"' {0}"

MUTATIONS: list[tuple[str, Callable[[dict], None], str]] = [
    ("renamed", lambda d: d.update(name="Something else"), "workflow name"),
    ("cron changed", lambda d: _set(d, [True, "schedule"], [{"cron": "0 3 * * 1"}]), "exactly one cron"),
    ("two crons", lambda d: _set(d, [True, "schedule"], [{"cron": "0 3 * * *"}, {"cron": "0 9 * * *"}]), "exactly one cron"),
    ("no schedule", lambda d: d[True].pop("schedule"), "exactly one cron"),
    ("no manual run", lambda d: d[True].pop("workflow_dispatch"), "workflow_dispatch"),
    ("push trigger", lambda d: d[True].update(push={"branches": ["main"]}), "unexpected trigger"),
    ("pull request trigger", lambda d: d[True].update(pull_request=None), "unexpected trigger"),
    ("no triggers at all", lambda d: d.pop(True), "no 'on' mapping"),
    ("workflow can write contents", lambda d: _set(d, ["permissions", "contents"], "write"), "workflow-level permissions"),
    ("workflow permissions removed", lambda d: d.pop("permissions"), "workflow-level permissions"),
    ("write-all", lambda d: d.update(permissions="write-all"), "workflow-level permissions"),
    ("scrape job given an OIDC token", lambda d: _set(d, ["jobs", SCRAPE_JOB, "permissions"], {"id-token": "write"}), "must not be granted write"),
    ("deploy job also writes contents", lambda d: _set(d, ["jobs", DEPLOY_JOB, "permissions", "contents"], "write"), "deploy-map permissions"),
    ("deploy job loses pages write", lambda d: d["jobs"][DEPLOY_JOB]["permissions"].pop("pages"), "deploy-map permissions"),
    ("no concurrency", lambda d: d.pop("concurrency"), "concurrency group"),
    ("cancel in progress", lambda d: _set(d, ["concurrency", "cancel-in-progress"], True), "cancel-in-progress"),
    ("cancel-in-progress left out", lambda d: d["concurrency"].pop("cancel-in-progress"), "cancel-in-progress"),
    ("concurrency group renamed", lambda d: _set(d, ["concurrency", "group"], "some-other-group"), "concurrency group must be exactly"),
    (
        "concurrency group per run",
        lambda d: _set(d, ["concurrency", "group"], "daily-uae-real-estate-${{ github.run_id }}"),
        "concurrency group must be exactly",
    ),
    ("scrape job missing", lambda d: d["jobs"].pop(SCRAPE_JOB), "is missing"),
    ("deploy job missing", lambda d: d["jobs"].pop(DEPLOY_JOB), "is missing"),
    ("no test step", _drop_role("tests"), "no tests step"),
    ("tests run after the scrape", _swap_tests_after_scrape, "out of order"),
    ("tests may fail", lambda d: step_of(d, "tests").update({"continue-on-error": True}), "continue-on-error"),
    ("tests swallow failure", lambda d: step_of(d, "tests").update(run="python -m pytest || true"), "plain 'python -m pytest'"),
    ("tests narrowed by -k", lambda d: step_of(d, "tests").update(run="python -m pytest -k nothing_matches"), "plain 'python -m pytest'"),
    ("tests narrowed by a marker", lambda d: step_of(d, "tests").update(run="python -m pytest -m slow_only"), "plain 'python -m pytest'"),
    ("tests collect only", lambda d: step_of(d, "tests").update(run="python -m pytest --collect-only"), "plain 'python -m pytest'"),
    ("tests with a file deselected", lambda d: step_of(d, "tests").update(run="python -m pytest --deselect tests/test_main.py"), "plain 'python -m pytest'"),
    ("tests ignoring a folder", lambda d: step_of(d, "tests").update(run="python -m pytest --ignore=tests"), "plain 'python -m pytest'"),
    ("tests behind a switch", lambda d: step_of(d, "tests").update({"if": "vars.RUN_TESTS == 'true'"}), "unconditional"),
    # --- the test gate check (scripts/check_test_gate.py) must run first, plain, with nothing to hide behind ---
    ("no test gate check", _drop_role("gate"), "no gate step"),
    ("test gate check after the scrape", _move_after("gate", "scrape"), "out of order"),
    ("test gate check after the tests", _move_after("gate", "tests"), "out of order"),
    ("test gate check before the install", _move_after("gate", "checkout"), "out of order"),
    ("test gate check twice", lambda d: d["jobs"][SCRAPE_JOB]["steps"].append(copy.deepcopy(step_of(d, "gate"))), "more than one gate"),
    (
        "test gate check given a secret",
        lambda d: step_of(d, "gate").update(env={"SPREADSHEET_ID": "${{ secrets.SPREADSHEET_ID }}"}),
        "only the scrape and export",
    ),
    ("test gate check given an env name", lambda d: step_of(d, "gate").update(env={"EXTRA_FLAG": "1"}), "not one of the documented names"),
    (
        "test gate check given PYTEST_ADDOPTS",
        lambda d: step_of(d, "gate").update(env={"PYTEST_ADDOPTS": "--collect-only"}),
        "env PYTEST_ADDOPTS can change which code",
    ),
    ("test gate check may fail", lambda d: step_of(d, "gate").update({"continue-on-error": True}), "continue-on-error"),
    (
        "test gate check swallows failure",
        lambda d: step_of(d, "gate").update(run="python -m scripts.check_test_gate || true"),
        "plain 'python -m scripts.check_test_gate'",
    ),
    (
        "test gate check given an argument",
        lambda d: step_of(d, "gate").update(run="python -m scripts.check_test_gate --skip"),
        "plain 'python -m scripts.check_test_gate'",
    ),
    (
        "another module run in its place",
        lambda d: step_of(d, "gate").update(run="python -m scripts.check_test_gate_old"),
        "plain 'python -m scripts.check_test_gate'",
    ),
    ("test gate check replaced by a harmless command", lambda d: step_of(d, "gate").update(run="true"), "no gate step"),
    ("test gate check behind a switch", lambda d: step_of(d, "gate").update({"if": "vars.RUN_GATE == 'true'"}), "gate check must be unconditional"),
    ("test gate check given options", lambda d: step_of(d, "gate").update({"with": {"x": "1"}}), "option 'x'"),
    ("whole secrets context", lambda d: step_of(d, "scrape")["env"].update(ALL="${{ toJSON(secrets) }}"), "reads 'secrets'"),
    ("secret by bracket", lambda d: step_of(d, "scrape")["env"].update(SPREADSHEET_ID="${{ secrets['SPREADSHEET_ID'] }}"), "reads 'secrets'"),
    ("job token handed to a step", lambda d: step_of(d, "export")["env"].update(TOKEN="${{ github.token }}"), "reads 'github.token'"),
    ("secret by bracket in an if", lambda d: step_of(d, "export").update({"if": "secrets['GEMINI_API_KEY'] != ''"}), "reads 'secrets'"),
    ("tests given a secret", lambda d: step_of(d, "tests").update(env={"SPREADSHEET_ID": "${{ secrets.SPREADSHEET_ID }}"}), "only the scrape and export"),
    ("secret in a run command", lambda d: step_of(d, "install").update(run="pip install -r requirements.txt ${{ secrets.GEMINI_API_KEY }}"), "not in env"),
    ("secret printed", lambda d: step_of(d, "install").update(run="echo $GEMINI_API_KEY"), "print the environment"),
    ("secret at job level", lambda d: d["jobs"][SCRAPE_JOB].update(env={"GEMINI_API_KEY": "${{ secrets.GEMINI_API_KEY }}"}), "job-level"),
    ("secret at workflow level", lambda d: d.update(env={"GEMINI_API_KEY": "${{ secrets.GEMINI_API_KEY }}"}), "workflow-level env"),
    ("secret in with:", lambda d: step_of(d, "checkout").update({"with": {"persist-credentials": False, "token": "${{ secrets.GEMINI_API_KEY }}"}}), "not in env"),
    ("spreadsheet id dropped", lambda d: step_of(d, "scrape")["env"].pop("SPREADSHEET_ID"), "SPREADSHEET_ID"),
    ("a variable dropped", lambda d: step_of(d, "scrape")["env"].pop("REQUIRE_SOURCES"), "REQUIRE_SOURCES"),
    ("env mapped to the wrong secret", lambda d: step_of(d, "scrape")["env"].update(SPREADSHEET_ID="${{ secrets.GEMINI_API_KEY }}"), "names must match"),
    ("undocumented secret", lambda d: step_of(d, "scrape")["env"].update(EXTRA="${{ secrets.GITHUB_TOKEN }}"), "not one of the documented secrets"),
    ("undocumented variable", lambda d: step_of(d, "scrape")["env"].update(EXTRA="${{ vars.SOMETHING_NEW }}"), "not one of the documented variables"),
    ("export gets the share list", lambda d: step_of(d, "export")["env"].update(SHARE_WITH_EMAILS="${{ secrets.SHARE_WITH_EMAILS }}"), "export step"),
    ("export loses the sheet id", lambda d: step_of(d, "export")["env"].pop("SPREADSHEET_ID"), "export step must receive exactly"),
    ("no snapshot gate", lambda d: step_of(d, "snapshot").pop("if"), "UPLOAD_SNAPSHOT"),
    ("snapshot gate inverted", lambda d: step_of(d, "snapshot").update({"if": "vars.UPLOAD_SNAPSHOT != 'true'"}), "UPLOAD_SNAPSHOT"),
    ("snapshot gate defeated", lambda d: step_of(d, "snapshot").update({"if": "always() || vars.UPLOAD_SNAPSHOT == 'true'"}), "UPLOAD_SNAPSHOT"),
    ("snapshot uploads the whole repo", lambda d: step_of(d, "snapshot")["with"].update(path="."), "snapshot path"),
    ("snapshot kept for 90 days", lambda d: step_of(d, "snapshot")["with"].update({"retention-days": 90}), "retention-days"),
    ("snapshot retention missing", lambda d: step_of(d, "snapshot")["with"].pop("retention-days"), "retention-days"),
    ("no pages gate", lambda d: step_of(d, "pages-artifact").pop("if"), "DEPLOY_MAP"),
    ("pages gate always on", lambda d: step_of(d, "pages-artifact").update({"if": "always()"}), "DEPLOY_MAP"),
    ("no deploy gate", lambda d: d["jobs"][DEPLOY_JOB].pop("if"), "deploy job must be gated"),
    ("deploy runs after a failed scrape", lambda d: d["jobs"][DEPLOY_JOB].update({"if": "always() && vars.DEPLOY_MAP == 'true'"}), "deploy job must be gated"),
    ("deploy does not wait for the scrape", lambda d: d["jobs"][DEPLOY_JOB].pop("needs"), "needs"),
    ("deploy wrong environment", lambda d: _set(d, ["jobs", DEPLOY_JOB, "environment"], {"name": "production"}), "github-pages environment"),
    ("deploy job runs code", lambda d: d["jobs"][DEPLOY_JOB]["steps"].insert(0, {"run": "python -m src.main"}), "nothing else"),
    ("deploy job checks out code", lambda d: d["jobs"][DEPLOY_JOB]["steps"].insert(0, {"uses": "actions/checkout@v6"}), "nothing else"),
    ("deploy-pages in the scrape job", lambda d: d["jobs"][SCRAPE_JOB]["steps"].append({"uses": "actions/deploy-pages@v5"}), "deploy-pages belongs"),
    ("action on a moving branch", lambda d: step_of(d, "checkout").update(uses="actions/checkout@main"), "not pinned"),
    ("action without a version", lambda d: step_of(d, "setup-python").update(uses="actions/setup-python"), "not pinned"),
    ("checkout keeps the token", lambda d: step_of(d, "checkout").pop("with"), "persist-credentials"),
    ("python version as a number", lambda d: step_of(d, "setup-python")["with"].update({"python-version": 3.12}), "python-version"),
    ("python 3.11 left behind", lambda d: step_of(d, "setup-python")["with"].update({"python-version": "3.11"}), "python-version"),
    ("python 3.10 as a number", lambda d: step_of(d, "setup-python")["with"].update({"python-version": 3.10}), "python-version"),
    ("no job timeout", lambda d: d["jobs"][SCRAPE_JOB].pop("timeout-minutes"), "timeout-minutes"),
    ("six hour timeout", lambda d: d["jobs"][SCRAPE_JOB].update({"timeout-minutes": 360}), "timeout-minutes"),
    ("no deploy timeout", lambda d: d["jobs"][DEPLOY_JOB].pop("timeout-minutes"), "timeout-minutes"),
    ("six hour deploy timeout", lambda d: d["jobs"][DEPLOY_JOB].update({"timeout-minutes": 360}), "timeout-minutes"),
    ("contact address as a visible variable", lambda d: step_of(d, "scrape")["env"].update(GEOCODE_CONTACT="${{ vars.GEOCODE_CONTACT }}"), "GEOCODE_CONTACT"),
    ("self-hosted runner", lambda d: d["jobs"][SCRAPE_JOB].update({"runs-on": "self-hosted"}), "runs-on"),
    ("scrape may fail", lambda d: step_of(d, "scrape").update({"continue-on-error": True}), "continue-on-error"),
    ("job may fail", lambda d: d["jobs"][SCRAPE_JOB].update({"continue-on-error": True}), "continue-on-error"),
    ("scrape started the old way", lambda d: step_of(d, "scrape").update(run="python src/main.py"), "no scrape step"),
    ("export step removed", _drop_role("export"), "no export step"),
    ("scrape twice", lambda d: d["jobs"][SCRAPE_JOB]["steps"].append(copy.deepcopy(step_of(d, "scrape"))), "more than one scrape"),
    ("condition on a secret", lambda d: step_of(d, "export").update({"if": "secrets.GEMINI_API_KEY != ''"}), "must not depend on a secret"),
    ("event data in a command", lambda d: step_of(d, "install").update(run="pip install ${{ github.event.inputs.pkg }}"), "script-injection"),
    # --- the checker is an allowlist: an unknown action, step, command, key or name is a problem on its own ---
    ("unknown action after the tests", _insert_after("tests", {"uses": "some-owner/some-action@v1"}), "not an allowlisted action"),
    ("unlisted action of the allowed owner", _insert_after("tests", {"uses": "actions/cache@v4"}), "not an allowlisted action"),
    ("local action", _insert_after("tests", {"uses": "./local-action"}), "not an allowlisted action"),
    ("docker action", _insert_after("tests", {"uses": "docker://alpine:3"}), "not an allowlisted action"),
    ("action on latest", lambda d: step_of(d, "checkout").update(uses="actions/checkout@latest"), "not pinned"),
    ("action on a short hash", lambda d: step_of(d, "checkout").update(uses="actions/checkout@abc1234"), "not pinned"),
    ("action on a branch of a listed repo", lambda d: step_of(d, "setup-python").update(uses="actions/setup-python@main"), "not pinned"),
    ("printf into GITHUB_ENV", _insert_after("tests", {"run": "printf 'PYTHONPATH=%s\\n' \"$PWD/x\" >> \"$GITHUB_ENV\""}), "tokens ['GITHUB_ENV']"),
    ("harmless unknown step", _insert_after("tests", {"run": "ls"}), "not one of the steps"),
    ("python -c writes sitecustomize", _insert_after("tests", {"run": "python -c \"open('sitecustomize.py','w').write('import os')\""}), "not one of the steps"),
    ("second pip install from a URL", _insert_after("tests", {"run": "pip install https://example.invalid/pkg.tar.gz"}), "more than one install"),
    ("pip install from a URL inside the install step", lambda d: step_of(d, "install").update(run="pip install -r requirements.txt\npip install https://example.invalid/pkg.tar.gz"), "install step must run"),
    ("install step without the requirements file", lambda d: step_of(d, "install").update(run="pip install --upgrade pip"), "install step must run"),
    ("a second checkout", _insert_after("checkout", {"uses": "actions/checkout@v6", "with": {"persist-credentials": False}}), "more than one checkout"),
    ("steps appended after the optional ones", lambda d: d["jobs"][SCRAPE_JOB]["steps"].append({"run": "python -m pytest"}), "more than one tests"),
    ("export before the tests", lambda d: d["jobs"][SCRAPE_JOB]["steps"].insert(2, copy.deepcopy(step_of(d, "export"))), "more than one export"),
    ("both uses and run", lambda d: step_of(d, "checkout").update(run="python -m pytest"), "exactly one of uses: or run:"),
    ("a step that is not a mapping", lambda d: d["jobs"][SCRAPE_JOB]["steps"].append("python -m pytest"), "list of mappings"),
    ("unexpected step key", lambda d: step_of(d, "tests").update({"timeout-minutes": 1}), "unexpected key"),
    ("unexpected job key", lambda d: d["jobs"][SCRAPE_JOB].update(container="example.invalid/image:1"), "unexpected key 'container'"),
    ("scrape job given an environment", lambda d: d["jobs"][SCRAPE_JOB].update(environment="production"), "unexpected key 'environment'"),
    ("unexpected workflow key", lambda d: d.update(run_name="x"), "unexpected key 'run_name'"),
    ("an extra job", lambda d: d["jobs"].update(extra={"runs-on": "ubuntu-latest", "steps": [{"run": "python -m pytest"}]}), "unexpected job"),
    ("scrape step run after failed tests", lambda d: step_of(d, "scrape").update({"if": "${{ always() }}"}), "may carry an if"),
    ("export step run after failed tests", lambda d: step_of(d, "export").update({"if": "${{ always() }}"}), "may carry an if"),
    # --- wrapped shells and other directories (M5, M6, M8) ---
    ("shell wrapper on the scrape step", lambda d: step_of(d, "scrape").update(shell=_WRAPPED_SHELL), "'shell' is not allowed anywhere"),
    ("shell wrapper on the tests step", lambda d: step_of(d, "tests").update(shell=_WRAPPED_SHELL), "'shell' is not allowed anywhere"),
    ("default shell for every step", lambda d: d.update(defaults={"run": {"shell": _WRAPPED_SHELL}}), "'defaults' is not allowed anywhere"),
    ("default directory for every step of a job", lambda d: d["jobs"][SCRAPE_JOB].update(defaults={"run": {"working-directory": "site"}}), "'defaults' is not allowed anywhere"),
    ("scrape in another directory", lambda d: step_of(d, "scrape").update({"working-directory": "site"}), "'working-directory' is not allowed anywhere"),
    ("export in another directory", lambda d: step_of(d, "export").update({"working-directory": "site"}), "'working-directory' is not allowed anywhere"),
    ("shell hidden in with:", lambda d: step_of(d, "checkout")["with"].update(shell=_WRAPPED_SHELL), "'shell' is not allowed anywhere"),
    # --- interpreter and loader environment (M7, M9) ---
    ("scrape given PYTHONPATH", lambda d: step_of(d, "scrape")["env"].update(PYTHONPATH="${{ github.workspace }}/vendored"), "env PYTHONPATH"),
    ("scrape given LD_PRELOAD", lambda d: step_of(d, "scrape")["env"].update(LD_PRELOAD="/tmp/x.so"), "env LD_PRELOAD"),
    ("scrape given LD_LIBRARY_PATH", lambda d: step_of(d, "scrape")["env"].update(LD_LIBRARY_PATH="/tmp"), "env LD_LIBRARY_PATH"),
    ("scrape given PATH", lambda d: step_of(d, "scrape")["env"].update(PATH="/tmp:/usr/bin"), "env PATH"),
    ("scrape given PYTHONSTARTUP", lambda d: step_of(d, "scrape")["env"].update(PYTHONSTARTUP="x.py"), "env PYTHONSTARTUP"),
    ("scrape given a package index", lambda d: step_of(d, "scrape")["env"].update(PIP_INDEX_URL="https://example.invalid/simple"), "env PIP_INDEX_URL"),
    ("scrape given an undocumented harmless name", lambda d: step_of(d, "scrape")["env"].update(EXTRA_FLAG="1"), "not one of the documented names"),
    ("scrape given ACTIONS_STEP_DEBUG", lambda d: step_of(d, "scrape")["env"].update(ACTIONS_STEP_DEBUG="true"), "env ACTIONS_STEP_DEBUG"),
    ("tests given BASH_ENV", lambda d: step_of(d, "tests").update(env={"BASH_ENV": "x.sh"}), "env BASH_ENV"),
    # --- the test runner's own environment: one value can make the test gate pass while nothing runs ---
    (
        "tests given PYTEST_ADDOPTS",
        lambda d: step_of(d, "tests").update(env={"PYTEST_ADDOPTS": "--collect-only"}),
        "env PYTEST_ADDOPTS can change which code",
    ),
    (
        "tests given PYTEST_PLUGINS",
        lambda d: step_of(d, "tests").update(env={"PYTEST_PLUGINS": "skip_everything"}),
        "env PYTEST_PLUGINS can change which code",
    ),
    (
        "tests given PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        lambda d: step_of(d, "tests").update(env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}),
        "env PYTEST_DISABLE_PLUGIN_AUTOLOAD can change which code",
    ),
    (
        "scrape given PYTEST_ADDOPTS",
        lambda d: step_of(d, "scrape")["env"].update(PYTEST_ADDOPTS="--ignore=tests"),
        "env PYTEST_ADDOPTS can change which code",
    ),
    (
        "install given a lower-case pytest name",
        lambda d: step_of(d, "install").update(env={"pytest_addopts": "-k nothing"}),
        "env pytest_addopts can change which code",
    ),
    (
        "job given PYTEST_ADDOPTS",
        lambda d: d["jobs"][SCRAPE_JOB]["env"].update(PYTEST_ADDOPTS="--collect-only"),
        "env PYTEST_ADDOPTS can change which code",
    ),
    (
        "workflow given PYTEST_PLUGINS",
        lambda d: d.update(env={"PYTEST_PLUGINS": "skip_everything"}),
        "env PYTEST_PLUGINS can change which code",
    ),
    ("install given a package index", lambda d: step_of(d, "install").update(env={"PIP_INDEX_URL": "https://example.invalid/simple"}), "env PIP_INDEX_URL"),
    ("export given PYTHONSTARTUP", lambda d: step_of(d, "export")["env"].update(PYTHONSTARTUP="x.py"), "env PYTHONSTARTUP"),
    ("export given an undocumented name", lambda d: step_of(d, "export")["env"].update(EXTRA_FLAG="1"), "not one of the documented names"),
    ("export pin minimum lowered", lambda d: step_of(d, "export")["env"].update(MIN_PINS="0"), "MIN_PINS must be"),
    ("job given PYTHONPATH", lambda d: d["jobs"][SCRAPE_JOB]["env"].update(PYTHONPATH="x"), "env PYTHONPATH"),
    ("job given a second name", lambda d: d["jobs"][SCRAPE_JOB]["env"].update(EXTRA_FLAG="1"), "not one of the documented names"),
    ("deploy job given env", lambda d: d["jobs"][DEPLOY_JOB].update(env={"PYTHONUNBUFFERED": "1"}), "unexpected key 'env'"),
    ("workflow given PATH", lambda d: d.update(env={"PATH": "/tmp/x"}), "env PATH"),
    ("env written as an expression", lambda d: step_of(d, "scrape").update(env="${{ fromJSON(vars.EXTRA_ENV) }}"), "not an expression"),
    ("job env written as an expression", lambda d: d["jobs"][SCRAPE_JOB].update(env="${{ fromJSON(vars.EXTRA_ENV) }}"), "not an expression"),
    # --- the options an action may be given ---
    ("checkout of another repository", lambda d: step_of(d, "checkout")["with"].update(repository="example/other"), "option 'repository'"),
    ("checkout of another ref", lambda d: step_of(d, "checkout")["with"].update(ref="refs/heads/other"), "option 'ref'"),
    ("setup-python reading a version file", lambda d: step_of(d, "setup-python")["with"].update({"python-version-file": "x"}), "option 'python-version-file'"),
    ("snapshot with hidden files", lambda d: step_of(d, "snapshot")["with"].update({"include-hidden-files": True}), "option 'include-hidden-files'"),
    ("deploy-pages given a token", lambda d: d["jobs"][DEPLOY_JOB]["steps"][0].update({"with": {"token": "x"}}), "option 'token'"),
    ("with written as an expression", lambda d: step_of(d, "checkout").update({"with": "${{ fromJSON(vars.X) }}"}), "with: must be a mapping"),
    ("pages artifact of the whole repository", lambda d: step_of(d, "pages-artifact")["with"].update(path="."), "Pages artifact path"),
    # --- command shapes: the scrape may skip geocoding or name the snapshot, and nothing else ---
    ("scrape dry run", lambda d: step_of(d, "scrape").update(run="python -m src.main --dry-run"), "scrape step must run"),
    ("scrape layering a file under the secrets", lambda d: step_of(d, "scrape").update(run="python -m src.main --env-file x.env"), "scrape step must run"),
    ("scrape with a shell tail", lambda d: step_of(d, "scrape").update(run="python -m src.main || true"), "scrape step must run"),
    ("scrape snapshot elsewhere", lambda d: step_of(d, "scrape").update(run="python -m src.main --snapshot site/all.json"), "scrape step must run"),
    ("export lowered pin minimum", lambda d: step_of(d, "export").update(run="python -m scripts.export_map_pins --out site/pins.geojson --min-pins 0"), "export step must run"),
    ("export written elsewhere", lambda d: step_of(d, "export").update(run='python -m scripts.export_map_pins --out pins.geojson --min-pins "$MIN_PINS"'), "export step must run"),
    # --- verbose and debug flags on any run command ---
    ("scrape with -v", lambda d: step_of(d, "scrape").update(run="python -m src.main -v"), "verbose or debug flag"),
    ("scrape with -vv", lambda d: step_of(d, "scrape").update(run="python -m src.main -vv"), "verbose or debug flag"),
    ("scrape with --verbose", lambda d: step_of(d, "scrape").update(run="python -m src.main --verbose"), "verbose or debug flag"),
    ("scrape with an abbreviated --verb", lambda d: step_of(d, "scrape").update(run="python -m src.main --verb"), "verbose or debug flag"),
    ("scrape with --debug", lambda d: step_of(d, "scrape").update(run="python -m src.main --debug"), "verbose or debug flag"),
    ("tests with -v", lambda d: step_of(d, "tests").update(run="python -m pytest -v"), "verbose or debug flag"),
    ("tests with combined short flags", lambda d: step_of(d, "tests").update(run="python -m pytest -sv"), "verbose or debug flag"),
    ("export with --verbose", lambda d: step_of(d, "export").update(run='python -m scripts.export_map_pins --out site/pins.geojson --min-pins "$MIN_PINS" --verbose'), "verbose or debug flag"),
    ("install with -v", lambda d: step_of(d, "install").update(run="pip install -v -r requirements.txt"), "verbose or debug flag"),
    ("unknown step with -v", _insert_after("tests", {"run": "ls -v"}), "verbose or debug flag"),
]

# The free-text identity settings may not come back as repository variables (by name or as a value).
MUTATIONS += [
    (
        f"the scrape step passes {name} as a repository variable ({kind})",
        lambda d, n=name: step_of(d, "scrape")["env"].update({n: f"${{{{ vars.{n} }}}}"}),
        expected.format(name=name),
    )
    for name in sorted(LOCAL_ONLY_SETTINGS)
    for kind, expected in (
        ("env name", "env {name} is not one of the documented names"),
        ("vars reference", "variable {name} is not one of the documented variables"),
    )
]

# Every file and token the runner offers a step as a way to change what later steps do or see.
MUTATIONS += [
    (f"a run command reaches {hook}", _insert_after("tests", {"run": f'ls > "${hook}"'}), f"tokens ['{hook}']")
    for hook in (
        "GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STATE", "GITHUB_STEP_SUMMARY", "GITHUB_TOKEN",
        "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    )
]


@pytest.mark.parametrize("label, mutate, expected", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_each_broken_copy_is_caught(doc, label, mutate, expected):
    original = copy.deepcopy(doc)
    mutate(doc)
    assert doc != original, f"the mutation {label!r} changed nothing, so it proves nothing"
    problems = workflow_problems(doc)
    assert any(expected.lower() in p.lower() for p in problems), (label, expected, problems)


def test_the_unmutated_copy_still_passes_after_the_mutation_tests(doc):
    # Guards the fixture itself: a fresh parse must be clean every time.
    assert workflow_problems(doc) == []


# ---------------------------------------------------------------------------
# The allowlist is not stricter than the real file: what it permits is permitted
# ---------------------------------------------------------------------------
def test_the_scrape_job_runs_exactly_the_known_steps_in_order(doc):
    assert [_role(s) for s in doc["jobs"][SCRAPE_JOB]["steps"]] == list(SCRAPE_STEPS)
    assert [_role(s) for s in doc["jobs"][DEPLOY_JOB]["steps"]] == ["deploy-pages"]


def test_the_real_env_names_are_exactly_the_allowed_ones(doc):
    assert set(step_of(doc, "scrape")["env"]) == SCRAPE_ENV - WORKFLOW_ONLY_VARIABLES
    assert set(step_of(doc, "export")["env"]) == EXPORT_ENV
    assert set(doc["jobs"][SCRAPE_JOB]["env"]) == JOB_ENV[SCRAPE_JOB]
    assert not any(_LOADER_ENV.fullmatch(name) for name in SCRAPE_ENV | EXPORT_ENV), "a documented name looks like a loader variable"


@pytest.mark.parametrize(
    "command",
    [
        "python -m src.main",
        "python -m src.main --no-geocode",
        f"python -m src.main --snapshot {SNAPSHOT_PATH}",
        f"python -m src.main --no-geocode --snapshot {SNAPSHOT_PATH}",
    ],
)
def test_the_documented_scrape_flags_are_accepted(doc, command):
    step_of(doc, "scrape")["run"] = command
    assert workflow_problems(doc) == []


@pytest.mark.parametrize("ref", ["v6", "v6.1", "v6.1.2", "0123456789abcdef" * 2 + "01234567"])
def test_a_major_tag_or_a_full_commit_is_an_accepted_pin(doc, ref):
    step_of(doc, "checkout")["uses"] = f"actions/checkout@{ref}"
    assert workflow_problems(doc) == []


def test_the_install_step_may_skip_the_pip_upgrade(doc):
    step_of(doc, "install")["run"] = "pip install -r requirements.txt"
    assert workflow_problems(doc) == []


@pytest.mark.parametrize("text", ["-v", "-vv", "-sv", "-vs", "--verbose", "--verb", "--debug", "--debug=1", "python -m pytest -x -v tests"])
def test_the_verbose_pattern_sees_every_spelling(text):
    assert _VERBOSE_FLAG.search(text), text


@pytest.mark.parametrize(
    "text",
    ["--upgrade", "--min-pins 1", "-r requirements.txt", "--out site/pins.geojson", "-m pytest", "my-venv/bin/python", "--no-geocode", "$MIN_PINS"],
)
def test_the_verbose_pattern_leaves_the_real_commands_alone(text):
    assert _VERBOSE_FLAG.search(text) is None, text


@pytest.mark.parametrize(
    "name",
    [
        "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "PATH", "BASH_ENV",
        "ENV", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "NODE_OPTIONS", "http_proxy", "HTTPS_PROXY", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
        "GITHUB_ENV", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "RUNNER_TEMP",
        "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG", "PYTEST_CURRENT_TEST", "pytest_addopts",
    ],
)
def test_loader_and_interpreter_variables_are_named_as_such(name):
    problems = _env_name_problems("a step", {name: "x"}, frozenset())
    assert len(problems) == 1 and "can change which code" in problems[0], problems


# ---------------------------------------------------------------------------
# The checker survives malformed input (empty, None, wrong types, half-written)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "document",
    [None, "", "name: x", [], [1, 2], 42, {}, {"jobs": None}, {"jobs": []}, {"jobs": {"scrape-and-sync": None}},
     {"jobs": {"scrape-and-sync": {"steps": "run it"}}}, {"jobs": {"scrape-and-sync": {"steps": [None, 3, "x", {"run": None}]}}},
     {"jobs": {"deploy-map": {"needs": 5, "steps": [{"uses": 7, "env": "oops"}]}}}, {"on": "push", "permissions": 7},
     {True: [], "concurrency": "yes"}],
)
def test_malformed_documents_give_problems_not_crashes(document):
    problems = workflow_problems(document)
    assert problems and all(isinstance(p, str) for p in problems)


def test_a_document_that_is_not_a_mapping_has_exactly_one_problem():
    assert workflow_problems(None) == ["the workflow is not a YAML mapping"]


def test_half_written_yaml_is_an_error_not_a_pass():
    with pytest.raises(yaml.YAMLError):
        load_workflow("name: x\njobs:\n  a: [unclosed\n")


def test_an_empty_file_parses_to_none_and_is_reported():
    assert workflow_problems(load_workflow("")) == ["the workflow is not a YAML mapping"]


def test_unicode_and_huge_values_do_not_break_the_checker(doc):
    doc["jobs"][SCRAPE_JOB]["steps"][0]["name"] = "تحقق " + "x" * 100_000
    assert workflow_problems(doc) == []
    # a documented name (an undocumented one is a problem since the checker became an allowlist)
    step_of(doc, "scrape")["env"]["GEMINI_MODEL"] = "y" * 100_000
    assert workflow_problems(doc) == []


# ---------------------------------------------------------------------------
# The workflow, the design, the example environment file and the guides agree
# ---------------------------------------------------------------------------
def _design_names(label: str) -> set[str]:
    for line in DESIGN_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{label}:"):
            return set(re.findall(r"`([A-Z][A-Z0-9_]+)`", line))
    raise AssertionError(f"docs/DESIGN.md has no '{label}:' line")


def test_secret_names_match_the_design():
    assert SECRET_NAMES == _design_names("Secrets")


def test_variable_names_match_the_design():
    # The design may list the two free-text settings the workflow does not pass beside the variables or
    # beside the local-only ones; either way every setting it names is known here, and every known one is named.
    documented = _design_names("Variables") | _design_names("Local-only")
    assert VARIABLE_NAMES | LOCAL_ONLY_SETTINGS | {"GCP_SERVICE_ACCOUNT_FILE"} == documented


def test_env_example_and_workflow_agree_on_names(doc):
    text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    declared = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
    local_only = {"GCP_SERVICE_ACCOUNT_FILE"} | LOCAL_ONLY_SETTINGS
    passed = set(step_of(doc, "scrape")["env"])
    assert passed <= declared, f"the workflow passes names .env.example does not declare: {sorted(passed - declared)}"
    forgotten = declared - local_only - passed - {"DEPLOY_MAP"}
    assert not forgotten, f".env.example declares settings the workflow never passes: {sorted(forgotten)}"


@pytest.fixture(scope="module")
def readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def setup_guide() -> str:
    return SETUP_PATH.read_text(encoding="utf-8")


def test_the_setup_guide_names_every_secret_and_variable(setup_guide):
    missing = sorted(n for n in SECRET_NAMES | VARIABLE_NAMES | LOCAL_ONLY_SETTINGS if f"`{n}`" not in setup_guide)
    assert not missing, f"setup_google_sheets.md does not mention: {missing}"


def test_the_guides_say_what_the_workflow_does_by_default(readme, setup_guide):
    for text in (readme, setup_guide):
        assert "UPLOAD_SNAPSHOT" in text and "DEPLOY_MAP" in text
    assert "60 days" in readme
    assert "public" in readme.lower()


def _readme_section(readme: str, heading: str) -> str:
    """The text under one `## heading`, up to the next `## ` line, whitespace collapsed."""
    match = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", readme, re.M | re.S)
    assert match, f"README.md has no '## {heading}' section"
    return " ".join(match.group(1).split())


def test_the_readme_marks_the_code_licence_as_the_owners_decision(readme):
    # A public page carries no to-do note: the open question is stated as a fact, in the Licence section.
    assert "TODO" not in readme, "a public README states the licence position; it does not carry a to-do note"
    section = _readme_section(readme, "Licence").lower()
    assert "has not chosen an open-source licence" in section, "the Licence section must say the owner has not chosen one"
    assert re.search(r"nobody is granted any right to [^.]*\breuse\b", section), "it must say that no reuse right is granted yet"
    assert "all rights remain with" in section
    assert not re.search(r"(?i)\b(MIT|Apache|GPL|BSD)\b", readme), "the owner has not chosen a licence; do not invent one"


def test_the_licence_section_reader_fails_on_a_missing_section():
    # The reader must not return an empty string for a README with no Licence section, which would
    # turn the checks above into assertions about nothing.
    with pytest.raises(AssertionError, match="no '## Licence' section"):
        _readme_section("# Title\n\n## Other\n\ntext\n", "Licence")
    assert _readme_section("## Licence\n\nline one\nline two\n\n## Next\n\nmore\n", "Licence") == "line one line two"


def test_the_readme_carries_the_data_attribution_and_caveats(readme):
    assert "OpenStreetMap" in readme and "ODbL" in readme
    assert "dedupe_sheet.py" in readme and "--apply" in readme
    for fact in ("Data.Dubai", "SRERD"):
        assert fact in readme


@pytest.mark.parametrize("path", [README_PATH, SETUP_PATH], ids=["README.md", "setup_google_sheets.md"])
def test_guides_contain_no_real_looking_values(path):
    text = path.read_text(encoding="utf-8")
    emails = [e for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text) if not e.endswith(("example.invalid", "example.com"))]
    assert emails == [], f"real-looking email addresses in {path.name}: {emails}"
    tokens = re.findall(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b", text)
    assert tokens == [], f"id- or key-shaped strings in {path.name}"
    assert "-----BEGIN" not in text and "AIza" not in text


# ---------------------------------------------------------------------------
# The guides tell the truth about the command line, the exit codes and the Python version
# ---------------------------------------------------------------------------
def test_the_readme_documents_every_command_line_flag_and_exit_code(readme):
    import src.main as app

    actions = [a for a in app.build_parser()._actions if a.option_strings and "-h" not in a.option_strings]
    assert actions, "the parser should have flags"
    # a flag is documented when any of its spellings opens a code span, e.g. `--snapshot PATH`
    missing = sorted(a.option_strings[-1] for a in actions if not any(f"`{o}" in readme for o in a.option_strings))
    assert not missing, f"README.md does not document: {missing}"
    for code in (app.EXIT_OK, app.EXIT_FAILED, app.EXIT_SETUP):
        assert re.search(rf"^\| {code} \|", readme, re.M), f"README.md has no row for exit code {code}"
    assert "--dry-run" in readme and "first run" in readme.lower()


def test_the_readme_has_a_known_limitations_list_and_a_tab_table(readme):
    assert "### Known limitations" in readme
    for phrase in ("unverified", "Sharjah", "geocoding", "No prices", "licence"):
        assert phrase.lower() in readme.lower(), phrase
    for tab in ("Registry_Projects", "Listing_Data", "Launch_Candidates", "Geocode_Overrides", "Map_Pins", "Scrape_Run_Logs"):
        assert re.search(rf"^\| `{tab}` \|", readme, re.M), f"no row for the {tab} tab"


def test_every_document_names_the_python_the_workflow_uses(doc, readme, setup_guide):
    version = step_of(doc, "setup-python")["with"]["python-version"]
    assert version == "3.12"
    design = DESIGN_PATH.read_text(encoding="utf-8")
    assert f"Python {version}" in readme and f"Python {version}" in design
    for name, text in (("README.md", readme), ("setup_google_sheets.md", setup_guide), ("docs/DESIGN.md", design)):
        assert not re.search(r"Python 3\.1[01]\b", text), f"{name} still names another Python"


@pytest.mark.parametrize("path", [README_PATH, SETUP_PATH], ids=["README.md", "setup_google_sheets.md"])
def test_guides_contain_no_local_paths_or_account_names(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:\\Users\\|/home/|/Users/|@users\.noreply", text), f"a local path or account in {path.name}"


def test_the_geocode_contact_is_a_secret_in_the_workflow_and_the_guide(doc, setup_guide):
    assert step_of(doc, "scrape")["env"]["GEOCODE_CONTACT"] == "${{ secrets.GEOCODE_CONTACT }}"
    row = next(line for line in setup_guide.splitlines() if line.startswith("| `GEOCODE_CONTACT`"))
    assert "secret" in row.lower()
    variables_section = setup_guide.split("## 5.", 1)[1].split("## 6.", 1)[0]
    assert "`GEOCODE_CONTACT`" not in variables_section
