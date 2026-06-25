#!/usr/bin/env bash
#
# t24_run.sh — interactive multi-server command runner for T24 test environments
# -----------------------------------------------------------------------------
# Reads a CSV of environments, then runs ONE global command on every SSH host
# in the list at once. On each host it:
#       1. cd's into that host's bnk.run path  (from the CSV)
#       2. sources a trimmed .profile so PATH/env are loaded WITHOUT triggering
#          the interactive jBASE login process:
#               . <(sed '/jpqn.*loginproc/,$d' "$HOME/.profile")
#       3. runs your command, with the bnk.run path passed as the last argument
#
# CSV columns (header row required, this exact order):
#   Groups,Label,Tags,Hostname/IP,Protocol,Port,Username,Password,bnk.run
#
# Auth: password from the CSV via plink (PuTTY) -pw.
# Requires: bash (Git Bash / WSL / Linux), plink.exe (PuTTY) on PATH
#           (timeout/gtimeout used if present).
# Launch from cmd with the bundled t24_run.cmd wrapper, or run directly in bash.
# -----------------------------------------------------------------------------

# ----------------------------- tunables --------------------------------------
DEFAULT_CSV="Test_Environments"        # base name looked for first (.csv also tried)
CMD_LIBRARY=".t24_cmd_library.tsv"     # saved labelled commands (label<TAB>command)
SSH_TIMEOUT=60                         # hard cap per host (seconds) via timeout(1)
APPEND_BNKRUN_ARG=0                    # 0 = run your command as-is in the bnk.run dir (correct for T24
                                       #     verbs like LIST/SELECT). 1 = also append the bnk.run path as
                                       #     the last arg (only if YOUR command expects a path argument).
PROFILE_PATH='"$HOME/.profile"'        # remote-evaluated. You said .profile lives in bnk.run;
                                       # since we cd into bnk.run first, change this to '.profile'
                                       # if $HOME is NOT the bnk.run directory.
# ------------------------------------------------------------------------------

set -u

# --- locate a timeout command (Linux: timeout, macOS/brew: gtimeout) ----------
TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_BIN="gtimeout"; fi

# --- prerequisite check -------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: '$1' not found in PATH." >&2; MISSING=1; }; }
MISSING=0
need plink
if [ "$MISSING" = "1" ]; then
  echo "Install PuTTY (provides plink.exe) and make sure it is on PATH, then re-run." >&2
  echo "  Download: https://www.putty.org/   (typical path: C:\\Program Files\\PuTTY)" >&2
  exit 1
fi
[ -z "$TIMEOUT_BIN" ] && echo "NOTE: no timeout/gtimeout found — hung hosts will not be force-killed." >&2

# --- helpers ------------------------------------------------------------------
trim() {  # strip surrounding whitespace, CR, and one layer of quotes
  local s="$1"
  s="${s%$'\r'}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  s="${s%\"}"; s="${s#\"}"
  printf '%s' "$s"
}
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# Split one CSV/TSV record into the global array CSV_F[], honouring double-quoted
# fields (which may contain the delimiter) and "" as an escaped quote.
parse_csv_line() {
  local line="$1" delim="$2" i=0 n c field="" inq=0
  CSV_F=()
  n=${#line}
  while [ "$i" -lt "$n" ]; do
    c="${line:$i:1}"
    if [ "$inq" -eq 1 ]; then
      if [ "$c" = '"' ]; then
        if [ "${line:$((i+1)):1}" = '"' ]; then field+='"'; i=$((i+2)); continue; fi
        inq=0; i=$((i+1)); continue
      fi
      field+="$c"; i=$((i+1)); continue
    fi
    if   [ "$c" = '"' ];      then inq=1; i=$((i+1))
    elif [ "$c" = "$delim" ]; then CSV_F+=("$field"); field=""; i=$((i+1))
    else field+="$c"; i=$((i+1)); fi
  done
  CSV_F+=("$field")
}

# ============================ 1. find the CSV =================================
CSV=""
for cand in "$DEFAULT_CSV" "$DEFAULT_CSV.csv"; do
  [ -f "$cand" ] && { CSV="$cand"; break; }
done
if [ -z "$CSV" ]; then
  echo "Could not find '$DEFAULT_CSV' (or '$DEFAULT_CSV.csv') in $(pwd)."
  while :; do
    printf 'Enter the CSV filename to use: '
    read -r CSV
    [ -n "$CSV" ] && [ -f "$CSV" ] && break
    echo "  '$CSV' not found — try again."
  done
fi
echo "Using environment file: $CSV"

# --- detect delimiter from the header (tab or comma) --------------------------
HEADER="$(head -n 1 "$CSV" | tr -d '\r')"
HEADER="${HEADER#$'\xef\xbb\xbf'}"   # drop UTF-8 BOM if present
if printf '%s' "$HEADER" | grep -q $'\t'; then DELIM=$'\t'; else DELIM=','; fi

# ============================ 2. parse the CSV ===============================
# Arrays, one entry per SSH host.
LBL=(); HOST=(); PORT=(); USER=(); PASS=(); BNK=()
linenum=0
while IFS= read -r raw || [ -n "$raw" ]; do
  linenum=$((linenum+1))
  raw="${raw%$'\r'}"
  [ "$linenum" = "1" ] && continue           # skip header
  [ -z "${raw//[[:space:]]/}" ] && continue   # skip blank lines

  parse_csv_line "$raw" "$DELIM"
  c_label="${CSV_F[1]:-}"; c_host="${CSV_F[3]:-}"; c_proto="${CSV_F[4]:-}"
  c_port="${CSV_F[5]:-}";  c_user="${CSV_F[6]:-}"; c_pass="${CSV_F[7]:-}"
  c_bnk="${CSV_F[8]:-}"

  proto="$(lower "$(trim "${c_proto:-}")")"
  case "$proto" in
    ssh|"" ) : ;;          # blank protocol assumed SSH
    * ) continue ;;        # skip non-SSH rows (e.g. RDP/HTTP)
  esac

  host="$(trim "${c_host:-}")"
  [ -z "$host" ] && continue
  port="$(trim "${c_port:-}")"; [ -z "$port" ] && port=22
  user="$(trim "${c_user:-}")"
  pass="$(trim "${c_pass:-}")"
  bnk="$(trim "${c_bnk:-}")"
  label="$(trim "${c_label:-}")"; [ -z "$label" ] && label="$host"

  LBL+=("$label"); HOST+=("$host"); PORT+=("$port")
  USER+=("$user"); PASS+=("$pass"); BNK+=("$bnk")
done < "$CSV"

N=${#HOST[@]}
if [ "$N" -eq 0 ]; then
  echo "No SSH hosts parsed from '$CSV'. Check the header/columns." >&2
  exit 1
fi

echo
echo "Parsed $N SSH environment(s):"
for i in $(seq 0 $((N-1))); do
  printf '  %2d) %-22s %s@%s:%s   bnk.run=%s\n' \
    "$((i+1))" "${LBL[$i]}" "${USER[$i]}" "${HOST[$i]}" "${PORT[$i]}" "${BNK[$i]:-<none>}"
done
echo

# ===================== 3. choose / enter the command =========================
COMMAND=""
if [ -f "$CMD_LIBRARY" ] && [ -s "$CMD_LIBRARY" ]; then
  echo "Saved commands (from $CMD_LIBRARY):"
  declare -a LIB_LABEL LIB_CMD
  while IFS=$'\t' read -r l c; do
    [ -z "$l" ] && continue
    LIB_LABEL+=("$l"); LIB_CMD+=("$c")
    printf '   %2d) [%s]  %s\n' "${#LIB_LABEL[@]}" "$l" "$c"
  done < "$CMD_LIBRARY"
  echo
  printf 'Pick a number (or label) to reuse, or press Enter to type a NEW command: '
  read -r pick
  if [ -n "$pick" ]; then
    if printf '%s' "$pick" | grep -qE '^[0-9]+$'; then
      idx=$((pick-1))
      if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#LIB_CMD[@]}" ]; then
        COMMAND="${LIB_CMD[$idx]}"
      else
        echo "No saved command numbered '$pick' — you'll enter a new one."
      fi
    else
      for j in "${!LIB_LABEL[@]}"; do
        if [ "${LIB_LABEL[$j]}" = "$pick" ]; then COMMAND="${LIB_CMD[$j]}"; break; fi
      done
      [ -z "$COMMAND" ] && echo "No saved command labelled '$pick' — you'll enter a new one."
    fi
  fi
  [ -n "$COMMAND" ] && echo "Selected: $COMMAND"
fi

if [ -z "$COMMAND" ]; then
  printf 'Enter the global command to run on all servers:\n> '
  read -r COMMAND
  [ -z "$COMMAND" ] && { echo "No command entered. Aborting."; exit 1; }

  printf 'Label for this command (to reuse later; blank = do not save): '
  read -r newlabel
  if [ -n "$newlabel" ]; then
    # replace any existing entry with the same label, then append
    if [ -f "$CMD_LIBRARY" ]; then
      grep -v -P "^${newlabel}\t" "$CMD_LIBRARY" > "$CMD_LIBRARY.tmp" 2>/dev/null || \
        grep -v "^${newlabel}	" "$CMD_LIBRARY" > "$CMD_LIBRARY.tmp" 2>/dev/null || \
        : > "$CMD_LIBRARY.tmp"
      mv "$CMD_LIBRARY.tmp" "$CMD_LIBRARY"
    fi
    printf '%s\t%s\n' "$newlabel" "$COMMAND" >> "$CMD_LIBRARY"
    echo "Saved as '[$newlabel]' in $CMD_LIBRARY."
  fi
fi

echo
echo "About to run on $N host(s):"
echo "   $COMMAND"
printf 'Proceed? [y/N]: '
read -r go
case "$(lower "$go")" in y|yes) : ;; *) echo "Cancelled."; exit 0 ;; esac

# ============================ 4. execute =====================================
TS="$(date +%Y%m%d_%H%M%S 2>/dev/null || echo run)"
LOG="t24_results_${TS}.log"
TMP="$(mktemp -d 2>/dev/null || echo "/tmp/t24_$$")"; mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

run_on_host() {  # idx label host port user pass bnkrun
  local idx="$1" label="$2" host="$3" port="$4" user="$5" pass="$6" bnk="$7"
  local out="$TMP/$idx.out"

  # build the remote command line; optionally append the bnk.run path as last arg
  local cmdline="$COMMAND"
  [ "$APPEND_BNKRUN_ARG" = "1" ] && cmdline="$COMMAND \"$bnk\""

  # 1) Non-interactive host-key handling (trust-on-first-use). plink can't be told
  #    to "auto-accept new keys", and piping 'y' to the prompt doesn't work (plink
  #    reads it from the console). So we PROBE: a -batch connect prints the server's
  #    fingerprint to stderr then aborts when the key isn't cached. We capture that
  #    fingerprint and hand it back via -hostkey, so the real run accepts exactly
  #    that key with no prompt. (Host-key check happens before auth, so this works
  #    even before the password is verified. If the key is already cached, the probe
  #    succeeds, fp is empty, and we just rely on the cache.)
  local probe fp=""
  probe="$(plink -batch -ssh -P "$port" -pw "$pass" "$user@$host" exit 2>&1)"
  fp="$(printf '%s' "$probe" | grep -oE 'SHA256:[A-Za-z0-9+/=]+' | head -n1)"

  # 2) real run: hand the remote script to `bash -s` on stdin — no quoting hell,
  #    and bash (non-login) means the interactive jBASE loginproc never fires.
  #    -batch = never prompt interactively.
  local runner=(plink -ssh -P "$port" -pw "$pass" -batch)
  [ -n "$fp" ] && runner+=(-hostkey "$fp")
  runner+=("$user@$host" "bash -s")
  [ -n "$TIMEOUT_BIN" ] && runner=("$TIMEOUT_BIN" "$SSH_TIMEOUT" "${runner[@]}")

  # Unquoted heredoc => $bnk / $PROFILE_PATH / $cmdline expand LOCALLY (their
  # literal values are inserted); \$d is kept literal for the remote shell.
  "${runner[@]}" >"$out" 2>&1 <<REMOTE_EOF
cd "$bnk" || { echo "ERROR: cannot cd into bnk.run: $bnk"; exit 3; }
. <(sed '/jpqn.*loginproc/,\$d' $PROFILE_PATH) 2>/dev/null
$cmdline
REMOTE_EOF
  echo $? > "$TMP/$idx.rc"
}

# ============================ 5. execute (sequential, live) ===================
{
  echo "T24 multi-server run — $TS"
  echo "Command: $COMMAND"
  echo "CSV: $CSV   Hosts: $N"
  echo "================================================================"
} > "$LOG"

echo
echo "Running sequentially on $N host(s)..."
echo

ok=0; fail=0
for i in $(seq 0 $((N-1))); do
  label="${LBL[$i]}"; host="${HOST[$i]}"; port="${PORT[$i]}"; user="${USER[$i]}"

  # live progress line: "[3/11] ENV-01 (user@<host>:22) ... executing... "
  printf '[%d/%d] %-18s (%s@%s:%s) ... executing... ' \
         "$((i+1))" "$N" "$label" "$user" "$host" "$port"

  run_on_host "$i" "$label" "$host" "$port" "$user" "${PASS[$i]}" "${BNK[$i]}"
  rc="$(cat "$TMP/$i.rc" 2>/dev/null || echo 999)"
  out="$(cat "$TMP/$i.out" 2>/dev/null)"

  # Classify on what actually happened: T24 verbs return unreliable exit codes,
  # so "OK" = we connected and ran. Only genuine connect/auth failures = FAILED.
  if [ "$rc" = "124" ]; then
    status="TIMEOUT"; fail=$((fail+1))
  elif printf '%s' "$out" | grep -qiE 'FATAL ERROR|Unable to open connection|Network error|Connection (refused|timed out|abandoned)|Access denied|password was not accepted|host key'; then
    status="FAILED";  fail=$((fail+1))
  else
    status="OK";      ok=$((ok+1))
  fi
  printf 'done [%s]\n' "$status"

  # show this host's output indented, and append the full block to the log
  printf '%s\n' "$out" | sed 's/^/      /'
  echo
  {
    printf '%s\n' "----- [$status] $label ($user@$host:$port)  exit=$rc -----"
    printf '%s\n' "$out"
    echo
  } >> "$LOG"
done

# ============================ 6. summary ======================================
printf '%s\n' "================================================================"
printf 'SUMMARY:  %d OK,  %d failed,  %d total.\n' "$ok" "$fail" "$N"
echo "Full log: $LOG"
{
  printf '%s\n' "================================================================"
  printf 'SUMMARY:  %d OK,  %d failed,  %d total.\n' "$ok" "$fail" "$N"
} >> "$LOG"
