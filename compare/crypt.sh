#!/usr/bin/env bash
# Hybrid encryption for the model comparison: the repository is public, so Nina's voices and the
# posts made from them are committed only encrypted.
#   bash compare/crypt.sh seal PUBKEY SRC_DIR OUT_PREFIX   -> OUT_PREFIX.key (RSA-OAEP), OUT_PREFIX.tgz.enc (AES-256)
#   bash compare/crypt.sh open PRIVKEY IN_PREFIX DEST_DIR
set -euo pipefail
cmd=$1
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
case "$cmd" in
  seal)
    pub=$2 src=$3 out=$4
    openssl rand -hex 32 > "$tmp/k"
    openssl pkeyutl -encrypt -pubin -inkey "$pub" -pkeyopt rsa_padding_mode:oaep -in "$tmp/k" -out "$out.key"
    tar czf - -C "$src" . | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$tmp/k" -out "$out.tgz.enc"
    ;;
  open)
    priv=$2 in=$3 dest=$4
    openssl pkeyutl -decrypt -inkey "$priv" -pkeyopt rsa_padding_mode:oaep -in "$in.key" -out "$tmp/k"
    mkdir -p "$dest"
    openssl enc -d -aes-256-cbc -pbkdf2 -pass "file:$tmp/k" -in "$in.tgz.enc" | tar xzf - -C "$dest"
    ;;
  *) echo "usage: crypt.sh seal|open ..." >&2; exit 2 ;;
esac
