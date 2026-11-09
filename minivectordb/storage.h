#pragma once

#include "sql.h"

#include <array>
#include <bit>
#include <functional>
#include <list>
#include <map>
#include <span>
#include <unordered_map>

namespace mvdb {

void fault(std::string_view name);
void write_all(int fd, std::span<const char> bytes, uint64_t offset);
void sync_file(int fd);
void append_u32(std::string &out, uint32_t value);
void append_u64(std::string &out, uint64_t value);
uint32_t read_u32(std::string_view &bytes);
uint64_t read_u64(std::string_view &bytes);
std::string_view read_bytes(std::string_view &bytes, size_t size);
void append_string(std::string &out, std::string_view value);
std::string read_string(std::string_view &bytes);

// Caller validates the complete vector span once with read_bytes.
inline float load_float(const char *data) {
  const auto *p = reinterpret_cast<const unsigned char *>(data);
  const uint32_t bits = static_cast<uint32_t>(p[0]) |
                        (static_cast<uint32_t>(p[1]) << 8U) |
                        (static_cast<uint32_t>(p[2]) << 16U) |
                        (static_cast<uint32_t>(p[3]) << 24U);
  return std::bit_cast<float>(bits);
}

struct Table {
  std::vector<Column> columns;
  uint32_t primary = 0;
  int32_t vector = -1;
  uint64_t root = 0;
  uint64_t vector_root = 0;
  std::string index_name;
};
using Catalog = std::map<std::string, Table>;

class Storage {
 public:
  explicit Storage(const std::string &directory);
  ~Storage();
  Storage(const Storage &) = delete;
  Storage &operator=(const Storage &) = delete;
  uint64_t end() const { return end_; }
  const std::string &directory() const { return directory_; }
  Catalog load();
  void publish(const Catalog &catalog);
  void rewind(uint64_t end);
  uint64_t blob(std::string_view bytes);
  std::string_view blob(uint64_t offset);
  uint64_t find(uint64_t root, int64_t key);
  uint64_t put(uint64_t root, int64_t key, uint64_t value, uint64_t boundary);
  void scan(uint64_t root, const std::function<bool(int64_t, uint64_t)> &visit);
 private:
  struct Entry { int64_t key; uint64_t ref; };
  struct Node { bool leaf = true; std::vector<Entry> entries; };
  struct Cached {
    Node node;
    bool dirty = false;
    std::list<uint64_t>::iterator lru;
  };
  struct Split { Entry left; bool has_right = false; Entry right{}; };
  static constexpr size_t page_size = 4096;
  static constexpr size_t capacity = (page_size - 16) / 16;
  static constexpr size_t cache_pages = 512;
  std::string directory_;
  int dir_fd_ = -1;
  int lock_fd_ = -1;
  int data_fd_ = -1;
  uint64_t end_ = 16;
  const char *mapping_ = nullptr;
  size_t mapping_size_ = 0;
  std::unordered_map<uint64_t, Cached> cache_;
  std::list<uint64_t> lru_;
  std::string_view bytes(uint64_t offset, size_t length);
  void unmap();
  Node node(uint64_t offset);
  void cache_node(uint64_t offset, Node value, bool dirty);
  void flush_node(uint64_t offset, Cached &cached);
  void flush();
  uint64_t save_node(uint64_t offset, Node value, uint64_t boundary);
  Split insert(uint64_t root, int64_t key, uint64_t value, uint64_t boundary);
};

}  // namespace mvdb
