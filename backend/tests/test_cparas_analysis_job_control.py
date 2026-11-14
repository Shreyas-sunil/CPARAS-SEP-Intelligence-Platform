"""
Unit tests for CPARAS's v0.6 start/stop/pause/continue cooperative-stop
primitives (api/routes/cparas/analysis.py) -- the low-level mechanism that
POST /jobs/{job_id}/pause and /stop use to signal an in-flight POST
/analyze call, and that _process_one_chunk / _process_one_batch check
between units of work.

Exercised directly (not through the full /analyze endpoint, which needs a
real LLM backend) -- these tests cover exactly the part that's new and
backend-agnostic: registering a job, requesting a stop, observing it, and
cleaning up.

Run: python -m pytest backend/tests/test_cparas_analysis_job_control.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.routes.cparas import analysis as an  # noqa: E402


def test_unregistered_job_stop_request_is_rejected():
    assert an.request_analysis_stop("no-such-job") is False
    assert an.is_analysis_stop_requested("no-such-job") is False
    assert an.is_analysis_job_running("no-such-job") is False


def test_register_stop_observe_and_unregister():
    job_id = "test-job-1"
    an._register_job(job_id)
    try:
        assert an.is_analysis_job_running(job_id) is True
        assert an.is_analysis_stop_requested(job_id) is False

        accepted = an.request_analysis_stop(job_id)
        assert accepted is True
        assert an.is_analysis_stop_requested(job_id) is True
    finally:
        an._unregister_job(job_id)

    # Once unregistered, the job "doesn't exist" from the control
    # mechanism's point of view -- a stale stop request must not linger
    # and must not be mistaken for one still running.
    assert an.is_analysis_job_running(job_id) is False
    assert an.is_analysis_stop_requested(job_id) is False
    assert an.request_analysis_stop(job_id) is False


def test_none_job_id_never_registers_or_stops():
    # /analyze's job_id is optional -- every control primitive must be a
    # safe no-op when it's None, never raise or accidentally match some
    # other job.
    an._register_job(None)
    assert an.is_analysis_stop_requested(None) is False
    an._unregister_job(None)  # must not raise


def test_two_jobs_are_independent():
    an._register_job("job-a")
    an._register_job("job-b")
    try:
        an.request_analysis_stop("job-a")
        assert an.is_analysis_stop_requested("job-a") is True
        assert an.is_analysis_stop_requested("job-b") is False
    finally:
        an._unregister_job("job-a")
        an._unregister_job("job-b")
