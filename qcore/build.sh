#!/usr/bin/env bash
# Build the qcore C++ extension (qcore/_qcore<ext>.so) with the local toolchain.
# No CMake needed. Requires: a C++ compiler + `pip install pybind11`.
#
#   ./qcore/build.sh          # build
#   CXX=g++ ./qcore/build.sh  # pick a compiler
#
# After building, `python -c "import qcore; print(qcore.BACKEND)"` prints "cpp".
set -euo pipefail
cd "$(dirname "$0")"

PYINC=$(python3 -m pybind11 --includes)
EXT=$(python3-config --extension-suffix)
CXX=${CXX:-c++}

FLAGS=(-O3 -Wall -shared -std=c++17 -fPIC)   # C++17 for std::optional
# shellcheck disable=SC2206
FLAGS+=($PYINC)
if [[ "$(uname)" == "Darwin" ]]; then
    FLAGS+=(-undefined dynamic_lookup)   # macOS Python extensions
fi

echo "building _qcore$EXT with $CXX ..."
$CXX "${FLAGS[@]}" _qcore.cpp -o "_qcore$EXT"
echo "built qcore/_qcore$EXT"
