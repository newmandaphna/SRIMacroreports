"""The log scrubber is a backstop against PHI reaching a log file.

These tests use obviously fake patient names, in the surname comma format the Q sheet
uses, because that is the shape the scrubber has to catch.
"""

from __future__ import annotations

import logging
import logging.config

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


# --------------------------------------------------- the holes the sweep found


def test_sqlalchemy_batched_bind_names_are_scrubbed():
    """A real quarterly import batches its inserts, so SQLAlchemy names its bound
    parameters patient_name__0, patient_code__0 and so on. The key pattern used to
    require a word boundary immediately after the key name, which those suffixes
    break, so a database error during a batched insert put every patient code in
    the batch into the log in the clear."""
    text = "{'patient_name__0': 'Testcase Patientag', 'patient_code__0': 'PATAG'}"
    cleaned = scrub(text)
    assert "PATAG" not in cleaned
    assert "Patientag" not in cleaned


def test_the_normalized_column_name_is_scrubbed():
    """patient_name_normalized is a real column, and it holds a patient name."""
    cleaned = scrub("patient_name_normalized=SOMEBODY REAL")
    assert "SOMEBODY REAL" not in cleaned


def test_a_patient_code_has_no_fallback_pattern_so_the_key_must_match():
    """Names are also caught by the surname pattern, which depends on the sheet's
    Surname,Given format. A patient code has no such shape, so if the key pattern
    misses it nothing else will."""
    for key in ("patient_code", "patient_code__0", "patient_code_normalized"):
        assert "PATAH" not in scrub(f"{{'{key}': 'PATAH'}}"), key


def test_uvicorn_loggers_cannot_bypass_the_scrubber():
    """uvicorn ships its own handlers with propagation off, so a traceback logged
    through them printed unscrubbed next to the scrubbed copy. Reaching the
    deployment log is exactly the path that matters on Replit."""
    import uvicorn.config

    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    configure_logging("INFO")

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        assert logger.propagate, f"{name} does not propagate to the scrubbed root handler"
        assert logger.handlers == [], f"{name} keeps a handler that bypasses the root"
        # Asserted on the logger, not on its (now absent) handlers: a loop over an
        # empty list would pass without checking anything, which is the shape of
        # vacuous test this sweep was run to find.
        assert any(isinstance(f, PHIScrubbingFilter) for f in logger.filters), (
            f"{name} has no scrubbing filter of its own"
        )


def test_uvicorn_output_is_scrubbed_end_to_end(capsys):
    import uvicorn.config

    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    configure_logging("INFO")
    logging.getLogger("uvicorn.error").error(
        "insert failed: {'patient_code__0': 'PATAI', 'patient_name__0': 'Testcase Patientai'}"
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "PATAI" not in combined
    assert "Patientai" not in combined
