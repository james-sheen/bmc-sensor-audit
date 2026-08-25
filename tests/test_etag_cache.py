"""Conditional requests, reported from outside as issue #2.

**The issue asked for the wrong thing, and this is the narrower thing that is
right.** It asked for `--if-none-match ETAG` on `capture`, singular. A Redfish
walk is not one resource: it is the service root, a chassis collection, each
chassis, each sensor collection and every sensor in it. There is no single ETag
to send.

The obvious repair -- send `If-None-Match` per resource -- does not survive
either. A `304` carries no body, so using one means having kept the previous
body, which means a cache of raw Redfish payloads on disk. Those carry serial
numbers, part numbers, asset tags and MAC addresses. *The parse is the
redaction* exists so this tool never writes one, and a cache that reintroduced
it would trade a disclosure for a bandwidth saving.

What is left is worth having. A COLLECTION's representation is its member list,
so its ETag moves when a sensor appears or disappears. Probing only the
collections answers *has the sensor set changed* in a handful of requests
instead of a full walk -- which is the question the fleet collector upstream of
this actually asks.

**And it answers nothing else.** A threshold edited on a sensor that stayed
present changes that sensor's resource, not its collection. Every test below
that asserts *unchanged* has a sibling asserting the tool SAID what it did not
check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bmc_sensor_audit import cli  # noqa: E402
from bmc_sensor_audit.inventory.redfish import (  # noqa: E402
    ETAG_CACHE_FORMAT, RedfishClient, etag_cache, membership_unchanged,
    walk_chassis)
from bmc_sensor_audit.testing.mock_redfish import MockBMC, serve  # noqa: E402

NAMES = ("Fan_CPU_1", "Fan_CPU_2", "Inlet_Temp")


def _machine(etags=True, names=NAMES):
    bmc = MockBMC(etags=etags)
    for name in names:
        bmc.add(name, upper_critical=95.0)
    return bmc


def _cache_from(url):
    client = RedfishClient(url)
    walk_chassis(client)
    return etag_cache(client)


class TestTheCacheRecordsCollectionsOnly:
    def test_it_declares_its_format(self):
        with serve(_machine()) as url:
            assert _cache_from(url)["format"] == ETAG_CACHE_FORMAT

    def test_it_holds_the_collections_and_not_the_sensors(self):
        """**The property that keeps a payload cache off disk.** If individual
        sensor resources appeared here, the next step would be caching their
        bodies to make 304s usable -- and those bodies are the disclosure."""
        with serve(_machine()) as url:
            cached = _cache_from(url)["collections"]
        assert cached, "nothing was cached at all"
        assert all(path.endswith(("/Chassis", "/Sensors")) for path in cached), cached
        assert not any("Sensors/" in path for path in cached), cached

    def test_a_bmc_without_etags_caches_nothing(self):
        """*Cannot tell* has to be visible as an empty cache, not as an empty
        answer that reads like agreement."""
        with serve(_machine(etags=False)) as url:
            assert _cache_from(url)["collections"] == {}


class TestTheProbeAnswersMembership:
    def test_an_unchanged_machine_answers_unchanged(self):
        with serve(_machine()) as url:
            cache = _cache_from(url)
            verdict, why = membership_unchanged(RedfishClient(url), cache)
        assert verdict is True, why

    def test_a_removed_sensor_answers_changed(self):
        """The whole point. A sensor that vanished must move a collection ETag,
        or a fleet collector would skip the walk that would have found it."""
        bmc = _machine()
        with serve(bmc) as url:
            cache = _cache_from(url)
        bmc.remove("Fan_CPU_2")
        with serve(bmc) as url:
            verdict, why = membership_unchanged(RedfishClient(url), cache)
        assert verdict is False, why

    def test_an_added_sensor_answers_changed(self):
        bmc = _machine()
        with serve(bmc) as url:
            cache = _cache_from(url)
        bmc.add("Outlet_Temp")
        with serve(bmc) as url:
            verdict, _ = membership_unchanged(RedfishClient(url), cache)
        assert verdict is False

    def test_a_bmc_with_no_etags_answers_CANNOT_TELL_not_unchanged(self):
        """**The failure this must never have.** A BMC that ignores the header
        returns 200 and no ETag. Reading that as *unchanged* would skip every
        walk on every machine that does not implement ETags, silently, forever."""
        bmc = _machine()
        with serve(bmc) as url:
            cache = _cache_from(url)
        quiet = _machine(etags=False)
        with serve(quiet) as url:
            verdict, why = membership_unchanged(RedfishClient(url), cache)
        assert verdict is None, why
        assert "cannot answer" in why

    def test_partial_etag_support_is_also_cannot_tell(self):
        """Answering *unchanged* because the collections that DO carry ETags
        agreed would be a guess about the ones that do not."""
        with serve(_machine()) as url:
            cache = _cache_from(url)
        cache["collections"]["/redfish/v1/Chassis/1/Thermal"] = '"invented"'
        with serve(_machine()) as url:
            verdict, why = membership_unchanged(RedfishClient(url), cache)
        assert verdict is None, why

    def test_an_empty_cache_is_cannot_tell(self):
        verdict, why = membership_unchanged(None, {"collections": {}})
        assert verdict is None and "nothing to compare" in why

    def test_an_unreachable_bmc_is_cannot_tell_not_unchanged(self):
        with serve(_machine()) as url:
            cache = _cache_from(url)
        # The server is down now; the URL resolves and nothing answers.
        verdict, why = membership_unchanged(RedfishClient(url, timeout=2), cache)
        assert verdict is None, why


class TestTheCaptureCommand:
    def _run(self, capsys, *argv):
        code = cli.main(list(argv))
        return code, capsys.readouterr().out

    def test_the_first_run_walks_and_writes_a_cache(self, tmp_path, capsys):
        out, cache = tmp_path / "walk.json", tmp_path / "etags.json"
        with serve(_machine()) as url:
            code, printed = self._run(capsys, "capture", "--target", url,
                                      "--out", str(out), "--etag-cache", str(cache))
        assert code == 0, printed
        assert out.is_file() and cache.is_file()
        assert json.loads(cache.read_text())["format"] == ETAG_CACHE_FORMAT

    def test_the_second_run_skips_the_walk_and_says_so(self, tmp_path, capsys):
        out, cache = tmp_path / "walk.json", tmp_path / "etags.json"
        bmc = _machine()
        with serve(bmc) as url:
            self._run(capsys, "capture", "--target", url, "--out", str(out),
                      "--etag-cache", str(cache))
            before = out.read_bytes()
            code, printed = self._run(capsys, "capture", "--target", url,
                                      "--out", str(out), "--etag-cache", str(cache))
        assert code == 0
        assert "sensor set unchanged" in printed
        assert out.read_bytes() == before, "the file was rewritten anyway"

    def test_the_skip_says_what_it_did_not_check(self, tmp_path, capsys):
        """**The sentence that keeps this honest.** A run that printed only
        *unchanged* would be read as *nothing changed*, and a threshold edited on
        a sensor that stayed present is exactly what it cannot see."""
        out, cache = tmp_path / "walk.json", tmp_path / "etags.json"
        with serve(_machine()) as url:
            self._run(capsys, "capture", "--target", url, "--out", str(out),
                      "--etag-cache", str(cache))
            _, printed = self._run(capsys, "capture", "--target", url,
                                   "--out", str(out), "--etag-cache", str(cache))
        assert "membership only" in printed
        assert "threshold" in printed

    def test_a_changed_set_walks_in_full_and_rewrites(self, tmp_path, capsys):
        out, cache = tmp_path / "walk.json", tmp_path / "etags.json"
        bmc = _machine()
        with serve(bmc) as url:
            self._run(capsys, "capture", "--target", url, "--out", str(out),
                      "--etag-cache", str(cache))
        bmc.remove("Fan_CPU_2")
        with serve(bmc) as url:
            code, printed = self._run(capsys, "capture", "--target", url,
                                      "--out", str(out), "--etag-cache", str(cache))
        assert code == 0
        assert "walking in full" in printed
        assert len(json.loads(out.read_text())["sensors"]) == 2

    def test_a_bmc_without_etags_walks_every_time_and_says_why(self, tmp_path,
                                                               capsys):
        out, cache = tmp_path / "walk.json", tmp_path / "etags.json"
        with serve(_machine(etags=False)) as url:
            _, first = self._run(capsys, "capture", "--target", url,
                                 "--out", str(out), "--etag-cache", str(cache))
            _, second = self._run(capsys, "capture", "--target", url,
                                  "--out", str(out), "--etag-cache", str(cache))
        assert "returned no ETags" in first
        assert "sensor set unchanged" not in second

    def test_an_unreadable_cache_walks_rather_than_refusing(self, tmp_path,
                                                            capsys):
        """An unusable cache is a reason to do the full work, never a reason to
        skip it -- and never a reason to fail a fleet run."""
        out, cache = tmp_path / "walk.json", tmp_path / "etags.json"
        cache.write_text("{not json")
        with serve(_machine()) as url:
            code, printed = self._run(capsys, "capture", "--target", url,
                                      "--out", str(out), "--etag-cache", str(cache))
        assert code == 0
        assert "unusable" in printed and out.is_file()

    def test_without_the_flag_nothing_changes(self, tmp_path, capsys):
        """Non-vacuity for the default path: the feature is opt-in, and a
        capture with no `--etag-cache` must behave exactly as it did."""
        out = tmp_path / "walk.json"
        with serve(_machine()) as url:
            code, printed = self._run(capsys, "capture", "--target", url,
                                      "--out", str(out))
        assert code == 0
        assert "etag" not in printed.lower()
        assert len(json.loads(out.read_text())["sensors"]) == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
