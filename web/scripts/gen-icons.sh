#!/usr/bin/env bash
# Renders every icon the app ships from the ONE brand raster,
# web/public/brand/viberpit-mascot.png (the full mascot, transparent).
#
# Run MANUALLY after the mascot changes, then commit the PNGs:
#     (cd web && scripts/gen-icons.sh)
#
# Not part of `npm run build` — that would add an image-rendering step to every
# CI build for assets that change roughly never. Needs ImageMagick (`convert`).
#
# The app icon / mark is a CROP of the mascot, not a separate drawing: the head
# (sunglasses to tongue) sits inside the centre 80% of a Deep Night plate — the
# only region Android's maskable-icon shapes are guaranteed to show — and the
# coil and laptop run off the plate's edges, so the crop's straight cut lines
# always fall under the rounded corners or the launcher's mask. One mark
# therefore serves every target: the `any` icons and favicon get rounded
# corners (r = 22% of the side, the same 7/32 as the mark this replaces),
# apple-touch-icon and the maskable icon are full-bleed and let iOS / Android
# apply their own shape.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC=public/brand/viberpit-mascot.png
PLATE='#0B1020' # brand "Deep Night" — the plate colour, also the maskable bleed

# Head crop, in the 1024-wide mascot's pixel space. Retune here, nowhere else.
CROP=760x760+290+0
MARK=$(mktemp --suffix=.png)
trap 'rm -f "$MARK"' EXIT

convert "$SRC" -crop "$CROP" +repage -resize 1024x1024 \
  \( -size 1024x1024 xc:"$PLATE" \) +swap -gravity center -composite \
  -depth 8 "$MARK"

# $1 = size, $2 = out path, $3 = rounded|square
emit() {
  local size=$1 out=$2 shape=$3 r
  r=$(( size * 22 / 100 ))
  if [[ $shape == rounded ]]; then
    convert "$MARK" -resize "${size}x${size}" -filter Lanczos \
      \( -size "${size}x${size}" xc:none -draw "roundrectangle 0,0,$((size-1)),$((size-1)),$r,$r" \) \
      -compose DstIn -composite -strip -depth 8 "$out"
  else
    convert "$MARK" -resize "${size}x${size}" -filter Lanczos -strip -depth 8 "$out"
  fi
  echo "wrote $out (${size}x${size}, $shape)"
}

mkdir -p public/icons
emit 32  public/favicon.png              rounded
emit 180 public/apple-touch-icon.png     square   # iOS rounds it
emit 192 public/icons/icon-192.png       rounded
emit 512 public/icons/icon-512.png       rounded
emit 512 public/icons/icon-512-maskable.png square   # launcher masks it
