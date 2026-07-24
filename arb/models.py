"""Opportunity lifecycle and reject reasons across phases."""

from __future__ import annotations

from enum import Enum


class OppState(str, Enum):
    """State machine for a detected opportunity.

    Phase 1: DISCOVERED → GAMMA_FLAG → CLOB_VERIFIED | REJECTED
    Phase 2: CLOB_VERIFIED → RISK_OK | REJECTED → ORDER_PLACED → FILLED → SETTLED → CLOSED
    """

    DISCOVERED = "DISCOVERED"
    GAMMA_FLAG = "GAMMA_FLAG"
    CLOB_VERIFIED = "CLOB_VERIFIED"
    REJECTED = "REJECTED"
    RISK_OK = "RISK_OK"
    ORDER_PLACED = "ORDER_PLACED"
    FILLED = "FILLED"
    SETTLED = "SETTLED"
    CLOSED = "CLOSED"


class RejectReason(str, Enum):
    """Why a gamma flag did not become an executable CLOB plan."""

    NO_BOOK = "no_book"
    MISSING_BID_ASK = "missing_bid_ask"
    EDGE_EVAPORATED = "edge_evaporated"
    BELOW_MIN_EDGE = "below_min_edge"
    ILLIQUID = "illiquid"
    INVALID_PRICES = "invalid_prices"
    # Phase 2 — complete-set plan validation failures
    STALE_BOOK = "stale_book"
    UNKNOWN_FEE = "unknown_fee"
    INVALID_TICK = "invalid_tick"
    BELOW_MIN_SIZE = "below_min_size"
    UNSUPPORTED = "unsupported"
    OTHER = "other"


class RiskRejectReason(str, Enum):
    """Why a CLOB-verified opp failed the risk gate."""

    KILL_SWITCH = "kill_switch"
    STUDY_MODE = "study_mode"
    SCAN_ONLY = "scan_only"
    SHADOW = "shadow"
    PAPER_EXECUTION_DISABLED = "paper_execution_disabled"
    UNSUPPORTED_STRATEGY = "unsupported_strategy"
    BELOW_MIN_EDGE = "below_min_edge"
    MAX_POSITION = "max_position"
    MAX_OPEN = "max_open"
    DAILY_LOSS = "daily_loss"
    DAILY_TRADES = "daily_trades"
    CATEGORY_BLOCKED = "category_blocked"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    DUPLICATE_OPEN = "duplicate_open"
    EXEC_VERIFY_FAILED = "exec_verify_failed"
    GAMMA_ONLY = "gamma_only"
    OTHER = "other"


class ExecMode(str, Enum):
    """Execution mode — paper is Phase 2 default; live needs credentials + opt-in."""

    PAPER = "paper"
    LIVE = "live"
    DISABLED = "disabled"


class SafetyMode(str, Enum):
    """Master execution-safety switch — scanner/shadow-first.

    Governs whether the bot may create simulated or real orders/fills at all,
    independent of the lower-level ``ExecMode`` (paper vs live mechanics):

    - ``SCAN_ONLY``  — scan, verify, and log only. No orders, no fills. (default)
    - ``SHADOW``     — record observations (shadow candidates) but no orders/fills.
    - ``PAPER_EXECUTION`` — simulated order/fill creation, but ONLY when the
      explicit ``ARB_PAPER_EXECUTION_ENABLED`` gate is also true.
    - ``LIVE``       — real orders, still behind every live gate (allow_live,
      dry_run off, kill switch off, private key, study off).
    """

    SCAN_ONLY = "scan_only"
    SHADOW = "shadow"
    PAPER_EXECUTION = "paper_execution"
    LIVE = "live"
