#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/dist"

rm -rf "$OUT"
mkdir -p "$OUT/assets" "$OUT/data"

cp "$ROOT/index.html" "$OUT/index.html"
cp -a "$ROOT/assets/." "$OUT/assets/"

required_data=(
  "state.json"
  "live-state.json"
)

optional_data=(
  "ai_daily_analysis.json"
  "last-update-report.json"
  "provider-health.json"
)

for name in "${required_data[@]}"; do
  test -f "$ROOT/data/$name"
  cp "$ROOT/data/$name" "$OUT/data/$name"
done

for name in "${optional_data[@]}"; do
  if [[ -f "$ROOT/data/$name" ]]; then
    cp "$ROOT/data/$name" "$OUT/data/$name"
  fi
done

cat > "$OUT/_headers" <<'EOF'
/index.html
  Cache-Control: no-store, no-cache, must-revalidate, max-age=0

/data/*
  Cache-Control: no-store, no-cache, must-revalidate, max-age=0

/assets/*
  Cache-Control: public, max-age=3600
EOF

touch "$OUT/.nojekyll"

echo "CLOUDFLARE_PAGES_BUILD=GREEN"
echo "OUTPUT_DIR=$OUT"
