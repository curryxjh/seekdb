"""Generate instrumented source copies; fail closed if a source anchor changes.

Production sources, compiler options, algorithms and official fixtures are not
edited. The manifest records each exact replacement and both source hashes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="build/diagnostics")
    parser.add_argument("--image", default="minivectordb:diagnostic-nov16")
    parser.add_argument("--alloc", action="store_true")
    args = parser.parse_args()
    target = ROOT / args.output
    assert target.resolve().is_relative_to((ROOT / "build").resolve())
    target.mkdir(parents=True, exist_ok=True)
    source = target / "minivectordb"
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(ROOT / "minivectordb", source)
    shutil.copy(ROOT / "tests/diagnostic_runtime.h", source / "diagnostic_runtime.h")
    changes = []

    def replace(file, old, new, times=1):
        path = source / file
        text = path.read_text()
        assert text.count(old) == times, (file, old[:120], text.count(old), times)
        path.write_text(text.replace(old, new))
        changes.append({"file": file, "old": old, "new": new, "occurrences": times})

    for file in ["main.cpp", "database.cpp", "storage.cpp", "sql.cpp", "screen.cpp"]:
        path = source / file
        path.write_text('#include "diagnostic_runtime.h"\n' + path.read_text())
    replace("main.cpp", "      mvdb::Request request;",
            "      diag::begin(); diag::Scope request_scope(diag::request);\n      mvdb::Request request;")
    replace("main.cpp", "        request = mvdb::parse_request(line);",
            "        { diag::Scope timer(diag::json_parse); request = mvdb::parse_request(line); }")
    replace("main.cpp", "          return 0;", "          request_scope.stop(); diag::finish(); return 0;")
    replace("main.cpp", "        response = std::make_unique<mvdb::Spool>(database.execute(mvdb::parse(request.sql)));",
            """        mvdb::Statement statement;
        { diag::Scope timer(diag::sql_parse); statement=mvdb::parse(request.sql); }
        { diag::Scope timer(diag::execute);
          response = std::make_unique<mvdb::Spool>(database.execute(statement)); }""")
    replace("main.cpp", "        continue;", "        request_scope.stop(); diag::finish(); continue;")
    replace("main.cpp", "      response->output(stdout, request.id);",
            "      { diag::Scope timer(diag::output);\n      response->output(stdout, request.id);")
    replace("main.cpp", '      mvdb::fault("after_success_response");',
            '      mvdb::fault("after_success_response"); }\n      request_scope.stop(); diag::finish();')

    replace("database.cpp", "Spool Database::select(const Statement &s, const Table &t) {",
            "Spool Database::select(const Statement &s, const Table &t) {\n  diag::Scope init(diag::query_init);")
    replace("database.cpp", "  const auto emit = [&](const Candidate &value) {",
            "  const auto emit = [&](const Candidate &value) {\n    diag::Scope timer(diag::projection);")
    replace("database.cpp", "  const auto accept = [&](Candidate value) {",
            "  const auto accept = [&](Candidate value) {\n    diag::Scope timer(diag::final_heap, true);")
    replace("database.cpp", "if (top.size() < s.limit) top.push(std::move(value));",
            "if (top.size() < s.limit) { diag::count(diag::final_fill); top.push(std::move(value)); }")
    replace("database.cpp", "else if (better(value, top.top())) { top.pop(); top.push(std::move(value)); }",
            "else if (better(value, top.top())) { diag::count(diag::final_replace); top.pop(); top.push(std::move(value)); }")
    replace("database.cpp", "  RowView row(t);\n  const auto visit",
            "  init.stop();\n  RowView row(t);\n  const auto visit")
    replace("database.cpp", "    if (scan_root_ == 0 || scan_root_ != t.root) {\n      invalidate_scan();",
            "    if (scan_root_ == 0 || scan_root_ != t.root) {\n      diag::Scope metadata_timer(diag::cache_metadata);\n      invalidate_scan();")
    replace("database.cpp", "          scan_screen_.build(code_count, dimension,",
            "          metadata_timer.stop();\n          diag::Scope codes_timer(diag::cache_codes);\n          scan_screen_.build(code_count, dimension,")
    replace("database.cpp", "    const std::vector<uint64_t> *filter = nullptr;",
            "    diag::Scope filter_timer(diag::filter);\n    const std::vector<uint64_t> *filter = nullptr;")
    replace("database.cpp", "if (cached.key == key) { filter = &cached.bits; break; }",
            "if (cached.key == key) { diag::count(diag::filter_hit); filter = &cached.bits; break; }")
    replace("database.cpp", "        if (filter == nullptr) {\n          ScanFilter",
            "        if (filter == nullptr) {\n          diag::count(diag::filter_miss);\n          ScanFilter")
    replace("database.cpp", "    const auto screening_query = scan_screen_.query(order.query);",
            """    filter_timer.stop();
    diag::Scope quantize_timer(diag::query_quantize);
    const auto screening_query = scan_screen_.query(order.query);
    quantize_timer.stop();""")
    replace("database.cpp", "      const auto &cached = scan_rows_[i];\n      accept_vector",
            "      diag::count(diag::vector_accesses); diag::count(diag::vector_logical_bytes, dimension*4);\n      const auto &cached = scan_rows_[i];\n      diag::Scope view_timer(diag::vector_view, true);\n      const auto input = storage_.view(scan_vector_offsets_[i], dimension * sizeof(float));\n      view_timer.stop();\n      accept_vector")
    replace("database.cpp", "                    storage_.view(scan_vector_offsets_[i], dimension * sizeof(float)));",
            "                    input);")
    replace("database.cpp", "      if (where.empty()) {\n        coded_count",
            "      if (where.empty()) {\n        coded_count")
    replace("database.cpp", "        distances = scan_screen_.code_distances(coded_count, screening_query);",
            "        diag::Scope batch_timer(diag::int8_batch);\n        distances = scan_screen_.code_distances(coded_count, screening_query);")
    replace("database.cpp", "        rows.reserve(scan_rows_.size());",
            "        diag::Scope materialize_timer(diag::candidate_materialize);\n        rows.reserve(scan_rows_.size());")
    replace("database.cpp", "        distances = scan_screen_.code_distances(std::span<const uint32_t>(rows.data(), coded_count), screening_query);",
            """        materialize_timer.stop();
        diag::Scope batch_timer(diag::int8_batch);
        distances = scan_screen_.code_distances(std::span<const uint32_t>(rows.data(), coded_count), screening_query);""")
    replace("database.cpp", "      using Seed = std::pair<int32_t, size_t>;",
            """      diag::count(diag::eligible, where.empty() ? scan_rows_.size() : rows.size());
      diag::count(diag::coded, coded_count);
      diag::Scope seed_timer(diag::seed_heap);
      using Seed = std::pair<int32_t, size_t>;""")
    replace("database.cpp", "if (seeds.size() < s.limit) seeds.push(seed);",
            "if (seeds.size() < s.limit) { diag::count(diag::seed_fill); seeds.push(seed); }")
    replace("database.cpp", "else if (seed < seeds.top()) { seeds.pop(); seeds.push(seed); }",
            "else if (seed < seeds.top()) { diag::count(diag::seed_replace); seeds.pop(); seeds.push(seed); }")
    replace("database.cpp", "      if (!seeds.empty()) {\n        const Seed last_seed",
            "      seed_timer.stop();\n      if (!seeds.empty()) {\n        const Seed last_seed")
    replace("database.cpp", "        while (!seeds.empty()) { refine(row_at(seeds.top().second)); seeds.pop(); }",
            """        { diag::Scope seed_refine_timer(diag::seed_refine);
          while (!seeds.empty()) { diag::count(diag::seed_pop); refine(row_at(seeds.top().second)); seeds.pop(); } }
        diag::Scope scan_timer(diag::integer_scan);""")
    replace("database.cpp", "          if (rank > last_seed && (s.descending ? distances[i] >= cutoff : distances[i] <= cutoff)) refine(row_at(i));",
            """          if (rank <= last_seed) { diag::count(diag::seed_skipped); continue; }
          diag::count(diag::integer_checks);
          if (s.descending ? distances[i] < cutoff : distances[i] > cutoff) {
            diag::count(diag::integer_reject); continue;
          }
          diag::count(diag::integer_pass);
          bool shadow_rejected=false;
          if (diag::shadow) {
            diag::count(diag::shadow_checks);
            const double current=std::get<double>(top.top().key);
            const double dynamic=scan_screen_.refined_cutoff(screening_query, current, s.descending);
            shadow_rejected=s.descending ? distances[i] < dynamic : distances[i] > dynamic;
            if (shadow_rejected) diag::count(diag::shadow_reject);
          }
          const auto updates=diag::events[diag::final_replace];
          refine(row_at(i));
          if (diag::shadow && shadow_rejected && diag::events[diag::final_replace] != updates)
            diag::count(diag::shadow_heap_update);""")
    replace("database.cpp", "        const double approximate = screen(input, order.query);",
            """        diag::count(diag::float_checks);
        diag::Scope float_timer(diag::float_screen, true);
        const double approximate = screen(input, order.query);
        float_timer.stop();""")
    replace("database.cpp", "approximate * (1.0 + screen_error) + screen_slack < accept_square))) return true;",
            "approximate * (1.0 + screen_error) + screen_slack < accept_square))) { diag::count(diag::float_reject); return true; }")
    replace("database.cpp", "    return accept({refined_distance(input, order.query), id, offset});",
            """    diag::count(diag::refines);
    diag::Scope refine_timer(diag::compensated, true);
    const double exact=refined_distance(input, order.query);
    refine_timer.stop();
    return accept({exact, id, offset});""")
    replace("database.cpp", "      std::vector<Candidate> values;",
            "      diag::Scope sort_timer(diag::final_sort);\n      std::vector<Candidate> values;")
    replace("database.cpp", "      for (const auto &value : values) emit(value);",
            "      sort_timer.stop();\n      for (const auto &value : values) emit(value);")
    replace("database.cpp", "          Row row(t.columns.size());",
            "          diag::count(diag::rows_constructed);\n          diag::Scope row_timer(diag::row_build, true);\n          Row row(t.columns.size());")
    replace("database.cpp", "          const auto id = std::get<int64_t>(row[t.primary]);",
            "          row_timer.stop();\n          const auto id = std::get<int64_t>(row[t.primary]);")
    replace("database.cpp", "uint64_t Database::encode(const Table &t, const Row &row) {",
            "uint64_t Database::encode(const Table &t, const Row &row) {\n  diag::Scope encode_timer(diag::row_encode, true);")
    replace("database.cpp", "  return storage_.blob(out);",
            "  encode_timer.stop();\n  return storage_.blob(out);")
    replace("database.cpp", "void Database::commit() {",
            "void Database::commit() {\n  diag::Scope commit_timer(diag::commit);")

    sql = (source / "sql.cpp").read_text()
    signature = next(line for line in sql.splitlines() if line.startswith("std::vector<float> vector_literal("))
    replace("sql.cpp", signature, signature + "\n  diag::Scope vector_timer(diag::vector_convert, true);")
    # Counts per vector, not per decimal component.
    replace("database.cpp", "  return vector_literal(value.text, c.size);",
            "  diag::count(diag::vector_components, c.size);\n  return vector_literal(value.text, c.size);")
    for signature, stage, sparse in [
        ("uint64_t Storage::find(uint64_t root, int64_t key) {", "tree_find", True),
        ("uint64_t Storage::put(uint64_t root, int64_t key, uint64_t value, uint64_t boundary) {", "tree_put", True),
        ("uint64_t Storage::blob(std::string_view value) {", "blob_write", True),
        ("uint64_t checksum_region(int fd, uint64_t begin, uint64_t end) {", "commit_checksum", False),
        ("void sync_file(int fd) {", "fsync", False),
        ("void write_all(int fd, std::span<const char> bytes, uint64_t offset) {", "pwrite", True)]:
        replace("storage.cpp", signature,
                signature + f"\n  diag::Scope timer(diag::{stage}, {'true' if sparse else 'false'});")
    replace("storage.cpp", "  std::array<char, 65536> buffer{};",
            "  diag::count(diag::checksum_bytes, end-begin);\n  std::array<char, 65536> buffer{};")
    replace("storage.cpp", "    const auto n = ::pwrite(fd, bytes.data(), bytes.size(), static_cast<off_t>(offset));",
            """    diag::count(diag::pwrite_calls);
    const auto n = ::pwrite(fd, bytes.data(), bytes.size(), static_cast<off_t>(offset));
    if (n>0) diag::count(diag::pwrite_bytes, static_cast<uint64_t>(n));""")
    replace("storage.cpp", "  std::string encoded; encoded.reserve(4 + value.size());",
            "  diag::count(diag::blob_copy); diag::count(diag::blob_copy_bytes, value.size());\n  std::string encoded; encoded.reserve(4 + value.size());")
    replace("storage.cpp", "    lru_.splice(lru_.begin(), lru_, found->second.lru); return found->second.node;",
            """    diag::count(diag::node_cache_copy);
    diag::count(diag::node_copy_bytes, found->second.node.entries.size()*sizeof(Entry));
    lru_.splice(lru_.begin(), lru_, found->second.lru); return found->second.node;""")
    replace("storage.cpp", "void Storage::flush() { for (auto &[offset, value] : cache_) flush_node(offset, value); }",
            "void Storage::flush() { diag::Scope timer(diag::flush); for (auto &[offset, value] : cache_) flush_node(offset, value); }")
    for invocation, event in [
        ("    distances_vnni(", "kernel_vnni"),
        ("    distances_avx2(", "kernel_avx2"),
        ("    distances_avx2_contiguous(", "kernel_avx2"),
        ("  distances_scalar(", "kernel_scalar"),
        ("  for (size_t row = 0; row < count; ++row) result[row] = code_distance(row, q);", "kernel_scalar")]:
        if invocation == "    distances_vnni(":
            replace("screen.cpp", invocation, f"    diag::count(diag::{event});\n" + invocation, 2)
        else:
            replace("screen.cpp", invocation, f"  diag::count(diag::{event});\n" + invocation)

    # Complete standard new/delete families used by these sources. Counting is
    # enabled only in the separate alloc run, and uses no allocating bookkeeping.
    (source / ("diagnostic_alloc.cpp" if args.alloc else "diagnostic_alloc.cpp.txt")).write_text(r'''
#include "diagnostic_runtime.h"
void *operator new(std::size_t n) {
  diag::allocated(n);
  if (void *p=std::malloc(n ? n : 1)) return p;
  throw std::bad_alloc();
}
void *operator new[](std::size_t n) { return ::operator new(n); }
void operator delete(void *p) noexcept { std::free(p); }
void operator delete[](void *p) noexcept { std::free(p); }
void operator delete(void *p, std::size_t) noexcept { std::free(p); }
void operator delete[](void *p, std::size_t) noexcept { std::free(p); }
void *operator new(std::size_t n, std::align_val_t a) {
  diag::allocated(n); void *p=nullptr;
  if (::posix_memalign(&p, static_cast<std::size_t>(a), n ? n : 1) == 0) return p;
  throw std::bad_alloc();
}
void *operator new[](std::size_t n, std::align_val_t a) { return ::operator new(n,a); }
void operator delete(void *p, std::align_val_t) noexcept { std::free(p); }
void operator delete[](void *p, std::align_val_t) noexcept { std::free(p); }
void operator delete(void *p, std::size_t, std::align_val_t) noexcept { std::free(p); }
void operator delete[](void *p, std::size_t, std::align_val_t) noexcept { std::free(p); }
''')
    shutil.copy(ROOT / "Dockerfile", target / "Dockerfile")
    manifest = {"source_sha256": {}, "generated_sha256": {}, "replacements": changes}
    for path in sorted((ROOT / "minivectordb").glob("*")):
        manifest["source_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(source.glob("*")):
        manifest["generated_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
    subprocess.run(["docker", "build", "--platform", "linux/amd64", "-t", args.image, str(target)], check=True)
    print(json.dumps({"image": args.image, "generated": str(target), "replacements": len(changes)}))


if __name__ == "__main__":
    main()
