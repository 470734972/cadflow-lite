import pytest

from app.collectors.lsf import CommandError, LsfCollector, ParseError, SafeRunner, parse_duration_seconds, parse_lmstat, parse_lshosts, parse_lsload, parse_pipe_table, parse_whitespace_table
from app.main import collection_failure_detail


def test_parse_pipe_table():
    text = "840101|alice|RUN|normal|compute01|8|12G|01:00:00|orion\n"
    columns = ["job_id", "user", "status", "queue", "exec_host", "slots", "max_mem", "runtime", "project"]
    rows = parse_pipe_table(text, columns)
    assert rows[0]["job_id"] == "840101"
    assert rows[0]["project"] == "orion"


def test_pipe_table_rejects_unexpected_lsf_format():
    with pytest.raises(ParseError, match="expected 2 columns"):
        parse_pipe_table("840101|alice|RUN\n", ["job_id", "user"])


def test_parse_pipe_table_allows_pipes_in_lsf_job_name():
    columns = ["job_id", "user", "status", "queue", "submit_host", "exec_host", "job_name", "submit_time", "slots", "max_mem", "runtime", "project"]
    text = "943700|jingbaoliu|RUN|int|lg9|rd11:rd11|pt_shell 2>&1 | tee pt_shell.log|Aug 20 17:24|8|65.3 Gbytes|434482|project-a\n"
    rows = parse_pipe_table(text, columns, embedded_delimiter_index=6)
    assert rows[0]["job_name"] == "pt_shell 2>&1 | tee pt_shell.log"
    assert rows[0]["project"] == "project-a"


def test_parse_legacy_whitespace_table():
    rows = parse_whitespace_table(
        "HOST_NAME STATUS JL/U MAX NJOBS RUN SSUSP USUSP RSV\ncompute01 ok - 64 8 8 0 0 0\n",
        {"HOST_NAME", "STATUS", "MAX", "RUN"},
    )
    assert rows == [{"HOST_NAME": "compute01", "STATUS": "ok", "JL/U": "-", "MAX": "64", "NJOBS": "8", "RUN": "8", "SSUSP": "0", "USUSP": "0", "RSV": "0"}]


def test_duration_and_lsload_parsing():
    assert parse_duration_seconds("01:02:03") == 3723
    assert parse_duration_seconds("2:01:02:03") == 176523
    assert parse_duration_seconds("-") == 0
    loads = parse_lsload(
        "HOST_NAME status r15s r1m r15m ut pg ls it tmp swp mem\n"
        "compute01 ok 0.1 0.2 0.3 72% 0 0 0 0 64G 128G\n"
    )
    assert loads == {"compute01": {"cpu_pct": 72, "load_1m": 0.2, "load_15m": 0.3, "free_mem_mb": 131072, "free_tmp_mb": 0, "free_swap_mb": 65536}}


def test_parse_lsload_explicit_delimiter_keeps_unknown_values():
    loads = parse_lsload(
        "HOST_NAME|status|r1m|r15m|ut|tmp|swp|mem\n"
        "lg12|ok|0.0|0.3|0%|128G|995.5G|-\n"
    )
    assert loads["lg12"] == {
        "cpu_pct": 0, "load_1m": 0, "load_15m": 0.3,
        "free_mem_mb": 0, "free_tmp_mb": 131072, "free_swap_mb": 1019392,
    }


def test_parse_lsload_legacy_short_row_is_not_dropped():
    loads = parse_lsload(
        "HOST_NAME status r15s r1m r15m ut pg ls it tmp swp mem\n"
        "lg12 ok 0.0 0.0 0.3 0% 0.0 0 186510648G 128G 995.5G\n"
    )
    assert loads["lg12"]["free_tmp_mb"] == 131072
    assert loads["lg12"]["free_swap_mb"] == 1019392
    assert loads["lg12"]["free_mem_mb"] == 0


def test_parse_lshosts_total_memory():
    capacities = parse_lshosts(
        "HOST_NAME type model cpuf ncpus maxmem maxswp maxtmp rexpri server RESOURCES\n"
        "compute01 X86_64 model 2.0 64 256G 128G 100G 0 1 -\n"
    )
    assert capacities == {"compute01": {"total_mem_mb": 262144}}


def test_parse_lmstat():
    text = "Users of VCS:  (Total of 120 licenses issued;  Total of 108 licenses in use)"
    rows = parse_lmstat(text, "27000@license01")
    assert rows == [{"server": "27000@license01", "vendor": "", "feature": "VCS", "total": 120, "used": 108, "expires_at": "", "status": "warning"}]


def test_parse_lmstat_includes_node_locked_inventory():
    rows = parse_lmstat("Users of amps:  (Uncounted, node-locked)", "27000@license01", "snpslmd,cdslmd")
    assert rows == [{"server": "27000@license01", "vendor": "snpslmd,cdslmd", "feature": "amps", "total": 0, "used": 0, "expires_at": "", "status": "node_locked"}]


def test_collection_failure_detail_identifies_failed_data_source():
    assert collection_failure_detail({"status": "error", "error": "required command is not executable: /path/to/lmstat"}) == {
        "component": "FlexNet License", "message": "required command is not executable: /path/to/lmstat",
    }
    assert collection_failure_detail({"status": "error", "error": "bhosts timed out"}) == {
        "component": "LSF 节点", "message": "bhosts timed out",
    }


def test_runner_rejects_non_allowlisted_command():
    try:
        SafeRunner().run(["rm", "-rf", "/tmp/example"])
    except Exception as exc:
        assert "allowlist" in str(exc)
    else:
        raise AssertionError("unsafe command was accepted")


def test_lsf_collector_uses_real_command_contract_without_inventing_requested_memory():
    class FakeRunner:
        def __init__(self):
            self.commands = []

        def check_available(self, commands):
            return {command: f"/path/to/lsf/bin/{command}" for command in commands}

        def run(self, argv):
            self.commands.append(argv)
            if argv[0] == "bjobs" and any(value.startswith("-p") for value in argv[1:]):
                return "2|bob|PEND|normal|login02|-|waiting_job|2026-07-22T10:31:00+00:00|1|-|-|-\n"
            outputs = {
                "bjobs": "1|alice|RUN|normal|login01|compute01|vcs_compile_top|2026-07-22T10:30:00+00:00|8|12G|01:00:00|orion\n",
                "bqueues": "QUEUE_NAME PRIO STATUS MAX JL/U JL/P JL/H NJOBS PEND RUN SUSP RSV\nnormal 30 Open:Active - - - - 11 3 8 0 0\n",
                "bhosts": "HOST_NAME STATUS JL/U MAX NJOBS RUN SSUSP USUSP RSV\ncompute01 ok - 64 8 8 0 0 0\n",
                "lsload": "HOST_NAME status r15s r1m r15m ut pg ls it tmp swp mem\ncompute01.eda.lan ok 0.1 0.2 0.3 72% 0 0 0 0 64G 128G\n",
                "lshosts": "HOST_NAME type model cpuf ncpus maxmem maxswp maxtmp rexpri server RESOURCES\ncompute01.eda.lan X86_64 model 2.0 64 256G 128G 100G 0 1 -\n",
                "lmstat": "Users of VCS:  (Total of 120 licenses issued;  Total of 108 licenses in use)\n",
            }
            return outputs[argv[0]]

    runner = FakeRunner()
    payload = LsfCollector(20, "/path/to/lmstat", ("27000@license-host",), license_vendor="snpslmd", runner=runner).collect()
    assert payload["jobs"][0]["runtime_seconds"] == 3600
    assert payload["jobs"][0]["requested_mem_mb"] == 0
    assert payload["jobs"][0]["submit_host"] == "login01"
    assert payload["jobs"][0]["job_name"] == "vcs_compile_top"
    assert any(job["status"] == "PEND" for job in payload["jobs"])
    assert payload["hosts"][0]["cpu_pct"] == 72
    assert payload["hosts"][0]["total_mem_mb"] == 262144
    assert payload["hosts"][0]["free_tmp_mb"] == 0
    assert payload["licenses"][0]["vendor"] == "snpslmd"
    assert ["bjobs", "-u", "all", "-a", "-noheader", "-o", "jobid user stat queue from_host exec_host job_name submit_time slots max_mem run_time proj_name delimiter='|'"] in runner.commands
    assert ["lsload", "-o", "HOST_NAME status r1m r15m ut tmp swp mem delimiter='|'"] in runner.commands


def test_lsf_collection_keeps_hosts_when_flexnet_is_unavailable():
    class BrokenLicenseRunner:
        def check_available(self, commands):
            if commands == ["lmstat"]:
                raise CommandError("required command is not executable: /path/to/lmstat")
            return {command: f"/path/to/lsf/bin/{command}" for command in commands}

        def run(self, argv):
            outputs = {
                "bjobs": "",
                "bqueues": "QUEUE_NAME PRIO STATUS MAX JL/U JL/P JL/H NJOBS PEND RUN SUSP RSV\nnormal 30 Open:Active - - - - 0 0 0 0 0\n",
                "bhosts": "HOST_NAME STATUS JL/U MAX NJOBS RUN SSUSP USUSP RSV\ncompute01 ok - 64 0 0 0 0 0\n",
                "lsload": "HOST_NAME status r15s r1m r15m ut pg ls it tmp swp mem\ncompute01 ok 0.1 0.2 0.3 2% 0 0 0 0 4G 8G\n",
            }
            return outputs[argv[0]]

    payload = LsfCollector(20, "/missing/lmstat", ("27000@license01",), runner=BrokenLicenseRunner()).collect()
    assert payload["hosts"][0]["name"] == "compute01"
    assert payload["licenses"] == []
    assert payload["_warnings"] == ["FlexNet License: required command is not executable: /path/to/lmstat"]
