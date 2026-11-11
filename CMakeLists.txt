cmake_minimum_required(VERSION 3.16)
project(minivectordb LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_executable(minivectordb
  minivectordb/main.cpp
  minivectordb/protocol.cpp
  minivectordb/sql.cpp
  minivectordb/storage.cpp
  minivectordb/database.cpp)
target_compile_options(minivectordb PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)
option(MINIVECTORDB_SANITIZE "Build with address and undefined behavior checks" OFF)
option(MINIVECTORDB_FAULTS "Enable deterministic crash points for testing" OFF)
if(MINIVECTORDB_FAULTS)
  target_compile_definitions(minivectordb PRIVATE MINIVECTORDB_FAULTS)
endif()
if(MINIVECTORDB_SANITIZE)
  target_compile_options(minivectordb PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(minivectordb PRIVATE -fsanitize=address,undefined)
endif()
