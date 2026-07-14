import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from delegate import MODELS

def test_cost_arithmetic_with_cache():
    # flash pricing: cin = 0.14, cout = 0.28, cin_cached = 0.014
    spec = MODELS["flash"]
    assert spec["cin"] == 0.14
    assert spec["cout"] == 0.28
    assert spec["cin_cached"] == 0.014

    pin, pout, cached = 1_000_000, 1_000_000, 500_000
    cost = ((pin - cached) / 1e6 * spec["cin"]) + (cached / 1e6 * spec["cin_cached"]) + (pout / 1e6 * spec["cout"])
    import math
    assert math.isclose(cost, 0.357, rel_tol=1e-9)

def test_cost_arithmetic_no_cache_regression():
    spec = MODELS["grok"]  # no cin_cached
    assert "cin_cached" not in spec
    pin, pout, cached = 1_000_000, 1_000_000, 0
    
    # Old formula: pin * cin + pout * cout
    old_cost = (pin / 1e6 * spec["cin"]) + (pout / 1e6 * spec["cout"])
    
    # New formula should be the same
    cached_clamped = min(cached, pin)
    cin_cached = spec.get("cin_cached", spec["cin"])
    new_cost = ((pin - cached_clamped) / 1e6 * spec["cin"]) + (cached_clamped / 1e6 * cin_cached) + (pout / 1e6 * spec["cout"])
    
    import math
    assert math.isclose(old_cost, new_cost, rel_tol=1e-9)

def test_cost_arithmetic_clamp_and_gemini():
    spec = MODELS["gemini"]
    # Free tier, should cost 0
    assert spec["cin"] == 0.0
    assert spec["cout"] == 0.0
    
    pin, pout, cached = 100, 200, 150 # cached > pin clamp
    cached_clamped = min(cached, pin)
    assert cached_clamped == 100
    
    cin_cached = spec.get("cin_cached", spec["cin"])
    cost = ((pin - cached_clamped) / 1e6 * spec["cin"]) + (cached_clamped / 1e6 * cin_cached) + (pout / 1e6 * spec["cout"])
    assert cost == 0.0

