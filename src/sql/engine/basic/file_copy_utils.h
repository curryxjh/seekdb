/*
 * Copyright (c) 2026 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

#include <atomic>

#include "sql/engine/basic/file_scan_utils.h"

namespace oceanbase {
namespace sql {
namespace file_sql {

// Write into a private sibling, then publish with an atomic no-replace link.
// The directory descriptor anchors all mutations to the checked directory.
class CsvWriter {
 public:
  CsvWriter() = default;
  CsvWriter(const CsvWriter&) = delete;
  CsvWriter& operator=(const CsvWriter&) = delete;
  std::string error;
  ~CsvWriter() { abort(); }
  bool open(const Options& options) {
    abort();
    error.clear();
    options_ = options;
    const std::string& path = options.path;
    if (path.empty() || path.size() >= 4096 || path.find('\0') != std::string::npos)
      return fail("invalid COPY path");
    size_t slash = path.find_last_of('/');
    std::string parent = slash == std::string::npos ? "."
                         : slash == 0               ? "/"
                                                    : path.substr(0, slash);
    name_ = slash == std::string::npos ? path : path.substr(slash + 1);
    std::string real, root;
    if (name_.empty() || name_ == "." || name_ == ".." || !canonical(parent, real) ||
        !canonical(options.root, root) || !within(root, real))
      return fail("COPY path rejected by secure_file_priv");
    dir_ = ::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    for (size_t p = 1; dir_ >= 0 && p < real.size();) {
      size_t end = real.find('/', p);
      if (end == std::string::npos) end = real.size();
      int next = ::openat(dir_, real.substr(p, end - p).c_str(),
                          O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
      ::close(dir_);
      dir_ = next;
      p = end + 1;
    }
    if (dir_ < 0) return fail("cannot open COPY output directory");
    struct stat st;
    if (::fstatat(dir_, name_.c_str(), &st, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT)
      return fail("COPY target already exists or cannot be inspected");
    static std::atomic<uint64_t> sequence{0};
    for (int i = 0; i < 100; ++i) {
      temp_ = ".seekdb-copy-" + std::to_string(::getpid()) + "-" +
              std::to_string(sequence.fetch_add(1));
      int fd =
          ::openat(dir_, temp_.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
      if (fd < 0) {
        if (errno == EEXIST) continue;
        return fail("cannot create COPY temporary file");
      }
      owns_temp_ = true;
      file_ = ::fdopen(fd, "wb");
      if (!file_) {
        ::close(fd);
        return fail("cannot buffer COPY output");
      }
      return true;
    }
    return fail("cannot allocate COPY temporary file");
  }
  bool row(const std::vector<Cell>& cells) {
    if (!file_) return fail("COPY output is not open");
    if (cells.size() > MAX_COLUMNS) return fail("COPY column count exceeds resource limit");
    // Bound the encoded record, including quoting and escape expansion, so
    // every successfully exported row fits the reader's record limit.
    size_t size = 1 + (cells.empty() ? 0 : cells.size() - 1);
    for (const Cell& cell : cells) {
      if (!cell.null && !utf8(cell.text)) return fail("COPY requires valid UTF-8 text");
      size_t n = cell.null ? options_.nullstr.size() : cell.text.size() + 2;
      if (!cell.null)
        for (char c : cell.text)
          if (c == options_.quote || c == options_.escape) ++n;
      if (n > MAX_RECORD || size > MAX_RECORD - n)
        return fail("COPY record exceeds resource limit");
      size += n;
    }
    for (size_t i = 0; i < cells.size(); ++i) {
      if (i && ::fputc(options_.delimiter, file_) == EOF) return fail("COPY write failed");
      const Cell& cell = cells[i];
      if (cell.null) {
        if (::fwrite(options_.nullstr.data(), 1, options_.nullstr.size(), file_) !=
            options_.nullstr.size())
          return fail("COPY write failed");
      } else {
        // Quote all non-NULL fields, preserving empty strings and NULL text.
        if (::fputc(options_.quote, file_) == EOF) return fail("COPY write failed");
        for (char c : cell.text) {
          if ((c == options_.quote || c == options_.escape) &&
              ::fputc(options_.escape, file_) == EOF)
            return fail("COPY write failed");
          if (::fputc(static_cast<unsigned char>(c), file_) == EOF)
            return fail("COPY write failed");
        }
        if (::fputc(options_.quote, file_) == EOF) return fail("COPY write failed");
      }
    }
    if (::fputc('\n', file_) == EOF) return fail("COPY write failed");
    return true;
  }
  bool publish() {
    if (!file_) return fail("COPY output is not open");
    bool ok = ::fflush(file_) == 0 && ::fsync(::fileno(file_)) == 0;
    if (::fclose(file_) != 0) ok = false;
    file_ = nullptr;
    if (!ok) return fail("COPY flush failed");
    if (::linkat(dir_, temp_.c_str(), dir_, name_.c_str(), 0) != 0)
      return fail("COPY target exists or publication failed");
    // Retain ownership on an interrupted cleanup so destruction retries it.
    if (::unlinkat(dir_, temp_.c_str(), 0) == 0) owns_temp_ = false;
    return true;
  }

 private:
  bool fail(const char* message) {
    error = message;
    return false;
  }
  void abort() {
    if (file_) {
      ::fclose(file_);
      file_ = nullptr;
    }
    if (owns_temp_ && dir_ >= 0) ::unlinkat(dir_, temp_.c_str(), 0);
    if (dir_ >= 0) {
      ::close(dir_);
      dir_ = -1;
    }
    owns_temp_ = false;
    temp_.clear();
  }
  Options options_;
  FILE* file_ = nullptr;
  int dir_ = -1;
  bool owns_temp_ = false;
  std::string name_, temp_;
};

}  // namespace file_sql
}  // namespace sql
}  // namespace oceanbase
