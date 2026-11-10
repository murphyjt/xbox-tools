# xbox-tools

Command-line tools for managing games on a softmodded original Xbox over FTP,
using the FTP server built into the UnleashX dashboard.

## Requirements

- Python 3.10 or newer (tested with 3.12 and 3.14). No extra packages.
- An Xbox running UnleashX with FTP enabled, including anonymous login
  (used for read-only checks; uploads and deletes ask for your FTP login).
- For `xbox-stage` only: [extract-xiso](https://github.com/XboxDev/extract-xiso/releases),
  on your PATH or unzipped to `tools/extract-xiso/artifacts/`.

The tools default to the Xbox at `192.168.137.2`. Use `--ip` to change it.

## Install

```
pip install git+https://github.com/murphyjt/xbox-tools
```

Or from a clone, so your edits take effect right away:

```
pip install -e .
```

## Commands

| Command | Does |
| --- | --- |
| `xbox-push` | Uploads a game from a disc image (`.iso` / `.xiso`) or a staging folder. Skips files already on the Xbox, checks free space before starting, and resumes if interrupted. |
| `xbox-remove` | Lists the games on the Xbox and deletes the one you pick. |
| `xbox-stage` | Extracts a disc image into a staging folder in the current folder. |
| `xbox-info` | Shows what's in a disc image: title, region, size, files. |

Run any command with `-h` for its options. Without installing, run the scripts
directly instead, e.g. `python push_stage.py`.

`push-480i-config.sh` turns UnleashX's 480p setting off or back on. It's a Bash
script (use Git Bash on Windows) and isn't installed as a command.

## Examples

```
xbox-info "Game (USA).iso"
xbox-push "Game (USA).iso" --dry-run
xbox-push "Game (USA).iso"
xbox-remove
```

Games go to `E:\Games\<title>` by default. A staging folder mirrors the Xbox's
drives, so `my-stage/E/Games/Game/...` uploads to `E:\Games\Game\...`.

Only use these with disc images of games you own.

## Tests

```
python -m unittest discover -s tests
```

The tests use a fake FTP server and generated disc images, so they don't need
an Xbox or any game files.

## License

MIT
