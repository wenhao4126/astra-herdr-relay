#!/usr/bin/env bash
set -u
BIN="$HOME/.local/bin"
CFG="$HOME/.config/astra-herdr-relay"
AGENTS="$HOME/.codex/AGENTS.md"

echo "=== Astra Relay v0.5.0 clean-baseline doctor ==="
printf "relay version: "; astra-relay --version 2>/dev/null || echo "MISSING"
service_status=$(systemctl --user is-active astra-herdr-relay.service 2>&1)
service_rc=$?
if [[ $service_rc -eq 0 ]]; then
  printf "service: %s\n" "$service_status"
elif [[ "$service_status" == "inactive" ]]; then
  printf "service: inactive\n"
else
  printf "service: UNKNOWN (%s)\n" "${service_status:-exit $service_rc}"
fi
printf "Jev key: "
if grep -q '^TYPESAFE_API_KEY=.' "$CFG/secret.env" 2>/dev/null; then echo "configured"; else echo "MISSING"; fi

echo "--- Relay AGENTS blocks ---"
grep -o '<!-- ASTRA-HERDR-RELAY[^>]*BEGIN -->' "$AGENTS" 2>/dev/null || true

echo "--- stale known wrappers ---"
for f in astra-agent-read astra-agent-wait astra-agent-send astra-relay-events astra-relay-daemon-state astra-relay-reviews; do
  [[ -e "$BIN/$f" ]] && echo "STALE: $BIN/$f"
done

echo "--- Herdr ---"
command -v herdr || true
herdr --version 2>/dev/null || true
herdr tab list 2>/dev/null || true
herdr agent list 2>/dev/null || true

echo "--- Codex Relay hook ---"
test -x "$HOME/.local/lib/astra-herdr-relay/hooks/astra_relay_subagent_redirect.py" && echo "redirect script: OK" || echo "redirect script: MISSING"
python3 - "$HOME/.codex/hooks.json" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])
try:
    o=json.loads(p.read_text())
    found=any("astra_relay_subagent_redirect.py" in str(h.get("command",""))
              for g in o.get("hooks",{}).get("PreToolUse",[])
              for h in (g.get("hooks",[]) if isinstance(g,dict) else [])
              if isinstance(h,dict))
    print("PreToolUse redirect:", "OK" if found else "MISSING")
except Exception as e: print("PreToolUse redirect: ERROR",e)
PY
