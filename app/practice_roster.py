"""The practice's therapist roster, taken from its own Valant exports.

Extracted 2026-07-28 from the Valant Live Review workbook: the display names come
from the provider fields of the RAW_PatientStatement, RAW_Appointments, and
RAW_Documentation exports, and each alias is the surname exactly as the Q sheet's
Therapist column writes it. Nothing here is guessed, and none of it is patient data:
therapist names are workforce data, classified not PHI in SECURITY.md section 2.

This list exists to seed editable records, one admin click, once. It is not consulted
during import: the importer resolves names only through the therapist and alias tables,
so editing or deactivating a seeded record in the app fully overrides this file.

PAVLOVA and ROSENFELD are two different people, confirmed by the practice and by the
exports themselves: Inna Pavlova-Rosenfeld and Anita Rosenfeld each appear as their own
provider. They are seeded as separate records.

Employment type is deliberately left as OTHER on every seeded record, because the
exports do not know who is salaried and who is percentage based. OTHER means the
therapist is not measured against the session threshold, and the In development page
keeps flagging that until an admin classifies each person.
"""

from __future__ import annotations

# (sheet alias, display name, credential as exported)
PRACTICE_ROSTER: tuple[tuple[str, str, str], ...] = (
    ("ALEXANDER", "Elaine Alexander", "PhD"),
    ("BEN-AMOS", "Batsheva Ben-Amos", "PhD"),
    ("BENNER", "Violet Benner", "LPC"),
    ("BOLGER", "Davida Bolger", "LPC"),
    ("COLE-CERRONE", "Mary Cole-Cerrone", "LCSW"),
    ("DEGUIDA", "Ellyn DeGuida", "LCSW"),
    ("DRAKE", "Lesa Drake", "LCSW"),
    ("EBHUOMA", "Deborah Ebhuoma", "LCSW"),
    ("EINBINDER-SCHATZ", "Kathy Einbinder-Schatz", "LPC"),
    ("ELLIAS", "Maggie Ellias", "LCSW"),
    ("FLEISHMAN", "Laurie Fleishman-Pogach", "LPC"),
    ("HALL", "Siobhann Hall", "LCSW"),
    ("HARRIS", "Andrea Harris", "LMFT"),
    ("HOLLIDAY", "Marguerite Holliday", "LPC"),
    ("HUNT", "Susan Hunt", "PsyD"),
    ("IVERS", "John Ivers", "LCSW"),
    ("JORDAN", "Michael Jordan", "LCSW"),
    ("KAHN", "Jennifer Kahn", "LCSW"),
    ("KOSZAREK", "Stephanie Koszarek", "LPC"),
    ("LAMB", "Ronald Lamb", "LCSW"),
    ("LEHRER", "Jeanne Lehrer", "PhD"),
    ("LUTZ", "James Lutz", "LCSW"),
    ("MANOHARAN", "Christy Manoharan", "LPC"),
    ("MAURER", "Kristin Maurer", "LPC"),
    ("MCNULTY", "Mary Kathleen McNulty", "LPC"),
    ("OMEALLY", "Nicholas O'Meally", "LCSW"),
    ("PAVLOVA", "Inna Pavlova-Rosenfeld", "LPC"),
    ("PERLMUTTER", "Bonnie Perlmutter", "PhD"),
    ("RANDAZZO", "Rachel Randazzo", "LCSW"),
    ("ROSENFELD", "Anita Rosenfeld", "LPC"),
    ("ROTH", "Carol Roth", "LCSW"),
    ("RUDICK", "David Rudick", "PhD"),
    ("SALERNO SIGMAN", "Michelle Salerno Sigman", "LPC"),
    ("SCHOR", "Robin Schor", "MD"),
    ("SCHWARTZ", "Frank Schwartz", "PhD"),
    ("SCOTT", "Natalie Scott", "LCSW"),
    ("SEGAL", "Sion Segal", "PhD"),
    ("SHARIF", "Mushiyrah Sharif", "LMFT"),
    ("SIEGEL", "Debra Siegel", "LCSW"),
    ("SOLAZZO", "Alexandra Solazzo", "LPC"),
    ("YEO", "Hyung Yeo", "MD"),
)

SEED_NOTE = (
    "Seeded from the practice's own Valant exports on 2026-07-28. "
    "Confirm the employment type; until then this therapist is not measured "
    "against the session threshold."
)
