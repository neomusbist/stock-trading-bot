from datetime import datetime, timedelta, timezone
import unittest

from automation.capital import Costs, entry_economics, entry_quantity, signal_economics
from automation.decisions import safe_values
from automation.exit_plan import invalidation_lines
from automation.policy import AutomationConfig, HARD_LOCKS
from automation.structural_breakout import structural_breakout
from core.settings import Config as Settings
from core.util import floor_tick, tick_size

KST = timezone(timedelta(hours=9))


def _make_reference_bars(asof: datetime, support: float, breakout: float, noise_span: float, overhead_peak: float | None = None):
    """Build deterministic 1-minute completed bars ending before `asof`."""
    bars = []
    start = asof.replace(second=0, microsecond=0) - timedelta(minutes=15)
    for i in range(15):
        t = start + timedelta(minutes=i)
        if i == 3 and overhead_peak is not None:
            # Confirmed swing peak well before the base
            lo = overhead_peak - noise_span
            hi = overhead_peak
            cl = overhead_peak - tick_size(overhead_peak)
        elif i == 2 and overhead_peak is not None:
            lo = breakout
            hi = overhead_peak - tick_size(overhead_peak)
            cl = hi - tick_size(hi)
        elif i == 4 and overhead_peak is not None:
            lo = breakout
            hi = overhead_peak - tick_size(overhead_peak)
            cl = lo + tick_size(lo)
        elif i == 8:
            # Anchor swing peak <= breakout
            hi = breakout
            lo = breakout - noise_span
            cl = breakout - tick_size(breakout)
        elif i == 7 or i == 9:
            hi = breakout - tick_size(breakout)
            lo = hi - noise_span
            cl = lo + tick_size(lo)
        elif i == 11:
            # Support touch bar
            lo = support
            hi = support + noise_span
            cl = support + tick_size(support)
        elif i >= 12:
            # Rising confirmation bars above support and touch high
            lo = support + tick_size(support) * (i - 10)
            hi = lo + noise_span
            cl = max(support + noise_span + tick_size(support), hi - tick_size(hi))
        else:
            lo = support + tick_size(support) * 2
            hi = lo + noise_span
            cl = lo + tick_size(lo)
        bars.append({
            "time": t,
            "open": float(lo),
            "high": float(hi),
            "low": float(lo),
            "close": float(cl),
            "volume": 50000.0 + i * 1000.0,
        })
    return bars


def test_four_stop_and_risk_prices_consistency():
    """Verify the 4 distinct stop/risk prices and their consistency between entry and exit."""
    cfg = Settings(live_mode=False)
    auto = AutomationConfig(confirmed_invalidation_enabled=True, structural_breakout_enabled=True)
    asof = datetime(2026, 11, 2, 9, 25, 50, tzinfo=KST)

    # GS건설 (006360) @ 09:25:50 exact geometry:
    # breakout=28,050, entry=28,100, support=27,600, structural_stop=27,550, emergency_stop=27,300
    bars = _make_reference_bars(asof, support=27600, breakout=28050, noise_span=250)
    setup = {
        "pattern": "A",
        "breakout": 28050,
        "support": 27600,
        "projection": 28950,
        "entry_basis": "PRIOR_HIGH_BREAKOUT",
    }
    passed, ev = structural_breakout(bars, asof, setup, cfg, entry=28100, bid=28050, ask=28100, auto=auto)
    assert passed is True

    # 1. Structural invalidation line (raw pattern support)
    assert ev["support"] == 27600
    # 2. Planned candle-close exit line (structural_stop = floor_tick(support - buffer))
    assert ev["structural_stop"] == 27550
    # 3. Intrabar catastrophic line (emergency_stop = floor_tick(structural_stop - noise_band))
    assert ev["emergency_stop"] == 27300

    # Verify exit_plan.invalidation_lines maps active_structure -> 27,550 and emergency -> 27,300
    pos = {
        "avg": 28100,
        "stop": ev["emergency_stop"],
        "initial_stop": ev["emergency_stop"],
        "signal": {"pattern": "A", "invalidation_policy": {"enabled": True, **ev}},
    }
    lines = invalidation_lines(cfg, auto, pos)
    assert lines["close_based"] is True
    assert lines["active_structure"] == 27550
    assert lines["emergency"] == 27300

    # 4. Quantity-sizing risk price (risk_per_share uses emergency_stop + round-trip fees/tax/slippage reserves)
    sig = {"pattern": "A", "limit": 28100, "stop": 27300, "target": 28950, "invalidation_policy": {"enabled": True, **ev}}
    econ = signal_economics(cfg, auto, sig)
    assert round(econ["risk_per_share"], 2) == 1001.01
    assert round(econ["gross_rr"], 4) == 1.0625
    assert round(econ["net_rr"], 3) == 0.640
    assert econ["rr_pass"] is False


def test_focus_symbols_1x_vs_2x_vs_confirmed_resistance():
    """Compare 1x projection, 2x projection, and confirmed resistance on 047040, 006360, 009830."""
    cfg = Settings(live_mode=False)
    costs = Costs(0.00015, 0.00015, 0.0020, cfg.entry_slippage_fraction, cfg.exit_slippage_fraction)

    # 1) 047040 대우건설: breakout=11,400, entry=11,430, support=11,140, s_stop=11,120, e_stop=11,050
    ec_047040_1x = entry_economics(11430, 11120, 11050, 11920, costs, 1.5)
    ec_047040_2x = entry_economics(11430, 11120, 11050, 12440, costs, 1.5)
    ec_047040_res = entry_economics(11430, 11120, 11050, 13550, costs, 1.5)
    assert round(ec_047040_1x["net_rr"], 3) == 0.876 and ec_047040_1x["rr_pass"] is False
    assert round(ec_047040_2x["net_rr"], 3) == 1.997 and ec_047040_2x["rr_pass"] is True
    assert round(ec_047040_res["net_rr"], 3) == 4.389 and ec_047040_res["rr_pass"] is True

    # 2) 006360 GS건설: breakout=28,050, entry=28,100, support=27,600, s_stop=27,550, e_stop=27,300
    ec_006360_1x = entry_economics(28100, 27550, 27300, 28950, costs, 1.5)
    ec_006360_2x = entry_economics(28100, 27550, 27300, 29850, costs, 1.5)
    ec_006360_res = entry_economics(28100, 27550, 27300, 30800, costs, 1.5)
    assert round(ec_006360_1x["net_rr"], 3) == 0.640 and ec_006360_1x["rr_pass"] is False
    assert round(ec_006360_2x["net_rr"], 3) == 1.534 and ec_006360_2x["rr_pass"] is True
    assert round(ec_006360_res["net_rr"], 3) == 2.478 and ec_006360_res["rr_pass"] is True

    # 3) 009830 한화솔루션: breakout=22,750, entry=22,900, support=22,450, s_stop=22,400, e_stop=22,200
    ec_009830_1x = entry_economics(22900, 22400, 22200, 23600, costs, 1.5)
    ec_009830_2x = entry_economics(22900, 22400, 22200, 23950, costs, 1.5)
    ec_009830_res = entry_economics(22900, 22400, 22200, 24382, costs, 1.5)
    assert round(ec_009830_1x["gross_rr"], 3) == 1.000
    assert round(ec_009830_1x["net_rr"], 3) == 0.613 and ec_009830_1x["rr_pass"] is False
    assert round(ec_009830_2x["net_rr"], 3) == 1.016 and ec_009830_2x["rr_pass"] is False
    assert round(ec_009830_res["net_rr"], 3) == 1.514 and ec_009830_res["rr_pass"] is True


def test_paper_rr_alignment_preserves_risk_invariants_and_blocks_live():
    """Verify PAPER-only Setup A alignment preserves net RR >= 1.5, 2% risk, 4% daily risk, HARD_LOCK, and blocks LIVE."""
    from automation.decisions import check_experiment_consistency
    cfg_paper = Settings(live_mode=False, risk_fraction=0.02, max_daily_loss_fraction=0.04)
    auto_paper = AutomationConfig(
        confirmed_invalidation_enabled=True,
        structural_breakout_enabled=True,
        setup_a_paper_rr_alignment_enabled=True,
        setup_a_target_mode="PROJECTION_2X",
        setup_a_rr_stop_basis="STRUCTURAL_STOP",
    )
    auto_paper.validate()
    assert check_experiment_consistency(cfg_paper) is True

    # Check risk invariants
    assert cfg_paper.min_reward_risk == 1.5
    assert cfg_paper.risk_fraction == 0.02
    assert cfg_paper.max_daily_loss_fraction == 0.04
    assert HARD_LOCKS == {"005930", "017900", "052260"}

    # Even when setup_a_rr_stop_basis == 'STRUCTURAL_STOP', risk_per_share for sizing MUST use emergency_stop
    asof = datetime(2026, 11, 2, 9, 25, 50, tzinfo=KST)
    bars = _make_reference_bars(asof, support=27600, breakout=28050, noise_span=250, overhead_peak=30200)
    setup = {
        "pattern": "A",
        "breakout": 28050,
        "support": 28000,  # Narrow micro_support (simulating micro-vs-macro base mismatch)
        "projection": 28150,
        "entry_basis": "PRIOR_HIGH_BREAKOUT",
    }
    passed, ev = structural_breakout(bars, asof, setup, cfg_paper, entry=28100, bid=28050, ask=28100, auto=auto_paper)
    assert passed is True
    assert ev["projection_1x"] == 28950
    assert ev["projection_2x"] == 29850
    assert ev["target_ceiling"] == 29850  # Capped by min(29850, overhead_peak=30200)

    sig = {"pattern": "A", "limit": 28100, "stop": ev["emergency_stop"], "target": ev["target_ceiling"], "invalidation_policy": {"enabled": True, **ev}}
    econ = signal_economics(cfg_paper, auto_paper, sig)
    # Position sizing risk_per_share is strictly emergency_stop-based (1001.01), NOT structural_stop-based (752.30)
    assert round(econ["risk_per_share"], 2) == 1001.01
    assert round(econ["planned_exit_risk_per_share"], 2) == 752.30
    qty = entry_quantity(econ, cash_budget=2_000_000, risk_budget=40_000)  # 2% of 2,000,000 = 40,000 KRW
    assert qty["risk_quantity"] == 39  # floor(40,000 / 1001.01) = 39 shares

    # When live_mode=True, structural_breakout and signal_economics ignore setup_a_paper_rr_alignment_enabled
    cfg_live = Settings(live_mode=True)
    _, ev_live = structural_breakout(bars, asof, setup, cfg_live, entry=28100, bid=28050, ask=28100, auto=auto_paper)
    assert ev_live["projection"] == 28950
    assert "projection_2x" not in ev_live


def test_safe_values_serializes_datetime():
    """Verify safe_values converts datetime objects to ISO strings so json.dumps never fails."""
    dt = datetime(2026, 11, 2, 9, 10, 0, tzinfo=KST)
    res = safe_values({"bar_time": dt, "nested": [dt]})
    assert res == {"bar_time": dt.isoformat(), "nested": [dt.isoformat()]}


def test_paper_expansion_guard_rejects_weak_or_overextended_bases():
    """Verify weak 1x bases (034020) and over-extended wide bases (005880) are blocked from 2x/resistance expansion."""
    cfg_paper = Settings(live_mode=False)
    auto_paper = AutomationConfig(
        confirmed_invalidation_enabled=True,
        structural_breakout_enabled=True,
        setup_a_paper_rr_alignment_enabled=True,
        setup_a_target_mode="PROJECTION_2X",
        setup_a_rr_stop_basis="EMERGENCY_STOP",
    )
    asof = datetime(2026, 11, 2, 9, 25, 50, tzinfo=KST)

    # 1) Weak base (034020-like: support=54,600, breakout=54,950, entry=55,200 -> base_gross_rr_1x = 0.5625 < 1.40)
    bars_weak = _make_reference_bars(asof, support=54600, breakout=54950, noise_span=400, overhead_peak=62200)
    setup_weak = {"pattern": "A", "breakout": 54950, "support": 54600, "projection": 55650, "entry_basis": "PRIOR_HIGH_BREAKOUT"}
    passed_w, ev_w = structural_breakout(bars_weak, asof, setup_weak, cfg_paper, entry=55200, bid=55100, ask=55200, auto=auto_paper)
    assert passed_w is True
    assert ev_w["expansion_guard_pass"] is False
    assert ev_w["projection"] == ev_w["projection_1x"] == 55650

    # 2) Over-extended wide base (005880-like: support=1,949, breakout=2,040 -> base_range_pct = 4.67% > 2.5%)
    bars_wide = _make_reference_bars(asof, support=1949, breakout=2040, noise_span=15, overhead_peak=2400)
    setup_wide = {"pattern": "A", "breakout": 2040, "support": 1949, "projection": 2222, "entry_basis": "PRIOR_HIGH_BREAKOUT"}
    passed_wide, ev_wide = structural_breakout(bars_wide, asof, setup_wide, cfg_paper, entry=2045, bid=2040, ask=2045, auto=auto_paper)
    assert passed_wide is True
    assert ev_wide["expansion_guard_pass"] is False
    assert ev_wide["projection"] == ev_wide["projection_1x"] == 2222


class SetupARRStructureResearchTests(unittest.TestCase):
    def test_four_stop_and_risk_prices_consistency(self):
        test_four_stop_and_risk_prices_consistency()

    def test_focus_symbols_1x_vs_2x_vs_confirmed_resistance(self):
        test_focus_symbols_1x_vs_2x_vs_confirmed_resistance()

    def test_paper_rr_alignment_preserves_risk_invariants_and_blocks_live(self):
        test_paper_rr_alignment_preserves_risk_invariants_and_blocks_live()

    def test_safe_values_serializes_datetime(self):
        test_safe_values_serializes_datetime()

    def test_paper_expansion_guard_rejects_weak_or_overextended_bases(self):
        test_paper_expansion_guard_rejects_weak_or_overextended_bases()


if __name__ == "__main__":
    unittest.main()
