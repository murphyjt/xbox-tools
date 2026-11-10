# xbox-tools

Scripts for managing games on a softmodded original Xbox over FTP, using the
FTP server built into the UnleashX dashboard.

## Requirements

- Python 3 (tested with 3.12 and 3.14). No extra packages.
- An Xbox running UnleashX with FTP enabled, including anonymous login
  (used for read-only checks; uploads and deletes ask for your FTP login).
- For `stage_game.py` only: [extract-xiso](https://github.com/XboxDev/extract-xiso/releases),
  unzipped to `tools/extract-xiso/artifacts/`.

The scripts default to the Xbox at `192.168.137.2`. Use `--ip` to change it.

## Scripts

| Script | Does |
| --- | --- |
| `push_stage.py` | Uploads a game from a disc image (`.iso` / `.xiso`) or a staging folder. Skips files already on the Xbox, checks free space before starting, and resumes if interrupted. |
| `remove_game.py` | Lists the games on the Xbox and deletes the one you pick. |
| `stage_game.py` | Extracts a disc image into a staging folder. |
| `xiso.py` | Shows what's in a disc image: title, region, size, files. |
| `push-480i-config.sh` | Turns UnleashX's 480p setting off or back on (needs Bash, e.g. Git Bash). |

Run any script with `-h` for its options.

## Examples

```
python xiso.py "Game (USA).iso"
python push_stage.py "Game (USA).iso" --dry-run
python push_stage.py "Game (USA).iso"
python remove_game.py
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
