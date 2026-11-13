#include "storage.h"

#include <algorithm>
#include <bit>
#include <cerrno>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

namespace mvdb {

namespace {
[[noreturn]] void io_error(const char *operation) {
  throw Error("io_error", std::string(operation) + ": " + std::strerror(errno));
}
void close_fd(int fd) { if (fd >= 0) ::close(fd); }
uint64_t checksum(std::string_view bytes) {
  uint64_t value = 14695981039346656037ULL;
  for (char ch : bytes) { value ^= static_cast<unsigned char>(ch); value *= 1099511628211ULL; }
  return value;
}
constexpr std::string_view data_magic = "MVDBDATA00000001";
constexpr std::string_view catalog_magic = "MVDBCAT000000001";
constexpr std::string_view commit_magic = "MVDBCOMMIT000001";
constexpr std::string_view legacy_guard = "MVDBLOG000000002: this directory requires commit-log storage.\n";
constexpr size_t commit_footer_size = 40 + commit_magic.size();

bool read_at(int fd, void *destination, size_t size, uint64_t offset) {
  auto *out = static_cast<char *>(destination);
  while (size != 0) {
    const auto n = ::pread(fd, out, size, static_cast<off_t>(offset));
    if (n < 0) { if (errno == EINTR) continue; io_error("pread commit"); }
    if (n == 0) return false;
    const auto amount = static_cast<size_t>(n);
    out += amount; size -= amount; offset += amount;
  }
  return true;
}
uint64_t checksum_region(int fd, uint64_t begin, uint64_t end) {
  std::array<char, 65536> buffer{};
  uint64_t value = 14695981039346656037ULL;
  while (begin < end) {
    const size_t size = static_cast<size_t>(std::min<uint64_t>(buffer.size(), end - begin));
    require(read_at(fd, buffer.data(), size, begin), "corrupt_data", "truncated commit payload");
    for (size_t i = 0; i < size; ++i) {
      value ^= static_cast<unsigned char>(buffer[i]); value *= 1099511628211ULL;
    }
    begin += size;
  }
  return value;
}
}

void fault(std::string_view name) {
#ifdef MINIVECTORDB_FAULTS
  const char *point = std::getenv("MVDB_FAULT");
  if (point != nullptr && name == point) {
    static unsigned hit = 0;
    ++hit;
    const char *target = std::getenv("MVDB_FAULT_HIT");
    const auto wanted = target == nullptr ? 1UL : std::strtoul(target, nullptr, 10);
    if (hit == wanted) ::kill(::getpid(), SIGKILL);
  }
#else
  (void)name;
#endif
}

void write_all(int fd, std::span<const char> bytes, uint64_t offset) {
  while (!bytes.empty()) {
    const auto n = ::pwrite(fd, bytes.data(), bytes.size(), static_cast<off_t>(offset));
    if (n < 0) { if (errno == EINTR) continue; io_error("pwrite"); }
    require(n > 0, "io_error", "pwrite made no progress");
    offset += static_cast<uint64_t>(n);
    bytes = bytes.subspan(static_cast<size_t>(n));
  }
}
void sync_file(int fd) {
  while (::fsync(fd) != 0) { if (errno != EINTR) io_error("fsync"); }
}
void append_u32(std::string &out, uint32_t value) {
  for (unsigned i = 0; i < 4; ++i) out += static_cast<char>((value >> (i * 8U)) & 255U);
}
void append_u64(std::string &out, uint64_t value) {
  for (unsigned i = 0; i < 8; ++i) out += static_cast<char>((value >> (i * 8U)) & 255U);
}
std::string_view read_bytes(std::string_view &bytes, size_t size) {
  require(bytes.size() >= size, "corrupt_data", "truncated storage record");
  auto result = bytes.substr(0, size); bytes.remove_prefix(size); return result;
}
uint32_t read_u32(std::string_view &bytes) {
  auto input = read_bytes(bytes, 4); uint32_t out = 0;
  for (unsigned i = 0; i < 4; ++i) out |= static_cast<uint32_t>(static_cast<unsigned char>(input[i])) << (8U * i);
  return out;
}
uint64_t read_u64(std::string_view &bytes) {
  auto input = read_bytes(bytes, 8); uint64_t out = 0;
  for (unsigned i = 0; i < 8; ++i) out |= static_cast<uint64_t>(static_cast<unsigned char>(input[i])) << (8U * i);
  return out;
}
void append_string(std::string &out, std::string_view value) {
  require(value.size() <= UINT32_MAX, "resource_limit", "storage value too large");
  append_u32(out, static_cast<uint32_t>(value.size())); out += value;
}
std::string read_string(std::string_view &bytes) {
  const auto length = read_u32(bytes); return std::string(read_bytes(bytes, length));
}

Storage::Storage(const std::string &directory) : directory_(directory) {
  std::filesystem::create_directories(directory);
  try {
    dir_fd_ = ::open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (dir_fd_ < 0) io_error("open data directory");
    lock_fd_ = ::openat(dir_fd_, "LOCK", O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (lock_fd_ < 0) io_error("open lock");
    require(::flock(lock_fd_, LOCK_EX | LOCK_NB) == 0, "data_locked", "data directory is in use");
    data_fd_ = ::openat(dir_fd_, "data", O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (data_fd_ < 0) io_error("open data");
  } catch (...) {
    close_fd(data_fd_); close_fd(lock_fd_); close_fd(dir_fd_); throw;
  }
}
Storage::~Storage() {
  unmap(); close_fd(data_fd_); close_fd(lock_fd_); close_fd(dir_fd_);
}
void Storage::unmap() {
  if (mapping_ != nullptr) ::munmap(const_cast<char *>(mapping_), mapping_size_);
  mapping_ = nullptr; mapping_size_ = 0;
}

std::string_view Storage::bytes(uint64_t offset, size_t length) {
  require(offset <= end_ && length <= end_ - offset, "corrupt_data", "offset beyond data boundary");
  if (offset + length > mapping_size_) {
    unmap();
    struct stat st {};
    if (::fstat(data_fd_, &st) != 0) io_error("fstat data");
    require(st.st_size >= 0 && static_cast<uint64_t>(st.st_size) >= offset + length,
            "corrupt_data", "missing data bytes");
    mapping_size_ = static_cast<size_t>(st.st_size);
    void *address = ::mmap(nullptr, mapping_size_, PROT_READ, MAP_SHARED, data_fd_, 0);
    if (address == MAP_FAILED) { mapping_size_ = 0; io_error("mmap"); }
    mapping_ = static_cast<const char *>(address);
  }
  return {mapping_ + offset, length};
}

// Commit records share the data file. A checksum-protected catalog and footer
// are appended after all data/pages, then one fsync orders and persists both.
// Recovery scans backward in bounded chunks: a torn/absent last footer cannot
// hide an older complete commit. No data file entry is recreated during commit.
std::string Storage::latest_catalog() {
  struct stat st {};
  if (::fstat(data_fd_, &st) != 0) io_error("fstat commit data");
  require(st.st_size >= 0, "corrupt_data", "negative data size");
  uint64_t high = static_cast<uint64_t>(st.st_size);
  constexpr uint64_t chunk_size = 1024U * 1024U;
  while (high > 16) {
    const uint64_t low = high > chunk_size + 16 ? high - chunk_size : 16;
    std::string chunk(static_cast<size_t>(high - low), '\0');
    require(read_at(data_fd_, chunk.data(), chunk.size(), low), "corrupt_data", "truncated commit scan");
    size_t search = chunk.size();
    while (true) {
      const size_t found = chunk.rfind(commit_magic, search);
      if (found == std::string::npos) break;
      const uint64_t marker = low + found;
      if (marker >= 16 + 40) {
        std::array<char, 40> footer{};
        require(read_at(data_fd_, footer.data(), footer.size(), marker - 40),
                "corrupt_data", "truncated commit footer");
        std::string_view fields(footer.data(), footer.size());
        const uint64_t offset = read_u64(fields), size = read_u64(fields), expected = read_u64(fields);
        const uint64_t previous = read_u64(fields), data_checksum = read_u64(fields);
        if (offset >= 16 && size >= 36 && size <= 4U * 1024U * 1024U &&
            offset <= marker - 40 && size == marker - 40 - offset &&
            previous >= 16 && previous <= offset) {
          std::string contents(static_cast<size_t>(size), '\0');
          require(read_at(data_fd_, contents.data(), contents.size(), offset),
                  "corrupt_data", "truncated commit catalog");
          if (checksum(contents) == expected && contents.starts_with(catalog_magic)) {
            std::string_view header(contents); header.remove_prefix(catalog_magic.size());
            if (read_u64(header) == marker + commit_magic.size() &&
                checksum_region(data_fd_, previous, offset) == data_checksum) return contents;
          }
        }
      }
      if (found == 0) break;
      search = found - 1;
    }
    if (low == 16) break;
    high = low + commit_magic.size() - 1;  // include signatures crossing a chunk edge
  }
  return {};
}

Catalog Storage::load() {
  fault("recovery_start");
  Catalog catalog;
  std::string journal = latest_catalog();
  const bool journal_format = !journal.empty();
  const int fd = journal.empty() ? ::openat(dir_fd_, "catalog", O_RDONLY | O_CLOEXEC) : -1;
  if (fd < 0 && journal.empty()) {
    if (errno != ENOENT) io_error("open catalog");
    end_ = data_magic.size();
    write_all(data_fd_, std::span(data_magic.data(), data_magic.size()), 0);
    if (::ftruncate(data_fd_, static_cast<off_t>(end_)) != 0) io_error("initialize data");
    publish(catalog);
    // The persistent data inode must exist durably before the first ready.
    sync_file(dir_fd_);
  } else {
    std::string contents = std::move(journal);
    if (fd >= 0) try {
      struct stat st {};
      if (::fstat(fd, &st) != 0) io_error("fstat catalog");
      require(st.st_size >= 32 && st.st_size <= 4 * 1024 * 1024,
              "corrupt_data", "invalid catalog size");
      contents.resize(static_cast<size_t>(st.st_size));
      size_t done = 0;
      while (done < contents.size()) {
        auto n = ::read(fd, contents.data() + done, contents.size() - done);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) io_error("read catalog");
        require(n > 0, "corrupt_data", "truncated catalog");
        done += static_cast<size_t>(n);
      }
      close_fd(fd);
    } catch (...) { close_fd(fd); throw; }
    std::string_view view(contents);
    require(read_bytes(view, catalog_magic.size()) == catalog_magic, "corrupt_data", "invalid catalog magic");
    end_ = read_u64(view);
    const auto expected = read_u64(view);
    require(checksum(view) == expected, "corrupt_data", "catalog checksum mismatch");
    const auto count = read_u32(view);
    require(count <= 4096, "corrupt_data", "invalid table count");
    for (uint32_t i = 0; i < count; ++i) {
      std::string name = read_string(view);
      Table t; t.root = read_u64(view); t.vector_root = read_u64(view);
      t.primary = read_u32(view); t.vector = std::bit_cast<int32_t>(read_u32(view));
      t.index_name = read_string(view);
      const auto columns = read_u32(view);
      require(columns > 0 && columns <= 1024 && t.primary < columns &&
                  (t.vector == -1 || (t.vector >= 0 && static_cast<uint32_t>(t.vector) < columns)),
              "corrupt_data", "invalid schema");
      for (uint32_t j = 0; j < columns; ++j) {
        Column c; c.name = read_string(view);
        const auto type = read_u32(view);
        require(type <= static_cast<uint32_t>(Type::Vector), "corrupt_data", "invalid column type");
        c.type = static_cast<Type>(type); c.size = read_u32(view); c.primary = j == t.primary;
        t.columns.push_back(std::move(c));
      }
      require(catalog.emplace(name, std::move(t)).second, "corrupt_data", "duplicate table");
    }
    require(view.empty(), "corrupt_data", "trailing catalog bytes");
    require(end_ >= 16 && bytes(0, 16) == data_magic, "corrupt_data", "invalid data header");
    // Ensure even the committed tail is present, not merely the header.
    if (end_ > 16) (void)bytes(end_ - 1, 1);
  }
  fault("recovery_before_truncate");
  rewind(end_);
  writeback_end_ = end_;
  published_end_ = end_;
  fault("recovery_after_truncate");
  if (::unlinkat(dir_fd_, "catalog.next", 0) != 0 && errno != ENOENT) io_error("remove unpublished catalog");
  if (journal_format) guard_legacy_writer();
  // All anonymous query/merge spools are unlinked on creation. No replay is needed.
  fault("recovery_ready");
  return catalog;
}

void Storage::guard_legacy_writer() {
  if (legacy_guarded_) return;
  // Old binaries otherwise interpret an absent/stale external catalog as an
  // empty/older database and truncate the new log. Leave a durable format guard
  // in that exact path, but only AFTER a valid journal commit is durable.
  const int existing = ::openat(dir_fd_, "catalog", O_RDONLY | O_CLOEXEC);
  if (existing >= 0) {
    std::array<char, 16> prefix{};
    const bool guarded = read_at(existing, prefix.data(), prefix.size(), 0) &&
                         std::string_view(prefix.data(), prefix.size()) == legacy_guard.substr(0, 16);
    close_fd(existing);
    if (guarded) { legacy_guarded_ = true; return; }
  } else if (errno != ENOENT) io_error("read format guard");
  const int fd = ::openat(dir_fd_, "catalog", O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC, 0600);
  if (fd < 0) io_error("create format guard");
  try {
    write_all(fd, std::span(legacy_guard.data(), legacy_guard.size()), 0);
    sync_file(fd);
  } catch (...) { close_fd(fd); throw; }
  close_fd(fd);
  sync_file(dir_fd_);
  legacy_guarded_ = true;
}

void Storage::publish(const Catalog &catalog) {
  fault("before_data_flush");
  flush();
  if (::ftruncate(data_fd_, static_cast<off_t>(end_)) != 0) io_error("size data");
  fault("after_data_flush");
  std::string body;
  append_u32(body, static_cast<uint32_t>(catalog.size()));
  for (const auto &[name, t] : catalog) {
    append_string(body, name); append_u64(body, t.root); append_u64(body, t.vector_root);
    append_u32(body, t.primary); append_u32(body, std::bit_cast<uint32_t>(t.vector));
    append_string(body, t.index_name);
    append_u32(body, static_cast<uint32_t>(t.columns.size()));
    for (const auto &c : t.columns) {
      append_string(body, c.name); append_u32(body, static_cast<uint32_t>(c.type)); append_u32(body, c.size);
    }
  }
  require(body.size() <= 4U * 1024U * 1024U - 32U, "resource_limit", "local catalog exceeds 4 MiB");
  const uint64_t start = end_;
  const uint64_t committed_end = start + 32 + body.size() + commit_footer_size;
  std::string header(catalog_magic); append_u64(header, committed_end);
  append_u64(header, checksum(body)); header += body;
  std::string footer;
  append_u64(footer, start); append_u64(footer, header.size());
  append_u64(footer, checksum(header)); append_u64(footer, published_end_);
  // Validate all newly reachable bytes as well as metadata. If an interrupted
  // sync writes the footer before some data pages, recovery rejects that record.
  append_u64(footer, checksum_region(data_fd_, published_end_, start)); footer += commit_magic;
  // Once a complete footer can reach disk, an I/O error has an uncertain
  // outcome. Terminate instead of reporting failure and continuing old roots.
  try {
    write_all(data_fd_, std::span(header.data(), header.size()), start);
    fault("after_commit_catalog");
    write_all(data_fd_, std::span(footer.data(), footer.size()), start + header.size());
    end_ = committed_end;
    fault("after_commit_footer");
    fault("after_catalog_write");
    sync_file(data_fd_);
    writeback_end_ = end_;
    published_end_ = end_;
    guard_legacy_writer();
    fault("after_data_sync");
    fault("after_catalog_sync");
  } catch (...) { ::_exit(74); }
  // Compatibility fault aliases for the former catalog-rename publication.
  // The actual commit point is the completed data-file fsync above.
  fault("after_catalog_rename");
  fault("after_directory_sync");
}

void Storage::rewind(uint64_t end) {
  unmap();
  for (auto it = cache_.begin(); it != cache_.end();) {
    if (it->first >= end) { lru_.erase(it->second.lru); it = cache_.erase(it); }
    else ++it;
  }
  if (::ftruncate(data_fd_, static_cast<off_t>(end)) != 0) io_error("truncate unpublished data");
  end_ = end;
  writeback_end_ = std::min(writeback_end_, end_);
}
void Storage::bound_dirty_pages() {
  // A transaction may be larger than the container memory limit. Drain dirty
  // file pages while its roots remain unpublished; durability of bytes is not
  // visibility of a transaction. The durable commit footer publishes its roots.
  if (end_ - writeback_end_ >= 8U * 1024U * 1024U) {
    sync_file(data_fd_);
    writeback_end_ = end_;
    fault("after_early_writeback");
  }
}
uint64_t Storage::blob(std::string_view value) {
  require(value.size() <= UINT32_MAX, "resource_limit", "row exceeds storage limit");
  const auto offset = end_;
  std::string encoded; encoded.reserve(4 + value.size());
  append_u32(encoded, static_cast<uint32_t>(value.size())); encoded += value;
  fault("before_blob_write");
  write_all(data_fd_, std::span(encoded.data(), encoded.size()), end_);
  end_ += encoded.size();
  bound_dirty_pages();
  fault("after_blob_write");
  return offset;
}
std::string_view Storage::blob(uint64_t offset) {
  auto head = bytes(offset, 4); const auto size = read_u32(head);
  return bytes(offset + 4, size);
}

void Storage::flush_node(uint64_t offset, Cached &cached) {
  if (!cached.dirty) return;
  std::string page; page.reserve(page_size);
  append_u32(page, cached.node.leaf ? 1 : 0);
  append_u32(page, static_cast<uint32_t>(cached.node.entries.size()));
  append_u64(page, 0);
  for (const auto &e : cached.node.entries) {
    append_u64(page, std::bit_cast<uint64_t>(e.key)); append_u64(page, e.ref);
  }
  page.resize(page_size, '\0');
  write_all(data_fd_, std::span(page.data(), page.size()), offset);
  cached.dirty = false;
  bound_dirty_pages();
}
void Storage::flush() { for (auto &[offset, value] : cache_) flush_node(offset, value); }
void Storage::cache_node(uint64_t offset, Node value, bool dirty) {
  auto existing = cache_.find(offset);
  if (existing != cache_.end()) {
    lru_.erase(existing->second.lru); cache_.erase(existing);
  }
  while (cache_.size() >= cache_pages) {
    auto it = cache_.find(lru_.back());
    flush_node(it->first, it->second); lru_.pop_back(); cache_.erase(it);
  }
  lru_.push_front(offset);
  cache_.emplace(offset, Cached{std::move(value), dirty, lru_.begin()});
}
Storage::Node Storage::node(uint64_t offset) {
  auto found = cache_.find(offset);
  if (found != cache_.end()) {
    lru_.splice(lru_.begin(), lru_, found->second.lru); return found->second.node;
  }
  auto data = bytes(offset, page_size);
  Node result; const auto leaf = read_u32(data); const auto size = read_u32(data);
  require(leaf <= 1 && size > 0 && size <= capacity, "corrupt_data", "invalid B+ tree node");
  result.leaf = leaf == 1; (void)read_u64(data);
  result.entries.reserve(size);
  for (uint32_t i = 0; i < size; ++i) {
    const int64_t key = std::bit_cast<int64_t>(read_u64(data));
    const uint64_t ref = read_u64(data);
    require(i == 0 || result.entries.back().key < key, "corrupt_data", "unordered B+ tree keys");
    result.entries.push_back({key, ref});
  }
  cache_node(offset, result, false); return result;
}
uint64_t Storage::save_node(uint64_t offset, Node value, uint64_t boundary) {
  if (offset < boundary) { offset = end_; end_ += page_size; }
  cache_node(offset, std::move(value), true); return offset;
}
uint64_t Storage::find(uint64_t root, int64_t key) {
  while (root != 0) {
    Node n = node(root);
    const auto it = std::upper_bound(n.entries.begin(), n.entries.end(), key,
                                    [](int64_t k, const Entry &e) { return k < e.key; });
    if (it == n.entries.begin()) return 0;
    const auto &entry = *std::prev(it);
    if (n.leaf) return entry.key == key ? entry.ref : 0;
    root = entry.ref;
  }
  return 0;
}
Storage::Split Storage::insert(uint64_t root, int64_t key, uint64_t value, uint64_t boundary) {
  Node n = node(root);
  bool appended = false;
  if (n.leaf) {
    auto it = std::lower_bound(n.entries.begin(), n.entries.end(), key,
                               [](const Entry &e, int64_t k) { return e.key < k; });
    if (it != n.entries.end() && it->key == key) it->ref = value;
    else {
      appended = it == n.entries.end();
      n.entries.insert(it, {key, value});
    }
  } else {
    auto it = std::upper_bound(n.entries.begin(), n.entries.end(), key,
                               [](int64_t k, const Entry &e) { return k < e.key; });
    size_t index = it == n.entries.begin() ? 0 : static_cast<size_t>(std::prev(it) - n.entries.begin());
    Split child = insert(n.entries[index].ref, key, value, boundary);
    n.entries[index] = child.left;
    if (child.has_right) {
      appended = index + 1 == n.entries.size();
      n.entries.insert(n.entries.begin() + static_cast<ptrdiff_t>(index + 1), child.right);
    }
  }
  Split result{};
  if (n.entries.size() <= capacity) {
    const auto first = n.entries.front().key;
    result.left = {first, save_node(root, std::move(n), boundary)};
  } else {
    Node right; right.leaf = n.leaf;
    // A new rightmost entry can start a right sibling without leaving a
    // half-empty historical page on every ascending split.
    const size_t mid = appended ? capacity : n.entries.size() / 2;
    right.entries.assign(n.entries.begin() + static_cast<ptrdiff_t>(mid), n.entries.end());
    n.entries.resize(mid);
    const auto first = n.entries.front().key;
    const auto second = right.entries.front().key;
    result.left = {first, save_node(root, std::move(n), boundary)};
    result.right = {second, save_node(0, std::move(right), boundary)};
    result.has_right = true;
  }
  return result;
}
uint64_t Storage::put(uint64_t root, int64_t key, uint64_t value, uint64_t boundary) {
  if (root == 0) {
    Node n; n.entries.push_back({key, value}); return save_node(0, std::move(n), boundary);
  }
  Split result = insert(root, key, value, boundary);
  if (!result.has_right) return result.left.ref;
  Node n; n.leaf = false; n.entries = {result.left, result.right};
  return save_node(0, std::move(n), boundary);
}
void Storage::scan(uint64_t root, const std::function<bool(int64_t, uint64_t)> &visit) {
  const auto recurse = [&](auto &&self, uint64_t offset, unsigned depth) -> bool {
    require(depth < 32, "corrupt_data", "invalid B+ tree depth");
    if (offset == 0) return true;
    const Node n = node(offset);
    for (const auto &entry : n.entries) {
      if (n.leaf) {
        if (entry.ref != 0 && !visit(entry.key, entry.ref)) return false;
      } else if (!self(self, entry.ref, depth + 1)) return false;
    }
    return true;
  };
  recurse(recurse, root, 0);
}

}  // namespace mvdb
