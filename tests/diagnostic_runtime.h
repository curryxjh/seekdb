#pragma once
// Included only in generated source copies under build/. Not a product feature.
#include <array>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <new>

namespace diag {
#define DIAG_STAGES(X) \
  X(request) X(json_parse) X(sql_parse) X(execute) X(output) \
  X(query_init) X(cache_metadata) X(cache_codes) X(filter) X(query_quantize) \
  X(candidate_materialize) X(int8_batch) X(seed_heap) X(seed_refine) \
  X(integer_scan) X(final_sort) X(projection) \
  X(vector_convert) X(row_build) X(tree_find) X(tree_put) X(row_encode) \
  X(blob_write) X(commit) X(flush) X(commit_checksum) X(fsync) X(pwrite) \
  X(float_screen) X(compensated) X(final_heap) X(vector_view)
#define DIAG_ENUM(x) x,
enum Stage { DIAG_STAGES(DIAG_ENUM) stage_count };
#undef DIAG_ENUM
#define DIAG_NAME(x) #x,
inline const char *stage_names[] = { DIAG_STAGES(DIAG_NAME) };
#undef DIAG_NAME
#define DIAG_EVENTS(X) \
  X(eligible) X(coded) X(seed_fill) X(seed_replace) X(seed_pop) \
  X(integer_checks) X(integer_reject) X(integer_pass) X(seed_skipped) \
  X(float_checks) X(float_reject) X(refines) X(final_fill) X(final_replace) \
  X(vector_accesses) X(vector_logical_bytes) X(filter_hit) X(filter_miss) \
  X(rows_constructed) X(vector_components) X(node_cache_copy) X(node_copy_bytes) \
  X(blob_copy) X(blob_copy_bytes) X(pwrite_calls) X(pwrite_bytes) X(checksum_bytes) \
  X(new_calls) X(new_bytes) X(shadow_checks) X(shadow_reject) \
  X(shadow_heap_update) X(shadow_cutoff_changes) X(kernel_scalar) X(kernel_avx2) X(kernel_vnni)
#define DIAG_ENUM(x) x,
enum Event { DIAG_EVENTS(DIAG_ENUM) event_count };
#undef DIAG_ENUM
#define DIAG_NAME(x) #x,
inline const char *event_names[] = { DIAG_EVENTS(DIAG_NAME) };
#undef DIAG_NAME
struct Slot { uint64_t calls=0, samples=0, ns=0, self_ns=0; };
inline std::array<Slot, stage_count> slots;
inline std::array<uint64_t, stage_count> serials{};
inline std::array<uint64_t, event_count> events{};
inline bool active=false;
// coarse: coarse clocks only; events: counts + 1/64 sparse clocks;
// shadow: events + observational cutoff; alloc: events + operator-new counts.
inline const char *mode = [] {
  const char *value = std::getenv("MVDB_DIAG_MODE");
  return value ? value : "events";
}();
inline bool detailed = std::strcmp(mode, "coarse") != 0;
inline bool shadow = std::strcmp(mode, "shadow") == 0;
inline bool allocations = std::strcmp(mode, "alloc") == 0;
inline uint64_t sample_mask = [] {
  const char *value=std::getenv("MVDB_DIAG_SAMPLE_STRIDE");
  const uint64_t stride=value ? std::strtoull(value, nullptr, 10) : 64;
  if (stride == 0 || (stride & (stride-1)) != 0) std::abort();
  return stride-1;
}();
inline uint64_t sequence=0;
inline uint64_t now() {
  return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count());
}
inline void count(Event event, uint64_t n=1) { if (active && detailed) events[event] += n; }
struct Scope;
inline Scope *parent=nullptr;
struct Scope {
  Stage stage;
  uint64_t start=0, child=0;
  Scope *previous=nullptr;
  bool running=false;
  explicit Scope(Stage value, bool sparse=false) : stage(value) {
    if (!active || (sparse && !detailed)) return;
    auto &slot=slots[stage]; ++slot.calls;
    if (sparse && ((serials[stage]++) & sample_mask) != 0) return;
    ++slot.samples;
    running=true; previous=parent; parent=this; start=now();
  }
  Scope(const Scope &)=delete;
  void stop() {
    if (!running) return;
    const auto elapsed=now()-start;
    auto &slot=slots[stage]; slot.ns+=elapsed;
    slot.self_ns+=elapsed >= child ? elapsed-child : 0;
    parent=previous;
    if (previous) previous->child+=elapsed;
    running=false;
  }
  ~Scope() { stop(); }
};
inline void begin() {
  slots={}; events={}; parent=nullptr; ++sequence; active=true;
}
inline void finish() {
  active=false;
  // stdout remains exactly the official protocol; the host drains stderr.
  std::fprintf(stderr, "@diag {\"sequence\":%llu,\"mode\":\"%s\",\"stride\":%llu,\"timers\":{",
               static_cast<unsigned long long>(sequence), mode,
               static_cast<unsigned long long>(sample_mask+1));
  bool first=true;
  for (size_t i=0; i<stage_count; ++i) {
    const auto &s=slots[i]; if (!s.calls) continue;
    std::fprintf(stderr, "%s\"%s\":{\"calls\":%llu,\"samples\":%llu,\"ns\":%llu,\"self_ns\":%llu}",
        first ? "" : ",", stage_names[i],
        static_cast<unsigned long long>(s.calls), static_cast<unsigned long long>(s.samples),
        static_cast<unsigned long long>(s.ns), static_cast<unsigned long long>(s.self_ns));
    first=false;
  }
  std::fputs("},\"events\":{", stderr); first=true;
  for (size_t i=0; i<event_count; ++i) {
    if (!events[i]) continue;
    std::fprintf(stderr, "%s\"%s\":%llu", first ? "" : ",", event_names[i],
                 static_cast<unsigned long long>(events[i])); first=false;
  }
  std::fputs("}}\n", stderr); std::fflush(stderr);
}
inline void allocated(size_t n) {
  if (active && allocations) { ++events[new_calls]; events[new_bytes]+=n; }
}
}  // namespace diag
