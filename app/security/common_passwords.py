"""A list of commonly used passwords, checked at password set time.

Deliberately a curated list rather than a multi megabyte corpus: it covers the
passwords people actually reach for first, plus terms specific to this practice that
staff here would plausibly pick. A longer list can be swapped in later by replacing
COMMON_PASSWORDS with a loader; nothing else needs to change.

Entries are lowercase. The caller lowercases before comparing.
"""

from __future__ import annotations

_BASE = """
123456 123456789 12345678 12345 1234567 1234567890 123123 111111 000000 654321
password password1 password123 passw0rd p@ssword p@ssw0rd passwords letmein
qwerty qwerty123 qwertyuiop 1qaz2wsx zaq12wsx qazwsx asdfgh asdfghjkl zxcvbnm
abc123 abcd1234 a1b2c3d4 iloveyou princess sunshine shadow monkey dragon
football baseball basketball superman batman pokemon starwars trustno1
welcome welcome1 welcome123 admin admin123 administrator root toor guest
login logmein master secret changeme change123 default temp temporary
whatever nothing anything nobody somebody freedom trustme access
michael jennifer jordan hunter harley ranger buster soccer tigger charlie
summer winter spring autumn january february monday sunday
computer internet samsung google facebook amazon microsoft apple
qwerty1234 asdf1234 zxcv1234 1q2w3e4r 1q2w3e4r5t 1qazxsw2
letmein123 iloveyou1 princess1 sunshine1 shadow123 dragon123
"""

# Terms specific to this deployment. Staff at a behavioral health practice reach for
# their own vocabulary, and none of it is unguessable to someone who knows the place.
_LOCAL = """
sri sripsych sripsychological psychological therapy therapist therapists
counseling counselling behavioral behavioural clinic clinical practice
patient patients valant jenkintown revere reverecommons telehealth
dashboard reports billing frontdesk front desk reception
sri2026 sri12345 sridashboard sripassword sriadmin
"""


def _expand(raw: str) -> set[str]:
    words = {w for w in raw.split() if w}
    expanded = set(words)
    for word in words:
        # The obvious suffixes people add when a policy demands a number.
        for suffix in ("1", "12", "123", "1234", "!", "1!", "2024", "2025", "2026"):
            expanded.add(word + suffix)
        expanded.add(word.capitalize().lower())
    return expanded


COMMON_PASSWORDS: frozenset[str] = frozenset(_expand(_BASE) | _expand(_LOCAL))
