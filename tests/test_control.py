import threading
import time

from lucid_vault_exporter.control import Cancelled, Control


def test_checkpoint_passes_when_idle():
    Control().checkpoint()  # no pause, no cancel -> returns immediately


def test_cancel_raises_at_checkpoint():
    c = Control()
    c.cancel()
    assert c.is_cancelled
    try:
        c.checkpoint()
        raise AssertionError("expected Cancelled")
    except Cancelled:
        pass


def test_pause_blocks_until_resume():
    c = Control(poll_seconds=0.01)
    c.pause()
    assert c.is_paused
    released = []

    def worker():
        c.checkpoint()
        released.append(True)

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.05)
    assert released == []  # still parked in checkpoint
    c.resume()
    t.join(timeout=1.0)
    assert released == [True]


def test_cancel_unblocks_a_paused_checkpoint():
    c = Control(poll_seconds=0.01)
    c.pause()
    raised = []

    def worker():
        try:
            c.checkpoint()
        except Cancelled:
            raised.append(True)

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.05)
    c.cancel()
    t.join(timeout=1.0)
    assert raised == [True]
