#!/usr/bin/env python3
"""Compile the focused FTS test against an existing Linux CMake Release build.

Build the existing `oceanbase` target first. This runner uses the production
Storage target's flags and does not change the repository's build entry points.
"""
import argparse
from pathlib import Path
import platform
import shlex
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("run this test against a Linux build")
    build = args.build_dir.resolve()
    cache = {}
    for line in (build / "CMakeCache.txt").read_text().splitlines():
        if not line.startswith(("#", "//")) and ":" in line and "=" in line:
            key, value = line.split("=", 1)
            cache[key.split(":", 1)[0]] = value
    source = Path(cache["CMAKE_HOME_DIRECTORY"])
    flags_file = build / "src/storage/CMakeFiles/ob_storage.dir/flags.make"
    flags = {}
    compiler = cache.get("CMAKE_CXX_COMPILER")
    for line in flags_file.read_text().splitlines():
        if line.startswith("# compile CXX with "):
            compiler = line[len("# compile CXX with "):]
        if " = " in line:
            key, value = line.split(" = ", 1)
            flags[key] = shlex.split(value)
    sdk = source / "deps/3rd/usr/local/oceanbase/deps/devel"
    library = build / "src/observer/liboceanbase.so"
    if not library.is_file():
        parser.error("build the existing oceanbase target first")
    output_dir = build / "fts-regression"
    output_dir.mkdir(exist_ok=True)
    binary = output_dir / "test_word_aggregation"
    if not compiler:
        parser.error("cannot locate the Storage target's C++ compiler")
    command = [compiler]
    for name in ("CXX_DEFINES", "CXX_INCLUDES", "CXX_FLAGS"):
        command.extend(flags[name])
    command.extend([
        "-I" + str(sdk / "include"),
        str(Path(__file__).with_name("test_word_aggregation.cpp").resolve()),
        "-o", str(binary), str(sdk / "lib/libgtest.a"), str(library),
        "-Wl,-rpath," + str(library.parent),
        "-pthread", "-ldl", "-static-libgcc", "-static-libstdc++",
    ])
    subprocess.run(command, cwd=build, check=True)
    subprocess.run([str(binary)], cwd=output_dir, check=True)


if __name__ == "__main__":
    main()
