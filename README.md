# unraid-cache-puller

A Docker container for Unraid that keeps your **frequently used files on the
cache pool**, for shares whose primary storage is a pool and whose secondary
storage is the array.

Unraid's mover works by rule ("everything to the array" or "everything to the
cache"). This container works by use: it watches which files actually get
opened and moves the hot ones from the array to the share's pool, so
they load from the SSD and the array disks can stay spun down.

## How it works

1. **Tracking.** inotify watches every directory of each managed share on
   every array disk (`/mnt/diskN/<share>`) and on the pool
   (`/mnt/<pool>/<share>`). Every file open counts, including opens through
   `/mnt/user` (shfs opens the underlying file on the disk). Opens of the same
   file within 15 minutes count once. Each file gets a score that works
   like a count of accesses with a 72-hour half-life. Renames (e.g. by
   Sonarr/Radarr) carry the score over.
2. **Promoting.** Every hour, files with a score of at least `MIN_SCORE`
   (default 3) that are only on the array are moved to the share's pool,
   hottest first, while the pool stays under `CACHE_MAX_PERCENT` and above
   `CACHE_MIN_FREE`.
3. **Keeping them there.** For `yes` shares (mover: pool → array), the
   container writes a list of hot files on the pool to
   `/config/mover-ignore.txt`. You can plug this into the
   [Mover Tuning plugin](#mover-integration) so the mover leaves them alone.
4. **Making room.** If the pool goes over `CACHE_MAX_PERCENT`, files the
   container promoted earlier that have since gone cold are moved back to
   the array disk they came from.

### Which shares are managed

| Primary storage | Secondary storage | Mover action | Managed? |
|---|---|---|---|
| pool | array | pool → array (`yes`) | ✅ |
| pool | array | array → pool (`prefer`) | ✅ |
| pool | none (`only`) | – | ❌ |
| array (`no`) | none | – | ❌ |
| pool | another pool (Unraid 7) | – | ❌ |

You can narrow this down with `INCLUDE_SHARES`, `EXCLUDE_SHARES` and `SHARE_MODES`.

## Safety

Moving files around underneath a running server has to be boring and
predictable. The container:

- **Starts in dry-run mode.** Nothing is changed until you set `DRY_RUN=false`.
  In dry-run the log (and `cache-puller status`) shows what would happen.
- **Never deletes the original before the copy is verified.** Each move copies
  into a hidden temp file next to the destination, fsyncs it, drops it from
  the page cache and re-reads it from disk to compare a BLAKE2 checksum. Only then
  is it published with `link()` (which fails instead of overwriting anything),
  and only then is the original deleted. There is never a moment where the
  file exists in neither place.
- **Aborts if the file is touched.** The source is checked for changes (inode,
  size, mtime, ctime) and for open handles (any process on the host, including
  shfs and Plex) before copying, after copying, and right before deleting. If
  anything changed, the copy is removed and the original stays.
- **Waits for the right moment.** Nothing moves while the array isn't started,
  during a parity check/rebuild, while the mover is running (pid file or
  `mover` / `age_mover` process), or outside `ALLOWED_HOURS` if you set it. These
  checks run before every single file.
- **Leaves tricky files alone.** It skips hard-linked files (common with *arr
  setups; moving would break the link and double the used space), symlinks,
  files that exist on more than one disk, files modified in the last hour,
  temp/partial downloads (`EXCLUDE_PATTERNS`) and files that already exist on
  the pool.
- **Keeps metadata.** Owner, permissions, timestamps and extended attributes
  are copied. Missing parent directories are created with the owner and mode of
  their array counterparts. On ZFS pools it won't create a missing share
  directory (Unraid creates datasets for those).
- **Recovers from crashes.** The temp file of the transfer in progress is
  recorded in a journal and removed on the next start. A crash at the worst
  possible moment leaves the file on both the pool and the array with identical
  content, and shfs serves the pool copy.
- **Only moves back what it moved.** Pressure relief (demotion) only touches files
  this container promoted and that have gone cold, never your other files on
  the pool.
- **Needs no network.** The template sets `--network=none`.

## Installation

### Unraid template

Copy `unraid/cache-puller.xml` to
`/boot/config/plugins/dockerMan/templates-user/my-cache-puller.xml`, then in
the Docker tab choose **Add Container** → template **cache-puller**.

### Manual `docker run`

```sh
docker run -d --name cache-puller \
  --pid=host --cap-add=SYS_PTRACE --network=none \
  -v /mnt:/mnt:rw,slave \
  -v /mnt/user/appdata/cache-puller:/config \
  -v /boot/config/shares:/boot/config/shares:ro \
  -v /var/local/emhttp:/var/local/emhttp:ro \
  -e DRY_RUN=true \
  ghcr.io/cnoblereimer/unraid-cache-puller:latest
```

Why each piece is needed:

- `--pid=host --cap-add=SYS_PTRACE`: to see which files are open on the host and
  whether the mover is running. Without it the container refuses to move
  anything (unless you set `OPEN_FILE_CHECK=auto` or `off`, which isn't recommended).
- `/mnt:/mnt:rw,slave`: must be `/mnt` on both sides so the paths in the mover
  ignore list are valid on the host; `slave` makes disks mounted after the
  container started (array stop/start) visible.
- `/boot/config/shares` and `/var/local/emhttp` (read-only): share settings and
  array/parity state.

To build the image yourself: `docker build -t unraid-cache-puller .`

### First run

```sh
docker exec cache-puller cache-puller check    # is everything visible and safe?
docker logs -f cache-puller                    # what would be moved (dry run)
docker exec cache-puller cache-puller status   # hottest files, recent actions
```

The container knows nothing about past usage when it first starts, so it
needs to watch for a while (a few days is best) before it has a useful picture.
Once the plan in the log looks right, set `DRY_RUN=false`.

`docker exec cache-puller cache-puller once` runs a single cycle right away.

## Mover integration

For shares set to **yes** (mover: pool → array), the stock mover moves
promoted files back to the array on its next run. To stop that:

1. Install **Mover Tuning** from Community Applications.
2. Under *Settings → Scheduler → Mover Tuning*, enable **Ignore files listed
   inside of a text file** and point it at
   `/mnt/user/appdata/cache-puller/mover-ignore.txt`.

The list holds the hot files currently on the pool (`/mnt/cache/<share>/...`
paths; set `MOVER_IGNORE_STYLE=user` or `both` if your mover setup expects
`/mnt/user/...` paths). It's rewritten after every cycle, so files that cool
down drop off the list and the mover moves them to the array normally.

Shares set to **prefer** don't need this: the mover moves files to the pool
anyway, and this container just makes sure the hot ones get there first.

## Configuration

All settings are environment variables. Sizes accept `K`, `M`, `G`, `T`
suffixes (powers of 1024).

| Variable | Default | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | Only log what would happen. |
| `INCLUDE_SHARES` | *(all)* | Comma separated shares to manage. |
| `EXCLUDE_SHARES` | | Shares never to touch. |
| `SHARE_MODES` | `yes,prefer` | Which mover directions to manage. |
| `MIN_SCORE` | `3` | Score needed to be promoted (≈ recent accesses). |
| `HALF_LIFE_HOURS` | `72` | How fast old accesses stop counting. |
| `ACCESS_DEBOUNCE_MINUTES` | `15` | Repeated opens within this window count once. |
| `CACHE_MAX_PERCENT` | `80` | Never fill a pool above this; above it, cold promoted files are moved back. |
| `CACHE_MIN_FREE` | `50G` | Always keep this much free on the pool (the share's *minimum free space* is honoured as well). |
| `MAX_BYTES_PER_RUN` | `200G` | Data moved per cycle at most. |
| `MAX_FILES_PER_RUN` | `1000` | Files moved per cycle at most. |
| `MIN_FILE_SIZE` / `MAX_FILE_SIZE` | `0` / `0` | Size filters (`0` max = unlimited). |
| `MIN_FILE_AGE_MINUTES` | `60` | Skip files modified more recently than this. |
| `EXCLUDE_PATTERNS` | | Globs matched against the path inside the share and the file name, e.g. `*.part,downloads/*`. |
| `RUN_INTERVAL_MINUTES` | `60` | How often to move files. |
| `ALLOWED_HOURS` | *(any)* | e.g. `1-6` or `22-5`: only move files during these hours. |
| `DEMOTE_ON_PRESSURE` | `true` | Move cold promoted files back when the pool is over the limit. |
| `VERIFY` | `hash` | `hash` = checksum re-read; `size` = size only (faster). |
| `OPEN_FILE_CHECK` | `on` | `on` = refuse to move without host process visibility; `auto` = check if possible; `off`. |
| `SKIP_DURING_PARITY` | `true` | Don't move during parity check/rebuild. |
| `REQUIRE_ARRAY_STATE` | `true` | Don't move if the array state can't be read. |
| `MOVER_IGNORE_FILE` | `/config/mover-ignore.txt` | Empty disables it. |
| `MOVER_IGNORE_STYLE` | `pool` | `pool`, `user` or `both` path style. |
| `MOVER_PID_FILES` | `/proc/1/root/var/run/mover.pid` | Pid files that mean "mover running". |
| `MOVER_PROCESS_NAMES` | `mover,age_mover` | Process names that mean "mover running". |
| `LOG_LEVEL` | `INFO` | |

## Notes and limitations

- **inotify watch limit.** One watch per directory is needed. `cache-puller
  check` shows the current limit; raise it on the host if the log says it was
  reached, e.g. with the *Tips and Tweaks* plugin or
  `sysctl fs.inotify.max_user_watches=1048576`.
- **Startup scan.** On start the container walks the share directories on each
  disk to set up watches. This reads directory metadata and may spin up disks
  once.
- Files are moved one at a time; a cycle doesn't hold up the mover for longer
  than the current file (the mover check runs before each file).
- Demoted files go back to the disk they came from if it still has room,
  otherwise to the allowed disk with the most free space that already has the
  share directory. Split level and allocation method aren't applied.
- State lives in `/config/cache-puller.db` (SQLite).

## Development

```sh
pip install pytest
python -m pytest
```

The code is plain Python 3.11+ with no runtime dependencies:

| Module | Purpose |
|---|---|
| `config.py` | environment variables |
| `unraid.py` | share configs, disks, pools, array state |
| `tracker.py`, `inotify.py` | access tracking |
| `db.py` | scores, promoted files, history |
| `safety.py` | open-file and mover checks |
| `transfer.py` | the verified move |
| `service.py` | planning and running a cycle |
| `cli.py` | `run`, `once`, `check`, `status` |
