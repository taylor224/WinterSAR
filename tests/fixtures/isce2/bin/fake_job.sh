#!/bin/sh
# Stand-in for 'SentinelWrapper.py -c <config>' in the run_files fixtures.
# usage: fake_job.sh <config_name> [sleep_seconds] [exit_code]
name="$1"
sleep_s="${2:-0}"
rc="${3:-0}"
echo "fake_job start $name"
if [ "$sleep_s" != "0" ]; then sleep "$sleep_s"; fi
if [ -n "$WINTERSAR_FAKE_JOB_TOUCH" ]; then
  mkdir -p "$WINTERSAR_FAKE_JOB_TOUCH" && : > "$WINTERSAR_FAKE_JOB_TOUCH/$name"
fi
echo "fake_job end $name rc=$rc"
exit "$rc"
