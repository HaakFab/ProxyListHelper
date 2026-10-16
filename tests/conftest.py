"""Repo-wide pytest configuration.

This project deliberately splits into two dependency sets (requirements-web.txt
for the portal, requirements-worker.txt for the print pipeline). Running
pytest from either environment should skip the tests that need the OTHER
side's dependencies rather than error out during collection -- that's
expected, not a failure.

- Web venv (no numpy/torch/Pillow/reportlab/scipy): skips test_pipeline.py.
- Worker interpreter (no fastapi/sqlalchemy/argon2 -- it only ever runs
  moxfield_mpc_to_pdf.py, never the portal): skips every portal-backed test.

Run the full suite from whichever venv has both installed, or run each half
from its own environment: `~/miniconda3/bin/python -m pytest` for the pipeline
tests, the web venv for everything else.
"""
collect_ignore = []

try:
    import numpy  # noqa: F401
except ImportError:
    collect_ignore.append("test_pipeline.py")

try:
    import fastapi  # noqa: F401
    import sqlalchemy  # noqa: F401
except ImportError:
    collect_ignore.extend([
        "test_auth.py",
        "test_account_deletion_cascade.py",
        "test_admin_round_rates.py",
        "test_e2e_smoke.py",
        "test_orders_service.py",
        "test_jobs.py",
        "test_pricing.py",
        "test_portal_checking.py",
        "test_state_machine.py",
        "test_slugs.py",
    ])
