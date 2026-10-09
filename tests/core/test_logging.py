"""P2-3 日志分级单测：--verbose/--quiet/--debug 级别控制。"""

from __future__ import annotations

import logging

from sunoauxtool.logging_setup import setup_logging


def test_default_info_level():
    setup_logging()
    assert logging.getLogger("sunoauxtool").level == logging.INFO


def test_verbose_debug_level():
    setup_logging(verbose=True)
    assert logging.getLogger("sunoauxtool").level == logging.DEBUG


def test_debug_debug_level():
    setup_logging(debug=True)
    assert logging.getLogger("sunoauxtool").level == logging.DEBUG


def test_quiet_error_level():
    setup_logging(quiet=True)
    assert logging.getLogger("sunoauxtool").level == logging.ERROR


def test_quiet_wins_over_verbose():
    setup_logging(verbose=True, quiet=True)
    assert logging.getLogger("sunoauxtool").level == logging.ERROR
