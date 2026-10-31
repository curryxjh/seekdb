# FTS module tests share the server implementation graph.
find_library(SEEKDB_GTEST_LIBRARY NAMES gtest
  HINTS "${DEP_DIR}/lib" "${CMAKE_SOURCE_DIR}/deps/3rd/usr/local/oceanbase/deps/devel/lib")
if(NOT SEEKDB_GTEST_LIBRARY)
  message(STATUS "Google Test not installed; module test targets are unavailable")
  return()
endif()
foreach(module share storage)
  file(GLOB_RECURSE module_sources CONFIGURE_DEPENDS "${module}/test_*.cpp")
  if(module_sources)
    add_executable(${module}_tests EXCLUDE_FROM_ALL test_main.cpp ${module_sources})
    target_link_libraries(${module}_tests PRIVATE oceanbase ${SEEKDB_GTEST_LIBRARY} pthread)
    target_include_directories(${module}_tests PRIVATE
      "${CMAKE_SOURCE_DIR}/deps/3rd/usr/local/oceanbase/deps/devel/include")
    set_target_properties(${module}_tests PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/${module}")
    add_test(NAME ${module}_tests_shard_0 COMMAND ${module}_tests)
  endif()
endforeach()
add_custom_target(pretest DEPENDS share_tests storage_tests)
