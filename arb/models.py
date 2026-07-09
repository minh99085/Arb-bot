"""Opportunity lifecycle states for Phase 1 study mode."""

from __future__ import annotations

from enum import Enum


class OppState(str, Enum):
    """State machine for a detected opportunity.

    Phase 1 only advances through DISCOVERED → GAMMA_FLAG → CLOB_VERIFIED / REJECTED.
    Later phases add RISK_OK → ORDER_PLACED → FILLED → SETTLED → CLOSED.
    """

    DISCOVERED = "DISCOVERED"
    GAMMA_FLAG = "GAMMA_FLAG"
    CLOB_VERIFIED = "CLOB_VERIFIED"
    REJECTED = "REJECTED"
    # Reserved for Phase 2+
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
