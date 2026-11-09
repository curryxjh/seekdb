#include "database.h"

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <iostream>

namespace {
volatile std::sig_atomic_t stopping = 0;
void stop(int) { stopping = 1; }

bool has_sql(std::string_view text) {
  size_t i = 0;
  while (i < text.size()) {
    if (text[i] == ' ' || text[i] == '\n' || text[i] == '\r' || text[i] == '\t') { ++i; continue; }
    if (text[i] == '#' || text.substr(i, 2) == "--") {
      const auto end = text.find('\n', i);
      if (end == std::string_view::npos) return false;
      i = end + 1;
    } else if (text.substr(i, 2) == "/*") {
      const auto end = text.find("*/", i + 2);
      if (end == std::string_view::npos) return true;
      i = end + 2;
    } else return true;
  }
  return false;
}
void error(const mvdb::Error &e) {
  const auto response = "{\"ok\":false,\"error\":" + mvdb::json_string(e.code) +
                        ",\"message\":" + mvdb::json_string(e.what()) + "}\n";
  if (std::fwrite(response.data(), 1, response.size(), stdout) != response.size() ||
      std::fflush(stdout) != 0) throw std::runtime_error("stdout failed");
}
}

int main(int argc, char **argv) {
  std::string directory = "/data";
  std::string mode = "exact";
  try {
    for (int i = 1; i < argc; ++i) {
      const std::string option = argv[i];
      mvdb::require((option == "--data" || option == "--mode") && i + 1 < argc,
                    "arguments", "usage: minivectordb [--data directory] [--mode exact|ann]");
      if (option == "--data") directory = argv[++i];
      else mode = argv[++i];
    }
    mvdb::require(mode == "exact" || mode == "ann", "arguments", "invalid search mode");
    struct sigaction action {};
    action.sa_handler = stop;
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, nullptr);
    sigaction(SIGINT, &action, nullptr);
    mvdb::Database database(directory);
    std::puts("{\"ready\":true,\"protocol\":\"minivectordb-local-v1\"}");
    if (std::fflush(stdout) != 0) return 74;
    mvdb::Framer framer;
    const auto execute = [&](const std::string &sql) {
      if (!has_sql(sql)) return;
      std::unique_ptr<mvdb::Spool> response;
      try { response = std::make_unique<mvdb::Spool>(database.execute(mvdb::parse(sql))); }
      catch (const mvdb::Error &e) { error(e); return; }
      // Output failures after execution are fatal, never converted to a second
      // response suggesting that a committed statement was rolled back.
      mvdb::fault("before_success_response");
      response->output(stdout);
      if (std::putchar('\n') == EOF || std::fflush(stdout) != 0) throw std::runtime_error("stdout failed");
      mvdb::fault("after_success_response");
    };
    while (!stopping) {
      int next = std::getchar();
      if (next == EOF) {
        if (std::ferror(stdin) && errno == EINTR && !stopping) { std::clearerr(stdin); continue; }
        break;
      }
      if (next == '\\' && framer.empty()) {
        std::string control;
        while ((next = std::getchar()) != EOF && next != '\n') {
          mvdb::require(control.size() < 64, "protocol", "invalid local control");
          control += static_cast<char>(next);
        }
        if (control == "mode exact" || control == "mode ann") {
          mode = control.substr(5); std::puts("{\"ok\":true}"); std::fflush(stdout);
        } else error(mvdb::Error("protocol", "unknown local control"));
        continue;
      }
      std::string sql;
      if (framer.feed(static_cast<char>(next), sql)) execute(sql);
    }
    if (!stopping) {
      try { execute(framer.finish()); }
      catch (const mvdb::Error &e) { error(e); }
    }
    database.shutdown();
  } catch (const std::exception &e) {
    std::fprintf(stderr, "minivectordb: %s\n", e.what()); return 74;
  }
  return 0;
}
