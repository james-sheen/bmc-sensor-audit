"""The credential and TLS surfaces, reported from outside as issues #3 and #4.

Both came from building a rack collector on top of this tool, and neither is
visible from inside it: the tool works, and the gaps only take a shape once
something has to drive `capture` continuously across a fleet.

**#4 — the password crossed argv.** `--password VALUE` is readable by any user
on the host for the lifetime of the walk, and a collector walks continuously.
`--password-env` and `--password-file` keep the value out of the process table.
The old flag stays, because removing it would break every caller at once; its
help text now says what it costs.

**#3 — there was no way to verify a BMC.** The only TLS control was
`--insecure`, so the choice was between no verification and no walk. `--cafile`
verifies against a certificate the operator supplies; `--pin-sha256` requires
one exact certificate and REPLACES chain verification, which is the only thing
that works for a self-signed certificate that chains to nothing.

**What is not tested here, stated rather than implied**: no real TLS handshake.
Proving the pin against a live socket needs a certificate and its private key as
a fixture, and this repository refuses to carry key material -- the hygiene rules
would refuse the commit, correctly. What IS tested is the refusal itself, driven
through the connection class with a stubbed socket, and the parsing that decides
which certificate would be accepted.
"""

from __future__ import annotations

import hashlib
import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bmc_sensor_audit import cli  # noqa: E402
from bmc_sensor_audit.inventory.redfish import (  # noqa: E402
    CertificatePinError, RedfishClient, _pinned_opener)

#: A syntactically valid pin, in the spelling `openssl` prints.
PIN = ":".join("ab" for _ in range(32))


class TestThePasswordNeedNotCrossArgv:
    """Issue #4."""

    def _args(self, **kwargs):
        base = {"password": None, "password_env": None, "password_file": None}
        base.update(kwargs)
        return type("Args", (), base)()

    def test_an_environment_variable_is_read_at_the_moment_of_use(self, monkeypatch):
        monkeypatch.setenv("BMC_PASS", "first")
        args = self._args(password_env="BMC_PASS")
        assert cli._resolve_password(args) == "first"
        monkeypatch.setenv("BMC_PASS", "second")
        assert cli._resolve_password(args) == "second"

    def test_a_missing_variable_is_a_run_that_could_not_happen(self, monkeypatch):
        """**Not silently *no password*.** That would reach the BMC as an
        anonymous request and come back 401, sending the operator to look at
        their credentials on the BMC rather than at the flag they mistyped."""
        monkeypatch.delenv("BMC_PASS", raising=False)
        with pytest.raises(cli.CredentialError, match="not set"):
            cli._resolve_password(self._args(password_env="BMC_PASS"))

    def test_a_file_is_read_without_its_trailing_newline(self, tmp_path):
        """Every editor adds one and no BMC expects it."""
        path = tmp_path / "secret"
        path.write_text("hunter2\n")
        assert cli._resolve_password(self._args(password_file=str(path))) == "hunter2"

    def test_only_the_first_line_of_a_file_is_used(self, tmp_path):
        path = tmp_path / "secret"
        path.write_text("hunter2\n# a note somebody added\n")
        assert cli._resolve_password(self._args(password_file=str(path))) == "hunter2"

    def test_an_unreadable_file_is_named(self, tmp_path):
        with pytest.raises(cli.CredentialError, match="password-file"):
            cli._resolve_password(self._args(password_file=str(tmp_path / "absent")))

    def test_the_old_flag_still_works(self):
        """Non-vacuity, and a compatibility promise. Removing `--password` would
        break every existing caller at once to fix a hazard they may not have."""
        assert cli._resolve_password(self._args(password="hunter2")) == "hunter2"

    def test_nothing_named_means_no_password(self):
        assert cli._resolve_password(self._args()) is None

    @pytest.mark.parametrize("command", ["capture", "coverage", "detect"])
    def test_every_subcommand_that_reaches_a_bmc_offers_them(self, command):
        """**The reason the flags are declared once.** These were three copies,
        which is how a new flag lands on one subcommand and not the others -- and
        the operator who finds it on `capture` has no reason to re-check
        `coverage`."""
        parser = cli.build_parser()
        sub = parser._subparsers._group_actions[0].choices[command]
        flags = {s for action in sub._actions for s in action.option_strings}
        assert {"--password-env", "--password-file", "--cafile",
                "--pin-sha256"} <= flags

    @pytest.mark.parametrize("pair", [
        ["--password", "x", "--password-env", "Y"],
        ["--password-env", "Y", "--password-file", "/tmp/z"],
        ["--password", "x", "--password-file", "/tmp/z"],
    ])
    def test_two_credential_sources_are_refused(self, pair):
        """Two answers to one question. Picking one silently is a guess about
        which the operator meant, made at the moment it matters least."""
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(
                ["capture", "--target", "https://x", "--out", "/tmp/o", *pair])


class TestTheBmcCanBeVerified:
    """Issue #3."""

    def test_a_plain_client_is_unchanged(self):
        """Non-vacuity for everything below: the default path must not have
        acquired an opener or a context by accident."""
        client = RedfishClient("https://x")
        assert client._opener is None and client._ctx is None

    def test_insecure_still_turns_verification_off(self):
        assert RedfishClient("https://x", verify_tls=False)._ctx.verify_mode is \
            ssl.CERT_NONE

    def test_a_cafile_builds_a_verifying_context(self, tmp_path):
        """Hostname checking stays ON. A BMC reached by IP whose certificate
        names a hostname fails here, and that failure is correct -- `--pin-sha256`
        is the flag for that case, not a quieter `--cafile`."""
        # Copied from whichever system bundle this host has, because a
        # certificate has to be REAL to load and this repository will not carry
        # key material to make one. `get_default_verify_paths().cafile` is None
        # on Debian, where the bundle is at a path OpenSSL reports separately --
        # so both are tried before giving up.
        candidates = [ssl.get_default_verify_paths().cafile,
                      ssl.get_default_verify_paths().openssl_cafile,
                      "/etc/ssl/certs/ca-certificates.crt",
                      "/etc/pki/tls/certs/ca-bundle.crt"]
        source = next((Path(c) for c in candidates
                       if c and Path(c).is_file() and Path(c).stat().st_size), None)
        if source is None:
            pytest.skip("no system CA bundle on this host, so there is no real "
                        "certificate to load; the context builder is unchecked here")
        bundle = tmp_path / "ca.pem"
        bundle.write_bytes(source.read_bytes())
        client = RedfishClient("https://x", cafile=str(bundle))
        assert client._ctx is not None
        assert client._ctx.verify_mode is ssl.CERT_REQUIRED
        assert client._ctx.check_hostname is True

    def test_a_pin_builds_an_opener_rather_than_a_context(self):
        client = RedfishClient("https://x", pin_sha256=PIN)
        assert client._opener is not None and client._ctx is None

    @pytest.mark.parametrize("bad", ["zz", "ab:cd", "0" * 63, "0" * 65,
                                     "g" * 64, ""])
    def test_a_pin_that_is_not_a_sha256_is_refused_before_any_connection(self, bad):
        """Refused at construction, not at the first request. A pin typed wrong
        must not fail as a network error hours into a fleet run."""
        with pytest.raises(CertificatePinError, match="64 hex"):
            RedfishClient("https://x", pin_sha256=bad)

    @pytest.mark.parametrize("spelling", [PIN, PIN.upper(), PIN.replace(":", ""),
                                          f"  {PIN}  "])
    def test_the_spellings_openssl_prints_are_all_accepted(self, spelling):
        """`openssl x509 -fingerprint -sha256` prints colons and uppercase. An
        operator copies that string; re-typing it by hand is how a pin ends up
        subtly wrong."""
        assert _pinned_opener(spelling) is not None

    def _connection(self, pin: str, presented: bytes | None):
        """A pinned connection whose handshake is stubbed out.

        The socket is a stand-in because a real one needs a certificate and its
        private key, and this repository will not carry key material. What is
        exercised is the comparison and the refusal, which is the part that can
        be wrong.
        """
        opener = _pinned_opener(pin)
        handler = next(h for h in opener.handlers
                       if type(h).__name__ == "_PinnedHandler")
        connection_class = None

        class _Probe:
            def getpeercert(self, binary_form=False):
                return presented

        import http.client

        for cell in handler.https_open.__func__.__closure__ or ():
            if isinstance(cell.cell_contents, type) and issubclass(
                    cell.cell_contents, http.client.HTTPSConnection):
                connection_class = cell.cell_contents
        assert connection_class is not None, "the pinned connection class moved"

        conn = connection_class.__new__(connection_class)
        conn.sock = _Probe()
        conn.close = lambda: None
        original = http.client.HTTPSConnection.connect
        http.client.HTTPSConnection.connect = lambda self: None
        try:
            conn.connect()
        finally:
            http.client.HTTPSConnection.connect = original

    def test_a_matching_certificate_is_accepted(self):
        der = b"the certificate bytes"
        self._connection(hashlib.sha256(der).hexdigest(), der)

    def test_a_different_certificate_is_refused(self):
        """The assertion the whole flag exists for."""
        with pytest.raises(CertificatePinError, match="the BMC presented"):
            self._connection(hashlib.sha256(b"expected").hexdigest(), b"actual")

    def test_no_certificate_at_all_is_refused(self):
        """A handshake that produced no peer certificate must not compare equal
        to anything. An empty digest against an empty expectation is the shape
        that quietly passes."""
        with pytest.raises(CertificatePinError):
            self._connection(hashlib.sha256(b"expected").hexdigest(), None)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

class TestATlsFlagOnANonHttpsTargetIsRefused:
    """The defect 0.1.3 shipped, found by a consumer's end-to-end test.

    **urllib picks a handler by SCHEME.** A pinned HTTPS handler is never
    consulted for an `http://` URL, so the pin was built, ignored, and the walk
    succeeded unverified. An operator who typed a fingerprint would have
    believed the connection was checked — which is worse than not offering
    pinning at all.

    It was invisible to every test here, because they all built a client and
    inspected it rather than walking anything. The downstream test that found it
    pinned a WRONG certificate and expected the walk to fail. It passed.
    """

    @pytest.mark.parametrize("kwargs", [
        {"pin_sha256": PIN},
        {"cafile": "/etc/ssl/certs/ca-certificates.crt"},
    ])
    def test_http_with_a_tls_flag_is_refused(self, kwargs):
        with pytest.raises(CertificatePinError, match="not https"):
            RedfishClient("http://bmc.example", **kwargs)

    @pytest.mark.parametrize("kwargs", [
        {"pin_sha256": PIN},
        {"cafile": None},
    ])
    def test_https_still_works(self, kwargs):
        """Non-vacuity: the refusal must be about the scheme, not about the
        flags existing at all."""
        assert RedfishClient("https://bmc.example", **kwargs) is not None

    def test_plain_http_without_tls_flags_is_untouched(self):
        """The mock server speaks HTTP, and so do plenty of lab BMCs. Refusing
        those would break every test and every bring-up bench."""
        assert RedfishClient("http://bmc.example") is not None

    def test_the_refusal_names_which_flag(self):
        with pytest.raises(CertificatePinError, match="--pin-sha256"):
            RedfishClient("http://bmc.example", pin_sha256=PIN)
        with pytest.raises(CertificatePinError, match="--cafile"):
            RedfishClient("http://bmc.example", cafile="/dev/null")

    def test_the_scheme_check_is_case_insensitive(self):
        """`HTTPS://` is a legal URL and would otherwise be refused."""
        assert RedfishClient("HTTPS://bmc.example", pin_sha256=PIN) is not None

class TestARefusalIsExitTwoNotATraceback:
    """`1` means FINDINGS here. A refusal that escapes as a traceback exits `1`.

    **Found by the consumer, immediately after the fix above shipped.** The
    scheme refusal was correct and uncaught: `capture` crashed, Python exited
    `1`, and a fleet collector read a misconfigured flag as *this machine has
    findings*. The tool answered a question nobody asked.

    So `main` catches a named tuple rather than one class — adding a refusal is
    one edit, and this test can enumerate what is covered.
    """

    def _run(self, argv):
        import contextlib, io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, err.getvalue()

    def test_a_pin_on_http_exits_two(self, tmp_path):
        code, err = self._run(["capture", "--target", "http://bmc.example",
                               "--out", str(tmp_path / "w.json"),
                               "--pin-sha256", PIN])
        assert code == 2, "a refusal came back as findings or as a crash"
        assert "not https" in err

    def test_a_missing_password_variable_exits_two(self, tmp_path, monkeypatch):
        """The refusal that was already covered, kept so the tuple is proved to
        hold more than one member."""
        monkeypatch.delenv("BMC_PASS_ABSENT", raising=False)
        code, err = self._run(["capture", "--target", "https://bmc.example",
                               "--out", str(tmp_path / "w.json"),
                               "--password-env", "BMC_PASS_ABSENT"])
        assert code == 2
        assert "not set" in err

    def test_every_refusal_is_an_exception_class(self):
        assert cli.REFUSALS
        for refusal in cli.REFUSALS:
            assert isinstance(refusal, type) and issubclass(refusal, Exception)

    def test_the_error_classes_this_package_defines_are_accounted_for(self):
        """**The class, not the instance.** A new `...Error` that nothing
        catches is the next uncaught traceback, and this names it at the moment
        it is written rather than when a consumer trips over it.

        Locally-caught ones are listed by name: each is caught inside the
        subcommand that raises it, which is the other correct answer.
        """
        import pathlib
        caught_locally = {"SupplementalError", "UnknownResourceType",
                          "DeclarationSourceError"}
        in_main = {r.__name__ for r in cli.REFUSALS}
        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "bmc_sensor_audit"
        defined = set()
        for path in src.rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("class ") and "Error" in line.split("(")[0]:
                    defined.add(line.split()[1].split("(")[0])
                elif line.startswith("class UnknownResourceType"):
                    defined.add("UnknownResourceType")
        unaccounted = defined - in_main - caught_locally
        assert not unaccounted, (
            f"{sorted(unaccounted)} are raised by this package and neither "
            f"caught in main nor listed as caught locally. An uncaught refusal "
            f"exits 1, which means FINDINGS")



class TestTheToolCanBeAskedWhatItIs:
    """`--version`, and the reason it is not cosmetic.

    Every consumer in this family runs this tool as a **subprocess resolved on
    PATH**, never as an import. `fleet-sensor-baseline` declares
    `bmc-sensor-audit>=0.1.5,<0.2`; pip enforces that over the environment it
    installed into, and then the collector runs whatever `PATH` hands it. A
    system-wide install, a pipx shim, or simply a different venv earlier on PATH
    silently answers instead, and the floor is never consulted.

    Until this flag existed there was no way to close that: `--version` exited 2
    with an argparse usage error, so a downstream floor could be declared and
    could not be checked. The flag is the surface that makes a runtime check
    possible at all.
    """

    def _run(self, *args):
        import os as _os
        import subprocess as sp
        import sys as _sys
        from pathlib import Path as _Path
        root = _Path(__file__).resolve().parents[1]
        env = {**_os.environ, "PYTHONPATH": str(root / "src")}
        return sp.run([_sys.executable, "-m", "bmc_sensor_audit.cli", *args],
                      capture_output=True, text=True, env=env)

    def test_it_exits_clean_and_names_the_distribution(self):
        done = self._run("--version")
        assert done.returncode == 0, done.stderr
        assert done.stdout.startswith("bmc-sensor-audit "), done.stdout

    def test_it_reports_the_version_the_package_declares(self):
        """Not a literal repeated here -- that would be the same number written
        twice, and it would drift the first time one of them moved."""
        from bmc_sensor_audit import __version__
        assert self._run("--version").stdout.strip() == \
            f"bmc-sensor-audit {__version__}"

    def test_a_consumer_can_parse_a_floor_out_of_it(self):
        """The shape a subprocess consumer actually needs: one line, on stdout,
        with a dotted version it can compare. stderr and a usage dump are not
        parseable, and that is what this used to emit."""
        done = self._run("--version")
        assert done.stderr == ""
        name, _, version = done.stdout.strip().partition(" ")
        assert name == "bmc-sensor-audit"
        assert tuple(int(p) for p in version.split(".")[:3]) >= (0, 1, 5)
