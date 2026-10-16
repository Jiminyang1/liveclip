# Deployment: recording server + live-follow

```
recording server (24/7)                         editing machine (Mac or Linux)
DouyinLiveRecorder ─ 5-min .ts segments ─┐
uploader.py (systemd timer, every minute) ├─ rclone ─▶ remote ◀─ rclone ─ liveclip follow ─▶ clips, dashboard
                                          └─ ENDED marker when the session ends
```

Any rclone backend works (Google Drive, S3, R2, WebDAV, even an SFTP folder). The editing machine may sleep; segments
wait on the remote and `liveclip follow` catches up when it runs again.

## Recording server

1. A small Linux box (2 vCPU / 1 GB was enough for one or two rooms in source quality). Whether a server outside
   mainland China receives the original-quality stream depends on the platform and the room — test first.
2. Install [DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder) (MIT) from upstream into
   `/opt/liveclip/DouyinLiveRecorder` with its own venv `/opt/liveclip/venv`. Follow its README for cookies
   (some platforms need a logged-in cookie; keep it on the server only) and for the Node.js runtime it may install.
3. In its `config/config.ini` set (Chinese keys as in upstream):

   | key | value | why |
   |---|---|---|
   | 直播保存路径 | `/opt/liveclip/recordings` | where uploader.py looks |
   | 保存文件夹是否以作者区分 | 是 | one folder per anchor |
   | 视频保存格式 | ts | segments stay playable while recording |
   | 原画\|超清\|… | 原画 | source quality |
   | 分段录制是否开启 | 是 | live-follow needs segments |
   | 视频分段时间(秒) | 300 | 5-minute segments |
   | 录制完成后自动转为mp4格式 | 否 | LiveClip remuxes itself |
   | 循环时间(秒) | 60 | how often rooms are checked |

   Rooms go into `config/URL_config.ini` — see `vps/URL_config.example.ini`. The anchor name there becomes the
   segment prefix; list it in `live.anchors` of your LiveClip config to give sessions a short key.
4. `rclone config` a remote (for Google Drive, create your own OAuth client ID instead of rclone's shared one —
   the shared one is rate-limited and being retired). Test with `rclone lsd myremote:`.
5. Copy `vps/uploader.py` to `/opt/liveclip/uploader.py`, the units to `/etc/systemd/system/`, set
   `LIVECLIP_REMOTE` in `liveclip-uploader.service`, then:
   ```bash
   systemctl daemon-reload
   systemctl enable --now liveclip-recorder.service liveclip-uploader.timer
   ```
6. Optional: let the dashboard show the server — set `recorder.ssh_host` (an alias from `~/.ssh/config` with key auth)
   and the log paths in config.yaml.

## Editing machine

```bash
liveclip follow --keep-awake          # uses live.remote from config.yaml
```
Run it under `nohup`, `tmux`, launchd or a systemd user service. Test the whole chain without a live stream:
```bash
liveclip sim-live videos/replay.mp4 --minutes 30 --every 20 --remote /tmp/livesim   # a local dir works as a "remote"
liveclip follow --remote /tmp/livesim
```

## Recorder on the same machine (no server)

Point DouyinLiveRecorder's "自定义脚本执行命令" at `deploy/local/on_recorded.sh` (set `LIVECLIP_HOME`). Each finished
recording is clipped with `liveclip after-record` — simpler, but clips only appear after the stream ends.
