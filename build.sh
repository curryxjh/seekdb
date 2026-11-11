#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
set -- -std=c++20 -Wall -Wextra -Wpedantic -Wconversion -Wshadow
if [ "${SANITIZE:-0}" = 1 ]; then
  set -- "$@" -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer
else
  set -- "$@" -O3
fi
if [ "${FAULTS:-0}" = 1 ]; then
  set -- "$@" -DMINIVECTORDB_FAULTS
fi
"${CXX:-c++}" "$@" minivectordb/main.cpp minivectordb/protocol.cpp minivectordb/sql.cpp \
  minivectordb/storage.cpp minivectordb/database.cpp -o "${OUTPUT:-build/minivectordb}"
