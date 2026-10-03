"""End-to-end runs of the command line, always offline."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from context import FIXTURES, SCRIPT, cc

SAMPLE = os.path.join(FIXTURES, "sample.md")


def run(*args, **kw):
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, **kw)


ASCII_LOCALE = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0",
                "PYTHONCOERCECLOCALE": "0"}


class TestVersionFlag(unittest.TestCase):
    def test_version_flag_prints_version_and_exits_zero(self):
        proc = run("--version")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(cc.__version__, proc.stdout)


class TestUncheckedEntriesAreAnnounced(unittest.TestCase):
    """A run where nobody could reach the authorities must not read as a pass."""

    def refs(self):
        r = cc.parse_entry("Smith, J. (2020) 'A paper about things', Journal of Things.")
        r.status, r.unchecked = "UNVERIFIABLE", True
        r.notes.append("could not search OpenAlex (HTTP 429)")
        return [r]

    def test_the_summary_says_they_were_not_checked(self):
        report = cc.build_report(self.refs(), [], "doc.md", offline=False)
        self.assertIn("could not be checked at all", report)
        self.assertNotIn("**No entry failed verification.**\n\n## Entries", report)

    def test_the_mailto_remedy_is_offered_when_no_address_was_given(self):
        report = cc.build_report(self.refs(), [], "doc.md", offline=False)
        self.assertIn("--mailto", report)

    def test_the_remedy_is_not_repeated_at_someone_already_using_it(self):
        report = cc.build_report(self.refs(), [], "doc.md", offline=False, polite=True)
        self.assertIn("could not be checked at all", report)
        self.assertNotIn("--mailto", report)

    def test_a_clean_run_says_nothing_about_unchecked_entries(self):
        r = cc.parse_entry("Smith, J. (2020) 'A paper about things', Journal of Things.")
        r.status = "VERIFIED"
        self.assertNotIn("could not be checked at all",
                         cc.build_report([r], [], "doc.md", offline=False))


class TestCheckCommand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = os.path.join(cls.tmp.name, "report.md")
        cls.data = os.path.join(cls.tmp.name, "data.json")
        cls.proc = run("check", SAMPLE, "--offline",
                       "--out", cls.out, "--json", cls.data,
                       "--cache", os.path.join(cls.tmp.name, "cache"))
        with open(cls.out, encoding="utf-8") as fh:
            cls.report = fh.read()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_an_offline_run_with_an_uncited_entry_exits_two(self):
        self.assertEqual(self.proc.returncode, 2, self.proc.stderr)

    def test_the_report_names_the_document_and_the_totals(self):
        self.assertIn("# Citation check — sample.md", self.report)
        self.assertIn("8 reference entries, 8 in-text citation instances.", self.report)

    def test_offline_runs_say_so_prominently(self):
        # Nobody should mistake an offline run for a verified bibliography.
        self.assertIn("offline mode, nothing was verified against a live source",
                      self.report)

    def test_uncited_entries_are_listed(self):
        section = self.report.split("### Reference-list entries never cited")[1]
        self.assertIn("Uncited (2021)", section.split("###")[0])

    def test_unresolved_citations_are_listed_with_their_caveat(self):
        section = self.report.split("### In-text citations with no matching")[1]
        self.assertIn("Nonexistent (2019)", section)
        self.assertIn("Expect false positives here", section)

    def test_the_report_states_what_it_did_not_check(self):
        self.assertIn("supports the claim it is attached to", self.report)

    def test_progress_goes_to_stderr_so_stdout_stays_pipeable(self):
        self.assertIn("[1/8]", self.proc.stderr)

    def test_json_output_is_machine_readable(self):
        with open(self.data, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(len(data["references"]), 8)
        self.assertEqual(len(data["citations"]), 8)
        self.assertEqual(data["style"], "author-date")
        self.assertTrue(all("status" in r for r in data["references"]))

    def test_the_report_names_the_citation_style(self):
        self.assertIn("Style: author-date (detected)", self.report)


class TestStyleOverride(unittest.TestCase):
    def test_a_forced_style_is_reported_as_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.md")
            proc = run("check", SAMPLE, "--offline", "--style", "author-date",
                       "--out", out, "--cache", os.path.join(tmp, "cache"))
            with open(out, encoding="utf-8") as fh:
                report = fh.read()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("Style: author-date (forced)", report)

    def test_an_unknown_style_is_rejected_before_any_work(self):
        proc = run("check", SAMPLE, "--style", "footnotes")
        self.assertNotEqual(proc.returncode, 0)


class TestNumericDocument(unittest.TestCase):
    """End-to-end over the IEEE-shaped fixture, offline."""

    def test_check_reports_the_expected_totals_and_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.md")
            proc = run("check", os.path.join(FIXTURES, "sample-numeric.md"),
                       "--offline", "--out", out,
                       "--cache", os.path.join(tmp, "cache"))
            with open(out, encoding="utf-8") as fh:
                report = fh.read()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("10 reference entries, 17 in-text citation instances.", report)
        self.assertIn("Style: numeric (detected)", report)

    def test_the_orphan_marker_is_reported_with_its_bracket_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.md")
            run("check", os.path.join(FIXTURES, "sample-numeric.md"),
                "--offline", "--out", out, "--cache", os.path.join(tmp, "cache"))
            with open(out, encoding="utf-8") as fh:
                report = fh.read()
        section = report.split("### In-text citations with no matching")[1]
        self.assertIn("`[12]`", section)

    def test_claims_dedupes_repeated_markers_in_one_sentence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "claims.md")
            run("claims", os.path.join(FIXTURES, "sample-numeric.md"),
                "--out", out)
            with open(out, encoding="utf-8") as fh:
                worklist = fh.read()
        # 17 marker instances, 13 unique source-and-sentence rows.
        self.assertEqual(worklist.count("- **Verdict**:"), 13)


class TestChicagoAuthorDateDocument(unittest.TestCase):
    """End-to-end over the Chicago fixture, offline like every CLI test."""

    def test_check_reports_the_expected_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.md")
            proc = run("check", os.path.join(FIXTURES, "sample-chicago-ad.md"),
                       "--offline", "--out", out,
                       "--cache", os.path.join(tmp, "cache"))
            with open(out, encoding="utf-8") as fh:
                report = fh.read()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("7 reference entries, 8 in-text citation instances.", report)
        self.assertIn("Style: author-date (detected)", report)


class TestMlaDocument(unittest.TestCase):
    """End-to-end over the MLA fixture, offline."""

    def test_check_reports_the_expected_totals_and_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.md")
            proc = run("check", os.path.join(FIXTURES, "sample-mla.md"),
                       "--offline", "--out", out,
                       "--cache", os.path.join(tmp, "cache"))
            with open(out, encoding="utf-8") as fh:
                report = fh.read()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("6 reference entries, 7 in-text citation instances.", report)
        self.assertIn("Style: mla (detected)", report)

    def test_unresolved_citations_render_as_author_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.md")
            run("check", os.path.join(FIXTURES, "sample-mla.md"),
                "--offline", "--out", out, "--cache", os.path.join(tmp, "cache"))
            with open(out, encoding="utf-8") as fh:
                report = fh.read()
        section = report.split("### In-text citations with no matching")[1]
        self.assertIn("`Nonexistent 7`", section)
        self.assertIn("`Smith 42`", section)


class TestNotesDocument(unittest.TestCase):
    """End-to-end over the footnotes fixture, offline."""

    def test_check_reports_the_expected_totals_and_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.md")
            proc = run("check", os.path.join(FIXTURES, "sample-notes.md"),
                       "--offline", "--out", out,
                       "--cache", os.path.join(tmp, "cache"))
            with open(out, encoding="utf-8") as fh:
                report = fh.read()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("5 reference entries, 6 in-text citation instances.", report)
        self.assertIn("Style: notes (detected)", report)

    def test_the_orphan_marker_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.md")
            run("check", os.path.join(FIXTURES, "sample-notes.md"),
                "--offline", "--out", out, "--cache", os.path.join(tmp, "cache"))
            with open(out, encoding="utf-8") as fh:
                report = fh.read()
        section = report.split("### In-text citations with no matching")[1]
        self.assertIn("`[9]`", section)

    def test_claims_rows_carry_the_sentence(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "claims.md")
            run("claims", os.path.join(FIXTURES, "sample-notes.md"), "--out", out)
            with open(out, encoding="utf-8") as fh:
                worklist = fh.read()
        self.assertEqual(worklist.count("- **Verdict**:"), 6)
        self.assertIn("Prompt injection was characterised", worklist)


class TestClaimsCommand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        out = os.path.join(cls.tmp.name, "claims.md")
        cls.proc = run("claims", SAMPLE, "--out", out)
        with open(out, encoding="utf-8") as fh:
            cls.worklist = fh.read()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exits_zero(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)

    def test_one_row_per_in_text_citation(self):
        self.assertEqual(self.worklist.count("- **Verdict**:"), 8)

    def test_each_row_carries_the_sentence_making_the_claim(self):
        self.assertIn("Prompt injection was first characterised", self.worklist)

    def test_rows_carry_a_locator_for_the_source(self):
        self.assertIn("arXiv:2406.13352", self.worklist)
        self.assertIn("https://modelcontextprotocol.io/specification", self.worklist)

    def test_a_citation_with_no_entry_says_so_rather_than_inventing_one(self):
        self.assertIn("_no matching reference-list entry_", self.worklist)

    def test_the_worklist_forbids_guessing(self):
        self.assertIn("Do not guess", self.worklist)


class TestExitCodes(unittest.TestCase):
    REFERENCE = "Smith, J. (2020) 'A paper about things', Journal of Things."
    SECOND = "Jones, A. (2021) 'Another paper about things', Journal of Things."
    UNCITED = "Brown, B. (2022) 'A third paper about things', Journal of Things."
    REFERENCES = f"{REFERENCE}\n\n{SECOND}\n\n{UNCITED}"
    CITED_BODY = "The sources (Smith, 2020), (Jones, 2021), and (Brown, 2022) agree."

    def check_document(self, body, references):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "paper.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"# Paper\n\n{body}\n\n## References\n\n{references}\n")
            return run("check", path, "--offline", "--cache", os.path.join(tmp, "cache"))

    def test_uncited_reference_exits_two_offline(self):
        proc = self.check_document(
            "The sources (Smith, 2020) and (Jones, 2021) support this.",
            self.REFERENCES,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("Brown (2022)", proc.stdout)

    def test_ambiguous_suffix_exits_two_without_uncited_references(self):
        proc = self.check_document(
            "The drafts (IETF, 2025a) and (IETF, 2025b) differ. "
            "The overview (IETF, 2025) does not say which. "
            "The comparison (Smith, 2020) agrees.",
            "IETF (2025a) 'The OAuth 2.1 Framework', Internet-Draft.\n\n"
            "IETF (2025b) 'OAuth Security Topics', Internet-Draft.\n\n"
            "Smith, J. (2020) 'A paper about things', Journal of Things.",
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("cited without a suffix", proc.stdout)
        uncited = proc.stdout.split("### Reference-list entries never cited in the text")[1]
        self.assertIn("_None._", uncited.split("###")[0])

    def test_unresolved_citation_alone_does_not_fail(self):
        proc = self.check_document(
            f"{self.CITED_BODY} (Missing, 2019) has no entry.",
            self.REFERENCES,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("`Missing (2019)`", proc.stdout)

    def test_valid_document_exits_zero(self):
        proc = self.check_document(self.CITED_BODY, self.REFERENCES)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("### Reference-list entries never cited in the text\n\n_None._", proc.stdout)

    def test_verified_references_still_fail_for_an_uncited_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "paper.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# Paper\n\nThe sources (Smith, 2020) and (Jones, 2021) "
                         f"support this.\n\n## References\n\n{self.REFERENCES}\n")

            def fake_verify(ref, fetcher):
                ref.status = "VERIFIED"

            argv = ["touchneedle.py", "check", path, "--cache", os.path.join(tmp, "cache")]
            with mock.patch.object(cc, "verify", fake_verify), \
                 mock.patch.object(sys, "argv", argv), \
                 contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cc.main(), 2)

    def test_a_problem_status_exits_two_so_it_can_gate_ci(self):
        def fake_verify(ref, fetcher):
            ref.status = "MISMATCH"
            ref.notes.append("planted by the test")

        with tempfile.TemporaryDirectory() as tmp:
            argv = ["touchneedle.py", "check", SAMPLE,
                    "--out", os.path.join(tmp, "r.md"),
                    "--cache", os.path.join(tmp, "cache")]
            with mock.patch.object(cc, "verify", fake_verify), \
                 mock.patch.object(sys, "argv", argv), \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cc.main(), 2)

    def test_references_are_verified_concurrently_but_reported_in_order(self):
        refs = [
            cc.parse_entry("First, A. (2020) 'First paper', Journal of Things."),
            cc.parse_entry("Second, B. (2021) 'Second paper', Journal of Things."),
        ]
        for ref in refs:
            ref.cited_by.append("cited in test document")
        met = threading.Barrier(2)
        second_done = threading.Event()
        completed = []

        def fake_verify(ref, fetcher):
            met.wait(timeout=1)
            if ref is refs[0]:
                self.assertTrue(second_done.wait(timeout=1))
            else:
                ref.status = "VERIFIED"
                completed.append(ref.name)
                second_done.set()
                return
            ref.status = "VERIFIED"
            completed.append(ref.name)

        with tempfile.TemporaryDirectory() as tmp:
            argv = ["touchneedle.py", "check", "doc.md", "--out",
                    os.path.join(tmp, "report.md"), "--cache",
                    os.path.join(tmp, "cache")]
            stderr = io.StringIO()
            with mock.patch.object(cc, "collect", return_value=(refs, [], "author-date")), \
                 mock.patch.object(cc, "verify", fake_verify), \
                 mock.patch.object(sys, "argv", argv), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(cc.main(), 0)

        self.assertEqual(completed, ["Second", "First"])
        progress = stderr.getvalue()
        self.assertLess(progress.index("    [1/2] VERIFIED"),
                        progress.index("    [2/2] VERIFIED"))

    def test_an_unreadable_document_fails_loudly(self):
        proc = run("check", os.path.join(FIXTURES, "does-not-exist.md"), "--offline")
        self.assertNotEqual(proc.returncode, 0)

    def test_a_document_with_no_reference_list_explains_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bare.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# Just a document\n\nNo reference list here.\n")
            proc = run("check", path, "--offline")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no 'References'", proc.stderr)


class TestMailtoIsOptIn(unittest.TestCase):
    def test_mailto_defaults_to_unset(self):
        # It goes to third parties, so it must never be inferred.
        with tempfile.TemporaryDirectory() as tmp:
            f = cc.Fetcher(tmp, offline=True)
        self.assertIsNone(f.mailto)

    def test_help_documents_that_mailto_is_sent_to_third_parties(self):
        proc = run("check", "--help")
        self.assertIn("Crossref/OpenAlex", proc.stdout)


class TestALegacyCodePageDoesNotCrashARun(unittest.TestCase):
    """Issue #30: a legacy code page must not crash a run."""

    def test_a_report_survives_an_ascii_stdout(self):
        proc = run("check", SAMPLE, "--offline", env=ASCII_LOCALE)
        self.assertEqual(proc.returncode, 2, proc.stderr)

    def test_the_claims_worklist_survives_an_ascii_stdout(self):
        proc = run("claims", SAMPLE, env=ASCII_LOCALE)
        self.assertEqual(proc.returncode, 0, proc.stderr)


@unittest.skipUnless(shutil.which("pandoc"), "pandoc is not installed")
class TestAConvertedDocumentKeepsItsAccents(unittest.TestCase):
    """Issue #31: a document converted by pandoc keeps its accents."""

    SOURCE = """\
# A short paper

Müller (2019) argues the point, and Lévy (2020) agrees.
A third view is offered by Sørensen (2021).

## References

Müller, J. (2019) 'Über die Ordnung der Dinge', Journal of Things, 12(3), pp. 1-20.

Lévy, C. (2020) 'Une étude des systèmes', Revue des Systèmes, 8(1), pp. 30-45.

Sørensen, K. (2021) 'Målinger og resultater', Nordic Journal of Measurement, 4(2), pp. 5-19.
"""

    def test_a_docx_is_read_as_utf8_under_an_ascii_locale(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = os.path.join(tmp, "doc.md")
            docx = os.path.join(tmp, "doc.docx")
            with open(md, "w", encoding="utf-8") as fh:
                fh.write(self.SOURCE)
            subprocess.run(["pandoc", md, "-o", docx], check=True, capture_output=True)
            proc = run("check", docx, "--offline", env=ASCII_LOCALE)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Müller", proc.stdout)





if __name__ == "__main__":
    unittest.main()
