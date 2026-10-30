"""Decision logic for the self-improving memory. Pure functions: no database, no browser, no clock of their own.

The loop (see README, "Memory"):  observe -> distill -> apply -> verify -> prune.

  * Navigation "arms" are a short ladder of load strategies, cheapest first. Each site gets its own record of how every
    arm has worked. The next visit uses the arm with the best score (Laplace success rate minus a small cost for the
    slower arms), skips arms that failed twice in a row for a week, and now and then probes the cheaper arm again so a
    site that stopped blocking is noticed. Old evidence decays with a 30 day half-life.
  * Selectors the browser re-uses on its own (pop-up dismissers, extractors) are validated here against a strict
    grammar. Learned selectors are only ever handed to the browser, never shown to the model, so a page cannot use
    memory to talk to the assistant.
"""
from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass

HALF_LIFE_DAYS = 30.0
DEMOTE_AFTER = 2  # consecutive failures before a strategy is benched
COOLDOWN_DAYS = 7.0  # how long a benched strategy stays benched
COST_PER_STEP = 0.04  # score penalty per step up the ladder (slower, heavier loads)
MIN_EVIDENCE = 0.3  # decayed run count below which a record is forgotten
FORGIVE_BELOW = 1.9  # runs of evidence needed before a bad record counts against an arm (see score)
MIN_RATE = 0.6  # smoothed success rate ((ok+1)/(runs+2)) a recipe needs to keep being used: one miss in three is forgiven
MIN_OK = 1.9  # "two successes": a hair under 2 because evidence decays continuously (2 runs a minute apart sum to 1.9999)
EXPLORE = {"off": 0.0, "low": 0.05, "medium": 0.08, "high": 0.15}


# ---------------------------------------------------------------- navigation arms
@dataclass(frozen=True)
class Arm:
    index: int
    wait_until: str
    settle_ms: int

    @property
    def label(self) -> str:
        return f"{self.wait_until}+{self.settle_ms}ms"


ARMS = (
    Arm(0, "domcontentloaded", 500),
    Arm(1, "load", 1500),
    Arm(2, "networkidle", 2500),
    Arm(3, "networkidle", 4000),
)
DEFAULT_ARM = 0


@dataclass(frozen=True)
class Plan:
    arm: int
    wait_until: str
    settle_ms: int
    reason: str  # default | learned | probe | all-benched

    @property
    def is_default(self) -> bool:
        return self.arm == DEFAULT_ARM and self.reason == "default"


@dataclass
class ArmStat:
    runs: float = 0.0
    ok: float = 0.0
    demoted_until: float = 0.0
    last_seen: float = 0.0


def arm_for(wait_until: str | None, settle_ms: int | None) -> int | None:
    """Index of the arm an explicit navigate call corresponds to, or None (custom settings are not learned from)."""
    wu = wait_until or ARMS[DEFAULT_ARM].wait_until
    st = ARMS[DEFAULT_ARM].settle_ms if settle_ms is None else settle_ms
    for a in ARMS:
        if a.wait_until == wu and a.settle_ms == st:
            return a.index
    return None


def decay(value: float, last_seen: float, now: float) -> float:
    """Halve evidence every HALF_LIFE_DAYS so old lessons fade and a changed site can be re-learned."""
    if not last_seen or now <= last_seen:
        return value
    return value * math.pow(0.5, (now - last_seen) / (HALF_LIFE_DAYS * 86400))


def score(stat: ArmStat | None, index: int) -> float:
    """Smoothed success rate minus a cost per step up the ladder. With less than two runs of evidence an arm is never
    scored below an untried one: a single failure is forgiven, and old evidence that has decayed away is forgotten."""
    runs, ok = (stat.runs, stat.ok) if stat else (0.0, 0.0)
    rate = (ok + 1.0) / (runs + 2.0)
    if runs < FORGIVE_BELOW:
        rate = max(rate, 0.5)
    return rate - COST_PER_STEP * index


def choose_arm(stats: dict[int, ArmStat], now: float, rng: random.Random | None = None, explore: float = EXPLORE["medium"]) -> Plan:
    """Pick the arm for the next visit to a site. `stats` already carries decayed counts."""
    if not any(s.runs > 0 for s in stats.values()):
        a = ARMS[DEFAULT_ARM]
        return Plan(a.index, a.wait_until, a.settle_ms, "default")
    usable = [i for i in range(len(ARMS)) if (stats.get(i) or ArmStat()).demoted_until <= now]
    reason = "learned"
    if not usable:  # everything is benched: take the least bad rather than refuse to browse
        usable, reason = list(range(len(ARMS))), "all-benched"
    best = max(usable, key=lambda i: (score(stats.get(i), i), -i))
    if best > 0 and rng is not None and explore > 0 and rng.random() < explore:
        cheaper = best - 1
        st = stats.get(cheaper) or ArmStat()
        stale = st.last_seen and (now - st.last_seen) > 14 * 86400
        if st.demoted_until <= now and (st.runs < 6 or stale):
            best, reason = cheaper, "probe"
    a = ARMS[best]
    return Plan(a.index, a.wait_until, a.settle_ms, reason)


def reliable(ok: float, runs: float) -> bool:
    """Enough successes, at a good enough smoothed rate, to keep using a recipe. A small tolerance absorbs the float
    noise left by continuous decay (two good runs a second apart sum to 1.999999, not 2)."""
    return ok >= MIN_OK and (ok + 1.0) / (runs + 2.0) >= MIN_RATE - 0.01


def next_streak(streak: int, ok: bool, now: float) -> tuple[int, float]:
    """(consecutive failures, demoted_until) after one more outcome."""
    if ok:
        return 0, 0.0
    streak += 1
    return streak, (now + COOLDOWN_DAYS * 86400 if streak >= DEMOTE_AFTER else 0.0)


# ---------------------------------------------------------------- selector grammar
_TAGS = frozenset(
    "a abbr article aside b blockquote body button canvas caption code dd details dialog div dl dt em fieldset "
    "figcaption figure footer form h1 h2 h3 h4 h5 h6 header i img input label legend li main mark nav ol option p "
    "pre section select small span strong summary svg table tbody td template textarea tfoot th thead time tr u ul "
    "video".split()
)
_NAME = r"[A-Za-z_][\w-]{0,59}"
_ATTR = r"\[[A-Za-z_][\w:-]{0,30}(?:[~|^$*]?=\"[\w \-]{0,40}\")?\]"
_PIECE = rf"(?:#{_NAME}|\.{_NAME}|{_ATTR}|:first-child|:last-child)"
_COMPOUND_RX = re.compile(rf"(?P<tag>[a-z][a-z0-9]*|\*)?(?P<rest>{_PIECE}*)")
_COMB_RX = re.compile(r"\s*[>+~]\s*|\s+")
MAX_SELECTOR = 160
MAX_COMPOUNDS = 6


def safe_selector(sel) -> str | None:
    """The selector if it only uses plain CSS (tags, #id, .class, [attr] / [attr="v"], > + ~ and descendant spaces),
    else None. No Playwright engines (text=, xpath=, >>), no scripts, no sentences: only known HTML tag names."""
    if not isinstance(sel, str):
        return None
    sel = sel.strip()
    if not sel or len(sel) > MAX_SELECTOR or "\n" in sel:
        return None
    i, count = 0, 0
    while True:
        m = _COMPOUND_RX.match(sel, i)
        if not m or m.end() == i:  # an empty compound: ", a", "a >", "a  > > b"
            return None
        tag = m.group("tag")
        if tag and tag != "*" and tag not in _TAGS:
            return None
        count += 1
        if count > MAX_COMPOUNDS:
            return None
        i = m.end()
        if i == len(sel):
            return sel
        c = _COMB_RX.match(sel, i)
        if not c:
            return None  # something the grammar does not cover (a comma, an engine prefix, a stray quote ...)
        i = c.end()
        if i == len(sel):
            return None  # dangling combinator


_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,24}$")
_ATTR_NAME = re.compile(r"^[a-z][a-z-]{0,24}$")


def safe_field_spec(spec) -> str | None:
    """'css', 'css@attr' or '@attr' for extract_structured fields, validated like selectors."""
    if not isinstance(spec, str):
        return None
    css, sep, attr = spec.partition("@")
    if sep and not _ATTR_NAME.match(attr):
        return None
    if css and safe_selector(css) is None:
        return None
    return spec if (css or sep) else None


def canonical_extractor(item_selector, fields) -> str | None:
    """Canonical JSON of a validated extract_structured recipe, or None if any part is not plain CSS."""
    if safe_selector(item_selector) is None or not isinstance(fields, dict) or not 1 <= len(fields) <= 12:
        return None
    clean = {}
    for k, v in fields.items():
        if not isinstance(k, str) or not _FIELD_NAME.match(k) or safe_field_spec(v) is None:
            return None
        clean[k] = v
    return json.dumps({"item": item_selector.strip(), "fields": clean}, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------- pop-up dismissal
_DISMISS_WORDS = re.compile(
    r"\b(accept|agree|allow|got it|ok|okay|close|dismiss|reject|decline|deny|necessary|continue|no thanks|not now|"
    r"consent|understood|i understand)\b|αποδοχ|αποδέχ|συμφων|κλείσιμο|εντάξει|απόρριψη|απορρίπτω|κατάλαβα|"
    r"akzeptieren|zustimmen|ablehnen|schlie(ß|ss)en|accepter|refuser|fermer|aceptar|rechazar|cerrar|accetta|rifiuta|chiudi",
    re.I,
)
MAX_BUTTON_TEXT = 40


def looks_like_dismiss(text) -> bool:
    """Does a button label look like a cookie/pop-up dismiss action? (Learning only happens for these.)"""
    if not isinstance(text, str):
        return False
    t = " ".join(text.split())
    return 0 < len(t) <= MAX_BUTTON_TEXT and bool(_DISMISS_WORDS.search(t))
