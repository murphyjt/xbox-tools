#!/usr/bin/env bash
# Turns UnleashX's 480p setting off (the 480i fix) or back on, by editing the
# Xbox's CURRENT C:\Dashboard\config.xml - only the Use480P value changes, so your
# other UnleashX settings (network, auto-launch, etc.) are kept.
#   1. Reads the live config.xml (read-only anonymous login) and saves a backup
#      to snapshot/config-backup-<date>.xml
#   2. Changes Use480P (keeping the file's exact line endings)
#   3. Asks for the FTP login, uploads it, reads it back and compares
# Usage (Git Bash):
#   bash push-480i-config.sh            # set Use480P="No"  (480i fix)
#   bash push-480i-config.sh restore    # set Use480P="Yes" (undo)
#   bash push-480i-config.sh [480i|restore] [ip]
#   DRYRUN=1 bash push-480i-config.sh   # show what would change, no login needed
set -u
MODE="${1:-480i}"; IP="${2:-192.168.137.2}"
case "$MODE" in
  480i)    WANT=No ;;
  restore) WANT=Yes ;;
  *) echo "Usage: bash push-480i-config.sh [480i|restore] [ip]"; exit 1 ;;
esac
DIR="$(cd "$(dirname "$0")" && pwd)/snapshot"; mkdir -p "$DIR"
URL="ftp://$IP/C/Dashboard/config.xml"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

curl -sS --max-time 20 -u anonymous:guest@ "$URL" -o "$TMP/live.xml" \
  || { echo "Couldn't read config.xml from the Xbox (is UnleashX on screen?)."; exit 1; }
NOW=$(grep -a -o 'Use480P="[A-Za-z]*"' "$TMP/live.xml" | head -1)
[ -n "$NOW" ] || { echo "No Use480P setting found in config.xml - not touching it."; exit 1; }
echo "On the Xbox now: $NOW"
if [ "$NOW" = "Use480P=\"$WANT\"" ]; then echo "Already set - nothing to do."; exit 0; fi

sed -b "s/Use480P=\"[A-Za-z]*\"/Use480P=\"$WANT\"/" "$TMP/live.xml" > "$TMP/new.xml"
echo "Will change: $NOW -> Use480P=\"$WANT\" (nothing else in the file changes)"
if [ "${DRYRUN:-0}" = 1 ]; then echo "Dry run - not uploading."; exit 0; fi
BACKUP="$DIR/config-backup-$(date +%Y%m%d-%H%M%S).xml"
cp "$TMP/live.xml" "$BACKUP"
echo "Backup saved: $BACKUP"

read -rp  "FTP user: " U
read -rsp "FTP password: " P; echo
printf 'user = "%s:%s"\n' "$U" "$P" | curl -sS --max-time 30 -K - -T "$TMP/new.xml" "$URL" \
  || { echo "Upload failed (curl exit $?). Nothing changed on the Xbox."; exit 1; }

curl -sS --max-time 20 -u anonymous:guest@ "$URL" -o "$TMP/verify.xml" || { echo "Read-back failed."; exit 1; }
if cmp -s "$TMP/verify.xml" "$TMP/new.xml"; then
  echo "VERIFIED. On the Xbox now: $(grep -a -o 'Use480P="[A-Za-z]*"' "$TMP/verify.xml")"
  echo "Restart the Xbox (full power cycle) for UnleashX to reload it."
else
  echo "WARNING: the file on the Xbox doesn't match what was sent. Backup: $BACKUP"; exit 1
fi
