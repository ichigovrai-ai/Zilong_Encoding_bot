"""Guard against 'button does nothing' bugs: every valid callback verb must be
routed by the callback handler (either a wizard verb or one handled inline)."""
from __future__ import annotations

import inspect

from bot.handlers import callbacks, wizard
from bot.ui.callbacks import VALID_VERBS


# Verbs the router handles directly (not through wizard.handle_selection).
# Kept explicit so adding a verb without routing it fails this test.
INLINE_VERBS = frozenset({
    "n",    # decorative no-op
    "st",   # settings toggle
    "sd",   # seedr management
    "x",    # cancel
    "rt",   # retry
    "mi",   # mediainfo card/full toggle
    "cv",   # convert subtitle
    "a",    # action menu
    "go",   # start / confirm
    "e", "ea", "fx", "fs",  # episode selection
})


def test_every_verb_is_routed():
    routed = wizard.WIZARD_VERBS | INLINE_VERBS
    unrouted = VALID_VERBS - routed
    assert not unrouted, f"These callback verbs are never handled: {unrouted}"


def test_wizard_verbs_actually_handled_by_handle_selection():
    # handle_selection dispatches on `verb ==`/`in` — its source must mention
    # every wizard verb, so a verb added to WIZARD_VERBS without handling fails.
    src = inspect.getsource(wizard.handle_selection)
    for verb in wizard.WIZARD_VERBS:
        assert f'"{verb}"' in src, f"handle_selection doesn't handle verb {verb!r}"


def test_router_dispatches_wizard_verbs():
    # The router must reference wizard.WIZARD_VERBS (not a hand-copied list that
    # can drift out of sync).
    src = inspect.getsource(callbacks.on_callback)
    assert "wizard.WIZARD_VERBS" in src
