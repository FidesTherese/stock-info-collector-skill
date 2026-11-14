"""Tests for the private settings file: validation, the Git check, and privacy.

The tests use temporary folders and a fake Git runner. One test uses the real
`git` program in a temporary repository and is skipped when Git is not installed.
The contact details in this module are made up. Their addresses end in `.test`, a name
reserved for testing that can never be a real mailbox, and no test sends them anywhere.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from stock_info.providers import SecAccess, convert_to_json  # noqa: E402
from stock_info.settings import (  # noqa: E402
    CONFIG_NAME, EXAMPLE_NAME, inspect_git_state, load_settings, parse_sec_contact, summarize_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

NAME = "Test Operator"
EMAIL = "operator@collector.test"
LINE = f"{NAME} {EMAIL}"
VALID = f'[sec]\ncontact_name = "{NAME}"\ncontact_email = "{EMAIL}"\n'
GARBAGE = b"\xff\xfe this is not a settings file"


def find_files_with_contact(root: Path, line: str) -> list[str]:
    """Search the files that Git tracks or could add for the contact details and return their paths.

    The contact line `NAME EMAIL` is split at its last space. A file is reported
    when it contains the name or the address, ignoring letter case. Ignored files,
    such as the private settings file itself, are not searched. The result holds
    only paths, so a failing test never prints the details.

    :param root: Path; folder inside a Git work tree.
    :param line: str; contact line `NAME EMAIL`.
    return paths: list[str]; root-relative paths of the files that contain a detail, sorted.
    """
    name, email = line.rsplit(" ", 1)
    needles = [name.lower().encode("utf-8"), email.lower().encode("utf-8")]
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard",
                              "-z"], check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    found = []
    for raw in listing.stdout.split(b"\0"):
        path = root / raw.decode("utf-8") if raw else None
        if path is None or not path.is_file():
            continue
        data = path.read_bytes().lower()
        if any(needle in data for needle in needles):
            found.append(raw.decode("utf-8"))
    return sorted(found)


class FakeGit:
    """Imitate `subprocess.run` for the two Git questions and record every command.

    An exit code is returned as a completed process; an exception instance is raised.

    :param tracked: int | Exception; answer to `git ls-files --error-unmatch`; 0 means tracked.
    :param ignored: int | Exception; answer to `git check-ignore -q`; 0 means ignored.
    return instance: FakeGit; callable fake runner.
    """

    def __init__(self, tracked: int | Exception = 1, ignored: int | Exception = 0) -> None:
        """Store the answers and start with empty call logs.

        :param tracked: int | Exception; answer to the tracking question.
        :param ignored: int | Exception; answer to the ignore question.
        return None: None; no value is returned.
        """
        self.tracked = tracked
        self.ignored = ignored
        self.calls: list[list[str]] = []
        self.options: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **options: Any) -> subprocess.CompletedProcess:
        """Record one command and return or raise the prepared answer.

        :param command: list[str]; program and arguments.
        :param options: Any; keyword options given to the runner.
        return done: subprocess.CompletedProcess; finished process with the prepared exit code.
        """
        self.calls.append(list(command))
        self.options.append(options)
        answer = self.tracked if "ls-files" in command else self.ignored
        if isinstance(answer, Exception):
            raise answer
        return subprocess.CompletedProcess(command, answer, "", "")


class SettingsTestCase(unittest.TestCase):
    """Provide a temporary project root that can look like a Git repository."""

    def setUp(self) -> None:
        """Create the temporary root.

        return None: None; no value is returned.
        """
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def make_repository(self) -> None:
        """Create an empty `.git` folder so that the root looks like a repository.

        return None: None; no value is returned.
        """
        (self.root / ".git").mkdir()

    def write_config(self, content: str | bytes) -> None:
        """Write the private settings file with the given content.

        :param content: str | bytes; file content; text is written as UTF-8.
        return None: None; no value is returned.
        """
        data = content.encode("utf-8") if isinstance(content, str) else content
        (self.root / CONFIG_NAME).write_bytes(data)


class ParseTests(unittest.TestCase):
    """Check which settings texts yield a contact line, nothing, or an error."""

    def test_valid_contact_returns_the_line(self) -> None:
        """Both values, with surrounding spaces removed, form the line NAME EMAIL."""
        self.assertEqual(parse_sec_contact(VALID), LINE)
        padded = f'[sec]\ncontact_name = "  {NAME} "\ncontact_email = " {EMAIL}  "\n'
        self.assertEqual(parse_sec_contact(padded), LINE)

    def test_unset_contact_returns_none(self) -> None:
        """An empty file, an empty table, and two empty values all mean that no contact is set."""
        for text in ("", "# only a comment\n", "[sec]\n", '[sec]\ncontact_name = ""\ncontact_email = "  "\n'):
            self.assertIsNone(parse_sec_contact(text), text)

    def test_invalid_files_raise_without_quoting_their_content(self) -> None:
        """Every invalid file raises ValueError, and the reason never repeats a value from the file."""
        secret_name, secret_mail = "Hidden Person", "hidden.person@private-mail.test"
        cases = [
            f'[sec]\ncontact_name = "{secret_name}"\ncontact_email = ""\n',
            f'[sec]\ncontact_name = ""\ncontact_email = "{secret_mail}"\n',
            f'[other]\nnote = "{secret_name}"\n',
            f'top_level = "{secret_name}"\n',
            f'[sec]\ncontact_name = "{secret_name}"\ncontact_email = "{secret_mail}"\ncontact_phone = "12345"\n',
            f'[sec]\ncontact_name = 5\ncontact_email = "{secret_mail}"\n',
            f'sec = "{secret_name}"\n',
            f'[sec]\ncontact_name = "Jos\u00e9 {secret_name}"\ncontact_email = "{secret_mail}"\n',
            f'[sec]\ncontact_name = "{secret_name}\\nX-Injected: 1"\ncontact_email = "{secret_mail}"\n',
            f'[sec]\ncontact_name = "{"A" * 81}"\ncontact_email = "{secret_mail}"\n',
            '[sec]\ncontact_name = "12345"\ncontact_email = "digits@private-mail.test"\n',
            f'[sec]\ncontact_name = "{secret_name}"\ncontact_email = "not-an-address"\n',
            f'[sec]\ncontact_name = "{secret_name}"\ncontact_email = "two@@private-mail.test"\n',
            f'[sec]\ncontact_name = "{secret_name}"\ncontact_email = "someone@example.com"\n',
            f'[sec]\ncontact_name = "{secret_name}"\ncontact_email = "someone@mail.invalid"\n',
            f'[sec\ncontact_name = "{secret_name}"\n',
        ]
        for text in cases:
            with self.assertRaises(ValueError, msg=text) as ctx:
                parse_sec_contact(text)
            reason = str(ctx.exception)
            self.assertTrue(reason, text)
            for secret in (secret_name, secret_mail, "12345", "someone@", "X-Injected"):
                self.assertNotIn(secret, reason, text)

    def test_syntax_error_reports_only_the_position(self) -> None:
        """A syntax error names the line and column so that the person can find it."""
        with self.assertRaises(ValueError) as ctx:
            parse_sec_contact('[sec]\ncontact_name = "unfinished\n')
        self.assertIn("line 2", str(ctx.exception))


class GitStateTests(SettingsTestCase):
    """Check how the answers of Git become a state."""

    def test_folder_outside_a_repository_does_not_start_git(self) -> None:
        """Without a `.git` entry in the root or its parents, Git is not started."""
        git = FakeGit()
        self.assertEqual(inspect_git_state(self.root, git), "no_repository")
        self.assertEqual(git.calls, [])

    def test_exit_codes_become_states(self) -> None:
        """Tracked wins over ignored; unexpected answers and a missing program give unknown."""
        self.make_repository()
        cases: list[tuple[int | Exception, int | Exception, str, int]] = [
            (0, 0, "tracked", 1),
            (1, 0, "ignored", 2),
            (1, 1, "not_ignored", 2),
            (1, 128, "unknown", 2),
            (128, 0, "unknown", 1),
            (FileNotFoundError("git"), 0, "unknown", 1),
            (1, subprocess.TimeoutExpired("git", 5), "unknown", 2),
        ]
        for tracked, ignored, expected, call_count in cases:
            git = FakeGit(tracked, ignored)
            self.assertEqual(inspect_git_state(self.root, git), expected, (tracked, ignored))
            self.assertEqual(len(git.calls), call_count, (tracked, ignored))

    def test_commands_are_fixed_and_run_without_a_shell(self) -> None:
        """Git receives fixed arguments, the fixed file name, a timeout, and no shell."""
        self.make_repository()
        git = FakeGit(1, 0)
        inspect_git_state(self.root, git)
        prefix = ["git", "-C", str(self.root)]
        self.assertEqual(git.calls, [prefix + ["ls-files", "--error-unmatch", "--", "config.toml"],
                                     prefix + ["check-ignore", "-q", "--", "config.toml"]])
        for options in git.options:
            self.assertFalse(options.get("shell", False))
            self.assertEqual(options["timeout"], 5.0)
            self.assertIs(options["stdin"], subprocess.DEVNULL)

    def test_parent_repository_is_found(self) -> None:
        """A skill folder inside another repository is checked against that repository."""
        self.make_repository()
        nested = self.root / "skills" / "collector"
        nested.mkdir(parents=True)
        git = FakeGit(1, 1)
        self.assertEqual(inspect_git_state(nested, git), "not_ignored")
        self.assertEqual(git.calls[0][:3], ["git", "-C", str(nested)])

    @unittest.skipUnless(shutil.which("git"), "the git program is not installed")
    def test_real_git_answers(self) -> None:
        """The real Git program reports not ignored, then ignored, then tracked."""
        def git(*arguments: str) -> None:
            """Run one real Git command in the temporary root and fail the test on an error.

            :param arguments: str; Git arguments after `git -C ROOT`.
            return None: None; no value is returned.
            """
            subprocess.run(["git", "-C", str(self.root), *arguments], check=True, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=30)

        git("init", "-q", ".")
        self.write_config(VALID)
        self.assertEqual(inspect_git_state(self.root), "not_ignored")
        with (self.root / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
            handle.write("\n/config.toml\n")
        self.assertEqual(inspect_git_state(self.root), "ignored")
        git("add", "-f", "config.toml")
        self.assertEqual(inspect_git_state(self.root), "tracked")


class LoadSettingsTests(SettingsTestCase):
    """Check the contact state, the issues, and the SEC access record for every situation."""

    def codes(self, settings: Any) -> list[tuple[str, str]]:
        """Return the issue codes and severities of a settings record.

        :param settings: Any; Settings record.
        return codes: list[tuple[str, str]]; (code, severity) pairs in order.
        """
        return [(issue.code, issue.severity) for issue in settings.issues]

    def test_missing_file_outside_a_repository(self) -> None:
        """Without a file the contact is missing, and SEC requests are blocked with that reason."""
        settings = load_settings(self.root, run=FakeGit())
        self.assertEqual((settings.exists, settings.git_state, settings.sec_contact),
                         (False, "no_repository", "missing"))
        self.assertEqual(self.codes(settings), [("sec_contact_missing", "warning")])
        self.assertIsNone(settings.sec_access.user_agent)
        self.assertEqual(settings.sec_access.block_code, "sec_contact_missing")

    def test_valid_ignored_file_is_configured(self) -> None:
        """A valid file that Git ignores yields the line and no issue; a byte-order mark is accepted."""
        self.make_repository()
        for content in (VALID, b"\xef\xbb\xbf" + VALID.encode("utf-8")):
            self.write_config(content)
            settings = load_settings(self.root, run=FakeGit(1, 0))
            self.assertEqual((settings.exists, settings.git_state, settings.sec_contact),
                             (True, "ignored", "configured"))
            self.assertEqual(settings.issues, ())
            self.assertEqual(settings.sec_access.user_agent, LINE)

    def test_tracked_file_is_blocked_and_never_opened(self) -> None:
        """A tracked file blocks the contact; unreadable content proves that the file was not parsed."""
        self.make_repository()
        for content in (VALID, GARBAGE):
            self.write_config(content)
            settings = load_settings(self.root, run=FakeGit(0, 0))
            self.assertEqual(settings.sec_contact, "blocked")
            self.assertEqual(self.codes(settings), [("config_tracked_by_git", "error")])
            self.assertIsNone(settings.sec_access.user_agent)
            self.assertEqual(settings.sec_access.block_code, "config_tracked_by_git")
            self.assertIn("git rm --cached", settings.sec_access.block_message)

    def test_unignored_file_is_blocked_and_never_opened(self) -> None:
        """A file that Git does not ignore blocks the contact and is not parsed."""
        self.make_repository()
        for content in (VALID, GARBAGE):
            self.write_config(content)
            settings = load_settings(self.root, run=FakeGit(1, 1))
            self.assertEqual(settings.sec_contact, "blocked")
            self.assertEqual(self.codes(settings), [("config_not_ignored", "error")])
            self.assertIsNone(settings.sec_access.user_agent)
            self.assertEqual(settings.sec_access.block_code, "config_not_ignored")

    def test_missing_file_that_git_would_not_ignore_warns_early(self) -> None:
        """Before the file exists, a missing ignore rule is a warning beside the missing contact."""
        self.make_repository()
        settings = load_settings(self.root, run=FakeGit(1, 1))
        self.assertEqual((settings.exists, settings.sec_contact), (False, "missing"))
        self.assertEqual(self.codes(settings), [("config_not_ignored", "warning"), ("sec_contact_missing", "warning")])
        self.assertEqual(settings.sec_access.block_code, "sec_contact_missing")

    def test_invalid_files(self) -> None:
        """A wrong value, bytes that are not UTF-8, and an oversized file are all invalid."""
        secret = "hidden.person@private-mail.test"
        contents: list[str | bytes] = [
            f'[sec]\ncontact_name = ""\ncontact_email = "{secret}"\n',
            GARBAGE,
            "# " + "x" * (64 * 1024) + "\n" + VALID,
        ]
        for content in contents:
            self.write_config(content)
            settings = load_settings(self.root, run=FakeGit())
            self.assertEqual(settings.sec_contact, "invalid")
            self.assertEqual(self.codes(settings), [("config_invalid", "error")])
            self.assertIsNone(settings.sec_access.user_agent)
            self.assertEqual(settings.sec_access.block_code, "config_invalid")
            self.assertNotIn(secret, settings.issues[0].message)

    def test_empty_values_mean_missing(self) -> None:
        """A copied template with empty values is a missing contact, not an error."""
        self.write_config('[sec]\ncontact_name = ""\ncontact_email = ""\n')
        settings = load_settings(self.root, run=FakeGit())
        self.assertEqual((settings.exists, settings.sec_contact), (True, "missing"))
        self.assertEqual(self.codes(settings), [("sec_contact_missing", "warning")])

    def test_unknown_git_state_still_reads_the_file_with_a_warning(self) -> None:
        """When Git gives no usable answer, the contact is read and a warning asks for a manual check."""
        self.make_repository()
        self.write_config(VALID)
        settings = load_settings(self.root, run=FakeGit(FileNotFoundError("git")))
        self.assertEqual((settings.git_state, settings.sec_contact), ("unknown", "configured"))
        self.assertEqual(self.codes(settings), [("config_git_state_unknown", "warning")])
        self.assertEqual(settings.sec_access.user_agent, LINE)


class PrivacyTests(SettingsTestCase):
    """Check that the contact details never appear in text that the program may print or store."""

    def test_details_never_appear_in_printable_or_stored_forms(self) -> None:
        """The repr, the JSON summary, the stored JSON form, and the issues hide the details."""
        self.make_repository()
        self.write_config(VALID)
        settings = load_settings(self.root, run=FakeGit(1, 0))
        self.assertEqual(settings.sec_access.user_agent, LINE)
        blocked = load_settings(self.root, run=FakeGit(0, 0))
        texts = [repr(settings), repr(settings.sec_access), str(summarize_settings(settings)),
                 str(convert_to_json(settings.sec_access)), repr(blocked), str(summarize_settings(blocked)),
                 repr(SecAccess(user_agent=LINE))]
        for text in texts:
            for secret in (NAME, EMAIL, str(self.root)):
                self.assertNotIn(secret, text)
        self.assertEqual(repr(settings.sec_access), "SecAccess(configured)")
        self.assertEqual(repr(blocked.sec_access), "SecAccess(blocked: config_tracked_by_git)")

    def test_summary_shape(self) -> None:
        """The summary for the config command has a fixed shape and reports errors in its status."""
        self.make_repository()
        self.write_config(VALID)
        summary = summarize_settings(load_settings(self.root, run=FakeGit(1, 0)))
        self.assertEqual(summary, {
            "schema_version": 1, "command": "config", "status": "ok",
            "settings": {"file": "config.toml", "template": "config.example.toml", "exists": True,
                         "git_state": "ignored", "sec_contact": "configured"},
            "issues": [],
        })
        blocked = summarize_settings(load_settings(self.root, run=FakeGit(0, 0)))
        self.assertEqual((blocked["status"], blocked["settings"]["sec_contact"]), ("error", "blocked"))
        issue = blocked["issues"][0]
        self.assertEqual((issue["code"], issue["severity"], issue["dataset"]),
                         ("config_tracked_by_git", "error", "settings"))
        missing = summarize_settings(load_settings(self.root / "absent", run=FakeGit(1, 0)))
        self.assertEqual((missing["status"], missing["settings"]["exists"]), ("ok", False))


class RepositoryTests(unittest.TestCase):
    """Check the published template and the real project folder."""

    def test_published_template_contains_no_contact(self) -> None:
        """The template is valid and empty, so nobody's details can be published through it."""
        text = (PROJECT_ROOT / EXAMPLE_NAME).read_text(encoding="utf-8")
        self.assertIsNone(parse_sec_contact(text))
        self.assertIn('contact_name = ""', text)
        self.assertIn('contact_email = ""', text)

    def test_git_does_not_track_the_private_file(self) -> None:
        """In the real project folder, Git must never track the private settings file."""
        self.assertNotEqual(inspect_git_state(PROJECT_ROOT), "tracked")

    @unittest.skipUnless(shutil.which("git"), "the git program is not installed")
    def test_search_finds_details_in_publishable_files_only(self) -> None:
        """The search reports tracked and untracked files with a detail, and skips ignored files."""
        with TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(["git", "-C", str(root), "init", "-q", "."], check=True, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=30)
            (root / ".git" / "info" / "exclude").write_text("config.toml\n", encoding="utf-8")
            (root / "config.toml").write_text(VALID, encoding="utf-8")
            (root / "tracked.py").write_text(f"AUTHOR = '{NAME.upper()}'\n", encoding="utf-8")
            (root / "notes.txt").write_text(f"write to {EMAIL}\n", encoding="utf-8")
            (root / "clean.txt").write_text("nothing personal\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "tracked.py"], check=True, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=30)
            self.assertEqual(find_files_with_contact(root, LINE), ["notes.txt", "tracked.py"])

    @unittest.skipUnless(shutil.which("git"), "the git program is not installed")
    def test_no_publishable_file_contains_the_configured_contact(self) -> None:
        """When the owner has filled in the private settings file, no other file may repeat the details."""
        line = load_settings(PROJECT_ROOT).sec_access.user_agent
        if line is None:
            self.skipTest("no usable private settings file in this project folder")
        self.assertEqual(find_files_with_contact(PROJECT_ROOT, line), [],
                         "these files repeat the contact details from config.toml")


if __name__ == "__main__":
    unittest.main()
