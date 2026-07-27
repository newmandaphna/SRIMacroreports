"""The log scrubber is a backstop against PHI reaching a log file.

These tests use obviously fake patient names, in the surname comma format the Q sheet
uses, because that is the shape the scrubber has to catch.
"""

from __future__ import annotations

import logging

from app.logging_setup import REDACTED, PHIScrubbingFilter, configure_logging, scrub


def test_scrubs_surname_comma_patient_name():
    assert "Patientaa,Testcase" not in scrub("row for Patientaa,Testcase failed")
    assert REDACTED in scrub("row for Patientaa,Testcase failed")


def test_scrubs_composite_match_key():
    line = "unmatched key TESTPROVIDER|Patientab,Testcase|4/1/2026|90837 in source 3"
    cleaned = scrub(line)
    assert "Patientab" not in cleaned
    assert REDACTED in cleaned


def test_scrubs_keyed_values():
    cleaned = scrub("patient_name='Patientac,Testcase' patient_code=PATAC dob=1980-01-01")
    assert "Patientac" not in cleaned
    assert "PATAC" not in cleaned
    assert "1980-01-01" not in cleaned


def test_scrubs_secrets():
    cleaned = scrub('{"private_key": "abc123", "token": "xyz789"}')
    assert "abc123" not in cleaned
    assert "xyz789" not in cleaned


def test_leaves_ordinary_text_alone():
    line = "sync run 12 read 9389 rows, upserted 8477, rejected 11"
    assert scrub(line) == line


def test_filter_scrubs_lazy_format_args(caplog):
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed on %s",
        args=("Patientad,Testcase",),
        exc_info=None,
    )
    PHIScrubbingFilter().filter(record)
    assert "Patientad" not in record.getMessage()


def test_configure_logging_installs_filter_and_scrubs_output(capsys):
    configure_logging("INFO")
    logging.getLogger("test.scrub").info("importing Patientae,Testcase")
    captured = capsys.readouterr().out
    assert "Patientae" not in captured
    assert REDACTED in captured


def test_traceback_locals_are_scrubbed(capsys):
    """The formatter is the last gate, after filters have run."""
    configure_logging("INFO")
    try:
        raise ValueError("bad row for Patientaf,Testcase")
    except ValueError:
        logging.getLogger("test.scrub").exception("import failed")
    captured = capsys.readouterr().out
    assert "Patientaf" not in captured
