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
    """Why a gamma flag did not become CLOB_VERIFIED."""

    NO_BOOK = "no_book"
    MISSING_BID_ASK = "missing_bid_ask"
    EDGE_EVAPORATED = "edge_evaporated"
    BELOW_MIN_EDGE = "below_min_edge"
    ILLIQUID = "illiquid"
    INVALID_PRICES = "invalid_prices"
    OTHER = "other"


class RiskRejectReason(str, Enum):
    """Why a CLOB-verified opp failed the risk gate."""

    KILL_SWITCH = "kill_switch"
    STUDY_MODE = "study_mode"
    BELOW_MIN_EDGE = "below_min_edge"
    MAX_POSITION = "max_position"
    MAX_OPEN = "max_open"
    DAILY_LOSS = "daily_loss"
    DAILY_TRADES = "daily_trades"
    CATEGORY_BLOCKED = "category_blocked"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    DUPLICATE_OPEN = "duplicate_open"
    OTHER = "other"


class ExecMode(str, Enum):
    """Execution mode — paper is Phase 2 default; live needs credentials + opt-in."""

    PAPER = "paper"
    LIVE = "live"
    DISABLED = "disabled"
