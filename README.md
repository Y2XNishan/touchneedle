# touchneedle

[![PyPI](https://img.shields.io/pypi/v/touchneedle)](https://pypi.org/project/touchneedle/)
[![Python versions](https://img.shields.io/pypi/pyversions/touchneedle)](https://pypi.org/project/touchneedle/)
[![Tests](https://github.com/nicoleman0/touchneedle/actions/workflows/test.yml/badge.svg)](https://github.com/nicoleman0/touchneedle/actions/workflows/test.yml)
[![Licence](https://img.shields.io/pypi/l/touchneedle)](LICENSE)

Verifies that the citations in a document are real, accurately described, and
consistently used — including the fabricated-citation signature an AI-drafted
bibliography produces: a real title carrying the wrong authors, or a plausible
reference to a paper that does not exist.

Useful for students and examiners, and for anyone checking a reference list a
model wrote.

## Quick start

```bash
pip install touchneedle
touchneedle check thesis.docx --out report.md
```

`report.md` opens with the entries that failed a check, worst first, then the
cross-reference pass in both directions. Exit status is 2 when something needs
attention and 0 when the list is clean, so the same command drops into CI
unchanged.

Nothing is needed beyond Python 3.11+. `pandoc` has to be on PATH for `.docx`
input, and for nothing else.

## Why not a .bib checker

Most commercial citation checkers want a `.bib` file and check it against academic
databases. That covers journal articles but misses standards,
specifications, vendor documentation, and blog posts. In a lot of real
bibliographies, this is half the list.

So this tool parses a **prose reference list** straight out of Markdown or
`.docx`, in the four style families a real document uses — author-date
(Harvard, APA, Chicago author-date), numeric (IEEE, Vancouver/AMA), MLA, and
footnote styles (Chicago notes, MHRA) — and routes each entry to whichever
authority can actually confirm it. The style is auto-detected, or forced with
`--style`.

## What it checks

**Existence and metadata** — scripted and deterministic:

| Entry carries | Checked against |
|---|---|
| arXiv id | arXiv API |
| DOI | Crossref |
| RFC number | IETF datatracker, falling back to rfc-editor |
| `draft-*` name | IETF datatracker, **including whether the cited revision is still current** |
| Quoted title in an academic venue | Crossref, then OpenAlex, by title |
| A URL and nothing else | Fetched live; page title compared with the cited title |

Entries with both an identifier and a URL get both, so a real paper behind a dead
link is still reported. An entry the retrieved record contradicts comes back as
`MISMATCH` — but only where the record is the cited work by construction, which
means an identifier. A title search returns neighbours, so a poor match there is
reported as not found, with the closest candidate named, rather than as a
disagreement the search is in no position to assert.

**Internal consistency** — every in-text citation resolves to a list entry, every
list entry is cited somewhere, and `2025a`/`2025b` suffixes are used
unambiguously. Bracket markers resolve by number, author-page citations by
surname, footnote markers through their note — a shortened note or an `Ibid.`
links to the full citation it repeats.

**Claim support** — the pass that needs reading rather than fetching. `claims`
emits a worklist pairing each in-text citation with the sentence making the claim
and a locator for the source, to be ruled SUPPORTED / PARTIAL / UNSUPPORTED /
INACCESSIBLE one row at a time. This catches the failure the database checks
cannot: a genuine source attached to a claim it does not make. Who does that
reading is the subject of [Two ways to run it](#two-ways-to-run-it).

## What it produces

A Markdown report, worst findings first. From the test fixture, run live:

```markdown
## Entries needing attention

### STALE — IETF (2025a)

> IETF (2025a) 'The OAuth 2.1 Authorization Framework', Internet-Draft draft-ietf-oauth-v2-1-13.

- cited as -13 but the current revision is -15; an Internet-Draft is a moving
  target, so confirm the cited text survived

### NOT_FOUND — Uncited (2021)

> Uncited, A. (2021) 'A paper that nobody in this document cites', Journal of
> Irreproducible Results. doi:10.1000/uncited.

- Crossref has no record for DOI 10.1000/uncited

## Cross-reference consistency

### In-text citations with no matching reference entry

- `Nonexistent (2019)` — …An orphan citation appears here (Nonexistent, 2019).…
```

[The full report](docs/example-report.md) — five entries verified against arXiv,
Crossref and the IETF datatracker, one link that has quietly moved, and the
cross-reference pass in both directions.

`--json` writes the same results machine-readably, for a CI step or a dashboard.

## Two ways to run it

touchneedle is a command-line program. `check` is entirely scripted — it fetches
records and compares fields, so it returns the same answer every run, with no
model involved anywhere in it.

`claims` is the half the script cannot finish. It emits a worklist: each in-text
citation, the sentence that cites it, and a locator for the source. Ruling on
those rows means reading the sources, which is a judgement rather than a lookup.

So there are two ways to work that list. Do it yourself, from a terminal. Or run
the tool inside a coding agent, which calls the same script for the scripted
pass and then reads each source to fill in the second. The agent path is not a
different tool and not a wrapper — it is `scripts/touchneedle.py` either way.

### From a terminal

```bash
pip install touchneedle

touchneedle check thesis.docx --out report.md --json data.json
touchneedle claims thesis.docx --out claims.md
```

From a clone, without installing, that is `python3 scripts/touchneedle.py …` —
the same file.

### Inside Claude Code

As a skill:

```bash
git clone https://github.com/nicoleman0/touchneedle ~/.claude/skills/touchneedle
```

Or as a plugin:

```
/plugin marketplace add nicoleman0/touchneedle
/plugin install touchneedle
```

Either way, ask for it in plain words — *"check the citations in thesis.docx"* —
and the model runs `check`, reads the report, then works the claims worklist
source by source.

## Options and exit codes

Both subcommands take the document, and:

| Option | Effect |
|---|---|
| `--out FILE` | Write the report here instead of stdout |
| `--style STYLE` | `auto` (default), `author-date`, `numeric`, `mla` or `notes`. Force one when detection guesses wrong |

`check` takes five more, all of them about reaching the network:

| Option | Effect |
|---|---|
| `--json FILE` | Also write the results machine-readably, for a CI step or a dashboard |
| `--offline` | Parse and cross-check only, contacting nothing. Useful for smoke-testing the parse |
| `--cache DIR` | HTTP cache, 7-day TTL, so re-runs are nearly free |
| `--timeout N` | Per-request timeout, 25 seconds by default |
| `--mailto ADDRESS` | Contact address for Crossref and OpenAlex's polite rate-limit pool |

`--mailto` is worth more than politeness. OpenAlex rate-limits anonymous search
when it is busy, and OpenAlex is the fallback that covers what Crossref does not
index — USENIX, NeurIPS, ICLR, most conference proceedings. Without a contact
address those entries come back unchecked rather than verified, and the report
says so at the top rather than passing them off as clean.

It stays off by default and is never inferred, because it sends an address to
third parties. It is also read from `CITATION_CHECK_MAILTO`.

Live checks use a modest pool across independent references. Requests to one
host remain spaced by the politeness delay, while different authorities can be
checked in parallel; progress output and report rows remain in bibliography
order.

`check` exits 2 for failed verification, uncited references, or ambiguous year
suffixes, and 0 otherwise. Unresolved in-text citations alone do not fail the
run because that list has known false positives. `claims` always exits 0 — it
asks a question rather than answering one.

## Statuses

`MISMATCH` and `NOT_FOUND` are the ones that damage a submission. `LINK_DEAD` and
`STALE` need a fix but not a retraction. `PARTIAL`, `LINK_MOVED` and
`UNVERIFIABLE` are for a glance — notably, PDFs and JS-rendered pages land in
`PARTIAL` routinely, because no `<title>` can be read from them. A `PARTIAL` is a
limit of the check, not evidence against the citation.

## Limits

Page numbers, edition and publisher details are not checked.

MLA narrative citations that end in a bare page number (`Smith argues the
point (42)`) are not matched, because a bare parenthesised number cannot be
told from any other parenthesised digit. A shortened footnote note that cannot
be linked to its full citation is kept as an entry with a caveat rather than
silently merged.

The list of in-text citations with no matching entry has expected false
positives: a regex cannot distinguish `(Smith, 2024)` from `(ICLR 2023)`, or
`[12]` from a figure reference. The report says which shape to expect per
style.

Sources behind paywalls cannot be verified beyond their metadata record.

## Development

```bash
python3 -m unittest discover -s tests -t tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

The short version: standard library only, tests stay offline, and never let a coverage gap
report itself as a finding.

## Licence

MIT — see [LICENSE](LICENSE).
