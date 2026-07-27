"""A synthetic workbook that mirrors the real Q2 Snapshot layout exactly.

Every patient here is obviously fake: Patient AA through Patient AN, codes PATAA
through PATAN. Therapists use the same surname capitals style as the real sheet but
are invented names. Nothing in this file could be mistaken for a real person or a real
payment.

The point of mirroring the layout precisely is that the demo exercises the same
mapping the live sheet will need, including its awkward parts:

  the header row has two unnamed columns (M and N) that must not be imported
  Patient Code sits in column S, after Total balance, not where the spec lists it
  numeric codes arrive as Excel floats, so CPT is 90837.0 and location 1 is 1.0
  the same procedure appears suffixed (90791-ADHD) and plain (90791)
  the cancellation codes are present: 99998 with no fee, 99999 with a 75.00 fee
  a few rows are deliberately broken, to exercise the rejection path
"""

from __future__ import annotations

# Exactly the real Q2 Snapshot header row, including the two blank headers.
Q2_HEADERS: list[object] = [
    "Therapist",
    "Patient name",
    "DOS",
    "CPT",
    "Ins",
    "Loc",
    "NOTE",
    "Due from pt",
    "Paid by pt",
    "Pt. Amount Due",
    "Due from ins",
    "Paid by ins",
    None,  # column M, a composite match key used by the workbook's own macros
    None,  # column N, empty throughout
    "Ins balance",
    "Total due",
    "Total paid",
    "Total balance",
    "Patient Code",
    "Recorded",
]


def _row(
    therapist: str,
    patient: str,
    code: str,
    dos: str,
    cpt: object,
    ins: str,
    loc: object,
    note: str,
    due_pt: object,
    paid_pt: object,
    due_ins: object,
    paid_ins: object,
    recorded: str = "",
) -> list[object]:
    """Build one row with the derived columns computed the way the sheet does."""

    def num(v: object) -> float:
        return float(v) if isinstance(v, int | float) else 0.0

    pt_amount_due = round(num(due_pt) - num(paid_pt), 2)
    ins_balance = round(num(due_ins) - num(paid_ins), 2)
    total_due = round(num(due_pt) + num(due_ins), 2)
    total_paid = round(num(paid_pt) + num(paid_ins), 2)
    total_balance = round(total_due - total_paid, 2)

    return [
        therapist,
        patient,
        dos,
        cpt,
        ins,
        loc,
        note,
        due_pt,
        paid_pt,
        pt_amount_due,
        due_ins,
        paid_ins,
        f"{therapist}|{patient}|{dos}|{cpt}",  # column M
        None,  # column N
        ins_balance,
        total_due,
        total_paid,
        total_balance,
        code,
        recorded,
    ]


# Clean rows, covering the shapes the real sheet contains.
_CLEAN: list[list[object]] = [
    # Ordinary telehealth and in person therapy hours.
    _row(
        "QUINCEY",
        "Patient AA",
        "PATAA",
        "2026-04-01",
        90837.0,
        "BLSH",
        "TH",
        "X",
        15.0,
        15.0,
        156.05,
        156.05,
    ),
    _row(
        "QUINCEY",
        "Patient AB",
        "PATAB",
        "2026-04-01",
        90837.0,
        "KS",
        1.0,
        "X",
        30.0,
        30.0,
        161.76,
        161.76,
    ),
    _row(
        "QUINCEY",
        "Patient AC",
        "PATAC",
        "2026-04-02",
        90834.0,
        "PC",
        "TH",
        "X",
        38.35,
        38.35,
        120.00,
        120.00,
    ),
    _row(
        "QUINCEY",
        "Patient AA",
        "PATAA",
        "2026-04-08",
        90837.0,
        "BLSH",
        "TH",
        "X",
        15.0,
        15.0,
        156.05,
        156.05,
    ),
    _row(
        "QUINCEY",
        "Patient AD",
        "PATAD",
        "2026-04-09",
        90791.0,
        "MC",
        1.0,
        "X",
        0.0,
        0.0,
        210.00,
        210.00,
    ),
    # Suffixed spellings of the same procedures. These must group with 90791 and 90837.
    _row(
        "QUINCEY",
        "Patient AE",
        "PATAE",
        "2026-04-10",
        "90791-ADHD",
        "AE",
        "TH",
        "X",
        25.0,
        25.0,
        210.00,
        0.0,
    ),
    _row(
        "THORNBURY",
        "Patient AF",
        "PATAF",
        "2026-04-13",
        "90837 - ADHD",
        "KS",
        "TH",
        "X",
        20.0,
        20.0,
        156.05,
        156.05,
    ),
    # An outstanding balance on both sides, so revenue outstanding is not always zero.
    _row(
        "THORNBURY",
        "Patient AG",
        "PATAG",
        "2026-04-14",
        90837.0,
        "IA",
        1.0,
        "X",
        50.0,
        0.0,
        250.00,
        0.0,
    ),
    _row(
        "THORNBURY",
        "Patient AH",
        "PATAH",
        "2026-04-15",
        90847.0,
        "PC",
        1.0,
        "X",
        40.0,
        40.0,
        180.00,
        132.81,
    ),
    # Draft documentation rather than signed.
    _row(
        "THORNBURY",
        "Patient AI",
        "PATAI",
        "2026-04-16",
        90834.0,
        "QU",
        "TH",
        "D",
        25.0,
        25.0,
        118.63,
        118.63,
    ),
    # Blank insurance. Unknown, not self pay: SP is a real separate code.
    _row(
        "THORNBURY",
        "Patient AJ",
        "PATAJ",
        "2026-04-17",
        90837.0,
        "",
        "TH",
        "X",
        30.0,
        30.0,
        150.00,
        150.00,
    ),
    _row(
        "WREN",
        "Patient AK",
        "PATAK",
        "2026-04-20",
        99213.0,
        "MC",
        1.0,
        "X",
        20.0,
        20.0,
        95.00,
        95.00,
    ),
    _row(
        "WREN",
        "Patient AL",
        "PATAL",
        "2026-04-21",
        90837.0,
        "SP",
        "TH",
        "X",
        175.0,
        175.0,
        0.0,
        0.0,
    ),
    _row(
        "WREN",
        "Patient AM",
        "PATAM",
        "2026-04-22",
        90792.0,
        "BLSH",
        1.0,
        "X",
        35.0,
        35.0,
        240.00,
        240.00,
    ),
    _row(
        "WREN",
        "Patient AN",
        "PATAN",
        "2026-04-23",
        90837.0,
        "KS",
        "TH",
        "X",
        15.0,
        15.0,
        156.05,
        156.05,
    ),
    # Cancellation, no fee charged. Not a session, and carries no money.
    _row(
        "QUINCEY", "Patient AB", "PATAB", "2026-04-24", 99998.0, "MC", 1.0, "X", 0.0, 0.0, 0.0, 0.0
    ),
    _row(
        "THORNBURY",
        "Patient AG",
        "PATAG",
        "2026-04-27",
        99998.0,
        "IA",
        "TH",
        "X",
        0.0,
        0.0,
        0.0,
        0.0,
    ),
    # Cancellation with the standard 75.00 no show fee. Not a session, but real money,
    # and insurance never pays it.
    _row(
        "WREN", "Patient AK", "PATAK", "2026-04-28", 99999.0, "SP", 1.0, "X", 75.0, 75.0, 0.0, 0.0
    ),
    _row(
        "QUINCEY",
        "Patient AC",
        "PATAC",
        "2026-04-29",
        99999.0,
        "SP",
        "TH",
        "X",
        75.0,
        0.0,
        0.0,
        0.0,
    ),
    # Non session codes that the practice confirmed do not count.
    _row(
        "WREN", "Patient AL", "PATAL", "2026-04-30", "QBCHK", "SP", 1.0, "X", 45.0, 45.0, 0.0, 0.0
    ),
    _row(
        "THORNBURY",
        "Patient AH",
        "PATAH",
        "2026-05-01",
        "pro bono",
        "SP",
        "TH",
        "X",
        0.0,
        0.0,
        0.0,
        0.0,
    ),
    # A therapist written the Valant way, to exercise alias resolution.
    _row(
        "Rosalind Wren, LMFT (R.Wren)",
        "Patient AM",
        "PATAM",
        "2026-05-04",
        90837.0,
        "MC",
        "TH",
        "X",
        25.0,
        25.0,
        160.00,
        160.00,
    ),
    # Patient Code blank, which is 41 percent of the real sheet. Must still import.
    _row(
        "QUINCEY",
        "Patient AA",
        "",
        "2026-05-05",
        90837.0,
        "BLSH",
        "TH",
        "X",
        15.0,
        15.0,
        156.05,
        156.05,
    ),
    _row(
        "THORNBURY",
        "Patient AI",
        "",
        "2026-05-06",
        90834.0,
        "QU",
        1.0,
        "X",
        25.0,
        25.0,
        118.63,
        118.63,
    ),
    # An unrecorded visit, the only Recorded value the real sheet ever carries.
    _row(
        "WREN",
        "Patient AN",
        "PATAN",
        "2026-05-07",
        90837.0,
        "KS",
        "TH",
        "",
        15.0,
        0.0,
        156.05,
        0.0,
        "UNRECORDED",
    ),
]

# Rows that must be rejected rather than imported, one per failure mode.
_BROKEN: list[list[object]] = [
    # No date of service: cannot be placed in any period, so it cannot be counted.
    _row(
        "QUINCEY", "Patient AD", "PATAD", "", 90837.0, "KS", "TH", "X", 15.0, 15.0, 156.05, 156.05
    ),
    # An unreadable date.
    _row(
        "QUINCEY",
        "Patient AE",
        "PATAE",
        "not a date",
        90837.0,
        "KS",
        "TH",
        "X",
        15.0,
        15.0,
        156.05,
        156.05,
    ),
    # An amount that will not parse. Must not silently become zero.
    _row(
        "THORNBURY",
        "Patient AF",
        "PATAF",
        "2026-05-11",
        90837.0,
        "PC",
        "TH",
        "X",
        "see note",
        0.0,
        0.0,
        0.0,
    ),
    # A therapist nobody has an alias for. Never auto created.
    _row(
        "UNKNOWNPERSON",
        "Patient AG",
        "PATAG",
        "2026-05-12",
        90837.0,
        "KS",
        "TH",
        "X",
        15.0,
        15.0,
        156.05,
        156.05,
    ),
    # No patient name, so the row has no identity under the upsert key.
    _row("WREN", "", "", "2026-05-13", 90837.0, "KS", "TH", "X", 15.0, 15.0, 156.05, 156.05),
]

# Formula residue below the data block, exactly as the real sheet has it: about five
# thousand rows of dragged down formulas evaluating to 0, with no identity. The
# importer must stop at the end of the data rather than import these.
_RESIDUE: list[list[object]] = [
    ["", "", "", "", "", "", "", "", "", 0.0, "", "", "", "", 0.0, 0.0, 0.0, 0.0, "", ""]
    for _ in range(25)
]

Q2_SNAPSHOT_DEMO: list[list[object]] = [Q2_HEADERS, *_CLEAN, *_BROKEN, *_RESIDUE]


# The Abbreviations tab, long name to short code. Many to one, as in the real sheet.
ABBREVIATIONS_DEMO: list[list[object]] = [
    [
        "Insurance Company Name",
        "Ins - Short",
        "Locations",
        "Loc - Short",
        "Note Codes",
        "Notes - Short",
    ],
    ["Blue Shield (demo payer)", "BLSH", "Jenkintown", 1.0, "Draft", "D"],
    ["Blue Shield HMO (demo payer)", "BLSH", "Revere Commons", 2.0, "Finalized", "X"],
    ["Keystone Select (demo payer)", "KS", "Telehealth", "TH", None, None],
    ["Keystone HMO (demo payer)", "KS", "Telehealth (Medicare Option)", "TH", None, None],
    ["Personal Choice (demo payer)", "PC", "Telehealth (Optum)", "TH", None, None],
    ["Medicare (demo payer)", "MC", None, None, None, None],
    ["Aetna Elect (demo payer)", "AE", None, None, None, None],
    ["Independence Administrators (demo payer)", "IA", None, None, None, None],
    ["Quest Behavioral (demo payer)", "QU", None, None, None, None],
    ["Self Pay", "SP", None, None, None, None],
]


# The provider override table, mirroring the real workbook's Config tab.
CONFIG_DEMO: list[list[object]] = [
    ["Type", "Raw Text Contains", "Output", "Notes"],
    ["PROVIDER", "Rosalind Wren", "WREN", "Provider override"],
    ["PROVIDER", "Tobias Quincey", "QUINCEY", "Provider override"],
    ["LOCATION", "Telehealth", "TH", "Location override"],
    ["LOCATION", "In Person", 1.0, "Location override"],
]


# A RAW tab, present only so the tests can prove it is refused. Its columns are named
# after the real ones; its values are placeholders, not data.
RAW_APPOINTMENTS_DEMO: list[list[object]] = [
    ["Provider", "Date", "Patient", "BirthDate1", "HomeEmail", "MainPhoneAndExtension"],
    ["blocked", "blocked", "blocked", "blocked", "blocked", "blocked"],
]


DEMO_TAB_NAME = "Q2 Snapshot (demo)"
DEMO_ABBREVIATIONS_TAB = "Abbreviations"
DEMO_CONFIG_TAB = "Config"

DEMO_TABS: dict[str, list[list[object]]] = {
    DEMO_TAB_NAME: Q2_SNAPSHOT_DEMO,
    DEMO_ABBREVIATIONS_TAB: ABBREVIATIONS_DEMO,
    DEMO_CONFIG_TAB: CONFIG_DEMO,
    "RAW_Appointments": RAW_APPOINTMENTS_DEMO,
}

DEMO_CLEAN_ROW_COUNT = len(_CLEAN)
DEMO_BROKEN_ROW_COUNT = len(_BROKEN)

# The therapists the demo source expects to already exist, with the aliases that
# resolve its rows. UNKNOWNPERSON is deliberately absent so that one row is rejected.
DEMO_THERAPISTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Quincey", ("QUINCEY", "TOBIAS QUINCEY")),
    ("Thornbury", ("THORNBURY",)),
    ("Wren", ("WREN", "ROSALIND WREN, LMFT (R.WREN)")),
)
