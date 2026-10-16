#!/bin/bash
# DouyinLiveRecorder "run a script after recording" hook (recorder on the same machine as LiveClip).
# Args: $1 anchor name, $2 recording file. Clipping takes a while, so it runs in the background and the recorder
# goes straight back to watching the room (otherwise a quick reconnect could be missed).
# Set LIVECLIP_HOME to your workspace (the folder with config.yaml) and make sure `liveclip` is on PATH.
cd "${LIVECLIP_HOME:?set LIVECLIP_HOME}" || exit 1
mkdir -p logs
nohup liveclip after-record "$2" >> logs/after_record.log 2>&1 < /dev/null &
exit 0
