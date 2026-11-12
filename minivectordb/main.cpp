#include "database.h"
#include "protocol.h"

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <unistd.h>

namespace {
volatile std::sig_atomic_t stopping = 0;
void stop(int) { stopping = 1; }

void emit(std::string_view response) {
  if (std::fwrite(response.data(), 1, response.size(), stdout) != response.size() ||
      std::putchar('\n') == EOF || std::fflush(stdout) != 0)
    throw std::runtime_error("stdout failed");
}

// Read physical JSONL lines, never split the transport on SQL punctuation.
bool read_line(std::string &line) {
  static char buffer[65536];
  static size_t begin = 0, end = 0;
  line.clear();
  while (!stopping) {
    if (begin == end) {
      const auto n = ::read(STDIN_FILENO, buffer, sizeof(buffer));
      if (n < 0) {
        if (errno == EINTR) continue;
        throw std::runtime_error("stdin failed");
      }
      if (n == 0) return !line.empty();
      begin = 0; end = static_cast<size_t>(n);
    }
    const auto *newline = static_cast<const char *>(std::memchr(buffer + begin, '\n', end - begin));
    const size_t length = newline ? static_cast<size_t>(newline - (buffer + begin)) : end - begin;
    mvdb::require(length <= 16U * 1024U * 1024U - line.size(), "request_limit", "JSON request exceeds 16 MiB");
    line.append(buffer + begin, length); begin += length;
    if (newline) { ++begin; return true; }
  }
  return false;
}
}

int main(int argc, char **argv) {
  try {
    std::string directory = "/data";
    for (int i = 1; i < argc; ++i) {
      mvdb::require(std::string_view(argv[i]) == "--data" && i + 1 < argc,
                    "arguments", "usage: minivectordb [--data directory]");
      directory = argv[++i];
    }
    struct sigaction action {};
    action.sa_handler = stop;
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, nullptr);
    sigaction(SIGINT, &action, nullptr);
    mvdb::Database database(directory);
    emit("{\"type\":\"ready\"}");
    std::string line;
    while (read_line(line)) {
      mvdb::Request request;
      std::unique_ptr<mvdb::Spool> response;
      try {
        request = mvdb::parse_request(line);
        mvdb::require(request.has_id, "protocol", "request id is required");
        if (request.op == "shutdown") {
          database.shutdown();
          emit("{\"id\":" + request.id + ",\"ok\":true}");
          return 0;
        }
        mvdb::require(request.op == "execute", "protocol", "unsupported operation");
        mvdb::require(request.has_sql, "protocol", "sql string is required");
        mvdb::require(request.search_mode == "exact" || request.search_mode == "ann",
                      "protocol", "invalid search mode");
        // Both supported modes currently use exact retrieval.
        response = std::make_unique<mvdb::Spool>(database.execute(mvdb::parse(request.sql)));
      } catch (const mvdb::Error &e) {
        emit("{\"id\":" + request.id + ",\"ok\":false,\"error\":{\"code\":" +
             mvdb::json_string(e.code) + ",\"message\":" + mvdb::json_string(e.what()) + "}}");
        continue;
      }
      // Output failures after execution are fatal, never a second response.
      mvdb::fault("before_success_response");
      response->output(stdout, request.id);
      if (std::putchar('\n') == EOF || std::fflush(stdout) != 0) throw std::runtime_error("stdout failed");
      mvdb::fault("after_success_response");
    }
    database.shutdown();
  } catch (const std::exception &e) {
    std::fprintf(stderr, "minivectordb: %s\n", e.what()); return 74;
  }
  return 0;
}
