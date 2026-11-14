"""Private settings: read the untracked settings file and ask Git whether the file is safe.

The settings file `config.toml` holds personal contact details that the SEC asks
automated tools to send. This module never logs or prints those details. Other
modules receive them only inside a SecAccess record, whose contact line is
hidden from `repr()` and from JSON output.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, TypeAlias

from .providers import Issue, JsonValue, SecAccess, convert_to_json, make_issue

CONFIG_NAME = "config.toml"
EXAMPLE_NAME = "config.example.toml"
MAX_CONFIG_BYTES = 64 * 1024
GIT_TIMEOUT_SECONDS = 5.0

GitState: TypeAlias = Literal["no_repository", "ignored", "not_ignored", "tracked", "unknown"]
ContactState: TypeAlias = Literal["configured", "missing", "invalid", "blocked"]
GitRunner: TypeAlias = Callable[..., Any]

TRACKED_MESSAGE = (
    f"Git tracks {CONFIG_NAME}, so the contact details could be published. Run `git rm --cached {CONFIG_NAME}`,"
    " add an ignore rule for the file, and treat the details as exposed if the file was ever pushed."
    " The collector does not read the file while Git tracks it.")
NOT_IGNORED_MESSAGE = (
    f"Git does not ignore {CONFIG_NAME} in this folder, so the file could be committed by mistake. Add the line"
    f" `{CONFIG_NAME}` to .git/info/exclude or to a .gitignore file, then run the command again."
    " The collector does not read the file until Git ignores it.")
UNKNOWN_MESSAGE = (
    f"The collector could not ask Git whether {CONFIG_NAME} is ignored. Check it yourself with"
    f" `git check-ignore -v {CONFIG_NAME}`; the command must print a rule.")

_NAME = re.compile(r"[A-Za-z0-9 .,'&()\-]{2,80}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@([A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63})+)")
_POSITION = re.compile(r"at line (\d+), column (\d+)")
_RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
_RESERVED_ENDINGS = (".example", ".invalid", ".localhost")
_ALLOWED_KEYS = frozenset({"contact_name", "contact_email"})


@dataclass(frozen=True)
class Settings:
    """Describe the state of the private settings file and return the settings record.

    The record holds no path and no personal detail of its own. The contact line
    exists only inside `sec_access`, where it is hidden from `repr()`.

    :param exists: bool; True when the settings file exists.
    :param git_state: GitState; what Git says about the file: `no_repository`, `ignored`, `not_ignored`,
        `tracked`, or `unknown`.
    :param sec_contact: ContactState; `configured`, `missing`, `invalid`, or `blocked`.
    :param sec_access: SecAccess; contact line for SEC requests, or the reason why they are blocked.
    :param issues: tuple[Issue, ...]; warnings and errors for the person, in the order they were found.
    return instance: Settings; immutable settings record.
    """

    exists: bool
    git_state: GitState
    sec_contact: ContactState
    sec_access: SecAccess
    issues: tuple[Issue, ...]


def _ask_git(root: Path, run: GitRunner, *arguments: str) -> int | None:
    """Run one Git question about the settings file and return its exit code, or None when Git gave no answer.

    The command is `git -C ROOT ARGUMENTS -- config.toml`. It runs without a shell,
    with standard input closed and a five-second timeout. A missing `git` program,
    a timeout, and every other operating-system error give None.

    :param root: Path; project root that contains the settings file.
    :param run: GitRunner; function with the call shape of `subprocess.run`.
    :param arguments: str; Git sub-command and its options.
    return code: int | None; exit code of Git, or None.
    """
    command = ["git", "-C", str(root), *arguments, "--", CONFIG_NAME]
    try:
        done = run(command, capture_output=True, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode


def inspect_git_state(root: Path, run: GitRunner = subprocess.run) -> GitState:
    """Ask Git whether it tracks or ignores the settings file and return the state.

    When neither the root nor any parent folder contains an entry named `.git`,
    the folder is outside every repository and Git is not started. Otherwise
    `git ls-files --error-unmatch` exits with 0 for a tracked file, and
    `git check-ignore -q` exits with 0 when an ignore rule matches and with 1 when
    none does. Any other answer gives `unknown`. The function never raises.

    :param root: Path; absolute project root that contains the settings file.
    :param run: GitRunner; function with the call shape of `subprocess.run`; tests pass a fake.
    return state: GitState; `no_repository`, `tracked`, `ignored`, `not_ignored`, or `unknown`.
    """
    if not any((folder / ".git").exists() for folder in (root, *root.parents)):
        return "no_repository"
    tracked = _ask_git(root, run, "ls-files", "--error-unmatch")
    if tracked == 0:
        return "tracked"
    if tracked != 1:
        return "unknown"
    ignored = _ask_git(root, run, "check-ignore", "-q")
    if ignored == 0:
        return "ignored"
    return "not_ignored" if ignored == 1 else "unknown"


def parse_sec_contact(text: str) -> str | None:
    """Parse settings text and return the SEC identification line `NAME EMAIL`, or None when no contact is set.

    Input (TOML text): an optional table `[sec]` with the optional text entries
    `contact_name` and `contact_email`. No other table or entry is allowed. Both
    entries empty or absent means that no contact is set. The name must have 2 to
    80 characters from plain letters, digits, spaces, and `. , ' & ( ) -`, with at
    least one letter, because it becomes part of an HTTP header. The address must
    be a normal e-mail address outside the reserved example domains.

    Raises ValueError for every invalid text. The reason never repeats a value or
    a line from the text; for a syntax error it gives only the line and column.

    :param text: str; content of the settings file.
    return line: str | None; `NAME EMAIL` with surrounding spaces removed, or None.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        position = _POSITION.search(str(exc))
        where = f" near line {position.group(1)}, column {position.group(2)}" if position else ""
        raise ValueError(f"the file is not valid TOML{where}") from None
    section = data.get("sec", {})
    if set(data) - {"sec"} or not isinstance(section, dict) or set(section) - _ALLOWED_KEYS:
        raise ValueError("the file may contain only the table [sec] with the entries contact_name and contact_email")
    name, email = section.get("contact_name", ""), section.get("contact_email", "")
    if not isinstance(name, str) or not isinstance(email, str):
        raise ValueError("contact_name and contact_email must be text between quotation marks")
    name, email = name.strip(), email.strip()
    if not name and not email:
        return None
    if not name or not email:
        raise ValueError("contact_name and contact_email must both be filled in")
    if _NAME.fullmatch(name) is None or not any(letter.isalpha() for letter in name):
        raise ValueError("contact_name must have 2 to 80 characters, at least one letter, and only plain letters"
                         " (A to Z), digits, spaces, and the characters . , ' & ( ) -")
    match = _EMAIL.fullmatch(email)
    if match is None or len(email) > 254:
        raise ValueError("contact_email must be a normal e-mail address such as name@your-domain")
    domain = match.group(1).lower()
    if any(domain == reserved or domain.endswith("." + reserved) for reserved in _RESERVED_DOMAINS) \
            or domain.endswith(_RESERVED_ENDINGS):
        raise ValueError("contact_email uses a reserved example domain; write your own address")
    return f"{name} {email}"


def _read_contact(path: Path) -> str | None:
    """Read the settings file and return the SEC identification line, or None when no contact is set.

    Reads at most 64 KiB, accepts UTF-8 with or without a byte-order mark, and
    passes the text to parse_sec_contact. Raises ValueError with a reason that
    never quotes the file content.

    :param path: Path; settings file.
    return line: str | None; `NAME EMAIL`, or None.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ValueError("the file cannot be read") from None
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError("the file is larger than 64 KiB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("the file is not UTF-8 text") from None
    return parse_sec_contact(text)


def _make_settings_issue(code: str, message: str, severity: Literal["warning", "error"] = "error") -> Issue:
    """Build one settings issue and return it.

    :param code: str; issue code such as `config_invalid`.
    :param message: str; explanation without personal details.
    :param severity: Literal["warning", "error"]; impact of the issue.
    return issue: Issue; issue whose dataset is `settings`.
    """
    return make_issue(code, message, "settings", severity=severity)


def load_settings(root: Path, *, run: GitRunner = subprocess.run) -> Settings:
    """Check and read the private settings file under the project root and return the settings record.

    The function asks Git first. A tracked file, or an existing file that Git does
    not ignore, is never opened: the contact state is `blocked`. Otherwise a missing
    file or empty values give `missing`, an unusable file gives `invalid`, and two
    valid values give `configured`. The function never raises for a settings
    problem; every problem becomes an issue, and `sec_access` carries the first
    reason that blocks SEC requests.

    Issue codes: `config_tracked_by_git` and `config_invalid` (errors);
    `config_not_ignored` (error for an existing file, warning before the file
    exists); `sec_contact_missing` and `config_git_state_unknown` (warnings).

    :param root: Path; absolute project root; the settings file is `root/config.toml`.
    :param run: GitRunner; function with the call shape of `subprocess.run`; tests pass a fake.
    return settings: Settings; state, issues, and SEC access.
    """
    path = root / CONFIG_NAME
    git_state = inspect_git_state(root, run)
    exists = path.is_file()
    issues: list[Issue] = []
    blocker: Issue | None = None
    if git_state == "tracked":
        blocker = _make_settings_issue("config_tracked_by_git", TRACKED_MESSAGE)
    elif git_state == "not_ignored":
        blocker = _make_settings_issue("config_not_ignored", NOT_IGNORED_MESSAGE, "error" if exists else "warning")
    elif git_state == "unknown":
        issues.append(_make_settings_issue("config_git_state_unknown", UNKNOWN_MESSAGE, "warning"))
    if blocker is not None and blocker.severity == "error":
        access = SecAccess(block_code=blocker.code, block_message=blocker.message)
        return Settings(exists, git_state, "blocked", access, (blocker,))
    if blocker is not None:
        issues.append(blocker)
    line: str | None = None
    state: ContactState = "missing"
    if exists:
        try:
            line = _read_contact(path)
        except ValueError as exc:
            invalid = _make_settings_issue(
                "config_invalid", f"{CONFIG_NAME} is not usable: {exc}. Compare it with {EXAMPLE_NAME}.")
            access = SecAccess(block_code=invalid.code, block_message=invalid.message)
            return Settings(exists, git_state, "invalid", access, (*issues, invalid))
    if line is None:
        missing = _make_settings_issue("sec_contact_missing", SecAccess().block_message, "warning")
        return Settings(exists, git_state, state, SecAccess(), (*issues, missing))
    return Settings(exists, git_state, "configured", SecAccess(user_agent=line), tuple(issues))


def summarize_settings(settings: Settings) -> dict[str, JsonValue]:
    """Describe the settings for the `config` command and return the JSON object.

    Output schema:
      - schema_version: int, required; always 1.
      - command: str, required; always `config`.
      - status: str, required; `error` when any issue has severity `error`, otherwise `ok`.
      - settings: dict, required:
          - file: str, required; name of the private settings file, `config.toml`.
          - template: str, required; name of the published template, `config.example.toml`.
          - exists: bool, required; whether the private file exists.
          - git_state: str, required; `no_repository`, `ignored`, `not_ignored`, `tracked`, or `unknown`.
          - sec_contact: str, required; `configured`, `missing`, `invalid`, or `blocked`.
      - issues: list[dict], required, may be empty; each item is an Issue as JSON with the keys code,
        message, severity, dataset (`settings`), symbol_id, provider, url, report_date, retryable, and
        cached_preserved.

    The object contains no name, no e-mail address, and no absolute path.

    :param settings: Settings; record from load_settings.
    return summary: dict[str, JsonValue]; JSON object following the output schema above.
    """
    status = "error" if any(issue.severity == "error" for issue in settings.issues) else "ok"
    return {
        "schema_version": 1, "command": "config", "status": status,
        "settings": {"file": CONFIG_NAME, "template": EXAMPLE_NAME, "exists": settings.exists,
                     "git_state": settings.git_state, "sec_contact": settings.sec_contact},
        "issues": [convert_to_json(issue) for issue in settings.issues],
    }
