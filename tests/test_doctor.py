"""`system-diff doctor` reports where a read's time goes, and what usually explains a slow or quiet setup."""
from __future__ import annotations

from system_diff import doctor


def test_doctor_times_a_look_and_checks_the_setup(repo, capsys):
    assert doctor.main(repo) == 0
    out = capsys.readouterr().out
    for said in ("system-diff ", "files: ", "snapshot of the working tree: ", "pane look 1: ", "pane look 2: ",
                 "hooks: ", "web page: ", "caches: "):
        assert said in out, said
