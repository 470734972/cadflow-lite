from app.collectors.lsf import SafeRunner, parse_lmstat, parse_pipe_table


def test_parse_pipe_table():
    text = "840101|alice|RUN|normal|compute01|8|4G|12G|3600|orion\n"
    columns = ["job_id", "user", "status", "queue", "exec_host", "slots", "used_mem", "max_mem", "runtime", "project"]
    rows = parse_pipe_table(text, columns)
    assert rows[0]["job_id"] == "840101"
    assert rows[0]["project"] == "orion"


def test_parse_lmstat():
    text = "Users of VCS:  (Total of 120 licenses issued;  Total of 108 licenses in use)"
    rows = parse_lmstat(text, "27000@license01")
    assert rows == [{"server": "27000@license01", "vendor": "", "feature": "VCS", "total": 120, "used": 108, "expires_at": "", "status": "warning"}]


def test_runner_rejects_non_allowlisted_command():
    try:
        SafeRunner().run(["rm", "-rf", "/tmp/example"])
    except Exception as exc:
        assert "allowlist" in str(exc)
    else:
        raise AssertionError("unsafe command was accepted")

