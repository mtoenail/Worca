# swarm/exits.py - the B2 exit rules, in ONE place.
#
# The shadow book and the live position manager both call this. If the rule lived in
# two implementations, the swarm-vs-solo comparison would eventually be measuring the
# drift between them rather than the Oracle's allocation.

GAMMA_TAKE_PROFIT = 0.50      # +50% of premium
GAMMA_STOP_LOSS = -0.50       # -50% of premium
GAMMA_WALL_INVALIDATION = 0.02  # spot >2% from the wall -> thesis is gone
GAMMA_TIME_STOP_DTE = 5
VOL_REVERSION_Z = 0.5         # |z| back inside 0.5 -> dislocation realised
VOL_STOP_LOSS = -1.0          # -100% of the net debit
VOL_TIME_STOP_DTE = 7         # on the FRONT leg
BLACKOUT_DTE = 1              # force-close into the expiry-week close


def exit_reason(agent, *, entry_debit, mark, front_dte, spot=None,
                wall_strike=None, live_zscore=None):
    """Why this position should close now, or None to hold.

    `mark` and `entry_debit` are both dollars per unit, so the P&L fraction is signed
    against what was paid - which is why a calendar (a net debit) and a long single leg
    can share the same expression.
    """
    pnl_pct = (mark - entry_debit) / abs(entry_debit) if entry_debit else 0.0

    if agent == "gamma_scout":
        if pnl_pct >= GAMMA_TAKE_PROFIT:
            return f"target +{pnl_pct:.0%} of premium"
        if pnl_pct <= GAMMA_STOP_LOSS:
            return f"stop {pnl_pct:.0%} of premium"
        if wall_strike and spot and abs(spot - wall_strike) / spot > GAMMA_WALL_INVALIDATION:
            return (f"thesis invalidated: spot {abs(spot - wall_strike) / spot:.1%} "
                    f"from wall {wall_strike}")
        if front_dte <= GAMMA_TIME_STOP_DTE:
            return f"time stop {front_dte} DTE"
    else:                                              # vol_surfer calendar
        # Reversion is the thesis being RIGHT, so it is checked before the stop.
        if live_zscore is not None and abs(live_zscore) < VOL_REVERSION_Z:
            return f"thesis realised: z reverted to {live_zscore:+.2f}"
        if pnl_pct <= VOL_STOP_LOSS:
            return "stop -100% of net debit"
        if front_dte <= VOL_TIME_STOP_DTE:
            return f"time stop {front_dte} DTE on front leg"

    if front_dte <= BLACKOUT_DTE:
        return "expiry-week close blackout"
    return None
