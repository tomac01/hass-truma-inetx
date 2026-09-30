#!/usr/bin/env python3
"""Offline check for the "the panel refuses to encrypt" repair issue.

No hardware, no Home Assistant install: the HA imports are stubbed so the real
``const`` classifier and the real coordinator methods run.

Why this exists: on 2026-09-28 the proxy lost its key for the panel while the
panel went on listing it as paired (REV-007). Every session got a GATT link up,
was refused with ATT error 15, and gave up -- for 41 hours, with every entity
unavailable and nothing anywhere saying why. The risk in surfacing it is
crying wolf: warning on a single refusal, or hanging this advice on a failure
that is not this fault at all.

What it pins:

1. the classifier knows the measured wording and does NOT answer to the
   unbonded error beside it,
2. a run shorter than the threshold stays silent,
3. the issue is raised exactly at the threshold and NOT re-raised afterwards,
4. a successful session clears both the run and the issue,
5. an attempt that never reached GATT does not count -- that is the no-route
   fault, with different advice,
6. any other kind of failure does not count, and breaks the run,
7. the two issues are separate, in both directions: clearing either one leaves
   the other standing,
8. every language file actually carries the text, it names the count it took
   and it names the remedy -- a translation_key with nothing behind it shows
   the user an empty card, and that is the very bug
   ISSUE_NO_PROXY_ROUTE_LEGACY exists to clean up after,
9. the two call sites are where the design says they are: counted before the
   client is torn down, cleared only after a subscribe has proved the key.

Run: ``python3 tests/test_lost_bond_issue.py``
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stubs  # noqa: E402

PANEL = "Truma iNetX-BBCCDD"

# Measured on the van, 2026-09-28, once per session over every cycle: what an
# ESPHome proxy raises when the panel demands encryption we cannot provide.
REFUSED = "GATT Error handle=35 error=15 Insufficient encryption"
# Its sibling, and deliberately NOT this fault: ATT 5 means the panel does not
# know us, which ordinary pairing fixes.
UNBONDED = "GATT Error handle=35 error=5 Insufficient authentication"

# The texts spell the threshold out in words rather than printing the constant,
# so this is how the two are kept in step. Checking for the digit instead would
# prove nothing: every text carries a numbered list of steps, so "3" is in all
# of them whatever the threshold says.
COUNT_WORDS = {2: ("twice", "zweimal"), 3: ("three times", "dreimal"),
               4: ("four times", "viermal")}


class _IssueRegistry:
    """Record create/delete calls the way HA's issue_registry would apply them."""

    class IssueSeverity:
        WARNING = "warning"
        ERROR = "error"

    def __init__(self) -> None:
        self.active: dict[str, dict] = {}
        self.creates = 0
        self.deletes = 0

    def async_create_issue(self, _hass, domain, issue_id, **kw):
        self.creates += 1
        self.active[f"{domain}.{issue_id}"] = kw

    def async_delete_issue(self, _hass, domain, issue_id):
        self.deletes += 1
        self.active.pop(f"{domain}.{issue_id}", None)


IR = _IssueRegistry()

stubs.install_homeassistant()
# Before the coordinator is loaded: it binds ``issue_registry`` by name at
# import time, so a double swapped in afterwards would never be seen.
sys.modules["homeassistant.helpers"].issue_registry = IR
stubs.stub_transport()
stubs.mod("truma_pkg.session", run_startup=None, request_measurements=None,
          discover_params=None, StartupFailed=RuntimeError, handle_frame=None)
stubs.stub_protocol()

stubs.load_truma("const")
stubs.load("bus")
CONST = stubs.load("const")
stubs.load("operations")
COORD = stubs.load("coordinator")

KEY = f"{CONST.DOMAIN}.{CONST.ISSUE_LOST_BOND}"
NO_ROUTE_KEY = f"{CONST.DOMAIN}.{CONST.ISSUE_NO_ROUTE}"


class _Client:
    """A TrumaBleClient, in the one property the gate reads.

    ``transport`` is ``None`` until ``establish_connection`` has returned and
    the inner bleak client is in place (ble.py:222-224, :247). That is exactly
    the question "did a GATT connection come up", and it stays answered after
    the link has gone -- which it has, because the proxy tears it down on the
    refused write.
    """

    def __init__(self, transport: str | None) -> None:
        self.transport = transport


def _proxy_link() -> _Client:
    return _Client("proxy")


def _no_link() -> _Client:
    return _Client(None)


class _Coord:
    """Carries only what the methods under test touch."""

    hass = object()
    unique_id = PANEL

    def __init__(self, client: _Client | None = None) -> None:
        self._encryption_failures = 0
        self._no_route_misses = 0
        self._client = client if client is not None else _proxy_link()

    _async_note_encryption_failure = (
        COORD.TrumaCoordinator._async_note_encryption_failure
    )
    _async_clear_encryption_failure = (
        COORD.TrumaCoordinator._async_clear_encryption_failure
    )
    # Borrowed only to prove the other issue's clear keeps its hands off ours.
    _async_clear_no_route = COORD.TrumaCoordinator._async_clear_no_route


def _refuse(coord: _Coord, times: int = 1) -> None:
    """Let ``times`` sessions end the way the measured ones did."""
    for _ in range(times):
        coord._async_note_encryption_failure(RuntimeError(REFUSED))


def test_classifier_knows_the_measured_wording() -> None:
    assert CONST.is_encryption_failure(RuntimeError(REFUSED))
    # Case is the library's to change, the words are the panel's.
    assert CONST.is_encryption_failure(RuntimeError(REFUSED.upper()))
    # ATT 5 is the unbonded case: ordinary pairing fixes it, and this advice
    # -- delete the panel's entry first -- would be wrong for it.
    assert not CONST.is_encryption_failure(RuntimeError(UNBONDED))
    assert not CONST.is_encryption_failure(TimeoutError("timed out"))


def test_debounced_warning() -> None:
    threshold = CONST.ENCRYPTION_FAILURES_BEFORE_WARNING
    assert threshold >= 2, "a threshold of 1 would warn on a single refusal"

    IR.__init__()
    c = _Coord()

    _refuse(c, threshold - 1)
    assert KEY not in IR.active, "warned before the debounce threshold"

    _refuse(c)
    assert KEY in IR.active, "no issue raised at the threshold"
    assert IR.active[KEY]["is_fixable"] is False
    assert IR.active[KEY]["translation_key"] == CONST.ISSUE_LOST_BOND
    assert IR.active[KEY]["severity"] == IR.IssueSeverity.WARNING


def test_the_fourth_refusal_raises_nothing() -> None:
    """The panel refuses every reconnect; the user may be told once."""
    IR.__init__()
    c = _Coord()
    _refuse(c, CONST.ENCRYPTION_FAILURES_BEFORE_WARNING)
    before = IR.creates
    _refuse(c)
    assert IR.creates == before, "issue re-created on the fourth refusal"
    _refuse(c, 20)
    assert IR.creates == before, "issue re-created while the fault lasted"


def test_a_successful_session_clears() -> None:
    IR.__init__()
    c = _Coord()
    _refuse(c, CONST.ENCRYPTION_FAILURES_BEFORE_WARNING)
    assert KEY in IR.active

    c._async_clear_encryption_failure()
    assert KEY not in IR.active, "issue survived a session that encrypted"
    assert c._encryption_failures == 0

    # And the full threshold has to elapse again before re-warning, or a
    # flapping link re-notifies on every second poll.
    _refuse(c, CONST.ENCRYPTION_FAILURES_BEFORE_WARNING - 1)
    assert KEY not in IR.active


def test_an_attempt_that_never_reached_gatt_does_not_count() -> None:
    """No link, no refusal. That fault is ISSUE_NO_ROUTE's, with other advice.

    Telling this user to delete the panel's pairing entry would be actively
    harmful: it costs them the bond they still have.
    """
    IR.__init__()
    c = _Coord(_no_link())
    _refuse(c, CONST.ENCRYPTION_FAILURES_BEFORE_WARNING * 3)
    assert IR.creates == 0
    # Not counted either: otherwise a spell out of range pre-loads the counter
    # and the first real refusal trips the warning.
    assert c._encryption_failures == 0

    # Same for an attempt that got no client at all.
    c = _Coord()
    c._client = None
    _refuse(c, CONST.ENCRYPTION_FAILURES_BEFORE_WARNING * 3)
    assert IR.creates == 0
    assert c._encryption_failures == 0


def test_another_kind_of_failure_breaks_the_run() -> None:
    """Mixed failures are not this fault and must not add up to it."""
    IR.__init__()
    c = _Coord()
    _refuse(c, CONST.ENCRYPTION_FAILURES_BEFORE_WARNING - 1)
    c._async_note_encryption_failure(TimeoutError("timed out waiting for ack"))
    assert c._encryption_failures == 0, "an unrelated failure kept the run"
    _refuse(c)
    assert IR.creates == 0, "a broken run still reached the threshold"


def test_the_two_issues_stay_apart() -> None:
    """Neither clear may touch the other issue: their advice is opposite.

    Both directions, because both are one line away from being wrong: a clear
    that deletes the wrong id, and a clear that deletes both "to be safe".
    """
    IR.__init__()
    c = _Coord()
    _refuse(c, CONST.ENCRYPTION_FAILURES_BEFORE_WARNING)
    IR.async_create_issue(None, CONST.DOMAIN, CONST.ISSUE_NO_ROUTE)
    assert CONST.ISSUE_LOST_BOND != CONST.ISSUE_NO_ROUTE

    c._async_clear_encryption_failure()
    assert KEY not in IR.active
    assert NO_ROUTE_KEY in IR.active, "clearing ours dropped the no-route issue"

    # The other way round: a resolve that succeeded says nothing about
    # encryption, so _async_clear_no_route must leave this issue standing.
    IR.__init__()
    c = _Coord()
    _refuse(c, CONST.ENCRYPTION_FAILURES_BEFORE_WARNING)
    assert KEY in IR.active
    c._async_clear_no_route()
    assert KEY in IR.active, "the no-route clear dropped the lost-bond issue"


def test_every_language_has_the_text() -> None:
    """A raised issue with no text behind it is an empty card in Repairs."""
    src = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"
    n = CONST.ENCRYPTION_FAILURES_BEFORE_WARNING
    assert n in COUNT_WORDS, (
        f"the texts spell the threshold out; add the word for {n} to "
        "COUNT_WORDS and to all three files"
    )
    for path in (src / "strings.json", src / "translations" / "de.json",
                 src / "translations" / "en.json"):
        issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
        assert CONST.ISSUE_LOST_BOND in issues, f"{path.name} has no text"
        text = issues[CONST.ISSUE_LOST_BOND]
        assert text["title"].strip()
        lower = text["description"].lower()
        # The three things the text exists to say (REV-007). Checked by the
        # threshold and by the two words of the remedy that cost a pairing
        # slot when they are missing, not by the whole wording -- rephrasing
        # is free, dropping the instruction is not.
        assert any(word in lower for word in COUNT_WORDS[n]), (
            f"{path.name} does not say how many refusals it took"
        )
        assert "delete" in lower or "löschen" in lower, (
            f"{path.name} never tells the user to delete the old entry first"
        )
        assert "slot" in lower or "platz" in lower or "plätze" in lower, (
            f"{path.name} never says that deleting first costs no pairing slot"
        )


def test_the_hooks_are_wired() -> None:
    """The counter is useless unless the session loop actually calls it.

    Read off the real source rather than driving the whole loop: standing up
    ``_run`` needs a Store, a clock and a live client (that is
    tests/test_coordinator_state_flags.py's job), while what can go wrong here
    is placement -- a method nobody calls, or one called a few lines too early.
    None of it is reachable from the checks above.
    """
    run = inspect.getsource(COORD.TrumaCoordinator._run)
    assert "_async_note_encryption_failure(exc)" in run, (
        "the session loop never counts a refusal"
    )
    # Before the client is torn down: _disconnect_client drops ``_client``,
    # and the client is the only witness that a GATT connection came up.
    assert run.index("_async_note_encryption_failure") < run.index(
        "await self._disconnect_client()"
    ), "the refusal is counted after the client has already been dropped"

    connect = inspect.getsource(COORD.TrumaCoordinator._connect_and_run)
    # Twice, because there are two ways a session comes up: the adopted
    # pairing hand-off and the ordinary dial. Both subscribe, so both prove
    # the key, and a clear on only one of them leaves the issue standing after
    # a successful re-pair -- which is the moment it is most wrong.
    assert connect.count("_async_clear_encryption_failure()") == 2, (
        "a session that encrypted does not clear the issue on both paths"
    )
    # Each clear must sit behind the thing that proves the key, not beside
    # _async_clear_no_route. That line is reached as soon as the *resolver*
    # returned an address -- before a single byte has been encrypted -- so a
    # clear there would drop the issue on the very attempt about to be refused
    # again, and it could never stand for two consecutive sessions.
    assert connect.index("_async_clear_encryption_failure") > connect.index(
        "await client.adopt(initial)"
    ), "the adopted path clears the issue before it has encrypted anything"
    assert connect.index("await client.connect(ble_device)") < connect.rindex(
        "_async_clear_encryption_failure"
    ), "the dial path clears the issue before it has encrypted anything"


if __name__ == "__main__":
    stubs.run_tests(globals(), "lost-bond repair issue")
