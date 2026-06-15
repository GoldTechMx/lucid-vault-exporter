import logging

from lucid_vault_exporter.jobs import (
    JobLogHandler,
    JobRegistry,
    RedactionFilter,
    scrub_text,
)


def test_scrub_text_masks_secrets():
    assert scrub_text("token=abc123 ok", ["abc123"]) == "token=*** ok"
    assert scrub_text("nothing", []) == "nothing"


def test_redaction_filter_scrubs_record_message():
    f = RedactionFilter()
    f.set_secrets(["s3cr3t"])
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "leak s3cr3t here", None, None)
    f.filter(rec)
    assert "s3cr3t" not in rec.getMessage()


def test_registry_create_and_get():
    reg = JobRegistry()
    job = reg.create("export")
    assert reg.get(job.id) is job
    assert reg.get("nope") is None
    assert job.status == "running"


def test_job_progress_resets_phase_clock():
    reg = JobRegistry()
    job = reg.create("export")
    job.started = job.phase_started = 100.0
    job.progress("API", 3, 10, "doc")
    assert job.phase == "API" and job.done == 3 and job.total == 10 and job.detail == "doc"
    assert job.phase_started != 100.0  # changed to a real monotonic value on phase switch
    # a same-phase progress must NOT reset the phase clock
    same = job.phase_started
    job.progress("API", 5, 10, "doc2")
    assert job.phase_started == same and job.done == 5


def test_redaction_filter_scrubs_string_args():
    f = RedactionFilter()
    f.set_secrets(["t0pSecret"])
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "value=%s end", ("t0pSecret",), None)
    f.filter(rec)
    assert "t0pSecret" not in rec.getMessage()
    assert rec.getMessage() == "value=*** end"


def test_job_progress_total_none_becomes_zero():
    reg = JobRegistry()
    job = reg.create("export")
    job.progress("scan", 0, None, "x")
    assert job.total == 0


def test_log_handler_routes_to_active_job_and_bounds_buffer():
    reg = JobRegistry()
    job = reg.create("export")
    reg.active = job
    h = JobLogHandler(reg)
    h.setFormatter(logging.Formatter("%(message)s"))
    for i in range(1050):
        h.emit(logging.LogRecord("n", logging.INFO, __file__, 1, f"line{i}", None, None))
    assert len(job.logs) == 1000  # bounded
    assert job.log_dropped == 50  # remembered how many were trimmed
    assert job.logs[-1] == "line1049"


def test_log_handler_no_active_job_is_noop():
    reg = JobRegistry()
    h = JobLogHandler(reg)
    h.emit(logging.LogRecord("n", logging.INFO, __file__, 1, "x", None, None))  # must not raise
