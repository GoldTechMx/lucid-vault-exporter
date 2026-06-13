from lucid_vault_exporter.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def mono(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.slept.append(secs)
        self.now += secs


def make(per_5s=2):
    clk = FakeClock()
    rl = RateLimiter(
        budgets={"export": per_5s}, monotonic=clk.mono, sleep=clk.sleep
    )
    return rl, clk


def test_burst_within_budget_does_not_sleep():
    rl, clk = make(per_5s=3)
    for _ in range(3):
        rl.acquire("export")
    assert clk.slept == []


def test_exceeding_budget_sleeps_until_refill():
    rl, clk = make(per_5s=2)
    rl.acquire("export")
    rl.acquire("export")
    rl.acquire("export")  # third within the window must wait ~2.5s (refill 2/5s)
    assert sum(clk.slept) > 2.0


def test_note_throttled_pauses_budget():
    rl, clk = make(per_5s=100)
    rl.note_throttled("export", retry_after=30.0)
    rl.acquire("export")
    assert sum(clk.slept) >= 30.0


def test_unknown_budget_uses_default():
    rl, clk = make()
    rl.acquire("other")  # must not raise


def test_note_throttled_falls_back_to_60_for_nonpositive():
    rl, _ = make(per_5s=100)
    assert rl.note_throttled("export", 0) == 60.0
    assert rl.note_throttled("export", -5.0) == 60.0
    assert rl.note_throttled("export", None) == 60.0


def test_unknown_budget_uses_default_capacity():
    rl, clk = make()  # only "export" is configured; "other" uses default_per_5s=60
    for _ in range(60):
        rl.acquire("other")
    assert clk.slept == []  # 60 acquires fit within the default budget without sleeping
    rl.acquire("other")  # 61st exceeds default budget -> must sleep
    assert sum(clk.slept) > 0
