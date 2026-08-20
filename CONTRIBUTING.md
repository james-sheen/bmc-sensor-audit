# Contributing

The most useful thing you can send is a machine or a configuration where the
gate said clean over something it did not audit. That is the failure this tool
exists to prevent, and it is the report this project wants most. The
second-most useful is a real entity-manager declaration the reader mis-parses
silently — the upstream corpus has already taught this parser five lessons the
format documentation does not mention, and there is no reason to believe it is
done teaching.

## What is open right now

**Issues: open.** Bug reports, adversarial findings, and questions about
running the tool against your hardware are all welcome. A report that can be
acted on here carries four things: the configuration (or the name of the
vendored one that shows it), the walk or capture it ran against, the full
report text with the exit code, and — if the `[detect]` extra is involved —
the installed `arbiter-engine` version, because presence and correctness are
different checks and a stale engine fails in code-shaped ways.

**Real-hardware captures: actively wanted.** Criterion 2's last line closes
with a capture from physical hardware, and `capture` writes only the parsed
sensor set by design — serials, asset tags and MAC addresses never reach the
file. Run one command, attach the output, and say what machine it came from.

**Documentation pull requests: open.** If the README describes behaviour the
package does not have, a PR is the fastest route and it will be merged. Note
that the README's commands and its test count are themselves under test, so a
documentation change may fail the suite until the matching assertion moves —
that is the suite doing its job.

**Code pull requests: not being merged yet, and the reason is capacity.**
This mirrors the sibling engine's policy for the same one-maintainer reason,
and what ends it is the same: a second reviewer, not a version number.

## A report does not have to be complete to be worth sending

The best finding this repository has received so far was a lead rather than a
proof, and the half of it that was left open turned out to be the half that
mattered: a note that the parser never consulted an entry's `Labels` array, and
that a sensor declared through `Labels` alone would therefore be invisible. Both
halves were correct. The first was fixed and the second was recorded as an open
question — and measuring it later showed it had been hiding most of the rails in
the vendored corpus, every one of them a sensor whose absence could never have
been reported.

So: if you can see that something is wrong but not how far it goes, send it
anyway and say where you stopped looking. *I did not audit X* is the most useful
sentence in a report, and it will be read as a lead rather than as a disclaimer.

## Running the suite

Enable the hook first — the suite fails without it, deliberately:

    git config core.hooksPath .githooks

The suite has two honest populations: without the `[detect]` extra (Stage 1,
dependency-free) and with it. Both are green at head; the exact counts live in
the README's Tests row, which is enforced by a test rather than promised by a
sentence.
