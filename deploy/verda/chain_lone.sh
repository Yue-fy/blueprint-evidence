#!/usr/bin/env bash
# ON the instance: files-alone run (solver upload on neutral copies), then the path-based control (pipeline, same rows
# and shards, original dataset paths). Same commit, same vLLM server, so the difference = the folder/sibling hints.
cd /root
SOLVER=upload SPLIT=lone SHARDS=256 PROCS=${PROCS:-24} WORKERS=${WORKERS:-6} TAG=lone bash /root/ship/run_full.sh > /root/work/run-lone.log 2>&1
SOLVER=pipeline SPLIT=same SHARDS=256 PROCS=${PROCS:-24} WORKERS=${WORKERS:-6} TAG=same bash /root/ship/run_full.sh > /root/work/run-same.log 2>&1
